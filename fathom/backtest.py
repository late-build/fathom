"""
Backtest runner — replays historical graduation data through a strategy.

Feeds GraduationEvents and PriceUpdates from recorded data, using the
same strategy code that runs live. No lookahead bias.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from fathom.core.events import EventBus, EventType, PriceUpdate
from fathom.core.strategy import Strategy
from fathom.adapters.paper import PaperAdapter
from fathom.adapters.pumpfun.graduation import GraduationEvent

logger = logging.getLogger("fathom.backtest")


@dataclass
class BacktestResult:
    total_graduations: int = 0
    trades_entered: int = 0
    trades_exited: int = 0
    total_pnl: float = 0.0
    win_count: int = 0
    loss_count: int = 0
    best_trade: float = 0.0
    worst_trade: float = 0.0
    max_drawdown: float = 0.0
    final_balance: float = 0.0
    initial_balance: float = 0.0
    duration_seconds: float = 0.0


class BacktestRunner:
    """
    Replay historical data through a strategy.

    Data format (JSON array):
    [
        {
            "mint": "...",
            "symbol": "EXAMPLE",
            "graduated_at": 1708000000,
            "initial_price_usd": 0.000042,
            "sol_raised": 85.0,
            "holder_count": 200,
            "creator": "...",
            "pool_address": "...",
            "pool_type": "pumpswap",
            "price_history": [
                {"timestamp": 1708000060, "price": 0.000045, "volume_5m": 12000},
                ...
            ]
        },
        ...
    ]
    """

    def __init__(
        self,
        strategy: Strategy,
        adapter: PaperAdapter,
        data: list[dict[str, Any]],
    ) -> None:
        self.strategy = strategy
        self.adapter = adapter
        self.data = data
        self.event_bus = EventBus()

    def run(self) -> BacktestResult:
        start = time.time()

        # Wire up
        self.strategy.bind(self.event_bus)
        self.adapter.bind(self.event_bus)

        # Manually connect adapter (sync, for backtest)
        self.adapter._connected = True
        self.event_bus.subscribe(EventType.ORDER_SUBMITTED, self.adapter._handle_order)
        self.event_bus.subscribe(EventType.PRICE_UPDATE, self.adapter._track_price)

        self.strategy.on_start()

        result = BacktestResult(
            initial_balance=self.adapter.initial_balance_usd,
            total_graduations=len(self.data),
        )

        peak_balance = self.adapter.initial_balance_usd

        # Sort by graduation time
        sorted_data = sorted(self.data, key=lambda d: d.get("graduated_at", 0))

        for record in sorted_data:
            mint = record["mint"]
            symbol = record.get("symbol", mint[:8])

            # Seed initial price so paper adapter can fill at correct price
            if record.get("initial_price_usd", 0) > 0:
                self.adapter.set_price(mint, record["initial_price_usd"])

            # Emit graduation event
            grad = GraduationEvent(
                source="backtest",
                mint=mint,
                symbol=symbol,
                pool_address=record.get("pool_address", ""),
                pool_type=record.get("pool_type", "pumpswap"),
                sol_raised=record.get("sol_raised", 0),
                holder_count=record.get("holder_count", 0),
                creator=record.get("creator", ""),
                initial_price_usd=record.get("initial_price_usd", 0),
            )
            self.event_bus.publish(grad)

            # Replay price history
            for point in record.get("price_history", []):
                price = point.get("price", 0)
                if price <= 0:
                    continue

                self.event_bus.publish(PriceUpdate(
                    source="backtest",
                    token=mint,
                    price_usd=price,
                    volume_24h=point.get("volume_5m", 0) * 288,
                ))

            # Track balance for drawdown
            current = self.adapter.balance_usd
            if current > peak_balance:
                peak_balance = current
            dd = (peak_balance - current) / peak_balance if peak_balance > 0 else 0
            if dd > result.max_drawdown:
                result.max_drawdown = dd

        self.strategy.on_stop()

        result.final_balance = self.adapter.balance_usd
        result.total_pnl = self.adapter.pnl
        result.trades_entered = self.adapter._fill_count
        result.duration_seconds = time.time() - start

        # Extract win/loss from strategy stats if available
        if hasattr(self.strategy, '_exits'):
            exits = getattr(self.strategy, '_exits', {})
            result.win_count = exits.get("take_profit", 0) + exits.get("trailing_stop", 0)
            result.loss_count = exits.get("stop_loss", 0) + exits.get("timeout", 0) + exits.get("dev_sell", 0)

        return result

    @staticmethod
    def print_report(r: BacktestResult) -> None:
        total_trades = r.win_count + r.loss_count
        win_rate = r.win_count / total_trades * 100 if total_trades > 0 else 0
        roi = (r.final_balance - r.initial_balance) / r.initial_balance * 100 if r.initial_balance > 0 else 0

        print()
        print("=" * 50)
        print("  BACKTEST RESULTS")
        print("=" * 50)
        print(f"  Graduations:    {r.total_graduations}")
        print(f"  Trades taken:   {total_trades}")
        print(f"  Wins:           {r.win_count}")
        print(f"  Losses:         {r.loss_count}")
        print(f"  Win rate:       {win_rate:.1f}%")
        print(f"  P&L:            ${r.total_pnl:+,.2f}")
        print(f"  ROI:            {roi:+.1f}%")
        print(f"  Max drawdown:   {r.max_drawdown:.1%}")
        print(f"  Final balance:  ${r.final_balance:,.2f}")
        print(f"  Runtime:        {r.duration_seconds:.2f}s")
        print("=" * 50)
