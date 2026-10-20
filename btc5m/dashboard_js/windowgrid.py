"""Live multi-timeframe window grid for the dashboard BOT tab.

Shows all three updown timeframes (5m/15m/30m) at once — one row per
timeframe with the live YES/NO best asks, combined, and best-combined —
regardless of which bot is running. Reads ONLY public books (CLOB REST +
Gamma metadata), the same read-only mechanism the isolated loop bot uses;
no trading, no auth.

``snapshot()`` is called synchronously by ``/api/windows`` and caches:

  * Gamma token resolution per window epoch (markets don't change mid-window)
  * book asks for ~4 s (the dashboard polls every 5 s)

so the endpoint never hammers the public APIs.
"""
from __future__ import annotations

import threading
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

from dashboard_js import loopbot

try:  # the runner is a plain script; import for shared pure helpers
    import scripts.run_loop_bot as runner  # type: ignore[import-not-found]
except Exception:  # noqa: BLE001
    runner = None

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"
TIMEFRAMES = {"5m": 300, "15m": 900, "30m": 1800}
ASSET = "btc"

BOOK_TTL_S = 8.0
TOKEN_TTL_S = 300.0  # negative-cache: retry failed token resolution at most every 5 min
USER_AGENT = "pm-dashboard-windows/1.0 (read-only public books)"

_CACHE: Dict[str, Any] = {"lock": threading.Lock(), "tokens": {}, "books": {}}


def _http_json(url: str, timeout: float = 5.0) -> Optional[Any]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            import json

            return json.loads(response.read().decode("utf-8", errors="replace"))
    except Exception:  # noqa: BLE001
        return None


def _window_ts(now: float, seconds: int) -> int:
    return int(now // seconds) * seconds


def _slug(tf: str, ts: int) -> str:
    return f"{ASSET}-updown-{tf}-{ts}"


def _parse_tokens(market: Dict[str, Any]) -> Optional[Dict[str, str]]:
    if runner is not None:
        return runner.parse_updown_tokens(market)
    tokens = market.get("tokens") or []
    up_tok = next((t for t in tokens if str(t.get("outcome", "")).lower() == "up"), None)
    down_tok = next((t for t in tokens if str(t.get("outcome", "")).lower() == "down"), None)
    if up_tok and down_tok and up_tok.get("token_id") and down_tok.get("token_id"):
        return {"yes": str(up_tok["token_id"]), "no": str(down_tok["token_id"])}
    return None


def _resolve_tokens(slug: str) -> Optional[Dict[str, str]]:
    for path in ("/events", "/markets"):
        query = urllib.parse.urlencode({"slug": slug})
        payload = _http_json(f"{GAMMA_BASE}{path}?{query}")
        if not payload:
            continue
        raw = payload if isinstance(payload, list) else (payload.get("data", []) if isinstance(payload, dict) else [])
        for item in raw:
            if not isinstance(item, dict):
                continue
            market = item
            if path == "/events":
                markets = item.get("markets") or []
                if not markets:
                    continue
                market = markets[0]
            tokens = _parse_tokens(market)
            if tokens:
                return tokens
    return None


def _best_ask(token_id: str) -> Optional[float]:
    query = urllib.parse.urlencode({"token_id": token_id})
    book = _http_json(f"{CLOB_BASE}/book?{query}", timeout=3.0)
    if not isinstance(book, dict):
        return None
    best: Optional[float] = None
    for pair in book.get("asks") or []:
        try:
            price = float(pair["price"]) if isinstance(pair, dict) else float(pair[0])
        except (TypeError, ValueError, KeyError, IndexError):
            continue
        if best is None or price < best:
            best = price
    return best


def _book_ask_cached(token_id: str, key: str) -> Optional[float]:
    """Best ask with a ~4 s cache so the 5 s dashboard poll is cheap."""
    now = time.monotonic()
    with _CACHE["lock"]:
        entry = _CACHE["books"].get(key)
        if entry and now - entry["t"] < BOOK_TTL_S:
            return entry["v"]
    value = _best_ask(token_id)
    with _CACHE["lock"]:
        _CACHE["books"][key] = {"t": now, "v": value}
    return value


def snapshot() -> Dict[str, Any]:
    """One row per timeframe: current window + live combined book."""
    now = time.time()
    rows: Dict[str, Dict[str, Any]] = {}
    for tf, seconds in TIMEFRAMES.items():
        w = _window_ts(now, seconds)
        slug = _slug(tf, w)
        row: Dict[str, Any] = {
            "tf": tf, "window": w, "slug": slug,
            "yes": None, "no": None, "combined": None, "best_combined": None,
            "position": "—", "source": None,
        }
        with _CACHE["lock"]:
            entry = _CACHE["tokens"].get(slug)
        if entry is not None and now - entry.get("t", 0.0) < TOKEN_TTL_S:
            tokens = entry.get("v")  # warm — may be None (negative cache)
        else:
            tokens = _resolve_tokens(slug)
            with _CACHE["lock"]:
                # cache both successes AND failures so a missing market (e.g.
                # the 30m series) doesn't cost 2 Gamma requests on every poll
                _CACHE["tokens"][slug] = {"t": now, "v": tokens}
        if tokens:
            yes = _book_ask_cached(tokens["yes"], f"{slug}:yes")
            no = _book_ask_cached(tokens["no"], f"{slug}:no")
            row["yes"], row["no"] = yes, no
            if yes is not None and no is not None:
                row["combined"] = round(yes + no, 4)
            row["source"] = "clob public book"
        # main-session / loop-bot position annotation (honest, no synthesis)
        try:
            lb = loopbot.status()
            loop_running = bool(lb.get("managed_running"))
            tf_state = (lb.get("timeframes") or {}).get(tf) or {}
            if loop_running and tf_state.get("entered") and tf_state.get("entry"):
                row["position"] = "loop bot OPEN"
            elif loop_running and tf_state.get("slug") == slug:
                row["position"] = "loop bot flat"
        except Exception:  # noqa: BLE001
            pass
        if row["best_combined"] is None and row["combined"] is not None:
            row["best_combined"] = row["combined"]
        rows[tf] = row
    return {
        "fetched_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "timeframes": rows,
        "paper_only": True,
    }
