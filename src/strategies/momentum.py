"""
Example: Simple momentum strategy.

Buys when a token's price moves up by a threshold within a lookback window.
Sells when the position hits a take-profit or stop-loss level.

This is an example — not financial advice. Modify and backtest before
using with real funds.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass

from fathom.core.events import PriceUpdate
from fathom.core.strategy import Strategy


@dataclass
class PricePoint:
    price: float
    timestamp_ns: int


class MomentumStrategy(Strategy):
    """
    Momentum-based entry with configurable take-profit and stop-loss.
    
    Args:
        lookback_window: Seconds to look back for momentum calculation
        entry_threshold: Minimum % move to trigger entry (0.02 = 2%)
        position_size: Fraction of portfolio per trade (0.1 = 10%)
        take_profit: Exit at this % gain (0.05 = 5%)
        stop_loss: Exit at this % loss (0.03 = 3%)
        tokens: List of tokens to trade (empty = trade all)
    """
    
    name = "momentum"
    
    def __init__(
        self,
        lookback_window: int = 60,
        entry_threshold: float = 0.02,
        position_size: float = 0.1,
        take_profit: float = 0.05,
        stop_loss: float = 0.03,
        tokens: list[str] | None = None,
        portfolio_usd: float = 1000.0,
    ) -> None:
        super().__init__()
        self.lookback_window = lookback_window
        self.entry_threshold = entry_threshold
        self.position_size = position_size
        self.take_profit = take_profit
        self.stop_loss = stop_loss
        self.target_tokens = set(tokens) if tokens else set()
        self.portfolio_usd = portfolio_usd
        
        # Price history per token
        self._history: dict[str, deque[PricePoint]] = {}
        
        # Active positions: token -> entry_price
        self._entries: dict[str, float] = {}

    def on_start(self) -> None:
        pass

    def on_price_update(self, event: PriceUpdate) -> None:
        token = event.token
        
        # Filter tokens if specified
        if self.target_tokens and token not in self.target_tokens:
            return
        
        price = event.price_usd
        now = event.timestamp_ns
        
        # Update price history
        if token not in self._history:
            self._history[token] = deque(maxlen=10_000)
        
        self._history[token].append(PricePoint(price=price, timestamp_ns=now))
        
        # Prune old entries outside lookback window
        cutoff = now - (self.lookback_window * 1_000_000_000)
        while self._history[token] and self._history[token][0].timestamp_ns < cutoff:
            self._history[token].popleft()
        
        # Check for exit conditions on existing positions
        if token in self._entries:
            entry_price = self._entries[token]
            pnl_pct = (price - entry_price) / entry_price
            
            if pnl_pct >= self.take_profit:
                # Take profit
                amount = self._position.get(token, 0)
                if amount > 0:
                    self.sell(token, amount)
                    self._pnl += amount * price - amount * entry_price
                    del self._entries[token]
                    self._position.pop(token, None)
                return
            
            if pnl_pct <= -self.stop_loss:
                # Stop loss
                amount = self._position.get(token, 0)
                if amount > 0:
                    self.sell(token, amount)
                    self._pnl += amount * price - amount * entry_price
                    del self._entries[token]
                    self._position.pop(token, None)
                return
        
        # Check for entry conditions
        if token not in self._entries and len(self._history[token]) >= 2:
            oldest_price = self._history[token][0].price
            if oldest_price > 0:
                momentum = (price - oldest_price) / oldest_price
                
                if momentum >= self.entry_threshold:
                    # Enter position
                    trade_usd = self.portfolio_usd * self.position_size
                    self.buy(token, amount_usd=trade_usd)
                    self._entries[token] = price
                    self._position[token] = trade_usd / price
