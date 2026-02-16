"""
Pump.fun Graduation Monitor for Fathom.

Tracks bonding curves on pump.fun, detects graduations to PumpSwap/Raydium,
and emits events that strategies can act on.

This is the core data source for memecoin trading strategies — it answers
the question "what just graduated and is it worth trading?"
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any

import aiohttp

from fathom.adapters.base import BaseDataFeed
from fathom.core.events import Event, EventType, EventBus

logger = logging.getLogger("fathom.pumpfun")

# Pump.fun program IDs
PUMP_FUN_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMP_SWAP_AMM = "PSwapMdSai8tjrEXcxFeQth87xC4rRsa4VA5mhGhXkP"
RAYDIUM_AMM_V4 = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"

# Graduation threshold (bonding curve fills at ~85 SOL / $12K)
GRADUATION_SOL_THRESHOLD = 85.0

# DexScreener API for post-graduation price data
DEXSCREENER_API = "https://api.dexscreener.com/latest/dex"


class TokenPhase(Enum):
    """Lifecycle phases of a pump.fun token."""
    BONDING = auto()       # Active on bonding curve
    GRADUATING = auto()    # Approaching graduation threshold
    GRADUATED = auto()     # Migrated to PumpSwap/Raydium
    DEAD = auto()          # No volume, abandoned


@dataclass
class TokenState:
    """Tracked state of a pump.fun token."""
    mint: str
    name: str = ""
    symbol: str = ""
    phase: TokenPhase = TokenPhase.BONDING
    bonding_progress_pct: float = 0.0
    sol_raised: float = 0.0
    holder_count: int = 0
    creator: str = ""
    created_at_ns: int = 0
    graduated_at_ns: int = 0
    pool_address: str = ""           # Raydium/PumpSwap pool after graduation
    pool_type: str = ""              # "pumpswap" or "raydium"
    initial_price_usd: float = 0.0   # Price at graduation
    current_price_usd: float = 0.0
    market_cap_usd: float = 0.0
    volume_5m_usd: float = 0.0
    dev_sold: bool = False
    dev_sell_pct: float = 0.0
    
    @property
    def age_seconds(self) -> float:
        if not self.created_at_ns:
            return 0
        return (time.time_ns() - self.created_at_ns) / 1e9
    
    @property
    def time_since_graduation_seconds(self) -> float:
        if not self.graduated_at_ns:
            return 0
        return (time.time_ns() - self.graduated_at_ns) / 1e9


# -- Custom event types for graduation system --

@dataclass(frozen=True)
class GraduationEvent(Event):
    """Emitted when a token graduates from pump.fun to a DEX pool."""
    event_type: EventType = EventType.SIGNAL
    mint: str = ""
    symbol: str = ""
    pool_address: str = ""
    pool_type: str = ""
    sol_raised: float = 0.0
    holder_count: int = 0
    creator: str = ""
    initial_price_usd: float = 0.0


@dataclass(frozen=True)
class BondingProgressEvent(Event):
    """Emitted when a token's bonding curve makes significant progress."""
    event_type: EventType = EventType.SIGNAL
    mint: str = ""
    symbol: str = ""
    progress_pct: float = 0.0
    sol_raised: float = 0.0
    holder_count: int = 0


@dataclass(frozen=True)
class DevActivityEvent(Event):
    """Emitted when the dev wallet makes a significant move post-graduation."""
    event_type: EventType = EventType.SIGNAL
    mint: str = ""
    symbol: str = ""
    action: str = ""         # "sell", "transfer", "add_liquidity"
    amount_pct: float = 0.0  # % of supply involved


class GraduationMonitor(BaseDataFeed):
    """
    Monitors pump.fun for token graduations and post-graduation activity.
    
    This is the primary data source for memecoin trading strategies.
    It feeds the engine with:
    
    1. Bonding progress updates (tokens approaching graduation)
    2. Graduation events (token migrated to PumpSwap/Raydium)
    3. Post-graduation metrics (price, volume, dev activity)
    4. Dev wallet monitoring (are they dumping?)
    
    Strategies subscribe to these events and decide whether to trade.
    
    Args:
        helius_api_key: Helius API key for transaction streaming
        min_bonding_progress: Minimum % to start tracking a token (0-100)
        min_holders: Minimum holder count to consider a token
        track_dev_wallets: Monitor creator wallets post-graduation
        poll_interval_ms: How often to poll for updates
    """
    
    name = "graduation_monitor"
    
    def __init__(
        self,
        helius_api_key: str,
        min_bonding_progress: float = 70.0,
        min_holders: int = 50,
        track_dev_wallets: bool = True,
        poll_interval_ms: int = 2000,
    ) -> None:
        super().__init__()
        self.helius_api_key = helius_api_key
        self.min_bonding_progress = min_bonding_progress
        self.min_holders = min_holders
        self.track_dev_wallets = track_dev_wallets
        self.poll_interval_ms = poll_interval_ms
        
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        
        # Token tracking state
        self._tracked_tokens: dict[str, TokenState] = {}
        self._graduated_tokens: dict[str, TokenState] = {}
        self._dev_wallets: dict[str, str] = {}  # creator_address -> mint
        
        # Stats
        self._graduations_detected: int = 0
        self._tokens_scanned: int = 0
        self._ws_messages: int = 0

    async def connect(self) -> None:
        """
        Connect to Helius WebSocket and subscribe to pump.fun program logs.
        
        We monitor two programs:
        1. Pump.fun program — bonding curve transactions
        2. PumpSwap AMM / Raydium — pool creation (graduation)
        """
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30),
        )
        
        ws_url = f"wss://mainnet.helius-rpc.com/?api-key={self.helius_api_key}"
        
        try:
            self._ws = await self._session.ws_connect(ws_url)
            
            # Subscribe to pump.fun program logs
            await self._ws.send_json({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "logsSubscribe",
                "params": [
                    {"mentions": [PUMP_FUN_PROGRAM]},
                    {"commitment": "confirmed"},
                ],
            })
            
            # Subscribe to PumpSwap AMM for graduation detection
            await self._ws.send_json({
                "jsonrpc": "2.0",
                "id": 2,
                "method": "logsSubscribe",
                "params": [
                    {"mentions": [PUMP_SWAP_AMM]},
                    {"commitment": "confirmed"},
                ],
            })
            
            self._connected = True
            logger.info(
                f"Graduation monitor connected | "
                f"min_progress={self.min_bonding_progress}% "
                f"min_holders={self.min_holders}"
            )
            
            if self._event_bus:
                self._event_bus.publish(Event(
                    event_type=EventType.ADAPTER_CONNECTED,
                    source=self.name,
                ))
            
            # Start listener and poller tasks
            asyncio.create_task(self._ws_listener())
            asyncio.create_task(self._poll_graduated_tokens())
            
            if self.track_dev_wallets:
                asyncio.create_task(self._monitor_dev_wallets())
                
        except Exception as e:
            logger.error(f"Graduation monitor connect failed: {e}")
            # Fall back to polling-only mode
            self._connected = True
            asyncio.create_task(self._poll_new_graduations())

    async def disconnect(self) -> None:
        if self._ws:
            await self._ws.close()
        if self._session:
            await self._session.close()
        self._connected = False
        logger.info(
            f"Graduation monitor stopped | "
            f"graduations={self._graduations_detected} "
            f"tracked={len(self._tracked_tokens)} "
            f"ws_msgs={self._ws_messages}"
        )

    # -- WebSocket listener --
    
    async def _ws_listener(self) -> None:
        """Process WebSocket messages for pump.fun and PumpSwap transactions."""
        if not self._ws:
            return
        
        try:
            async for msg in self._ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    self._ws_messages += 1
                    data = json.loads(msg.data)
                    await self._process_log_message(data)
                elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                    logger.warning("WebSocket closed, reconnecting...")
                    await asyncio.sleep(2)
                    await self.connect()
                    return
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"WebSocket listener error: {e}")

    async def _process_log_message(self, data: dict[str, Any]) -> None:
        """
        Parse transaction logs to detect bonding curve updates and graduations.
        
        Pump.fun transactions include:
        - Buy/sell on bonding curve (updates progress)
        - CreatePool (graduation to PumpSwap)
        - Migrate (graduation to Raydium)
        """
        params = data.get("params", {})
        result = params.get("result", {})
        value = result.get("value", {})
        logs = value.get("logs", [])
        signature = value.get("signature", "")
        
        if not logs:
            return
        
        log_str = " ".join(logs)
        
        # Detect graduation events
        if "CreatePool" in log_str or "Initialize" in log_str:
            if PUMP_SWAP_AMM in log_str or RAYDIUM_AMM_V4 in log_str:
                await self._handle_graduation(signature, logs)
                return
        
        # Detect bonding curve activity
        if PUMP_FUN_PROGRAM in log_str:
            if "Buy" in log_str or "Sell" in log_str:
                await self._handle_bonding_activity(signature, logs)

    async def _handle_graduation(self, signature: str, logs: list[str]) -> None:
        """
        Process a graduation event — token migrated from bonding curve to DEX pool.
        
        This is the critical moment. We need to:
        1. Extract the token mint and new pool address
        2. Determine if it's PumpSwap or Raydium
        3. Fetch initial price/liquidity
        4. Emit a GraduationEvent for strategies
        """
        self._graduations_detected += 1
        
        # Parse transaction for mint and pool details
        tx_data = await self._fetch_transaction(signature)
        if not tx_data:
            return
        
        # Extract token info from transaction
        token_info = self._parse_graduation_tx(tx_data)
        if not token_info:
            return
        
        mint = token_info["mint"]
        pool_address = token_info.get("pool", "")
        pool_type = token_info.get("pool_type", "pumpswap")
        
        # Create or update token state
        state = self._tracked_tokens.get(mint, TokenState(mint=mint))
        state.phase = TokenPhase.GRADUATED
        state.graduated_at_ns = time.time_ns()
        state.pool_address = pool_address
        state.pool_type = pool_type
        
        # Fetch post-graduation price from DexScreener
        price_data = await self._fetch_dexscreener_price(mint)
        if price_data:
            state.initial_price_usd = price_data.get("price_usd", 0)
            state.market_cap_usd = price_data.get("market_cap", 0)
            state.volume_5m_usd = price_data.get("volume_5m", 0)
        
        # Move to graduated tracking
        self._graduated_tokens[mint] = state
        self._tracked_tokens.pop(mint, None)
        
        # Track dev wallet
        if self.track_dev_wallets and state.creator:
            self._dev_wallets[state.creator] = mint
        
        logger.info(
            f"🎓 GRADUATION | {state.symbol or mint[:8]} | "
            f"pool={pool_type} | price=${state.initial_price_usd:.6f} | "
            f"mcap=${state.market_cap_usd:,.0f}"
        )
        
        # Emit graduation event
        if self._event_bus:
            self._event_bus.publish(GraduationEvent(
                source=self.name,
                mint=mint,
                symbol=state.symbol,
                pool_address=pool_address,
                pool_type=pool_type,
                sol_raised=state.sol_raised,
                holder_count=state.holder_count,
                creator=state.creator,
                initial_price_usd=state.initial_price_usd,
            ))

    async def _handle_bonding_activity(self, signature: str, logs: list[str]) -> None:
        """Track bonding curve buy/sell activity to estimate graduation progress."""
        self._tokens_scanned += 1
        
        # TODO: Parse the transaction to extract:
        # - Token mint address
        # - SOL amount in/out
        # - Updated bonding curve state
        # 
        # For now, we rely on polling to track bonding progress.
        # Full implementation would decode the pump.fun program accounts
        # to get exact bonding curve position.

    # -- Polling fallbacks --
    
    async def _poll_new_graduations(self) -> None:
        """
        Fallback: Poll DexScreener for recently created Solana pairs.
        
        Less responsive than WebSocket but works without Helius.
        Checks for new pairs every poll_interval_ms.
        """
        if not self._session:
            return
        
        logger.info("Using polling fallback for graduation detection")
        seen_pairs: set[str] = set()
        
        try:
            while self._connected:
                try:
                    url = f"{DEXSCREENER_API}/pairs/solana"
                    async with self._session.get(url) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            pairs = data.get("pairs", [])
                            
                            for pair in pairs:
                                pair_addr = pair.get("pairAddress", "")
                                if pair_addr in seen_pairs:
                                    continue
                                seen_pairs.add(pair_addr)
                                
                                # Check if this is a recent pump.fun graduation
                                created = pair.get("pairCreatedAt", 0)
                                age_seconds = (time.time() * 1000 - created) / 1000 if created else 999999
                                
                                if age_seconds < 300:  # Less than 5 min old
                                    await self._process_new_pair(pair)
                                    
                except Exception as e:
                    logger.debug(f"Poll error: {e}")
                
                await asyncio.sleep(self.poll_interval_ms / 1000)
        except asyncio.CancelledError:
            pass

    async def _poll_graduated_tokens(self) -> None:
        """
        Continuously update price/volume for recently graduated tokens.
        
        This provides the ongoing data stream that strategies use to
        make buy/sell decisions post-graduation.
        """
        if not self._session or not self._event_bus:
            return
        
        try:
            while self._connected:
                for mint, state in list(self._graduated_tokens.items()):
                    # Stop tracking after 1 hour
                    if state.time_since_graduation_seconds > 3600:
                        self._graduated_tokens.pop(mint, None)
                        continue
                    
                    try:
                        price_data = await self._fetch_dexscreener_price(mint)
                        if price_data:
                            state.current_price_usd = price_data.get("price_usd", 0)
                            state.market_cap_usd = price_data.get("market_cap", 0)
                            state.volume_5m_usd = price_data.get("volume_5m", 0)
                            
                            # Emit as price update for strategies
                            from fathom.core.events import PriceUpdate
                            self._event_bus.publish(PriceUpdate(
                                source=self.name,
                                token=mint,
                                price_usd=state.current_price_usd,
                                volume_24h=state.volume_5m_usd * 288,  # extrapolate
                                liquidity=price_data.get("liquidity", 0),
                            ))
                    except Exception as e:
                        logger.debug(f"Price update failed for {mint[:8]}: {e}")
                
                await asyncio.sleep(self.poll_interval_ms / 1000)
        except asyncio.CancelledError:
            pass

    async def _monitor_dev_wallets(self) -> None:
        """
        Track creator wallets for post-graduation selling.
        
        If the dev dumps, strategies need to know immediately.
        """
        if not self._session or not self._event_bus:
            return
        
        try:
            while self._connected:
                for creator, mint in list(self._dev_wallets.items()):
                    state = self._graduated_tokens.get(mint)
                    if not state or state.time_since_graduation_seconds > 3600:
                        self._dev_wallets.pop(creator, None)
                        continue
                    
                    try:
                        sold, sell_pct = await self._check_dev_sells(creator, mint)
                        if sold and not state.dev_sold:
                            state.dev_sold = True
                            state.dev_sell_pct = sell_pct
                            
                            logger.warning(
                                f"⚠️ DEV SELL | {state.symbol or mint[:8]} | "
                                f"{sell_pct:.1f}% of supply"
                            )
                            
                            self._event_bus.publish(DevActivityEvent(
                                source=self.name,
                                mint=mint,
                                symbol=state.symbol,
                                action="sell",
                                amount_pct=sell_pct,
                            ))
                    except Exception as e:
                        logger.debug(f"Dev wallet check failed: {e}")
                
                await asyncio.sleep(5)  # Check every 5 seconds
        except asyncio.CancelledError:
            pass

    # -- Data fetching helpers --

    async def _fetch_transaction(self, signature: str) -> dict | None:
        """Fetch parsed transaction data from Helius."""
        if not self._session:
            return None
        
        url = f"https://api.helius.xyz/v0/transactions/?api-key={self.helius_api_key}"
        payload = {"transactions": [signature]}
        
        try:
            async with self._session.post(url, json=payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data[0] if data else None
        except Exception as e:
            logger.debug(f"Transaction fetch failed: {e}")
        return None

    async def _fetch_dexscreener_price(self, mint: str) -> dict | None:
        """Fetch current price data from DexScreener for a token mint."""
        if not self._session:
            return None
        
        url = f"{DEXSCREENER_API}/tokens/{mint}"
        
        try:
            async with self._session.get(url) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
            
            pairs = data.get("pairs", [])
            if not pairs:
                return None
            
            # Get the highest-liquidity pair
            best = max(pairs, key=lambda p: float(p.get("liquidity", {}).get("usd", 0)))
            
            return {
                "price_usd": float(best.get("priceUsd", 0)),
                "market_cap": float(best.get("marketCap", 0)),
                "volume_5m": float(best.get("volume", {}).get("m5", 0)),
                "liquidity": float(best.get("liquidity", {}).get("usd", 0)),
                "pair_address": best.get("pairAddress", ""),
            }
        except Exception:
            return None

    async def _check_dev_sells(self, creator: str, mint: str) -> tuple[bool, float]:
        """
        Check if a creator wallet has sold tokens post-graduation.
        
        Returns (sold: bool, sell_percentage: float)
        """
        if not self._session:
            return False, 0.0
        
        # Use Helius parsed transaction history for the creator
        url = (
            f"https://api.helius.xyz/v0/addresses/{creator}/transactions"
            f"?api-key={self.helius_api_key}&type=SWAP"
        )
        
        try:
            async with self._session.get(url) as resp:
                if resp.status != 200:
                    return False, 0.0
                txs = await resp.json()
            
            for tx in txs[:10]:  # Check last 10 transactions
                # Look for swaps where the creator sold the graduated token
                token_transfers = tx.get("tokenTransfers", [])
                for transfer in token_transfers:
                    if (transfer.get("mint") == mint and 
                        transfer.get("fromUserAccount") == creator):
                        amount = float(transfer.get("tokenAmount", 0))
                        # Estimate percentage (would need total supply for accuracy)
                        return True, min(amount / 1_000_000_000 * 100, 100)
            
            return False, 0.0
        except Exception:
            return False, 0.0

    async def _process_new_pair(self, pair: dict) -> None:
        """Process a newly detected DexScreener pair as a potential graduation."""
        mint = pair.get("baseToken", {}).get("address", "")
        if not mint or mint in self._graduated_tokens:
            return
        
        state = TokenState(
            mint=mint,
            name=pair.get("baseToken", {}).get("name", ""),
            symbol=pair.get("baseToken", {}).get("symbol", ""),
            phase=TokenPhase.GRADUATED,
            graduated_at_ns=time.time_ns(),
            pool_address=pair.get("pairAddress", ""),
            initial_price_usd=float(pair.get("priceUsd", 0)),
            market_cap_usd=float(pair.get("marketCap", 0)),
        )
        
        self._graduated_tokens[mint] = state
        self._graduations_detected += 1
        
        if self._event_bus:
            self._event_bus.publish(GraduationEvent(
                source=self.name,
                mint=mint,
                symbol=state.symbol,
                pool_address=state.pool_address,
                initial_price_usd=state.initial_price_usd,
            ))

    def _parse_graduation_tx(self, tx_data: dict) -> dict | None:
        """
        Parse a Helius-enriched transaction to extract graduation details.
        
        Returns dict with mint, pool, pool_type or None if not a graduation.
        """
        tx_type = tx_data.get("type", "")
        
        # Helius enriches pump.fun transactions
        if tx_type in ("CREATE_POOL", "SWAP", "TRANSFER"):
            token_transfers = tx_data.get("tokenTransfers", [])
            account_data = tx_data.get("accountData", [])
            
            mint = ""
            pool = ""
            
            for transfer in token_transfers:
                if transfer.get("mint") and transfer["mint"] != "So11111111111111111111111111111111111111112":
                    mint = transfer["mint"]
                    break
            
            # Determine pool type from involved programs
            instructions = tx_data.get("instructions", [])
            pool_type = "pumpswap"
            for ix in instructions:
                if ix.get("programId") == RAYDIUM_AMM_V4:
                    pool_type = "raydium"
                    break
            
            if mint:
                return {
                    "mint": mint,
                    "pool": pool,
                    "pool_type": pool_type,
                    "creator": tx_data.get("feePayer", ""),
                }
        
        return None

    # -- Backtest support --

    def load_historical_graduations(self, data: list[dict]) -> None:
        """
        Load historical graduation data for backtesting.
        
        Args:
            data: List of graduation records with format:
                {
                    "mint": "...",
                    "symbol": "...",
                    "graduated_at": 1708000000,  # unix timestamp
                    "initial_price_usd": 0.000042,
                    "pool_address": "...",
                    "pool_type": "pumpswap",
                    "price_history": [
                        {"timestamp": 1708000060, "price": 0.000045, "volume_5m": 12000},
                        ...
                    ]
                }
        """
        for record in data:
            mint = record["mint"]
            state = TokenState(
                mint=mint,
                symbol=record.get("symbol", ""),
                phase=TokenPhase.GRADUATED,
                graduated_at_ns=int(record.get("graduated_at", 0)) * 1_000_000_000,
                pool_address=record.get("pool_address", ""),
                pool_type=record.get("pool_type", "pumpswap"),
                initial_price_usd=record.get("initial_price_usd", 0),
            )
            self._graduated_tokens[mint] = state
        
        logger.info(f"Loaded {len(data)} historical graduations for backtesting")

    @property
    def tracked_tokens(self) -> dict[str, TokenState]:
        """Currently tracked pre-graduation tokens."""
        return dict(self._tracked_tokens)
    
    @property
    def graduated_tokens(self) -> dict[str, TokenState]:
        """Recently graduated tokens being monitored."""
        return dict(self._graduated_tokens)

    @property
    def stats(self) -> dict:
        return {
            "name": self.name,
            "connected": self._connected,
            "graduations_detected": self._graduations_detected,
            "tokens_tracked": len(self._tracked_tokens),
            "tokens_graduated": len(self._graduated_tokens),
            "dev_wallets_monitored": len(self._dev_wallets),
            "ws_messages": self._ws_messages,
            "tokens_scanned": self._tokens_scanned,
        }
