"""Strategies. Subclass `Strategy`, emit `Signal`s, let the engine size them."""

from .base import Strategy, StrategyState
from .ema_crossover import EmaCrossoverStrategy
from .orb import OpeningRangeBreakoutStrategy

#: Name -> class, used by the CLI's --strategy flag.
REGISTRY: dict[str, type[Strategy]] = {
    "ema_crossover": EmaCrossoverStrategy,
    "orb": OpeningRangeBreakoutStrategy,
}

__all__ = [
    "Strategy", "StrategyState", "EmaCrossoverStrategy",
    "OpeningRangeBreakoutStrategy", "REGISTRY", "get_strategy",
]


def get_strategy(name: str) -> type[Strategy]:
    try:
        return REGISTRY[name]
    except KeyError:
        raise KeyError(
            f"Unknown strategy {name!r}. Available: {', '.join(sorted(REGISTRY))}"
        ) from None
