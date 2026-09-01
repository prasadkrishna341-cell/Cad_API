"""The live broker. Every call here moves real money.

Two guards sit in front of order placement:

* the client is only constructed from a `Settings` whose `is_live` is True
  (mode selected *and* explicitly confirmed), and
* `place_order` refuses anything the risk manager has not already approved,
  because the engine passes orders through risk first.

Kite's REST errors are translated into `BrokerError` so the engine can treat a
rejection the same way regardless of adapter.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from ..config import IST, ConfigError, Settings
from ..models import Fill, Instrument, Order, OrderStatus, Side
from ..ratelimit import ORDER_LIMITER, QUOTE_LIMITER
from .base import Broker, BrokerError

log = logging.getLogger(__name__)

_KITE_STATUS = {
    "COMPLETE": OrderStatus.COMPLETE,
    "CANCELLED": OrderStatus.CANCELLED,
    "REJECTED": OrderStatus.REJECTED,
    "OPEN": OrderStatus.OPEN,
    "TRIGGER PENDING": OrderStatus.OPEN,
    "PUT ORDER REQ RECEIVED": OrderStatus.PENDING,
    "VALIDATION PENDING": OrderStatus.PENDING,
    "OPEN PENDING": OrderStatus.PENDING,
}


class KiteBroker(Broker):
    mode = "live"

    def __init__(self, kite, settings: Settings) -> None:
        if not settings.is_live:
            raise ConfigError(
                "Refusing to build a live broker: set KITE_TRADING_MODE=live and "
                "KITE_LIVE_CONFIRM=I_UNDERSTAND_THE_RISK to trade real money."
            )
        self.kite = kite
        self.settings = settings
        self._fills: list[Fill] = []

    # -- helpers ----------------------------------------------------------
    @staticmethod
    def _wrap(exc: Exception, action: str) -> BrokerError:
        return BrokerError(f"{action} failed: {type(exc).__name__}: {exc}")

    @staticmethod
    def _map_status(raw: Optional[str]) -> OrderStatus:
        return _KITE_STATUS.get((raw or "").upper(), OrderStatus.PENDING)

    # -- Broker interface -------------------------------------------------
    def place_order(self, order: Order) -> Order:
        params = order.to_kite_params()
        log.warning("LIVE ORDER -> %s", params)
        ORDER_LIMITER.acquire()
        try:
            order_id = self.kite.place_order(**params)
        except Exception as exc:
            order.status = OrderStatus.REJECTED
            order.status_message = str(exc)
            raise self._wrap(exc, f"place_order {order.instrument.key}") from exc

        order.order_id = str(order_id)
        order.status = OrderStatus.PENDING
        order.created_at = datetime.now(IST)
        log.info("Live order %s submitted for %s", order.order_id, order.instrument.key)
        return order

    def sync_order(self, order: Order) -> Order:
        """Refresh one order's status from the order book."""
        for row in self.orders():
            if str(row.get("order_id")) == str(order.order_id):
                order.status = self._map_status(row.get("status"))
                order.filled_quantity = int(row.get("filled_quantity") or 0)
                order.average_price = float(row.get("average_price") or 0.0)
                order.status_message = row.get("status_message") or ""
                if order.is_complete and order.average_price:
                    self._record_fill(order, row)
                break
        return order

    def _record_fill(self, order: Order, row: dict) -> None:
        if any(f.order_id == order.order_id for f in self._fills):
            return
        stamp = row.get("exchange_timestamp") or row.get("order_timestamp")
        if not isinstance(stamp, datetime):
            stamp = datetime.now(IST)
        self._fills.append(
            Fill(
                order_id=order.order_id,
                instrument=order.instrument,
                side=order.side,
                quantity=order.filled_quantity or order.quantity,
                price=order.average_price,
                timestamp=stamp,
                tag=order.tag,
            )
        )

    def cancel_order(self, order: Order) -> Order:
        ORDER_LIMITER.acquire()
        try:
            self.kite.cancel_order(variety=order.variety.value, order_id=order.order_id)
        except Exception as exc:
            raise self._wrap(exc, f"cancel_order {order.order_id}") from exc
        order.status = OrderStatus.CANCELLED
        return order

    def modify_order(self, order: Order, **changes) -> Order:
        ORDER_LIMITER.acquire()
        try:
            self.kite.modify_order(
                variety=order.variety.value, order_id=order.order_id, **changes
            )
        except Exception as exc:
            raise self._wrap(exc, f"modify_order {order.order_id}") from exc
        for key, value in changes.items():
            if hasattr(order, key):
                setattr(order, key, value)
        return order

    def ltp(self, instruments: list[Instrument]) -> dict[int, float]:
        if not instruments:
            return {}
        keys = [i.key for i in instruments]
        QUOTE_LIMITER.acquire()
        try:
            quotes = self.kite.ltp(keys)
        except Exception as exc:
            raise self._wrap(exc, "ltp") from exc
        by_key = {i.key: i.instrument_token for i in instruments}
        return {
            by_key[key]: float(payload["last_price"])
            for key, payload in quotes.items()
            if key in by_key and payload.get("last_price") is not None
        }

    def orders(self) -> list[dict]:
        ORDER_LIMITER.acquire()
        try:
            return self.kite.orders()
        except Exception as exc:
            raise self._wrap(exc, "orders") from exc

    def positions(self) -> list[dict]:
        ORDER_LIMITER.acquire()
        try:
            return self.kite.positions().get("net", [])
        except Exception as exc:
            raise self._wrap(exc, "positions") from exc

    def holdings(self) -> list[dict]:
        ORDER_LIMITER.acquire()
        try:
            return self.kite.holdings()
        except Exception as exc:
            raise self._wrap(exc, "holdings") from exc

    def fills(self) -> list[Fill]:
        return list(self._fills)

    def margin_available(self) -> Optional[float]:
        try:
            margins = self.kite.margins(segment="equity")
        except Exception as exc:
            log.warning("Could not read margins: %s", exc)
            return None
        return float(margins.get("available", {}).get("live_balance", 0.0))

    def square_off_all(self, tag: str = "squareoff") -> list[Order]:
        """Flatten every open intraday position. Used by the square-off timer."""
        placed: list[Order] = []
        for row in self.positions():
            quantity = int(row.get("quantity") or 0)
            if quantity == 0:
                continue
            instrument = Instrument(
                instrument_token=int(row.get("instrument_token") or 0),
                tradingsymbol=row.get("tradingsymbol", ""),
                exchange=row.get("exchange", "NSE"),
            )
            from ..models import Product
            order = Order(
                instrument=instrument,
                side=Side.SELL if quantity > 0 else Side.BUY,
                quantity=abs(quantity),
                product=Product(row.get("product", self.settings.default_product)),
                tag=tag,
            )
            try:
                placed.append(self.place_order(order))
            except BrokerError as exc:
                log.error("Square-off failed for %s: %s", instrument.key, exc)
        return placed
