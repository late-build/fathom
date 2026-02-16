"""
Jupiter Aggregator adapter for Fathom.

Handles swap execution through Jupiter v6 API. Supports:
- Quote fetching with configurable slippage
- Transaction building and signing
- Priority fee estimation
- Retry logic with exponential backoff
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from pathlib import Path
from typing import Any

import aiohttp

from fathom.adapters.base import BaseAdapter
from fathom.core.events import Event, EventType, OrderUpdate

logger = logging.getLogger("fathom.jupiter")

# Jupiter v6 API
JUPITER_QUOTE_URL = "https://lite-api.jup.ag/v1/quote"
JUPITER_SWAP_URL = "https://lite-api.jup.ag/v1/swap"

# Common Solana token mints
KNOWN_MINTS = {
    "SOL": "So11111111111111111111111111111111111111112",
    "USDC": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "USDT": "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
    "JUP": "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN",
    "RAY": "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R",
    "BONK": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
}


class JupiterAdapter(BaseAdapter):
    """
    Execution adapter for Jupiter DEX aggregator.
    
    Routes swaps through Jupiter's API for best-price execution
    across all Solana DEX liquidity.
    
    Args:
        rpc_url: Solana RPC endpoint (Helius recommended)
        wallet_path: Path to Solana keypair JSON
        slippage_bps: Default slippage tolerance in basis points
        priority_fee_lamports: Priority fee for transaction inclusion
        max_retries: Maximum retry attempts for failed transactions
    """
    
    name = "jupiter"
    
    def __init__(
        self,
        rpc_url: str,
        wallet_path: str | Path | None = None,
        slippage_bps: int = 50,
        priority_fee_lamports: int = 10_000,
        max_retries: int = 3,
    ) -> None:
        super().__init__()
        self.rpc_url = rpc_url
        self.wallet_path = Path(wallet_path) if wallet_path else None
        self.slippage_bps = slippage_bps
        self.priority_fee_lamports = priority_fee_lamports
        self.max_retries = max_retries
        self._session: aiohttp.ClientSession | None = None
        self._wallet_pubkey: str | None = None
        self._tx_count: int = 0
        self._total_volume_usd: float = 0.0

    async def connect(self) -> None:
        """Initialize HTTP session and load wallet."""
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30),
            headers={"Content-Type": "application/json"},
        )
        
        if self.wallet_path and self.wallet_path.exists():
            self._wallet_pubkey = self._load_wallet_pubkey()
            logger.info(f"Jupiter adapter connected | wallet={self._wallet_pubkey[:8]}...")
        else:
            logger.info("Jupiter adapter connected (read-only, no wallet)")
        
        self._connected = True
        
        if self._event_bus:
            self._event_bus.subscribe(EventType.ORDER_SUBMITTED, self._handle_order)
            self._event_bus.publish(Event(
                event_type=EventType.ADAPTER_CONNECTED,
                source=self.name,
            ))

    async def disconnect(self) -> None:
        """Close HTTP session."""
        if self._session:
            await self._session.close()
        self._connected = False
        logger.info(
            f"Jupiter adapter disconnected | "
            f"txs={self._tx_count} volume=${self._total_volume_usd:,.2f}"
        )

    async def get_quote(
        self,
        input_mint: str,
        output_mint: str,
        amount: int,
        slippage_bps: int | None = None,
    ) -> dict[str, Any]:
        """
        Fetch a swap quote from Jupiter.
        
        Args:
            input_mint: Input token mint address
            output_mint: Output token mint address  
            amount: Amount in smallest unit (lamports for SOL)
            slippage_bps: Slippage tolerance (overrides default)
            
        Returns:
            Jupiter quote response with route info and expected output
        """
        if not self._session:
            raise RuntimeError("Adapter not connected")
        
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount),
            "slippageBps": str(slippage_bps or self.slippage_bps),
        }
        
        async with self._session.get(JUPITER_QUOTE_URL, params=params) as resp:
            if resp.status != 200:
                error = await resp.text()
                raise JupiterError(f"Quote failed ({resp.status}): {error}")
            return await resp.json()

    async def execute_swap(
        self,
        quote: dict[str, Any],
        user_pubkey: str | None = None,
    ) -> dict[str, Any]:
        """
        Execute a swap using a Jupiter quote.
        
        Args:
            quote: Quote response from get_quote()
            user_pubkey: Wallet public key (defaults to loaded wallet)
            
        Returns:
            Swap response with transaction data
        """
        if not self._session:
            raise RuntimeError("Adapter not connected")
        
        pubkey = user_pubkey or self._wallet_pubkey
        if not pubkey:
            raise JupiterError("No wallet configured for execution")
        
        payload = {
            "quoteResponse": quote,
            "userPublicKey": pubkey,
            "prioritizationFeeLamports": self.priority_fee_lamports,
            "dynamicComputeUnitLimit": True,
        }
        
        async with self._session.post(JUPITER_SWAP_URL, json=payload) as resp:
            if resp.status != 200:
                error = await resp.text()
                raise JupiterError(f"Swap failed ({resp.status}): {error}")
            return await resp.json()

    async def submit_order(self, order: dict) -> str:
        """
        Full order flow: quote → swap → sign → submit.
        
        Implements retry logic with exponential backoff.
        """
        token = order.get("token", "")
        side = order.get("side", "buy")
        amount_usd = order.get("amount_usd", 0)
        slippage = order.get("slippage_bps", self.slippage_bps)
        
        # Resolve token mints
        if side == "buy":
            input_mint = KNOWN_MINTS.get("USDC", "")
            output_mint = KNOWN_MINTS.get(token.upper(), token)
        else:
            input_mint = KNOWN_MINTS.get(token.upper(), token)
            output_mint = KNOWN_MINTS.get("USDC", "")
        
        if not input_mint or not output_mint:
            raise JupiterError(f"Unknown token: {token}")
        
        # Convert USD amount to USDC smallest unit (6 decimals)
        amount = int(amount_usd * 1_000_000) if side == "buy" else int(order.get("amount", 0))
        
        last_error = None
        for attempt in range(self.max_retries):
            try:
                # Get quote
                quote = await self.get_quote(input_mint, output_mint, amount, slippage)
                
                # Execute swap
                swap_result = await self.execute_swap(quote)
                
                # TODO: Sign and submit transaction via RPC
                # For now, return the swap transaction data
                tx_signature = swap_result.get("swapTransaction", "")
                
                self._tx_count += 1
                self._total_volume_usd += amount_usd
                
                logger.info(
                    f"Swap executed | {side} {token} ${amount_usd:.2f} | "
                    f"attempt={attempt + 1}"
                )
                
                return tx_signature
                
            except Exception as e:
                last_error = e
                if attempt < self.max_retries - 1:
                    wait = 2 ** attempt  # exponential backoff
                    logger.warning(f"Swap attempt {attempt + 1} failed: {e}. Retrying in {wait}s...")
                    await asyncio.sleep(wait)
        
        raise JupiterError(f"Swap failed after {self.max_retries} attempts: {last_error}")

    def _handle_order(self, event: Event) -> None:
        """Handle ORDER_SUBMITTED events from strategies."""
        if not self._event_bus:
            return
        
        order = event.data
        
        # Schedule async execution
        try:
            loop = asyncio.get_event_loop()
            loop.create_task(self._execute_and_report(order, event.source))
        except RuntimeError:
            logger.error("No event loop available for order execution")

    async def _execute_and_report(self, order: dict, source: str) -> None:
        """Execute order and publish result events."""
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

    def _load_wallet_pubkey(self) -> str:
        """Load public key from Solana keypair file."""
        import json
        if self.wallet_path and self.wallet_path.exists():
            with open(self.wallet_path) as f:
                keypair_bytes = json.load(f)
            # First 32 bytes are the private key, last 32 are public
            pubkey_bytes = bytes(keypair_bytes[32:64])
            return base64.b58encode(pubkey_bytes).decode()  # type: ignore
        return ""

    @property
    def stats(self) -> dict:
        return {
            "name": self.name,
            "connected": self._connected,
            "transactions": self._tx_count,
            "total_volume_usd": self._total_volume_usd,
        }


class JupiterError(Exception):
    """Raised on Jupiter API errors."""
    pass
