"""
Log-only strategy — prints every event without trading.

Used by `fathom monitor` to observe graduations.
"""

from __future__ import annotations

import logging

from fathom.core.events import Event, EventType, PriceUpdate
from fathom.core.strategy import Strategy
from fathom.adapters.pumpfun.graduation import (
    GraduationEvent,
    DevActivityEvent,
    BondingProgressEvent,
)

logger = logging.getLogger("fathom.monitor")


class LogOnlyStrategy(Strategy):
    name = "log_only"

    def __init__(self) -> None:
        super().__init__()
        self._graduations: int = 0
        self._prices: int = 0

    def bind(self, event_bus) -> None:
        super().bind(event_bus)
        event_bus.subscribe(EventType.SIGNAL, self._handle_signal)

    def _handle_signal(self, event: Event) -> None:
        if isinstance(event, GraduationEvent):
            self._graduations += 1
            logger.info(
                f"🎓 #{self._graduations} | {event.symbol or event.mint[:12]} | "
                f"holders={event.holder_count} | sol={event.sol_raised:.1f} | "
                f"price=${event.initial_price_usd:.8f} | pool={event.pool_type}"
            )
        elif isinstance(event, DevActivityEvent):
            emoji = "🚨" if event.action == "sell" else "ℹ️"
            logger.info(
                f"{emoji} DEV {event.action.upper()} | {event.symbol or event.mint[:12]} | "
                f"{event.amount_pct:.1f}%"
            )
        elif isinstance(event, BondingProgressEvent):
            logger.info(
                f"📈 BONDING | {event.symbol or event.mint[:12]} | "
                f"{event.progress_pct:.1f}% | {event.sol_raised:.1f} SOL | "
                f"holders={event.holder_count}"
            )

    def on_price_update(self, event: PriceUpdate) -> None:
        self._prices += 1
        if self._prices % 50 == 0:  # Log every 50th to avoid spam
            logger.debug(
                f"💰 {event.token[:12]} | ${event.price_usd:.8f} | "
                f"vol24h=${event.volume_24h:,.0f}"
            )

    def on_stop(self) -> None:
        logger.info(
            f"[monitor] {self._graduations} graduations observed | "
            f"{self._prices} price updates"
        )
