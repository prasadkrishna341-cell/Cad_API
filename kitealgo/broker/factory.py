"""Chooses the broker for a run. Paper unless live is explicitly confirmed."""

from __future__ import annotations

import logging

from ..config import Settings
from .base import Broker
from .paper import PaperBroker

log = logging.getLogger(__name__)


def build_broker(settings: Settings, kite=None) -> Broker:
    settings.require_live_ready()
    if not settings.is_live:
        log.info("PAPER mode — orders are simulated, nothing reaches your Kite account.")
        return PaperBroker(starting_cash=settings.risk.capital)

    from ..auth import build_kite_client
    from .kite import KiteBroker

    log.warning("LIVE mode — orders will be placed on your real Kite account.")
    return KiteBroker(kite or build_kite_client(settings), settings)
