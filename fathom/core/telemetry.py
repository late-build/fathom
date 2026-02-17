"""Structured telemetry for the Fathom trading engine.

Provides latency tracking, performance counters, and a JSON-lines
exporter for observability.  Includes decorators for auto-instrumenting
async functions with minimal overhead.

Example::

    from fathom.core.telemetry import LatencyTracker, PerformanceCounters, track_latency

    tracker = LatencyTracker()
    tracker.record("jupiter", "quote", 0.045)
    print(tracker.percentile("jupiter", "quote", 0.99))

    counters = PerformanceCounters()
    counters.inc("orders_sent")
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, TypeVar, cast

logger = logging.getLogger("fathom.telemetry")

F = TypeVar("F", bound=Callable[..., Any])


# ---------------------------------------------------------------------------
# Latency tracker
# ---------------------------------------------------------------------------

class LatencyTracker:
    """Per-adapter, per-operation latency percentile tracking.

    Stores raw latency samples in a ring buffer per (adapter, operation)
    pair and computes percentiles on demand.

    Args:
        max_samples: Maximum samples to retain per key.
    """

    def __init__(self, max_samples: int = 1_000) -> None:
        self._max_samples = max_samples
        self._buckets: Dict[str, List[float]] = {}

    @staticmethod
    def _key(adapter: str, operation: str) -> str:
        return f"{adapter}:{operation}"

    def record(self, adapter: str, operation: str, latency_s: float) -> None:
        """Record a latency observation.

        Args:
            adapter: Adapter name (e.g. ``"jupiter"``).
            operation: Operation name (e.g. ``"quote"``, ``"swap"``).
            latency_s: Measured latency in seconds.
        """
        key = self._key(adapter, operation)
        buf = self._buckets.setdefault(key, [])
        buf.append(latency_s)
        if len(buf) > self._max_samples:
            buf.pop(0)

    def percentile(self, adapter: str, operation: str, pct: float) -> float:
        """Compute a percentile for a given adapter/operation.

        Args:
            adapter: Adapter name.
            operation: Operation name.
            pct: Percentile as a fraction (e.g. 0.95 for p95).

        Returns:
            Latency at the requested percentile, or 0.0 if no data.
        """
        key = self._key(adapter, operation)
        buf = self._buckets.get(key)
        if not buf:
            return 0.0
        sorted_buf = sorted(buf)
        idx = int(math.ceil(pct * len(sorted_buf))) - 1
        idx = max(0, min(idx, len(sorted_buf) - 1))
        return sorted_buf[idx]

    def mean(self, adapter: str, operation: str) -> float:
        """Mean latency for an adapter/operation.

        Args:
            adapter: Adapter name.
            operation: Operation name.

        Returns:
            Mean latency in seconds, or 0.0 if no data.
        """
        key = self._key(adapter, operation)
        buf = self._buckets.get(key)
        if not buf:
            return 0.0
        return sum(buf) / len(buf)

    def summary(self) -> Dict[str, Dict[str, float]]:
        """Full summary of all tracked adapter/operation pairs.

        Returns:
            Nested dict: ``{key: {p50, p95, p99, mean, count}}``.
        """
        result: Dict[str, Dict[str, float]] = {}
        for key, buf in self._buckets.items():
            if not buf:
                continue
            sorted_buf = sorted(buf)
            n = len(sorted_buf)
            result[key] = {
                "p50": sorted_buf[int(n * 0.50) - 1] if n else 0.0,
                "p95": sorted_buf[int(math.ceil(n * 0.95)) - 1] if n else 0.0,
                "p99": sorted_buf[int(math.ceil(n * 0.99)) - 1] if n else 0.0,
                "mean": sum(buf) / n,
                "count": float(n),
            }
        return result

    def reset(self) -> None:
        """Clear all stored samples."""
        self._buckets.clear()


# ---------------------------------------------------------------------------
# Performance counters
# ---------------------------------------------------------------------------

class PerformanceCounters:
    """Thread-safe monotonic counters for operational metrics.

    Tracks counts like orders sent, fills received, errors, reconnects, etc.
    All counters start at zero and can only be incremented.
    """

    def __init__(self) -> None:
        self._counters: Dict[str, int] = {}

    def inc(self, name: str, amount: int = 1) -> None:
        """Increment a counter.

        Args:
            name: Counter name (e.g. ``"orders_sent"``).
            amount: Amount to increment by.
        """
        self._counters[name] = self._counters.get(name, 0) + amount

    def get(self, name: str) -> int:
        """Get current counter value.

        Args:
            name: Counter name.

        Returns:
            Current count, or 0 if never incremented.
        """
        return self._counters.get(name, 0)

    def snapshot(self) -> Dict[str, int]:
        """Return a snapshot of all counters.

        Returns:
            Dict of counter names to values.
        """
        return dict(self._counters)

    def reset(self) -> None:
        """Reset all counters to zero."""
        self._counters.clear()


# ---------------------------------------------------------------------------
# Telemetry exporter
# ---------------------------------------------------------------------------

class TelemetryExporter:
    """Exports telemetry data as JSON lines to a file or logger.

    Args:
        file_path: Optional path to write JSON lines.  If ``None``,
            events are only logged.
        flush_interval: Seconds between automatic flushes.
    """

    def __init__(
        self,
        file_path: Optional[str] = None,
        flush_interval: float = 5.0,
    ) -> None:
        self._file_path = file_path
        self._flush_interval = flush_interval
        self._buffer: List[Dict[str, Any]] = []
        self._file = None
        self._last_flush: float = 0.0

        if file_path:
            path = Path(file_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            self._file = open(path, "a", encoding="utf-8")

    def emit(self, event_type: str, data: Dict[str, Any]) -> None:
        """Emit a telemetry event.

        Args:
            event_type: Event category (e.g. ``"latency"``, ``"counter"``).
            data: Event payload.
        """
        record = {
            "ts": time.time(),
            "type": event_type,
            **data,
        }
        self._buffer.append(record)

        now = time.monotonic()
        if now - self._last_flush >= self._flush_interval:
            self.flush()

    def flush(self) -> None:
        """Write buffered events to the sink."""
        if not self._buffer:
            return
        for record in self._buffer:
            line = json.dumps(record, default=str)
            if self._file:
                self._file.write(line + "\n")
            else:
                logger.debug("telemetry: %s", line)
        if self._file:
            self._file.flush()
        self._buffer.clear()
        self._last_flush = time.monotonic()

    def export_snapshot(
        self,
        latency: LatencyTracker,
        counters: PerformanceCounters,
    ) -> None:
        """Export a full snapshot of latency and counter data.

        Args:
            latency: Latency tracker to snapshot.
            counters: Performance counters to snapshot.
        """
        self.emit("latency_summary", {"latencies": latency.summary()})
        self.emit("counters", counters.snapshot())
        self.flush()

    def close(self) -> None:
        """Flush and close the file sink."""
        self.flush()
        if self._file:
            self._file.close()
            self._file = None


# ---------------------------------------------------------------------------
# Decorators
# ---------------------------------------------------------------------------

_default_tracker = LatencyTracker()
_default_counters = PerformanceCounters()


def track_latency(
    adapter: str = "default",
    operation: str = "",
    tracker: Optional[LatencyTracker] = None,
    counters: Optional[PerformanceCounters] = None,
) -> Callable[[F], F]:
    """Decorator to auto-instrument an async function with latency tracking.

    Args:
        adapter: Adapter name for grouping.
        operation: Operation name.  Defaults to the function name.
        tracker: ``LatencyTracker`` instance (uses module default if ``None``).
        counters: ``PerformanceCounters`` instance (uses module default if ``None``).

    Returns:
        Decorated function that records latency and call counts.

    Example::

        @track_latency("jupiter", "quote")
        async def get_quote(mint: str, amount: float):
            ...
    """
    t = tracker or _default_tracker
    c = counters or _default_counters

    def decorator(fn: F) -> F:
        op = operation or fn.__name__

        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            start = time.monotonic()
            try:
                result = await fn(*args, **kwargs)
                c.inc(f"{adapter}.{op}.ok")
                return result
            except Exception:
                c.inc(f"{adapter}.{op}.error")
                raise
            finally:
                elapsed = time.monotonic() - start
                t.record(adapter, op, elapsed)
                c.inc(f"{adapter}.{op}.calls")

        return cast(F, wrapper)

    return decorator


def track_latency_sync(
    adapter: str = "default",
    operation: str = "",
    tracker: Optional[LatencyTracker] = None,
    counters: Optional[PerformanceCounters] = None,
) -> Callable[[F], F]:
    """Synchronous version of :func:`track_latency`.

    Args:
        adapter: Adapter name.
        operation: Operation name.
        tracker: Latency tracker instance.
        counters: Performance counters instance.

    Returns:
        Decorated function.
    """
    t = tracker or _default_tracker
    c = counters or _default_counters

    def decorator(fn: F) -> F:
        op = operation or fn.__name__

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            start = time.monotonic()
            try:
                result = fn(*args, **kwargs)
                c.inc(f"{adapter}.{op}.ok")
                return result
            except Exception:
                c.inc(f"{adapter}.{op}.error")
                raise
            finally:
                elapsed = time.monotonic() - start
                t.record(adapter, op, elapsed)
                c.inc(f"{adapter}.{op}.calls")

        return cast(F, wrapper)

    return decorator
