"""
Multi-source SOL/USD price feed.
10 fallback sources tried in priority order, 3s timeout each.
In-memory cache TTL = 60s; file cache at data/sol_price_cache.json for cold starts.
"""
import json
import time
import urllib.request
from enum import Enum
from pathlib import Path
from typing import Tuple

_CACHE_FILE = Path(__file__).parent / "data" / "sol_price_cache.json"
_TTL = 60.0


class PriceFeedStatus(str, Enum):
    LIVE         = "LIVE"         # fetched from a live exchange this cycle
    FRESH_CACHE  = "FRESH_CACHE"  # file cache < 5 min (fast cold-start)
    STALE_CACHE  = "STALE_CACHE"  # all live sources failed; old file cache used
    UNAVAILABLE  = "UNAVAILABLE"  # no cache at all; hardcoded fallback returned


def _source_to_status(source: str) -> PriceFeedStatus:
    if source == "fallback":
        return PriceFeedStatus.UNAVAILABLE
    if source == "cache":
        return PriceFeedStatus.STALE_CACHE
    if source == "file-cache":
        return PriceFeedStatus.FRESH_CACHE
    return PriceFeedStatus.LIVE

_mem_price: float = 0.0
_mem_ts: float = 0.0
_mem_source: str = "none"


# ── Per-source fetchers ───────────────────────────────────────────────────────

def _fetch_binance() -> float:
    with urllib.request.urlopen(
        "https://api.binance.com/api/v3/ticker/price?symbol=SOLUSDT", timeout=3
    ) as r:
        return float(json.loads(r.read())["price"])


def _fetch_bybit() -> float:
    with urllib.request.urlopen(
        "https://api.bybit.com/v5/market/tickers?category=spot&symbol=SOLUSDT", timeout=3
    ) as r:
        data = json.loads(r.read())
        return float(data["result"]["list"][0]["lastPrice"])


def _fetch_okx() -> float:
    with urllib.request.urlopen(
        "https://www.okx.com/api/v5/market/ticker?instId=SOL-USDT", timeout=3
    ) as r:
        data = json.loads(r.read())
        return float(data["data"][0]["last"])


def _fetch_kucoin() -> float:
    with urllib.request.urlopen(
        "https://api.kucoin.com/api/v1/market/orderbook/level1?symbol=SOL-USDT", timeout=3
    ) as r:
        data = json.loads(r.read())
        return float(data["data"]["price"])


def _fetch_gateio() -> float:
    with urllib.request.urlopen(
        "https://api.gateio.ws/api/v4/spot/tickers?currency_pair=SOL_USDT", timeout=3
    ) as r:
        data = json.loads(r.read())
        return float(data[0]["last"])


def _fetch_mexc() -> float:
    with urllib.request.urlopen(
        "https://api.mexc.com/api/v3/ticker/price?symbol=SOLUSDT", timeout=3
    ) as r:
        return float(json.loads(r.read())["price"])


def _fetch_kraken() -> float:
    with urllib.request.urlopen(
        "https://api.kraken.com/0/public/Ticker?pair=SOLUSD", timeout=3
    ) as r:
        data = json.loads(r.read())
        return float(list(data["result"].values())[0]["c"][0])


def _fetch_coinbase() -> float:
    with urllib.request.urlopen(
        "https://api.coinbase.com/v2/prices/SOL-USD/spot", timeout=3
    ) as r:
        return float(json.loads(r.read())["data"]["amount"])


def _fetch_coingecko() -> float:
    with urllib.request.urlopen(
        "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd",
        timeout=3,
    ) as r:
        return float(json.loads(r.read())["solana"]["usd"])


def _fetch_coinpaprika() -> float:
    with urllib.request.urlopen(
        "https://api.coinpaprika.com/v1/tickers/sol-solana", timeout=3
    ) as r:
        data = json.loads(r.read())
        return float(data["quotes"]["USD"]["price"])


_SOURCES = [
    ("Binance",     _fetch_binance),
    ("Bybit",       _fetch_bybit),
    ("OKX",         _fetch_okx),
    ("KuCoin",      _fetch_kucoin),
    ("Gate.io",     _fetch_gateio),
    ("MEXC",        _fetch_mexc),
    ("Kraken",      _fetch_kraken),
    ("Coinbase",    _fetch_coinbase),
    ("CoinGecko",   _fetch_coingecko),
    ("CoinPaprika", _fetch_coinpaprika),
]


# ── Cache helpers ─────────────────────────────────────────────────────────────

_FILE_CACHE_TTL = 300.0  # use file cache if < 5 min old


def _load_file_cache(max_age: float = 0.0) -> float:
    """Return cached price. If max_age > 0, only return if cache is that fresh."""
    try:
        if _CACHE_FILE.exists():
            data = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
            price = float(data["price"])
            if max_age > 0:
                age = time.time() - float(data.get("ts", 0))
                if age > max_age:
                    return 0.0
            return price
    except Exception:
        pass
    return 0.0


def _save_file_cache(price: float, source: str) -> None:
    try:
        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_FILE.write_text(
            json.dumps({"price": price, "source": source, "ts": time.time()}),
            encoding="utf-8",
        )
    except Exception:
        pass


# ── Public API ────────────────────────────────────────────────────────────────

def get_sol_usd() -> Tuple[float, PriceFeedStatus, str]:
    """
    Returns (price_usd, status, source_name).

    Status guide:
      LIVE         -- freshly fetched from a live exchange
      FRESH_CACHE  -- file cache < 5 min (cold-start fast path)
      STALE_CACHE  -- all live sources failed; old file cache used
      UNAVAILABLE  -- no cache; hardcoded 150.0 returned

    Callers that size real positions MUST check status != LIVE before
    allowing an increase in stake.
    """
    global _mem_price, _mem_ts, _mem_source

    now = time.time()
    if _mem_price > 0 and now - _mem_ts < _TTL:
        return _mem_price, _source_to_status(_mem_source), _mem_source

    # Use file cache immediately if fresh enough -- prevents 30s hang on restart
    fresh = _load_file_cache(max_age=_FILE_CACHE_TTL)
    if fresh > 0:
        _mem_price  = fresh
        _mem_ts     = now
        _mem_source = "file-cache"
        return fresh, PriceFeedStatus.FRESH_CACHE, "file-cache"

    for name, fn in _SOURCES:
        try:
            price = fn()
            if price > 0:
                _mem_price  = price
                _mem_ts     = now
                _mem_source = name
                _save_file_cache(price, name)
                return price, PriceFeedStatus.LIVE, name
        except Exception:
            continue

    # All live sources failed -- use any file cache regardless of age
    cached = _load_file_cache(max_age=0.0)
    if cached > 0:
        _mem_price  = cached
        _mem_ts     = now
        _mem_source = "cache"
        return cached, PriceFeedStatus.STALE_CACHE, "cache"

    return 150.0, PriceFeedStatus.UNAVAILABLE, "fallback"
