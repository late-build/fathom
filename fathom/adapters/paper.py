"""
Paper trading adapter — simulates execution without touching the chain.

Logs every order, tracks simulated positions, and reports P&L.
Perfect for testing strategies before going live.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from fathom.adapters.base import BaseAdapter
from fathom.core.events import Event, EventType, OrderUpdate

logger = logging.getLogger("fathom.paper")


class PaperAdapter(BaseAdapter):
    """
    Simulated execution adapter.

    Every ORDER_SUBMITTED is immediately "filled" at the current price.
    Tracks balance, positions, and trade history for post-run analysis.
    """

    name = "paper"

    def __init__(self, initial_balance_usd: float = 1000.0) -> None:
        super().__init__()
        self.initial_balance_usd = initial_balance_usd
        self.balance_usd = initial_balance_usd
        self._positions: dict[str, float] = {}  # token -> amount
        self._entry_prices: dict[str, float] = {}  # token -> avg entry USD
        self._trades: list[dict[str, Any]] = []
        self._fill_count: int = 0
        self._total_volume: float = 0.0
        self._last_prices: dict[str, float] = {}

    async def connect(self) -> None:
        self._connected = True
        if self._event_bus:
            self._event_bus.subscribe(EventType.ORDER_SUBMITTED, self._handle_order)
            self._event_bus.subscribe(EventType.PRICE_UPDATE, self._track_price)
            self._event_bus.publish(Event(
                event_type=EventType.ADAPTER_CONNECTED,
                source=self.name,
            ))
        logger.info(f"Paper adapter ready | balance=${self.initial_balance_usd:,.2f}")

    async def disconnect(self) -> None:
        self._connected = False
        logger.info(
            f"Paper adapter stopped | trades={self._fill_count} "
            f"volume=${self._total_volume:,.2f} "
            f"balance=${self.balance_usd:,.2f}"
        )

    async def submit_order(self, order: dict) -> str:
        """Simulate order execution instantly."""
        side = order.get("side", "buy")
        token = order.get("token", "")
        amount_usd = order.get("amount_usd", 0)
        amount_tokens = order.get("amount", 0)

        sig = f"paper_{self._fill_count}_{int(time.time())}"

        if side == "buy":
            if amount_usd > self.balance_usd:
                raise PaperError(f"Insufficient balance: ${self.balance_usd:.2f} < ${amount_usd:.2f}")
            self.balance_usd -= amount_usd
            # We need a price to convert — use last known
            price = self._last_prices.get(token, 0)
            if price > 0:
                tokens = amount_usd / price
            else:
                tokens = amount_usd  # fallback: 1:1
            self._positions[token] = self._positions.get(token, 0) + tokens
            self._entry_prices[token] = price if price > 0 else amount_usd / tokens
            self._total_volume += amount_usd
            logger.info(f"📝 PAPER BUY  | {token[:12]} | ${amount_usd:.2f} @ ${price:.8f}")

        elif side == "sell":
            held = self._positions.get(token, 0)
            sell_amount = min(amount_tokens, held) if amount_tokens > 0 else held
            price = self._last_prices.get(token, 0)
            proceeds = sell_amount * price if price > 0 else 0
            self.balance_usd += proceeds
            self._positions[token] = held - sell_amount
            if self._positions[token] <= 0:
                self._positions.pop(token, None)
                self._entry_prices.pop(token, None)
            self._total_volume += proceeds
            logger.info(f"📝 PAPER SELL | {token[:12]} | {sell_amount:.2f} tokens @ ${price:.8f} = ${proceeds:.2f}")

        self._trades.append({
            "side": side,
            "token": token,
            "amount_usd": amount_usd,
            "amount_tokens": amount_tokens,
            "price": self._last_prices.get(token, 0),
            "timestamp": time.time(),
            "signature": sig,
        })
        self._fill_count += 1
        return sig

    def _handle_order(self, event: Event) -> None:
        if not self._event_bus:
            return
        order = event.data
        # Synchronous fill — works in both backtest and live (via event loop)
        self._sync_fill(order)

    def _sync_fill(self, order: dict) -> None:
        """Execute order synchronously (backtest-safe)."""
        sig = f"paper_{self._fill_count}_{int(time.time())}"
        side = order.get("side", "buy")
        token = order.get("token", "")
        amount_usd = order.get("amount_usd", 0)
        price = self._last_prices.get(token, 0)

        if side == "buy":
            if amount_usd > self.balance_usd:
                if self._event_bus:
                    self._event_bus.publish(OrderUpdate(
                        event_type=EventType.ORDER_REJECTED,
                        source=self.name,
                        token_in=token,
                        error=f"Insufficient balance: ${self.balance_usd:.2f}",
                    ))
                return
            self.balance_usd -= amount_usd
            tokens = amount_usd / price if price > 0 else amount_usd
            self._positions[token] = self._positions.get(token, 0) + tokens
            self._entry_prices[token] = price if price > 0 else 1
            self._total_volume += amount_usd
            self._fill_count += 1
            logger.info(f"📝 PAPER BUY  | {token[:12]} | ${amount_usd:.2f} @ ${price:.8f}")
        elif side == "sell":
            held = self._positions.get(token, 0)
            amount_tokens = order.get("amount", 0)
            sell_amount = min(amount_tokens, held) if amount_tokens > 0 else held
            proceeds = sell_amount * price if price > 0 else 0
            self.balance_usd += proceeds
            self._positions[token] = held - sell_amount
            if self._positions[token] <= 0:
                self._positions.pop(token, None)
            self._total_volume += proceeds
            self._fill_count += 1
            logger.info(f"📝 PAPER SELL | {token[:12]} | {sell_amount:.4f} tokens @ ${price:.8f} = ${proceeds:.2f}")

        if self._event_bus:
            self._event_bus.publish(OrderUpdate(
                event_type=EventType.ORDER_FILLED,
                source=self.name,
                token_in=token,
                amount_in=amount_usd,
                tx_signature=sig,
            ))

    async def _execute_and_report(self, order: dict, source: str) -> None:
        if not self._event_bus:
            return
        try:
            sig = await self.submit_order(order)
            self._event_bus.publish(OrderUpdate(
                event_type=EventType.ORDER_FILLED,
                source=self.name,
                token_in=order.get("token", ""),
                amount_in=order.get("amount_usd", 0),
                tx_signature=sig,
            ))
        except Exception as e:
            self._event_bus.publish(OrderUpdate(
                event_type=EventType.ORDER_REJECTED,
                source=self.name,
                token_in=order.get("token", ""),
                error=str(e),
            ))

    def _track_price(self, event: Event) -> None:
        from fathom.core.events import PriceUpdate
        if isinstance(event, PriceUpdate) and event.price_usd > 0:
            self._last_prices[event.token] = event.price_usd

    def set_price(self, token: str, price: float) -> None:
        """Manually set price (useful for backtest seeding)."""
        self._last_prices[token] = price

    @property
    def pnl(self) -> float:
        """Unrealized + realized P&L."""
        # Realized is balance change
        realized = self.balance_usd - self.initial_balance_usd
        # Unrealized is open positions at last price
        unrealized = sum(
            self._positions.get(t, 0) * self._last_prices.get(t, 0)
            for t in self._positions
        )
        return realized + unrealized

    @property
    def stats(self) -> dict:
        return {
            "name": self.name,
            "balance_usd": self.balance_usd,
            "pnl": self.pnl,
            "trades": self._fill_count,
            "volume_usd": self._total_volume,
            "open_positions": len(self._positions),
        }


class PaperError(Exception):
    pass
