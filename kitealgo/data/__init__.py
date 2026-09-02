"""Market data: historical candles, live ticks, and tick-to-bar aggregation."""

from .bars import BarAggregator, BarBuilder
from .historical import HistoricalData, INTERVAL_MAX_DAYS

__all__ = ["BarAggregator", "BarBuilder", "HistoricalData", "INTERVAL_MAX_DAYS", "TickStream"]


def __getattr__(name: str):
    if name == "TickStream":  # needs kiteconnect, import lazily
        from .stream import TickStream
        return TickStream
    raise AttributeError(name)
