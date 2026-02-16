"""
PumpSwap Direct Adapter for Fathom.

Executes swaps directly on PumpSwap AMM pools — no aggregator overhead.
This is the fastest execution path for freshly graduated pump.fun tokens.

PumpSwap is pump.fun's native AMM. When a token graduates from the bonding
curve, it creates a PumpSwap pool. This adapter interacts with that pool
directly for minimum latency.

Supports:
- Direct pool swaps (buy/sell)
- Pool state reading (reserves, price, liquidity)
- Jito bundle submission for MEV protection
- Priority fee estimation
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import struct
import time
from pathlib import Path
from typing import Any

import aiohttp

from fathom.adapters.base import BaseAdapter
from fathom.core.events import Event, EventType, OrderUpdate

logger = logging.getLogger("fathom.pumpswap")

# PumpSwap program
PUMPSWAP_PROGRAM = "PSwapMdSai8tjrEXcxFeQth87xC4rRsa4VA5mhGhXkP"

# Jito block engine for bundle submission
JITO_BLOCK_ENGINE = "https://mainnet.block-engine.jito.wtf/api/v1/bundles"

# SOL mint
WSOL_MINT = "So11111111111111111111111111111111111111112"


class PumpSwapAdapter(BaseAdapter):
    """
    Direct swap execution on PumpSwap AMM.
    
    For freshly graduated tokens, this is significantly faster than
    routing through Jupiter because:
    1. No quote API call needed — we read pool state directly
    2. No route optimization overhead — there's only one pool
    3. Transaction is simpler — single swap instruction
    
    For MEV protection, transactions can be submitted as Jito bundles
    with a tip, ensuring they land without being sandwiched.
    
    Args:
        rpc_url: Solana RPC endpoint
        wallet_path: Path to Solana keypair JSON
        slippage_bps: Default slippage tolerance
        use_jito: Submit via Jito bundles for MEV protection
        jito_tip_lamports: Tip amount for Jito validators
        priority_fee_lamports: Compute unit price
    """
    
    name = "pumpswap"
    
    def __init__(
        self,
        rpc_url: str,
        wallet_path: str | Path | None = None,
        slippage_bps: int = 300,
        use_jito: bool = True,
        jito_tip_lamports: int = 100_000,   # 0.0001 SOL
        priority_fee_lamports: int = 50_000,
        max_retries: int = 2,
    ) -> None:
        super().__init__()
        self.rpc_url = rpc_url
        self.wallet_path = Path(wallet_path) if wallet_path else None
        self.slippage_bps = slippage_bps
        self.use_jito = use_jito
        self.jito_tip_lamports = jito_tip_lamports
        self.priority_fee_lamports = priority_fee_lamports
        self.max_retries = max_retries
        self._session: aiohttp.ClientSession | None = None
        self._wallet_pubkey: str | None = None
        self._keypair_bytes: bytes | None = None
        self._tx_count: int = 0
        self._total_volume_usd: float = 0.0
        self._jito_bundles_sent: int = 0

    async def connect(self) -> None:
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15),
        )
        
        if self.wallet_path and self.wallet_path.exists():
            self._load_wallet()
            logger.info(
                f"PumpSwap adapter connected | wallet={self._wallet_pubkey[:8]}... | "
                f"jito={'ON' if self.use_jito else 'OFF'}"
            )
        else:
            logger.info("PumpSwap adapter connected (read-only)")
        
        self._connected = True
        
        if self._event_bus:
            self._event_bus.subscribe(EventType.ORDER_SUBMITTED, self._handle_order)
            self._event_bus.publish(Event(
                event_type=EventType.ADAPTER_CONNECTED,
                source=self.name,
            ))

    async def disconnect(self) -> None:
        if self._session:
            await self._session.close()
        self._connected = False
        logger.info(
            f"PumpSwap adapter stopped | txs={self._tx_count} "
            f"jito_bundles={self._jito_bundles_sent} "
            f"volume=${self._total_volume_usd:,.2f}"
        )

    async def get_pool_state(self, pool_address: str) -> PoolState | None:
        """
        Read current state of a PumpSwap pool.
        
        Returns reserves, current price, and liquidity info.
        This is a direct RPC call — no external API dependency.
        """
        if not self._session:
            return None
        
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getAccountInfo",
            "params": [
                pool_address,
                {"encoding": "base64", "commitment": "confirmed"},
            ],
        }
        
        try:
            async with self._session.post(self.rpc_url, json=payload) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
            
            account = data.get("result", {}).get("value")
            if not account:
                return None
            
            account_data = base64.b64decode(account["data"][0])
            return self._decode_pool_state(account_data, pool_address)
            
        except Exception as e:
            logger.debug(f"Pool state fetch failed: {e}")
            return None

    async def submit_order(self, order: dict) -> str:
        """
        Execute a swap on PumpSwap.
        
        Flow:
        1. Read pool state to get current reserves
        2. Calculate expected output with slippage
        3. Build swap instruction
        4. Submit via Jito bundle (if enabled) or direct RPC
        """
        pool_address = order.get("pool_address", "")
        side = order.get("side", "buy")
        amount = order.get("amount", 0)
        slippage = order.get("slippage_bps", self.slippage_bps)
        
        if not pool_address:
            raise PumpSwapError("No pool address provided")
        
        if not self._wallet_pubkey:
            raise PumpSwapError("No wallet configured")
        
        # Get current pool state
        pool = await self.get_pool_state(pool_address)
        if not pool:
            raise PumpSwapError(f"Could not read pool state: {pool_address}")
        
        # Calculate swap amounts
        if side == "buy":
            # Buying token with SOL
            amount_in = amount  # SOL amount in lamports
            expected_out = self._calculate_output(
                amount_in, pool.sol_reserves, pool.token_reserves
            )
            min_out = int(expected_out * (1 - slippage / 10000))
        else:
            # Selling token for SOL
            amount_in = amount  # Token amount
            expected_out = self._calculate_output(
                amount_in, pool.token_reserves, pool.sol_reserves
            )
            min_out = int(expected_out * (1 - slippage / 10000))
        
        # Build transaction
        tx = self._build_swap_tx(
            pool_address=pool_address,
            side=side,
            amount_in=amount_in,
            min_amount_out=min_out,
        )
        
        # Submit
        last_error = None
        for attempt in range(self.max_retries):
            try:
                if self.use_jito:
                    sig = await self._submit_jito_bundle(tx)
                    self._jito_bundles_sent += 1
                else:
                    sig = await self._submit_rpc(tx)
                
                self._tx_count += 1
                logger.info(
                    f"PumpSwap {side} executed | pool={pool_address[:8]}... | "
                    f"attempt={attempt + 1}"
                )
                return sig
                
            except Exception as e:
                last_error = e
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(0.5 * (attempt + 1))
        
        raise PumpSwapError(f"Swap failed after {self.max_retries} attempts: {last_error}")

    def _calculate_output(
        self, amount_in: int, reserve_in: int, reserve_out: int
    ) -> int:
        """
        Constant-product AMM output calculation.
        
        output = (amount_in * reserve_out) / (reserve_in + amount_in)
        
        Accounts for the 0.25% PumpSwap fee.
        """
        fee_bps = 25  # 0.25% fee
        amount_after_fee = amount_in * (10000 - fee_bps) // 10000
        
        numerator = amount_after_fee * reserve_out
        denominator = reserve_in + amount_after_fee
        
        if denominator == 0:
            return 0
        
        return numerator // denominator

    def _build_swap_tx(
        self,
        pool_address: str,
        side: str,
        amount_in: int,
        min_amount_out: int,
    ) -> bytes:
        """
        Build a PumpSwap swap transaction.
        
        TODO: Full implementation requires:
        1. Derive pool token accounts (PDAs)
        2. Build swap instruction with correct accounts
        3. Add compute budget instructions
        4. Add Jito tip instruction (if using bundles)
        5. Sign with wallet keypair
        
        This is the main area that needs Solana SDK integration.
        """
        # Placeholder — real implementation needs solders/solana-py
        # The instruction layout for PumpSwap swap is:
        # [discriminator(8)] [amount_in(u64)] [min_amount_out(u64)]
        
        logger.debug(
            f"Building swap tx | pool={pool_address[:8]} | "
            f"side={side} | in={amount_in} | min_out={min_amount_out}"
        )
        
        # Return empty bytes as placeholder
        # Real implementation: use solders to build + sign
        return b""

    async def _submit_jito_bundle(self, tx: bytes) -> str:
        """
        Submit transaction as a Jito bundle for MEV protection.
        
        Jito bundles guarantee atomic execution without sandwich attacks.
        The tip goes to the validator as an incentive to include the bundle.
        """
        if not self._session:
            raise PumpSwapError("Not connected")
        
        # Jito bundle format
        bundle = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "sendBundle",
            "params": [
                [base64.b64encode(tx).decode()],
            ],
        }
        
        async with self._session.post(JITO_BLOCK_ENGINE, json=bundle) as resp:
            if resp.status != 200:
                error = await resp.text()
                raise PumpSwapError(f"Jito bundle failed ({resp.status}): {error}")
            
            data = await resp.json()
            bundle_id = data.get("result", "")
            logger.debug(f"Jito bundle submitted: {bundle_id}")
            return bundle_id

    async def _submit_rpc(self, tx: bytes) -> str:
        """Submit transaction directly via RPC."""
        if not self._session:
            raise PumpSwapError("Not connected")
        
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "sendTransaction",
            "params": [
                base64.b64encode(tx).decode(),
                {
                    "encoding": "base64",
                    "skipPreflight": True,
                    "maxRetries": 0,
                },
            ],
        }
        
        async with self._session.post(self.rpc_url, json=payload) as resp:
            data = await resp.json()
            
            if "error" in data:
                raise PumpSwapError(f"RPC error: {data['error']}")
            
            return data.get("result", "")

    def _load_wallet(self) -> None:
        """Load wallet keypair from JSON file."""
        if not self.wallet_path or not self.wallet_path.exists():
            return
        
        with open(self.wallet_path) as f:
            keypair_list = json.load(f)
        
        self._keypair_bytes = bytes(keypair_list)
        # Public key is last 32 bytes of 64-byte keypair
        pubkey_bytes = self._keypair_bytes[32:64]
        self._wallet_pubkey = base64.b58encode(pubkey_bytes).decode()

    def _decode_pool_state(self, data: bytes, pool_address: str) -> PoolState | None:
        """
        Decode PumpSwap pool account data.
        
        Pool account layout (approximate):
        [discriminator(8)] [bump(1)] [pool_type(1)] [token_mint(32)]
        [sol_reserves(u64)] [token_reserves(u64)] [lp_supply(u64)]
        [fee_bps(u16)] ...
        """
        try:
            if len(data) < 90:
                return None
            
            # Skip discriminator (8 bytes) + bump (1) + pool_type (1)
            offset = 10
            
            # Token mint (32 bytes)
            token_mint = base64.b58encode(data[offset:offset + 32]).decode()
            offset += 32
            
            # SOL reserves (u64, 8 bytes)
            sol_reserves = struct.unpack_from("<Q", data, offset)[0]
            offset += 8
            
            # Token reserves (u64, 8 bytes)
            token_reserves = struct.unpack_from("<Q", data, offset)[0]
            offset += 8
            
            # LP supply (u64, 8 bytes)
            lp_supply = struct.unpack_from("<Q", data, offset)[0]
            
            return PoolState(
                pool_address=pool_address,
                token_mint=token_mint,
                sol_reserves=sol_reserves,
                token_reserves=token_reserves,
                lp_supply=lp_supply,
            )
        except Exception as e:
            logger.debug(f"Pool decode error: {e}")
            return None

    def _handle_order(self, event: Event) -> None:
        """Handle ORDER_SUBMITTED events."""
        if not self._event_bus:
            return
        order = event.data
        try:
            loop = asyncio.get_event_loop()
            loop.create_task(self._execute_and_report(order, event.source))
        except RuntimeError:
            logger.error("No event loop for order execution")

    async def _execute_and_report(self, order: dict, source: str) -> None:
        if not self._event_bus:
            return
        try:
            tx_sig = await self.submit_order(order)
            self._event_bus.publish(OrderUpdate(
                event_type=EventType.ORDER_FILLED,
                source=self.name,
                token_in=order.get("token", ""),
                amount_in=order.get("amount_usd", 0),
                tx_signature=tx_sig,
            ))
        except Exception as e:
            self._event_bus.publish(OrderUpdate(
                event_type=EventType.ORDER_REJECTED,
                source=self.name,
                token_in=order.get("token", ""),
                error=str(e),
            ))

    @property
    def stats(self) -> dict:
        return {
            "name": self.name,
            "connected": self._connected,
            "transactions": self._tx_count,
            "jito_bundles": self._jito_bundles_sent,
            "volume_usd": self._total_volume_usd,
        }


class PoolState:
    """Decoded state of a PumpSwap pool."""
    
    def __init__(
        self,
        pool_address: str,
        token_mint: str,
        sol_reserves: int,
        token_reserves: int,
        lp_supply: int,
    ) -> None:
        self.pool_address = pool_address
        self.token_mint = token_mint
        self.sol_reserves = sol_reserves
        self.token_reserves = token_reserves
        self.lp_supply = lp_supply
    
    @property
    def price_sol(self) -> float:
        """Current token price in SOL."""
        if self.token_reserves == 0:
            return 0
        return self.sol_reserves / self.token_reserves
    
    @property
    def sol_liquidity(self) -> float:
        """SOL side liquidity in SOL (not lamports)."""
        return self.sol_reserves / 1e9
    
    def __repr__(self) -> str:
        return (
            f"PoolState(token={self.token_mint[:8]}... "
            f"sol={self.sol_liquidity:.2f} SOL "
            f"price={self.price_sol:.10f})"
        )


class PumpSwapError(Exception):
    pass
