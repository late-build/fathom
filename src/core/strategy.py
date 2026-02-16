"""
Strategy base class.

All trading strategies extend this. The key contract: write your strategy
once, run it in backtest or live with zero code changes.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from fathom.core.events import EventBus, Event, EventType, PriceUpdate, Trade

if TYPE_CHECKING:
    pass

logger = logging.getLogger("fathom.strategy")


class Strategy(ABC):
    """
    Base class for all fathom trading strategies.
    
    Subclass this and implement on_price_update() at minimum.
    The engine calls lifecycle methods automatically.
    
    Example:
        class MyStrategy(Strategy):
            name = "my_strategy"
            
            def on_price_update(self, event: PriceUpdate):
                if event.token == "SOL" and event.price_usd < 100:
                    self.buy("SOL", amount_usd=50)
    """
    
    name: str = "unnamed_strategy"
    
    def __init__(self) -> None:
        self._event_bus: EventBus | None = None
        self._position: dict[str, float] = {}  # token -> amount
        self._pnl: float = 0.0
        self._trade_count: int = 0

    def bind(self, event_bus: EventBus) -> None:
        """Called by the engine to wire this strategy into the event bus."""
        self._event_bus = event_bus
        event_bus.subscribe(EventType.PRICE_UPDATE, self._handle_price_update)
        event_bus.subscribe(EventType.TRADE, self._handle_trade)
        event_bus.subscribe(EventType.ORDER_FILLED, self._handle_order_filled)

    def _handle_price_update(self, event: Event) -> None:
        if isinstance(event, PriceUpdate):
            self.on_price_update(event)

    def _handle_trade(self, event: Event) -> None:
        if isinstance(event, Trade):
            self.on_trade(event)

    def _handle_order_filled(self, event: Event) -> None:
        self._trade_count += 1
        self.on_order_filled(event)

    # -- Lifecycle methods (override these) --

    def on_start(self) -> None:
        """Called when the engine starts. Initialize state here."""
        pass

    def on_stop(self) -> None:
        """Called when the engine stops. Clean up here."""
        logger.info(f"[{self.name}] stopped | trades={self._trade_count} pnl={self._pnl:.4f}")

    @abstractmethod
    def on_price_update(self, event: PriceUpdate) -> None:
        """Called on every price update. Core strategy logic goes here."""
        ...

    def on_trade(self, event: Trade) -> None:
        """Called on every observed trade (not just ours)."""
        pass

    def on_order_filled(self, event: Event) -> None:
        """Called when one of our orders fills."""
        pass

    # -- Order methods --

    def buy(self, token: str, amount_usd: float, slippage_bps: int = 50) -> None:
        """Submit a buy order through the execution adapter."""
        if self._event_bus is None:
            raise RuntimeError("Strategy not bound to engine")
        
        self._event_bus.publish(Event(
            event_type=EventType.ORDER_SUBMITTED,
            source=self.name,
            data={
                "side": "buy",
                "token": token,
                "amount_usd": amount_usd,
                "slippage_bps": slippage_bps,
            },
        ))

    def sell(self, token: str, amount: float, slippage_bps: int = 50) -> None:
        """Submit a sell order through the execution adapter."""
        if self._event_bus is None:
            raise RuntimeError("Strategy not bound to engine")
        
        self._event_bus.publish(Event(
            event_type=EventType.ORDER_SUBMITTED,
            source=self.name,
            data={
                "side": "sell",
                "token": token,
                "amount": amount,
                "slippage_bps": slippage_bps,
            },
        ))

    @property
    def stats(self) -> dict:
        return {
            "name": self.name,
            "trades": self._trade_count,
            "pnl": self._pnl,
            "positions": dict(self._position),
        }
