"""Instrument master: symbol -> instrument_token lookup.

Kite's websocket speaks in numeric `instrument_token`s while humans (and order
placement) speak in `tradingsymbol`s.  The full dump is a few megabytes, so it
is fetched once a day and cached under the state directory.
"""

from __future__ import annotations

import csv
import logging
from datetime import date
from pathlib import Path
from typing import Iterable, Optional

from .config import Settings
from .models import Instrument
from .ratelimit import QUOTE_LIMITER

log = logging.getLogger(__name__)

_FIELDS = [
    "instrument_token", "exchange_token", "tradingsymbol", "name", "last_price",
    "expiry", "strike", "tick_size", "lot_size", "instrument_type", "segment", "exchange",
]


def _to_instrument(row: dict) -> Instrument:
    def num(key, cast, default):
        try:
            value = row.get(key)
            return cast(value) if value not in (None, "") else default
        except (TypeError, ValueError):
            return default

    expiry = row.get("expiry") or None
    return Instrument(
        instrument_token=num("instrument_token", int, 0),
        tradingsymbol=str(row.get("tradingsymbol", "")).strip(),
        exchange=str(row.get("exchange", "")).strip(),
        name=str(row.get("name", "") or "").strip(),
        lot_size=num("lot_size", int, 1) or 1,
        tick_size=num("tick_size", float, 0.05) or 0.05,
        segment=str(row.get("segment", "") or "").strip(),
        instrument_type=str(row.get("instrument_type", "") or "").strip(),
        expiry=str(expiry) if expiry else None,
    )


class InstrumentMaster:
    """Loads, caches and indexes Kite's instrument dump."""

    def __init__(self, settings: Settings, kite=None, exchange: Optional[str] = None) -> None:
        self.settings = settings
        self.kite = kite
        self.exchange = exchange
        self._by_key: dict[str, Instrument] = {}
        self._by_token: dict[int, Instrument] = {}

    # -- cache ------------------------------------------------------------
    def _cache_path(self, on: Optional[date] = None) -> Path:
        stamp = (on or date.today()).isoformat()
        suffix = self.exchange or "ALL"
        return self.settings.cache_dir / f"instruments_{suffix}_{stamp}.csv"

    def _read_cache(self) -> Optional[list[dict]]:
        path = self._cache_path()
        if not path.is_file():
            return None
        with path.open(newline="") as handle:
            return list(csv.DictReader(handle))

    def _write_cache(self, rows: Iterable[dict]) -> None:
        self.settings.ensure_dirs()
        path = self._cache_path()
        with path.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=_FIELDS, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({k: row.get(k, "") for k in _FIELDS})
        # Yesterday's dumps are dead weight.
        for stale in self.settings.cache_dir.glob("instruments_*.csv"):
            if stale != path:
                stale.unlink(missing_ok=True)

    # -- loading ----------------------------------------------------------
    def load(self, force_refresh: bool = False) -> "InstrumentMaster":
        rows = None if force_refresh else self._read_cache()
        if rows is None:
            if self.kite is None:
                raise RuntimeError(
                    "No cached instrument dump and no Kite client to fetch one. "
                    "Run `python -m kitealgo.cli instruments --refresh` first."
                )
            log.info("Downloading instrument master (exchange=%s)", self.exchange or "ALL")
            QUOTE_LIMITER.acquire()
            rows = self.kite.instruments(self.exchange) if self.exchange else self.kite.instruments()
            self._write_cache(rows)
        self._index(rows)
        log.info("Loaded %d instruments", len(self._by_token))
        return self

    def _index(self, rows: Iterable[dict]) -> None:
        self._by_key.clear()
        self._by_token.clear()
        for row in rows:
            instrument = _to_instrument(row)
            if not instrument.instrument_token or not instrument.tradingsymbol:
                continue
            self._by_key[instrument.key] = instrument
            self._by_token[instrument.instrument_token] = instrument

    def load_from_rows(self, rows: Iterable[dict]) -> "InstrumentMaster":
        """Index an in-memory dump — used by tests and backtests."""
        self._index(rows)
        return self

    # -- lookup -----------------------------------------------------------
    def __len__(self) -> int:
        return len(self._by_token)

    def get(self, symbol: str, exchange: str = "NSE") -> Instrument:
        """Look up by `INFY` + exchange, or by the `NSE:INFY` combined form."""
        key = symbol.upper() if ":" in symbol else f"{exchange.upper()}:{symbol.upper()}"
        try:
            return self._by_key[key]
        except KeyError:
            raise KeyError(
                f"Unknown instrument {key!r}. Check the tradingsymbol and exchange, "
                "or refresh the instrument dump."
            ) from None

    def by_token(self, token: int) -> Instrument:
        try:
            return self._by_token[int(token)]
        except KeyError:
            raise KeyError(f"Unknown instrument_token {token}") from None

    def resolve_all(self, symbols: Iterable[str], exchange: str = "NSE") -> list[Instrument]:
        return [self.get(s, exchange) for s in symbols]

    def search(self, text: str, limit: int = 20) -> list[Instrument]:
        """Substring search over tradingsymbol and name."""
        needle = text.upper()
        hits = [
            i for i in self._by_token.values()
            if needle in i.tradingsymbol.upper() or needle in i.name.upper()
        ]
        hits.sort(key=lambda i: (len(i.tradingsymbol), i.tradingsymbol))
        return hits[:limit]
