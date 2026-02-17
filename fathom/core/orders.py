"""Order type system for the Fathom trading engine.

Defines a rich order model with multiple order types, time-in-force
semantics, status tracking, fill recording, and an in-memory order book
for managing pending orders.  Also includes fill simulation logic used
by the backtest runner.

Typical usage::

    from fathom.core.orders import Order, OrderType, TimeInForce, OrderBook

    order = Order.market(token="SOL", side="buy", quantity=10.0)
    book = OrderBook()
    book.submit(order)
    book.try_fill(order.order_id, fill_price=148.0, fill_qty=10.0)
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Dict, List, Optional, Sequence


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class OrderType(Enum):
    """Supported order types.

    Attributes:
        MARKET: Execute immediately at best available price.
        LIMIT: Execute at specified price or better.
        STOP: Trigger a market order when stop price is reached.
        STOP_LIMIT: Trigger a limit order when stop price is reached.
        TRAILING_STOP: Stop that trails the market by a fixed offset.
        TWAP: Time-weighted average price — split into slices over time.
        ICEBERG: Show only a portion of the total size at a time.
    """
    MARKET = auto()
    LIMIT = auto()
    STOP = auto()
    STOP_LIMIT = auto()
    TRAILING_STOP = auto()
    TWAP = auto()
    ICEBERG = auto()


class OrderStatus(Enum):
    """Lifecycle states of an order."""
    PENDING = auto()
    SUBMITTED = auto()
    ACCEPTED = auto()
    PARTIALLY_FILLED = auto()
    FILLED = auto()
    CANCELLED = auto()
    REJECTED = auto()
    EXPIRED = auto()


class TimeInForce(Enum):
    """How long an order remains active.

    Attributes:
        GTC: Good-til-cancelled.
        IOC: Immediate-or-cancel (fill what you can, cancel rest).
        FOK: Fill-or-kill (all or nothing).
        GTD: Good-til-date (expires at a specified time).
    """
    GTC = auto()
    IOC = auto()
    FOK = auto()
    GTD = auto()


class OrderSide(Enum):
    """Trade direction."""
    BUY = auto()
    SELL = auto()


# ---------------------------------------------------------------------------
# Fill record
# ---------------------------------------------------------------------------

@dataclass
class Fill:
    """Record of a partial or complete fill.

    Attributes:
        fill_id: Unique fill identifier.
        order_id: Parent order ID.
        price: Execution price.
        quantity: Filled quantity.
        timestamp_ns: Fill time in nanoseconds.
        fees_usd: Estimated fees for this fill.
        tx_signature: On-chain transaction hash (if applicable).
    """

    __slots__ = (
        "fill_id", "order_id", "price", "quantity",
        "timestamp_ns", "fees_usd", "tx_signature",
    )
    fill_id: str
    order_id: str
    price: float
    quantity: float
    timestamp_ns: int
    fees_usd: float
    tx_signature: str


# ---------------------------------------------------------------------------
# Order
# ---------------------------------------------------------------------------

@dataclass
class Order:
    """A trading order with full lifecycle tracking.

    Attributes:
        order_id: Unique identifier (auto-generated UUID4).
        token: Token mint or symbol.
        side: Buy or sell.
        order_type: Type of order.
        quantity: Total quantity requested.
        limit_price: Price for LIMIT / STOP_LIMIT orders.
        stop_price: Trigger price for STOP / STOP_LIMIT / TRAILING_STOP.
        trail_offset_pct: Percentage offset for TRAILING_STOP (e.g. 0.05 = 5%).
        time_in_force: Order duration policy.
        expire_at_ns: Expiry time for GTD orders (nanoseconds).
        slippage_bps: Maximum tolerated slippage in basis points.
        twap_slices: Number of time slices for TWAP orders.
        twap_interval_s: Seconds between TWAP slices.
        iceberg_show_qty: Visible quantity for ICEBERG orders.
        status: Current order status.
        filled_quantity: Cumulative filled quantity.
        avg_fill_price: Volume-weighted average fill price.
        fills: List of individual fills.
        created_at_ns: Creation timestamp.
        updated_at_ns: Last status change timestamp.
        strategy: Strategy that created this order.
        metadata: Arbitrary key-value metadata.
    """

    order_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    token: str = ""
    side: OrderSide = OrderSide.BUY
    order_type: OrderType = OrderType.MARKET
    quantity: float = 0.0
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    trail_offset_pct: float = 0.05
    time_in_force: TimeInForce = TimeInForce.GTC
    expire_at_ns: int = 0
    slippage_bps: int = 50
    twap_slices: int = 5
    twap_interval_s: float = 60.0
    iceberg_show_qty: float = 0.0
    status: OrderStatus = OrderStatus.PENDING
    filled_quantity: float = 0.0
    avg_fill_price: float = 0.0
    fills: List[Fill] = field(default_factory=list)
    created_at_ns: int = field(default_factory=time.time_ns)
    updated_at_ns: int = field(default_factory=time.time_ns)
    strategy: str = ""
    metadata: Dict[str, str] = field(default_factory=dict)

    # -- factory methods -----------------------------------------------------

    @classmethod
    def market(
        cls,
        token: str,
        side: str,
        quantity: float,
        slippage_bps: int = 50,
        strategy: str = "",
    ) -> Order:
        """Create a market order.

        Args:
            token: Token to trade.
            side: ``"buy"`` or ``"sell"``.
            quantity: Amount to trade.
            slippage_bps: Max slippage in basis points.
            strategy: Originating strategy name.
        """
        return cls(
            token=token,
            side=OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL,
            order_type=OrderType.MARKET,
            quantity=quantity,
            slippage_bps=slippage_bps,
            strategy=strategy,
        )

    @classmethod
    def limit(
        cls,
        token: str,
        side: str,
        quantity: float,
        limit_price: float,
        time_in_force: TimeInForce = TimeInForce.GTC,
        strategy: str = "",
    ) -> Order:
        """Create a limit order.

        Args:
            token: Token to trade.
            side: ``"buy"`` or ``"sell"``.
            quantity: Amount to trade.
            limit_price: Maximum (buy) or minimum (sell) price.
            time_in_force: Duration policy.
            strategy: Originating strategy name.
        """
        return cls(
            token=token,
            side=OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL,
            order_type=OrderType.LIMIT,
            quantity=quantity,
            limit_price=limit_price,
            time_in_force=time_in_force,
            strategy=strategy,
        )

    @classmethod
    def stop(
        cls,
        token: str,
        side: str,
        quantity: float,
        stop_price: float,
        strategy: str = "",
    ) -> Order:
        """Create a stop (market) order.

        Args:
            token: Token to trade.
            side: ``"buy"`` or ``"sell"``.
            quantity: Amount to trade.
            stop_price: Price that triggers the order.
            strategy: Originating strategy name.
        """
        return cls(
            token=token,
            side=OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL,
            order_type=OrderType.STOP,
            quantity=quantity,
            stop_price=stop_price,
            strategy=strategy,
        )

    @classmethod
    def trailing_stop(
        cls,
        token: str,
        side: str,
        quantity: float,
        trail_offset_pct: float = 0.05,
        strategy: str = "",
    ) -> Order:
        """Create a trailing stop order.

        Args:
            token: Token to trade.
            side: ``"buy"`` or ``"sell"``.
            quantity: Amount to trade.
            trail_offset_pct: Trail distance as fraction (0.05 = 5%).
            strategy: Originating strategy name.
        """
        return cls(
            token=token,
            side=OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL,
            order_type=OrderType.TRAILING_STOP,
            quantity=quantity,
            trail_offset_pct=trail_offset_pct,
            strategy=strategy,
        )

    # -- lifecycle -----------------------------------------------------------

    def record_fill(
        self,
        price: float,
        quantity: float,
        fees_usd: float = 0.0,
        tx_signature: str = "",
    ) -> Fill:
        """Record a fill against this order.

        Updates ``filled_quantity``, ``avg_fill_price``, and ``status``.

        Args:
            price: Fill price.
            quantity: Filled quantity.
            fees_usd: Transaction fees.
            tx_signature: On-chain tx hash.

        Returns:
            The ``Fill`` object created.
        """
        fill = Fill(
            fill_id=uuid.uuid4().hex[:12],
            order_id=self.order_id,
            price=price,
            quantity=quantity,
            timestamp_ns=time.time_ns(),
            fees_usd=fees_usd,
            tx_signature=tx_signature,
        )
        # Update VWAP
        prev_notional = self.avg_fill_price * self.filled_quantity
        self.filled_quantity += quantity
        if self.filled_quantity > 0:
            self.avg_fill_price = (prev_notional + price * quantity) / self.filled_quantity

        self.fills.append(fill)
        self.updated_at_ns = time.time_ns()

        if self.filled_quantity >= self.quantity:
            self.status = OrderStatus.FILLED
        else:
            self.status = OrderStatus.PARTIALLY_FILLED

        return fill

    def cancel(self) -> None:
        """Cancel this order if it is still active."""
        if self.status in (
            OrderStatus.PENDING,
            OrderStatus.SUBMITTED,
            OrderStatus.ACCEPTED,
            OrderStatus.PARTIALLY_FILLED,
        ):
            self.status = OrderStatus.CANCELLED
            self.updated_at_ns = time.time_ns()

    @property
    def is_active(self) -> bool:
        """Whether this order can still receive fills."""
        return self.status in (
            OrderStatus.PENDING,
            OrderStatus.SUBMITTED,
            OrderStatus.ACCEPTED,
            OrderStatus.PARTIALLY_FILLED,
        )

    @property
    def remaining_quantity(self) -> float:
        """Unfilled quantity."""
        return max(self.quantity - self.filled_quantity, 0.0)

    def validate(self) -> List[str]:
        """Validate order fields and return a list of errors (empty = valid).

        Returns:
            List of validation error strings.
        """
        errors: List[str] = []
        if self.quantity <= 0:
            errors.append("quantity must be > 0")
        if not self.token:
            errors.append("token is required")
        if self.order_type in (OrderType.LIMIT, OrderType.STOP_LIMIT):
            if self.limit_price is None or self.limit_price <= 0:
                errors.append(f"{self.order_type.name} requires a positive limit_price")
        if self.order_type in (OrderType.STOP, OrderType.STOP_LIMIT):
            if self.stop_price is None or self.stop_price <= 0:
                errors.append(f"{self.order_type.name} requires a positive stop_price")
        if self.order_type == OrderType.TRAILING_STOP:
            if self.trail_offset_pct <= 0 or self.trail_offset_pct >= 1:
                errors.append("trail_offset_pct must be between 0 and 1")
        if self.time_in_force == TimeInForce.GTD and self.expire_at_ns <= 0:
            errors.append("GTD orders require a positive expire_at_ns")
        if self.order_type == OrderType.ICEBERG:
            if self.iceberg_show_qty <= 0 or self.iceberg_show_qty >= self.quantity:
                errors.append("iceberg_show_qty must be > 0 and < quantity")
        return errors


# ---------------------------------------------------------------------------
# Order book
# ---------------------------------------------------------------------------

class OrderBook:
    """In-memory order book for managing pending and active orders.

    Provides submit, cancel, fill, and query operations.  Used by both
    live execution adapters and the backtest fill simulator.
    """

    def __init__(self) -> None:
        self._orders: Dict[str, Order] = {}
        self._active_ids: List[str] = []

    def submit(self, order: Order) -> List[str]:
        """Submit an order to the book.

        Validates the order before accepting it.

        Args:
            order: The order to submit.

        Returns:
            List of validation errors (empty on success).
        """
        errors = order.validate()
        if errors:
            order.status = OrderStatus.REJECTED
            return errors
        order.status = OrderStatus.SUBMITTED
        order.updated_at_ns = time.time_ns()
        self._orders[order.order_id] = order
        self._active_ids.append(order.order_id)
        return []

    def cancel(self, order_id: str) -> bool:
        """Cancel an order by ID.

        Args:
            order_id: The order to cancel.

        Returns:
            ``True`` if the order was found and cancelled.
        """
        order = self._orders.get(order_id)
        if order is None:
            return False
        order.cancel()
        if order_id in self._active_ids:
            self._active_ids.remove(order_id)
        return True

    def try_fill(
        self,
        order_id: str,
        fill_price: float,
        fill_qty: float,
        fees_usd: float = 0.0,
        tx_signature: str = "",
    ) -> Optional[Fill]:
        """Attempt to fill an order.

        Args:
            order_id: The order to fill.
            fill_price: Execution price.
            fill_qty: Quantity to fill.
            fees_usd: Transaction fees.
            tx_signature: On-chain tx hash.

        Returns:
            The ``Fill`` if successful, ``None`` otherwise.
        """
        order = self._orders.get(order_id)
        if order is None or not order.is_active:
            return None
        qty = min(fill_qty, order.remaining_quantity)
        if qty <= 0:
            return None
        fill = order.record_fill(fill_price, qty, fees_usd, tx_signature)
        if not order.is_active and order_id in self._active_ids:
            self._active_ids.remove(order_id)
        return fill

    def get(self, order_id: str) -> Optional[Order]:
        """Look up an order by ID."""
        return self._orders.get(order_id)

    @property
    def active_orders(self) -> List[Order]:
        """All orders that can still receive fills."""
        return [self._orders[oid] for oid in self._active_ids if oid in self._orders]

    @property
    def all_orders(self) -> List[Order]:
        """All orders (active and inactive)."""
        return list(self._orders.values())

    def cancel_all(self) -> int:
        """Cancel all active orders.

        Returns:
            Number of orders cancelled.
        """
        count = 0
        for oid in list(self._active_ids):
            if self.cancel(oid):
                count += 1
        return count


# ---------------------------------------------------------------------------
# Fill simulator (for backtesting)
# ---------------------------------------------------------------------------

class FillSimulator:
    """Simulates order fills against a price stream for backtesting.

    Processes active orders in an ``OrderBook`` against incoming prices,
    filling market orders immediately and limit/stop orders when their
    conditions are met.

    Args:
        book: The order book to process.
        slippage_bps: Default slippage to apply to market fills.
        fee_bps: Fee rate in basis points.
    """

    def __init__(
        self,
        book: OrderBook,
        slippage_bps: int = 10,
        fee_bps: int = 30,
    ) -> None:
        self.book = book
        self.slippage_bps = slippage_bps
        self.fee_bps = fee_bps
        self._trailing_peaks: Dict[str, float] = {}

    def on_price(self, token: str, price: float) -> List[Fill]:
        """Process a price tick and fill any triggered orders.

        Args:
            token: Token that received a price update.
            price: The new price.

        Returns:
            List of fills generated by this tick.
        """
        fills: List[Fill] = []
        for order in self.book.active_orders:
            if order.token != token:
                continue
            fill = self._try_match(order, price)
            if fill is not None:
                fills.append(fill)
        return fills

    def _try_match(self, order: Order, price: float) -> Optional[Fill]:
        """Check if an order should fill at the given price."""
        slip = price * (self.slippage_bps / 10_000)
        fee_rate = self.fee_bps / 10_000

        if order.order_type == OrderType.MARKET:
            fill_price = price + slip if order.side == OrderSide.BUY else price - slip
            fees = fill_price * order.remaining_quantity * fee_rate
            return self.book.try_fill(
                order.order_id, fill_price, order.remaining_quantity, fees,
            )

        if order.order_type == OrderType.LIMIT:
            if order.limit_price is None:
                return None
            if order.side == OrderSide.BUY and price <= order.limit_price:
                fees = order.limit_price * order.remaining_quantity * fee_rate
                return self.book.try_fill(
                    order.order_id, order.limit_price, order.remaining_quantity, fees,
                )
            if order.side == OrderSide.SELL and price >= order.limit_price:
                fees = order.limit_price * order.remaining_quantity * fee_rate
                return self.book.try_fill(
                    order.order_id, order.limit_price, order.remaining_quantity, fees,
                )

        if order.order_type == OrderType.STOP:
            if order.stop_price is None:
                return None
            triggered = (
                (order.side == OrderSide.SELL and price <= order.stop_price)
                or (order.side == OrderSide.BUY and price >= order.stop_price)
            )
            if triggered:
                fill_price = price + slip if order.side == OrderSide.BUY else price - slip
                fees = fill_price * order.remaining_quantity * fee_rate
                return self.book.try_fill(
                    order.order_id, fill_price, order.remaining_quantity, fees,
                )

        if order.order_type == OrderType.TRAILING_STOP:
            key = order.order_id
            if key not in self._trailing_peaks:
                self._trailing_peaks[key] = price
            if order.side == OrderSide.SELL:
                self._trailing_peaks[key] = max(self._trailing_peaks[key], price)
                trail_price = self._trailing_peaks[key] * (1 - order.trail_offset_pct)
                if price <= trail_price:
                    fill_price = price - slip
                    fees = fill_price * order.remaining_quantity * fee_rate
                    del self._trailing_peaks[key]
                    return self.book.try_fill(
                        order.order_id, fill_price, order.remaining_quantity, fees,
                    )
            else:
                self._trailing_peaks[key] = min(self._trailing_peaks[key], price)
                trail_price = self._trailing_peaks[key] * (1 + order.trail_offset_pct)
                if price >= trail_price:
                    fill_price = price + slip
                    fees = fill_price * order.remaining_quantity * fee_rate
                    del self._trailing_peaks[key]
                    return self.book.try_fill(
                        order.order_id, fill_price, order.remaining_quantity, fees,
                    )

        return None
