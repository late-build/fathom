"""
Helius data feed adapter for Fathom.

Streams real-time token data via Helius WebSocket and REST APIs.
Produces PriceUpdate events for the engine's event bus.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import aiohttp

from fathom.adapters.base import BaseDataFeed
from fathom.core.events import Event, EventType, PriceUpdate

logger = logging.getLogger("fathom.helius")

HELIUS_WS_URL = "wss://mainnet.helius-rpc.com/?api-key={api_key}"
HELIUS_REST_URL = "https://mainnet.helius-rpc.com/?api-key={api_key}"


class HeliusDataFeed(BaseDataFeed):
    """
    Real-time Solana data feed via Helius.
    
    Subscribes to token account changes and transaction updates
    to generate price events. Falls back to polling if WebSocket
    is unavailable.
    
    Args:
        api_key: Helius API key
        tokens: List of token symbols or mint addresses to track
        poll_interval_ms: Fallback polling interval in milliseconds
    """
    
    name = "helius"
    
    def __init__(
        self,
        api_key: str,
        tokens: list[str] | None = None,
        poll_interval_ms: int = 1000,
    ) -> None:
        super().__init__()
        self.api_key = api_key
        self.tokens = tokens or ["SOL"]
        self.poll_interval_ms = poll_interval_ms
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._session: aiohttp.ClientSession | None = None
        self._poll_task: asyncio.Task | None = None
        self._update_count: int = 0
        self._last_prices: dict[str, float] = {}

    async def connect(self) -> None:
        """Connect to Helius WebSocket for real-time updates."""
        self._session = aiohttp.ClientSession()
        
        try:
            ws_url = HELIUS_WS_URL.format(api_key=self.api_key)
            self._ws = await self._session.ws_connect(ws_url)
            
            # Subscribe to account updates for tracked tokens
            for token in self.tokens:
                await self._subscribe_token(token)
            
            self._connected = True
            logger.info(f"Helius feed connected | tracking {len(self.tokens)} tokens")
            
            if self._event_bus:
                self._event_bus.publish(Event(
                    event_type=EventType.ADAPTER_CONNECTED,
                    source=self.name,
                ))
            
            # Start listening
            self._poll_task = asyncio.create_task(self._listen())
            
        except Exception as e:
            logger.warning(f"WebSocket connect failed: {e}. Falling back to polling.")
            self._connected = True
            self._poll_task = asyncio.create_task(self._poll_prices())

    async def disconnect(self) -> None:
        """Close WebSocket and HTTP session."""
        if self._poll_task:
            self._poll_task.cancel()
        if self._ws:
            await self._ws.close()
        if self._session:
            await self._session.close()
        self._connected = False
        logger.info(f"Helius feed disconnected | updates={self._update_count}")

    async def _subscribe_token(self, token: str) -> None:
        """Subscribe to WebSocket updates for a token."""
        if not self._ws:
            return
        
        # Subscribe to account changes (simplified — real impl would
        # resolve mint addresses and subscribe to relevant accounts)
        subscribe_msg = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "accountSubscribe",
            "params": [
                token,
                {"encoding": "jsonParsed", "commitment": "confirmed"},
            ],
        }
        await self._ws.send_json(subscribe_msg)

    async def _listen(self) -> None:
        """Listen for WebSocket messages and emit events."""
        if not self._ws:
            return
        
        try:
            async for msg in self._ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    self._process_ws_message(data)
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    logger.error(f"WebSocket error: {self._ws.exception()}")
                    break
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"WebSocket listener error: {e}")
            # Fall back to polling
            await self._poll_prices()

    async def _poll_prices(self) -> None:
        """
        Fallback: poll token prices via DexScreener API.
        
        DexScreener is used instead of Helius REST for price data
        because it provides pre-aggregated USD prices across all DEX pools.
        """
        if not self._session:
            return
        
        logger.info("Using polling fallback for price data")
        
        try:
            while self._connected:
                for token in self.tokens:
                    try:
                        await self._fetch_price(token)
                    except Exception as e:
                        logger.debug(f"Price fetch failed for {token}: {e}")
                
                await asyncio.sleep(self.poll_interval_ms / 1000)
        except asyncio.CancelledError:
            pass

    async def _fetch_price(self, token: str) -> None:
        """Fetch current price from DexScreener and emit PriceUpdate."""
        if not self._session or not self._event_bus:
            return
        
        # Use DexScreener search API for token prices
        url = f"https://api.dexscreener.com/latest/dex/search?q={token}"
        
        async with self._session.get(url) as resp:
            if resp.status != 200:
                return
            data = await resp.json()
        
        pairs = data.get("pairs", [])
        if not pairs:
            return
        
        # Take the highest-liquidity Solana pair
        solana_pairs = [p for p in pairs if p.get("chainId") == "solana"]
        if not solana_pairs:
            return
        
        best_pair = max(solana_pairs, key=lambda p: float(p.get("liquidity", {}).get("usd", 0)))
        
        price = float(best_pair.get("priceUsd", 0))
        volume = float(best_pair.get("volume", {}).get("h24", 0))
        liquidity = float(best_pair.get("liquidity", {}).get("usd", 0))
        
        # Only emit if price changed
        if self._last_prices.get(token) == price:
            return
        
        self._last_prices[token] = price
        self._update_count += 1
        
        self._event_bus.publish(PriceUpdate(
            event_type=EventType.PRICE_UPDATE,
            source=self.name,
            token=token,
            price_usd=price,
            volume_24h=volume,
            liquidity=liquidity,
        ))

    def _process_ws_message(self, data: dict[str, Any]) -> None:
        """Process a WebSocket message and emit appropriate events."""
        # Handle subscription confirmations
        if "result" in data and isinstance(data["result"], int):
            logger.debug(f"Subscription confirmed: {data['result']}")
            return
        
        # Handle account update notifications
        params = data.get("params", {})
        if params.get("result"):
            self._update_count += 1
            # TODO: Parse account data into PriceUpdate events
            # This requires resolving the account type and extracting
            # relevant price/balance information
            logger.debug(f"Account update received (#{self._update_count})")

    @property
    def stats(self) -> dict:
        return {
            "name": self.name,
            "connected": self._connected,
            "tokens_tracked": len(self.tokens),
            "updates_received": self._update_count,
            "last_prices": dict(self._last_prices),
        }
