import pytest

from kitealgo.config import ConfigError, RiskLimits, Settings


def test_paper_is_the_default(settings):
    assert settings.is_live is False
    assert settings.mode_label == "PAPER"


def test_live_needs_explicit_confirmation(monkeypatch):
    monkeypatch.setenv("KITE_TRADING_MODE", "live")
    monkeypatch.delenv("KITE_LIVE_CONFIRM", raising=False)
    settings = Settings.from_env()
    assert settings.is_live is False
    with pytest.raises(ConfigError, match="I_UNDERSTAND_THE_RISK"):
        settings.require_live_ready()


def test_live_mode_when_confirmed(monkeypatch):
    monkeypatch.setenv("KITE_TRADING_MODE", "live")
    monkeypatch.setenv("KITE_LIVE_CONFIRM", "I_UNDERSTAND_THE_RISK")
    settings = Settings.from_env()
    assert settings.is_live is True
    settings.require_live_ready()  # must not raise


def test_wrong_confirmation_phrase_stays_paper(monkeypatch):
    monkeypatch.setenv("KITE_TRADING_MODE", "live")
    monkeypatch.setenv("KITE_LIVE_CONFIRM", "yes")
    assert Settings.from_env().is_live is False


def test_missing_credentials_are_reported(monkeypatch):
    monkeypatch.delenv("KITE_API_KEY", raising=False)
    monkeypatch.delenv("KITE_API_SECRET", raising=False)
    with pytest.raises(ConfigError, match="KITE_API_KEY"):
        Settings.from_env().require_credentials()


@pytest.mark.parametrize("field,value", [
    ("capital", 0),
    ("risk_per_trade", 0),
    ("risk_per_trade", 1.5),
    ("max_daily_loss_pct", -0.1),
    ("max_open_positions", 0),
])
def test_invalid_risk_limits_rejected(field, value):
    with pytest.raises(ConfigError):
        RiskLimits(**{field: value})


def test_derived_risk_amounts():
    limits = RiskLimits(capital=200_000, risk_per_trade=0.02,
                        max_position_pct=0.1, max_daily_loss_pct=0.05)
    assert limits.risk_amount_per_trade == 4_000
    assert limits.max_position_value == 20_000
    assert limits.max_loss_amount == 10_000


def test_times_must_be_ordered(monkeypatch):
    monkeypatch.setenv("KITE_TRADE_START_TIME", "15:00")
    monkeypatch.setenv("KITE_TRADE_END_TIME", "09:20")
    with pytest.raises(ConfigError, match="trade_start"):
        Settings.from_env()


def test_malformed_time_rejected(monkeypatch):
    monkeypatch.setenv("KITE_TRADE_START_TIME", "nine-twenty")
    with pytest.raises(ConfigError, match="HH:MM"):
        Settings.from_env()
