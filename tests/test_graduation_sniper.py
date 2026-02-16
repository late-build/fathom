"""Tests for the GraduationSniper strategy."""

import time
from fathom.core.events import EventBus, EventType, PriceUpdate
from fathom.adapters.pumpfun.graduation import GraduationEvent, DevActivityEvent
from fathom.strategies.graduation_sniper import GraduationSniper


def make_graduation(
    mint="TestMint123",
    symbol="TEST",
    holders=150,
    sol_raised=70.0,
    price=0.000042,
) -> GraduationEvent:
    return GraduationEvent(
        event_type=EventType.SIGNAL,
        source="test",
        mint=mint,
        symbol=symbol,
        pool_address="pool123",
        pool_type="pumpswap",
        sol_raised=sol_raised,
        holder_count=holders,
        creator="creator123",
        initial_price_usd=price,
    )


def make_price(mint="TestMint123", price=0.000042) -> PriceUpdate:
    return PriceUpdate(
        event_type=EventType.PRICE_UPDATE,
        source="test",
        token=mint,
        price_usd=price,
    )


class TestGraduationSniperFilters:
    def setup_method(self):
        self.bus = EventBus()
        self.orders = []
        self.bus.subscribe(EventType.ORDER_SUBMITTED, lambda e: self.orders.append(e))

    def test_enters_on_valid_graduation(self):
        strategy = GraduationSniper(min_holders=100, min_sol_raised=50)
        strategy.bind(self.bus)

        self.bus.publish(make_graduation(holders=150, sol_raised=70))
        assert len(self.orders) == 1

    def test_filters_low_holders(self):
        strategy = GraduationSniper(min_holders=100)
        strategy.bind(self.bus)

        self.bus.publish(make_graduation(holders=50))
        assert len(self.orders) == 0

    def test_filters_low_sol_raised(self):
        strategy = GraduationSniper(min_sol_raised=60)
        strategy.bind(self.bus)

        self.bus.publish(make_graduation(sol_raised=40))
        assert len(self.orders) == 0

    def test_filters_no_price(self):
        strategy = GraduationSniper()
        strategy.bind(self.bus)

        self.bus.publish(make_graduation(price=0))
        assert len(self.orders) == 0

    def test_max_positions_enforced(self):
        strategy = GraduationSniper(max_positions=2, min_holders=0, min_sol_raised=0)
        strategy.bind(self.bus)

        self.bus.publish(make_graduation(mint="A", holders=10, sol_raised=10, price=0.001))
        self.bus.publish(make_graduation(mint="B", holders=10, sol_raised=10, price=0.001))
        self.bus.publish(make_graduation(mint="C", holders=10, sol_raised=10, price=0.001))

        assert len(self.orders) == 2

    def test_no_duplicate_entry(self):
        strategy = GraduationSniper(min_holders=0, min_sol_raised=0)
        strategy.bind(self.bus)

        self.bus.publish(make_graduation(mint="A", holders=10, sol_raised=10))
        self.bus.publish(make_graduation(mint="A", holders=10, sol_raised=10))

        assert len(self.orders) == 1


class TestGraduationSniperExits:
    def setup_method(self):
        self.bus = EventBus()
        self.orders = []
        self.bus.subscribe(EventType.ORDER_SUBMITTED, lambda e: self.orders.append(e))

    def test_take_profit(self):
        strategy = GraduationSniper(
            take_profit_pct=0.50,
            min_holders=0,
            min_sol_raised=0,
        )
        strategy.bind(self.bus)

        # Enter at 0.001
        self.bus.publish(make_graduation(price=0.001))
        assert len(self.orders) == 1  # buy

        # Price goes up 60% → should trigger TP
        self.bus.publish(make_price(price=0.0016))
        assert len(self.orders) == 2  # buy + sell

    def test_stop_loss(self):
        strategy = GraduationSniper(
            stop_loss_pct=0.20,
            min_holders=0,
            min_sol_raised=0,
        )
        strategy.bind(self.bus)

        self.bus.publish(make_graduation(price=0.001))
        self.bus.publish(make_price(price=0.0007))  # -30%
        assert len(self.orders) == 2  # buy + sell

    def test_dev_sell_exit(self):
        strategy = GraduationSniper(
            exit_on_dev_sell=True,
            min_holders=0,
            min_sol_raised=0,
        )
        strategy.bind(self.bus)

        self.bus.publish(make_graduation(mint="X", price=0.001))
        assert len(self.orders) == 1

        self.bus.publish(DevActivityEvent(
            event_type=EventType.SIGNAL,
            source="test",
            mint="X",
            symbol="TEST",
            action="sell",
            amount_pct=50.0,
        ))
        assert len(self.orders) == 2  # exited on dev sell

    def test_trailing_stop(self):
        strategy = GraduationSniper(
            take_profit_pct=1.0,  # high TP so it doesn't trigger
            trailing_activate_pct=0.30,
            trailing_stop_pct=0.15,
            min_holders=0,
            min_sol_raised=0,
        )
        strategy.bind(self.bus)

        # Enter at 0.001
        self.bus.publish(make_graduation(price=0.001))

        # Price goes to 0.0015 (+50%, activates trailing)
        self.bus.publish(make_price(price=0.0015))
        assert len(self.orders) == 1  # still holding

        # Price drops to 0.00125 (16.7% from high, > 15% trailing)
        self.bus.publish(make_price(price=0.00125))
        assert len(self.orders) == 2  # trailing stop hit
