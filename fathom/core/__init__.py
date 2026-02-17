"""Fathom core — engine, events, strategy, risk, metrics, orders, telemetry."""

from fathom.core.engine import Engine
from fathom.core.events import Event, EventBus, EventType, PriceUpdate, Trade, OrderUpdate
from fathom.core.strategy import Strategy
from fathom.core.risk import (
    PositionSizer, SizingMethod, PortfolioLimits,
    DrawdownCircuitBreaker, BreakerState, ExposureTracker,
)
from fathom.core.metrics import (
    TradeJournal, TradeRecord, RoundTrip, RollingStats,
    compute_sharpe, compute_sortino, compute_calmar,
    compute_max_drawdown, compute_profit_factor, compute_expectancy,
)
from fathom.core.orders import (
    Order, OrderType, OrderStatus, OrderSide, TimeInForce,
    OrderBook, FillSimulator, Fill,
)
from fathom.core.telemetry import (
    LatencyTracker, PerformanceCounters, TelemetryExporter,
    track_latency, track_latency_sync,
)

__all__ = [
    "Engine", "Event", "EventBus", "EventType", "PriceUpdate", "Trade",
    "OrderUpdate", "Strategy",
    "PositionSizer", "SizingMethod", "PortfolioLimits",
    "DrawdownCircuitBreaker", "BreakerState", "ExposureTracker",
    "TradeJournal", "TradeRecord", "RoundTrip", "RollingStats",
    "compute_sharpe", "compute_sortino", "compute_calmar",
    "compute_max_drawdown", "compute_profit_factor", "compute_expectancy",
    "Order", "OrderType", "OrderStatus", "OrderSide", "TimeInForce",
    "OrderBook", "FillSimulator", "Fill",
    "LatencyTracker", "PerformanceCounters", "TelemetryExporter",
    "track_latency", "track_latency_sync",
]
