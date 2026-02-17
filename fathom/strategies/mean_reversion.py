"""Mean reversion strategy for Solana memecoins.

Uses Bollinger Bands and Z-score signals to identify when a token's
price has deviated significantly from its rolling mean, entering
positions that bet on reversion.  Includes dynamic band width
adaptation based on recent volatility regime.

The strategy is designed for tokens with established liquidity — it
should NOT be used on freshly-graduated tokens with insufficient
price history.

Example::

    from fathom.strategies.mean_reversion import MeanReversionStrategy

    strategy = MeanReversionStrategy(
        lookback=20,
        entry_z=-2.0,
        exit_z=-0.5,
        band_multiplier=2.0,
    )
    engine.add_strategy(strategy)
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from fathom.core.events import PriceUpdate
from fathom.core.strategy import Strategy

logger = logging.getLogger("fathom.strategy.mean_reversion")


@dataclass
class BollingerState:
    """Rolling Bollinger Band state for a single token.

    Attributes:
        prices: Recent price observations (ring buffer).
        lookback: Window size.
        mean: Current rolling mean.
        std: Current rolling standard deviation.
        upper: Upper band (mean + k*std).
        lower: Lower band (mean - k*std).
        z_score: Current Z-score of the last price.
        band_multiplier: Number of standard deviations for bands.
    """

    prices: List[float] = field(default_factory=list)
    lookback: int = 20
    mean: float = 0.0
    std: float = 0.0
    upper: float = 0.0
    lower: float = 0.0
    z_score: float = 0.0
    band_multiplier: float = 2.0

    def update(self, price: float) -> None:
        """Add a price observation and recompute bands.

        Args:
            price: New price observation.
        """
        self.prices.append(price)
        if len(self.prices) > self.lookback:
            self.prices.pop(0)

        n = len(self.prices)
        if n < 2:
            self.mean = price
            self.std = 0.0
            self.z_score = 0.0
            self.upper = price
            self.lower = price
            return

        self.mean = sum(self.prices) / n
        variance = sum((p - self.mean) ** 2 for p in self.prices) / (n - 1)
        self.std = math.sqrt(variance) if variance > 0 else 0.0

        self.upper = self.mean + self.band_multiplier * self.std
        self.lower = self.mean - self.band_multiplier * self.std

        if self.std > 0:
            self.z_score = (price - self.mean) / self.std
        else:
            self.z_score = 0.0

    @property
    def ready(self) -> bool:
        """Whether enough data has accumulated for signals."""
        return len(self.prices) >= self.lookback

    @property
    def bandwidth(self) -> float:
        """Bollinger bandwidth: ``(upper - lower) / mean``."""
        if self.mean <= 0:
            return 0.0
        return (self.upper - self.lower) / self.mean


class MeanReversionStrategy(Strategy):
    """Mean reversion strategy using Bollinger Bands and Z-scores.

    Enters a long position when the Z-score drops below ``entry_z``
    (oversold), and exits when the Z-score rises above ``exit_z``
    (reverted to mean).  An optional short mode can be enabled for
    overbought conditions.

    Args:
        lookback: Rolling window for Bollinger Band calculation.
        entry_z: Z-score threshold for entry (negative = oversold).
        exit_z: Z-score threshold for exit / take profit.
        band_multiplier: Number of standard deviations for bands.
        position_usd: Default position size in USD.
        max_positions: Maximum concurrent positions.
        min_bandwidth: Minimum bandwidth to consider a signal valid
            (filters out low-volatility regimes).
        adaptive_bands: If ``True``, dynamically adjust band width
            based on recent volatility regime.
        adaptive_scale_fast: Fast lookback for adaptive scaling.
        adaptive_scale_slow: Slow lookback for adaptive scaling.
        enable_short: Whether to take short positions on overbought signals.
        short_entry_z: Z-score threshold for short entry (positive = overbought).
        short_exit_z: Z-score threshold for short exit.
    """

    name: str = "mean_reversion"

    def __init__(
        self,
        lookback: int = 20,
        entry_z: float = -2.0,
        exit_z: float = -0.5,
        band_multiplier: float = 2.0,
        position_usd: float = 50.0,
        max_positions: int = 5,
        min_bandwidth: float = 0.01,
        adaptive_bands: bool = True,
        adaptive_scale_fast: int = 5,
        adaptive_scale_slow: int = 50,
        enable_short: bool = False,
        short_entry_z: float = 2.0,
        short_exit_z: float = 0.5,
    ) -> None:
        super().__init__()
        self.lookback = lookback
        self.entry_z = entry_z
        self.exit_z = exit_z
        self.band_multiplier = band_multiplier
        self.position_usd = position_usd
        self.max_positions = max_positions
        self.min_bandwidth = min_bandwidth
        self.adaptive_bands = adaptive_bands
        self.adaptive_scale_fast = adaptive_scale_fast
        self.adaptive_scale_slow = adaptive_scale_slow
        self.enable_short = enable_short
        self.short_entry_z = short_entry_z
        self.short_exit_z = short_exit_z

        self._states: Dict[str, BollingerState] = {}
        self._long_positions: Dict[str, float] = {}  # token -> entry_price
        self._short_positions: Dict[str, float] = {}
        self._vol_fast: Dict[str, List[float]] = defaultdict(list)
        self._vol_slow: Dict[str, List[float]] = defaultdict(list)

    def _get_state(self, token: str) -> BollingerState:
        """Get or create a BollingerState for a token."""
        if token not in self._states:
            self._states[token] = BollingerState(
                lookback=self.lookback,
                band_multiplier=self.band_multiplier,
            )
        return self._states[token]

    def _adaptive_multiplier(self, token: str) -> float:
        """Compute adaptive band multiplier based on vol regime.

        When recent (fast) volatility is higher than historical (slow),
        widen bands to avoid false signals.  When vol is contracting,
        tighten bands to be more responsive.

        Args:
            token: Token to compute for.

        Returns:
            Adjusted band multiplier.
        """
        fast = self._vol_fast.get(token, [])
        slow = self._vol_slow.get(token, [])
        if len(fast) < 2 or len(slow) < 2:
            return self.band_multiplier

        fast_std = _std(fast)
        slow_std = _std(slow)
        if slow_std <= 0:
            return self.band_multiplier

        ratio = fast_std / slow_std
        # Scale multiplier: expand when vol is high, contract when low
        return self.band_multiplier * max(0.5, min(ratio, 2.0))

    def on_price_update(self, event: PriceUpdate) -> None:
        """Process a price update and generate mean-reversion signals.

        Args:
            event: The price update event.
        """
        token = event.token
        price = event.price_usd
        if price <= 0:
            return

        # Track volatility for adaptive bands
        if self.adaptive_bands:
            self._vol_fast.setdefault(token, []).append(price)
            if len(self._vol_fast[token]) > self.adaptive_scale_fast:
                self._vol_fast[token].pop(0)
            self._vol_slow.setdefault(token, []).append(price)
            if len(self._vol_slow[token]) > self.adaptive_scale_slow:
                self._vol_slow[token].pop(0)

        state = self._get_state(token)

        # Apply adaptive bands if enabled
        if self.adaptive_bands:
            state.band_multiplier = self._adaptive_multiplier(token)

        state.update(price)

        if not state.ready:
            return

        # Skip low-volatility regimes
        if state.bandwidth < self.min_bandwidth:
            return

        active_count = len(self._long_positions) + len(self._short_positions)

        # --- Long signals ---
        if token in self._long_positions:
            # Exit: Z-score reverted above exit threshold
            if state.z_score >= self.exit_z:
                logger.info(
                    "[%s] EXIT LONG %s | z=%.2f price=%.6f mean=%.6f",
                    self.name, token, state.z_score, price, state.mean,
                )
                self.sell(token, amount=0.0)  # sell full position
                del self._long_positions[token]
        else:
            # Entry: Z-score dropped below entry threshold (oversold)
            if state.z_score <= self.entry_z and active_count < self.max_positions:
                logger.info(
                    "[%s] ENTER LONG %s | z=%.2f price=%.6f lower=%.6f",
                    self.name, token, state.z_score, price, state.lower,
                )
                self.buy(token, amount_usd=self.position_usd)
                self._long_positions[token] = price

        # --- Short signals (if enabled) ---
        if self.enable_short:
            if token in self._short_positions:
                if state.z_score <= self.short_exit_z:
                    logger.info(
                        "[%s] EXIT SHORT %s | z=%.2f price=%.6f",
                        self.name, token, state.z_score, price,
                    )
                    self.buy(token, amount_usd=self.position_usd)
                    del self._short_positions[token]
            else:
                if state.z_score >= self.short_entry_z and active_count < self.max_positions:
                    logger.info(
                        "[%s] ENTER SHORT %s | z=%.2f price=%.6f upper=%.6f",
                        self.name, token, state.z_score, price, state.upper,
                    )
                    self.sell(token, amount=0.0)
                    self._short_positions[token] = price

    def on_stop(self) -> None:
        """Log summary on shutdown."""
        logger.info(
            "[%s] stopped | long=%d short=%d tokens_tracked=%d",
            self.name,
            len(self._long_positions),
            len(self._short_positions),
            len(self._states),
        )
        super().on_stop()


def _std(values: List[float]) -> float:
    """Sample standard deviation."""
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    return math.sqrt(var) if var > 0 else 0.0
