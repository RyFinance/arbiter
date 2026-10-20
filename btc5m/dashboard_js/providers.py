"""External data providers for the dashboard: Massive + LSE + local scraper files.

Rules enforced here:
  * Massive free tier: hard token bucket of ``MASSIVE_CALLS_PER_MIN`` (5)
    requests per minute, shared by every Massive endpoint.  On exhaustion,
    callers get the stale cache when one exists, otherwise a quota error
    string.  The dashboard shows the quota state honestly.
  * LSE vault candles: one GET per symbol, cached ``LSE_TTL_S`` seconds.
  * Reddit / DeItaone feeds: read-only parsers over the real files produced
    by the existing scrapers in the sibling projects (project-sentiment with
    sentiment-oldft fallback).  These files are only updated when the
    scrapers run, so the payload carries the real last-run timestamp and a
    stale flag.
  * Keys come from ``dashboard_js.envs`` (the project .env); values are
    never logged or echoed.
"""
from __future__ import annotations

import json
import math
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from dashboard_js import envs

MASSIVE_BASE = "https://api.massive.com"
MASSIVE_CALLS_PER_MIN = 5
MASSIVE_WINDOW_S = 60.0
NEWS_TTL_S = 120.0
AGGS_TTL_S = 300.0
LSE_TTL_S = 300.0
REDDIT_TTL_S = 90.0
FINNHUB_TTL_S = 120.0
# All dashboard routes are sync `def`s sharing FastAPI's worker thread pool.
# A slow upstream provider used to be able to occupy a worker for up to 20s,
# which could queue an unrelated request (e.g. the loop bot's config-save or
# start POST) behind it long enough to feel broken. 6s is still generous for
# these market-data widgets while bounding worst-case thread-pool starvation.
REQUEST_TIMEOUT_S = 6.0

LSE_BASE = "https://api.londonstrategicedge.com"

CROSS_ASSET_SYMBOLS = ("BTC/USD", "ETH/USD", "XAU/USD", "EUR/USD", "GBP/USD", "AAPL", "TSLA", "NVDA")
HEATMAP_SYMBOLS = ("BTC/USD", "ETH/USD", "XAU/USD", "EUR/USD", "GBP/USD", "AAPL", "TSLA", "NVDA", "MSFT", "AMZN")
# Real sector groupings (every symbol verified live against the LSE vault
# candles endpoint on 2026-08-16; expanded watchlist per operator request).
SECTORS = (
    ("MACRO / CROSS-ASSET", ("BTC/USD", "ETH/USD", "XAU/USD", "EUR/USD", "GBP/USD", "USD/JPY", "AUD/USD", "USD/CAD")),
    ("SEMICONDUCTORS", ("NVDA", "AMD", "AVGO", "QCOM", "INTC", "MU", "MRVL", "LRCX", "KLAC", "AMAT", "TXN")),
    ("TECH", ("MSFT", "AAPL", "GOOGL", "META", "NFLX", "CRM", "ORCL", "ADBE", "PLTR")),
    ("EV / AUTO", ("TSLA", "F", "GM", "RIVN", "LCID")),
    ("FINANCIALS", ("JPM", "GS", "BAC", "WFC", "MS", "C", "SCHW", "AXP")),
    ("ENERGY", ("XOM", "CVX", "COP", "SLB", "EOG", "MPC")),
    ("HEALTHCARE", ("PFE", "JNJ", "MRK", "UNH", "LLY", "ABBV", "BMY", "TMO", "ABT")),
    ("INDUSTRIALS", ("CAT", "DE", "BA", "LMT", "RTX", "GE", "HON", "UPS", "FDX", "UNP")),
    ("MINERALS / MATERIALS", ("NEM", "FCX", "AA")),
    ("CONSUMER", ("KO", "PEP", "WMT", "COST", "HD", "NKE", "MCD", "SBUX", "DIS", "UBER", "PG", "TGT", "LOW", "BKNG")),
)
# Finnhub symbol mapping for the macro/cross-asset group (stocks pass through).
FINNHUB_MAP = {
    "BTC/USD": ("crypto", "BINANCE:BTCUSDT"),
    "ETH/USD": ("crypto", "BINANCE:ETHUSDT"),
    "XAU/USD": ("forex", "OANDA:XAU_USD"),
    "EUR/USD": ("forex", "OANDA:EUR_USD"),
    "GBP/USD": ("forex", "OANDA:GBP_USD"),
    "USD/JPY": ("forex", "OANDA:USD_JPY"),
    "AUD/USD": ("forex", "OANDA:AUD_USD"),
    "USD/CAD": ("forex", "OANDA:USD_CAD"),
}
INDEX_ETFS = (("SPY", "S&P 500 ETF proxy"), ("DIA", "Dow Jones ETF proxy"), ("QQQ", "Nasdaq-100 ETF proxy"), ("IWM", "Russell 2000 ETF proxy"))
NEWS_TICKERS = ("NVDA", "TSLA", "AAPL", "MSFT", "SPY", "QQQ")
_NEWS_ROTATION: Dict[str, Any] = {"lock": threading.Lock(), "index": 0}

SENTIMENT_DIR = Path("~/project-sentiment")
OLD_SENTIMENT_DIR = Path("~/sentiment-oldft")

# --------------------------------------------------------------------------
# Massive: token bucket + tiny REST client
# --------------------------------------------------------------------------

_BUCKET: Dict[str, Any] = {"lock": threading.Lock(), "stamps": []}
_CACHE: Dict[str, Dict[str, Any]] = {}


def _bucket_allow() -> bool:
    """Consume one Massive request slot; False when the minute quota is used up."""
    now = time.monotonic()
    with _BUCKET["lock"]:
        stamps = [stamp for stamp in _BUCKET["stamps"] if now - stamp < MASSIVE_WINDOW_S]
        _BUCKET["stamps"] = stamps
        if len(stamps) >= MASSIVE_CALLS_PER_MIN:
            return False
        _BUCKET["stamps"].append(now)
        return True


def bucket_state() -> Dict[str, Any]:
    """Current quota state: remaining calls in this minute, next free slot."""
    now = time.monotonic()
    with _BUCKET["lock"]:
        stamps = [stamp for stamp in _BUCKET["stamps"] if now - stamp < MASSIVE_WINDOW_S]
        _BUCKET["stamps"] = stamps
        remaining = max(0, MASSIVE_CALLS_PER_MIN - len(stamps))
        next_free = None
        if stamps and remaining == 0:
            next_free = round(max(0.0, MASSIVE_WINDOW_S - (now - stamps[0])), 1)
        return {
            "limit_per_min": MASSIVE_CALLS_PER_MIN,
            "remaining_this_minute": remaining,
            "next_free_in_s": next_free,
        }


def _cache_get(key: str) -> Optional[Dict[str, Any]]:
    entry = _CACHE.get(key)
    if not entry:
        return None
    return entry.get("value")


def _cache_put(key: str, value: Any, ttl: float) -> None:
    _CACHE[key] = {"value": value, "expires": time.monotonic() + ttl}


def _cache_fresh(key: str) -> bool:
    entry = _CACHE.get(key)
    return bool(entry and entry.get("expires", 0) > time.monotonic())


def _http_get_json(url: str, headers: Optional[Dict[str, str]] = None) -> Tuple[Optional[Any], Optional[str]]:
    """Return (parsed JSON, error). Never raises."""
    request = urllib.request.Request(url, headers={"User-Agent": "pm-dashboard/1.0", **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_S) as response:
            body = response.read().decode("utf-8", errors="replace")
            return json.loads(body), None
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
        except OSError:
            pass
        return None, f"HTTP {exc.code} {detail}"
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"


def _http_get_text(url: str, headers: Optional[Dict[str, str]] = None) -> Tuple[Optional[str], Optional[str]]:
    """Return (raw text body, error). Never raises."""
    request = urllib.request.Request(url, headers={"User-Agent": "pm-dashboard/1.0", **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_S) as response:
            return response.read().decode("utf-8", errors="replace"), None
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
        except OSError:
            pass
        return None, f"HTTP {exc.code} {detail}"
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"


def massive_get(path: str, params: Dict[str, str], cache_key: str, ttl: float) -> Tuple[Optional[Any], Optional[str], Dict[str, Any]]:
    """Cached + quota-limited Massive GET.

    Returns (data, error, quota) where quota carries the bucket state.  When
    the quota is exhausted, a stale cache entry (if any) is still returned so
    panels degrade to real-but-stale data instead of erroring.
    """
    quota = bucket_state()
    api_key = envs.massive_api_key()
    if not api_key:
        return None, "MASSIVE_API_KEY not set in .env", quota
    if _cache_fresh(cache_key):
        return _cache_get(cache_key), None, quota
    stale = _cache_get(cache_key)
    if not _bucket_allow():
        quota = bucket_state()
        return stale, None if stale is not None else "Massive rate limit: 5 calls/min exhausted; retry later", quota
    query = urllib.parse.urlencode({**params, "apiKey": api_key})
    data, error = _http_get_json(f"{MASSIVE_BASE}{path}?{query}")
    if error:
        if stale is not None:
            return stale, f"live fetch failed ({error}); serving cached", bucket_state()
        return None, error, bucket_state()
    _cache_put(cache_key, data, ttl)
    return data, None, bucket_state()


# --------------------------------------------------------------------------
# Massive-derived payloads
# --------------------------------------------------------------------------

def massive_news() -> Dict[str, Any]:
    """Live Massive headlines, one ticker per call.

    The v2 reference-news endpoint returns an empty result list for
    comma-separated ticker lists, so headlines are collected one ticker per
    call (rotating through ``NEWS_TICKERS``), cached per ticker, and merged
    newest-first.  This also keeps the free-tier 5-calls/minute bucket
    happy: one refresh consumes exactly one slot.
    """
    payload: Dict[str, Any] = {"source": "massive.com v2 reference news (per-ticker rotation)", "items": [], "quota": None}
    with _NEWS_ROTATION["lock"]:
        ticker = NEWS_TICKERS[_NEWS_ROTATION["index"] % len(NEWS_TICKERS)]
        _NEWS_ROTATION["index"] += 1
    cache_key = f"massive_news_{ticker}"
    data, error, quota = massive_get(
        "/v2/reference/news",
        {"ticker": ticker, "limit": "10"},
        cache_key=cache_key,
        ttl=NEWS_TTL_S * 3,
    )
    payload["quota"] = quota
    if error:
        payload["error"] = error
    seen: set = set()
    collected: List[Dict[str, Any]] = []
    for cached_ticker in NEWS_TICKERS:
        cached = _cache_get(f"massive_news_{cached_ticker}")
        if not isinstance(cached, dict):
            continue
        for row in cached.get("results", []):
            if not isinstance(row, dict) or not row.get("title"):
                continue
            row_id = str(row.get("id", ""))
            if row_id in seen:
                continue
            seen.add(row_id)
            publisher = row.get("publisher") or {}
            collected.append(
                {
                    "id": row_id,
                    "ts_utc": row.get("published_utc"),
                    "title": str(row["title"]),
                    "publisher": publisher.get("name") if isinstance(publisher, dict) else None,
                    "url": row.get("article_url"),
                    "tickers": row.get("tickers") if isinstance(row.get("tickers"), list) else [],
                }
            )
    collected.sort(key=lambda item: item["ts_utc"] or "", reverse=True)
    payload["items"] = collected[:30]
    payload["last_ticker_fetched"] = ticker
    return payload


def _aggs_from_bars(bars: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Latest close + day-over-day change from a Massive bars array."""
    closes = [float(row["c"]) for row in bars if isinstance(row, dict) and row.get("c") is not None]
    if not closes:
        return {"close": None, "change_pct": None, "ts_utc": None}
    latest = closes[-1]
    previous = closes[-2] if len(closes) >= 2 else closes[0]
    change_pct = ((latest - previous) / previous * 100.0) if previous else None
    ts_ms = None
    if bars and isinstance(bars[-1], dict) and isinstance(bars[-1].get("t"), (int, float)):
        ts_ms = int(bars[-1]["t"])
    ts_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts_ms / 1000.0)) if ts_ms else None
    return {"close": latest, "change_pct": change_pct, "ts_utc": ts_utc}


def massive_index_proxies() -> Dict[str, Any]:
    """Index-ETF day change via Massive v2 aggs (free tier is NOT entitled to
    the real index snapshot endpoint; the note says so honestly)."""
    payload: Dict[str, Any] = {
        "source": "massive.com v2 aggs (ETF proxies — free tier has no v3 index snapshot entitlement)",
        "items": [],
        "quota": None,
    }
    to_date = time.strftime("%Y-%m-%d")
    from_ms = int((time.time() - 30 * 86400) * 1000)
    for symbol, label in INDEX_ETFS:
        path = f"/v2/aggs/ticker/{symbol}/range/1/day/{from_ms}/{to_date}"
        data, error, quota = massive_get(
            path,
            {"limit": "60", "sort": "asc"},
            cache_key=f"massive_aggs_{symbol}",
            ttl=AGGS_TTL_S,
        )
        payload["quota"] = quota
        if error:
            payload.setdefault("errors", []).append(f"{symbol}: {error}")
            continue
        bars = data.get("results", []) if isinstance(data, dict) else []
        agg = _aggs_from_bars(bars)
        payload["items"].append(
            {"symbol": symbol, "name": label, "close": agg["close"], "change_pct": agg["change_pct"], "ts_utc": agg["ts_utc"]}
        )
    return payload


# --------------------------------------------------------------------------
# LSE vault candles
# --------------------------------------------------------------------------

_LSE_INFLIGHT: Dict[str, threading.Event] = {}
_LSE_INFLIGHT_LOCK = threading.Lock()


def lse_daily_candles(symbol: str, days: int = 60) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    api_key = envs.lse_api_key()
    if not api_key:
        return None, "LSE_API_KEY not set in .env"
    cache_key = f"lse_{symbol}"
    if _cache_fresh(cache_key):
        return _cache_get(cache_key), None
    # Deduplicate concurrent fetches of the same symbol across endpoints
    # (sector heatmap, cross-asset, and chart clicks can all ask at once).
    with _LSE_INFLIGHT_LOCK:
        event = _LSE_INFLIGHT.get(cache_key)
        if event is None:
            event = threading.Event()
            _LSE_INFLIGHT[cache_key] = event
            own = True
        else:
            own = False
    if not own:
        event.wait(timeout=REQUEST_TIMEOUT_S)
        if _cache_fresh(cache_key):
            return _cache_get(cache_key), None
        return None, "lse fetch in progress timed out"
    end = time.strftime("%Y-%m-%d")
    start = time.strftime("%Y-%m-%d", time.gmtime(time.time() - days * 86400))
    query = urllib.parse.urlencode({"symbol": symbol, "timeframe": "1d", "start": start, "end": end})
    data, error = _http_get_json(f"{LSE_BASE}/vault/candles?{query}", headers={"x-api-key": api_key})
    rows: List[Dict[str, Any]] = []
    if not error:
        for row in data if isinstance(data, list) else []:
            if not isinstance(row, dict) or row.get("close") is None:
                continue
            ts_text = str(row.get("ts", ""))[:10]
            try:
                epoch = time.mktime(time.strptime(ts_text, "%Y-%m-%d")) if ts_text else None
            except ValueError:
                epoch = None
            rows.append({"ts": ts_text, "ts_epoch": epoch, "close": float(row["close"])})
        _cache_put(cache_key, rows, LSE_TTL_S)
    with _LSE_INFLIGHT_LOCK:
        _LSE_INFLIGHT.pop(cache_key, None)
        event.set()
    return rows if not error else None, error


def _pearson(xs: List[float], ys: List[float]) -> Optional[float]:
    if len(xs) != len(ys) or len(xs) < 3:
        return None
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    den_x = math.sqrt(sum((x - mean_x) ** 2 for x in xs))
    den_y = math.sqrt(sum((y - mean_y) ** 2 for y in ys))
    if den_x == 0 or den_y == 0:
        return None
    return num / (den_x * den_y)


def lse_cross_asset() -> Dict[str, Any]:
    """Last close, day change, and 30d correlation vs BTC/USD over LSE daily candles."""
    payload: Dict[str, Any] = {"source": "londonstrategicedge.com vault candles (1d)", "items": [], "matrix": None}
    btc_rows, btc_error = lse_daily_candles("BTC/USD", days=60)
    if btc_error or not btc_rows:
        payload["error"] = btc_error or "no BTC/USD candles"
        return payload
    btc_by_ts = {row["ts"]: row["close"] for row in btc_rows}
    btc_closes = [row["close"] for row in btc_rows]
    btc_prev = btc_closes[-2] if len(btc_closes) >= 2 else btc_closes[0]
    btc_change = ((btc_closes[-1] - btc_prev) / btc_prev * 100.0) if btc_prev else None
    payload["items"].append(
        {
            "symbol": "BTC/USD",
            "close": btc_closes[-1],
            "change_pct": btc_change,
            "corr_vs_btc_30d": 1.0,
            "ts": btc_rows[-1]["ts"],
        }
    )
    corr_pairs: Dict[str, Optional[float]] = {"BTC/USD": 1.0}
    for symbol in CROSS_ASSET_SYMBOLS[1:]:
        rows, error = lse_daily_candles(symbol, days=60)
        if error or not rows:
            payload.setdefault("errors", []).append(f"{symbol}: {error or 'no candles'}")
            corr_pairs[symbol] = None
            continue
        by_ts = {row["ts"]: row["close"] for row in rows}
        common = sorted(set(btc_by_ts) & set(by_ts))[-30:]
        corr = _pearson([btc_by_ts[ts] for ts in common], [by_ts[ts] for ts in common]) if common else None
        corr_pairs[symbol] = corr
        closes = [row["close"] for row in rows]
        previous = closes[-2] if len(closes) >= 2 else closes[0]
        change = ((closes[-1] - previous) / previous * 100.0) if previous else None
        payload["items"].append(
            {"symbol": symbol, "close": closes[-1], "change_pct": change, "corr_vs_btc_30d": corr, "ts": rows[-1]["ts"]}
        )
    payload["matrix"] = {
        "pairs": corr_pairs,
        "window": "last 30 common trading days",
        "note": "Pearson correlation of daily closes vs BTC/USD",
    }
    return payload


def _lse_many(symbols: List[str]) -> Dict[str, Tuple[List[Dict[str, Any]], Optional[str]]]:
    """Fetch several LSE symbols in parallel (bounded pool + 429 retries).

    The LSE vault rate-limits concurrent requests ("too many concurrent
    requests for this key"), so the pool is small and 429s retry with
    backoff before giving up.
    """
    from concurrent.futures import ThreadPoolExecutor

    results: Dict[str, Tuple[List[Dict[str, Any]], Optional[str]]] = {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(lse_daily_candles, symbol, 60): symbol for symbol in symbols}
        for future in futures:
            symbol = futures[future]
            try:
                rows, error = future.result(timeout=REQUEST_TIMEOUT_S)
            except Exception as exc:  # noqa: BLE001
                rows, error = None, f"{type(exc).__name__}: {exc}"
            attempt = 0
            while error and "429" in error and attempt < 3:
                attempt += 1
                time.sleep(0.8 * attempt)
                try:
                    rows, error = lse_daily_candles(symbol, 60)
                except Exception as exc:  # noqa: BLE001
                    rows, error = None, f"{type(exc).__name__}: {exc}"
            results[symbol] = (rows, error)
    return results


def _day_change(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    closes = [row["close"] for row in rows]
    previous = closes[-2] if len(closes) >= 2 else closes[0]
    change = ((closes[-1] - previous) / previous * 100.0) if previous else None
    return {"close": closes[-1], "change_pct": change, "ts": rows[-1]["ts"]}


def lse_heatmap() -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "source": "londonstrategicedge.com vault candles (1d day change %)",
        "items": [],
        "sectors": [],
    }
    all_symbols = list(dict.fromkeys([*HEATMAP_SYMBOLS, *(s for _, symbols in SECTORS for s in symbols)]))
    fetched = _lse_many(all_symbols)
    for symbol in HEATMAP_SYMBOLS:
        rows, error = fetched.get(symbol, (None, "no fetch"))
        if error or not rows:
            payload.setdefault("errors", []).append(f"{symbol}: {error or 'no candles'}")
            continue
        payload["items"].append({"symbol": symbol, **_day_change(rows)})

    for sector_name, symbols in SECTORS:
        sector_items = []
        changes = []
        for symbol in symbols:
            rows, error = fetched.get(symbol, (None, "no fetch"))
            if error or not rows:
                payload.setdefault("errors", []).append(f"{symbol}: {error or 'no candles'}")
                continue
            change_row = _day_change(rows)
            sector_items.append({"symbol": symbol, **change_row})
            if change_row["change_pct"] is not None:
                changes.append(change_row["change_pct"])
        if not sector_items:
            continue
        payload["sectors"].append(
            {
                "sector": sector_name,
                "change_pct": sum(changes) / len(changes) if changes else None,
                "count": len(sector_items),
                "items": sector_items,
            }
        )
    return payload


def finnhub_quote(symbol: str) -> Dict[str, Any]:
    """Current-quote enrichment from Finnhub (free tier: /quote works, /stock/candle 403s).

    Verified 2026-08-16: stock and crypto quote endpoints are entitled;
    forex endpoints return 403, so FX symbols skip the call and fall back
    to the LSE series close instead.
    """
    payload: Dict[str, Any] = {"symbol": symbol, "quote": None, "source": None}
    api_key = envs.finnhub_api_key()
    if not api_key:
        payload["error"] = "FINNHUB_API_KEY not set in .env"
        return payload
    category, finnhub_symbol = FINNHUB_MAP.get(symbol, ("stock", symbol))
    if category == "forex":
        payload["error"] = "finnhub forex not entitled on this key (403); chart uses the LSE series close"
        return payload
    query = urllib.parse.urlencode({"symbol": finnhub_symbol, "token": api_key})
    data, error = _http_get_json(f"https://finnhub.io/api/v1/quote?{query}")
    if error:
        payload["error"] = error
        return payload
    if not isinstance(data, dict) or data.get("c") is None:
        payload["error"] = f"finnhub returned no quote for {finnhub_symbol}"
        return payload
    payload["quote"] = data
    payload["source"] = f"finnhub.io quote ({finnhub_symbol})"
    return payload


def finnhub_candles(symbol: str, days: int = 60) -> Optional[List[Dict[str, Any]]]:
    """Daily candle series from Finnhub /stock/candle.

    The free key is NOT entitled to this endpoint for some accounts (403),
    so callers must fall back to the LSE/Massive series.  Returns None on
    any failure so the fallback path stays the default.
    """
    api_key = envs.finnhub_api_key()
    if not api_key:
        return None
    category, finnhub_symbol = FINNHUB_MAP.get(symbol, ("stock", symbol))
    if category != "stock":
        return None
    to_ts = int(time.time())
    from_ts = to_ts - days * 86400
    query = urllib.parse.urlencode(
        {"symbol": finnhub_symbol, "resolution": "D", "from": str(from_ts), "to": str(to_ts), "token": api_key}
    )
    data, error = _http_get_json(f"https://finnhub.io/api/v1/stock/candle?{query}")
    if error or not isinstance(data, dict) or data.get("s") != "ok":
        return None
    closes = data.get("c") or []
    timestamps = data.get("t") or []
    series = [
        {"ts_utc": time.strftime("%Y-%m-%d", time.gmtime(float(ts))), "close": float(close)}
        for ts, close in zip(timestamps, closes)
        if isinstance(ts, (int, float)) and isinstance(close, (int, float))
    ]
    return series or None


def finnhub_search(query: str) -> Dict[str, Any]:
    """Symbol search against Finnhub /search (free tier entitled)."""
    payload: Dict[str, Any] = {"query": query, "items": []}
    api_key = envs.finnhub_api_key()
    if not api_key:
        payload["error"] = "FINNHUB_API_KEY not set in .env"
        return payload
    if not query or not query.strip():
        return payload
    params = urllib.parse.urlencode({"q": query.strip()[:64], "token": api_key})
    data, error = _http_get_json(f"https://finnhub.io/api/v1/search?{params}")
    if error:
        payload["error"] = error
        return payload
    if not isinstance(data, dict):
        payload["error"] = "unexpected finnhub search payload"
        return payload
    for result in (data.get("result") or [])[:12]:
        if not isinstance(result, dict):
            continue
        symbol = str(result.get("symbol") or "")
        if not symbol or "." in symbol or ":" in symbol:
            continue
        payload["items"].append(
            {
                "symbol": symbol,
                "description": str(result.get("description") or "")[:80],
                "type": str(result.get("type") or ""),
            }
        )
    return payload


# --------------------------------------------------------------------------
# OHLCV chart data + company news (yfinance primary, Finnhub/LSE fallback)
# --------------------------------------------------------------------------

# yfinance interval -> (period, interval) for the chart workspace.
OHLCV_RESOLUTIONS: Dict[str, tuple[str, str]] = {
    "1m": ("1d", "1m"),
    "5m": ("5d", "5m"),
    "15m": ("1mo", "15m"),
    "30m": ("1mo", "30m"),
    "1h": ("3mo", "1h"),
    "4h": ("3mo", "4h"),
    "1d": ("2y", "1d"),
    "1w": ("5y", "1wk"),
    "1mo": ("max", "1mo"),
}

def _yf_resolution_name(resolution: str) -> Optional[str]:
    """Map dashboard resolution names onto yfinance names."""
    if resolution in OHLCV_RESOLUTIONS:
        return OHLCV_RESOLUTIONS[resolution][1]
    return None


def yf_ohlcv(symbol: str, resolution: str = "1d", limit: int = 500) -> Dict[str, Any]:
    """OHLCV candles via yfinance (no API key; primary chart source).

    Returns ``{"items": [{t, o, h, l, c, v}], "source": ..., "meta": {...}}``
    or an error dict. Never raises.
    """
    payload: Dict[str, Any] = {"items": [], "source": None, "error": None}
    try:
        import yfinance as yf
    except ImportError:
        payload["error"] = "yfinance not installed"
        return payload
    interval = _yf_resolution_name(resolution)
    if interval is None:
        payload["error"] = f"unsupported resolution: {resolution}"
        return payload
    period = OHLCV_RESOLUTIONS[resolution][0]
    try:
        df = yf.download(symbol, period=period, interval=interval, progress=False, auto_adjust=False)
    except Exception as exc:  # noqa: BLE001
        payload["error"] = f"yfinance fetch failed: {type(exc).__name__}: {exc}"
        return payload
    if df is None or len(df) == 0:
        payload["error"] = f"yfinance returned no data for {symbol} ({interval})"
        return payload
    try:
        # yfinance returns MultiIndex columns like ("Open", "NVDA"); flatten
        # to single level so row["Open"] works regardless of ticker suffix.
        if isinstance(df.columns, type(df.index)) or hasattr(df.columns, "levels"):
            df.columns = [str(c[0]) if isinstance(c, tuple) else str(c) for c in df.columns]
        items = []
        for idx, row in df.iterrows():
            ts = int(idx.timestamp())
            try:
                o = float(row["Open"])
                high = float(row["High"])
                low = float(row["Low"])
                c = float(row["Close"])
                v = float(row["Volume"])
            except (TypeError, ValueError, KeyError):
                continue
            if o is None or high is None or low is None or c is None:
                continue
            items.append({"t": ts, "o": o, "h": high, "l": low, "c": c, "v": v})
    except Exception as exc:  # noqa: BLE001
        payload["error"] = f"yfinance parse failed: {type(exc).__name__}: {exc}"
        return payload
    if not items:
        payload["error"] = f"yfinance returned empty candles for {symbol} ({interval})"
        return payload
    payload["items"] = items[-max(1, min(limit, 2000)):]
    payload["source"] = "yfinance (no API key)"
    payload["meta"] = {"resolution": resolution, "interval": interval, "symbol": symbol}
    return payload


def finnhub_company_news(symbol: str, days: int = 3) -> Dict[str, Any]:
    """Company/ETF news via Finnhub /company-news (user's key is entitled)."""
    payload: Dict[str, Any] = {"items": [], "source": None, "error": None}
    api_key = envs.finnhub_api_key()
    if not api_key:
        payload["error"] = "FINNHUB_API_KEY not set in .env"
        return payload
    to_ts = time.time()
    from_ts = to_ts - days * 86400
    params = urllib.parse.urlencode({
        "symbol": symbol,
        "from": time.strftime("%Y-%m-%d", time.gmtime(from_ts)),
        "to": time.strftime("%Y-%m-%d", time.gmtime(to_ts)),
        "token": api_key,
    })
    data, error = _http_get_json(f"https://finnhub.io/api/v1/company-news?{params}")
    if error:
        payload["error"] = error
        return payload
    if not isinstance(data, list):
        payload["error"] = "unexpected finnhub company-news payload"
        return payload
    for article in data[:40]:
        if not isinstance(article, dict):
            continue
        headline = str(article.get("headline") or "")
        if not headline:
            continue
        ts = article.get("datetime")
        payload["items"].append({
            "ts_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(ts))) if isinstance(ts, (int, float)) else None,
            "headline": headline,
            "summary": str(article.get("summary") or "")[:300],
            "source": str(article.get("source") or "finnhub"),
            "url": article.get("url"),
            "image": article.get("image"),
        })
    payload["source"] = "finnhub.io company news"
    return payload


# --------------------------------------------------------------------------
# BTC sentiment proxy — real, labeled, never fabricated
# --------------------------------------------------------------------------

_BTC_SENTIMENT_TTL_S = 300.0


def btc_sentiment_proxy() -> Dict[str, Any]:
    """Fear/greed-style BTC gauge built from REAL market data.

    The sentiment-project composite (sentiment_results.json) is per-ticker
    social scoring with no BTC row and is stale, so this gauge derives a
    transparent proxy from yfinance BTC-USD daily closes — the same inputs
    the published "Crypto Fear & Greed" methodology uses (volatility,
    momentum, distance from moving average). Labeled as a proxy, never as
    survey sentiment.
    """
    payload: Dict[str, Any] = {
        "score": None, "label": None, "components": {}, "price": None,
        "source": "proxy: yfinance BTC-USD daily closes (momentum + SMA distance + realized vol)",
        "error": None,
    }
    cache_key = "btc_sentiment_proxy"
    if _cache_fresh(cache_key):
        cached = _cache_get(cache_key)
        return cached if isinstance(cached, dict) else payload
    try:
        import yfinance as yf

        df = yf.download("BTC-USD", period="3mo", interval="1d", progress=False, auto_adjust=False)
    except Exception as exc:  # noqa: BLE001
        payload["error"] = f"yfinance fetch failed: {type(exc).__name__}: {exc}"
        return payload
    if df is None or len(df) < 40:
        payload["error"] = "not enough BTC-USD history for the proxy"
        return payload
    try:
        if isinstance(df.columns, type(df.index)) or hasattr(df.columns, "levels"):
            df.columns = [str(c[0]) if isinstance(c, tuple) else str(c) for c in df.columns]
        closes = [float(v) for v in df["Close"].values]
    except (KeyError, TypeError):
        payload["error"] = "BTC-USD close series unreadable"
        return payload
    closes = [c for c in closes if c == c][-60:]  # drop NaN, last 60d
    if len(closes) < 40:
        payload["error"] = "not enough BTC-USD history for the proxy"
        return payload
    px = closes[-1]
    # momentum: % change vs 30d ago → 0..100
    mom30 = (px - closes[-31]) / closes[-31] * 100.0 if len(closes) > 31 else 0.0
    # distance from 50d SMA → 0..100
    sma50 = sum(closes[-50:]) / min(50, len(closes))
    sma_dist = (px / sma50 - 1.0) * 100.0
    # realized volatility (annualized) → 0..100
    rets = [(closes[i] - closes[i - 1]) / closes[i - 1] for i in range(max(1, len(closes) - 14), len(closes))]
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / len(rets)
    vol_ann = var ** 0.5 * (365 ** 0.5) * 100.0

    def _clamp(v: float) -> float:
        return max(0.0, min(100.0, v))

    c_mom = _clamp(50.0 + mom30 * 2.0)
    c_sma = _clamp(50.0 + sma_dist * 5.0)
    c_vol = _clamp(100.0 - vol_ann * 1.8)
    score = round(0.45 * c_mom + 0.35 * c_sma + 0.20 * c_vol, 1)
    if score >= 75:
        label = "EXTREME GREED"
    elif score >= 55:
        label = "GREED"
    elif score > 45:
        label = "NEUTRAL"
    elif score > 25:
        label = "FEAR"
    else:
        label = "EXTREME FEAR"
    payload.update(
        {
            "score": score,
            "label": label,
            "price": round(px, 2),
            "components": {
                "momentum_30d_pct": round(mom30, 2),
                "sma50_dist_pct": round(sma_dist, 2),
                "realized_vol_ann_pct": round(vol_ann, 1),
                "c_momentum": round(c_mom, 1),
                "c_sma": round(c_sma, 1),
                "c_vol": round(c_vol, 1),
            },
        }
    )
    payload["updated_at_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _cache_put(cache_key, payload, _BTC_SENTIMENT_TTL_S)
    return payload


def finnhub_news() -> Dict[str, Any]:
    """World news from Finnhub /news?category=general (cached)."""
    payload: Dict[str, Any] = {"items": [], "source": None}
    api_key = envs.finnhub_api_key()
    if not api_key:
        payload["error"] = "FINNHUB_API_KEY not set in .env"
        return payload
    cache_key = "finnhub_news_general"
    if _cache_fresh(cache_key):
        cached = _cache_get(cache_key)
        return cached if isinstance(cached, dict) else payload
    params = urllib.parse.urlencode({"category": "general", "token": api_key})
    data, error = _http_get_json(f"https://finnhub.io/api/v1/news?{params}")
    if error:
        payload["error"] = error
        return payload
    for article in (data or [])[:15]:
        if not isinstance(article, dict):
            continue
        ts = article.get("datetime")
        payload["items"].append(
            {
                "title": str(article.get("headline") or ""),
                "publisher": str(article.get("source") or "finnhub"),
                "url": article.get("url"),
                "ts_utc": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(float(ts))) if isinstance(ts, (int, float)) else None,
            }
        )
    payload["source"] = "finnhub.io general news"
    _cache_put(cache_key, payload, FINNHUB_TTL_S)
    return payload


def _massive_series(symbol: str) -> Optional[List[Dict[str, Any]]]:
    """Close series for a symbol from cached Massive v2 aggs (ETF/stock proxies)."""
    cache_key = f"massive_aggs_{symbol}"
    if not _cache_fresh(cache_key):
        to_date = time.strftime("%Y-%m-%d")
        from_ms = int((time.time() - 30 * 86400) * 1000)
        path = f"/v2/aggs/ticker/{symbol}/range/1/day/{from_ms}/{to_date}"
        data, error, _ = massive_get(path, {"limit": "60", "sort": "asc"}, cache_key=cache_key, ttl=AGGS_TTL_S)
        if error or not isinstance(data, dict):
            return None
    bars = _cache_get(cache_key).get("results", []) if isinstance(_cache_get(cache_key), dict) else []
    series = []
    for bar in bars:
        if not isinstance(bar, dict) or bar.get("c") is None:
            continue
        ts_ms = bar.get("t")
        series.append(
            {
                "ts_utc": time.strftime("%Y-%m-%d", time.gmtime(int(ts_ms) / 1000.0)) if isinstance(ts_ms, (int, float)) else None,
                "close": float(bar["c"]),
            }
        )
    return series or None


def stock_chart(symbol: str, days: int = 60) -> Dict[str, Any]:
    """Click-through chart: Finnhub daily candles when entitled, else LSE vault
    candles (+ Massive aggs fallback), enriched with the Finnhub live quote.
    """
    payload: Dict[str, Any] = {"symbol": symbol, "items": [], "quote": None, "sources": []}
    candles = finnhub_candles(symbol, days=days)
    if candles:
        payload["items"] = candles
        payload["sources"].append("finnhub.io candles (1d)")
    else:
        rows, error = lse_daily_candles(symbol, days=days)
        if error or not rows:
            fallback = _massive_series(symbol)
            if fallback:
                payload["items"] = fallback
                payload["sources"].append("massive.com v2 aggs (1d)")
            else:
                payload["error"] = error or "no chart series available (finnhub, LSE and Massive all unavailable)"
                return payload
        else:
            payload["items"] = [{"ts_utc": row["ts"], "close": row["close"]} for row in rows]
            payload["sources"].append("londonstrategicedge.com vault candles (1d)")
    closes = [row["close"] for row in payload["items"]]
    previous = closes[-2] if len(closes) >= 2 else closes[0]
    payload["change_pct"] = ((closes[-1] - previous) / previous * 100.0) if previous else None
    quote = finnhub_quote(symbol)
    if quote.get("quote"):
        payload["quote"] = quote["quote"]
        payload["quote"]["_source"] = "finnhub"
        payload["sources"].append(quote.get("source"))
    else:
        payload.setdefault("quote_error", quote.get("error"))
        payload["quote"] = {
            "c": closes[-1],
            "d": closes[-1] - previous if previous else None,
            "dp": payload["change_pct"],
            "_source": "lse_series",
        }
    return payload


# --------------------------------------------------------------------------
# Local scraper outputs (real files, real formats)
# --------------------------------------------------------------------------

def _first_existing(paths: List[Path]) -> Optional[Path]:
    for path in paths:
        if path.is_file():
            return path
    return None


def read_reddit_feed() -> Dict[str, Any]:
    payload: Dict[str, Any] = {"items": [], "source_file": None, "last_run_utc": None, "stale": None}
    path = _first_existing([SENTIMENT_DIR / "raw_data.json", OLD_SENTIMENT_DIR / "raw_data.json"])
    if path is None:
        payload["error"] = "no reddit scraper output found (checked project-sentiment and sentiment-oldft)"
        return payload
    payload["source_file"] = str(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        payload["error"] = f"unreadable scraper output: {type(exc).__name__}"
        return payload
    scraped_at = data.get("scraped_at")
    if scraped_at:
        payload["last_run_utc"] = str(scraped_at)
    posts = data.get("posts", []) if isinstance(data, dict) else []
    for post in posts:
        if not isinstance(post, dict):
            continue
        try:
            score = int(post.get("score") or 0)
        except (TypeError, ValueError):
            score = 0
        payload["items"].append(
            {
                "id": str(post.get("id", "")),
                "title": str(post.get("title", "")),
                "subreddit": str(post.get("subreddit", "")),
                "score": score,
                "num_comments": post.get("num_comments"),
                "created_utc": post.get("created_utc"),
            }
        )
    payload["items"].sort(key=lambda item: item["score"], reverse=True)
    payload["items"] = payload["items"][:40]
    payload["total_posts"] = data.get("total_posts") if isinstance(data, dict) else len(payload["items"])
    payload["sources"] = data.get("sources") if isinstance(data, dict) else {}
    payload["stale"] = True  # the scraper only refreshes when run with an active session
    return payload


def read_deitaone_feed() -> Dict[str, Any]:
    """@DeItaone feed — LIVE x.com profile-page scrape (logged-out, no API key).

    Port of the mechanism the sentiment project's ``monitor_deItaone.sh``
    launchd job has been running (it sends an iMessage on every new post):
    a plain browser-UA GET of ``https://x.com/DeItaone`` whose server-rendered
    HTML embeds the latest tweets (``data-tweet-id`` articles with the tweet
    text and ISO timestamps). No X API key, no session cookie, no rate limit.

    The old scraper-output file (``project-sentiment/news_feed.json``) is kept
    ONLY as a last-resort fallback when the live scrape is unreachable, and is
    labeled as a fallback so the panel never fakes freshness.
    """
    payload: Dict[str, Any] = {
        "items": [], "source_file": None, "status": "missing",
        "reason": None, "scraped_at_utc": None, "live": True,
    }
    live = x_profile_timeline("DeItaone")
    if live.get("ok"):
        payload.update(
            {
                "items": live["items"],
                "status": "ok",
                "reason": f"{len(live['items'])} tweets",
                "scraped_at_utc": live["fetched_at_utc"],
                "source": "x.com profile page scrape (logged-out, no API key)",
                "live": True,
            }
        )
        return payload
    payload["reason"] = live.get("error") or "live scrape returned nothing"
    # Last-resort fallback: the old scraper file, explicitly labeled as stale.
    path = _first_existing([SENTIMENT_DIR / "news_feed.json", OLD_SENTIMENT_DIR / "news_feed.json"])
    if path is None:
        return payload
    payload["source_file"] = str(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        payload["status"] = "error"
        payload["reason"] = f"live scrape failed ({live.get('error')}); unreadable news_feed.json: {type(exc).__name__}"
        return payload
    payload["status"] = data.get("status", "unknown")
    payload["reason"] = f"live scrape failed ({live.get('error')}) — showing stale scraper file"
    payload["scraped_at_utc"] = data.get("scraped_at_iso")
    payload["live"] = False
    payload["source"] = "x.com profile scrape unavailable — stale news_feed.json fallback"
    items = data.get("items", []) if isinstance(data, dict) else []
    for item in items:
        if not isinstance(item, dict):
            continue
        payload["items"].append(
            {
                "id": str(item.get("id", "")),
                "text": str(item.get("text", "")),
                "ts_utc": item.get("time_iso"),
                "url": item.get("url"),
            }
        )
    return payload


X_PROFILE_CACHE_TTL_S = 120.0
_X_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def _x_profile_parse_tweets(html_text: str) -> List[Dict[str, Any]]:
    """Parse server-rendered tweet articles out of the logged-out profile page.

    X's logged-out profile HTML embeds the newest tweets as
    ``<article data-tweet-id="...">`` blocks containing the tweet text and an
    ISO datetime. The SVG icon paths inside the same blocks are filtered by
    requiring two consecutive multi-letter words (path data never has those).
    """
    import html as _html
    import re as _re

    items: List[Dict[str, Any]] = []
    seen: set = set()
    for match in _re.finditer(r'data-tweet-id="(\d+)"', html_text):
        tid = match.group(1)
        if tid in seen:
            continue
        seen.add(tid)
        start = match.end()
        nxt = html_text.find('data-tweet-id="', start)
        seg = html_text[start:nxt if nxt != -1 else start + 60000]
        times = _re.findall(r"20\d\d-\d\d-\d\dT\d\d:\d\d", seg)
        runs = [_html.unescape(r).strip() for r in _re.findall(r">([^<>]{40,})<", seg)]
        text = ""
        for run in runs:
            if "See what" in run or "<" in run or ">" in run:
                continue
            if _re.search(r"[A-Za-z]{3,} [A-Za-z]{3,}", run):
                text = _re.sub(r"\s+", " ", run)
                break
        if not text:
            continue
        items.append(
            {
                "id": tid,
                "text": _re.sub(r"\s+", " ", text)[:500],
                "ts_utc": times[0] if times else None,
                "url": f"https://x.com/DeItaone/status/{tid}",
            }
        )
    return items


def x_profile_timeline(account: str) -> Dict[str, Any]:
    """Live scrape of an X account's logged-out profile page (no API key)."""
    payload: Dict[str, Any] = {"ok": False, "items": [], "error": None, "fetched_at_utc": None}
    cache_key = f"x_profile_{account.lower()}"
    if _cache_fresh(cache_key):
        cached = _cache_get(cache_key)
        if isinstance(cached, dict):
            return cached
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    html_text, error = _http_get_text(f"https://x.com/{account}", headers={"User-Agent": _X_UA})
    if error:
        payload["error"] = error
        payload["fetched_at_utc"] = now_iso
        return payload
    if not html_text:
        payload["error"] = "x.com returned an empty body"
        payload["fetched_at_utc"] = now_iso
        return payload
    items = _x_profile_parse_tweets(html_text)
    if not items:
        payload["error"] = "profile page fetched but 0 tweets parsed (markup may have changed)"
        payload["fetched_at_utc"] = now_iso
        return payload
    payload.update({"ok": True, "items": items, "fetched_at_utc": now_iso, "account": account})
    _cache_put(cache_key, payload, X_PROFILE_CACHE_TTL_S)
    return payload


def news_wire() -> Dict[str, Any]:
    """Combined wire: live Massive news + Finnhub world news + the DeItaone scraper feed."""
    payload: Dict[str, Any] = {
        "massive": massive_news(),
        "finnhub": finnhub_news(),
        "deitaone": read_deitaone_feed(),
    }
    return payload


# --------------------------------------------------------------------------
# Social feed: live Reddit hot posts (cached) + DeItaone/X scraper output
# --------------------------------------------------------------------------

SOCIAL_SUBREDDITS = ("wallstreetbets", "stocks", "bitcoin", "cryptocurrency", "worldnews", "politics")
_REDDIT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
# Reddit throttles aggressively (429/403) once an IP hammers the public JSON
# API.  Back off exponentially between retries and serve the last good
# snapshot while throttled.
_REDDIT_STATE: Dict[str, Any] = {"lock": threading.Lock(), "fail_until": 0.0, "consecutive": 0}

# The rdt-cli credential (sentiment_oldft's Agent-Reach Reddit scraper) holds
# a browser-captured `reddit_session` cookie — NOT an API key. Reddit's
# anonymous .json endpoints are 403 anti-bot-blocked, but the same public
# JSON endpoints return 200 with this saved session cookie and a browser UA
# (exactly how rdt-cli's get_subreddit works: BASE_URL www.reddit.com).
RDT_CREDENTIAL_PATH = Path.home() / ".config" / "rdt-cli" / "credential.json"


def _reddit_session_cookie() -> Optional[str]:
    """The reddit_session cookie from the rdt-cli credential file, or None."""
    try:
        data = json.loads(RDT_CREDENTIAL_PATH.read_text(encoding="utf-8"))
        cookie = data.get("cookies", {}).get("reddit_session")
        return str(cookie) if cookie else None
    except (OSError, json.JSONDecodeError, AttributeError):
        return None


def _live_reddit() -> Dict[str, Any]:
    """Hot posts from reddit.com's public JSON API with the saved session cookie.

    Port of the sentiment_oldft / Agent-Reach rdt-cli method
    (``scraper.py -> scrape_reddit()`` -> ``rdt sub``): the public
    ``www.reddit.com/r/{sub}/hot.json`` endpoints with a browser User-Agent
    plus the ``reddit_session`` cookie captured once from a browser login.
    No API key, no OAuth registration. Cached for ``REDDIT_TTL_S`` seconds;
    when reddit rate-limits or the network is down, the last successful
    payload is served (marked degraded) so the panel never regresses to the
    ancient scraper file while a fresh live snapshot exists.
    """
    payload: Dict[str, Any] = {"items": [], "source": None, "live": False}
    cache_key = "reddit_live_hot"
    if _cache_fresh(cache_key):
        cached = _cache_get(cache_key)
        return cached if isinstance(cached, dict) else payload
    now = time.monotonic()
    with _REDDIT_STATE["lock"]:
        if now < _REDDIT_STATE["fail_until"]:
            stale = _cache_get(cache_key)
            if isinstance(stale, dict) and stale.get("items"):
                stale["degraded"] = True
                stale["error"] = "reddit throttled — retrying with backoff"
                return stale
            payload["error"] = "reddit throttled — retrying with backoff"
            return payload
    session_cookie = _reddit_session_cookie()
    if not session_cookie:
        payload["error"] = "no reddit_session cookie (run rdt-cli login, or check ~/.config/rdt-cli/credential.json)"
        return payload
    path = "/r/" + "+".join(SOCIAL_SUBREDDITS) + "/hot.json"
    data, error = _http_get_json(
        "https://www.reddit.com" + path + "?limit=8&raw_json=1",
        headers={
            "User-Agent": _REDDIT_UA,
            "Accept": "application/json, text/plain, */*",
            "Cookie": "reddit_session=" + session_cookie,
        },
    )
    if error or not isinstance(data, dict):
        with _REDDIT_STATE["lock"]:
            _REDDIT_STATE["consecutive"] += 1
            _REDDIT_STATE["fail_until"] = now + min(600.0, 30.0 * (2 ** (_REDDIT_STATE["consecutive"] - 1)))
        stale = _cache_get(cache_key)
        if isinstance(stale, dict) and stale.get("items"):
            stale["degraded"] = True
            stale["error"] = error or "reddit returned no data"
            return stale
        payload["error"] = error or "reddit returned no data"
        return payload
    with _REDDIT_STATE["lock"]:
        _REDDIT_STATE["consecutive"] = 0
        _REDDIT_STATE["fail_until"] = 0.0
    for child in (data.get("data") or {}).get("children", []):
        post = (child or {}).get("data")
        if not isinstance(post, dict):
            continue
        try:
            score = int(post.get("score") or 0)
        except (TypeError, ValueError):
            score = 0
        payload["items"].append(
            {
                "id": str(post.get("id", "")),
                "title": str(post.get("title", "")),
                "subreddit": str(post.get("subreddit", "")),
                "score": score,
                "num_comments": post.get("num_comments"),
                "url": post.get("url"),
            }
        )
    payload["items"].sort(key=lambda item: item["score"], reverse=True)
    payload["source"] = "reddit.com public JSON + saved reddit_session cookie (rdt-cli method)"
    payload["live"] = True
    _cache_put(cache_key, payload, REDDIT_TTL_S)
    return payload


def social_feed() -> Dict[str, Any]:
    """Combined social payload: live Reddit (session-cookie, scraper-file
    fallback) + DeItaone/X (live profile-page scrape)."""
    reddit = _live_reddit()
    if not reddit.get("items"):
        fallback = read_reddit_feed()
        fallback["live"] = False
        fallback["fallback"] = True
        reddit = fallback
    return {"reddit": reddit, "x": read_deitaone_feed()}
