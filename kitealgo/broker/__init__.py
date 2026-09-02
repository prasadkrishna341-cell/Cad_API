"""Broker adapters. `PaperBroker` is the default; `KiteBroker` places real orders."""

from .base import Broker, BrokerError
from .paper import PaperBroker

__all__ = ["Broker", "BrokerError", "PaperBroker", "KiteBroker", "build_broker"]


def __getattr__(name: str):
    # Imported lazily so the package works without `kiteconnect` installed
    # (backtests and paper runs need no SDK at all).
    if name == "KiteBroker":
        from .kite import KiteBroker
        return KiteBroker
    if name == "build_broker":
        from .factory import build_broker
        return build_broker
    raise AttributeError(name)
