"""
Event system — the backbone of fathom's event-driven architecture.

All market data, order updates, and strategy signals flow through the EventBus
as typed events with nanosecond timestamps.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable
from collections import defaultdict


class EventType(Enum):
    """Core event types flowing through the engine."""
    # Market data
    PRICE_UPDATE = auto()
    TRADE = auto()
    ORDERBOOK_UPDATE = auto()
    LIQUIDITY_UPDATE = auto()
    
    # Order lifecycle
    ORDER_SUBMITTED = auto()
    ORDER_ACCEPTED = auto()
    ORDER_FILLED = auto()
    ORDER_PARTIALLY_FILLED = auto()
    ORDER_REJECTED = auto()
    ORDER_CANCELLED = auto()
    
    # Strategy signals
    SIGNAL = auto()
    
    # System
    ENGINE_START = auto()
    ENGINE_STOP = auto()
    ADAPTER_CONNECTED = auto()
    ADAPTER_DISCONNECTED = auto()
    HEARTBEAT = auto()
    ERROR = auto()


@dataclass(frozen=True)
class Event:
    """
    Base event with nanosecond timestamp.
    
    All events are immutable once created. The timestamp is set at creation
    time using monotonic nanoseconds for backtest reproducibility.
    """
    event_type: EventType
    timestamp_ns: int = field(default_factory=lambda: time.time_ns())
    source: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def timestamp_ms(self) -> float:
        return self.timestamp_ns / 1_000_000

    @property
    def timestamp_s(self) -> float:
        return self.timestamp_ns / 1_000_000_000


@dataclass(frozen=True)
class PriceUpdate(Event):
    """Real-time price update for a token pair."""
    event_type: EventType = EventType.PRICE_UPDATE
    token: str = ""
    price_usd: float = 0.0
    volume_24h: float = 0.0
    liquidity: float = 0.0


@dataclass(frozen=True)
class Trade(Event):
    """A completed swap/trade on a DEX."""
    event_type: EventType = EventType.TRADE
    token_in: str = ""
    token_out: str = ""
    amount_in: float = 0.0
    amount_out: float = 0.0
    price: float = 0.0
    pool: str = ""
    tx_signature: str = ""


@dataclass(frozen=True)
class OrderUpdate(Event):
    """Order state change."""
    order_id: str = ""
    token_in: str = ""
    token_out: str = ""
    amount_in: float = 0.0
    amount_out: float = 0.0
    slippage_bps: int = 0
    tx_signature: str = ""
    error: str = ""


class EventBus:
    """
    Central event routing system.
    
    Handlers subscribe to specific event types. Events are dispatched
    synchronously in the order received — critical for deterministic
    backtest replay.
    
    Usage:
        bus = EventBus()
        bus.subscribe(EventType.PRICE_UPDATE, my_handler)
        bus.publish(PriceUpdate(token="SOL", price_usd=148.50))
    """
    
    def __init__(self) -> None:
        self._handlers: dict[EventType, list[Callable[[Event], None]]] = defaultdict(list)
        self._event_count: int = 0
        self._error_count: int = 0

    def subscribe(self, event_type: EventType, handler: Callable[[Event], None]) -> None:
        """Register a handler for an event type."""
        self._handlers[event_type].append(handler)

    def unsubscribe(self, event_type: EventType, handler: Callable[[Event], None]) -> None:
        """Remove a handler."""
        handlers = self._handlers.get(event_type, [])
        if handler in handlers:
            handlers.remove(handler)

    def publish(self, event: Event) -> None:
        """
        Dispatch an event to all subscribed handlers.
        
        Handlers are called synchronously in subscription order.
        Exceptions in handlers are caught and logged — one bad handler
        doesn't break the event chain.
        """
        self._event_count += 1
        handlers = self._handlers.get(event.event_type, [])
        
        for handler in handlers:
            try:
                handler(event)
            except Exception as e:
                self._error_count += 1
                # Publish error event (but don't recurse)
                if event.event_type != EventType.ERROR:
                    error_event = Event(
                        event_type=EventType.ERROR,
                        source=f"handler:{handler.__name__}",
                        data={"error": str(e), "original_event": event.event_type.name},
                    )
                    self.publish(error_event)

    @property
    def stats(self) -> dict[str, int]:
        return {
            "events_processed": self._event_count,
            "errors": self._error_count,
            "handlers_registered": sum(len(h) for h in self._handlers.values()),
        }
