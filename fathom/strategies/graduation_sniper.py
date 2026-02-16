"""
Graduation Sniper Strategy — Multi-Factor Scoring Model.

Scores each graduation (0-100) across multiple dimensions:
- Momentum: buy/sell ratio, 5m/1h price change
- On-chain quality: holder concentration, dev holdings, sniper count
- Liquidity health: mcap/liq ratio (not absolute mcap)
- Activity: transaction count, volume
- Freshness: time since graduation

Position sizing is dynamic based on conviction score:
- Score 80+: full position
- Score 60-79: half position
- Score <60: skip

Same code runs in backtest, paper, and live.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from fathom.core.events import Event, EventType, PriceUpdate
from fathom.core.strategy import Strategy
from fathom.adapters.pumpfun.graduation import (
    GraduationEvent,
    DevActivityEvent,
)

logger = logging.getLogger("fathom.strategy.graduation_sniper")


@dataclass
class Position:
    """Active position tracking."""
    mint: str
    symbol: str
    entry_price: float
    amount_usd: float
    amount_tokens: float
    entered_at_ns: int
    score: int = 0
    highest_price: float = 0.0

    @property
    def age_seconds(self) -> float:
        return (time.time_ns() - self.entered_at_ns) / 1e9


@dataclass
class ScoreBreakdown:
    """Detailed scoring for logging/analysis."""
    momentum: int = 0
    quality: int = 0
    liquidity: int = 0
    activity: int = 0
    freshness: int = 0
    total: int = 0
    reasons: list[str] = field(default_factory=list)


def score_graduation(event: GraduationEvent) -> ScoreBreakdown:
    """
    Multi-factor scoring model for graduation events.

    Returns a ScoreBreakdown with total score 0-100.
    Higher = stronger trade candidate.
    """
    s = ScoreBreakdown()
    reasons = s.reasons

    # ── Momentum (max ±30) ──
    buys = event.buys_1h
    sells = event.sells_1h
    total_1h = buys + sells
    if total_1h > 0:
        buy_ratio = buys / total_1h
        if buy_ratio > 0.65:
            s.momentum += 15
            reasons.append(f"strong buying {buy_ratio:.0%}")
        elif buy_ratio > 0.55:
            s.momentum += 8
        elif buy_ratio < 0.35:
            s.momentum -= 15
            reasons.append(f"heavy selling {buy_ratio:.0%}")
        elif buy_ratio < 0.45:
            s.momentum -= 5

    if event.price_change_5m > 15:
        s.momentum += 10
        reasons.append(f"5m pump +{event.price_change_5m:.0f}%")
    elif event.price_change_5m > 0:
        s.momentum += 3
    elif event.price_change_5m < -15:
        s.momentum -= 10
        reasons.append(f"5m dump {event.price_change_5m:.0f}%")
    elif event.price_change_5m < 0:
        s.momentum -= 3

    if event.price_change_1h > 50:
        s.momentum += 5
    elif event.price_change_1h < -30:
        s.momentum -= 10
        reasons.append(f"1h down {event.price_change_1h:.0f}%")

    # ── On-chain quality (max ±30) ──
    if event.top10_concentration > 80:
        s.quality -= 25
        reasons.append(f"top10 hold {event.top10_concentration:.0f}%")
    elif event.top10_concentration > 50:
        s.quality -= 10
    elif event.top10_concentration > 0 and event.top10_concentration < 30:
        s.quality += 5

    if event.dev_holdings_pct > 10:
        s.quality -= 15
        reasons.append(f"dev holds {event.dev_holdings_pct:.1f}%")
    elif event.dev_holdings_pct > 5:
        s.quality -= 5
    elif event.dev_holdings_pct == 0:
        s.quality += 5

    if event.sniper_count > 50:
        s.quality -= 10
        reasons.append(f"{event.sniper_count} snipers")
    elif event.sniper_count > 20:
        s.quality -= 5
    elif event.sniper_count < 5:
        s.quality += 3

    if event.holder_count > 500:
        s.quality += 5
    elif event.holder_count < 50 and event.holder_count > 0:
        s.quality -= 5

    # ── Liquidity health (max ±25) ──
    mcap = event.market_cap_usd or (event.initial_price_usd * 1e9)
    liq = event.liquidity_usd
    if liq > 0:
        ratio = mcap / liq
        if ratio > 200:
            s.liquidity -= 25
            reasons.append(f"mcap/liq {ratio:.0f}:1 (rug risk)")
        elif ratio > 100:
            s.liquidity -= 15
        elif ratio > 50:
            s.liquidity -= 5
        elif ratio < 10:
            s.liquidity += 5
    else:
        s.liquidity -= 15

    if liq < 3000 and liq > 0:
        s.liquidity -= 10
        reasons.append(f"liq ${liq:,.0f} thin")
    elif liq > 50000:
        s.liquidity += 5

    # ── Activity (max ±15) ──
    if event.txns_24h > 10000:
        s.activity += 10
    elif event.txns_24h > 5000:
        s.activity += 5
    elif event.txns_24h > 1000:
        s.activity += 2
    elif event.txns_24h < 200 and event.txns_24h > 0:
        s.activity -= 10
        reasons.append(f"low txns ({event.txns_24h})")

    # ── Freshness (max ±10) — backtests won't have this ──
    # (computed externally if available)

    # ── Total ──
    raw = 50 + s.momentum + s.quality + s.liquidity + s.activity + s.freshness
    s.total = max(0, min(100, raw))
    return s


class GraduationSniper(Strategy):
    """
    Multi-factor graduation sniper.

    Scores each graduation on a 0-100 scale across momentum, holder quality,
    liquidity depth, activity, and freshness. Position sizing scales with
    conviction score.

    Args:
        base_position_usd: Full position size (score 80+)
        max_positions: Maximum concurrent positions
        min_score: Minimum score to trade (default 60)
        take_profit_pct: Exit at this % gain
        stop_loss_pct: Exit at this % loss
        trailing_stop_pct: Trailing stop distance
        trailing_activate_pct: Activate trailing after this gain
        max_hold_seconds: Force exit timeout
        exit_on_dev_sell: Auto-exit on dev dump
        min_liquidity: Hard floor on liquidity USD
        max_mcap_liq_ratio: Hard ceiling on mcap/liq ratio
        max_top10_concentration: Hard ceiling on top 10 holder %
    """

    name = "graduation_sniper"

    def __init__(
        self,
        base_position_usd: float = 50.0,
        max_positions: int = 5,
        min_score: int = 60,
        take_profit_pct: float = 0.50,
        stop_loss_pct: float = 0.20,
        trailing_stop_pct: float = 0.15,
        trailing_activate_pct: float = 0.30,
        max_hold_seconds: float = 600.0,
        exit_on_dev_sell: bool = True,
        # Hard filters (override score)
        min_liquidity: float = 3_000.0,
        max_mcap_liq_ratio: float = 200.0,
        max_top10_concentration: float = 90.0,
        # Legacy compat
        position_size_usd: float | None = None,
        min_holders: int = 0,
        min_sol_raised: float = 0.0,
        max_initial_mcap: float = 0.0,
        max_top10_concentration_legacy: float = 0.0,  # ignored, use max_top10_concentration
        **kwargs,  # absorb any extra legacy params
    ) -> None:
        super().__init__()
        self.base_position_usd = position_size_usd or base_position_usd
        self.max_positions = max_positions
        self.min_score = min_score
        self.take_profit_pct = take_profit_pct
        self.stop_loss_pct = stop_loss_pct
        self.trailing_stop_pct = trailing_stop_pct
        self.trailing_activate_pct = trailing_activate_pct
        self.max_hold_seconds = max_hold_seconds
        self.exit_on_dev_sell = exit_on_dev_sell
        self.min_liquidity = min_liquidity
        self.max_mcap_liq_ratio = max_mcap_liq_ratio
        self.max_top10_concentration = max_top10_concentration

        self._positions: dict[str, Position] = {}
        self._passed: int = 0
        self._filtered: int = 0
        self._scores: list[int] = []  # all scores for analysis
        self._exits: dict[str, int] = {
            "take_profit": 0,
            "stop_loss": 0,
            "trailing_stop": 0,
            "timeout": 0,
            "dev_sell": 0,
        }

    def bind(self, event_bus) -> None:
        super().bind(event_bus)
        event_bus.subscribe(EventType.SIGNAL, self._handle_signal)

    def _handle_signal(self, event: Event) -> None:
        if isinstance(event, GraduationEvent):
            self._on_graduation(event)
        elif isinstance(event, DevActivityEvent):
            self._on_dev_activity(event)

    def _on_graduation(self, event: GraduationEvent) -> None:
        mint = event.mint
        symbol = event.symbol or mint[:8]

        if mint in self._positions:
            return
        if len(self._positions) >= self.max_positions:
            self._filtered += 1
            return
        if event.initial_price_usd <= 0:
            self._filtered += 1
            return

        # ── Score the graduation ──
        breakdown = score_graduation(event)
        score = breakdown.total
        self._scores.append(score)

        # ── Hard filters (override score) ──
        mcap = event.market_cap_usd or (event.initial_price_usd * 1e9)
        liq = event.liquidity_usd

        if liq > 0 and liq < self.min_liquidity:
            self._filtered += 1
            logger.debug(f"SKIP {symbol}: liq ${liq:,.0f} < ${self.min_liquidity:,.0f}")
            return

        if liq > 0:
            ratio = mcap / liq
            if ratio > self.max_mcap_liq_ratio:
                self._filtered += 1
                logger.debug(f"SKIP {symbol}: mcap/liq {ratio:.0f}:1 > {self.max_mcap_liq_ratio:.0f}")
                return

        if event.top10_concentration > 0 and event.top10_concentration > self.max_top10_concentration:
            self._filtered += 1
            logger.debug(f"SKIP {symbol}: top10 {event.top10_concentration:.0f}% > {self.max_top10_concentration:.0f}%")
            return

        # ── Score threshold ──
        if score < self.min_score:
            self._filtered += 1
            logger.debug(
                f"SKIP {symbol}: score {score}/100 < {self.min_score} "
                f"[{', '.join(breakdown.reasons[:3])}]"
            )
            return

        # ── Dynamic position sizing ──
        if score >= 80:
            position_usd = self.base_position_usd
        elif score >= 70:
            position_usd = self.base_position_usd * 0.75
        else:
            position_usd = self.base_position_usd * 0.5

        # ── Entry ──
        self._passed += 1
        entry_price = event.initial_price_usd
        amount_tokens = position_usd / entry_price

        self._positions[mint] = Position(
            mint=mint,
            symbol=symbol,
            entry_price=entry_price,
            amount_usd=position_usd,
            amount_tokens=amount_tokens,
            entered_at_ns=time.time_ns(),
            score=score,
            highest_price=entry_price,
        )

        self.buy(mint, amount_usd=position_usd, slippage_bps=300)

        reasons_str = ", ".join(breakdown.reasons[:3]) if breakdown.reasons else "clean"
        logger.info(
            f"🎯 ENTRY | {symbol} | score={score} | ${entry_price:.8f} | "
            f"${position_usd:.0f} | [{reasons_str}]"
        )

    def on_price_update(self, event: PriceUpdate) -> None:
        mint = event.token
        if mint not in self._positions:
            return

        pos = self._positions[mint]
        price = event.price_usd
        if price <= 0:
            return

        if price > pos.highest_price:
            pos.highest_price = price

        pnl_pct = (price - pos.entry_price) / pos.entry_price
        drawdown_from_high = (pos.highest_price - price) / pos.highest_price if pos.highest_price > 0 else 0

        if pnl_pct >= self.take_profit_pct:
            self._exit(pos, price, "take_profit", pnl_pct)
            return
        if pnl_pct <= -self.stop_loss_pct:
            self._exit(pos, price, "stop_loss", pnl_pct)
            return

        peak_pnl = (pos.highest_price - pos.entry_price) / pos.entry_price
        if peak_pnl >= self.trailing_activate_pct:
            if drawdown_from_high >= self.trailing_stop_pct:
                self._exit(pos, price, "trailing_stop", pnl_pct)
                return

        if pos.age_seconds >= self.max_hold_seconds:
            self._exit(pos, price, "timeout", pnl_pct)
            return

    def _on_dev_activity(self, event: DevActivityEvent) -> None:
        if not self.exit_on_dev_sell:
            return
        mint = event.mint
        if mint not in self._positions:
            return
        if event.action == "sell":
            pos = self._positions[mint]
            logger.warning(f"⚠️ DEV SOLD {event.amount_pct:.1f}% of {event.symbol}")
            self._exit(pos, pos.entry_price, "dev_sell", 0)

    def _exit(self, pos: Position, price: float, reason: str, pnl_pct: float) -> None:
        realized_pnl = pos.amount_tokens * (price - pos.entry_price)
        self._pnl += realized_pnl
        self._exits[reason] = self._exits.get(reason, 0) + 1

        self.sell(pos.mint, pos.amount_tokens, slippage_bps=500)

        emoji = "✅" if realized_pnl > 0 else "❌"
        logger.info(
            f"{emoji} EXIT | {pos.symbol} | {reason} | score={pos.score} | "
            f"pnl={pnl_pct:+.1%} (${realized_pnl:+.2f}) | held={pos.age_seconds:.0f}s"
        )
        self._positions.pop(pos.mint, None)

    def on_stop(self) -> None:
        avg_score = sum(self._scores) / len(self._scores) if self._scores else 0
        logger.info(
            f"[{self.name}] FINAL | "
            f"entries={self._passed} filtered={self._filtered} | "
            f"avg_score={avg_score:.0f} | "
            f"pnl=${self._pnl:+.2f} | exits={dict(self._exits)}"
        )

    @property
    def stats(self) -> dict:
        return {
            **super().stats,
            "open_positions": len(self._positions),
            "passed_filter": self._passed,
            "filtered_out": self._filtered,
            "avg_score": sum(self._scores) / len(self._scores) if self._scores else 0,
            "exits_by_reason": dict(self._exits),
        }
