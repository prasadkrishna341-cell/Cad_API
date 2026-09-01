import json
from datetime import datetime, timedelta

import pytest

from kitealgo.auth import AuthError, TokenStore, extract_request_token, token_expiry
from kitealgo.config import IST


@pytest.mark.parametrize("text,expected", [
    ("http://127.0.0.1:5000/callback?request_token=abc123&action=login", "abc123"),
    ("?request_token=zz9&status=success", "zz9"),
    ("abc123", "abc123"),
    ("  abc123  ", "abc123"),
])
def test_extract_request_token(text, expected):
    assert extract_request_token(text) == expected


@pytest.mark.parametrize("text", ["", "   ", "http://x/callback?status=success"])
def test_extract_request_token_rejects_junk(text):
    with pytest.raises(AuthError):
        extract_request_token(text)


def test_token_issued_in_the_morning_expires_next_day():
    issued = datetime(2026, 9, 1, 10, 0, tzinfo=IST)
    assert token_expiry(issued) == datetime(2026, 9, 2, 6, 0, tzinfo=IST)


def test_token_issued_before_6am_expires_the_same_morning():
    issued = datetime(2026, 9, 1, 3, 0, tzinfo=IST)
    assert token_expiry(issued) == datetime(2026, 9, 1, 6, 0, tzinfo=IST)


def test_fresh_token_is_returned_from_cache(settings):
    store = TokenStore(settings)
    store.save("tok-123")
    assert store.load() == "tok-123"


def test_expired_token_is_not_returned(settings):
    store = TokenStore(settings)
    settings.ensure_dirs()
    stale = (datetime.now(IST) - timedelta(days=3)).isoformat()
    store.path.write_text(json.dumps({"access_token": "old", "issued_at": stale}))
    assert store.load() is None


def test_token_for_a_different_api_key_is_ignored(settings, monkeypatch):
    store = TokenStore(settings)
    settings.ensure_dirs()
    store.path.write_text(json.dumps({
        "access_token": "tok", "api_key": "someone-elses-key",
        "issued_at": datetime.now(IST).isoformat(),
    }))
    monkeypatch.setenv("KITE_API_KEY", "my-key")
    from kitealgo.config import Settings
    assert TokenStore(Settings.from_env()).load() is None


def test_corrupt_cache_is_ignored_not_fatal(settings):
    store = TokenStore(settings)
    settings.ensure_dirs()
    store.path.write_text("{not json")
    assert store.load() is None


def test_token_file_is_owner_readable_only(settings):
    store = TokenStore(settings)
    store.save("tok-123")
    assert store.path.stat().st_mode & 0o077 == 0


def test_clear_removes_the_cache(settings):
    store = TokenStore(settings)
    store.save("tok")
    store.clear()
    assert store.load() is None
