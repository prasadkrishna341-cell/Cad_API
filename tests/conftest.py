import os
import tempfile

import pytest

import kitealgo.config as _config

# A developer's real .env must never bleed into the suite — otherwise these
# tests pass or fail depending on whose machine they run on.
_config._load_dotenv_if_present = lambda: None
for _key in ("KITE_API_KEY", "KITE_API_SECRET", "KITE_REDIRECT_URL"):
    os.environ.pop(_key, None)

# Isolate every test run from the developer's real .env and state directory.
os.environ.setdefault("KITE_STATE_DIR", tempfile.mkdtemp(prefix="kitealgo-test-"))
for key in ("KITE_TRADING_MODE", "KITE_LIVE_CONFIRM"):
    os.environ.pop(key, None)
os.environ["KITE_TRADING_MODE"] = "paper"
os.environ["KITE_CAPITAL"] = "100000"
os.environ["KITE_RISK_PER_TRADE"] = "0.01"
os.environ["KITE_MAX_POSITION_PCT"] = "0.25"
os.environ["KITE_MAX_DAILY_LOSS_PCT"] = "0.03"
os.environ["KITE_TRADE_START_TIME"] = "09:20"
os.environ["KITE_TRADE_END_TIME"] = "15:00"
os.environ["KITE_SQUARE_OFF_TIME"] = "15:15"

from kitealgo.config import Settings          # noqa: E402
from kitealgo.models import Instrument        # noqa: E402


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("KITE_STATE_DIR", str(tmp_path))
    return Settings.from_env()


@pytest.fixture
def infy():
    return Instrument(408065, "INFY", "NSE", name="INFOSYS", lot_size=1, tick_size=0.05)


@pytest.fixture
def nifty_fut():
    return Instrument(260105, "NIFTY26SEPFUT", "NFO", name="NIFTY", lot_size=50, tick_size=0.05)
