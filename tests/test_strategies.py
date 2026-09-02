from datetime import datetime, timedelta

import pytest

from kitealgo.config import IST
from kitealgo.models import Bar, Position, Side
from kitealgo.strategy import REGISTRY, get_strategy
from kitealgo.strategy.ema_crossover import EmaCrossoverStrategy
from kitealgo.strategy.orb import OpeningRangeBreakoutStrategy

OPEN = datetime(2026, 9, 1, 9, 15, tzinfo=IST)   # Tuesday


def bars_from(prices, token=1, start=OPEN, step_minutes=1):
    return [
        Bar(start + timedelta(minutes=step_minutes * i), p - 0.5, p + 0.5, p - 1, p, 100, token)
        for i, p in enumerate(prices)
    ]


def collect(strategy, bars, position=None):
    """Replay bars, tracking a simple position so exits are exercised."""
    out = []
    for bar in bars:
        for signal in strategy.on_bar(bar, position):
            out.append(signal)
            if signal.is_exit:
                position = None
            else:
                position = Position(
                    signal.instrument, quantity=10 * signal.side.sign,
                    average_price=bar.close, stop_loss=signal.stop_loss,
                )
    return out


def test_registry_exposes_both_strategies():
    assert set(REGISTRY) == {"ema_crossover", "orb"}
    assert get_strategy("ema_crossover") is EmaCrossoverStrategy
    with pytest.raises(KeyError, match="Unknown strategy"):
        get_strategy("nope")


def test_ema_rejects_inverted_periods(infy):
    with pytest.raises(ValueError, match="must be less than"):
        EmaCrossoverStrategy([infy], fast_period=21, slow_period=9)


def test_ema_enters_long_on_upward_cross(infy):
    strategy = EmaCrossoverStrategy([infy], fast_period=3, slow_period=5, atr_period=3)
    prices = [110, 109, 108, 107, 106, 105, 104, 103, 102, 101, 100, 102, 104, 106, 108]
    signals = collect(strategy, bars_from(prices, infy.instrument_token))
    entries = [s for s in signals if not s.is_exit]
    assert entries and entries[0].side is Side.BUY
    assert entries[0].stop_loss is not None and entries[0].target is not None
    assert entries[0].timestamp is not None


def test_ema_stop_is_below_entry_for_longs(infy):
    strategy = EmaCrossoverStrategy([infy], fast_period=3, slow_period=5, atr_period=3)
    prices = [110, 109, 108, 107, 106, 105, 104, 103, 102, 101, 100, 102, 104, 106, 108]
    entry = next(s for s in collect(strategy, bars_from(prices, infy.instrument_token))
                 if not s.is_exit)
    assert entry.stop_loss < 104 < entry.target


def test_ema_long_only_exits_without_reversing(infy):
    strategy = EmaCrossoverStrategy([infy], fast_period=3, slow_period=5, atr_period=3)
    prices = ([110, 109, 108, 107, 106, 105, 104, 103, 102, 101, 100]
              + [102, 104, 106, 108, 110, 112, 114, 116]
              + [114, 112, 110, 108, 106, 104])
    signals = collect(strategy, bars_from(prices, infy.instrument_token))
    assert any(s.is_exit for s in signals)
    assert all(s.side is Side.BUY for s in signals if not s.is_exit)


def test_ema_reverses_when_shorts_allowed(infy):
    strategy = EmaCrossoverStrategy([infy], fast_period=3, slow_period=5,
                                    atr_period=3, allow_short=True)
    prices = ([110, 109, 108, 107, 106, 105, 104, 103, 102, 101, 100]
              + [102, 104, 106, 108, 110, 112, 114, 116]
              + [114, 112, 110, 108, 106, 104])
    signals = collect(strategy, bars_from(prices, infy.instrument_token))
    sells = [s for s in signals if s.side is Side.SELL]
    assert any(s.is_exit for s in sells)          # closed the long
    assert any(not s.is_exit for s in sells)      # and opened a short


def test_ema_ignores_bars_for_other_instruments(infy):
    strategy = EmaCrossoverStrategy([infy], fast_period=3, slow_period=5)
    assert strategy.on_bar(Bar(OPEN, 1, 1, 1, 1, 0, 999999)) == []


def test_orb_builds_range_then_breaks_out(infy):
    strategy = OpeningRangeBreakoutStrategy([infy], range_minutes=15)
    token = infy.instrument_token
    bars = [
        Bar(OPEN, 100, 102, 99, 101, 0, token),                            # in range
        Bar(OPEN + timedelta(minutes=5), 101, 103, 100, 102, 0, token),    # in range
        Bar(OPEN + timedelta(minutes=10), 102, 103, 101, 102, 0, token),   # in range
        Bar(OPEN + timedelta(minutes=15), 102, 105, 102, 104, 0, token),   # break high
    ]
    signals = collect(strategy, bars)
    assert len(signals) == 1
    assert signals[0].side is Side.BUY
    assert signals[0].stop_loss == 99.0        # the range low


def test_orb_shorts_a_downside_break(infy):
    strategy = OpeningRangeBreakoutStrategy([infy], range_minutes=15)
    token = infy.instrument_token
    bars = [
        Bar(OPEN, 100, 102, 99, 101, 0, token),
        Bar(OPEN + timedelta(minutes=10), 101, 102, 99, 100, 0, token),
        Bar(OPEN + timedelta(minutes=20), 99, 99, 96, 97, 0, token),
    ]
    signals = collect(strategy, bars)
    assert len(signals) == 1
    assert signals[0].side is Side.SELL
    assert signals[0].stop_loss == 102.0       # the range high


def test_orb_takes_only_one_trade_per_day(infy):
    strategy = OpeningRangeBreakoutStrategy([infy], range_minutes=15)
    token = infy.instrument_token
    bars = [Bar(OPEN, 100, 102, 99, 101, 0, token)]
    bars += [
        Bar(OPEN + timedelta(minutes=15 + 5 * i), 102, 106, 102, 105, 0, token)
        for i in range(5)
    ]
    assert len(collect(strategy, bars)) == 1


def test_orb_resets_its_range_each_session(infy):
    strategy = OpeningRangeBreakoutStrategy([infy], range_minutes=15)
    token = infy.instrument_token
    day_one = [
        Bar(OPEN, 100, 102, 99, 101, 0, token),
        Bar(OPEN + timedelta(minutes=20), 102, 105, 102, 104, 0, token),
    ]
    next_open = OPEN + timedelta(days=1)
    day_two = [
        Bar(next_open, 200, 202, 199, 201, 0, token),
        Bar(next_open + timedelta(minutes=20), 202, 205, 202, 204, 0, token),
    ]
    # Day one's position is squared off overnight, as MIS would be.
    first = collect(strategy, day_one)
    second = collect(strategy, day_two, position=None)
    assert len(first) == 1 and len(second) == 1
    assert second[0].stop_loss == 199.0        # day two's own range low


def test_orb_stays_flat_inside_the_range(infy):
    strategy = OpeningRangeBreakoutStrategy([infy], range_minutes=15)
    token = infy.instrument_token
    bars = [
        Bar(OPEN, 100, 102, 99, 101, 0, token),
        Bar(OPEN + timedelta(minutes=20), 101, 101.5, 100, 100.5, 0, token),
    ]
    assert collect(strategy, bars) == []


def test_exit_on_cross_false_holds_through_the_opposite_cross(infy):
    """The crossover exit caps winners; switching it off must let them run."""
    prices = ([110, 109, 108, 107, 106, 105, 104, 103, 102, 101, 100]
              + [102, 104, 106, 108, 110, 112, 114, 116]
              + [114, 112, 110, 108, 106, 104])
    bars = bars_from(prices, infy.instrument_token)

    with_exit = EmaCrossoverStrategy([infy], fast_period=3, slow_period=5,
                                     atr_period=3, exit_on_cross=True)
    without = EmaCrossoverStrategy([infy], fast_period=3, slow_period=5,
                                   atr_period=3, exit_on_cross=False)

    assert any(s.is_exit for s in collect(with_exit, bars))
    assert not any(s.is_exit for s in collect(without, bars))


def test_exit_on_cross_false_still_enters(infy):
    """Disabling the exit must not disable the entry signal."""
    prices = [110, 109, 108, 107, 106, 105, 104, 103, 102, 101, 100, 102, 104, 106, 108]
    strategy = EmaCrossoverStrategy([infy], fast_period=3, slow_period=5,
                                    atr_period=3, exit_on_cross=False)
    entries = [s for s in collect(strategy, bars_from(prices, infy.instrument_token))
               if not s.is_exit]
    assert entries and entries[0].side is Side.BUY
    assert entries[0].stop_loss is not None and entries[0].target is not None


def test_exit_on_cross_defaults_to_true(infy):
    strategy = EmaCrossoverStrategy([infy])
    assert strategy.exit_on_cross is True
    assert strategy.params["exit_on_cross"] is True
