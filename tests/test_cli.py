"""End-to-end CLI tests against a stubbed Kite client (no network)."""

from datetime import datetime, timedelta

import pytest

from kitealgo import cli
from kitealgo.config import IST

INSTRUMENT_ROWS = [
    {"instrument_token": 408065, "tradingsymbol": "INFY", "exchange": "NSE",
     "name": "INFOSYS", "lot_size": 1, "tick_size": 0.05,
     "instrument_type": "EQ", "segment": "NSE", "expiry": ""},
]


class FakeKite:
    """Just enough of KiteConnect for the CLI's backtest and instruments paths."""

    def __init__(self, candles=None):
        self._candles = candles or []

    def instruments(self, exchange=None):
        return INSTRUMENT_ROWS

    def historical_data(self, instrument_token, from_date, to_date, interval, **kwargs):
        return [c for c in self._candles if from_date <= c["date"].date() <= to_date]


def make_candles(days=3, bars_per_day=40):
    """A zig-zag series, guaranteed to produce EMA crossings."""
    out = []
    price = 100.0
    for day in range(days):
        start = datetime(2026, 8, 3 + day, 9, 15, tzinfo=IST)   # Mon..Wed
        for i in range(bars_per_day):
            price += 2.0 if (i // 7) % 2 == 0 else -2.0
            out.append({
                "date": start + timedelta(minutes=5 * i),
                "open": price, "high": price + 1, "low": price - 1,
                "close": price, "volume": 1000,
            })
    return out


@pytest.fixture
def stub_kite(monkeypatch):
    kite = FakeKite(make_candles())
    monkeypatch.setattr("kitealgo.auth.build_kite_client", lambda *a, **k: kite)
    monkeypatch.setattr("kitealgo.cli.Settings", cli.Settings)
    return kite


def test_parse_params_coerces_types():
    params = cli._parse_params(["fast_period=9", "atr_multiple=1.5",
                               "allow_short=true", "label=abc"])
    assert params == {"fast_period": 9, "atr_multiple": 1.5,
                      "allow_short": True, "label": "abc"}


def test_parse_params_rejects_malformed_pairs():
    with pytest.raises(SystemExit):
        cli._parse_params(["justakey"])


def test_status_runs_against_an_empty_database(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("KITE_STATE_DIR", str(tmp_path))
    assert cli.main(["status"]) == 0
    assert "PAPER" in capsys.readouterr().out


def test_backtest_end_to_end(tmp_path, monkeypatch, capsys, stub_kite):
    monkeypatch.setenv("KITE_STATE_DIR", str(tmp_path))
    exit_code = cli.main([
        "backtest", "--symbols", "INFY", "--days", "30", "--interval", "5minute",
        "--strategy", "ema_crossover",
        "--param", "fast_period=3", "--param", "slow_period=5", "--param", "atr_period=3",
        "--trades",
    ])
    output = capsys.readouterr().out
    assert exit_code == 0
    assert "BACKTEST RESULT" in output
    assert "Net PnL" in output
    assert "Max drawdown" in output


def test_backtest_reports_when_no_candles_come_back(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("KITE_STATE_DIR", str(tmp_path))
    monkeypatch.setattr("kitealgo.auth.build_kite_client", lambda *a, **k: FakeKite([]))
    assert cli.main(["backtest", "--symbols", "INFY", "--days", "5"]) == 1
    assert "No candles returned" in capsys.readouterr().out


def test_instruments_search(tmp_path, monkeypatch, capsys, stub_kite):
    monkeypatch.setenv("KITE_STATE_DIR", str(tmp_path))
    assert cli.main(["instruments", "--refresh", "--search", "INFY"]) == 0
    assert "INFY" in capsys.readouterr().out


def test_unknown_strategy_is_reported(tmp_path, monkeypatch, stub_kite):
    monkeypatch.setenv("KITE_STATE_DIR", str(tmp_path))
    assert cli.main(["backtest", "--symbols", "INFY", "--strategy", "nope"]) == 1


def test_run_refuses_unconfirmed_live_mode(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("KITE_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("KITE_TRADING_MODE", "live")
    monkeypatch.delenv("KITE_LIVE_CONFIRM", raising=False)
    assert cli.main(["run", "--symbols", "INFY"]) == 2
