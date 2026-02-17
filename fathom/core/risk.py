"""Risk management framework for the Fathom trading engine.

Provides position sizing algorithms, portfolio limits enforcement,
drawdown-based circuit breakers, and real-time exposure tracking.
These components are designed to be composed together and wired into
the engine's event bus so that every order submission passes through
risk checks before execution.

Typical usage::

    from fathom.core.risk import (
        PositionSizer, PortfolioLimits, DrawdownCircuitBreaker, ExposureTracker,
    )

    sizer = PositionSizer(method="kelly", max_position_usd=500.0)
    limits = PortfolioLimits(max_positions=10, max_exposure_pct=0.25)
    breaker = DrawdownCircuitBreaker(threshold=0.15, recovery=0.05)
    tracker = ExposureTracker(equity=10_000.0)
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Dict, List, Optional, Sequence

logger = logging.getLogger("fathom.risk")


# ---------------------------------------------------------------------------
# Position sizing
# ---------------------------------------------------------------------------

class SizingMethod(Enum):
    """Supported position-sizing algorithms."""
    FIXED = auto()
    PERCENT_EQUITY = auto()
    KELLY = auto()
    VOLATILITY_SCALED = auto()


@dataclass
class SizingResult:
    """Output of a position-sizing calculation.

    Attributes:
        amount_usd: Dollar amount to allocate to the trade.
        method: The algorithm that produced this result.
        raw_amount_usd: Amount before caps were applied.
        capped: Whether the result was clipped by ``max_position_usd``.
    """

    __slots__ = ("amount_usd", "method", "raw_amount_usd", "capped")
    amount_usd: float
    method: SizingMethod
    raw_amount_usd: float
    capped: bool


class PositionSizer:
    """Compute trade size using one of several algorithms.

    Args:
        method: Sizing algorithm to use.  Accepts enum value or lowercase
            string (``"fixed"``, ``"percent_equity"``, ``"kelly"``,
            ``"volatility_scaled"``).
        fixed_amount_usd: Dollar amount for ``FIXED`` sizing.
        equity_fraction: Fraction of equity for ``PERCENT_EQUITY`` sizing.
        kelly_win_rate: Historical win rate for Kelly criterion.
        kelly_avg_win: Average winning trade return (as a ratio, e.g. 0.5 = 50%).
        kelly_avg_loss: Average losing trade return (positive number).
        kelly_fraction: Fractional Kelly multiplier (0-1). Defaults to 0.5
            (half-Kelly) for safety.
        volatility_target: Target annualised volatility for vol-scaled sizing.
        max_position_usd: Hard cap on any single position.
        min_position_usd: Floor below which no trade is taken.
    """

    def __init__(
        self,
        method: SizingMethod | str = SizingMethod.FIXED,
        fixed_amount_usd: float = 100.0,
        equity_fraction: float = 0.02,
        kelly_win_rate: float = 0.55,
        kelly_avg_win: float = 0.40,
        kelly_avg_loss: float = 0.20,
        kelly_fraction: float = 0.50,
        volatility_target: float = 0.20,
        max_position_usd: float = 1_000.0,
        min_position_usd: float = 5.0,
    ) -> None:
        if isinstance(method, str):
            method = SizingMethod[method.upper()]
        self.method = method
        self.fixed_amount_usd = fixed_amount_usd
        self.equity_fraction = equity_fraction
        self.kelly_win_rate = kelly_win_rate
        self.kelly_avg_win = kelly_avg_win
        self.kelly_avg_loss = kelly_avg_loss
        self.kelly_fraction = kelly_fraction
        self.volatility_target = volatility_target
        self.max_position_usd = max_position_usd
        self.min_position_usd = min_position_usd

    # -- public API ----------------------------------------------------------

    def size(
        self,
        equity: float,
        recent_volatility: float = 0.0,
    ) -> SizingResult:
        """Compute the position size for a new trade.

        Args:
            equity: Current portfolio equity in USD.
            recent_volatility: Annualised volatility of the asset (required
                for ``VOLATILITY_SCALED``).

        Returns:
            A ``SizingResult`` with the recommended allocation.
        """
        if self.method == SizingMethod.FIXED:
            raw = self.fixed_amount_usd
        elif self.method == SizingMethod.PERCENT_EQUITY:
            raw = equity * self.equity_fraction
        elif self.method == SizingMethod.KELLY:
            raw = self._kelly(equity)
        elif self.method == SizingMethod.VOLATILITY_SCALED:
            raw = self._vol_scaled(equity, recent_volatility)
        else:
            raw = self.fixed_amount_usd

        capped = raw > self.max_position_usd
        amount = min(raw, self.max_position_usd)
        if amount < self.min_position_usd:
            amount = 0.0

        return SizingResult(
            amount_usd=round(amount, 4),
            method=self.method,
            raw_amount_usd=round(raw, 4),
            capped=capped,
        )

    # -- internals -----------------------------------------------------------

    def _kelly(self, equity: float) -> float:
        """Full Kelly criterion, scaled by ``kelly_fraction``.

        Kelly % = W - (1-W)/R  where W = win rate, R = win/loss ratio.
        """
        w = self.kelly_win_rate
        r = self.kelly_avg_win / self.kelly_avg_loss if self.kelly_avg_loss > 0 else 0.0
        kelly_pct = w - (1.0 - w) / r if r > 0 else 0.0
        kelly_pct = max(kelly_pct, 0.0) * self.kelly_fraction
        return equity * kelly_pct

    def _vol_scaled(self, equity: float, vol: float) -> float:
        """Scale position so that contribution to portfolio vol ≈ target."""
        if vol <= 0:
            return self.fixed_amount_usd
        scalar = self.volatility_target / vol
        return equity * min(scalar, 1.0) * self.equity_fraction


# ---------------------------------------------------------------------------
# Portfolio limits
# ---------------------------------------------------------------------------

@dataclass
class PortfolioLimits:
    """Enforces portfolio-level risk constraints.

    Attributes:
        max_positions: Maximum number of concurrent open positions.
        max_exposure_pct: Maximum fraction of equity any single token
            may represent (0-1).
        max_total_exposure_pct: Maximum fraction of equity deployed
            across all positions.
        max_correlated_positions: Maximum positions in the same
            sector/category (e.g. all memecoins).
        sector_limits: Per-sector position caps.
    """

    max_positions: int = 10
    max_exposure_pct: float = 0.25
    max_total_exposure_pct: float = 0.90
    max_correlated_positions: int = 5
    sector_limits: Dict[str, int] = field(default_factory=dict)

    def check(
        self,
        current_positions: int,
        token_exposure_pct: float,
        total_exposure_pct: float,
        sector: str = "",
        sector_count: int = 0,
    ) -> tuple[bool, str]:
        """Validate a proposed trade against portfolio limits.

        Args:
            current_positions: Number of open positions.
            token_exposure_pct: Fraction of equity this token would represent.
            total_exposure_pct: Total portfolio exposure as fraction of equity.
            sector: Optional sector tag for sector-limit checks.
            sector_count: Current number of positions in *sector*.

        Returns:
            Tuple of ``(allowed, reason)``.  ``reason`` is empty when allowed.
        """
        if current_positions >= self.max_positions:
            return False, f"max_positions ({self.max_positions}) reached"
        if token_exposure_pct > self.max_exposure_pct:
            return False, (
                f"token exposure {token_exposure_pct:.1%} exceeds "
                f"limit {self.max_exposure_pct:.1%}"
            )
        if total_exposure_pct > self.max_total_exposure_pct:
            return False, (
                f"total exposure {total_exposure_pct:.1%} exceeds "
                f"limit {self.max_total_exposure_pct:.1%}"
            )
        if sector and sector in self.sector_limits:
            if sector_count >= self.sector_limits[sector]:
                return False, f"sector '{sector}' limit ({self.sector_limits[sector]}) reached"
        if sector and sector_count >= self.max_correlated_positions:
            return False, (
                f"correlated position limit ({self.max_correlated_positions}) "
                f"reached for sector '{sector}'"
            )
        return True, ""


# ---------------------------------------------------------------------------
# Drawdown circuit breaker
# ---------------------------------------------------------------------------

class BreakerState(Enum):
    """Current state of the drawdown circuit breaker."""
    ACTIVE = auto()
    TRIPPED = auto()


class DrawdownCircuitBreaker:
    """Halts trading when portfolio drawdown exceeds a threshold.

    The breaker *trips* when drawdown from peak equity exceeds
    ``threshold``.  It *resets* when equity recovers to within
    ``recovery`` of the peak (measured as drawdown fraction falling
    below ``recovery``).

    Args:
        threshold: Drawdown fraction at which to trip (e.g. 0.15 = 15%).
        recovery: Drawdown fraction at which to reset (e.g. 0.05 = 5%).
        cooldown_seconds: Minimum seconds to stay tripped regardless of
            recovery.
    """

    def __init__(
        self,
        threshold: float = 0.15,
        recovery: float = 0.05,
        cooldown_seconds: float = 300.0,
    ) -> None:
        if recovery >= threshold:
            raise ValueError("recovery must be < threshold")
        self.threshold = threshold
        self.recovery = recovery
        self.cooldown_seconds = cooldown_seconds

        self._state = BreakerState.ACTIVE
        self._peak_equity: float = 0.0
        self._tripped_at: float = 0.0
        self._trip_count: int = 0

    @property
    def state(self) -> BreakerState:
        """Current breaker state."""
        return self._state

    @property
    def is_tripped(self) -> bool:
        """``True`` when trading is halted."""
        return self._state == BreakerState.TRIPPED

    @property
    def trip_count(self) -> int:
        """Number of times the breaker has tripped in this session."""
        return self._trip_count

    def update(self, equity: float) -> BreakerState:
        """Feed a new equity observation and return the updated state.

        Args:
            equity: Current portfolio equity in USD.

        Returns:
            The breaker state after processing the observation.
        """
        if equity > self._peak_equity:
            self._peak_equity = equity

        if self._peak_equity <= 0:
            return self._state

        drawdown = (self._peak_equity - equity) / self._peak_equity

        if self._state == BreakerState.ACTIVE:
            if drawdown >= self.threshold:
                self._state = BreakerState.TRIPPED
                self._tripped_at = time.monotonic()
                self._trip_count += 1
                logger.warning(
                    "Circuit breaker TRIPPED | dd=%.2f%% threshold=%.2f%%",
                    drawdown * 100,
                    self.threshold * 100,
                )
        elif self._state == BreakerState.TRIPPED:
            elapsed = time.monotonic() - self._tripped_at
            if drawdown <= self.recovery and elapsed >= self.cooldown_seconds:
                self._state = BreakerState.ACTIVE
                logger.info(
                    "Circuit breaker RESET | dd=%.2f%% recovery=%.2f%%",
                    drawdown * 100,
                    self.recovery * 100,
                )

        return self._state

    def reset(self) -> None:
        """Manually reset the breaker."""
        self._state = BreakerState.ACTIVE
        self._peak_equity = 0.0
        logger.info("Circuit breaker manually reset")


# ---------------------------------------------------------------------------
# Exposure tracker
# ---------------------------------------------------------------------------

@dataclass
class PositionRecord:
    """Snapshot of a single open position.

    Attributes:
        token: Token mint address or symbol.
        quantity: Number of tokens held.
        entry_price_usd: Average entry price.
        current_price_usd: Most recent price.
        sector: Optional sector tag.
    """

    __slots__ = ("token", "quantity", "entry_price_usd", "current_price_usd", "sector")
    token: str
    quantity: float
    entry_price_usd: float
    current_price_usd: float
    sector: str


class ExposureTracker:
    """Real-time portfolio exposure calculation.

    Tracks open positions and computes aggregate exposure metrics
    used by ``PortfolioLimits`` and the circuit breaker.

    Args:
        equity: Starting equity in USD.
    """

    def __init__(self, equity: float = 10_000.0) -> None:
        self._equity = equity
        self._positions: Dict[str, PositionRecord] = {}

    @property
    def equity(self) -> float:
        """Current equity (cash + mark-to-market positions)."""
        return self._equity + self.total_unrealised_pnl

    @property
    def position_count(self) -> int:
        """Number of open positions."""
        return len(self._positions)

    @property
    def positions(self) -> Dict[str, PositionRecord]:
        """Shallow copy of open positions keyed by token."""
        return dict(self._positions)

    # -- position lifecycle --------------------------------------------------

    def open_position(
        self,
        token: str,
        quantity: float,
        price_usd: float,
        sector: str = "memecoin",
    ) -> None:
        """Record a new position or add to an existing one.

        Args:
            token: Token mint / symbol.
            quantity: Tokens acquired.
            price_usd: Execution price per token.
            sector: Sector label for correlation limits.
        """
        if token in self._positions:
            pos = self._positions[token]
            total_qty = pos.quantity + quantity
            if total_qty > 0:
                avg_price = (
                    (pos.entry_price_usd * pos.quantity + price_usd * quantity)
                    / total_qty
                )
            else:
                avg_price = price_usd
            self._positions[token] = PositionRecord(
                token=token,
                quantity=total_qty,
                entry_price_usd=avg_price,
                current_price_usd=price_usd,
                sector=sector,
            )
        else:
            self._positions[token] = PositionRecord(
                token=token,
                quantity=quantity,
                entry_price_usd=price_usd,
                current_price_usd=price_usd,
                sector=sector,
            )
        self._equity -= quantity * price_usd

    def close_position(self, token: str, price_usd: float) -> float:
        """Close a position entirely and return realised PnL.

        Args:
            token: Token to close.
            price_usd: Execution price.

        Returns:
            Realised PnL in USD.
        """
        pos = self._positions.pop(token, None)
        if pos is None:
            return 0.0
        proceeds = pos.quantity * price_usd
        cost = pos.quantity * pos.entry_price_usd
        self._equity += proceeds
        return proceeds - cost

    def update_price(self, token: str, price_usd: float) -> None:
        """Mark a position to market.

        Args:
            token: Token to update.
            price_usd: Latest price.
        """
        if token in self._positions:
            pos = self._positions[token]
            self._positions[token] = PositionRecord(
                token=pos.token,
                quantity=pos.quantity,
                entry_price_usd=pos.entry_price_usd,
                current_price_usd=price_usd,
                sector=pos.sector,
            )

    # -- exposure calculations -----------------------------------------------

    @property
    def total_unrealised_pnl(self) -> float:
        """Aggregate unrealised PnL across all positions."""
        return sum(
            p.quantity * (p.current_price_usd - p.entry_price_usd)
            for p in self._positions.values()
        )

    def token_exposure_pct(self, token: str) -> float:
        """Fraction of equity a single token represents."""
        pos = self._positions.get(token)
        if pos is None or self.equity <= 0:
            return 0.0
        return (pos.quantity * pos.current_price_usd) / self.equity

    @property
    def total_exposure_pct(self) -> float:
        """Fraction of equity deployed across all positions."""
        if self.equity <= 0:
            return 0.0
        total = sum(
            p.quantity * p.current_price_usd for p in self._positions.values()
        )
        return total / self.equity

    def sector_count(self, sector: str) -> int:
        """Number of positions in a given sector."""
        return sum(1 for p in self._positions.values() if p.sector == sector)

    @property
    def exposure_summary(self) -> Dict[str, float]:
        """Per-token exposure breakdown as fraction of equity."""
        eq = self.equity
        if eq <= 0:
            return {}
        return {
            token: (p.quantity * p.current_price_usd) / eq
            for token, p in self._positions.items()
        }
