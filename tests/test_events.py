"""Tests for the event system."""

import time
from fathom.core.events import (
    Event, EventType, EventBus, PriceUpdate, Trade
)


class TestEvent:
    def test_event_has_nanosecond_timestamp(self):
        before = time.time_ns()
        event = Event(event_type=EventType.HEARTBEAT)
        after = time.time_ns()
        assert before <= event.timestamp_ns <= after

    def test_event_timestamp_conversions(self):
        event = Event(event_type=EventType.HEARTBEAT, timestamp_ns=1_000_000_000)
        assert event.timestamp_ms == 1000.0
        assert event.timestamp_s == 1.0

    def test_event_is_immutable(self):
        event = Event(event_type=EventType.HEARTBEAT)
        try:
            event.source = "modified"
            assert False, "Should not allow mutation"
        except AttributeError:
            pass

    def test_price_update_fields(self):
        event = PriceUpdate(
            event_type=EventType.PRICE_UPDATE,
            source="test",
            token="SOL",
            price_usd=148.50,
            volume_24h=1_000_000,
            liquidity=500_000,
        )
        assert event.token == "SOL"
        assert event.price_usd == 148.50
        assert event.event_type == EventType.PRICE_UPDATE

    def test_trade_fields(self):
        event = Trade(
            event_type=EventType.TRADE,
            token_in="USDC",
            token_out="SOL",
            amount_in=100.0,
            amount_out=0.67,
            price=148.50,
            pool="raydium_sol_usdc",
            tx_signature="abc123",
        )
        assert event.token_in == "USDC"
        assert event.amount_out == 0.67


class TestEventBus:
    def test_subscribe_and_publish(self):
        bus = EventBus()
        received = []

        def handler(event):
            received.append(event)

        bus.subscribe(EventType.PRICE_UPDATE, handler)
        event = PriceUpdate(
            event_type=EventType.PRICE_UPDATE,
            token="SOL",
            price_usd=150.0,
        )
        bus.publish(event)

        assert len(received) == 1
        assert received[0].token == "SOL"

    def test_multiple_handlers(self):
        bus = EventBus()
        counts = {"a": 0, "b": 0}

        def handler_a(event):
            counts["a"] += 1

        def handler_b(event):
            counts["b"] += 1

        bus.subscribe(EventType.HEARTBEAT, handler_a)
        bus.subscribe(EventType.HEARTBEAT, handler_b)
        bus.publish(Event(event_type=EventType.HEARTBEAT))

        assert counts["a"] == 1
        assert counts["b"] == 1

    def test_unsubscribe(self):
        bus = EventBus()
        received = []

        def handler(event):
            received.append(event)

        bus.subscribe(EventType.HEARTBEAT, handler)
        bus.unsubscribe(EventType.HEARTBEAT, handler)
        bus.publish(Event(event_type=EventType.HEARTBEAT))

        assert len(received) == 0

    def test_handler_error_isolation(self):
        """A failing handler should not prevent other handlers from running."""
        bus = EventBus()
        received = []

        def bad_handler(event):
            raise ValueError("broken")

        def good_handler(event):
            received.append(event)

        bus.subscribe(EventType.HEARTBEAT, bad_handler)
        bus.subscribe(EventType.HEARTBEAT, good_handler)
        bus.publish(Event(event_type=EventType.HEARTBEAT))

        assert len(received) == 1

    def test_handler_error_emits_error_event(self):
        bus = EventBus()
        errors = []

        def bad_handler(event):
            raise ValueError("test error")

        def error_handler(event):
            errors.append(event)

        bus.subscribe(EventType.HEARTBEAT, bad_handler)
        bus.subscribe(EventType.ERROR, error_handler)
        bus.publish(Event(event_type=EventType.HEARTBEAT))

        assert len(errors) == 1
        assert "test error" in errors[0].data["error"]

    def test_event_type_filtering(self):
        """Handlers only receive events they subscribed to."""
        bus = EventBus()
        received = []

        def handler(event):
            received.append(event)

        bus.subscribe(EventType.PRICE_UPDATE, handler)
        bus.publish(Event(event_type=EventType.HEARTBEAT))
        bus.publish(Event(event_type=EventType.TRADE))
        bus.publish(PriceUpdate(event_type=EventType.PRICE_UPDATE, token="SOL", price_usd=1.0))

        assert len(received) == 1

    def test_stats(self):
        bus = EventBus()
        bus.subscribe(EventType.HEARTBEAT, lambda e: None)
        bus.subscribe(EventType.HEARTBEAT, lambda e: None)
        bus.publish(Event(event_type=EventType.HEARTBEAT))
        bus.publish(Event(event_type=EventType.HEARTBEAT))

        stats = bus.stats
        assert stats["events_processed"] == 2
        assert stats["handlers_registered"] == 2
        assert stats["errors"] == 0
