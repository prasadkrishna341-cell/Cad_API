"""The interface every broker adapter implements."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from ..models import Fill, Instrument, Order


class BrokerError(RuntimeError):
    """An order was rejected, or the broker could not be reached."""


class Broker(ABC):
    """Minimal surface the engine needs. Keeps live and paper interchangeable."""

    #: Human-readable mode, surfaced in logs and the CLI banner.
    mode: str = "unknown"

    @abstractmethod
    def place_order(self, order: Order) -> Order:
        """Submit an order. Returns the same Order with broker id/status filled in."""

    @abstractmethod
    def cancel_order(self, order: Order) -> Order:
        """Cancel a resting order."""

    @abstractmethod
    def ltp(self, instruments: list[Instrument]) -> dict[int, float]:
        """Last traded price per instrument_token."""

    @abstractmethod
    def orders(self) -> list[dict]:
        """Raw order book."""

    @abstractmethod
    def positions(self) -> list[dict]:
        """Raw position book."""

    def fills(self) -> list[Fill]:
        """Fills produced so far. Brokers that don't track them return []."""
        return []

    def margin_available(self) -> Optional[float]:
        """Free cash for trading, when the broker can report it."""
        return None

    @property
    def is_live(self) -> bool:
        return self.mode == "live"
