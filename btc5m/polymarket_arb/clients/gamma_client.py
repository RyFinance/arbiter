"""Gamma discovery client — COLD PATH.

Fetches active binary markets, maps each to its YES/NO token-id pair, and returns
compact MarketMeta objects. Runs ONCE at startup (and on a slow background refresh),
never in the trading loop.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import httpx

from ..config import SETTINGS
from ..logger import log
from ..models import MarketMeta

_TIMEOUT = 20.0


def _as_json_list(v: Any) -> List[str]:
    """Gamma returns some fields as JSON-encoded strings, others as lists. Normalize."""
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x) for x in v]
    if isinstance(v, str):
        try:
            parsed = json.loads(v)
            if isinstance(parsed, list):
                return [str(x) for x in parsed]
        except (ValueError, TypeError):
            return [v]
    return [str(v)]


def _yes_no_index(outcomes: List[str]):
    """Return (yes_idx, no_idx) or None if this isn't a binary YES/NO market."""
    low = [o.lower() for o in outcomes]
    if "yes" not in low or "no" not in low:
        return None
    return low.index("yes"), low.index("no")


def _market_id(m: Dict[str, Any]) -> str:
    return str(m.get("id") or m.get("conditionId") or m.get("slug") or "")


def _parse_market(m: Dict[str, Any]) -> Optional[MarketMeta]:
    # Prefer the explicit `tokens` array when present.
    tokens = m.get("tokens") or []
    if tokens:
        yes_tok = next((t for t in tokens if str(t.get("outcome", "")).lower() == "yes"), None)
        no_tok = next((t for t in tokens if str(t.get("outcome", "")).lower() == "no"), None)
        if yes_tok and no_tok:
            return MarketMeta(
                market_id=_market_id(m),
                question=m.get("question", ""),
                yes_token_id=str(yes_tok.get("token_id", "")),
                no_token_id=str(no_tok.get("token_id", "")),
                tick_size=float(m.get("tickSize", 0.01) or 0.01),
            )

    # Fallback: outcomes[] + clobTokenIds[] aligned by index.
    outcomes = _as_json_list(m.get("outcomes"))
    clob = _as_json_list(m.get("clobTokenIds"))
    if not outcomes or not clob or len(outcomes) != len(clob):
        return None
    idx = _yes_no_index(outcomes)
    if idx is None:
        return None
    y, n = idx
    return MarketMeta(
        market_id=_market_id(m),
        question=m.get("question", ""),
        yes_token_id=clob[y],
        no_token_id=clob[n],
        tick_size=float(m.get("tickSize", 0.01) or 0.01),
    )


async def discover_markets(session: httpx.AsyncClient) -> List[MarketMeta]:
    """Fetch + filter + map target markets. COLD PATH."""
    s = SETTINGS
    markets: List[MarketMeta] = []

    params = {
        "closed": "false",
        "active": "true",
        "acceptingOrders": "true",
        "_sort": "volume24hr:desc",
        "limit": "100",
    }
    if s.market_filter_tag:
        params["tag_slug"] = s.market_filter_tag

    try:
        resp = await session.get("/markets", params=params, timeout=_TIMEOUT)
        resp.raise_for_status()
        raw = resp.json()
    except Exception as e:  # COLD PATH: discovery failure is fatal-ish, log and bail.
        log.error(f"gamma discovery failed: {e!r}")
        return markets

    if isinstance(raw, dict):
        raw = raw.get("data", [])

    whitelist = {w.lower() for w in s.market_whitelist}
    for m in raw:
        if not isinstance(m, dict):
            continue
        mid = _market_id(m)
        # Whitelist filter (exact id/slug match) when configured.
        if whitelist:
            if mid.lower() not in whitelist and str(m.get("slug", "")).lower() not in whitelist:
                continue
        meta = _parse_market(m)
        if meta is None:
            continue
        # Keyword filter on the question text (cold path; cheap, one-time).
        # Skipped entirely when broad scan is on (track the whole binary universe to
        # maximize the chance of catching a transient YES+NO < 1.00 dip).
        if s.market_keywords and not whitelist and not s.market_broad_scan:
            if not any(k in meta.question.lower() for k in s.market_keywords):
                continue
        markets.append(meta)

    log.info(f"discovered {len(markets)} binary markets")
    return markets


# ---- Rolling-window resolution (FIX #3) -------------------------------------
# Deterministic slug math (window_ts = now - now % WINDOW_SECONDS) + the network
# resolve live HERE so window_tracker.py has a single, consistent import surface.
# These are pure functions (no network, no clock skew beyond the caller's `now`),
# which is what makes the window contract testable offline (see tests/test_arb.py).

def current_window_ts(now: float, window_seconds: int) -> int:
    """The UTC epoch-second start of the window containing `now`.

    window_ts = floor(now / window_seconds) * window_seconds. Pure UTC, no DST.
    Anyone computing it at the same instant gets the same window (and slug).
    """
    n = int(now)
    return n - (n % int(window_seconds))


def next_window_ts(window_ts: int, window_seconds: int) -> int:
    """The start of the window immediately after the one starting at `window_ts`."""
    return int(window_ts) + int(window_seconds)


def window_slug(asset: str, timeframe: str, window_ts: int) -> str:
    """Deterministic slug for a rolling window, e.g. 'btc-updown-5m-1780319100'.

    This is the contract the whole window strategy rests on — the bot KNOWS which
    market it trades at every instant without scanning or guessing.
    """
    return f"{asset}-updown-{timeframe}-{int(window_ts)}"


def _up_down_index(outcomes: List[str]):
    """Return (up_idx, down_idx) for a BTC Up/Down market, else None.

    Up/Down windows use outcome labels like "Up"/"Down" (not "Yes"/"No"). Match
    case-insensitively and only accept a true Up/Down pair — we never guess which
    token is which, since the strategy buys BOTH legs and must know the window.
    """
    low = [o.lower() for o in outcomes]
    if "up" not in low or "down" not in low:
        return None
    return low.index("up"), low.index("down")


def _build_window_meta(m: Dict[str, Any], slug: str, window_start: float,
                       window_close: float, up_token: str, down_token: str,
                       outcome_a: str, outcome_b: str) -> MarketMeta:
    """Assemble a MarketMeta for a resolved window. Leg A (yes_token_id) == Up."""
    return MarketMeta(
        market_id=str(m.get("id") or m.get("conditionId") or slug),
        question=m.get("question", slug),
        yes_token_id=up_token,    # leg A = Up
        no_token_id=down_token,   # leg B = Down
        tick_size=float(m.get("tickSize", 0.01) or 0.01),
        min_size=float(m.get("minSize", 1.0) or 1.0),
        outcome_yes=outcome_a,
        outcome_no=outcome_b,
        window_start=window_start,
        window_close=window_close,
        window_slug=slug,
    )


def _parse_window_object(obj: Dict[str, Any], window_start: float,
                         window_close: float, slug: str,
                         is_event: bool) -> Optional[MarketMeta]:
    """Parse an Up/Down event OR market object into a MarketMeta.

    Events nest their market under `markets[0]`; a bare market IS the market. We
    need a clean Up/Down outcome pair — refusing is safer than guessing, since a
    mislabeled leg turns a hedged arb into a directional bet.
    """
    target = obj
    if is_event:
        markets = obj.get("markets") or []
        if not markets:
            return None
        target = markets[0]

    # Prefer explicit tokens[] carrying outcome labels.
    tokens = target.get("tokens") or []
    up_tok = next((t for t in tokens if str(t.get("outcome", "")).lower() == "up"), None)
    down_tok = next((t for t in tokens if str(t.get("outcome", "")).lower() == "down"), None)
    if up_tok and down_tok:
        return _build_window_meta(target, slug, window_start, window_close,
                                  str(up_tok.get("token_id", "")),
                                  str(down_tok.get("token_id", "")),
                                  "Up", "Down")

    # Fallback: outcomes[] + clobTokenIds[] aligned by index.
    outcomes = _as_json_list(target.get("outcomes"))
    clob = _as_json_list(target.get("clobTokenIds"))
    if not outcomes or not clob or len(outcomes) != len(clob):
        return None
    idx = _up_down_index(outcomes)
    if idx is None:
        return None
    u, d = idx
    return _build_window_meta(target, slug, window_start, window_close,
                              clob[u], clob[d], "Up", "Down")


async def resolve_window_event(session: httpx.AsyncClient, slug: str,
                               window_start: float = 0.0,
                               window_close: float = 0.0) -> Optional[MarketMeta]:
    """Resolve one rolling-window event by its deterministic slug. COLD PATH.

    Counterpart of discover_markets for window mode: we KNOW the slug (pure math)
    so we just fetch it — no scanning. Tries /events?slug= first (the rolling
    series is event-shaped), then /markets?slug= as a fallback. Returns None when
    the window's market isn't published yet (the rotation loop retries).
    """
    # 1) event lookup
    try:
        resp = await session.get("/events", params={"slug": slug}, timeout=_TIMEOUT)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as e:
        log.debug(f"window event fetch {slug} failed: {e!r}")
        payload = None
    raw = payload if isinstance(payload, list) else (
        payload.get("data", []) if isinstance(payload, dict) else []
    )
    for ev in raw:
        if isinstance(ev, dict):
            meta = _parse_window_object(ev, window_start, window_close, slug, is_event=True)
            if meta is not None:
                return meta

    # 2) direct market lookup fallback
    try:
        resp = await session.get("/markets", params={"slug": slug}, timeout=_TIMEOUT)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as e:
        log.debug(f"window market fetch {slug} failed: {e!r}")
        return None
    raw = payload if isinstance(payload, list) else (
        payload.get("data", []) if isinstance(payload, dict) else []
    )
    for m in raw:
        if isinstance(m, dict):
            meta = _parse_window_object(m, window_start, window_close, slug, is_event=False)
            if meta is not None:
                return meta
    return None


async def resolve_window_market(session: httpx.AsyncClient, slug: str) -> Optional[MarketMeta]:
    """Resolve a rolling window by slug — the name window_tracker.py imports.

    Thin wrapper over resolve_window_event: the tracker stamps window_start/close on
    the returned meta itself, so we only need (session, slug) here. Returns None when
    the window's market isn't published yet (the rotation loop retries).
    """
    return await resolve_window_event(session, slug)
