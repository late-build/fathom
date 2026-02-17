"""Composite multi-factor strategy for the Fathom trading engine.

Combines multiple signal sources with configurable weights and a
consensus threshold.  Each signal is normalised to [-1, +1] before
weighting, enabling heterogeneous signal combination (momentum + mean
reversion + volume, etc.).

Tracks per-signal performance attribution so you can see which factors
are actually contributing alpha.

Example::

    from fathom.strategies.composite import (
        CompositeStrategy, SignalSource, MomentumSignal, MeanReversionSignal,
    )

    strategy = CompositeStrategy(
        signals=[
            MomentumSignal(lookback=10, weight=0.4),
            MeanReversionSignal(lookback=20, weight=0.3),
            VolumeSignal(spike_threshold=2.5, weight=0.3),
        ],
        consensus_threshold=0.5,
    )
    engine.add_strategy(strategy)
"""

from __future__ import annotations

import logging
import math
from abc import ABC, abstractmethod
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from fathom.core.events import PriceUpdate
from fathom.core.strategy import Strategy

logger = logging.getLogger("fathom.strategy.composite")


# ---------------------------------------------------------------------------
# Signal framework
# ---------------------------------------------------------------------------

@dataclass
class SignalOutput:
    """Output from a single signal source.

    Attributes:
        name: Signal source name.
        value: Raw signal value (will be normalised to [-1, +1]).
        confidence: Confidence in the signal (0-1).
        metadata: Arbitrary context for debugging.
    """

    __slots__ = ("name", "value", "confidence", "metadata")
    name: str
    value: float
    confidence: float
    metadata: Dict[str, float]


class SignalSource(ABC):
    """Abstract base for signal sources in the composite strategy.

    Subclass this to create a new signal type.  The composite strategy
    will call ``update()`` on each price tick and ``signal()`` to get
    the current signal value.

    Args:
        weight: Relative weight of this signal (0-1).
        name: Human-readable name.
    """

    def __init__(self, weight: float = 1.0, name: str = "unnamed") -> None:
        self.weight = weight
        self.name = name

    @abstractmethod
    def update(self, token: str, price: float, volume: float) -> None:
        """Feed a new price/volume observation.

        Args:
            token: Token identifier.
            price: Latest price.
            volume: Latest volume.
        """
        ...

    @abstractmethod
    def signal(self, token: str) -> Optional[SignalOutput]:
        """Compute the current signal for a token.

        Args:
            token: Token identifier.

        Returns:
            ``SignalOutput`` or ``None`` if insufficient data.
        """
        ...

    @abstractmethod
    def ready(self, token: str) -> bool:
        """Whether this signal has enough data for the given token."""
        ...


# ---------------------------------------------------------------------------
# Built-in signal sources
# ---------------------------------------------------------------------------

class MomentumSignal(SignalSource):
    """Rate-of-change momentum signal.

    Signal = ``(price - price_n_bars_ago) / price_n_bars_ago``,
    clamped to [-1, +1].

    Args:
        lookback: Number of bars for momentum calculation.
        weight: Signal weight.
    """

    def __init__(self, lookback: int = 10, weight: float = 1.0) -> None:
        super().__init__(weight=weight, name="momentum")
        self.lookback = lookback
        self._prices: Dict[str, List[float]] = defaultdict(list)

    def update(self, token: str, price: float, volume: float) -> None:
        self._prices[token].append(price)
        if len(self._prices[token]) > self.lookback * 3:
            self._prices[token] = self._prices[token][-self.lookback * 2:]

    def ready(self, token: str) -> bool:
        return len(self._prices.get(token, [])) > self.lookback

    def signal(self, token: str) -> Optional[SignalOutput]:
        prices = self._prices.get(token, [])
        if len(prices) <= self.lookback:
            return None
        old = prices[-self.lookback - 1]
        if old <= 0:
            return None
        roc = (prices[-1] - old) / old
        normalised = max(-1.0, min(1.0, roc * 5))  # Scale for sensitivity
        confidence = min(1.0, len(prices) / (self.lookback * 2))
        return SignalOutput(
            name=self.name,
            value=normalised,
            confidence=confidence,
            metadata={"roc": roc, "lookback": float(self.lookback)},
        )


class MeanReversionSignal(SignalSource):
    """Bollinger Band Z-score signal (inverted for mean reversion).

    Signal = ``-z_score / band_multiplier``, clamped to [-1, +1].
    Negative Z (oversold) produces positive signal (buy).

    Args:
        lookback: Rolling window.
        band_multiplier: Standard deviation multiplier.
        weight: Signal weight.
    """

    def __init__(
        self,
        lookback: int = 20,
        band_multiplier: float = 2.0,
        weight: float = 1.0,
    ) -> None:
        super().__init__(weight=weight, name="mean_reversion")
        self.lookback = lookback
        self.band_multiplier = band_multiplier
        self._prices: Dict[str, List[float]] = defaultdict(list)

    def update(self, token: str, price: float, volume: float) -> None:
        self._prices[token].append(price)
        if len(self._prices[token]) > self.lookback * 3:
            self._prices[token] = self._prices[token][-self.lookback * 2:]

    def ready(self, token: str) -> bool:
        return len(self._prices.get(token, [])) >= self.lookback

    def signal(self, token: str) -> Optional[SignalOutput]:
        prices = self._prices.get(token, [])
        if len(prices) < self.lookback:
            return None
        window = prices[-self.lookback:]
        mean = sum(window) / len(window)
        var = sum((p - mean) ** 2 for p in window) / (len(window) - 1)
        std = math.sqrt(var) if var > 0 else 0.0
        if std <= 0:
            return None
        z = (prices[-1] - mean) / std
        normalised = max(-1.0, min(1.0, -z / self.band_multiplier))
        return SignalOutput(
            name=self.name,
            value=normalised,
            confidence=min(1.0, len(prices) / (self.lookback * 2)),
            metadata={"z_score": z, "mean": mean, "std": std},
        )


class VolumeSignal(SignalSource):
    """Volume spike signal.

    Signal = ``(volume - avg) / std / threshold``, clamped to [0, +1].
    Only produces positive (buy) signals.

    Args:
        lookback: Rolling window for volume stats.
        spike_threshold: Z-score threshold for a spike.
        weight: Signal weight.
    """

    def __init__(
        self,
        lookback: int = 20,
        spike_threshold: float = 2.5,
        weight: float = 1.0,
    ) -> None:
        super().__init__(weight=weight, name="volume")
        self.lookback = lookback
        self.spike_threshold = spike_threshold
        self._volumes: Dict[str, List[float]] = defaultdict(list)

    def update(self, token: str, price: float, volume: float) -> None:
        self._volumes[token].append(volume)
        if len(self._volumes[token]) > self.lookback * 3:
            self._volumes[token] = self._volumes[token][-self.lookback * 2:]

    def ready(self, token: str) -> bool:
        return len(self._volumes.get(token, [])) >= self.lookback

    def signal(self, token: str) -> Optional[SignalOutput]:
        vols = self._volumes.get(token, [])
        if len(vols) < self.lookback:
            return None
        window = vols[-self.lookback:]
        mean = sum(window) / len(window)
        var = sum((v - mean) ** 2 for v in window) / (len(window) - 1)
        std = math.sqrt(var) if var > 0 else 0.0
        if std <= 0 or mean <= 0:
            return None
        z = (vols[-1] - mean) / std
        normalised = max(0.0, min(1.0, z / self.spike_threshold))
        return SignalOutput(
            name=self.name,
            value=normalised,
            confidence=min(1.0, len(vols) / (self.lookback * 2)),
            metadata={"vol_z": z, "vol_mean": mean},
        )


# ---------------------------------------------------------------------------
# Performance attribution
# ---------------------------------------------------------------------------

@dataclass
class SignalAttribution:
    """Per-signal performance attribution.

    Attributes:
        name: Signal name.
        total_contribution: Weighted signal value summed across all trades.
        correct_calls: Times the signal direction matched trade outcome.
        total_calls: Total signal contributions.
        avg_signal_at_entry: Average signal value when entering a position.
    """

    name: str = ""
    total_contribution: float = 0.0
    correct_calls: int = 0
    total_calls: int = 0
    avg_signal_at_entry: float = 0.0
    _signal_sum: float = 0.0

    @property
    def accuracy(self) -> float:
        """Fraction of correct directional calls."""
        return self.correct_calls / self.total_calls if self.total_calls > 0 else 0.0


# ---------------------------------------------------------------------------
# Composite strategy
# ---------------------------------------------------------------------------

class CompositeStrategy(Strategy):
    """Multi-factor composite strategy with signal weighting and attribution.

    Collects signals from multiple ``SignalSource`` instances, normalises
    and weights them, and enters a position when the composite score
    exceeds a consensus threshold.

    Args:
        signals: List of signal sources.
        consensus_threshold: Minimum weighted composite score to enter (0-1).
        position_usd: Base position size.
        max_positions: Maximum concurrent positions.
        exit_threshold: Composite score at which to exit (below this = sell).
        min_signals_required: Minimum number of signals that must be ready
            before trading.
    """

    name: str = "composite"

    def __init__(
        self,
        signals: Optional[List[SignalSource]] = None,
        consensus_threshold: float = 0.5,
        position_usd: float = 50.0,
        max_positions: int = 5,
        exit_threshold: float = 0.0,
        min_signals_required: int = 2,
    ) -> None:
        super().__init__()
        self.signals: List[SignalSource] = signals or []
        self.consensus_threshold = consensus_threshold
        self.position_usd = position_usd
        self.max_positions = max_positions
        self.exit_threshold = exit_threshold
        self.min_signals_required = min_signals_required

        self._positions: Dict[str, float] = {}  # token -> entry composite score
        self._entry_signals: Dict[str, Dict[str, float]] = {}  # token -> {signal: value}
        self._attribution: Dict[str, SignalAttribution] = {}

        # Initialise attribution tracking
        for sig in self.signals:
            self._attribution[sig.name] = SignalAttribution(name=sig.name)

    def _compute_composite(self, token: str) -> tuple[float, Dict[str, float]]:
        """Compute the weighted composite signal for a token.

        Args:
            token: Token identifier.

        Returns:
            Tuple of ``(composite_score, per_signal_values)``.
        """
        total_weight = 0.0
        weighted_sum = 0.0
        signal_values: Dict[str, float] = {}
        ready_count = 0

        for sig in self.signals:
            if not sig.ready(token):
                continue
            ready_count += 1
            output = sig.signal(token)
            if output is None:
                continue
            weighted_value = output.value * output.confidence * sig.weight
            weighted_sum += weighted_value
            total_weight += sig.weight * output.confidence
            signal_values[sig.name] = output.value

        if total_weight <= 0 or ready_count < self.min_signals_required:
            return 0.0, signal_values

        return weighted_sum / total_weight, signal_values

    def _record_attribution(
        self,
        signal_values: Dict[str, float],
        trade_won: bool,
    ) -> None:
        """Update per-signal attribution after a trade closes.

        Args:
            signal_values: Signal values at entry.
            trade_won: Whether the trade was profitable.
        """
        for name, value in signal_values.items():
            attr = self._attribution.get(name)
            if attr is None:
                continue
            attr.total_calls += 1
            attr.total_contribution += value
            attr._signal_sum += value
            attr.avg_signal_at_entry = attr._signal_sum / attr.total_calls
            if (value > 0 and trade_won) or (value <= 0 and not trade_won):
                attr.correct_calls += 1

    def on_price_update(self, event: PriceUpdate) -> None:
        """Process price update and generate composite signals.

        Args:
            event: The price update event.
        """
        token = event.token
        price = event.price_usd
        volume = event.volume_24h
        if price <= 0:
            return

        # Feed all signals
        for sig in self.signals:
            sig.update(token, price, volume)

        composite, signal_values = self._compute_composite(token)

        # --- Exit logic ---
        if token in self._positions:
            if composite <= self.exit_threshold:
                entry_signals = self._entry_signals.pop(token, {})
                # Simple win/loss: check if current direction aligns
                entry_score = self._positions.pop(token, 0.0)
                trade_won = composite > entry_score * 0.5  # crude heuristic
                self._record_attribution(entry_signals, trade_won)
                logger.info(
                    "[%s] EXIT %s | composite=%.3f signals=%s",
                    self.name, token, composite,
                    {k: f"{v:.2f}" for k, v in signal_values.items()},
                )
                self.sell(token, amount=0.0)
            return

        # --- Entry logic ---
        if len(self._positions) >= self.max_positions:
            return

        if composite >= self.consensus_threshold:
            logger.info(
                "[%s] ENTER %s | composite=%.3f signals=%s",
                self.name, token, composite,
                {k: f"{v:.2f}" for k, v in signal_values.items()},
            )
            self.buy(token, amount_usd=self.position_usd)
            self._positions[token] = composite
            self._entry_signals[token] = signal_values

    @property
    def attribution_summary(self) -> Dict[str, Dict[str, float]]:
        """Per-signal performance attribution summary.

        Returns:
            Dict of signal name to attribution metrics.
        """
        return {
            name: {
                "accuracy": attr.accuracy,
                "total_contribution": attr.total_contribution,
                "total_calls": float(attr.total_calls),
                "avg_signal_at_entry": attr.avg_signal_at_entry,
            }
            for name, attr in self._attribution.items()
        }

    def on_stop(self) -> None:
        """Log attribution summary on shutdown."""
        logger.info(
            "[%s] stopped | positions=%d attribution=%s",
            self.name,
            len(self._positions),
            self.attribution_summary,
        )
        super().on_stop()
