"""Live market data over Kite's websocket (`KiteTicker`).

The SDK hands back raw dicts on a background thread; this wraps them into
`Tick` objects and pushes them onto a queue so strategy code runs on the main
thread instead of inside the websocket callback (where a slow handler stalls
the socket and a raised exception is swallowed).
"""

from __future__ import annotations

import logging
import queue
import threading
from datetime import datetime
from typing import Callable, Iterable, Optional

from ..config import IST, Settings
from ..models import Instrument, Tick

log = logging.getLogger(__name__)

MODE_LTP = "ltp"
MODE_QUOTE = "quote"
MODE_FULL = "full"


def normalise_tick(raw: dict) -> Optional[Tick]:
    """Convert one raw Kite tick dict into a `Tick`. Returns None if unusable."""
    token = raw.get("instrument_token")
    price = raw.get("last_price")
    if token is None or price is None:
        return None

    timestamp = raw.get("exchange_timestamp") or raw.get("last_trade_time")
    if not isinstance(timestamp, datetime):
        timestamp = datetime.now(IST)
    elif timestamp.tzinfo is None:
        # Kite sends naive IST datetimes.
        timestamp = timestamp.replace(tzinfo=IST)

    # 'volume_traded' in kiteconnect 4+, 'volume' in older payloads.
    volume = raw.get("volume_traded", raw.get("volume", 0)) or 0

    return Tick(
        instrument_token=int(token),
        last_price=float(price),
        timestamp=timestamp,
        volume=int(volume),
        oi=int(raw.get("oi") or 0),
        raw=raw,
    )


class TickStream:
    """Subscribes to instruments and yields normalised ticks."""

    def __init__(
        self,
        settings: Settings,
        access_token: str,
        instruments: Iterable[Instrument],
        mode: str = MODE_FULL,
        on_order_update: Optional[Callable[[dict], None]] = None,
    ) -> None:
        self.settings = settings
        self.instruments = list(instruments)
        self.tokens = [i.instrument_token for i in self.instruments]
        self.mode = mode
        self.on_order_update = on_order_update
        self.queue: "queue.Queue[Tick]" = queue.Queue(maxsize=10_000)
        self.connected = threading.Event()
        self._dropped = 0

        from kiteconnect import KiteTicker

        self.ticker = KiteTicker(settings.api_key, access_token)
        self.ticker.on_ticks = self._on_ticks
        self.ticker.on_connect = self._on_connect
        self.ticker.on_close = self._on_close
        self.ticker.on_error = self._on_error
        self.ticker.on_reconnect = self._on_reconnect
        self.ticker.on_noreconnect = self._on_noreconnect
        if on_order_update:
            self.ticker.on_order_update = lambda ws, data: on_order_update(data)

    # -- websocket callbacks (background thread) --------------------------
    def _on_ticks(self, ws, ticks: list[dict]) -> None:
        for raw in ticks:
            tick = normalise_tick(raw)
            if tick is None:
                continue
            try:
                self.queue.put_nowait(tick)
            except queue.Full:
                # Better to shed the oldest tick than to block the socket.
                self._dropped += 1
                if self._dropped % 100 == 1:
                    log.warning("Tick queue full; dropped %d ticks", self._dropped)

    def _on_connect(self, ws, response) -> None:
        log.info("Websocket connected; subscribing to %d instruments", len(self.tokens))
        ws.subscribe(self.tokens)
        ws.set_mode(self.mode, self.tokens)
        self.connected.set()

    def _on_close(self, ws, code, reason) -> None:
        log.warning("Websocket closed (%s): %s", code, reason)
        self.connected.clear()

    def _on_error(self, ws, code, reason) -> None:
        log.error("Websocket error (%s): %s", code, reason)

    def _on_reconnect(self, ws, attempts) -> None:
        log.warning("Websocket reconnecting (attempt %s)", attempts)

    def _on_noreconnect(self, ws) -> None:
        log.error("Websocket gave up reconnecting — no more live data.")
        self.connected.clear()

    # -- control ----------------------------------------------------------
    def start(self, timeout: float = 30.0) -> bool:
        """Connect in a background thread. True once subscribed."""
        self.ticker.connect(threaded=True)
        return self.connected.wait(timeout=timeout)

    def stop(self) -> None:
        try:
            self.ticker.close()
        except Exception as exc:
            log.debug("Error closing websocket: %s", exc)
        self.connected.clear()

    def get(self, timeout: float = 1.0) -> Optional[Tick]:
        """Next tick, or None if none arrived within `timeout`."""
        try:
            return self.queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def drain(self, max_items: int = 500) -> list[Tick]:
        """Every tick currently buffered, up to `max_items`."""
        out: list[Tick] = []
        while len(out) < max_items:
            try:
                out.append(self.queue.get_nowait())
            except queue.Empty:
                break
        return out
