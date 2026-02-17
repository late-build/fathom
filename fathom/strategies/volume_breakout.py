"""Volume breakout strategy for Solana memecoins.

Detects volume spikes that signal potential breakouts, confirms with
price action, and scales position size based on the magnitude of the
volume anomaly.  Designed for newly-graduated tokens where sudden
volume surges often precede large price moves.

Example::

    from fathom.strategies.volume_breakout import VolumeBreakoutStrategy

    strategy = VolumeBreakoutStrategy(
        volume_spike_threshold=3.0,
        confirmation_bars=2,
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

logger = logging.getLogger("fathom.strategy.volume_breakout")


@dataclass
class VolumeState:
    """Tracks volume and price history for breakout detection.

    Attributes:
        volumes: Recent volume observations.
        prices: Recent price observations.
        lookback: Window for computing average volume.
        spike_detected_at: Index when the last spike was detected.
        confirmation_count: Bars of positive price action after spike.
        in_position: Whether we currently hold this token.
        entry_price: Price at position entry.
        spike_magnitude: How many standard deviations the spike was.
    """

    volumes: List[float] = field(default_factory=list)
    prices: List[float] = field(default_factory=list)
    lookback: int = 20
    spike_detected_at: int = -1
    confirmation_count: int = 0
    in_position: bool = False
    entry_price: float = 0.0
    spike_magnitude: float = 0.0

    def add(self, price: float, volume: float) -> None:
        """Record a new observation.

        Args:
            price: Latest price.
            volume: Latest volume.
        """
        self.prices.append(price)
        self.volumes.append(volume)
        if len(self.prices) > self.lookback * 3:
            self.prices.pop(0)
        if len(self.volumes) > self.lookback * 3:
            self.volumes.pop(0)

    @property
    def avg_volume(self) -> float:
        """Rolling average volume over the lookback window."""
        window = self.volumes[-self.lookback:] if len(self.volumes) >= self.lookback else self.volumes
        return sum(window) / len(window) if window else 0.0

    @property
    def volume_std(self) -> float:
        """Rolling standard deviation of volume."""
        window = self.volumes[-self.lookback:] if len(self.volumes) >= self.lookback else self.volumes
        if len(window) < 2:
            return 0.0
        mean = sum(window) / len(window)
        var = sum((v - mean) ** 2 for v in window) / (len(window) - 1)
        return math.sqrt(var) if var > 0 else 0.0

    @property
    def ready(self) -> bool:
        """Whether enough data has accumulated."""
        return len(self.volumes) >= self.lookback

    @property
    def price_momentum(self) -> float:
        """Short-term price momentum (last 3 bars)."""
        if len(self.prices) < 4:
            return 0.0
        recent = self.prices[-3:]
        older = self.prices[-4]
        return (recent[-1] - older) / older if older > 0 else 0.0

    def is_volume_spike(self, threshold: float) -> tuple[bool, float]:
        """Check if the latest volume is a spike.

        Args:
            threshold: Number of standard deviations above mean.

        Returns:
            Tuple of ``(is_spike, magnitude_in_stds)``.
        """
        if not self.ready or not self.volumes:
            return False, 0.0
        latest = self.volumes[-1]
        avg = self.avg_volume
        std = self.volume_std
        if std <= 0 or avg <= 0:
            return False, 0.0
        z = (latest - avg) / std
        return z >= threshold, z

    def has_price_volume_divergence(self) -> bool:
        """Detect bullish divergence: volume up but price flat/down.

        This often precedes a breakout as accumulation occurs.

        Returns:
            ``True`` if divergence is detected.
        """
        if len(self.prices) < 5 or len(self.volumes) < 5:
            return False
        price_change = (self.prices[-1] - self.prices[-5]) / self.prices[-5] if self.prices[-5] > 0 else 0
        vol_change = (self.volumes[-1] - self.volumes[-5]) / self.volumes[-5] if self.volumes[-5] > 0 else 0
        # Volume up significantly, price flat or slightly down
        return vol_change > 0.5 and price_change < 0.05


class VolumeBreakoutStrategy(Strategy):
    """Volume breakout strategy with confirmation and position scaling.

    Workflow:
    1. Detect a volume spike (Z-score above threshold).
    2. Wait for ``confirmation_bars`` bars of positive price action.
    3. Enter with position size scaled by spike magnitude.
    4. Exit on trailing stop or mean reversion of volume.

    Args:
        volume_spike_threshold: Z-score threshold for spike detection.
        confirmation_bars: Bars of positive price action needed after spike.
        base_position_usd: Base position size before scaling.
        max_position_usd: Maximum position size after scaling.
        scale_factor: How much to scale position per std of volume spike.
        lookback: Rolling window for volume statistics.
        trailing_stop_pct: Trailing stop as fraction of price.
        max_positions: Maximum concurrent positions.
        volume_exit_threshold: Exit when volume drops below this fraction
            of the spike volume.
        divergence_mode: If ``True``, also enter on price-volume divergence.
    """

    name: str = "volume_breakout"

    def __init__(
        self,
        volume_spike_threshold: float = 3.0,
        confirmation_bars: int = 2,
        base_position_usd: float = 50.0,
        max_position_usd: float = 200.0,
        scale_factor: float = 0.25,
        lookback: int = 20,
        trailing_stop_pct: float = 0.10,
        max_positions: int = 5,
        volume_exit_threshold: float = 0.3,
        divergence_mode: bool = False,
    ) -> None:
        super().__init__()
        self.volume_spike_threshold = volume_spike_threshold
        self.confirmation_bars = confirmation_bars
        self.base_position_usd = base_position_usd
        self.max_position_usd = max_position_usd
        self.scale_factor = scale_factor
        self.lookback = lookback
        self.trailing_stop_pct = trailing_stop_pct
        self.max_positions = max_positions
        self.volume_exit_threshold = volume_exit_threshold
        self.divergence_mode = divergence_mode

        self._states: Dict[str, VolumeState] = {}
        self._trailing_highs: Dict[str, float] = {}

    def _get_state(self, token: str) -> VolumeState:
        """Get or create volume state for a token."""
        if token not in self._states:
            self._states[token] = VolumeState(lookback=self.lookback)
        return self._states[token]

    def _compute_position_size(self, magnitude: float) -> float:
        """Scale position based on volume spike magnitude.

        Args:
            magnitude: Volume spike in standard deviations.

        Returns:
            Position size in USD.
        """
        scaled = self.base_position_usd * (1.0 + self.scale_factor * magnitude)
        return min(scaled, self.max_position_usd)

    def on_price_update(self, event: PriceUpdate) -> None:
        """Process a price update and generate breakout signals.

        Args:
            event: The price update event.
        """
        token = event.token
        price = event.price_usd
        volume = event.volume_24h
        if price <= 0:
            return

        state = self._get_state(token)
        state.add(price, volume)

        if not state.ready:
            return

        # --- Position management (exits) ---
        if state.in_position:
            self._manage_position(token, price, state)
            return

        # --- Entry signals ---
        active_positions = sum(1 for s in self._states.values() if s.in_position)
        if active_positions >= self.max_positions:
            return

        is_spike, magnitude = state.is_volume_spike(self.volume_spike_threshold)

        # Divergence mode: enter on divergence signal
        if self.divergence_mode and state.has_price_volume_divergence():
            if not is_spike:
                is_spike = True
                magnitude = self.volume_spike_threshold  # Use base threshold

        if is_spike:
            if state.spike_detected_at < 0:
                state.spike_detected_at = len(state.prices) - 1
                state.spike_magnitude = magnitude
                state.confirmation_count = 0
                logger.info(
                    "[%s] SPIKE %s | mag=%.1fσ vol=%.0f avg=%.0f",
                    self.name, token, magnitude, volume, state.avg_volume,
                )

        # Confirmation: positive price action after spike
        if state.spike_detected_at >= 0:
            if state.price_momentum > 0:
                state.confirmation_count += 1
            else:
                # Reset if price turns negative
                state.spike_detected_at = -1
                state.confirmation_count = 0
                return

            if state.confirmation_count >= self.confirmation_bars:
                size = self._compute_position_size(state.spike_magnitude)
                logger.info(
                    "[%s] ENTER %s | price=%.6f size=$%.2f mag=%.1fσ",
                    self.name, token, price, size, state.spike_magnitude,
                )
                self.buy(token, amount_usd=size)
                state.in_position = True
                state.entry_price = price
                self._trailing_highs[token] = price
                state.spike_detected_at = -1

    def _manage_position(
        self,
        token: str,
        price: float,
        state: VolumeState,
    ) -> None:
        """Manage an open position (trailing stop, volume exit).

        Args:
            token: Token identifier.
            price: Current price.
            state: Volume state for this token.
        """
        # Update trailing high
        high = self._trailing_highs.get(token, price)
        if price > high:
            high = price
            self._trailing_highs[token] = high

        # Trailing stop
        stop_price = high * (1 - self.trailing_stop_pct)
        if price <= stop_price:
            pnl_pct = (price - state.entry_price) / state.entry_price * 100
            logger.info(
                "[%s] EXIT TRAILING %s | price=%.6f pnl=%.1f%%",
                self.name, token, price, pnl_pct,
            )
            self.sell(token, amount=0.0)
            state.in_position = False
            state.entry_price = 0.0
            self._trailing_highs.pop(token, None)
            return

        # Volume exit: volume has died down
        if state.avg_volume > 0:
            current_vol_ratio = (state.volumes[-1] if state.volumes else 0) / state.avg_volume
            if current_vol_ratio < self.volume_exit_threshold:
                pnl_pct = (price - state.entry_price) / state.entry_price * 100
                logger.info(
                    "[%s] EXIT VOLUME %s | price=%.6f vol_ratio=%.2f pnl=%.1f%%",
                    self.name, token, price, current_vol_ratio, pnl_pct,
                )
                self.sell(token, amount=0.0)
                state.in_position = False
                state.entry_price = 0.0
                self._trailing_highs.pop(token, None)

    def on_stop(self) -> None:
        """Log summary on shutdown."""
        active = sum(1 for s in self._states.values() if s.in_position)
        logger.info(
            "[%s] stopped | active=%d tokens_tracked=%d",
            self.name, active, len(self._states),
        )
        super().on_stop()
