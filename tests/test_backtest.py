from datetime import datetime, timedelta

import pytest

from kitealgo.backtest import Backtester, BacktestResult, Trade
from kitealgo.config import IST
from kitealgo.models import Bar, Side, Signal
from kitealgo.strategy.base import Strategy

OPEN = datetime(2026, 9, 1, 9, 25, tzinfo=IST)   # Tuesday, inside entry window


class BuyOnceStrategy(Strategy):
    """Buys on the first bar it sees, with a fixed stop and target."""
    name = "buy_once"

    def __init__(self, instruments, stop=None, target=None, **params):
        self.stop, self.target = stop, target
        super().__init__(instruments, **params)

    def _setup(self):
        self._done = False

    def on_bar(self, bar, position=None):
        if self._done:
            return []
        self._done = True
        return [Signal(self.instruments[0], Side.BUY,
                       reason="test entry", stop_loss=self.stop, target=self.target)]


def make_bars(prices, token, start=OPEN, step=5):
    return [
        Bar(start + timedelta(minutes=step * i), p, p + 1, p - 1, p, 1000, token)
        for i, p in enumerate(prices)
    ]


def test_entry_fills_on_the_next_bar_not_the_signal_bar(settings, infy):
    strategy = BuyOnceStrategy([infy])
    bars = make_bars([100, 150, 150], infy.instrument_token)
    result = Backtester(strategy, settings, slippage_bps=0).run(bars)
    # Signal is raised on bar 0 (close 100) but must fill at bar 1's OPEN (150),
    # never at 100 — that would be trading on unavailable information.
    assert result.trades[0].entry_price == pytest.approx(150.0)


def test_stop_is_assumed_hit_before_target_in_the_same_bar(settings, infy):
    strategy = BuyOnceStrategy([infy], stop=95.0, target=105.0)
    token = infy.instrument_token
    bars = [
        Bar(OPEN, 100, 101, 99, 100, 1000, token),
        # This bar's range spans both the stop and the target.
        Bar(OPEN + timedelta(minutes=5), 100, 106, 94, 100, 1000, token),
        Bar(OPEN + timedelta(minutes=10), 100, 101, 99, 100, 1000, token),
    ]
    result = Backtester(strategy, settings, slippage_bps=0).run(bars)
    assert result.trades[0].exit_reason == "stop loss"
    assert result.trades[0].exit_price == pytest.approx(95.0)


def test_target_taken_when_stop_is_untouched(settings, infy):
    strategy = BuyOnceStrategy([infy], stop=95.0, target=105.0)
    token = infy.instrument_token
    bars = [
        Bar(OPEN, 100, 101, 99, 100, 1000, token),
        Bar(OPEN + timedelta(minutes=5), 100, 106, 99.5, 105, 1000, token),
        Bar(OPEN + timedelta(minutes=10), 105, 106, 104, 105, 1000, token),
    ]
    result = Backtester(strategy, settings, slippage_bps=0).run(bars)
    assert result.trades[0].exit_reason == "target"
    assert result.trades[0].pnl > 0


def test_charges_are_deducted_from_net_pnl(settings, infy):
    strategy = BuyOnceStrategy([infy])
    result = Backtester(strategy, settings, slippage_bps=0).run(
        make_bars([100, 100, 110], infy.instrument_token)
    )
    trade = result.trades[0]
    assert trade.charges > 0
    assert trade.net_pnl == pytest.approx(trade.pnl - trade.charges)
    assert result.net_pnl < result.gross_pnl


def test_open_position_is_closed_at_end_of_data(settings, infy):
    strategy = BuyOnceStrategy([infy])
    result = Backtester(strategy, settings, slippage_bps=0).run(
        make_bars([100, 100, 105], infy.instrument_token)
    )
    assert len(result.trades) == 1
    assert result.trades[0].exit_reason == "end of data"


def test_rejections_are_counted_not_silently_dropped(settings, infy):
    # Bars outside the entry window: every signal must be refused and tallied.
    strategy = BuyOnceStrategy([infy])
    early = datetime(2026, 9, 1, 8, 0, tzinfo=IST)
    result = Backtester(strategy, settings).run(
        make_bars([100, 100, 100], infy.instrument_token, start=early)
    )
    assert result.trades == []
    assert sum(result.rejections.values()) >= 1


def test_metrics_on_a_known_set_of_trades():
    result = BacktestResult(starting_capital=100_000)
    result.trades = [
        Trade("A", "BUY", 1, OPEN, 100, OPEN, 110, pnl=100, charges=0),
        Trade("A", "BUY", 1, OPEN, 100, OPEN, 110, pnl=200, charges=0),
        Trade("A", "BUY", 1, OPEN, 100, OPEN, 90, pnl=-150, charges=0),
    ]
    assert result.net_pnl == 150
    assert result.win_rate == pytest.approx(66.6667, abs=1e-3)
    assert result.profit_factor == pytest.approx(2.0)
    assert result.expectancy == pytest.approx(50.0)
    assert result.average_win == pytest.approx(150.0)
    assert result.average_loss == pytest.approx(-150.0)


def test_max_drawdown_measures_peak_to_trough():
    result = BacktestResult(starting_capital=1_000)
    result.equity_curve = [
        (OPEN, 1000), (OPEN, 1200), (OPEN, 900), (OPEN, 1100),
    ]
    assert result.max_drawdown == pytest.approx(300.0)   # 1200 -> 900
    assert result.max_drawdown_pct == pytest.approx(30.0)


def test_empty_result_metrics_do_not_divide_by_zero():
    result = BacktestResult(starting_capital=100_000)
    assert result.win_rate == 0.0
    assert result.expectancy == 0.0
    assert result.max_drawdown == 0.0
    assert "BACKTEST RESULT" in result.format_report()


class LoseALittleDailyStrategy(Strategy):
    """Buys the first bar of each session and closes before the session ends."""
    name = "lose_daily"

    def _setup(self):
        self._day = None
        self._bar_no = 0

    def on_day_start(self, session_date):
        self._day = None
        self._bar_no = 0

    def on_bar(self, bar, position=None):
        day = bar.timestamp.date()
        if self._day != day:
            self._day, self._bar_no = day, 0
        self._bar_no += 1

        if self._bar_no == 1:
            return [Signal(self.instruments[0], Side.BUY, reason="daily entry")]
        if self._bar_no == 5 and position is not None and position.is_open:
            return [self.exit(position, "end of day")]
        return []


def test_drawdown_accumulates_across_sessions(settings, infy):
    """Regression: the equity curve must span the whole backtest.

    The portfolio zeroes realised PnL each session so the live engine's daily
    kill switch works. The backtester built its equity curve from that, so a
    campaign that bled for weeks reported only its worst single-day dip.

    Losses here are kept small on purpose — a fixture that loses enough to trip
    the daily kill switch stops trading and never produces five losing sessions.
    """
    token = infy.instrument_token
    bars = []
    price = 1000.0
    for day in range(5):                                  # Mon-Fri
        start = datetime(2026, 9, 7 + day, 9, 25, tzinfo=IST)
        for i in range(6):
            open_ = price
            price -= 2.0
            bars.append(Bar(start + timedelta(minutes=5 * i),
                            open_, open_ + 0.5, price - 0.5, price, 1000, token))

    result = Backtester(LoseALittleDailyStrategy([infy]), settings, slippage_bps=0).run(bars)

    assert len(result.trades) == 5, "expected one round trip per session"
    assert result.net_pnl < 0, "this fixture is meant to lose money"

    # Equity must end at capital + the whole campaign's PnL, not back at capital.
    final_equity = result.equity_curve[-1][1]
    assert final_equity == pytest.approx(settings.risk.capital + result.net_pnl, abs=1.0)

    # A steady bleed means drawdown covers essentially the entire loss.
    assert result.max_drawdown >= abs(result.net_pnl) * 0.9


def test_equity_curve_is_flat_before_the_first_trade(settings, infy):
    strategy = BuyOnceStrategy([infy])
    result = Backtester(strategy, settings, slippage_bps=0).run(
        make_bars([100, 100, 100], infy.instrument_token)
    )
    assert result.equity_curve[0][1] == pytest.approx(settings.risk.capital)
