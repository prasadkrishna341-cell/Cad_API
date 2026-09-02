"""Logging: console plus a dated file, with secrets redacted."""

from __future__ import annotations

import logging
import re
import sys
from datetime import date
from pathlib import Path
from typing import Optional

from .config import Settings

# Anything that looks like a credential gets masked before it can reach a log
# file — access tokens in particular are full account access.
_SECRET_PATTERNS = [
    re.compile(r"(access_token[\"'\s:=]+)([A-Za-z0-9_\-]{6,})", re.I),
    re.compile(r"(api_secret[\"'\s:=]+)([A-Za-z0-9_\-]{6,})", re.I),
    re.compile(r"(request_token[\"'\s:=]+)([A-Za-z0-9_\-]{6,})", re.I),
    re.compile(r"(api_key[\"'\s:=]+)([A-Za-z0-9_\-]{6,})", re.I),
]


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)
        for pattern in _SECRET_PATTERNS:
            message = pattern.sub(lambda m: f"{m.group(1)}***REDACTED***", message)
        return message


def setup_logging(settings: Optional[Settings] = None, level: Optional[str] = None) -> logging.Logger:
    settings = settings or Settings.from_env()
    resolved = getattr(logging, (level or settings.log_level).upper(), logging.INFO)

    root = logging.getLogger()
    root.setLevel(resolved)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    fmt = RedactingFormatter(
        "%(asctime)s %(levelname)-8s %(name)-22s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    try:
        log_dir = settings.state_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_dir / f"kitealgo-{date.today():%Y-%m-%d}.log")
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    except OSError as exc:  # read-only filesystem — console logging still works
        root.warning("File logging disabled: %s", exc)

    # The SDK's own logging is noisy at DEBUG.
    logging.getLogger("kiteconnect").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    return root
