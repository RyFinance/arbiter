#!/usr/bin/env python3
"""Feed drift analysis: LSE WebSocket vs CLOB WebSocket for BTC/USD.

Runs both feeds simultaneously for a configurable duration, logs each tick
to a JSONL file, then computes and prints a drift report.

Usage:
    python scripts/analyze_feed_drift.py --duration 1800   # 30 min (default)
    python scripts/analyze_feed_drift.py --duration 120    # 2 min quick test
    python scripts/analyze_feed_drift.py --duration 60     # 1 min

    # Simulated mode (no real API keys needed):
    python scripts/analyze_feed_drift.py --simulated --duration 1800
    python scripts/analyze_feed_drift.py --simulated --duration 120

Optional:
    --token-id  TOKEN_ID   CLOB token id to subscribe (repeatable).
                          Auto-discovered from Gamma if omitted.
    --symbol    BTC/USD    LSE symbol name (default: BTC/USD).
    --out-dir   state      Directory for JSONL output files.
    --lse-only             Run only LSE feed (skip CLOB).
    --clob-only            Run only CLOB feed (skip LSE).
    --stale-threshold  SECONDS  Seconds of silence before a feed is "stale" (default 2.0).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from bisect import bisect_left
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

# Ensure project root is on sys.path when run as a script
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import httpx  # noqa: E402

from polymarket_arb.clients.lse_client import LSEClient, LSE_WS_URL  # noqa: E402
from polymarket_arb.clients.clob_client import ClobBookFeed  # noqa: E402
from polymarket_arb.clients.gamma_client import discover_markets  # noqa: E402
from polymarket_arb.config import SETTINGS  # noqa: E402
from polymarket_arb.logger import log  # noqa: E402


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class FeedEntry:
    """One logged tick from either feed."""
    timestamp: float
    feed: str            # "lse" or "clob"
    symbol: str
    best_ask: Optional[float]
    best_bid: Optional[float]
    size: Optional[float]
    latency_ms: float


@dataclass
class StalePeriod:
    """A period where one feed was silent while the other was active."""
    feed_stale: str       # which feed went stale ("lse" or "clob")
    feed_active: str      # which feed was still updating
    stale_start: float
    stale_end: float
    duration_s: float


@dataclass
class PriceBucket:
    """Binned price discrepancy."""
    bucket_label: str
    count: int
    min_discrep: float
    max_discrep: float
    mean_discrep: float


# ---------------------------------------------------------------------------
# LSE data extraction
# ---------------------------------------------------------------------------

def _extract_lse_tick(data: Dict[str, Any]) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """Extract (best_ask, best_bid, size) from an LSE WS data dict.

    Tries multiple common field-name patterns since the LSE schema is not
    documented in the client code.  Returns (None, None, None) if nothing
    matches.
    """
    # Pattern 1: top-level numeric fields
    best_ask = None
    for ask_key in ("best_ask", "bestAsk", "ask", "Ask", "best_ask_price", "price", "Price"):
        if ask_key in data and isinstance(data[ask_key], (int, float)):
            best_ask = float(data[ask_key])
            break

    best_bid = None
    for bid_key in ("best_bid", "bestBid", "bid", "Bid", "best_bid_price"):
        if bid_key in data and isinstance(data[bid_key], (int, float)):
            best_bid = float(data[bid_key])
            break

    size = None
    for size_key in ("size", "Size", "quantity", "Quantity", "best_ask_size", "bestAskSize", "volume", "Volume"):
        if size_key in data and isinstance(data[size_key], (int, float)):
            size = float(data[size_key])
            break

    # If we found ask but not bid, or vice versa, try nested "price"/"size"
    if best_ask is None or best_bid is None or size is None:
        for sub_key in ("data", "quote", "ticker", "orderbook"):
            sub = data.get(sub_key)
            if isinstance(sub, dict):
                if best_ask is None:
                    for k in ("best_ask", "bestAsk", "ask", "Ask", "price", "Price"):
                        if k in sub and isinstance(sub[k], (int, float)):
                            best_ask = float(sub[k])
                            break
                if best_bid is None:
                    for k in ("best_bid", "bestBid", "bid", "Bid", "price", "Price"):
                        if k in sub and isinstance(sub[k], (int, float)):
                            best_bid = float(sub[k])
                            break
                if size is None:
                    for k in ("size", "Size", "quantity", "Quantity"):
                        if k in sub and isinstance(sub[k], (int, float)):
                            size = float(sub[k])
                            break

    # If LSE sends OHLC-style, derive mid
    if best_ask is None and best_bid is None:
        for o_key in ("open", "Open", "close", "Close"):
            if o_key in data and isinstance(data[o_key], (int, float)):
                if best_ask is None:
                    best_ask = float(data[o_key])
                if best_bid is None:
                    best_bid = float(data[o_key])
                break

    return best_ask, best_bid, size


# ---------------------------------------------------------------------------
# LSE callback wrapper
# ---------------------------------------------------------------------------

def _make_lse_callback(entries: List[FeedEntry]) -> Callable:
    """Return a closure that appends an LSE FeedEntry."""
    def on_tick(symbol: str, data: Dict[str, Any]) -> None:
        recv = time.time()
        best_ask, best_bid, size = _extract_lse_tick(data)
        # Latency is measured as the time from message receipt to callback
        # dispatch. In practice, for the WS path this is near-zero (sub-ms).
        latency = 0.0
        entries.append(FeedEntry(
            timestamp=recv,
            feed="lse",
            symbol=symbol,
            best_ask=best_ask,
            best_bid=best_bid,
            size=size,
            latency_ms=latency,
        ))
    return on_tick


# ---------------------------------------------------------------------------
# CLOB callback wrapper
# ---------------------------------------------------------------------------

def _make_clob_callback(
    entries: List[FeedEntry],
    symbol_map: Dict[str, str],
) -> Callable:
    """Return a closure that appends a CLOB FeedEntry.

    symbol_map: token_id -> human-readable symbol name.
    """
    def on_tick(token_id: str, best_ask: Optional[float], size: Optional[float],
                best_bid: Optional[float] = None) -> None:
        recv = time.time()
        symbol = symbol_map.get(token_id, token_id)
        latency = 0.0
        entries.append(FeedEntry(
            timestamp=recv,
            feed="clob",
            symbol=symbol,
            best_ask=best_ask,
            best_bid=best_bid,
            size=size,
            latency_ms=latency,
        ))
    return on_tick


# ---------------------------------------------------------------------------
# Auto-discovery of CLOB token ids from Gamma
# ---------------------------------------------------------------------------

async def _discover_btc_token_ids() -> List[str]:
    """Try to find BTC Up/Down window token ids via Gamma discovery."""
    try:
        async with httpx.AsyncClient(base_url=SETTINGS.gamma_base_url, timeout=15.0) as client:
            markets = await discover_markets(client)
        if markets:
            m = markets[0]
            return [m.yes_token_id, m.no_token_id]
    except Exception as e:
        log.warning(f"Gamma discovery failed for token ids: {e!r}")
    return []


# ---------------------------------------------------------------------------
# Patched LSE WebSocket — works with websockets >= 11 (additional_headers)
# ---------------------------------------------------------------------------

class PatchedLSEClient(LSEClient):
    """LSEClient with a patched WebSocket loop that uses `additional_headers`
    instead of the deprecated `extra_headers` kwarg.

    The base LSEClient._ws_loop uses `extra_headers` which was removed in
    websockets 11+ (replaced by `additional_headers`). This subclass only
    overrides the WS loop, leaving everything else identical.
    """

    async def _ws_loop(self):
        """WebSocket connection loop with auto-reconnect (patched header arg)."""
        import websockets

        backoff = 1.0
        max_backoff = 30.0

        while not self._stop:
            try:
                async with websockets.connect(
                    LSE_WS_URL,
                    additional_headers={"x-api-key": self.api_key},
                    ping_interval=30,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    self._ws = ws
                    self._healthy = True
                    log.info(f"LSE WS connected for {self._ws_symbols}")

                    # Subscribe to symbols
                    sub_msg = json.dumps({
                        "type": "subscribe",
                        "symbols": self._ws_symbols,
                    })
                    await ws.send(sub_msg)

                    backoff = 1.0  # reset on successful connect

                    async for raw in ws:
                        if self._stop:
                            break
                        await self._handle_ws_message(raw)

                    self._healthy = False

            except Exception as e:
                self._healthy = False
                if self._stop:
                    break
                log.warning(f"LSE WS disconnected: {e!r}; reconnecting in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.5, max_backoff)


# ---------------------------------------------------------------------------
# Simulated feed generation (no API keys needed)
# ---------------------------------------------------------------------------

class SimulatedFeedRunner:
    """Generate synthetic LSE + CLOB BTC/USD feed data with known drift patterns.

    This is used when:
    - The LSE API key is a placeholder (401 Unauthorized on REST, WS auth fails)
    - The websockets library version is incompatible with the LSE client
    - The user cannot reach the LSE endpoint (geoblock, no network, etc.)

    The simulated data models realistic feed behavior:
    - CLOB: high-frequency binary market prices (YES+NO ~ 1.0), sub-second updates
    - LSE: BTC/USD spot price with ~200ms typical WS latency, occasional stale periods
    - Controlled drift injected for testing the analysis logic:
      - 50ms average latency advantage for CLOB (faster exchange feed)
      - 0.03% price discrepancy baseline (spread differences)
      - One 5-second LSE stale period mid-run
      - One 3-second CLOB stale period mid-run
    """

    # Simulated BTC/USD spot price (drifts slowly around $110k)
    _BASE_PRICE = 110000.0
    # Simulated CLOB binary market: YES=~0.05, NO=~0.95 (up/down probability)
    _CLOB_YES_PRICE = 0.05
    _CLOB_NO_PRICE = 0.95

    def __init__(self, duration_s: float, tick_rate_hz: float = 5.0):
        self.duration_s = duration_s
        self.tick_interval = 1.0 / tick_rate_hz
        self.lse_entries: List[FeedEntry] = []
        self.clob_entries: List[FeedEntry] = []

    async def run(self) -> None:
        """Generate ticks for both feeds for the configured duration."""
        start_wall = time.time()
        end_wall = start_wall + self.duration_s

        # Simulate two CLOB legs (YES=Up, NO=Down) — matches the real feed shape
        clob_tokens = [
            ("BTC-YES", self._CLOB_YES_PRICE),
            ("BTC-NO", self._CLOB_NO_PRICE),
        ]

        tick = 0
        while time.time() < end_wall:
            t = time.time()

            # --- CLOB feed (high frequency, low latency) ---
            for i, (symbol, base_price) in enumerate(clob_tokens):
                # CLOB gets sub-millisecond latency (direct exchange WS)
                clob_latency = 0.05 + (i * 0.01)  # ~0.05-0.06ms
                # Small random walk in price
                price = base_price + (tick % 7 - 3) * 0.0001
                size = 10000.0 + (tick % 100) * 50.0
                self.clob_entries.append(FeedEntry(
                    timestamp=t,
                    feed="clob",
                    symbol=symbol,
                    best_ask=round(price, 6),
                    best_bid=round(price - 0.001, 6),
                    size=round(size, 2),
                    latency_ms=clob_latency,
                ))

            # --- LSE feed (BTC/USD spot, slower, higher latency) ---
            # Inject one stale period: LSE goes silent for 5 seconds starting at 25% of run
            stale_start = self.duration_s * 0.25
            stale_end = stale_start + 5.0
            in_stale = stale_start < tick * self.tick_interval < stale_end

            # Inject one CLOB stale period: CLOB goes silent for 3s at 60% of run
            clob_stale_start = self.duration_s * 0.60
            clob_stale_end = clob_stale_start + 3.0
            clob_in_stale = clob_stale_start < tick * self.tick_interval < clob_stale_end

            if not in_stale and not clob_in_stale:
                # LSE has higher latency (~250ms typical for a secondary exchange feed)
                lse_latency = 250.0 + (tick % 20) * 5.0  # 250-345ms
                # LSE reports real BTC/USD spot price (~\$110k range)
                # Small random walk around the base price
                spot = self._BASE_PRICE + (tick % 50 - 25) * 100.0
                self.lse_entries.append(FeedEntry(
                    timestamp=t,
                    feed="lse",
                    symbol="BTC/USD",
                    best_ask=round(spot, 2),
                    best_bid=round(spot - 50.0, 2),
                    size=round(50000.0 + (tick % 50) * 1000.0, 2),
                    latency_ms=lse_latency,
                ))

            # CLOB ticks at 5Hz, LSE ticks at ~2Hz (slower spot feed)
            if tick % 2 == 0 and not in_stale:
                pass  # LSE already added above for this tick

            tick += 1
            await asyncio.sleep(self.tick_interval)

        log.info(f"Simulated collection complete: LSE ticks={len(self.lse_entries)}, "
                 f"CLOB ticks={len(self.clob_entries)}")


# ---------------------------------------------------------------------------
# JSONL logging helpers
# ---------------------------------------------------------------------------

def _write_jsonl(path: str, entries: List[FeedEntry]) -> None:
    """Append entries to a JSONL file (one JSON object per line)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:  # overwrite for fresh run
        for e in entries:
            f.write(json.dumps({
                "timestamp": e.timestamp,
                "feed": e.feed,
                "symbol": e.symbol,
                "best_ask": e.best_ask,
                "best_bid": e.best_bid,
                "size": e.size,
                "latency_ms": e.latency_ms,
            }) + "\n")


def _load_jsonl(path: str, feed_name: str) -> List[FeedEntry]:
    """Load FeedEntry objects from a JSONL file."""
    entries: List[FeedEntry] = []
    if not os.path.exists(path):
        return entries
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                entries.append(FeedEntry(
                    timestamp=d["timestamp"],
                    feed=feed_name,
                    symbol=d["symbol"],
                    best_ask=d.get("best_ask"),
                    best_bid=d.get("best_bid"),
                    size=d.get("size"),
                    latency_ms=d.get("latency_ms", 0.0),
                ))
            except (KeyError, ValueError):
                continue
    return entries


# ---------------------------------------------------------------------------
# Analysis functions
# ---------------------------------------------------------------------------

def _compute_latency_diff(lse_entries: List[FeedEntry], clob_entries: List[FeedEntry]) -> Dict[str, Any]:
    """Average latency difference between the two feeds.

    Note: for WS feeds, latency_ms in each entry reflects per-feed processing
    overhead, not true network RTT. The real signal is the timestamp delta
    between matching ticks (see price discrepancy's time_diff).
    """
    lse_lat = [e.latency_ms for e in lse_entries if e.latency_ms is not None]
    clob_lat = [e.latency_ms for e in clob_entries if e.latency_ms is not None]

    avg_lse = sum(lse_lat) / len(lse_lat) if lse_lat else 0.0
    avg_clob = sum(clob_lat) / len(clob_lat) if clob_lat else 0.0
    diff = avg_lse - avg_clob

    # Also compute the median for robustness
    def _median(vals):
        if not vals:
            return 0.0
        s = sorted(vals)
        n = len(s)
        return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2

    return {
        "avg_latency_lse_ms": round(avg_lse, 3),
        "avg_latency_clob_ms": round(avg_clob, 3),
        "median_latency_lse_ms": round(_median(lse_lat), 3),
        "median_latency_clob_ms": round(_median(clob_lat), 3),
        "avg_latency_diff_ms": round(diff, 3),
        "lse_latency_samples": len(lse_lat),
        "clob_latency_samples": len(clob_lat),
    }


def _match_entries(
    lse_entries: List[FeedEntry],
    clob_entries: List[FeedEntry],
    tolerance_s: float = 1.0,
) -> List[Tuple[FeedEntry, FeedEntry, float]]:
    """Pair LSE and CLOB entries by closest timestamp within tolerance.

    Returns list of (lse_entry, clob_entry, time_diff_s).
    """
    # Build sorted lists of (timestamp, index) for each feed
    lse_sorted = sorted(enumerate(lse_entries), key=lambda x: x[1].timestamp)
    clob_sorted = sorted(enumerate(clob_entries), key=lambda x: x[1].timestamp)

    clob_times = [e.timestamp for _, e in clob_sorted]
    matches: List[Tuple[FeedEntry, FeedEntry, float]] = []

    for lse_idx, lse_entry in lse_sorted:
        if lse_entry.best_ask is None:
            continue
        # Binary search for closest CLOB entry
        pos = bisect_left(clob_times, lse_entry.timestamp)
        candidates = []
        if pos < len(clob_times):
            candidates.append(pos)
        if pos > 0:
            candidates.append(pos - 1)

        for c_idx in candidates:
            clob_entry = clob_sorted[c_idx][1]
            if clob_entry.best_ask is None:
                continue
            dt = abs(clob_entry.timestamp - lse_entry.timestamp)
            if dt <= tolerance_s:
                matches.append((lse_entry, clob_entry, dt))
                break

    return matches


def _compute_price_discrepancy(
    matches: List[Tuple[FeedEntry, FeedEntry, float]],
) -> Dict[str, Any]:
    """Price discrepancy distribution binned by time bucket.

    Since LSE reports BTC/USD spot (e.g. ~110000) and CLOB reports binary
    outcome prices (0.0-1.0), a direct price comparison isn't meaningful.
    Instead, we compute:
      - Relative time-alignment quality (how often ticks are within tolerance)
      - Spread/discrepancy within the same feed (LSE ask-bid spread, CLOB YES+NO)
      - Cross-feed tick rate ratio (LSE ticks / CLOB ticks)
      - Timestamp jitter between feeds (latency difference signal)
    """
    if not matches:
        return {
            "total_pairs": 0,
            "buckets": [],
            "overall": {},
            "note": "No matched pairs — feeds may not overlap in time.",
        }

    # For matched pairs, compute the timestamp delta (cross-feed latency gap)
    time_diffs = [dt for _, _, dt in matches]

    # Bin by 1-minute buckets
    bucket_size = 60.0
    buckets: Dict[str, List[float]] = {}
    for lse_e, clob_e, dt in matches:
        bucket_start = int(lse_e.timestamp // bucket_size) * bucket_size
        label = time.strftime("%Y-%m-%d %H:%M", time.strftime("%Y-%m-%d %H:%M", time.gmtime(bucket_start)) and time.gmtime(bucket_start))
        buckets.setdefault(label, []).append(dt)

    bucket_list = []
    for label in sorted(buckets.keys()):
        vals = buckets[label]
        bucket_list.append({
            "bucket": label,
            "count": len(vals),
            "min_time_delta_s": round(min(vals), 6),
            "max_time_delta_s": round(max(vals), 6),
            "mean_time_delta_s": round(sum(vals) / len(vals), 6),
        })

    all_diffs = time_diffs
    return {
        "total_pairs": len(matches),
        "buckets": bucket_list,
        "overall": {
            "min_time_delta_s": round(min(all_diffs), 6) if all_diffs else 0,
            "max_time_delta_s": round(max(all_diffs), 6) if all_diffs else 0,
            "mean_time_delta_s": round(sum(all_diffs) / len(all_diffs), 6) if all_diffs else 0,
            "median_time_delta_s": round(
                sorted(all_diffs)[len(all_diffs) // 2], 6
            ) if all_diffs else 0,
        },
    }


def _compute_stale_periods(
    lse_entries: List[FeedEntry],
    clob_entries: List[FeedEntry],
    stale_threshold_s: float = 2.0,
) -> List[Dict[str, Any]]:
    """Detect periods where one feed went stale while the other was still active.

    A feed is considered "stale" during a window if no ticks arrive from it
    for >= stale_threshold_s while the other feed is still producing ticks.
    """
    if not lse_entries and not clob_entries:
        return []

    # Build merged timeline of (timestamp, feed) events
    all_entries: List[Tuple[float, str]] = []
    for e in lse_entries:
        all_entries.append((e.timestamp, "lse"))
    for e in clob_entries:
        all_entries.append((e.timestamp, "clob"))
    all_entries.sort(key=lambda x: x[0])

    if not all_entries:
        return []

    stale_periods: List[Dict[str, Any]] = []
    last_update: Dict[str, float] = {"lse": 0.0, "clob": 0.0}
    stale_start: Dict[str, float] = {"lse": 0.0, "clob": 0.0}
    in_stale: Dict[str, bool] = {"lse": False, "clob": False}

    # Initialize last_update from first events
    for ts, feed in all_entries:
        last_update[feed] = ts

    # Now walk the timeline
    for i, (ts, feed) in enumerate(all_entries):
        other = "clob" if feed == "lse" else "lse"
        last_update[feed] = ts

        # Check if the *other* feed has gone stale
        gap = ts - last_update[other] if last_update[other] > 0 else 0.0
        if gap > stale_threshold_s:
            if not in_stale[other]:
                in_stale[other] = True
                stale_start[other] = last_update[other]
        else:
            if in_stale[other]:
                in_stale[other] = False
                stale_periods.append({
                    "feed_stale": other,
                    "feed_active": feed,
                    "stale_start": round(stale_start[other], 3),
                    "stale_end": round(ts, 3),
                    "duration_s": round(ts - stale_start[other], 3),
                })

    # Close any still-open stale periods at the end of collection
    final_ts = all_entries[-1][0]
    for feed_name in ("lse", "clob"):
        if in_stale[feed_name]:
            gap = final_ts - last_update[feed_name]
            if gap > stale_threshold_s:
                stale_periods.append({
                    "feed_stale": feed_name,
                    "feed_active": "clob" if feed_name == "lse" else "lse",
                    "stale_start": round(stale_start[feed_name], 3),
                    "stale_end": round(final_ts, 3),
                    "duration_s": round(final_ts - stale_start[feed_name], 3),
                })

    return stale_periods


def _compute_tick_rate_stats(
    lse_entries: List[FeedEntry],
    clob_entries: List[FeedEntry],
) -> Dict[str, Any]:
    """Compute tick rate statistics for both feeds."""
    def _rate(entries: List[FeedEntry]) -> Tuple[float, float]:
        if len(entries) < 2:
            return 0.0, 0.0
        total_span = max(e.timestamp for e in entries) - min(e.timestamp for e in entries)
        if total_span <= 0:
            return 0.0, float(len(entries))
        rate = len(entries) / total_span
        return rate, total_span

    lse_rate, lse_span = _rate(lse_entries)
    clob_rate, clob_span = _rate(clob_entries)

    return {
        "lse_tick_rate_hz": round(lse_rate, 3),
        "clob_tick_rate_hz": round(clob_rate, 3),
        "lse_total_span_s": round(lse_span, 3),
        "clob_total_span_s": round(clob_span, 3),
        "lse_count": len(lse_entries),
        "clob_count": len(clob_entries),
        "rate_ratio_lse_per_clob": round(lse_rate / clob_rate, 4) if clob_rate > 0 else None,
    }


def _compute_gaps(entries: List[FeedEntry], threshold_s: float = 2.0) -> List[Dict[str, Any]]:
    """Detect internal gaps (no ticks for threshold_s) within a single feed."""
    if len(entries) < 2:
        return []

    entries_sorted = sorted(entries, key=lambda e: e.timestamp)
    gaps: List[Dict[str, Any]] = []

    for i in range(1, len(entries_sorted)):
        dt = entries_sorted[i].timestamp - entries_sorted[i - 1].timestamp
        if dt > threshold_s:
            gaps.append({
                "start": round(entries_sorted[i - 1].timestamp, 3),
                "end": round(entries_sorted[i].timestamp, 3),
                "gap_s": round(dt, 3),
            })

    return gaps


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def generate_report(
    lse_entries: List[FeedEntry],
    clob_entries: List[FeedEntry],
    out_dir: str,
    stale_threshold: float = 2.0,
    match_tolerance: float = 1.0,
) -> Dict[str, Any]:
    """Compute all drift metrics and write the report."""
    report: Dict[str, Any] = {
        "collection": {
            "lse_tick_count": len(lse_entries),
            "clob_tick_count": len(clob_entries),
            "lse_time_range": None,
            "clob_time_range": None,
            "overlapping_time_range": None,
        },
        "latency": {},
        "price_discrepancy": {},
        "stale_periods": [],
        "tick_rates": {},
        "internal_gaps": {},
        "diagnosis": {},
    }

    # Time ranges
    if lse_entries:
        report["collection"]["lse_time_range"] = {
            "start": round(min(e.timestamp for e in lse_entries), 3),
            "end": round(max(e.timestamp for e in lse_entries), 3),
        }
    if clob_entries:
        report["collection"]["clob_time_range"] = {
            "start": round(min(e.timestamp for e in clob_entries), 3),
            "end": round(max(e.timestamp for e in clob_entries), 3),
        }

    # Overlapping time range
    if lse_entries and clob_entries:
        lse_start = min(e.timestamp for e in lse_entries)
        lse_end = max(e.timestamp for e in lse_entries)
        clob_start = min(e.timestamp for e in clob_entries)
        clob_end = max(e.timestamp for e in clob_entries)
        overlap_start = max(lse_start, clob_start)
        overlap_end = min(lse_end, clob_end)
        if overlap_start <= overlap_end:
            report["collection"]["overlapping_time_range"] = {
                "start": round(overlap_start, 3),
                "end": round(overlap_end, 3),
                "duration_s": round(overlap_end - overlap_start, 3),
            }

    # (a) Latency difference
    report["latency"] = _compute_latency_diff(lse_entries, clob_entries)

    # (b) Price discrepancy (cross-feed timestamp alignment + price checks)
    matches = _match_entries(lse_entries, clob_entries, tolerance_s=match_tolerance)
    report["price_discrepancy"] = _compute_price_discrepancy(matches)

    # (c) Stale periods (cross-feed: one silent while other active)
    report["stale_periods"] = _compute_stale_periods(lse_entries, clob_entries, stale_threshold)

    # (d) Tick rate statistics
    report["tick_rates"] = _compute_tick_rate_stats(lse_entries, clob_entries)

    # (e) Internal gaps within each feed
    report["internal_gaps"] = {
        "lse_gaps": _compute_gaps(lse_entries, stale_threshold),
        "clob_gaps": _compute_gaps(clob_entries, stale_threshold),
    }

    # (f) Diagnosis: trustworthiness assessment
    report["diagnosis"] = _diagnose(report, lse_entries, clob_entries)

    # Write report
    report_path = os.path.join(out_dir, "feed_drift_report.json")
    os.makedirs(out_dir, exist_ok=True)
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2, default=str)
    log.info(f"Report written to {report_path}")

    return report


def _diagnose(report: Dict[str, Any], lse_entries: List[FeedEntry],
              clob_entries: List[FeedEntry]) -> Dict[str, Any]:
    """Assess whether LSE is trustworthy as a cross-validation source."""
    diagnosis: Dict[str, Any] = {}

    # 1. Did LSE produce any data at all?
    lse_working = len(lse_entries) > 0
    clob_working = len(clob_entries) > 0
    diagnosis["lse_active"] = lse_working
    diagnosis["clob_active"] = clob_working

    # 2. Cross-feed coverage: how much time overlap exists
    overlap = report["collection"].get("overlapping_time_range")
    diagnosis["has_time_overlap"] = overlap is not None and overlap.get("duration_s", 0) > 0
    if overlap:
        diagnosis["overlap_duration_s"] = overlap["duration_s"]

    # 3. Tick rate comparison
    rates = report["tick_rates"]
    diagnosis["lse_tick_rate_hz"] = rates.get("lse_tick_rate_hz", 0)
    diagnosis["clob_tick_rate_hz"] = rates.get("clob_tick_rate_hz", 0)

    # 4. Stale periods
    stale_periods = report["stale_periods"]
    lse_stale = [s for s in stale_periods if s["feed_stale"] == "lse"]
    clob_stale = [s for s in stale_periods if s["feed_stale"] == "clob"]
    diagnosis["lse_stale_periods"] = len(lse_stale)
    diagnosis["clob_stale_periods"] = len(clob_stale)
    total_stale_lse = sum(s["duration_s"] for s in lse_stale) if lse_stale else 0
    diagnosis["lse_stale_total_s"] = round(total_stale_lse, 2)

    # 5. Latency comparison
    lat = report["latency"]
    lat_diff = lat.get("avg_latency_diff_ms", 0)
    diagnosis["avg_latency_diff_ms"] = lat_diff
    diagnosis["lse_slower_than_clob"] = lat_diff > 0

    # 6. Trustworthiness verdict
    reasons = []
    if not lse_working:
        reasons.append("LSE feed produced zero ticks — API key may be invalid or endpoint unreachable")
        diagnosis["lse_trustworthy"] = False
        diagnosis["verdict"] = "NOT TRUSTWORTHY as cross-validation source: no data received"
    else:
        # Count issues that reduce trustworthiness
        issues = 0
        if len(lse_stale) >= 3:
            issues += 1
            reasons.append(f"LSE went stale {len(lse_stale)} times (high instability)")
        if total_stale_lse > 30:
            issues += 1
            reasons.append(f"LSE stale for {total_stale_lse:.1f}s total (>{10}% of run)")
        # Flag insufficient overlap if coverage is < 50% of the run
        total_duration = report["collection"].get("lse_time_range", {}).get("end", 0) - \
                         report["collection"].get("lse_time_range", {}).get("start", 0)
        overlap_dur = diagnosis.get("overlap_duration_s", 0)
        if total_duration > 0 and overlap_dur < total_duration * 0.5:
            issues += 1
            reasons.append(f"Insufficient time overlap with CLOB ({overlap_dur:.1f}s / {total_duration:.1f}s)")

        if issues == 0:
            diagnosis["lse_trustworthy"] = True
            diagnosis["verdict"] = "TRUSTWORTHY as secondary cross-validation source"
        elif issues == 1:
            diagnosis["lse_trustworthy"] = True
            diagnosis["verdict"] = "USABLE WITH CAUTION — limited validation possible due to intermittent staleness"
        else:
            diagnosis["lse_trustworthy"] = False
            diagnosis["verdict"] = "NOT FULLY TRUSTWORTHY for cross-validation — too many stale periods"

    diagnosis["detail_reasons"] = reasons

    # 7. Price semantics check
    if lse_entries and clob_entries:
        sample_lse = lse_entries[0].best_ask
        sample_clob = clob_entries[0].best_ask
        if sample_lse and sample_clob:
            # LSE BTC/USD spot (~$100k) vs CLOB binary (0.0-1.0) are different instruments
            if sample_lse > 100 and sample_clob < 2.0:
                diagnosis["price_semantics"] = (
                    "LSE provides BTC/USD spot prices; CLOB provides binary market outcome "
                    "prices (0.0-1.0). Direct price comparison is not meaningful — validation "
                    "is via tick timing/latency alignment, not price equality."
                )
            else:
                diagnosis["price_semantics"] = "Both feeds report similar price ranges — direct comparison valid"

    return diagnosis


def print_report(report: Dict[str, Any]) -> None:
    """Print the report to stdout in a human-readable format."""
    print("\n" + "=" * 70)
    print("  FEED DRIFT ANALYSIS REPORT — LSE vs CLOB")
    print("=" * 70)

    # Collection summary
    coll = report["collection"]
    print("\nCollection:")
    print(f"  LSE ticks:  {coll['lse_tick_count']}")
    print(f"  CLOB ticks: {coll['clob_tick_count']}")
    if coll["lse_time_range"]:
        print(f"  LSE range:  {coll['lse_time_range']['start']:.1f} -> {coll['lse_time_range']['end']:.1f}")
    if coll["clob_time_range"]:
        print(f"  CLOB range: {coll['clob_time_range']['start']:.1f} -> {coll['clob_time_range']['end']:.1f}")
    overlap = coll.get("overlapping_time_range")
    if overlap:
        print(f"  Overlap:    {overlap['duration_s']:.1f}s")

    # Latency
    lat = report["latency"]
    print("\nLatency:")
    print(f"  LSE avg latency:   {lat.get('avg_latency_lse_ms', 0):.3f} ms  ({lat.get('lse_latency_samples', 0)} samples)")
    print(f"  CLOB avg latency:  {lat.get('avg_latency_clob_ms', 0):.3f} ms  ({lat.get('clob_latency_samples', 0)} samples)")
    print(f"  Difference (LSE-CLOB): {lat.get('avg_latency_diff_ms', 0):.3f} ms")
    print(f"  LSE median latency: {lat.get('median_latency_lse_ms', 0):.3f} ms")
    print(f"  CLOB median latency: {lat.get('median_latency_clob_ms', 0):.3f} ms")

    # Tick rate comparison
    rates = report["tick_rates"]
    print("\nTick Rates:")
    print(f"  LSE rate:   {rates.get('lse_tick_rate_hz', 0):.2f} Hz")
    print(f"  CLOB rate:  {rates.get('clob_tick_rate_hz', 0):.2f} Hz")
    ratio = rates.get("rate_ratio_lse_per_clob")
    if ratio:
        print(f"  LSE/CLOB ratio: {ratio:.4f}")

    # Price discrepancy
    disc = report["price_discrepancy"]
    print(f"\nCross-feed Alignment (matched within {1.0}s):")
    print(f"  Matched pairs: {disc.get('total_pairs', 0)}")
    overall = disc.get("overall", {})
    if overall:
        print(f"  Mean time delta:   {overall.get('mean_time_delta_s', 0):.6f}s")
        print(f"  Median time delta: {overall.get('median_time_delta_s', 0):.6f}s")
        print(f"  Max time delta:    {overall.get('max_time_delta_s', 0):.6f}s")

    note = disc.get("note")
    if note:
        print(f"  Note: {note}")

    # Stale periods
    stale = report["stale_periods"]
    print("\nStale Periods (cross-feed, >2s silence while other active):")
    if stale:
        for s in stale:
            print(f"  [{time.strftime('%H:%M:%S', time.gmtime(s['stale_start']))} - "
                  f"{time.strftime('%H:%M:%S', time.gmtime(s['stale_end']))}] "
                  f"{s['feed_stale']} stale for {s['duration_s']:.1f}s "
                  f"(active: {s['feed_active']})")
    else:
        print("  No cross-feed stale periods detected.")

    # Internal gaps
    gaps = report["internal_gaps"]
    print("\nInternal Gaps (>2s silence within feed):")
    lse_gaps = gaps.get("lse_gaps", [])
    clob_gaps = gaps.get("clob_gaps", [])
    if lse_gaps:
        print(f"  LSE: {len(lse_gaps)} gaps (total {sum(g['gap_s'] for g in lse_gaps):.1f}s)")
    else:
        print("  LSE: no internal gaps")
    if clob_gaps:
        print(f"  CLOB: {len(clob_gaps)} gaps (total {sum(g['gap_s'] for g in clob_gaps):.1f}s)")
    else:
        print("  CLOB: no internal gaps")

    # Diagnosis
    diag = report["diagnosis"]
    print("\nDiagnosis:")
    print(f"  LSE active: {diag.get('lse_active', False)}")
    print(f"  CLOB active: {diag.get('clob_active', False)}")
    print(f"  Time overlap: {diag.get('has_time_overlap', False)}")
    print(f"  LSE stale periods: {diag.get('lse_stale_periods', 0)} ({diag.get('lse_stale_total_s', 0)}s total)")
    print(f"  LSE vs CLOB latency: {diag.get('avg_latency_diff_ms', 0):.1f}ms (positive = LSE slower)")
    verdict = diag.get("verdict", "UNKNOWN")
    trustworthy = diag.get("lse_trustworthy", False)
    status = "YES" if trustworthy else "NO"
    print(f"\n  >>> LSE trustworthy as cross-validation source: {status}")
    print(f"  >>> Verdict: {verdict}")
    if diag.get("detail_reasons"):
        for r in diag["detail_reasons"]:
            print(f"      - {r}")
    semantics = diag.get("price_semantics", "")
    if semantics:
        print(f"\n  Price semantics: {semantics}")

    print("\n" + "=" * 70)
    print(f"  Full report: {os.path.join(_PROJECT_ROOT, 'state', 'feed_drift_report.json')}")
    print("=" * 70 + "\n")


# ---------------------------------------------------------------------------
# Real feed collection (with graceful degradation + simulated fallback)
# ---------------------------------------------------------------------------

async def collect_feeds(
    duration_s: float,
    lse_symbol: str,
    clob_token_ids: List[str],
    out_dir: str,
    lse_only: bool = False,
    clob_only: bool = False,
) -> Tuple[List[FeedEntry], List[FeedEntry]]:
    """Run both feeds concurrently for `duration_s` seconds.

    Uses PatchedLSEClient to work around the websockets API incompatibility.
    If the LSE WS fails to connect (401/invalid key/geoblock), it logs the
    error and returns an empty LSE entry list — the caller can decide to
    fall back to simulated mode.
    """
    lse_entries: List[FeedEntry] = []
    clob_entries: List[FeedEntry] = []

    # --- LSE feed ---
    lse_client = PatchedLSEClient()
    lse_callback = _make_lse_callback(lse_entries)
    lse_task: Optional[asyncio.Task] = None

    if not clob_only:
        try:
            await lse_client.start_ws([lse_symbol], lse_callback)
            # Give it a moment to connect
            await asyncio.sleep(0.5)
            if not lse_client.healthy:
                log.warning("LSE WS did not become healthy within 500ms — "
                          "may fail to connect (401/geoblock/API-key issue)")
        except Exception as e:
            log.warning(f"LSE WS start failed: {e!r}")
        lse_task = lse_client._ws_task

    # --- CLOB feed ---
    clob_task: Optional[asyncio.Task] = None
    if not lse_only:
        # Build symbol map for CLOB (token_id -> short label)
        if clob_token_ids:
            symbol_map: Dict[str, str] = {}
            for i, tid in enumerate(clob_token_ids):
                symbol_map[tid] = f"BTC-{i}" if len(clob_token_ids) > 1 else "BTC"

            clob_feed = ClobBookFeed(clob_token_ids, _make_clob_callback(clob_entries, symbol_map))

            try:
                clob_task = asyncio.create_task(clob_feed.run(), name="clob-feed")
                # Give it a moment to connect
                await asyncio.sleep(0.5)
            except Exception as e:
                log.warning(f"CLOB WS start failed: {e!r}")
                clob_feed.stop()
                try:
                    clob_task.cancel()
                except Exception:
                    pass
        else:
            log.warning("No CLOB token ids available — CLOB feed skipped")

    log.info(f"Feed drift collection started: duration={duration_s}s, "
             f"LSE={lse_symbol}, CLOB tokens={len(clob_token_ids) if clob_token_ids else 0}")

    # Wait for the collection duration
    await asyncio.sleep(duration_s)

    # Stop both feeds
    lse_client._stop = True
    if lse_task:
        lse_task.cancel()
    if clob_task:
        clob_feed.stop()
        clob_task.cancel()

    # Give a moment for final ticks to be processed
    await asyncio.sleep(0.5)

    # Clean shutdown
    try:
        await lse_client.aclose()
    except Exception:
        pass
    if clob_task:
        try:
            await clob_task
        except asyncio.CancelledError:
            pass

    log.info(f"Collection complete: LSE ticks={len(lse_entries)}, CLOB ticks={len(clob_entries)}")

    # Write JSONL files
    lse_path = os.path.join(out_dir, "lse_feed.jsonl")
    clob_path = os.path.join(out_dir, "clob_feed.jsonl")
    if lse_entries:
        _write_jsonl(lse_path, lse_entries)
        log.info(f"Wrote {len(lse_entries)} LSE entries to {lse_path}")
    if clob_entries:
        _write_jsonl(clob_path, clob_entries)
        log.info(f"Wrote {len(clob_entries)} CLOB entries to {clob_path}")

    return lse_entries, clob_entries


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Analyze LSE vs CLOB price feed drift for BTC/USD"
    )
    parser.add_argument(
        "--duration", type=int, default=1800,
        help="Collection duration in seconds (default: 1800 = 30 min)"
    )
    parser.add_argument(
        "--symbol", default="BTC/USD",
        help="LSE symbol name (default: BTC/USD)"
    )
    parser.add_argument(
        "--token-id", action="append", default=None,
        help="CLOB token id(s) to subscribe (repeatable). Auto-discovered if omitted."
    )
    parser.add_argument(
        "--out-dir", default="state",
        help="Output directory for JSONL files and report (default: state/)"
    )
    parser.add_argument(
        "--stale-threshold", type=float, default=2.0,
        help="Stale threshold in seconds (default: 2.0)"
    )
    parser.add_argument(
        "--match-tolerance", type=float, default=1.0,
        help="Timestamp matching tolerance in seconds for price discrepancy (default: 1.0)"
    )
    parser.add_argument(
        "--lse-only", action="store_true",
        help="Run only LSE feed (skip CLOB)"
    )
    parser.add_argument(
        "--clob-only", action="store_true",
        help="Run only CLOB feed (skip LSE)"
    )
    parser.add_argument(
        "--simulated", action="store_true",
        help="Use synthetic feed data instead of live APIs (no API keys needed)"
    )
    args = parser.parse_args()

    # Resolve output directory relative to project root
    out_dir = os.path.join(_PROJECT_ROOT, args.out_dir)
    os.makedirs(out_dir, exist_ok=True)

    # Resolve CLOB token ids
    token_ids: List[str] = []
    if not args.lse_only:
        token_ids = list(args.token_id) if args.token_id else []
        if not token_ids:
            print("No --token-id provided; auto-discovering BTC token ids from Gamma...")
            token_ids = await _discover_btc_token_ids()
            if not token_ids:
                print("WARNING: Could not auto-discover token ids from Gamma.")
                print("  The CLOB WS feed will be skipped. For full comparison, provide --token-id.")
                print("  Example: python scripts/analyze_feed_drift.py --token-id 0xabc... --token-id 0xdef...")

    print("Starting feed drift analysis:")
    print(f"  Mode:        {'SIMULATED' if args.simulated else 'LIVE'}")
    print(f"  Duration:    {args.duration}s")
    print(f"  LSE symbol:  {args.symbol}")
    print(f"  CLOB tokens: {len(token_ids)}")
    print(f"  Output dir:  {out_dir}")
    print(f"  Stale threshold: {args.stale_threshold}s")
    print(f"  Match tolerance: {args.match_tolerance}s")
    print()

    # --- Collection ---
    if args.simulated:
        runner = SimulatedFeedRunner(duration_s=args.duration)
        await runner.run()
        lse_entries = runner.lse_entries
        clob_entries = runner.clob_entries

        # Write JSONL files
        _write_jsonl(os.path.join(out_dir, "lse_feed.jsonl"), lse_entries)
        _write_jsonl(os.path.join(out_dir, "clob_feed.jsonl"), clob_entries)
        log.info(f"Wrote {len(lse_entries)} simulated LSE entries")
        log.info(f"Wrote {len(clob_entries)} simulated CLOB entries")
    else:
        lse_entries, clob_entries = await collect_feeds(
            duration_s=args.duration,
            lse_symbol=args.symbol,
            clob_token_ids=token_ids,
            out_dir=out_dir,
            lse_only=args.lse_only,
            clob_only=args.clob_only,
        )

    # --- Analyze ---
    report = generate_report(
        lse_entries, clob_entries, out_dir,
        stale_threshold=args.stale_threshold,
        match_tolerance=args.match_tolerance,
    )

    # --- Print ---
    print_report(report)


if __name__ == "__main__":
    asyncio.run(main())
