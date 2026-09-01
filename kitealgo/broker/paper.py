"""A simulated broker.

Fills happen against the last price the broker has been told about, adjusted
for slippage and charged brokerage, so paper results are not accidentally
rosier than live ones.  This is the default execution path — you have to go out
of your way to trade real money.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from ..config import IST
from ..models import Fill, Instrument, Order, OrderStatus, OrderType, Side
from .base import Broker, BrokerError

log = logging.getLogger(__name__)

# Zerodha intraday equity: 0.03% or Rs 20 per executed order, whichever is lower,
# plus statutory charges. This approximates the all-in cost per side.
DEFAULT_BROKERAGE_RATE = 0.0003
DEFAULT_BROKERAGE_CAP = 20.0
DEFAULT_SLIPPAGE_BPS = 2.0  # 0.02% — a realistic touch for liquid intraday names


class PaperBroker(Broker):
    mode = "paper"

    def __init__(
        self,
        slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
        brokerage_rate: float = DEFAULT_BROKERAGE_RATE,
        brokerage_cap: float = DEFAULT_BROKERAGE_CAP,
        starting_cash: float = 0.0,
    ) -> None:
        self.slippage_bps = slippage_bps
        self.brokerage_rate = brokerage_rate
        self.brokerage_cap = brokerage_cap
        self.starting_cash = starting_cash
        self._prices: dict[int, float] = {}
        self._orders: list[Order] = []
        self._fills: list[Fill] = []
        self._open_orders: list[Order] = []
        self.total_charges = 0.0
        self._clock: Optional[datetime] = None

    # -- test/backtest plumbing ------------------------------------------
    def set_price(self, instrument_or_token, price: float) -> None:
        token = getattr(instrument_or_token, "instrument_token", instrument_or_token)
        self._prices[int(token)] = float(price)

    def set_clock(self, now: datetime) -> None:
        """Pin 'now' so backtests stamp fills with bar time, not wall time."""
        self._clock = now

    def _now(self) -> datetime:
        return self._clock or datetime.now(IST)

    def charges_for(self, value: float) -> float:
        return min(abs(value) * self.brokerage_rate, self.brokerage_cap)

    def _fill_price(self, order: Order, market_price: float) -> float:
        """Market orders pay the spread; limit orders fill at their own price."""
        if order.order_type is OrderType.LIMIT and order.price is not None:
            return order.price
        slip = market_price * (self.slippage_bps / 10_000.0)
        return market_price + slip if order.side is Side.BUY else market_price - slip

    # -- Broker interface -------------------------------------------------
    def place_order(self, order: Order) -> Order:
        token = order.instrument.instrument_token
        market_price = self._prices.get(token)
        if market_price is None:
            order.status = OrderStatus.REJECTED
            order.status_message = f"paper broker has no price for {order.instrument.key}"
            self._orders.append(order)
            raise BrokerError(order.status_message)

        order.created_at = self._now()
        self._orders.append(order)

        # SL / SL-M rest until price crosses their trigger.
        if order.order_type in (OrderType.SL, OrderType.SLM):
            order.status = OrderStatus.OPEN
            self._open_orders.append(order)
            log.debug("paper: resting %s %s trigger=%s", order.order_type.value,
                      order.instrument.key, order.trigger_price)
            return order

        # Unmarketable limit orders rest too.
        if order.order_type is OrderType.LIMIT and order.price is not None:
            marketable = (
                (order.side is Side.BUY and order.price >= market_price)
                or (order.side is Side.SELL and order.price <= market_price)
            )
            if not marketable:
                order.status = OrderStatus.OPEN
                self._open_orders.append(order)
                return order

        self._execute(order, self._fill_price(order, market_price))
        return order

    def _execute(self, order: Order, price: float) -> Fill:
        price = order.instrument.round_to_tick(price)
        order.status = OrderStatus.COMPLETE
        order.filled_quantity = order.quantity
        order.average_price = price
        fill = Fill(
            order_id=order.order_id,
            instrument=order.instrument,
            side=order.side,
            quantity=order.quantity,
            price=price,
            timestamp=self._now(),
            tag=order.tag,
        )
        self._fills.append(fill)
        self.total_charges += self.charges_for(fill.value)
        log.info("paper fill: %s %d %s @ %.2f", order.side.value, order.quantity,
                 order.instrument.tradingsymbol, price)
        return fill

    def on_price(self, instrument_or_token, price: float) -> list[Fill]:
        """Advance the simulated market; returns fills triggered by the move."""
        self.set_price(instrument_or_token, price)
        token = int(getattr(instrument_or_token, "instrument_token", instrument_or_token))
        triggered: list[Fill] = []
        for order in list(self._open_orders):
            if order.instrument.instrument_token != token:
                continue
            if self._is_triggered(order, price):
                self._open_orders.remove(order)
                triggered.append(self._execute(order, self._fill_price(order, price)))
        return triggered

    @staticmethod
    def _is_triggered(order: Order, price: float) -> bool:
        if order.order_type in (OrderType.SL, OrderType.SLM):
            trigger = order.trigger_price or 0.0
            # A stop to sell fires when price falls to it; a stop to buy when it rises.
            return price <= trigger if order.side is Side.SELL else price >= trigger
        if order.order_type is OrderType.LIMIT and order.price is not None:
            return price <= order.price if order.side is Side.BUY else price >= order.price
        return False

    def cancel_order(self, order: Order) -> Order:
        if order in self._open_orders:
            self._open_orders.remove(order)
        if not order.status.is_terminal:
            order.status = OrderStatus.CANCELLED
        return order

    def ltp(self, instruments: list[Instrument]) -> dict[int, float]:
        return {
            i.instrument_token: self._prices[i.instrument_token]
            for i in instruments
            if i.instrument_token in self._prices
        }

    def orders(self) -> list[dict]:
        return [
            {
                "order_id": o.order_id,
                "tradingsymbol": o.instrument.tradingsymbol,
                "exchange": o.instrument.exchange,
                "transaction_type": o.side.value,
                "quantity": o.quantity,
                "status": o.status.value,
                "average_price": o.average_price,
                "order_type": o.order_type.value,
                "tag": o.tag,
            }
            for o in self._orders
        ]

    def positions(self) -> list[dict]:
        net: dict[int, dict] = {}
        for fill in self._fills:
            entry = net.setdefault(
                fill.instrument.instrument_token,
                {"tradingsymbol": fill.instrument.tradingsymbol,
                 "exchange": fill.instrument.exchange, "quantity": 0, "value": 0.0},
            )
            entry["quantity"] += fill.quantity * fill.side.sign
            entry["value"] -= fill.value * fill.side.sign
        return list(net.values())

    def fills(self) -> list[Fill]:
        return list(self._fills)

    def margin_available(self) -> Optional[float]:
        if not self.starting_cash:
            return None
        realised = sum(-f.value * f.side.sign for f in self._fills)
        return self.starting_cash + realised - self.total_charges
