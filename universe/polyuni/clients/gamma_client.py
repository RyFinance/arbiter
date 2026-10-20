"""Gamma discovery client — COLD PATH.

Fetches active binary markets, maps each to its YES/NO token-id pair, and returns
compact MarketMeta objects carrying the fields market_universe.py's filter needs.
Runs on a slow background cadence (UNIVERSE_RESCAN_INTERVAL_S), never in the
trading loop.

PolyUni still tracks N concurrent markets by lifecycle state rather than one
rotating slot. The one exception to bulk discovery is current+next BTC 5m/15m
slugs: Gamma omits these short windows from /markets pagination, so their official
market-by-slug route is queried explicitly and merged into the same universe.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx

from ..logger import log
from ..models import MarketMeta

_TIMEOUT = 20.0
# 2026-08-19: Gamma's /markets endpoint caps its response at 100 rows regardless
# of a larger `limit` value -- confirmed live (limit=250 and limit=500 both
# returned exactly 100). _PAGE_SIZE MUST match that real cap: the "short page,
# stop paginating" check below compares len(raw) against _PAGE_SIZE, and a
# _PAGE_SIZE larger than what the server actually honors makes every full page
# look short, silently truncating discovery to page 0 (page 0 only, ~100
# markets) no matter how many total markets exist. Caught by
# scripts/discover_universe_smoke.py's real-data run.
_PAGE_SIZE = 100
# Safety cap on pages fetched per discovery pass -- Gamma has thousands of
# historical/closed markets even filtered to active=true; this bounds one
# discovery pass to at most _MAX_PAGES * _PAGE_SIZE markets considered.
_MAX_PAGES = 50
_BTC_SHORT_WINDOWS_S = (300, 900)


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
    """Return the two complementary outcome indices for a binary market.

    Gamma exposes BTC windows with labels such as ``Up``/``Down`` even though
    they are the same two-outcome CTF structure. Prefer semantic YES/NO or
    UP/DOWN ordering when present; otherwise preserve Gamma's documented
    binary index order (slot 0, slot 1).
    """
    if len(outcomes) != 2:
        return None
    low = [o.lower() for o in outcomes]
    if "yes" not in low or "no" not in low:
        if "up" in low and "down" in low:
            return low.index("up"), low.index("down")
        return 0, 1
    return low.index("yes"), low.index("no")


def _market_id(m: Dict[str, Any]) -> str:
    return str(m.get("id") or m.get("conditionId") or m.get("slug") or "")


def _parse_time_value(raw: Any) -> float:
    """Parse a Gamma ISO timestamp to epoch seconds; 0.0 when unavailable."""
    if not raw:
        return 0.0
    try:
        text = str(raw)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        if "T" not in text:
            text += "T00:00:00+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, TypeError):
        return 0.0


def _parse_end_time(m: Dict[str, Any]) -> float:
    """Parse Gamma's endDate (full ISO datetime) to epoch seconds. 0.0 if absent
    or unparseable -- callers treat 0.0 as "unknown", never as "already ended"."""
    return _parse_time_value(m.get("endDate") or m.get("endDateIso"))


def _parse_start_time(m: Dict[str, Any]) -> float:
    # Short crypto Up/Down windows encode their exact opening boundary in the
    # slug (for example ``btc-updown-5m-1786971900``). Gamma's ``startDate`` can
    # instead describe when the event record was created, which may be well
    # before that tradable window and would make every "early" entry look late.
    slug = str(m.get("slug") or "").lower()
    slug_match = re.search(r"(?:^|-)updown-\d+[mh]-(\d{10})$", slug)
    if slug_match is not None:
        return float(slug_match.group(1))
    return _parse_time_value(
        m.get("startDate") or m.get("startDateIso") or m.get("eventStartTime")
    )


def _parse_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _parse_market(m: Dict[str, Any]) -> Optional[MarketMeta]:
    yes_token_id = no_token_id = ""
    outcome_yes, outcome_no = "Yes", "No"

    tokens = m.get("tokens") or []
    if len(tokens) == 2:
        labels = [str(t.get("outcome", "")) for t in tokens]
        idx = _yes_no_index(labels)
        if idx is not None:
            y, n = idx
            yes_token_id = str(tokens[y].get("token_id", ""))
            no_token_id = str(tokens[n].get("token_id", ""))
            outcome_yes, outcome_no = labels[y] or "Yes", labels[n] or "No"

    if not (yes_token_id and no_token_id):
        # Fallback: outcomes[] + clobTokenIds[] aligned by index.
        outcomes = _as_json_list(m.get("outcomes"))
        clob = _as_json_list(m.get("clobTokenIds"))
        if not outcomes or not clob or len(outcomes) != len(clob):
            return None
        idx = _yes_no_index(outcomes)
        if idx is None:
            return None
        y, n = idx
        yes_token_id, no_token_id = clob[y], clob[n]
        outcome_yes, outcome_no = outcomes[y], outcomes[n]

    if not (yes_token_id and no_token_id):
        return None

    description = str(m.get("description") or "").strip()
    fee_schedule = m.get("feeSchedule") if isinstance(m.get("feeSchedule"), dict) else {}
    fees_enabled = m.get("feesEnabled")
    if not isinstance(fees_enabled, bool):
        fees_enabled = None

    return MarketMeta(
        market_id=_market_id(m),
        question=m.get("question", ""),
        yes_token_id=yes_token_id,
        no_token_id=no_token_id,
        slug=str(m.get("slug") or ""),
        tick_size=_parse_float(m.get("tickSize"), 0.01),
        min_size=_parse_float(m.get("orderMinSize"), 1.0),
        outcome_yes=outcome_yes,
        outcome_no=outcome_no,
        end_time=_parse_end_time(m),
        start_time=_parse_start_time(m),
        fees_enabled=fees_enabled,
        fee_rate=_parse_float(fee_schedule.get("rate")),
        fee_exponent=_parse_float(fee_schedule.get("exponent"), 1.0),
        liquidity_usd=_parse_float(m.get("liquidityNum")),
        volume_24h_usd=_parse_float(m.get("volume24hr")),
        enable_order_book=bool(m.get("enableOrderBook", False)),
        neg_risk=bool(m.get("negRisk", False)),
        accepting_orders=bool(m.get("acceptingOrders", False)),
        has_description=bool(description),
        spread_at_discovery=_parse_float(m.get("spread")) if m.get("spread") is not None else None,
    )


async def discover_btc_short_windows(
    session: httpx.AsyncClient,
    *,
    now: Optional[float] = None,
) -> List[MarketMeta]:
    """Fetch current+next BTC 5m/15m markets by deterministic slug.

    These rotating markets are available through Gamma's official market-by-slug
    route but are absent from the bulk pagination used for the general universe.
    Looking one boundary ahead allows PriceFeed to subscribe before the early-entry
    window opens rather than discovering it halfway through.
    """
    wall = int(time.time() if now is None else now)
    slugs: List[str] = []
    for window_s in _BTC_SHORT_WINDOWS_S:
        current = (wall // window_s) * window_s
        slugs.extend(
            f"btc-updown-{window_s // 60}m-{boundary}"
            for boundary in (current, current + window_s)
        )

    async def fetch(slug: str) -> Optional[MarketMeta]:
        try:
            response = await session.get(f"/markets/slug/{slug}", timeout=_TIMEOUT)
            if response.status_code == 404:
                return None
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                return None
            if payload.get("closed") is True or payload.get("active") is False:
                return None
            return _parse_market(payload)
        except Exception as exc:
            log.debug(f"short-window discovery failed slug={slug}: {exc!r}")
            return None

    results = await asyncio.gather(*(fetch(slug) for slug in slugs))
    return [meta for meta in results if meta is not None]


async def discover_markets(
    session: httpx.AsyncClient,
    whitelist: Optional[List[str]] = None,
    *,
    include_btc_short_windows: bool = False,
) -> List[MarketMeta]:
    """Fetch every active, order-book-enabled binary market. COLD PATH.

    No tag/keyword pre-filtering (that was polymarket_arb's BTC-only narrowing) --
    numeric qualification (liquidity/volume/spread/time-to-resolution/negRisk)
    happens in market_universe.py against the full set this returns. An explicit
    whitelist, if given, still narrows the fetch (exact id/slug match) since there
    is no reason to page through markets the operator explicitly excluded from
    consideration either way.
    """
    whitelist_set = {w.lower() for w in (whitelist or [])}
    markets: List[MarketMeta] = []
    seen_ids: set[str] = set()

    for page in range(_MAX_PAGES):
        params = {
            "closed": "false",
            "active": "true",
            "acceptingOrders": "true",
            "_sort": "volume24hr:desc",
            "limit": str(_PAGE_SIZE),
            "offset": str(page * _PAGE_SIZE),
        }
        try:
            resp = await session.get("/markets", params=params, timeout=_TIMEOUT)
            resp.raise_for_status()
            raw = resp.json()
        except httpx.HTTPStatusError as e:
            # Gamma currently returns 422 once pagination crosses its maximum
            # supported offset (observed at offset=2100 after a valid offset=2000
            # page).  At that point we already have every row the endpoint will
            # expose, so this is an end-of-pagination signal rather than a failed
            # discovery pass.  Keep first-page 422s visible as real errors.
            if page > 0 and e.response.status_code == 422:
                log.info(
                    f"gamma discovery pagination exhausted at offset={page * _PAGE_SIZE}"
                )
                break
            log.error(f"gamma discovery page {page} failed: {e!r}")
            break
        except Exception as e:  # COLD PATH: log and stop paging, return what we have.
            log.error(f"gamma discovery page {page} failed: {e!r}")
            break

        if isinstance(raw, dict):
            raw = raw.get("data", [])
        if not raw:
            break

        for m in raw:
            if not isinstance(m, dict):
                continue
            mid = _market_id(m)
            if not mid or mid in seen_ids:
                continue
            if whitelist_set and mid.lower() not in whitelist_set and str(m.get("slug", "")).lower() not in whitelist_set:
                continue
            meta = _parse_market(m)
            if meta is None:
                continue
            seen_ids.add(mid)
            markets.append(meta)

        if len(raw) < _PAGE_SIZE:
            break  # short page -- reached the end

    if include_btc_short_windows:
        for meta in await discover_btc_short_windows(session):
            if meta.market_id in seen_ids:
                continue
            if (
                whitelist_set
                and meta.market_id.lower() not in whitelist_set
                and meta.slug.lower() not in whitelist_set
            ):
                continue
            seen_ids.add(meta.market_id)
            markets.append(meta)

    log.info(f"discovered {len(markets)} binary markets across the universe")
    return markets
