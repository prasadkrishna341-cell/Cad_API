"""Environment-driven configuration.

Everything the framework needs to run is read from environment variables (or a
`.env` file).  Nothing is hardcoded and no secret is ever written to disk by
this module.

The single most important thing here is :meth:`Settings.is_live`.  It returns
True only when the user has *both* selected live mode and typed the explicit
confirmation string, so an accidental env var cannot start trading real money.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

LIVE_CONFIRMATION_PHRASE = "I_UNDERSTAND_THE_RISK"


class ConfigError(RuntimeError):
    """Raised when configuration is missing or self-contradictory."""


def _load_dotenv_if_present() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:  # python-dotenv is optional at runtime
        return
    for candidate in (Path.cwd() / ".env", Path(__file__).resolve().parent.parent / ".env"):
        if candidate.is_file():
            load_dotenv(candidate, override=False)
            return


def _env_str(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _env_float(key: str, default: float) -> float:
    raw = _env_str(key)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be a number, got {raw!r}") from exc


def _env_int(key: str, default: int) -> int:
    raw = _env_str(key)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be an integer, got {raw!r}") from exc


def _env_time(key: str, default: str) -> dtime:
    raw = _env_str(key) or default
    try:
        hours, minutes = raw.split(":")
        return dtime(int(hours), int(minutes))
    except (ValueError, TypeError) as exc:
        raise ConfigError(f"{key} must look like HH:MM, got {raw!r}") from exc


@dataclass(frozen=True)
class RiskLimits:
    """Hard limits checked before every single order."""

    capital: float = 100_000.0
    risk_per_trade: float = 0.01
    max_position_pct: float = 0.25
    max_daily_loss_pct: float = 0.03
    max_open_positions: int = 5
    max_trades_per_day: int = 20

    def __post_init__(self) -> None:
        if self.capital <= 0:
            raise ConfigError("KITE_CAPITAL must be positive")
        for name in ("risk_per_trade", "max_position_pct", "max_daily_loss_pct"):
            value = getattr(self, name)
            if not 0 < value <= 1:
                raise ConfigError(f"{name} must be in (0, 1], got {value}")
        if self.max_open_positions < 1:
            raise ConfigError("max_open_positions must be >= 1")
        if self.max_trades_per_day < 1:
            raise ConfigError("max_trades_per_day must be >= 1")

    @property
    def max_loss_amount(self) -> float:
        return self.capital * self.max_daily_loss_pct

    @property
    def max_position_value(self) -> float:
        return self.capital * self.max_position_pct

    @property
    def risk_amount_per_trade(self) -> float:
        return self.capital * self.risk_per_trade


@dataclass(frozen=True)
class Settings:
    api_key: str = ""
    api_secret: str = ""
    redirect_url: str = "http://127.0.0.1:5000/callback"
    trading_mode: str = "paper"
    live_confirm: str = ""
    default_product: str = "MIS"
    trade_start: dtime = dtime(9, 20)
    trade_end: dtime = dtime(15, 0)
    square_off: dtime = dtime(15, 15)
    state_dir: Path = Path(".kitealgo")
    log_level: str = "INFO"
    risk: RiskLimits = field(default_factory=RiskLimits)

    # -- mode -------------------------------------------------------------
    @property
    def is_live(self) -> bool:
        """True only when live mode is selected *and* explicitly confirmed."""
        return (
            self.trading_mode.lower() == "live"
            and self.live_confirm == LIVE_CONFIRMATION_PHRASE
        )

    @property
    def mode_label(self) -> str:
        return "LIVE" if self.is_live else "PAPER"

    def require_live_ready(self) -> None:
        """Raise a helpful error if live mode was asked for but not confirmed."""
        if self.trading_mode.lower() == "live" and not self.is_live:
            raise ConfigError(
                "KITE_TRADING_MODE=live requires "
                f"KITE_LIVE_CONFIRM={LIVE_CONFIRMATION_PHRASE}. "
                "Refusing to place real orders without it."
            )

    def require_credentials(self) -> None:
        missing = [
            name
            for name, value in (("KITE_API_KEY", self.api_key), ("KITE_API_SECRET", self.api_secret))
            if not value
        ]
        if missing:
            raise ConfigError(
                f"Missing {', '.join(missing)}. Copy .env.example to .env and fill it in."
            )

    # -- paths ------------------------------------------------------------
    @property
    def token_file(self) -> Path:
        return self.state_dir / "access_token.json"

    @property
    def db_file(self) -> Path:
        return self.state_dir / "kitealgo.db"

    @property
    def cache_dir(self) -> Path:
        return self.state_dir / "cache"

    @property
    def holiday_file(self) -> Path:
        return self.state_dir / "holidays.json"

    def ensure_dirs(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_env(cls) -> "Settings":
        _load_dotenv_if_present()
        risk = RiskLimits(
            capital=_env_float("KITE_CAPITAL", 100_000.0),
            risk_per_trade=_env_float("KITE_RISK_PER_TRADE", 0.01),
            max_position_pct=_env_float("KITE_MAX_POSITION_PCT", 0.25),
            max_daily_loss_pct=_env_float("KITE_MAX_DAILY_LOSS_PCT", 0.03),
            max_open_positions=_env_int("KITE_MAX_OPEN_POSITIONS", 5),
            max_trades_per_day=_env_int("KITE_MAX_TRADES_PER_DAY", 20),
        )
        settings = cls(
            api_key=_env_str("KITE_API_KEY"),
            api_secret=_env_str("KITE_API_SECRET"),
            redirect_url=_env_str("KITE_REDIRECT_URL", "http://127.0.0.1:5000/callback"),
            trading_mode=_env_str("KITE_TRADING_MODE", "paper").lower(),
            live_confirm=_env_str("KITE_LIVE_CONFIRM"),
            default_product=_env_str("KITE_DEFAULT_PRODUCT", "MIS").upper(),
            trade_start=_env_time("KITE_TRADE_START_TIME", "09:20"),
            trade_end=_env_time("KITE_TRADE_END_TIME", "15:00"),
            square_off=_env_time("KITE_SQUARE_OFF_TIME", "15:15"),
            state_dir=Path(_env_str("KITE_STATE_DIR", ".kitealgo")),
            log_level=_env_str("KITE_LOG_LEVEL", "INFO").upper(),
            risk=risk,
        )
        if not settings.trade_start < settings.trade_end <= settings.square_off:
            raise ConfigError(
                "Times must satisfy trade_start < trade_end <= square_off "
                f"(got {settings.trade_start}, {settings.trade_end}, {settings.square_off})"
            )
        return settings
