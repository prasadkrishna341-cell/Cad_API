"""Domain types shared by the broker, strategy, risk and backtest layers.

These deliberately mirror Kite's vocabulary (BUY/SELL, MIS/CNC/NRML,
MARKET/LIMIT/SL/SL-M) so translating to an actual `place_order` call is a
field-for-field mapping with no guesswork.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def opposite(self) -> "Side":
        return Side.SELL if self is Side.BUY else Side.BUY

    @property
    def sign(self) -> int:
        """+1 for BUY, -1 for SELL — handy for signed quantity maths."""
        return 1 if self is Side.BUY else -1


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    SL = "SL"
    SLM = "SL-M"


class Product(str, Enum):
    MIS = "MIS"     # intraday, broker squares off
    CNC = "CNC"     # delivery
    NRML = "NRML"   # F&O carry-forward


class Variety(str, Enum):
    REGULAR = "regular"
    AMO = "amo"
    CO = "co"
    ICEBERG = "iceberg"


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    OPEN = "OPEN"
    COMPLETE = "COMPLETE"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"

    @property
    def is_terminal(self) -> bool:
        return self in (OrderStatus.COMPLETE, OrderStatus.CANCELLED, OrderStatus.REJECTED)


@dataclass(frozen=True)
class Instrument:
    """One tradable symbol, as it appears in Kite's instrument master."""

    instrument_token: int
    tradingsymbol: str
    exchange: str
    name: str = ""
    lot_size: int = 1
    tick_size: float = 0.05
    segment: str = ""
    instrument_type: str = "EQ"
    expiry: Optional[str] = None

    @property
    def key(self) -> str:
        """The `EXCHANGE:TRADINGSYMBOL` form used by ltp()/quote()."""
        return f"{self.exchange}:{self.tradingsymbol}"

    def round_to_tick(self, price: float) -> float:
        """Snap a price to the instrument's tick size (exchanges reject others)."""
        if self.tick_size <= 0:
            return round(price, 2)
        return round(round(price / self.tick_size) * self.tick_size, 2)


@dataclass
class Tick:
    """A normalised market tick. Only the fields strategies actually use."""

    instrument_token: int
    last_price: float
    timestamp: datetime
    volume: int = 0
    oi: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Bar:
    """An OHLCV candle."""

    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int = 0
    instrument_token: int = 0

    @property
    def range(self) -> float:
        return self.high - self.low


_order_ids = itertools.count(1)


@dataclass
class Order:
    """An order request plus its lifecycle state."""

    instrument: Instrument
    side: Side
    quantity: int
    order_type: OrderType = OrderType.MARKET
    product: Product = Product.MIS
    price: Optional[float] = None
    trigger_price: Optional[float] = None
    variety: Variety = Variety.REGULAR
    tag: str = ""
    # -- lifecycle --
    order_id: str = ""
    status: OrderStatus = OrderStatus.PENDING
    filled_quantity: int = 0
    average_price: float = 0.0
    status_message: str = ""
    created_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError(f"order quantity must be positive, got {self.quantity}")
        if self.order_type in (OrderType.LIMIT, OrderType.SL) and self.price is None:
            raise ValueError(f"{self.order_type.value} order requires a price")
        if self.order_type in (OrderType.SL, OrderType.SLM) and self.trigger_price is None:
            raise ValueError(f"{self.order_type.value} order requires a trigger_price")
        if not self.order_id:
            self.order_id = f"local-{next(_order_ids)}"
        # Kite rejects tags longer than 20 characters.
        self.tag = self.tag[:20]

    @property
    def is_complete(self) -> bool:
        return self.status is OrderStatus.COMPLETE

    def to_kite_params(self) -> dict[str, Any]:
        """Exact kwargs for `KiteConnect.place_order`."""
        params: dict[str, Any] = {
            "variety": self.variety.value,
            "exchange": self.instrument.exchange,
            "tradingsymbol": self.instrument.tradingsymbol,
            "transaction_type": self.side.value,
            "quantity": int(self.quantity),
            "product": self.product.value,
            "order_type": self.order_type.value,
        }
        if self.price is not None:
            params["price"] = self.instrument.round_to_tick(self.price)
        if self.trigger_price is not None:
            params["trigger_price"] = self.instrument.round_to_tick(self.trigger_price)
        if self.tag:
            params["tag"] = self.tag
        return params


@dataclass
class Fill:
    """A completed execution."""

    order_id: str
    instrument: Instrument
    side: Side
    quantity: int
    price: float
    timestamp: datetime
    tag: str = ""

    @property
    def value(self) -> float:
        return self.quantity * self.price


@dataclass
class Position:
    """Net position in one instrument, with running average price and PnL."""

    instrument: Instrument
    quantity: int = 0          # signed: positive long, negative short
    average_price: float = 0.0
    realised_pnl: float = 0.0
    last_price: float = 0.0
    stop_loss: Optional[float] = None
    target: Optional[float] = None
    opened_at: Optional[datetime] = None

    @property
    def is_open(self) -> bool:
        return self.quantity != 0

    @property
    def is_long(self) -> bool:
        return self.quantity > 0

    @property
    def is_short(self) -> bool:
        return self.quantity < 0

    @property
    def exposure(self) -> float:
        """Absolute notional value at the last traded price."""
        return abs(self.quantity) * (self.last_price or self.average_price)

    @property
    def unrealised_pnl(self) -> float:
        if not self.is_open or not self.last_price:
            return 0.0
        return (self.last_price - self.average_price) * self.quantity

    @property
    def total_pnl(self) -> float:
        return self.realised_pnl + self.unrealised_pnl

    def apply_fill(self, fill: Fill) -> float:
        """Fold a fill into this position, returning the realised PnL it booked.

        Handles the three cases that matter: adding to a position (weighted
        average price), reducing it (books PnL), and flipping through zero
        (books PnL on the closed part, re-opens the remainder at fill price).
        """
        signed = fill.quantity * fill.side.sign
        old_qty = self.quantity
        new_qty = old_qty + signed
        realised = 0.0

        if old_qty == 0 or (old_qty > 0) == (signed > 0):
            # Opening, or adding in the same direction -> weighted average.
            total_cost = abs(old_qty) * self.average_price + abs(signed) * fill.price
            self.average_price = total_cost / abs(new_qty) if new_qty else 0.0
        else:
            # Reducing or flipping.
            closed = min(abs(old_qty), abs(signed))
            direction = 1 if old_qty > 0 else -1
            realised = (fill.price - self.average_price) * closed * direction
            self.realised_pnl += realised
            if new_qty == 0:
                self.average_price = 0.0
                self.stop_loss = None
                self.target = None
            elif (new_qty > 0) != (old_qty > 0):
                # Flipped through zero: remainder opens at the fill price.
                self.average_price = fill.price

        self.quantity = new_qty
        self.last_price = fill.price
        if old_qty == 0 and new_qty != 0:
            self.opened_at = fill.timestamp
        return realised


@dataclass
class Signal:
    """What a strategy emits. The engine, not the strategy, decides size."""

    instrument: Instrument
    side: Side
    reason: str = ""
    order_type: OrderType = OrderType.MARKET
    limit_price: Optional[float] = None
    stop_loss: Optional[float] = None
    target: Optional[float] = None
    quantity: Optional[int] = None   # None -> let the risk manager size it
    is_exit: bool = False
    timestamp: Optional[datetime] = None
