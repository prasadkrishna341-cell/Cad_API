from datetime import datetime

import pytest

from kitealgo.indicators import ATR, EMA, RSI, SMA, Crossover
from kitealgo.models import Bar


def test_sma_waits_for_full_window():
    sma = SMA(3)
    assert sma.update(1) is None
    assert sma.update(2) is None
    assert sma.update(3) == 2.0
    assert sma.update(4) == 3.0
    assert sma.ready


def test_ema_seeds_from_sma_then_smooths():
    ema = EMA(3)
    for value in (1, 2):
        assert ema.update(value) is None
    assert ema.update(3) == 2.0            # seed = mean(1,2,3)
    assert ema.update(4) == pytest.approx(3.0)   # 2 + (4-2)*0.5


def test_rsi_matches_wilder_reference():
    """Cross-checked against the textbook Wilder series."""
    prices = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42,
              45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28, 46.00]
    rsi = RSI(14)
    values = [v for p in prices if (v := rsi.update(p)) is not None]
    assert values[0] == pytest.approx(70.4641, abs=1e-3)
    assert values[1] == pytest.approx(66.2496, abs=1e-3)


def test_rsi_is_100_when_price_only_rises():
    rsi = RSI(14)
    for price in range(1, 30):
        rsi.update(float(price))
    assert rsi.value == 100.0


def test_atr_uses_true_range_including_gaps():
    atr = ATR(2)
    now = datetime(2026, 9, 1, 9, 15)
    assert atr.update_bar(Bar(now, 10, 12, 9, 11)) is None      # TR = 3
    # Gap up: TR = max(14-12, |14-11|, |12-11|) = 3
    assert atr.update_bar(Bar(now, 13, 14, 12, 13)) == pytest.approx(3.0)


def test_atr_rejects_scalar_updates():
    with pytest.raises(TypeError, match="full bar"):
        ATR(14).update(10.0)


def test_crossover_fires_only_on_the_crossing_bar():
    cross = Crossover()
    assert cross.update(1, 2) is None      # first observation just records state
    assert cross.update(3, 2) == "up"
    assert cross.update(4, 2) is None      # still above, no new cross
    assert cross.update(1, 2) == "down"
    assert cross.update(1, 2) is None


def test_crossover_ignores_unready_indicators():
    cross = Crossover()
    assert cross.update(None, 2) is None
    assert cross.update(1, None) is None


@pytest.mark.parametrize("cls", [SMA, EMA, RSI, ATR])
def test_period_must_be_positive(cls):
    with pytest.raises(ValueError):
        cls(0)
