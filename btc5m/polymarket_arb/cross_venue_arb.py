"""Standalone Kalshi/Polymarket cross-venue observer.

Observation only:
  * no authenticated Kalshi calls
  * no Polymarket order submission
  * no paper orders

The observer maps each live Kalshi BTC 15-minute market to the three matching
Polymarket BTC 5-minute windows, fetches public books, computes price-gap
signals, and appends JSONL records to state/cross_venue_log.jsonl.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Optional

import httpx

from .clients.clob_client import _best_ask_from_book, _best_bid_from_book
from .clients.kalshi_client import KalshiBTCMarket, KalshiClient


POLY_GAMMA_BASE_URL = "https://gamma-api.polymarket.com"
POLY_CLOB_BASE_URL = "https://clob.polymarket.com"
POLY_WINDOW_SECONDS = 300
KALSHI_WINDOW_SECONDS = 900
ORACLE_MISMATCH_RISK = "kalshi_cf_benchmarks_brti_vs_polymarket_chainlink_btc_usd"
DEFAULT_LOG_PATH = Path("state/cross_venue_log.jsonl")


@dataclass(frozen=True)
class PolymarketWindow:
    """One Polymarket BTC 5-minute window aligned to a Kalshi 15-minute market."""
    slug: str
    start_ts: int
    end_ts: int
    market_id: str
    question: str
    up_token_id: str
    down_token_id: str
    resolution_source: str
    accepting_orders: bool
    active: bool
    closed: bool
    up_ask: Optional[float] = None
    down_ask: Optional[float] = None
    up_bid: Optional[float] = None
    down_bid: Optional[float] = None

    @property
    def has_prices(self) -> bool:
        return self.up_ask is not None and self.down_ask is not None

    def to_log(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "start_ts": self.start_ts,
            "end_ts": self.end_ts,
            "market_id": self.market_id,
            "question": self.question,
            "up_token_id": self.up_token_id,
            "down_token_id": self.down_token_id,
            "resolution_source": self.resolution_source,
            "accepting_orders": self.accepting_orders,
            "active": self.active,
            "closed": self.closed,
            "up_ask": self.up_ask,
            "down_ask": self.down_ask,
            "up_bid": self.up_bid,
            "down_bid": self.down_bid,
        }


@dataclass(frozen=True)
class CrossVenueSnapshot:
    """One cross-venue observation record (appended to state/cross_venue_log.jsonl).

    Holds the Kalshi market, the three aligned Polymarket sub-windows, dual proxies,
    and the computed gaps. Observation-only — never carries PnL or trade intent.
    """
    observed_at: str
    kalshi: KalshiBTCMarket
    polymarket_windows: list[PolymarketWindow]
    poly_up_proxy: Optional[float]
    poly_down_proxy: Optional[float]
    best_gap_side: Optional[str]
    best_gap_venue: Optional[str]
    best_gap: Optional[float]
    oracle_mismatch_risk: bool
    status: str
    reason: str

    def to_log(self) -> dict[str, Any]:
        return {
            "observed_at": self.observed_at,
            "status": self.status,
            "reason": self.reason,
            "oracle_mismatch_risk": self.oracle_mismatch_risk,
            "oracle_mismatch_kind": ORACLE_MISMATCH_RISK,
            "kalshi": self.kalshi.to_log(),
            "polymarket_windows": [w.to_log() for w in self.polymarket_windows],
            "poly_up_proxy": self.poly_up_proxy,
            "poly_down_proxy": self.poly_down_proxy,
            "kalshi_yes_ask": self.kalshi.yes_ask,
            "kalshi_no_ask": self.kalshi.no_ask,
            "gaps": compute_price_gaps(
                self.kalshi.yes_ask,
                self.kalshi.no_ask,
                self.poly_up_proxy,
                self.poly_down_proxy,
            ),
            "best_gap_side": self.best_gap_side,
            "best_gap_venue": self.best_gap_venue,
            "best_gap": self.best_gap,
        }


def floor_to_5m(ts: int) -> int:
    """Snap an epoch-second timestamp down to the most recent 5-minute window start."""
    return ts - (ts % POLY_WINDOW_SECONDS)


def window_slug(asset: str, timeframe: str, window_ts: int) -> str:
    """Build the deterministic Polymarket rolling-window slug (e.g. ``btc-updown-5m-N``)."""
    return f"{asset}-updown-{timeframe}-{window_ts}"


def kalshi_to_poly_window_starts(kalshi: KalshiBTCMarket) -> list[int]:
    """Map one Kalshi 15-minute BTC market to its three aligned Polymarket 5-min starts.

    Returns ``[]`` if the Kalshi window isn't exactly 900s aligned to 5-min boundaries,
    so callers can skip misaligned markets without branching on alignment math themselves.
    """
    start = int(kalshi.open_time.timestamp())
    end = int(kalshi.close_time.timestamp())
    if end - start != KALSHI_WINDOW_SECONDS:
        return []
    if start % POLY_WINDOW_SECONDS != 0 or end % POLY_WINDOW_SECONDS != 0:
        return []
    return [start, start + POLY_WINDOW_SECONDS, start + 2 * POLY_WINDOW_SECONDS]


def parse_clob_token_ids(raw: Any) -> tuple[str, str]:
    """Resolve the YES/NO CLOB token id pair from a Gamma market field (str or list)."""
    if isinstance(raw, str):
        ids = json.loads(raw)
    else:
        ids = raw
    if not isinstance(ids, list) or len(ids) < 2:
        raise ValueError("missing clobTokenIds")
    return str(ids[0]), str(ids[1])


def compute_price_gaps(
    kalshi_yes_ask: Optional[float],
    kalshi_no_ask: Optional[float],
    poly_up_proxy: Optional[float],
    poly_down_proxy: Optional[float],
) -> dict[str, Optional[float]]:
    """Return the four-direction price-gap dict between Kalshi asks and Polymarket proxies.

    Each gap is signed so positive = the named side is cheaper. Any missing input yields
    ``None`` for both aligned gaps; never raises. Used to find a leg-1 < leg-2 opportunity
    across venues for the observer to log.
    """
    gaps: dict[str, Optional[float]] = {
        "kalshi_yes_cheaper": None,
        "polymarket_up_proxy_cheaper": None,
        "kalshi_no_cheaper": None,
        "polymarket_down_proxy_cheaper": None,
    }
    if kalshi_yes_ask is not None and poly_up_proxy is not None:
        gaps["kalshi_yes_cheaper"] = poly_up_proxy - kalshi_yes_ask
        gaps["polymarket_up_proxy_cheaper"] = kalshi_yes_ask - poly_up_proxy
    if kalshi_no_ask is not None and poly_down_proxy is not None:
        gaps["kalshi_no_cheaper"] = poly_down_proxy - kalshi_no_ask
        gaps["polymarket_down_proxy_cheaper"] = kalshi_no_ask - poly_down_proxy
    return gaps


def best_positive_gap(gaps: dict[str, Optional[float]]) -> tuple[Optional[str], Optional[str], Optional[float]]:
    """Pick the largest positive gap from a ``compute_price_gaps`` result.

    Returns ``(side_label, venue_label, gap_value)`` where ``side_label`` is the
    cheaper side (e.g. ``kalshi_yes_cheaper``) and ``venue_label`` is the venue
    half of that label (``kalshi`` or ``polymarket``). All three are ``None`` if
    no gap is positive — the caller treats that as a no-op observation sample.
    """
    best_name: Optional[str] = None
    best_value: Optional[float] = None
    for name, value in gaps.items():
        if value is None or value <= 0:
            continue
        if best_value is None or value > best_value:
            best_name = name
            best_value = value
    if best_name is None:
        return None, None, None
    if best_name.startswith("kalshi_yes"):
        return "YES/UP", "kalshi", best_value
    if best_name.startswith("kalshi_no"):
        return "NO/DOWN", "kalshi", best_value
    if "up_proxy" in best_name:
        return "YES/UP", "polymarket_proxy", best_value
    return "NO/DOWN", "polymarket_proxy", best_value


class CrossVenueArbObserver:
    def __init__(
        self,
        kalshi_client: KalshiClient,
        http: httpx.AsyncClient,
        log_path: Path = DEFAULT_LOG_PATH,
    ):
        self.kalshi = kalshi_client
        self.http = http
        self.log_path = log_path

    async def resolve_polymarket_window(self, start_ts: int) -> Optional[PolymarketWindow]:
        slug = window_slug("btc", "5m", start_ts)
        try:
            response = await self.http.get(f"{POLY_GAMMA_BASE_URL}/markets/slug/{slug}", timeout=5.0)
            response.raise_for_status()
            data = response.json()
            up_token, down_token = parse_clob_token_ids(data.get("clobTokenIds"))
            window = PolymarketWindow(
                slug=slug,
                start_ts=start_ts,
                end_ts=start_ts + POLY_WINDOW_SECONDS,
                market_id=str(data.get("id") or slug),
                question=str(data.get("question") or ""),
                up_token_id=up_token,
                down_token_id=down_token,
                resolution_source=str(data.get("resolutionSource") or ""),
                accepting_orders=bool(data.get("acceptingOrders")),
                active=bool(data.get("active")),
                closed=bool(data.get("closed")),
            )
            return await self.with_polymarket_prices(window)
        except Exception:
            return None

    async def with_polymarket_prices(self, window: PolymarketWindow) -> PolymarketWindow:
        up_book, down_book = await asyncio.gather(
            self.fetch_clob_book(window.up_token_id),
            self.fetch_clob_book(window.down_token_id),
        )
        up_ask, _ = _best_ask_from_book(up_book or {})
        down_ask, _ = _best_ask_from_book(down_book or {})
        up_bid, _ = _best_bid_from_book(up_book or {})
        down_bid, _ = _best_bid_from_book(down_book or {})
        return PolymarketWindow(
            **{
                **window.__dict__,
                "up_ask": up_ask,
                "down_ask": down_ask,
                "up_bid": up_bid,
                "down_bid": down_bid,
            }
        )

    async def fetch_clob_book(self, token_id: str) -> Optional[dict[str, Any]]:
        try:
            response = await self.http.get(
                f"{POLY_CLOB_BASE_URL}/book",
                params={"token_id": token_id},
                timeout=5.0,
            )
            response.raise_for_status()
            return response.json()
        except Exception:
            return None

    async def observe_once(self) -> list[CrossVenueSnapshot]:
        markets = await self.kalshi.list_btc_15m_markets(status="open", limit=10)
        snapshots: list[CrossVenueSnapshot] = []
        for market in markets:
            snapshots.append(await self.observe_market(market))
        return snapshots

    async def observe_market(self, market: KalshiBTCMarket) -> CrossVenueSnapshot:
        observed_at = datetime.now(timezone.utc).isoformat()
        starts = kalshi_to_poly_window_starts(market)
        poly_windows: list[PolymarketWindow] = []
        if starts:
            resolved = await asyncio.gather(*(self.resolve_polymarket_window(ts) for ts in starts))
            poly_windows = [w for w in resolved if w is not None]

        priced_windows = [w for w in poly_windows if w.has_prices]
        poly_up_proxy = mean([w.up_ask for w in priced_windows if w.up_ask is not None]) if priced_windows else None
        poly_down_proxy = mean([w.down_ask for w in priced_windows if w.down_ask is not None]) if priced_windows else None
        gaps = compute_price_gaps(market.yes_ask, market.no_ask, poly_up_proxy, poly_down_proxy)
        best_side, best_venue, best_gap = best_positive_gap(gaps)

        status = "ok"
        reason = "observed"
        if not starts:
            status = "unmapped"
            reason = "kalshi window is not an exact 15-minute UTC window"
        elif len(poly_windows) != 3:
            status = "partial"
            reason = f"resolved {len(poly_windows)}/3 polymarket windows"
        elif len(priced_windows) != 3:
            status = "partial"
            reason = f"priced {len(priced_windows)}/3 polymarket windows"
        elif best_gap is None:
            status = "no_gap"
            reason = "no positive cross-venue price gap"

        return CrossVenueSnapshot(
            observed_at=observed_at,
            kalshi=market,
            polymarket_windows=poly_windows,
            poly_up_proxy=poly_up_proxy,
            poly_down_proxy=poly_down_proxy,
            best_gap_side=best_side,
            best_gap_venue=best_venue,
            best_gap=best_gap,
            oracle_mismatch_risk=True,
            status=status,
            reason=reason,
        )

    def append_logs(self, snapshots: list[CrossVenueSnapshot]) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as fh:
            for snapshot in snapshots:
                fh.write(json.dumps(snapshot.to_log(), separators=(",", ":"), default=str) + "\n")


def summarize_log(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"records": 0}
    records = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                records.append(json.loads(line))
    gaps = [float(r["best_gap"]) for r in records if r.get("best_gap") is not None]
    windows = {r.get("kalshi", {}).get("ticker") for r in records if r.get("kalshi")}
    status_counts: dict[str, int] = {}
    for record in records:
        status = str(record.get("status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
    return {
        "records": len(records),
        "kalshi_windows": len([w for w in windows if w]),
        "positive_gap_records": len(gaps),
        "max_gap": max(gaps) if gaps else None,
        "avg_gap": mean(gaps) if gaps else None,
        "status_counts": status_counts,
        "oracle_mismatch_risk_records": sum(1 for r in records if r.get("oracle_mismatch_risk")),
    }


def observer_error_record(exc: Exception) -> dict[str, Any]:
    return {
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "status": "error",
        "reason": "observer_poll_failed",
        "oracle_mismatch_risk": True,
        "oracle_mismatch_kind": ORACLE_MISMATCH_RISK,
        "error_type": exc.__class__.__name__,
        "error": str(exc),
    }


async def run_observer(duration_seconds: int, interval_seconds: int, log_path: Path) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=10.0) as http:
        async with KalshiClient() as kalshi:
            observer = CrossVenueArbObserver(kalshi, http, log_path=log_path)
            deadline = time.monotonic() + duration_seconds
            while time.monotonic() < deadline:
                try:
                    snapshots = await observer.observe_once()
                    observer.append_logs(snapshots)
                except Exception as exc:
                    log_path.parent.mkdir(parents=True, exist_ok=True)
                    with log_path.open("a", encoding="utf-8") as fh:
                        fh.write(json.dumps(observer_error_record(exc), sort_keys=True) + "\n")
                await asyncio.sleep(interval_seconds)
    return summarize_log(log_path)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Observe Kalshi/Polymarket BTC cross-venue price gaps.")
    parser.add_argument("--duration-seconds", type=int, default=900)
    parser.add_argument("--interval-seconds", type=int, default=15)
    parser.add_argument("--log-path", default=str(DEFAULT_LOG_PATH))
    parser.add_argument("--truncate-log", action="store_true")
    args = parser.parse_args(argv)

    log_path = Path(args.log_path)
    if args.truncate_log and log_path.exists():
        log_path.unlink()
    summary = asyncio.run(run_observer(args.duration_seconds, args.interval_seconds, log_path))
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
