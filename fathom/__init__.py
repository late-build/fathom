"""
Fathom — High-performance Solana DEX trading engine.

Usage:
    python -m fathom run --mode paper
    python -m fathom monitor
    python -m fathom backtest --data graduations.json
    python -m fathom quote SOL 100
    python -m fathom status
"""

__version__ = "0.1.0"

from fathom.core.engine import Engine
from fathom.core.strategy import Strategy
from fathom.adapters.jupiter.adapter import JupiterAdapter
from fathom.adapters.helius.feed import HeliusDataFeed
from fathom.adapters.paper import PaperAdapter
from fathom.adapters.pumpfun.graduation import GraduationMonitor
from fathom.config import FathomConfig, load_config

from fathom.core.risk import PositionSizer, PortfolioLimits, DrawdownCircuitBreaker, ExposureTracker
from fathom.core.metrics import TradeJournal, compute_sharpe, compute_sortino
from fathom.core.orders import Order, OrderType, OrderBook, FillSimulator
from fathom.core.telemetry import LatencyTracker, PerformanceCounters, TelemetryExporter
from fathom.data.normalize import OHLCVBar, resample, parse_dexscreener
from fathom.strategies.mean_reversion import MeanReversionStrategy
from fathom.strategies.volume_breakout import VolumeBreakoutStrategy
from fathom.strategies.composite import CompositeStrategy

__all__ = [
    "Engine",
    "Strategy",
    "JupiterAdapter",
    "HeliusDataFeed",
    "PaperAdapter",
    "GraduationMonitor",
    "FathomConfig",
    "load_config",
    "PositionSizer",
    "PortfolioLimits",
    "DrawdownCircuitBreaker",
    "ExposureTracker",
    "TradeJournal",
    "compute_sharpe",
    "compute_sortino",
    "Order",
    "OrderType",
    "OrderBook",
    "FillSimulator",
    "LatencyTracker",
    "PerformanceCounters",
    "TelemetryExporter",
    "OHLCVBar",
    "resample",
    "parse_dexscreener",
    "MeanReversionStrategy",
    "VolumeBreakoutStrategy",
    "CompositeStrategy",
]
