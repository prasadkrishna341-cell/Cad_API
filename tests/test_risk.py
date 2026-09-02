from datetime import datetime

import pytest

from kitealgo.config import IST
from kitealgo.models import Fill, Side, Signal
from kitealgo.portfolio import Portfolio
from kitealgo.risk import RiskManager

# A Tuesday, inside the 09:20-15:00 entry window.
DURING = datetime(2026, 9, 1, 10, 0, tzinfo=IST)
PRE_OPEN = datetime(2026, 9, 1, 9, 0, tzinfo=IST)
AFTER_SQUAREOFF = datetime(2026, 9, 1, 15, 20, tzinfo=IST)
SATURDAY = datetime(2026, 9, 5, 10, 0, tzinfo=IST)


@pytest.fixture
def risk(settings):
    manager = RiskManager(settings)
    manager.start_session(DURING.date())
    return manager


@pytest.fixture
def portfolio(settings):
    return Portfolio(settings.risk.capital)


def test_sizing_from_stop_distance(risk, infy):
    # capital 100k, 1% risk = 1000; stop 10 away -> 100 shares
    quantity, _ = risk.size_position(entry_price=100.0, stop_loss=90.0)
    assert quantity == 100


def test_sizing_capped_by_max_position_value(risk, infy):
    # A tight stop would allow a huge position; the notional cap binds first.
    quantity, basis = risk.size_position(entry_price=100.0, stop_loss=99.9)
    assert quantity == 250          # 25% of 100k / 100
    assert "max position value" in basis


def test_sizing_without_stop_uses_notional_cap(risk):
    quantity, basis = risk.size_position(entry_price=100.0, stop_loss=None)
    assert quantity == 250
    assert "no stop loss" in basis


def test_sizing_rounds_down_to_whole_lots(risk, nifty_fut):
    # 25,000 notional / 100 = 250 -> 5 whole lots of 50
    quantity, _ = risk.size_position(100.0, None, lot_size=nifty_fut.lot_size)
    assert quantity == 250
    assert quantity % nifty_fut.lot_size == 0


def test_sizing_returns_zero_when_a_lot_is_unaffordable(risk, nifty_fut):
    quantity, basis = risk.size_position(entry_price=25_000.0, stop_loss=None,
                                         lot_size=nifty_fut.lot_size)
    assert quantity == 0
    assert "too small" in basis


def test_entry_approved_inside_window(risk, portfolio, infy):
    decision = risk.evaluate_entry(
        Signal(infy, Side.BUY, stop_loss=90.0), portfolio, 100.0, DURING
    )
    assert decision.approved and decision.quantity == 100


def test_entry_blocked_before_window(risk, portfolio, infy):
    decision = risk.evaluate_entry(Signal(infy, Side.BUY), portfolio, 100.0, PRE_OPEN)
    assert not decision and "outside entry window" in decision.reason


def test_entry_blocked_on_weekend(risk, portfolio, infy):
    decision = risk.evaluate_entry(Signal(infy, Side.BUY), portfolio, 100.0, SATURDAY)
    assert not decision


def test_no_duplicate_position_in_same_instrument(risk, portfolio, infy):
    portfolio.apply_fill(Fill("1", infy, Side.BUY, 10, 100.0, DURING))
    decision = risk.evaluate_entry(Signal(infy, Side.BUY), portfolio, 100.0, DURING)
    assert not decision and "already holding" in decision.reason


def test_max_open_positions_enforced(risk, portfolio, infy, nifty_fut, settings):
    from kitealgo.models import Instrument
    for i in range(settings.risk.max_open_positions):
        other = Instrument(9000 + i, f"SYM{i}", "NSE")
        portfolio.apply_fill(Fill(str(i), other, Side.BUY, 1, 100.0, DURING))
    decision = risk.evaluate_entry(Signal(infy, Side.BUY), portfolio, 100.0, DURING)
    assert not decision and "max open positions" in decision.reason


def test_max_trades_per_day_enforced(risk, portfolio, infy, settings):
    portfolio.trades_today = settings.risk.max_trades_per_day
    decision = risk.evaluate_entry(Signal(infy, Side.BUY), portfolio, 100.0, DURING)
    assert not decision and "max trades per day" in decision.reason


def test_daily_loss_kill_switch_trips_and_latches(risk, portfolio, infy, settings):
    position = portfolio.position(infy)
    position.realised_pnl = -(settings.risk.max_loss_amount + 1)

    decision = risk.evaluate_entry(Signal(infy, Side.BUY), portfolio, 100.0, DURING)
    assert not decision and "halted" in decision.reason
    assert risk.halted

    # Even if PnL recovers, the halt stands until the next session.
    position.realised_pnl = 0.0
    assert not risk.evaluate_entry(Signal(infy, Side.BUY), portfolio, 100.0, DURING)

    risk.start_session(DURING.date())
    assert not risk.halted


def test_stop_on_wrong_side_is_rejected(risk, portfolio, infy):
    long_bad = risk.evaluate_entry(
        Signal(infy, Side.BUY, stop_loss=110.0), portfolio, 100.0, DURING
    )
    assert not long_bad and "at or above entry" in long_bad.reason

    short_bad = risk.evaluate_entry(
        Signal(infy, Side.SELL, stop_loss=90.0), portfolio, 100.0, DURING
    )
    assert not short_bad and "at or below entry" in short_bad.reason


def test_strategy_quantity_still_capped(risk, portfolio, infy):
    decision = risk.evaluate_entry(
        Signal(infy, Side.BUY, quantity=10_000), portfolio, 100.0, DURING
    )
    assert not decision and "exceeds max position value" in decision.reason


def test_should_exit_on_stop_target_and_squareoff(risk, portfolio, infy):
    position = portfolio.position(infy)
    position.apply_fill(Fill("1", infy, Side.BUY, 10, 100.0, DURING))
    position.stop_loss, position.target = 95.0, 110.0

    assert risk.should_exit(position, 100.0, DURING) is None
    assert "stop loss" in risk.should_exit(position, 94.0, DURING)
    assert "target" in risk.should_exit(position, 111.0, DURING)
    assert "square-off" in risk.should_exit(position, 100.0, AFTER_SQUAREOFF)


def test_should_exit_respects_short_direction(risk, portfolio, infy):
    position = portfolio.position(infy)
    position.apply_fill(Fill("1", infy, Side.SELL, 10, 100.0, DURING))
    position.stop_loss, position.target = 105.0, 90.0
    assert "stop loss" in risk.should_exit(position, 106.0, DURING)
    assert "target" in risk.should_exit(position, 89.0, DURING)
    assert risk.should_exit(position, 100.0, DURING) is None
