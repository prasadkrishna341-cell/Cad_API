import pytest

from kitealgo.instruments import InstrumentMaster

ROWS = [
    {"instrument_token": "408065", "tradingsymbol": "INFY", "exchange": "NSE",
     "name": "INFOSYS", "lot_size": "1", "tick_size": "0.05",
     "instrument_type": "EQ", "segment": "NSE", "expiry": ""},
    {"instrument_token": "260105", "tradingsymbol": "NIFTY26SEPFUT", "exchange": "NFO",
     "name": "NIFTY", "lot_size": "50", "tick_size": "0.05",
     "instrument_type": "FUT", "segment": "NFO-FUT", "expiry": "2026-09-24"},
    # Deliberately malformed — must be skipped, not crash the load.
    {"instrument_token": "", "tradingsymbol": "BROKEN", "exchange": "NSE"},
]


@pytest.fixture
def master(settings):
    return InstrumentMaster(settings).load_from_rows(ROWS)


def test_malformed_rows_are_skipped(master):
    assert len(master) == 2


def test_lookup_by_symbol_and_combined_key(master):
    assert master.get("INFY").instrument_token == 408065
    assert master.get("NSE:INFY").instrument_token == 408065
    assert master.get("infy").tradingsymbol == "INFY"          # case-insensitive


def test_lookup_by_token_preserves_lot_size(master):
    future = master.by_token(260105)
    assert future.lot_size == 50 and future.expiry == "2026-09-24"


def test_unknown_symbol_raises_a_helpful_error(master):
    with pytest.raises(KeyError, match="Unknown instrument"):
        master.get("TCS")
    with pytest.raises(KeyError, match="Unknown instrument_token"):
        master.by_token(1)


def test_resolve_all(master):
    resolved = master.resolve_all(["INFY"], "NSE")
    assert [i.tradingsymbol for i in resolved] == ["INFY"]


def test_search_matches_symbol_and_name(master):
    assert [i.tradingsymbol for i in master.search("nifty")] == ["NIFTY26SEPFUT"]
    assert [i.tradingsymbol for i in master.search("INFOSYS")] == ["INFY"]
    assert master.search("nothing-here") == []


def test_load_without_client_or_cache_is_an_error(settings):
    with pytest.raises(RuntimeError, match="No cached instrument dump"):
        InstrumentMaster(settings).load()
