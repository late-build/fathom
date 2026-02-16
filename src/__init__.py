"""
Fathom — High-performance Solana DEX trading engine.
"""

__version__ = "0.1.0"

from fathom.core.engine import Engine
from fathom.adapters.jupiter.adapter import JupiterAdapter
from fathom.adapters.helius.feed import HeliusDataFeed

__all__ = ["Engine", "JupiterAdapter", "HeliusDataFeed"]
