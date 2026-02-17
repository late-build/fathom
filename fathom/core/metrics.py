"""Performance analytics and trade journaling for the Fathom engine.

Provides portfolio-level performance ratios (Sharpe, Sortino, Calmar),
drawdown analysis, win/loss streaks, and a ``TradeJournal`` that records
every trade for post-hoc analysis.

All calculations use simple Python arithmetic (no NumPy dependency) so the
module stays zero-dependency beyond the standard library.

Example::

    from fathom.core.metrics import TradeJournal, compute_sharpe

    journal = TradeJournal()
    journal.record(token="ABC", side="buy", price=0.001, quantity=50_000,
                   timestamp_s=1700000000)
    journal.record(token="ABC", side="sell", price=0.0015, quantity=50_000,
                   timestamp_s=1700003600)
    print(journal.summary())
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple


# ---------------------------------------------------------------------------
# Trade record
# ---------------------------------------------------------------------------

@dataclass
class TradeRecord:
    """Immutable record of a single fill.

    Attributes:
        token: Token mint or symbol.
        side: ``"buy"`` or ``"sell"``.
        price: Execution price in USD.
        quantity: Token quantity.
        notional_usd: Dollar value of the fill.
        timestamp_s: Unix epoch seconds.
        strategy: Strategy that generated the trade.
        fees_usd: Estimated transaction fees.
        tx_signature: On-chain transaction hash (if available).
    """

    __slots__ = (
        "token", "side", "price", "quantity", "notional_usd",
        "timestamp_s", "strategy", "fees_usd", "tx_signature",
    )
    token: str
    side: str
    price: float
    quantity: float
    notional_usd: float
    timestamp_s: float
    strategy: str
    fees_usd: float
    tx_signature: str


# ---------------------------------------------------------------------------
# Round-trip (paired entry + exit)
# ---------------------------------------------------------------------------

@dataclass
class RoundTrip:
    """A matched entry/exit pair representing one complete trade.

    Attributes:
        token: Token traded.
        entry_price: Average entry price.
        exit_price: Average exit price.
        quantity: Position size.
        pnl_usd: Realised PnL after fees.
        pnl_pct: Return as a fraction (e.g. 0.5 = +50%).
        hold_seconds: Duration from entry to exit.
        entry_time: Entry timestamp (epoch seconds).
        exit_time: Exit timestamp (epoch seconds).
    """

    __slots__ = (
        "token", "entry_price", "exit_price", "quantity",
        "pnl_usd", "pnl_pct", "hold_seconds", "entry_time", "exit_time",
    )
    token: str
    entry_price: float
    exit_price: float
    quantity: float
    pnl_usd: float
    pnl_pct: float
    hold_seconds: float
    entry_time: float
    exit_time: float


# ---------------------------------------------------------------------------
# Pure-function metrics
# ---------------------------------------------------------------------------

def compute_sharpe(
    returns: Sequence[float],
    risk_free_rate: float = 0.0,
    periods_per_year: float = 365.0,
) -> float:
    """Annualised Sharpe ratio.

    Args:
        returns: Sequence of periodic returns (e.g. daily).
        risk_free_rate: Per-period risk-free rate.
        periods_per_year: Annualisation factor.

    Returns:
        Annualised Sharpe ratio, or 0.0 if insufficient data.
    """
    if len(returns) < 2:
        return 0.0
    excess = [r - risk_free_rate for r in returns]
    mean = sum(excess) / len(excess)
    var = sum((r - mean) ** 2 for r in excess) / (len(excess) - 1)
    std = math.sqrt(var) if var > 0 else 0.0
    if std == 0:
        return 0.0
    return (mean / std) * math.sqrt(periods_per_year)


def compute_sortino(
    returns: Sequence[float],
    risk_free_rate: float = 0.0,
    periods_per_year: float = 365.0,
) -> float:
    """Annualised Sortino ratio (downside deviation only).

    Args:
        returns: Sequence of periodic returns.
        risk_free_rate: Per-period risk-free rate.
        periods_per_year: Annualisation factor.

    Returns:
        Annualised Sortino ratio, or 0.0 if insufficient data.
    """
    if len(returns) < 2:
        return 0.0
    excess = [r - risk_free_rate for r in returns]
    mean = sum(excess) / len(excess)
    downside = [min(r, 0.0) ** 2 for r in excess]
    dd_var = sum(downside) / (len(downside) - 1)
    dd_std = math.sqrt(dd_var) if dd_var > 0 else 0.0
    if dd_std == 0:
        return 0.0
    return (mean / dd_std) * math.sqrt(periods_per_year)


def compute_calmar(
    total_return: float,
    max_drawdown: float,
    period_years: float = 1.0,
) -> float:
    """Calmar ratio (annualised return / max drawdown).

    Args:
        total_return: Cumulative return as a fraction.
        max_drawdown: Maximum drawdown as a positive fraction.
        period_years: Duration of the observation period in years.

    Returns:
        Calmar ratio, or 0.0 if drawdown is zero.
    """
    if max_drawdown <= 0 or period_years <= 0:
        return 0.0
    annualised = total_return / period_years
    return annualised / max_drawdown


def compute_max_drawdown(
    equity_curve: Sequence[float],
) -> Tuple[float, int, int]:
    """Maximum drawdown with start/end indices.

    Args:
        equity_curve: Sequence of equity values over time.

    Returns:
        Tuple of ``(max_dd_fraction, peak_index, trough_index)``.
    """
    if len(equity_curve) < 2:
        return 0.0, 0, 0

    peak = equity_curve[0]
    peak_idx = 0
    max_dd = 0.0
    max_dd_peak_idx = 0
    max_dd_trough_idx = 0

    for i, val in enumerate(equity_curve):
        if val > peak:
            peak = val
            peak_idx = i
        dd = (peak - val) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd
            max_dd_peak_idx = peak_idx
            max_dd_trough_idx = i

    return max_dd, max_dd_peak_idx, max_dd_trough_idx


def compute_drawdown_duration(
    equity_curve: Sequence[float],
) -> int:
    """Longest drawdown duration in number of periods.

    Args:
        equity_curve: Sequence of equity values.

    Returns:
        Length of the longest contiguous drawdown (periods below prior peak).
    """
    if len(equity_curve) < 2:
        return 0
    peak = equity_curve[0]
    current_duration = 0
    max_duration = 0
    for val in equity_curve:
        if val >= peak:
            peak = val
            current_duration = 0
        else:
            current_duration += 1
            max_duration = max(max_duration, current_duration)
    return max_duration


def compute_recovery_factor(total_return: float, max_drawdown: float) -> float:
    """Recovery factor = total return / max drawdown.

    Args:
        total_return: Absolute PnL in USD.
        max_drawdown: Maximum drawdown in USD (positive).

    Returns:
        Recovery factor, or 0.0 if drawdown is zero.
    """
    return total_return / max_drawdown if max_drawdown > 0 else 0.0


def compute_profit_factor(gross_profit: float, gross_loss: float) -> float:
    """Profit factor = gross profit / gross loss.

    Args:
        gross_profit: Sum of all winning trades (positive).
        gross_loss: Sum of all losing trades (positive).

    Returns:
        Profit factor, or ``float('inf')`` if no losses.
    """
    if gross_loss <= 0:
        return float("inf") if gross_profit > 0 else 0.0
    return gross_profit / gross_loss


def compute_expectancy(
    win_rate: float,
    avg_win: float,
    avg_loss: float,
) -> float:
    """Expected value per trade.

    Args:
        win_rate: Fraction of trades that are winners (0-1).
        avg_win: Average winning trade PnL (positive).
        avg_loss: Average losing trade PnL (positive).

    Returns:
        Expectancy in USD per trade.
    """
    return win_rate * avg_win - (1.0 - win_rate) * avg_loss


def compute_streaks(outcomes: Sequence[bool]) -> Dict[str, int]:
    """Compute win/loss streak statistics.

    Args:
        outcomes: Sequence of booleans (``True`` = win, ``False`` = loss).

    Returns:
        Dict with keys ``max_win_streak``, ``max_loss_streak``,
        ``current_streak``, ``current_is_win``.
    """
    if not outcomes:
        return {
            "max_win_streak": 0,
            "max_loss_streak": 0,
            "current_streak": 0,
            "current_is_win": False,
        }

    max_win = max_loss = 0
    cur = 1
    for i in range(1, len(outcomes)):
        if outcomes[i] == outcomes[i - 1]:
            cur += 1
        else:
            if outcomes[i - 1]:
                max_win = max(max_win, cur)
            else:
                max_loss = max(max_loss, cur)
            cur = 1

    # Final streak
    if outcomes[-1]:
        max_win = max(max_win, cur)
    else:
        max_loss = max(max_loss, cur)

    return {
        "max_win_streak": max_win,
        "max_loss_streak": max_loss,
        "current_streak": cur,
        "current_is_win": bool(outcomes[-1]),
    }


# ---------------------------------------------------------------------------
# Rolling statistics
# ---------------------------------------------------------------------------

class RollingStats:
    """Fixed-window rolling statistics using Welford's algorithm.

    Maintains a ring buffer of the last ``window`` observations and
    computes mean, variance, and standard deviation incrementally.

    Args:
        window: Number of observations in the rolling window.
    """

    def __init__(self, window: int = 30) -> None:
        if window < 2:
            raise ValueError("window must be >= 2")
        self._window = window
        self._buffer: List[float] = []
        self._sum: float = 0.0
        self._sum_sq: float = 0.0

    def push(self, value: float) -> None:
        """Add an observation."""
        if len(self._buffer) >= self._window:
            old = self._buffer.pop(0)
            self._sum -= old
            self._sum_sq -= old * old
        self._buffer.append(value)
        self._sum += value
        self._sum_sq += value * value

    @property
    def mean(self) -> float:
        """Rolling mean."""
        n = len(self._buffer)
        return self._sum / n if n > 0 else 0.0

    @property
    def variance(self) -> float:
        """Rolling sample variance."""
        n = len(self._buffer)
        if n < 2:
            return 0.0
        mean = self._sum / n
        return (self._sum_sq / n - mean * mean) * n / (n - 1)

    @property
    def std(self) -> float:
        """Rolling sample standard deviation."""
        return math.sqrt(max(self.variance, 0.0))

    @property
    def count(self) -> int:
        """Number of observations currently in the buffer."""
        return len(self._buffer)

    @property
    def full(self) -> bool:
        """Whether the buffer has reached window size."""
        return len(self._buffer) >= self._window


# ---------------------------------------------------------------------------
# Trade journal
# ---------------------------------------------------------------------------

class TradeJournal:
    """Records trades and computes performance analytics.

    Keeps a full audit trail of every fill and matches buy/sell pairs
    into ``RoundTrip`` objects for PnL analysis.

    Args:
        initial_equity: Starting portfolio equity for return calculations.
    """

    def __init__(self, initial_equity: float = 10_000.0) -> None:
        self.initial_equity = initial_equity
        self._trades: List[TradeRecord] = []
        self._round_trips: List[RoundTrip] = []
        self._open_buys: Dict[str, List[TradeRecord]] = {}
        self._equity_curve: List[float] = [initial_equity]
        self._current_equity = initial_equity

    def record(
        self,
        token: str,
        side: str,
        price: float,
        quantity: float,
        timestamp_s: float = 0.0,
        strategy: str = "",
        fees_usd: float = 0.0,
        tx_signature: str = "",
    ) -> None:
        """Record a fill and attempt to match round trips.

        Args:
            token: Token identifier.
            side: ``"buy"`` or ``"sell"``.
            price: Execution price.
            quantity: Token quantity.
            timestamp_s: Epoch seconds (defaults to now).
            strategy: Strategy name.
            fees_usd: Transaction fees.
            tx_signature: On-chain tx hash.
        """
        if timestamp_s <= 0:
            timestamp_s = time.time()

        rec = TradeRecord(
            token=token,
            side=side,
            price=price,
            quantity=quantity,
            notional_usd=price * quantity,
            timestamp_s=timestamp_s,
            strategy=strategy,
            fees_usd=fees_usd,
            tx_signature=tx_signature,
        )
        self._trades.append(rec)

        if side == "buy":
            self._open_buys.setdefault(token, []).append(rec)
        elif side == "sell":
            self._match_round_trip(rec)

    def _match_round_trip(self, sell: TradeRecord) -> None:
        """FIFO matching of a sell against open buys."""
        buys = self._open_buys.get(sell.token, [])
        if not buys:
            return
        buy = buys.pop(0)
        qty = min(buy.quantity, sell.quantity)
        pnl = qty * (sell.price - buy.price) - buy.fees_usd - sell.fees_usd
        pnl_pct = (sell.price - buy.price) / buy.price if buy.price > 0 else 0.0
        rt = RoundTrip(
            token=sell.token,
            entry_price=buy.price,
            exit_price=sell.price,
            quantity=qty,
            pnl_usd=pnl,
            pnl_pct=pnl_pct,
            hold_seconds=sell.timestamp_s - buy.timestamp_s,
            entry_time=buy.timestamp_s,
            exit_time=sell.timestamp_s,
        )
        self._round_trips.append(rt)
        self._current_equity += pnl
        self._equity_curve.append(self._current_equity)

    @property
    def trades(self) -> List[TradeRecord]:
        """All recorded fills."""
        return list(self._trades)

    @property
    def round_trips(self) -> List[RoundTrip]:
        """All matched round trips."""
        return list(self._round_trips)

    @property
    def equity_curve(self) -> List[float]:
        """Equity curve (one point per closed round trip)."""
        return list(self._equity_curve)

    def summary(self) -> Dict[str, float]:
        """Compute a comprehensive performance summary.

        Returns:
            Dictionary with win_rate, profit_factor, expectancy,
            sharpe, sortino, max_drawdown, total_pnl, trade_count, etc.
        """
        rts = self._round_trips
        if not rts:
            return {"trade_count": 0, "total_pnl": 0.0}

        wins = [rt for rt in rts if rt.pnl_usd > 0]
        losses = [rt for rt in rts if rt.pnl_usd <= 0]
        total_pnl = sum(rt.pnl_usd for rt in rts)
        gross_profit = sum(rt.pnl_usd for rt in wins)
        gross_loss = sum(abs(rt.pnl_usd) for rt in losses)
        win_rate = len(wins) / len(rts) if rts else 0.0
        avg_win = gross_profit / len(wins) if wins else 0.0
        avg_loss = gross_loss / len(losses) if losses else 0.0

        returns = [rt.pnl_pct for rt in rts]
        outcomes = [rt.pnl_usd > 0 for rt in rts]
        max_dd, _, _ = compute_max_drawdown(self._equity_curve)
        streaks = compute_streaks(outcomes)

        return {
            "trade_count": len(rts),
            "win_count": len(wins),
            "loss_count": len(losses),
            "win_rate": round(win_rate, 4),
            "total_pnl": round(total_pnl, 4),
            "gross_profit": round(gross_profit, 4),
            "gross_loss": round(gross_loss, 4),
            "profit_factor": round(compute_profit_factor(gross_profit, gross_loss), 4),
            "expectancy": round(compute_expectancy(win_rate, avg_win, avg_loss), 4),
            "avg_win": round(avg_win, 4),
            "avg_loss": round(avg_loss, 4),
            "sharpe": round(compute_sharpe(returns), 4),
            "sortino": round(compute_sortino(returns), 4),
            "max_drawdown": round(max_dd, 4),
            "max_drawdown_duration": compute_drawdown_duration(self._equity_curve),
            "recovery_factor": round(
                compute_recovery_factor(total_pnl, max_dd * self.initial_equity), 4
            ),
            "max_win_streak": streaks["max_win_streak"],
            "max_loss_streak": streaks["max_loss_streak"],
            "current_equity": round(self._current_equity, 4),
            "total_return_pct": round(
                (self._current_equity - self.initial_equity)
                / self.initial_equity
                * 100,
                2,
            ),
        }
