"""Tests for the updater's protection rules.

This is the one piece of the project that writes over a user's working copy,
so the rule deciding what it must never touch is worth pinning down.
"""

import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "kitealgo_update", Path(__file__).resolve().parent.parent / "update.py"
)
update = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(update)


@pytest.mark.parametrize("path", [
    ".env",
    ".venv/lib/python3.12/site-packages/pandas/__init__.py",
    ".kitealgo/access_token.json",
    ".kitealgo/cache/instruments_ALL_2026-09-02.csv",
    ".kitealgo/kitealgo.db",
])
def test_user_files_are_protected(path):
    assert update._is_protected(Path(path)) is True


@pytest.mark.parametrize("path", [
    "README.md",
    "kitealgo/backtest.py",
    "kitealgo/strategy/orb.py",
    "tests/test_risk.py",
    ".env.example",
    ".gitignore",
])
def test_project_files_are_replaceable(path):
    assert update._is_protected(Path(path)) is False


def test_env_example_is_not_confused_with_env():
    """The template ships with the project; the real .env never does."""
    assert update._is_protected(Path(".env.example")) is False
    assert update._is_protected(Path(".env")) is True


def test_sync_skips_protected_paths_even_if_present_in_a_download(tmp_path):
    """Belt and braces: a malformed archive containing .env must not overwrite one."""
    source, target = tmp_path / "src", tmp_path / "dst"
    (source / "kitealgo").mkdir(parents=True)
    (target).mkdir()

    (source / ".env").write_text("KITE_API_SECRET=attacker-supplied")
    (source / "kitealgo" / "risk.py").write_text("# new code")
    (target / ".env").write_text("KITE_API_SECRET=mine")

    update.sync(source, target, dry_run=False)

    assert (target / ".env").read_text() == "KITE_API_SECRET=mine"
    assert (target / "kitealgo" / "risk.py").read_text() == "# new code"


def test_dry_run_writes_nothing(tmp_path):
    source, target = tmp_path / "src", tmp_path / "dst"
    source.mkdir()
    target.mkdir()
    (source / "new.py").write_text("x = 1")

    update.sync(source, target, dry_run=True)
    assert not (target / "new.py").exists()
