"""
Fathom Engine — orchestrates adapters, strategies, and the event bus.

The engine is the central coordinator. It manages the lifecycle of all
components and routes events between them.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import time
from typing import TYPE_CHECKING

from fathom.core.events import EventBus, Event, EventType

if TYPE_CHECKING:
    from fathom.core.strategy import Strategy
    from fathom.adapters.base import BaseAdapter, BaseDataFeed

logger = logging.getLogger("fathom.engine")


class Engine:
    """
    Core trading engine.
    
    Manages the event loop, adapter connections, strategy execution,
    and graceful shutdown.
    
    Usage:
        engine = Engine()
        engine.add_adapter(JupiterAdapter(...))
        engine.add_data_feed(HeliusDataFeed(...))
        engine.add_strategy(MyStrategy(...))
        engine.run()
    """
    
    def __init__(self, mode: str = "live") -> None:
        """
        Args:
            mode: "live" for real execution, "backtest" for historical replay,
                  "paper" for live data with simulated execution.
        """
        if mode not in ("live", "backtest", "paper"):
            raise ValueError(f"Invalid mode: {mode}. Must be 'live', 'backtest', or 'paper'.")
        
        self.mode = mode
        self.event_bus = EventBus()
        self._adapters: list[BaseAdapter] = []
        self._data_feeds: list[BaseDataFeed] = []
        self._strategies: list[Strategy] = []
        self._running = False
        self._start_time_ns: int = 0
        
        # Wire up internal handlers
        self.event_bus.subscribe(EventType.ERROR, self._on_error)
        
        logger.info(f"Fathom engine initialized (mode={mode})")

    def add_adapter(self, adapter: BaseAdapter) -> None:
        """Register an execution adapter (e.g., Jupiter for swaps)."""
        adapter.bind(self.event_bus)
        self._adapters.append(adapter)
        logger.info(f"Adapter registered: {adapter.name}")

    def add_data_feed(self, feed: BaseDataFeed) -> None:
        """Register a data feed (e.g., Helius WebSocket)."""
        feed.bind(self.event_bus)
        self._data_feeds.append(feed)
        logger.info(f"Data feed registered: {feed.name}")

    def add_strategy(self, strategy: Strategy) -> None:
        """Register a trading strategy."""
        strategy.bind(self.event_bus)
        self._strategies.append(strategy)
        logger.info(f"Strategy registered: {strategy.name}")

    def run(self) -> None:
        """
        Start the engine.
        
        This blocks until the engine is stopped via SIGINT/SIGTERM
        or engine.stop() is called.
        """
        self._running = True
        self._start_time_ns = time.time_ns()
        
        self.event_bus.publish(Event(
            event_type=EventType.ENGINE_START,
            source="engine",
            data={"mode": self.mode},
        ))
        
        logger.info("Engine starting...")
        
        try:
            asyncio.run(self._run_async())
        except KeyboardInterrupt:
            logger.info("Keyboard interrupt received")
        finally:
            self.stop()

    async def _run_async(self) -> None:
        """Main async event loop."""
        loop = asyncio.get_event_loop()
        
        # Handle signals for graceful shutdown
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self.stop)
        
        # Connect all adapters and feeds
        connect_tasks = []
        for adapter in self._adapters:
            connect_tasks.append(adapter.connect())
        for feed in self._data_feeds:
            connect_tasks.append(feed.connect())
        
        if connect_tasks:
            await asyncio.gather(*connect_tasks, return_exceptions=True)
        
        # Initialize strategies
        for strategy in self._strategies:
            strategy.on_start()
        
        logger.info(
            f"Engine running | adapters={len(self._adapters)} "
            f"feeds={len(self._data_feeds)} strategies={len(self._strategies)}"
        )
        
        # Run until stopped
        while self._running:
            await asyncio.sleep(0.1)
            
            # Publish heartbeat
            self.event_bus.publish(Event(
                event_type=EventType.HEARTBEAT,
                source="engine",
                data=self.status,
            ))

    def stop(self) -> None:
        """Gracefully stop the engine."""
        if not self._running:
            return
            
        self._running = False
        
        # Stop strategies
        for strategy in self._strategies:
            try:
                strategy.on_stop()
            except Exception as e:
                logger.error(f"Error stopping strategy {strategy.name}: {e}")
        
        # Disconnect adapters and feeds
        for adapter in self._adapters:
            try:
                asyncio.get_event_loop().run_until_complete(adapter.disconnect())
            except Exception:
                pass
        
        for feed in self._data_feeds:
            try:
                asyncio.get_event_loop().run_until_complete(feed.disconnect())
            except Exception:
                pass
        
        self.event_bus.publish(Event(
            event_type=EventType.ENGINE_STOP,
            source="engine",
        ))
        
        uptime = (time.time_ns() - self._start_time_ns) / 1e9 if self._start_time_ns else 0
        logger.info(f"Engine stopped | uptime={uptime:.1f}s | {self.event_bus.stats}")

    def _on_error(self, event: Event) -> None:
        logger.error(f"[{event.source}] {event.data.get('error', 'unknown error')}")

    @property
    def status(self) -> dict:
        uptime = (time.time_ns() - self._start_time_ns) / 1e9 if self._start_time_ns else 0
        return {
            "mode": self.mode,
            "running": self._running,
            "uptime_s": round(uptime, 1),
            "adapters": len(self._adapters),
            "data_feeds": len(self._data_feeds),
            "strategies": len(self._strategies),
            **self.event_bus.stats,
        }
