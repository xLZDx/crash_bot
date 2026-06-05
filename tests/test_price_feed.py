"""Tests for price_feed typed status (PR6)."""
import os
import sys
import json
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import price_feed as pf
from price_feed import PriceFeedStatus, _source_to_status, get_sol_usd


# ---------------------------------------------------------------------------
# _source_to_status mapping
# ---------------------------------------------------------------------------

def test_source_to_status_live_exchange():
    for name in ("Binance", "Bybit", "OKX", "KuCoin", "Gate.io", "MEXC",
                 "Kraken", "Coinbase", "CoinGecko", "CoinPaprika"):
        assert _source_to_status(name) == PriceFeedStatus.LIVE

def test_source_to_status_file_cache():
    assert _source_to_status("file-cache") == PriceFeedStatus.FRESH_CACHE

def test_source_to_status_stale_cache():
    assert _source_to_status("cache") == PriceFeedStatus.STALE_CACHE

def test_source_to_status_fallback():
    assert _source_to_status("fallback") == PriceFeedStatus.UNAVAILABLE


# ---------------------------------------------------------------------------
# get_sol_usd() — return shape and status values
# ---------------------------------------------------------------------------

def _reset_mem_cache():
    pf._mem_price = 0.0
    pf._mem_ts    = 0.0
    pf._mem_source = "none"


def test_get_sol_usd_returns_three_tuple():
    result = get_sol_usd()
    assert len(result) == 3, "must return (price, status, source)"


def test_get_sol_usd_price_is_positive():
    price, status, source = get_sol_usd()
    assert price > 0


def test_get_sol_usd_status_is_pricefeedstatus():
    _, status, _ = get_sol_usd()
    assert isinstance(status, PriceFeedStatus)


def test_get_sol_usd_memory_cache_returns_live_or_fresh(monkeypatch):
    """In-memory cache hit preserves whatever status was set when it was filled."""
    _reset_mem_cache()
    pf._mem_price  = 180.0
    pf._mem_ts     = time.time()
    pf._mem_source = "Binance"
    price, status, source = get_sol_usd()
    assert price == pytest.approx(180.0)
    assert status == PriceFeedStatus.LIVE
    assert source == "Binance"


def test_get_sol_usd_fallback_returns_unavailable(monkeypatch, tmp_path):
    """When all sources fail and no file cache exists -> UNAVAILABLE + 150.0."""
    _reset_mem_cache()
    # Point cache file at an empty tmp dir (no cache file)
    monkeypatch.setattr(pf, "_CACHE_FILE", tmp_path / "no_such.json")
    # Make all fetchers raise
    monkeypatch.setattr(pf, "_SOURCES",
                        [(name, lambda: (_ for _ in ()).throw(OSError("fail")))
                         for name, _ in pf._SOURCES])
    price, status, source = get_sol_usd()
    assert price == pytest.approx(150.0)
    assert status == PriceFeedStatus.UNAVAILABLE
    assert source == "fallback"


def test_get_sol_usd_stale_cache_returned_when_live_fails(monkeypatch, tmp_path):
    """All live sources fail but file cache exists (no max_age limit) -> STALE_CACHE."""
    _reset_mem_cache()
    cache_file = tmp_path / "sol_price_cache.json"
    cache_file.write_text(
        json.dumps({"price": 175.0, "source": "Binance", "ts": time.time() - 3600}),
        encoding="utf-8",
    )
    monkeypatch.setattr(pf, "_CACHE_FILE", cache_file)
    # Fresh-cache check (< _FILE_CACHE_TTL) must NOT match the 1-hour-old entry
    monkeypatch.setattr(pf, "_FILE_CACHE_TTL", 60.0)  # 1 min; our entry is 1 h old
    monkeypatch.setattr(pf, "_SOURCES",
                        [(name, lambda: (_ for _ in ()).throw(OSError("fail")))
                         for name, _ in pf._SOURCES])
    price, status, source = get_sol_usd()
    assert price == pytest.approx(175.0)
    assert status == PriceFeedStatus.STALE_CACHE
    assert source == "cache"


def test_get_sol_usd_fresh_file_cache(monkeypatch, tmp_path):
    """Recent file cache (< _FILE_CACHE_TTL) -> FRESH_CACHE, skips live fetch."""
    _reset_mem_cache()
    cache_file = tmp_path / "sol_price_cache.json"
    cache_file.write_text(
        json.dumps({"price": 165.0, "source": "Bybit", "ts": time.time() - 10}),
        encoding="utf-8",
    )
    monkeypatch.setattr(pf, "_CACHE_FILE", cache_file)
    monkeypatch.setattr(pf, "_FILE_CACHE_TTL", 300.0)
    # Fetchers would fail, but fresh cache should be used before reaching them
    called = []
    monkeypatch.setattr(pf, "_SOURCES",
                        [("Binance", lambda: called.append(1) or (_ for _ in ()).throw(OSError()))])
    price, status, source = get_sol_usd()
    assert price == pytest.approx(165.0)
    assert status == PriceFeedStatus.FRESH_CACHE
    assert source == "file-cache"
    assert called == [], "live fetchers must not be called when file cache is fresh"


# ---------------------------------------------------------------------------
# Status sentinel semantics: STALE + UNAVAILABLE must not silently pass
# ---------------------------------------------------------------------------

def test_stale_and_unavailable_statuses_are_not_live():
    assert PriceFeedStatus.STALE_CACHE != PriceFeedStatus.LIVE
    assert PriceFeedStatus.UNAVAILABLE != PriceFeedStatus.LIVE


def test_status_values_are_strings():
    """PriceFeedStatus is a str Enum — safe to use in f-strings and JSON."""
    assert str(PriceFeedStatus.LIVE) == "PriceFeedStatus.LIVE"
    assert PriceFeedStatus.LIVE.value == "LIVE"
