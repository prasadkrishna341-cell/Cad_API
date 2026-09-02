from datetime import date, datetime, timedelta

import pytest

from kitealgo.config import IST
from kitealgo.data.bars import BarAggregator, BarBuilder, floor_time
from kitealgo.data.historical import INTERVAL_MAX_DAYS, chunk_ranges
from kitealgo.data.stream import normalise_tick
from kitealgo.models import Tick

T0 = datetime(2026, 9, 1, 9, 15, tzinfo=IST)


def test_floor_time_buckets_to_interval():
    assert floor_time(datetime(2026, 9, 1, 9, 17, 43), 300).minute == 15
    assert floor_time(datetime(2026, 9, 1, 9, 21, 0), 300).minute == 20


def test_bar_builder_accumulates_ohlc():
    builder = BarBuilder(1, 60)
    for offset, price in [(0, 100), (20, 102), (40, 99)]:
        assert builder.add(Tick(1, price, T0 + timedelta(seconds=offset))) is None
    bar = builder.add(Tick(1, 101, T0 + timedelta(seconds=65)))
    assert (bar.open, bar.high, bar.low, bar.close) == (100, 102, 99, 99)


def test_volume_is_deltas_not_cumulative():
    builder = BarBuilder(1, 60)
    builder.add(Tick(1, 100, T0, volume=1000))
    builder.add(Tick(1, 101, T0 + timedelta(seconds=30), volume=1500))
    bar = builder.add(Tick(1, 102, T0 + timedelta(seconds=70), volume=1800))
    assert bar.volume == 500          # 1500 - 1000, not 1500


def test_out_of_order_tick_does_not_corrupt_bar():
    builder = BarBuilder(1, 60)
    builder.add(Tick(1, 100, T0 + timedelta(seconds=90)))
    assert builder.add(Tick(1, 5000, T0)) is None       # stale tick ignored
    assert builder.current.high == 100


def test_flush_returns_partial_bar():
    builder = BarBuilder(1, 60)
    builder.add(Tick(1, 100, T0))
    assert builder.flush().close == 100
    assert builder.flush() is None


def test_aggregator_separates_instruments_and_calls_back():
    seen = []
    aggregator = BarAggregator(60, on_bar=seen.append)
    aggregator.add(Tick(1, 100, T0))
    aggregator.add(Tick(2, 200, T0))
    aggregator.add(Tick(1, 101, T0 + timedelta(seconds=70)))
    assert len(seen) == 1 and seen[0].instrument_token == 1
    assert aggregator.current(2).close == 200


@pytest.mark.parametrize("interval,expected_chunks", [
    ("minute", 7),      # 365 days / 60
    ("day", 1),         # 365 days fits in one 2000-day window
    ("15minute", 2),    # 365 / 200
])
def test_chunk_ranges_respect_kite_limits(interval, expected_chunks):
    chunks = chunk_ranges(date(2024, 1, 1), date(2024, 12, 31), interval)
    assert len(chunks) == expected_chunks
    span = INTERVAL_MAX_DAYS[interval]
    assert all((end - start).days < span for start, end in chunks)
    assert chunks[0][0] == date(2024, 1, 1) and chunks[-1][1] == date(2024, 12, 31)


def test_chunks_are_contiguous_and_non_overlapping():
    chunks = chunk_ranges(date(2024, 1, 1), date(2024, 12, 31), "minute")
    for (_, prev_end), (next_start, _) in zip(chunks, chunks[1:]):
        assert next_start == prev_end + timedelta(days=1)


def test_chunk_ranges_rejects_bad_input():
    with pytest.raises(ValueError, match="Unknown interval"):
        chunk_ranges(date(2024, 1, 1), date(2024, 2, 1), "7minute")
    with pytest.raises(ValueError, match="must not be after"):
        chunk_ranges(date(2024, 2, 1), date(2024, 1, 1), "day")


def test_normalise_tick_handles_both_volume_keys():
    assert normalise_tick({"instrument_token": 1, "last_price": 10.0,
                           "volume_traded": 99}).volume == 99
    assert normalise_tick({"instrument_token": 1, "last_price": 10.0,
                           "volume": 42}).volume == 42


def test_normalise_tick_attaches_ist_when_naive():
    tick = normalise_tick({"instrument_token": 1, "last_price": 10.0,
                           "exchange_timestamp": datetime(2026, 9, 1, 10, 0)})
    assert tick.timestamp.tzinfo is not None
    assert tick.timestamp.utcoffset() == timedelta(hours=5, minutes=30)


def test_normalise_tick_rejects_unusable_payloads():
    assert normalise_tick({"instrument_token": 1}) is None
    assert normalise_tick({"last_price": 10.0}) is None
