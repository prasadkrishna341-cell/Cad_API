"""The live trading loop.

Wires together: tick stream -> bar aggregator -> strategy -> risk manager ->
broker, with the portfolio and SQLite store recording everything.

Deliberate design points:

* Strategy code runs on the main thread, never inside the websocket callback.
* Every entry goes through `RiskManager.evaluate_entry`; the strategy has no
  route to the broker that skips it.
* Stops and targets are enforced *by the engine* on every tick, not left to
  resting orders alone — a websocket drop should not leave a naked position.
* Square-off is time-driven and unconditional, so nothing is carried overnight
  by accident on an MIS product.
* Ctrl-C flattens open positions before exiting rather than abandoning them.
"""

from __future__ import annotations

import logging
import signal
import time
from datetime import date, datetime
from typing import Iterable, Optional

from .broker.base import Broker, BrokerError
from .clock import SessionClock, as_ist, now_ist
from .config import Settings
from .data.bars import BarAggregator
from .models import (
    Bar, Fill, Instrument, Order, OrderStatus, OrderType, Product, Side, Signal, Tick,
)
from .portfolio import Portfolio
from .risk import RiskManager
from .store import Store

log = logging.getLogger(__name__)


class TradingEngine:
    def __init__(
        self,
        strategy,
        broker: Broker,
        settings: Settings,
        instruments: Optional[Iterable[Instrument]] = None,
        bar_interval_seconds: int = 300,
        store: Optional[Store] = None,
        square_off_on_exit: bool = True,
    ) -> None:
        self.strategy = strategy
        self.broker = broker
        self.settings = settings
        self.instruments = list(instruments or strategy.instruments)
        self.bar_interval_seconds = bar_interval_seconds
        self.square_off_on_exit = square_off_on_exit

        self.portfolio = Portfolio(settings.risk.capital)
        self.risk = RiskManager(settings)
        self.clock = SessionClock(settings)
        self.store = store or Store(settings.db_file)
        self.aggregator = BarAggregator(bar_interval_seconds, on_bar=self._on_bar)

        self._by_token = {i.instrument_token: i for i in self.instruments}
        self._running = False
        self._squared_off = False
        self._session: Optional[date] = None
        self._last_prices: dict[int, float] = {}
        self._last_persist = 0.0
        # Live orders come back PENDING; they are polled until they reach a
        # terminal state so the portfolio reflects real fills.
        self._pending_orders: list[tuple[Order, Optional[Signal], str]] = []
        self._last_sync = 0.0

    # -- lifecycle --------------------------------------------------------
    def _install_signal_handlers(self) -> None:
        def handler(signum, frame):
            log.warning("Received signal %s — shutting down.", signum)
            self._running = False
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, handler)
            except ValueError:
                pass  # not on the main thread (e.g. under a test runner)

    def _start_session(self, session_date: date) -> None:
        self._session = session_date
        self._squared_off = False
        self.portfolio.reset_day(session_date)
        self.risk.start_session(session_date)
        self.strategy.on_day_start(session_date)
        log.info("Session %s started | mode=%s | strategy=%s",
                 session_date, self.broker.mode.upper(), self.strategy.name)

    def run(self, tick_stream=None, max_runtime_seconds: Optional[float] = None) -> None:
        """Main loop. Blocks until the session ends or Ctrl-C."""
        self._install_signal_handlers()
        self._running = True
        started = time.monotonic()
        self._start_session(self.clock.session_date())

        banner = "LIVE — REAL ORDERS" if self.broker.is_live else "PAPER — simulated"
        log.warning("=" * 60)
        log.warning("  %s | capital %s | max daily loss %s",
                    banner, f"{self.settings.risk.capital:,.0f}",
                    f"{self.settings.risk.max_loss_amount:,.0f}")
        log.warning("  instruments: %s", ", ".join(i.tradingsymbol for i in self.instruments))
        log.warning("=" * 60)

        if tick_stream is None:
            tick_stream = self._build_tick_stream()

        try:
            if not tick_stream.start():
                raise RuntimeError("Websocket did not connect — check the access token.")
            while self._running:
                if max_runtime_seconds and time.monotonic() - started > max_runtime_seconds:
                    log.info("Reached max runtime; stopping.")
                    break
                ticks = tick_stream.drain()
                if not ticks:
                    tick = tick_stream.get(timeout=1.0)
                    ticks = [tick] if tick else []
                for tick in ticks:
                    self.on_tick(tick)
                self._housekeeping()
        finally:
            self.shutdown(tick_stream)

    def _build_tick_stream(self):
        from .auth import get_access_token
        from .data.stream import TickStream

        token = get_access_token(self.settings, allow_interactive=False)
        return TickStream(self.settings, token, self.instruments,
                          on_order_update=self._on_order_update)

    def shutdown(self, tick_stream=None) -> None:
        self._running = False
        if self.square_off_on_exit and self.portfolio.open_count:
            log.warning("Flattening %d open position(s) before exit.", self.portfolio.open_count)
            self.square_off("shutdown")
        if tick_stream is not None:
            tick_stream.stop()
        self._persist()
        log.info("Final: %s", self.portfolio.summary())

    # -- event handlers ---------------------------------------------------
    def on_tick(self, tick: Tick) -> None:
        instrument = self._by_token.get(tick.instrument_token)
        if instrument is None:
            return

        self._last_prices[tick.instrument_token] = tick.last_price
        self.portfolio.mark_to_market({tick.instrument_token: tick.last_price})

        # Roll the day over if the process has been running across midnight.
        session_day = as_ist(tick.timestamp).date()
        if self._session != session_day:
            self._start_session(session_day)

        self._enforce_exits(instrument, tick.last_price, tick.timestamp)

        # Tick-level strategies get a look in before bar aggregation.
        position = self.portfolio.positions.get(tick.instrument_token)
        for sig in self.strategy.on_tick(tick, position):
            self._handle_signal(sig, tick.last_price, tick.timestamp)

        self.aggregator.add(tick)

    def _on_bar(self, bar: Bar) -> None:
        instrument = self._by_token.get(bar.instrument_token)
        if instrument is None:
            return
        log.debug("Bar %s %s O=%.2f H=%.2f L=%.2f C=%.2f",
                  instrument.tradingsymbol, bar.timestamp, bar.open, bar.high, bar.low, bar.close)
        position = self.portfolio.positions.get(bar.instrument_token)
        for sig in self.strategy.on_bar(bar, position):
            self._handle_signal(sig, bar.close, bar.timestamp)

    def _on_order_update(self, data: dict) -> None:
        """Postback from the websocket when a live order changes state."""
        log.info("Order update: %s %s %s", data.get("order_id"),
                 data.get("tradingsymbol"), data.get("status"))

    # -- decision + execution ---------------------------------------------
    def _handle_signal(self, signal_obj: Signal, price: float, when: datetime) -> None:
        instrument = signal_obj.instrument
        position = self.portfolio.positions.get(instrument.instrument_token)

        if signal_obj.is_exit:
            if position and position.is_open:
                self._place(signal_obj, abs(position.quantity), price, when, signal_obj.reason)
            return

        decision = self.risk.evaluate_entry(signal_obj, self.portfolio, price, when)
        if not decision:
            log.info("Signal blocked for %s: %s", instrument.tradingsymbol, decision.reason)
            return

        log.info("ENTRY %s %s x%d @ ~%.2f | %s | sizing: %s",
                 signal_obj.side.value, instrument.tradingsymbol, decision.quantity,
                 price, signal_obj.reason, decision.reason)
        self._place(signal_obj, decision.quantity, price, when, signal_obj.reason)

    def _place(
        self, signal_obj: Signal, quantity: int, price: float, when: datetime, reason: str
    ) -> Optional[Order]:
        order = Order(
            instrument=signal_obj.instrument,
            side=signal_obj.side,
            quantity=quantity,
            order_type=signal_obj.order_type or OrderType.MARKET,
            price=signal_obj.limit_price,
            product=Product(self.settings.default_product),
            tag=self.strategy.name[:20],
        )
        try:
            self.broker.place_order(order)
        except BrokerError as exc:
            log.error("Order rejected for %s: %s", signal_obj.instrument.tradingsymbol, exc)
            self.store.record_order(order, self.broker.mode, self.strategy.name, reason)
            return None

        self.store.record_order(order, self.broker.mode, self.strategy.name, reason)

        # Paper fills are immediate; live fills arrive via postback/polling.
        if order.is_complete:
            self._book_fill(order, signal_obj, reason)
        elif not order.status.is_terminal:
            self._pending_orders.append((order, signal_obj, reason))
        return order

    def _book_fill(self, order: Order, signal_obj: Optional[Signal], reason: str) -> None:
        fill = next(
            (f for f in reversed(self.broker.fills()) if f.order_id == order.order_id), None
        )
        if fill is None:
            return
        charges = getattr(self.broker, "charges_for", lambda v: 0.0)(fill.value)
        realised = self.portfolio.apply_fill(fill, charges=charges)
        self.store.record_fill(fill, self.broker.mode, charges, realised, self._session)

        position = self.portfolio.position(order.instrument)
        if position.is_open and signal_obj is not None and not signal_obj.is_exit:
            position.stop_loss = signal_obj.stop_loss
            position.target = signal_obj.target
            log.info("Position open: %s x%d @ %.2f | SL %s | target %s",
                     order.instrument.tradingsymbol, position.quantity,
                     position.average_price, position.stop_loss, position.target)
        elif not position.is_open:
            log.info("Position closed: %s | %s | realised %.2f",
                     order.instrument.tradingsymbol, reason, realised)

    # -- protective logic -------------------------------------------------
    def _enforce_exits(self, instrument: Instrument, price: float, when: datetime) -> None:
        position = self.portfolio.positions.get(instrument.instrument_token)
        if not position or not position.is_open:
            return
        reason = self.risk.should_exit(position, price, when)
        if reason:
            log.warning("EXIT %s: %s", instrument.tradingsymbol, reason)
            self._place(
                Signal(instrument=instrument,
                       side=Side.SELL if position.is_long else Side.BUY,
                       quantity=abs(position.quantity), is_exit=True, reason=reason),
                abs(position.quantity), price, when, reason,
            )

    def square_off(self, reason: str = "square-off") -> None:
        """Flatten every open position at market."""
        for position in list(self.portfolio.open_positions):
            price = self._last_prices.get(
                position.instrument.instrument_token, position.last_price
            ) or position.average_price
            self._place(
                Signal(instrument=position.instrument,
                       side=Side.SELL if position.is_long else Side.BUY,
                       quantity=abs(position.quantity), is_exit=True, reason=reason),
                abs(position.quantity), price, now_ist(), reason,
            )
        self._squared_off = True

    def _sync_pending_orders(self) -> None:
        """Poll live orders until they fill, cancel or are rejected."""
        if not self._pending_orders or not hasattr(self.broker, "sync_order"):
            return
        self._last_sync = time.monotonic()
        still_pending = []
        for order, signal_obj, reason in self._pending_orders:
            try:
                self.broker.sync_order(order)
            except BrokerError as exc:
                log.warning("Could not sync order %s: %s", order.order_id, exc)
                still_pending.append((order, signal_obj, reason))
                continue

            if order.status is OrderStatus.COMPLETE:
                self._book_fill(order, signal_obj, reason)
            elif order.status.is_terminal:
                log.warning("Order %s ended %s: %s", order.order_id,
                            order.status.value, order.status_message)
            else:
                still_pending.append((order, signal_obj, reason))
        self._pending_orders = still_pending

    def _housekeeping(self) -> None:
        now = now_ist()

        # Poll live order state about once a second.
        if self._pending_orders and time.monotonic() - self._last_sync > 1.0:
            self._sync_pending_orders()

        if self.clock.should_square_off(now) and not self._squared_off:
            if self.portfolio.open_count:
                log.warning("Square-off time reached — flattening all positions.")
                self.square_off("square-off time")
            self._squared_off = True

        if self.risk.check_daily_loss(self.portfolio) and self.portfolio.open_count:
            log.error("Daily loss limit breached — flattening and standing down.")
            self.square_off("daily loss limit")

        if self.clock.is_session_over(now):
            log.info("Market closed — stopping.")
            self._running = False

        # Persist a PnL snapshot roughly every 30 seconds.
        if time.monotonic() - self._last_persist > 30:
            self._persist()

    def _persist(self) -> None:
        self._last_persist = time.monotonic()
        try:
            self.store.update_daily_pnl(
                self._session or date.today(),
                self.portfolio.realised_pnl,
                self.portfolio.unrealised_pnl,
                self.portfolio.charges,
                self.portfolio.trades_today,
                self.risk.halted,
                self.risk.halt_reason,
            )
        except Exception as exc:
            log.warning("Could not persist PnL: %s", exc)

    def status(self) -> dict:
        return {
            "mode": self.broker.mode,
            "session": str(self._session),
            "clock": self.clock.describe(),
            **self.portfolio.summary(),
            **self.risk.status(self.portfolio),
        }
