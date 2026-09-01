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
