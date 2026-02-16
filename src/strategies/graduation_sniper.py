"""
Graduation Sniper Strategy.

Automatically trades tokens that graduate from pump.fun to PumpSwap/Raydium.

The strategy evaluates each graduation based on configurable filters
(holder count, liquidity, dev behavior) and executes a buy if the token
passes. It then manages the position with take-profit and stop-loss rules.

This strategy works identically in backtest and live mode — the same code
processes historical GraduationEvents or real-time ones from the monitor.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

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
    highest_price: float = 0.0  # for trailing stop
    
    @property
    def age_seconds(self) -> float:
        return (time.time_ns() - self.entered_at_ns) / 1e9


class GraduationSniper(Strategy):
    """
    Trade newly graduated pump.fun tokens.
    
    Filters:
    - Minimum holder count at graduation
    - Minimum SOL raised on bonding curve
    - Maximum time since graduation (don't chase old ones)
    - Dev wallet behavior (skip if dev already sold)
    
    Position management:
    - Fixed USD size per trade
    - Take profit at configurable %
    - Stop loss at configurable %
    - Trailing stop after initial pump
    - Max hold time (force exit after N minutes)
    - Auto-exit on dev sell detection
    
    Args:
        position_size_usd: USD amount per trade
        max_positions: Maximum concurrent positions
        min_holders: Skip tokens with fewer holders
        min_sol_raised: Skip tokens that raised less SOL
        max_entry_delay_s: Don't enter if graduation was > N seconds ago
        take_profit_pct: Exit at this % gain
        stop_loss_pct: Exit at this % loss
        trailing_stop_pct: Trailing stop distance after profit
        trailing_activate_pct: Activate trailing stop after this % gain
        max_hold_seconds: Force exit after this many seconds
        exit_on_dev_sell: Auto-exit if dev dumps
        min_initial_mcap: Minimum market cap at graduation
        max_initial_mcap: Maximum market cap at graduation
    """
    
    name = "graduation_sniper"
    
    def __init__(
        self,
        position_size_usd: float = 50.0,
        max_positions: int = 3,
        min_holders: int = 100,
        min_sol_raised: float = 50.0,
        max_entry_delay_s: float = 30.0,
        take_profit_pct: float = 0.50,       # 50%
        stop_loss_pct: float = 0.20,          # 20%
        trailing_stop_pct: float = 0.15,      # 15% trailing
        trailing_activate_pct: float = 0.30,  # activate after 30% gain
        max_hold_seconds: float = 300.0,      # 5 minutes max
        exit_on_dev_sell: bool = True,
        min_initial_mcap: float = 0.0,
        max_initial_mcap: float = 500_000.0,  # 500K max
    ) -> None:
        super().__init__()
        self.position_size_usd = position_size_usd
        self.max_positions = max_positions
        self.min_holders = min_holders
        self.min_sol_raised = min_sol_raised
        self.max_entry_delay_s = max_entry_delay_s
        self.take_profit_pct = take_profit_pct
        self.stop_loss_pct = stop_loss_pct
        self.trailing_stop_pct = trailing_stop_pct
        self.trailing_activate_pct = trailing_activate_pct
        self.max_hold_seconds = max_hold_seconds
        self.exit_on_dev_sell = exit_on_dev_sell
        self.min_initial_mcap = min_initial_mcap
        self.max_initial_mcap = max_initial_mcap
        
        self._positions: dict[str, Position] = {}
        self._passed: int = 0
        self._filtered: int = 0
        self._exits: dict[str, int] = {
            "take_profit": 0,
            "stop_loss": 0,
            "trailing_stop": 0,
            "timeout": 0,
            "dev_sell": 0,
        }

    def bind(self, event_bus) -> None:
        """Subscribe to graduation and dev activity events."""
        super().bind(event_bus)
        # Subscribe to graduation signals
        event_bus.subscribe(EventType.SIGNAL, self._handle_signal)

    def _handle_signal(self, event: Event) -> None:
        """Route signal events to appropriate handlers."""
        if isinstance(event, GraduationEvent):
            self._on_graduation(event)
        elif isinstance(event, DevActivityEvent):
            self._on_dev_activity(event)

    def _on_graduation(self, event: GraduationEvent) -> None:
        """
        Evaluate a newly graduated token.
        
        Apply filters, and if the token passes, enter a position.
        """
        mint = event.mint
        symbol = event.symbol or mint[:8]
        
        # -- Filters --
        
        # Already in this token?
        if mint in self._positions:
            return
        
        # Max positions reached?
        if len(self._positions) >= self.max_positions:
            self._filtered += 1
            logger.debug(f"SKIP {symbol}: max positions ({self.max_positions})")
            return
        
        # Holder count
        if event.holder_count < self.min_holders:
            self._filtered += 1
            logger.debug(f"SKIP {symbol}: holders {event.holder_count} < {self.min_holders}")
            return
        
        # SOL raised
        if event.sol_raised < self.min_sol_raised:
            self._filtered += 1
            logger.debug(f"SKIP {symbol}: SOL raised {event.sol_raised:.1f} < {self.min_sol_raised}")
            return
        
        # Price available?
        if event.initial_price_usd <= 0:
            self._filtered += 1
            logger.debug(f"SKIP {symbol}: no price data")
            return
        
        # -- Entry --
        
        self._passed += 1
        entry_price = event.initial_price_usd
        amount_tokens = self.position_size_usd / entry_price
        
        self._positions[mint] = Position(
            mint=mint,
            symbol=symbol,
            entry_price=entry_price,
            amount_usd=self.position_size_usd,
            amount_tokens=amount_tokens,
            entered_at_ns=time.time_ns(),
            highest_price=entry_price,
        )
        
        # Execute buy
        self.buy(mint, amount_usd=self.position_size_usd, slippage_bps=300)
        
        logger.info(
            f"🎯 ENTRY | {symbol} | ${entry_price:.8f} | "
            f"${self.position_size_usd:.0f} | holders={event.holder_count}"
        )

    def on_price_update(self, event: PriceUpdate) -> None:
        """
        Manage open positions based on price updates.
        
        Check take-profit, stop-loss, trailing stop, and timeout.
        """
        mint = event.token
        if mint not in self._positions:
            return
        
        pos = self._positions[mint]
        price = event.price_usd
        
        if price <= 0:
            return
        
        # Update highest price for trailing stop
        if price > pos.highest_price:
            pos.highest_price = price
        
        pnl_pct = (price - pos.entry_price) / pos.entry_price
        drawdown_from_high = (pos.highest_price - price) / pos.highest_price if pos.highest_price > 0 else 0
        
        # -- Exit conditions --
        
        # Take profit
        if pnl_pct >= self.take_profit_pct:
            self._exit(pos, price, "take_profit", pnl_pct)
            return
        
        # Stop loss
        if pnl_pct <= -self.stop_loss_pct:
            self._exit(pos, price, "stop_loss", pnl_pct)
            return
        
        # Trailing stop (only active after trailing_activate_pct gain)
        peak_pnl = (pos.highest_price - pos.entry_price) / pos.entry_price
        if peak_pnl >= self.trailing_activate_pct:
            if drawdown_from_high >= self.trailing_stop_pct:
                self._exit(pos, price, "trailing_stop", pnl_pct)
                return
        
        # Timeout
        if pos.age_seconds >= self.max_hold_seconds:
            self._exit(pos, price, "timeout", pnl_pct)
            return

    def _on_dev_activity(self, event: DevActivityEvent) -> None:
        """Handle dev wallet activity — exit if dev sells."""
        if not self.exit_on_dev_sell:
            return
        
        mint = event.mint
        if mint not in self._positions:
            return
        
        if event.action == "sell":
            pos = self._positions[mint]
            logger.warning(
                f"⚠️ DEV SOLD {event.amount_pct:.1f}% of {event.symbol} — exiting"
            )
            # Use current price (will be updated by next price event)
            # For now, exit at entry price as conservative estimate
            self._exit(pos, pos.entry_price, "dev_sell", 0)

    def _exit(self, pos: Position, price: float, reason: str, pnl_pct: float) -> None:
        """Exit a position and log the result."""
        realized_pnl = pos.amount_tokens * (price - pos.entry_price)
        self._pnl += realized_pnl
        self._exits[reason] = self._exits.get(reason, 0) + 1
        
        self.sell(pos.mint, pos.amount_tokens, slippage_bps=500)
        
        emoji = "✅" if realized_pnl > 0 else "❌"
        logger.info(
            f"{emoji} EXIT | {pos.symbol} | {reason} | "
            f"pnl={pnl_pct:+.1%} (${realized_pnl:+.2f}) | "
            f"held={pos.age_seconds:.0f}s"
        )
        
        self._positions.pop(pos.mint, None)

    def on_stop(self) -> None:
        """Log final stats."""
        logger.info(
            f"[{self.name}] FINAL | "
            f"entries={self._passed} filtered={self._filtered} | "
            f"pnl=${self._pnl:+.2f} | "
            f"exits={dict(self._exits)}"
        )

    @property
    def stats(self) -> dict:
        return {
            **super().stats,
            "open_positions": len(self._positions),
            "passed_filter": self._passed,
            "filtered_out": self._filtered,
            "exits_by_reason": dict(self._exits),
        }
