"""
Base classes for adapters and data feeds.

All exchange integrations implement these interfaces. The separation between
execution adapters (submit orders) and data feeds (stream prices) is intentional —
you might use Helius for data but Jupiter for execution.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from fathom.core.events import EventBus


class BaseAdapter(ABC):
    """
    Execution adapter — submits and manages orders on a venue.
    
    Adapters listen for ORDER_SUBMITTED events and produce
    ORDER_ACCEPTED/FILLED/REJECTED events.
    """
    
    name: str = "base_adapter"
    
    def __init__(self) -> None:
        self._event_bus: EventBus | None = None
        self._connected: bool = False

    def bind(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus

    @abstractmethod
    async def connect(self) -> None:
        """Establish connection to the venue."""
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        """Gracefully disconnect."""
        ...

    @abstractmethod
    async def submit_order(self, order: dict) -> str:
        """
        Submit an order. Returns transaction signature or order ID.
        Raises on failure.
        """
        ...

    @property
    def connected(self) -> bool:
        return self._connected


class BaseDataFeed(ABC):
    """
    Data feed — streams market data into the event bus.
    
    Feeds produce PRICE_UPDATE, TRADE, and ORDERBOOK_UPDATE events.
    """
    
    name: str = "base_feed"
    
    def __init__(self) -> None:
        self._event_bus: EventBus | None = None
        self._connected: bool = False

    def bind(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus

    @abstractmethod
    async def connect(self) -> None:
        """Start streaming data."""
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        """Stop streaming."""
        ...

    @property
    def connected(self) -> bool:
        return self._connected
