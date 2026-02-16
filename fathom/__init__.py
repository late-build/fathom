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

__all__ = [
    "Engine",
    "Strategy",
    "JupiterAdapter",
    "HeliusDataFeed",
    "PaperAdapter",
    "GraduationMonitor",
    "FathomConfig",
    "load_config",
]
