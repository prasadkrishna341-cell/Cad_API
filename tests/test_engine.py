from datetime import datetime, timedelta

import pytest

from kitealgo.broker.paper import PaperBroker
from kitealgo.config import IST
from kitealgo.engine import TradingEngine
from kitealgo.models import Side, Signal, Tick
from kitealgo.store import Store
from kitealgo.strategy.base import Strategy

DURING = datetime(2026, 9, 1, 10, 0, tzinfo=IST)     # Tuesday, entries allowed
AFTER_SQUAREOFF = datetime(2026, 9, 1, 15, 20, tzinfo=IST)


class SignalOnceStrategy(Strategy):
    """Emits one entry on the first tick, then nothing."""
    name = "once"

    def __init__(self, instruments, side=Side.BUY, stop=None, target=None, **params):
        self.side, self.stop, self.target = side, stop, target
        super().__init__(instruments, **params)

    def _setup(self):
        self._fired = False

    def on_bar(self, bar, position=None):
        return []

    def on_tick(self, tick, position=None):
        if self._fired:
            return []
        self._fired = True
        return [Signal(self.instruments[0], self.side, reason="test",
                       stop_loss=self.stop, target=self.target)]


@pytest.fixture
def engine_parts(settings, infy, tmp_path):
    broker = PaperBroker(slippage_bps=0.0, starting_cash=settings.risk.capital)
    broker.set_price(infy, 100.0)
    strategy = SignalOnceStrategy([infy], stop=95.0, target=110.0)
    engine = TradingEngine(strategy, broker, settings, bar_interval_seconds=60,
                           store=Store(tmp_path / "t.db"))
    return engine, broker, strategy


def test_entry_is_sized_and_recorded(engine_parts, infy):
    engine, broker, _ = engine_parts
    engine._start_session(DURING.date())
    engine.on_tick(Tick(infy.instrument_token, 100.0, DURING))

    position = engine.portfolio.positions[infy.instrument_token]
    assert position.is_open and position.quantity == 200      # 1% risk / 5 stop distance
    assert position.stop_loss == 95.0 and position.target == 110.0
    assert engine.store.orders_for(DURING.date())


def test_stop_loss_is_enforced_on_tick(engine_parts, infy):
    engine, broker, _ = engine_parts
    engine._start_session(DURING.date())
    engine.on_tick(Tick(infy.instrument_token, 100.0, DURING))

    broker.set_price(infy, 94.0)
    engine.on_tick(Tick(infy.instrument_token, 94.0, DURING + timedelta(minutes=1)))

    assert not engine.portfolio.positions[infy.instrument_token].is_open
    assert engine.portfolio.realised_pnl < 0


def test_target_is_enforced_on_tick(engine_parts, infy):
    engine, broker, _ = engine_parts
    engine._start_session(DURING.date())
    engine.on_tick(Tick(infy.instrument_token, 100.0, DURING))

    broker.set_price(infy, 111.0)
    engine.on_tick(Tick(infy.instrument_token, 111.0, DURING + timedelta(minutes=1)))

    assert not engine.portfolio.positions[infy.instrument_token].is_open
    assert engine.portfolio.realised_pnl > 0


def test_square_off_flattens_everything(engine_parts, infy):
    engine, broker, _ = engine_parts
    engine._start_session(DURING.date())
    engine.on_tick(Tick(infy.instrument_token, 100.0, DURING))
    assert engine.portfolio.open_count == 1

    engine.square_off("test")
    assert engine.portfolio.open_count == 0


def test_entry_refused_outside_the_window(engine_parts, infy):
    engine, broker, _ = engine_parts
    engine._start_session(AFTER_SQUAREOFF.date())
    engine.on_tick(Tick(infy.instrument_token, 100.0, AFTER_SQUAREOFF))
    assert engine.portfolio.open_count == 0


def test_ticks_for_unknown_instruments_are_ignored(engine_parts):
    engine, _, _ = engine_parts
    engine._start_session(DURING.date())
    engine.on_tick(Tick(999999, 100.0, DURING))
    assert engine.portfolio.open_count == 0


def test_shutdown_squares_off_open_positions(engine_parts, infy):
    engine, broker, _ = engine_parts
    engine._start_session(DURING.date())
    engine.on_tick(Tick(infy.instrument_token, 100.0, DURING))
    assert engine.portfolio.open_count == 1

    engine.shutdown()
    assert engine.portfolio.open_count == 0


def test_status_reports_mode_and_limits(engine_parts):
    engine, _, _ = engine_parts
    engine._start_session(DURING.date())
    status = engine.status()
    assert status["mode"] == "paper"
    assert status["halted"] is False
    assert "/" in status["open_positions"]


class FakeLiveBroker(PaperBroker):
    """A broker whose orders sit PENDING until explicitly filled, like Kite's."""

    mode = "live"

    def __init__(self, price):
        super().__init__(slippage_bps=0.0)
        self._price = price
        self._queued = []

    def place_order(self, order):
        from kitealgo.models import OrderStatus
        order.order_id = f"live-{len(self._orders) + 1}"
        order.status = OrderStatus.PENDING
        self._orders.append(order)
        self._queued.append(order)
        return order

    def complete_all(self):
        """Simulate the exchange filling every resting order."""
        from datetime import datetime as dt

        from kitealgo.models import Fill, OrderStatus
        for order in self._queued:
            order.status = OrderStatus.COMPLETE
            order.filled_quantity = order.quantity
            order.average_price = self._price
            self._fills.append(Fill(order.order_id, order.instrument, order.side,
                                    order.quantity, self._price,
                                    dt(2026, 9, 1, 10, 0, tzinfo=IST), order.tag))
        self._queued = []

    def sync_order(self, order):
        return order


def test_live_pending_orders_are_polled_and_booked(settings, infy, tmp_path):
    broker = FakeLiveBroker(price=100.0)
    strategy = SignalOnceStrategy([infy], stop=95.0, target=110.0)
    engine = TradingEngine(strategy, broker, settings, store=Store(tmp_path / "t.db"))
    engine._start_session(DURING.date())

    engine.on_tick(Tick(infy.instrument_token, 100.0, DURING))
    # The order is live and pending, so nothing is in the portfolio yet.
    assert engine.portfolio.open_count == 0
    assert len(engine._pending_orders) == 1

    broker.complete_all()
    engine._sync_pending_orders()

    assert engine._pending_orders == []
    position = engine.portfolio.positions[infy.instrument_token]
    assert position.is_open and position.quantity == 200
    assert position.stop_loss == 95.0


def test_rejected_live_order_is_dropped_not_retried_forever(settings, infy, tmp_path):
    from kitealgo.models import OrderStatus

    broker = FakeLiveBroker(price=100.0)
    strategy = SignalOnceStrategy([infy], stop=95.0)
    engine = TradingEngine(strategy, broker, settings, store=Store(tmp_path / "t.db"))
    engine._start_session(DURING.date())
    engine.on_tick(Tick(infy.instrument_token, 100.0, DURING))

    engine._pending_orders[0][0].status = OrderStatus.REJECTED
    engine._sync_pending_orders()

    assert engine._pending_orders == []
    assert engine.portfolio.open_count == 0


def test_book_fill_matches_the_right_order(settings, infy, tmp_path):
    """Two orders in flight must not have their fills confused."""
    broker = FakeLiveBroker(price=100.0)
    strategy = SignalOnceStrategy([infy], stop=95.0)
    engine = TradingEngine(strategy, broker, settings, store=Store(tmp_path / "t.db"))
    engine._start_session(DURING.date())
    engine.on_tick(Tick(infy.instrument_token, 100.0, DURING))

    order, signal, reason = engine._pending_orders[0]
    broker.complete_all()
    # A later, unrelated fill must not be mistaken for this order's.
    from kitealgo.models import Fill, Side as S
    broker._fills.append(Fill("some-other-order", infy, S.SELL, 999, 50.0, DURING))

    engine._book_fill(order, signal, reason)
    assert engine.portfolio.positions[infy.instrument_token].quantity == 200


def test_orders_are_stamped_with_the_session_date_not_today(settings, infy, tmp_path):
    """Regression: records must carry the engine's session date.

    This was previously stamped with wall-clock `today`, which only looked
    correct while the test happened to run on the same calendar day.
    """
    from datetime import date

    broker = PaperBroker(slippage_bps=0.0)
    broker.set_price(infy, 100.0)
    strategy = SignalOnceStrategy([infy], stop=95.0)
    engine = TradingEngine(strategy, broker, settings, store=Store(tmp_path / "t.db"))

    # The engine takes its session date from the tick stream, so drive it with
    # a tick dated on a past session rather than setting the date by hand.
    session = DURING.date()
    assert session != date.today(), "fixture date must differ from today to be meaningful"

    engine._start_session(session)
    engine.on_tick(Tick(infy.instrument_token, 100.0, DURING))

    assert engine.store.orders_for(session), "order not filed under the session date"
    assert engine.store.fills_for(session), "fill not filed under the session date"
    assert engine.store.orders_for(date.today()) == []
