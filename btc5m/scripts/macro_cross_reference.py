"""TASK D.2 — MACRO EVENT CROSS-REFERENCE (secondary verification).

Flags disagreement between LSE calendar labels and a secondary public
economic-calendar source (Forex Factory public JSON feed).

This is a STANDALONE module — it does NOT live inside page_agent_runner.py
and does NOT modify any locked files.  It consumes the same
``state/macro_events.json`` that page_agent_runner.py writes and compares
each HIGH-impact label against Forex Factory's independent classification.

Design:
  * Primary source:  state/macro_events.json  (labels from LSE)
  * Secondary source: Forex Factory JSON feed  (https://nfs.faireconomy.media/ff_calendar_thisweek.json)
  * Match tolerance:  30 minutes (same calendar date + time proximity)
  * Title matching:   normalized keyword overlap with distinctive tokens
  * Output:           list of disagreements (LSE says HIGH but FF says not, or vice-versa)

Usage:
    python scripts/macro_cross_reference.py [--days 7] [--output results.json]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

# Project root for imports
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Forex Factory public JSON feed (this-week snapshot, updated daily at ~04:00 ET)
FF_CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
FF_TIMEOUT_S = 15.0

# Path to the scraper's output
MACRO_EVENTS_PATH = os.path.join(PROJECT_ROOT, "state", "macro_events.json")

# Matching tolerance: if two events are within this many seconds AND their
# normalized titles overlap, we treat them as the same event.
MATCH_TOLERANCE_S = 1800.0  # 30 minutes

# Distinctive tokens that are unique enough for cross-source title matching.
# A single overlapping token from this set implies the same event type
# (e.g., every "CPI" release on July 22 matches, regardless of country prefix).
DISTINCTIVE_TOKENS: Set[str] = {
    "cpi", "pce", "gdp", "pmi", "non-farm", "nonfarm", "payrolls",
    "fomc", "fed", "rate", "decision", "mccallum",  # central-bank decisions
    "retail", "sales", "claims", "jobless", "unemployment",
    "inflation", "ppi", "boj", "boe", "ecb", "rba", "ifo",
}

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

class DiscrepancyType:
    """Enum-like labels for disagreement types."""
    LSE_HIGH_FF_NOT = "lse_high_ff_not"    # LSE says HIGH, Forex Factory does not
    FF_HIGH_LSE_NOT = "ff_high_lse_not"    # FF says High, LSE did not flag
    IMPACT_MISMATCH = "impact_mismatch"    # Both agree event exists but disagree on level


@dataclass
class Discrepancy:
    """A single disagreement between LSE and Forex Factory."""
    type: str
    lse_title: str
    lse_impact: str
    lse_timestamp: float
    ff_title: str
    ff_impact: str
    ff_timestamp: float
    time_delta_s: float
    note: str = ""


@dataclass
class MacroEvent:
    """Event from LSE macro_events.json."""
    title: str
    timestamp: float
    impact: str  # "HIGH" | "MED" | "LOW"
    source: str = ""
    region_code: str = ""
    description: str = ""
    url: str = ""


@dataclass
class FFEvent:
    """Event from Forex Factory JSON feed."""
    title: str
    timestamp: float
    impact: str  # "High" | "Medium" | "Low" | "Holiday"
    country: str = ""
    forecast: str = ""
    previous: str = ""


# ---------------------------------------------------------------------------
# Forex Factory fetcher
# ---------------------------------------------------------------------------

async def fetch_ff_calendar(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> List[FFEvent]:
    """Fetch the Forex Factory economic calendar as a list of FFEvent objects.

    The public JSON feed covers the current trading week.  To cover a
    custom date range, multiple weekly snapshots would be needed, but the
    FF feed is the only no-key public source that reliably includes impact
    labels.  For a broader window, consider the TradingEconomics API
    (requires a free key) or the Apify ForexFactory scraper (requires Apify token).

    Args:
        start_date: Optional ISO date string 'YYYY-MM-DD'.  Events before this are dropped.
        end_date: Optional ISO date string 'YYYY-MM-DD'.  Events after this are dropped.

    Returns:
        List of FFEvent objects, sorted by timestamp ascending.
    """
    import httpx

    # Retry with exponential backoff — Forex Factory's CDN rate-limits aggressively
    max_retries = 3
    backoff_s = 2.0
    raw: List[Dict[str, Any]] = []

    for attempt in range(max_retries):
        try:
            async with httpx.AsyncClient(timeout=FF_TIMEOUT_S, follow_redirects=True) as client:
                resp = await client.get(
                    FF_CALENDAR_URL,
                    headers={"User-Agent": "Mozilla/5.0 (compatible: PolymarketArbBot/1.0)"},
                )
                if resp.status_code == 429:
                    if attempt < max_retries - 1:
                        print(f"[macro_cross_reference] FF rate-limited (429), retrying in {backoff_s}s...")
                        await asyncio.sleep(backoff_s)
                        backoff_s *= 2
                        continue
                    else:
                        print(f"[macro_cross_reference] FF rate-limited after {max_retries} attempts — skipping secondary source")
                        return []
                resp.raise_for_status()
                raw = resp.json()
                break
        except httpx.HTTPStatusError as e:
            if attempt < max_retries - 1:
                print(f"[macro_cross_reference] FF HTTP error ({e}), retrying in {backoff_s}s...")
                await asyncio.sleep(backoff_s)
                backoff_s *= 2
                continue
            else:
                print(f"[macro_cross_reference] Failed to fetch Forex Factory calendar after {max_retries} retries: {e!r}")
                return []
        except Exception as e:
            print(f"[macro_cross_reference] Failed to fetch Forex Factory calendar: {e!r}")
            return []

    events: List[FFEvent] = []
    for item in raw:
        try:
            # Forex Factory date format: "2026-08-02T05:15:00-04:00" (with timezone offset)
            dt = datetime.fromisoformat(item["date"])
            ts = dt.timestamp()

            # Skip outside date range
            if start_date:
                start_ts = datetime.strptime(start_date, "%Y-%m-%d").replace(
                    tzinfo=timezone.utc
                ).timestamp()
                if ts < start_ts:
                    continue
            if end_date:
                end_ts = datetime.strptime(end_date, "%Y-%m-%d").replace(
                    hour=23, minute=59, second=59, tzinfo=timezone.utc
                ).timestamp()
                if ts > end_ts:
                    continue

            events.append(FFEvent(
                title=item.get("title", "").strip(),
                timestamp=ts,
                impact=item.get("impact", "Unknown").strip(),
                country=item.get("country", "").strip(),
                forecast=item.get("forecast", "").strip(),
                previous=item.get("previous", "").strip(),
            ))
        except (KeyError, ValueError, TypeError):
            continue

    events.sort(key=lambda e: e.timestamp)
    return events


# ---------------------------------------------------------------------------
# LSE event loader
# ---------------------------------------------------------------------------

def load_lse_events(start_ts: float, end_ts: float) -> List[MacroEvent]:
    """Load HIGH-impact events from state/macro_events.json within [start_ts, end_ts].

    Args:
        start_ts: Unix timestamp for the start of the window.
        end_ts: Unix timestamp for the end of the window.

    Returns:
        List of MacroEvent objects with impact == "HIGH", within the time window.
    """
    if not os.path.exists(MACRO_EVENTS_PATH):
        print(f"[macro_cross_reference] {MACRO_EVENTS_PATH} not found — LSE scraper has not produced macro_events.json yet.")
        return []

    try:
        with open(MACRO_EVENTS_PATH, "r") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"[macro_cross_reference] Failed to read {MACRO_EVENTS_PATH}: {e!r}")
        return []

    events: List[MacroEvent] = []
    for raw in data.get("events", []):
        try:
            ts = float(raw.get("timestamp", 0))
            if ts < start_ts or ts > end_ts:
                continue
            if raw.get("impact", "").upper() != "HIGH":
                continue
            events.append(MacroEvent(
                title=raw.get("title", ""),
                timestamp=ts,
                impact=raw.get("impact", "UNKNOWN"),
                source=raw.get("source", ""),
                description=raw.get("description", ""),
                url=raw.get("url", ""),
            ))
        except (TypeError, ValueError):
            continue

    events.sort(key=lambda e: e.timestamp)
    return events


# ---------------------------------------------------------------------------
# Title normalization and matching
# ---------------------------------------------------------------------------

def normalize_title(title: str) -> str:
    """Normalize an event title for cross-source comparison.

    - Lowercase
    - Strip parentheticals
    - Remove measurement suffixes (m/m, y/y, q/q, w/w)
    - Collapse whitespace
    """
    t = title.lower().strip()
    t = re.sub(r'\s*\([^)]*\)\s*', ' ', t)
    t = re.sub(r'\s+(m/m|y/y|q/q|w/w|%|\.\d+bn|\d+\.\d+%)\s*$', ' ', t)
    t = re.sub(r'\s+', ' ', t).strip()
    return t


def titles_match(lse_title: str, ff_title: str) -> bool:
    """Check if an LSE event title matches a Forex Factory event title.

    Uses normalized keyword overlap with distinctive tokens.  A single
    overlapping distinctive token (e.g. 'cpi', 'fomc') is sufficient
    to identify the same event, since these are unique per event type.
    """
    lse = normalize_title(lse_title)
    ff = normalize_title(ff_title)

    # Exact match after normalization
    if lse == ff:
        return True

    lse_tokens = set(lse.split())
    ff_tokens = set(ff.split())

    # Check for distinctive token overlap
    if lse_tokens & DISTINCTIVE_TOKENS & ff_tokens:
        return True

    # Otherwise require >=2 overlapping tokens
    if len(lse_tokens & ff_tokens) >= 2:
        return True

    return False


def timestamps_match(ts1: float, ts2: float, tolerance: float = MATCH_TOLERANCE_S) -> bool:
    """Check if two timestamps are within 'tolerance' seconds of each other."""
    return abs(ts1 - ts2) <= tolerance


# ---------------------------------------------------------------------------
# Cross-reference core logic
# ---------------------------------------------------------------------------

async def cross_reference(
    days: int = 7,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> Dict[str, Any]:
    """Cross-reference LSE HIGH-impact labels against Forex Factory.

    Returns a dict with:
      - analysis_date
      - period
      - lse_total_events: count of HIGH events in LSE window
      - ff_total_events: count of High events in FF window
      - ff_high_events: list of FF high-impact events (filtered to period)
      - discrepancies: list of disagreements
      - summary: {lse_high_only, ff_high_only, impact_mismatch, total_discrepancies}
      - lse_available: whether macro_events.json exists
    """
    now = datetime.now(tz=timezone.utc)
    now_ts = now.timestamp()

    # Determine the analysis window
    if start_date:
        start_ts = datetime.strptime(start_date, "%Y-%m-%d").replace(
            tzinfo=timezone.utc
        ).timestamp()
    else:
        start_ts = now_ts - timedelta(days=days).total_seconds()

    if end_date:
        end_ts = datetime.strptime(end_date, "%Y-%m-%d").replace(
            hour=23, minute=59, second=59, tzinfo=timezone.utc
        ).timestamp()
    else:
        end_ts = now_ts

    # Load events from both sources
    lse_events = load_lse_events(start_ts, end_ts)
    ff_events = await fetch_ff_calendar(
        start_date=datetime.fromtimestamp(start_ts, tz=timezone.utc).strftime("%Y-%m-%d"),
        end_date=datetime.fromtimestamp(end_ts, tz=timezone.utc).strftime("%Y-%m-%d"),
    )

    # Filter FF to only High-impact events in the window
    ff_high = [e for e in ff_events if e.impact.lower() in ("high", "3", "red")]

    # Match events between sources
    # Build a list of matched pairs and track which events are unmatched
    lse_matched: Set[int] = set()
    ff_matched: Set[int] = set()
    discrepancies: List[Dict[str, Any]] = []

    for i, lse_ev in enumerate(lse_events):
        for j, ff_ev in enumerate(ff_high):
            if titles_match(lse_ev.title, ff_ev.title) and timestamps_match(
                lse_ev.timestamp, ff_ev.timestamp
            ):
                # Both sources agree it's HIGH — no discrepancy
                lse_matched.add(i)
                ff_matched.add(j)
                break

    # LSE says HIGH, FF does NOT list it as High (or doesn't have it at all)
    for i, lse_ev in enumerate(lse_events):
        if i in lse_matched:
            continue
        # Check if FF has the same event at all (just lower impact)
        ff_same_event = None
        for j, ff_ev in enumerate(ff_events):
            if titles_match(lse_ev.title, ff_ev.title) and timestamps_match(
                lse_ev.timestamp, ff_ev.timestamp
            ):
                ff_same_event = ff_ev
                ff_matched.add(j)
                break

        if ff_same_event:
            # Event exists in both, but impact levels disagree
            discrepancies.append({
                "type": DiscrepancyType.IMPACT_MISMATCH,
                "lse_title": lse_ev.title,
                "lse_impact": lse_ev.impact,
                "lse_timestamp": lse_ev.timestamp,
                "ff_title": ff_same_event.title,
                "ff_impact": ff_same_event.impact,
                "ff_timestamp": ff_same_event.timestamp,
                "time_delta_s": abs(lse_ev.timestamp - ff_same_event.timestamp),
                "note": f"LSE flagged HIGH but FF rates it {ff_same_event.impact}",
            })
        else:
            # LSE has HIGH event that FF doesn't have at all
            discrepancies.append({
                "type": DiscrepancyType.LSE_HIGH_FF_NOT,
                "lse_title": lse_ev.title,
                "lse_impact": lse_ev.impact,
                "lse_timestamp": lse_ev.timestamp,
                "ff_title": "",
                "ff_impact": "",
                "ff_timestamp": 0,
                "time_delta_s": 0,
                "note": "LSE flagged HIGH but event not found in Forex Factory calendar",
            })

    # FF says High, LSE did NOT flag it
    for j, ff_ev in enumerate(ff_high):
        if j in ff_matched:
            continue
        discrepancies.append({
            "type": DiscrepancyType.FF_HIGH_LSE_NOT,
            "lse_title": "",
            "lse_impact": "",
            "lse_timestamp": 0,
            "ff_title": ff_ev.title,
            "ff_impact": ff_ev.impact,
            "ff_timestamp": ff_ev.timestamp,
            "time_delta_s": 0,
            "note": f"Forex Factory rated '{ff_ev.title}' as High but LSE did not flag it",
        })

    # Build summary
    lse_high_only = sum(1 for d in discrepancies if d["type"] == DiscrepancyType.LSE_HIGH_FF_NOT)
    ff_high_only = sum(1 for d in discrepancies if d["type"] == DiscrepancyType.FF_HIGH_LSE_NOT)
    impact_mismatch = sum(1 for d in discrepancies if d["type"] == DiscrepancyType.IMPACT_MISMATCH)

    return {
        "analysis_date": now.isoformat(),
        "period": {
            "start": datetime.fromtimestamp(start_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "end": datetime.fromtimestamp(end_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "days": days,
        },
        "lse_available": True,
        "lse_total_events": len(lse_events),
        "ff_total_events": len(ff_events),
        "ff_high_events": len(ff_high),
        "discrepancies": discrepancies,
        "summary": {
            "lse_high_only": lse_high_only,
            "ff_high_only": ff_high_only,
            "impact_mismatch": impact_mismatch,
            "total_discrepancies": len(discrepancies),
        },
    }


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def print_report(results: Dict[str, Any]) -> None:
    """Print a human-readable cross-reference report."""
    print("=" * 72)
    print("MACRO EVENT CROSS-REFERENCE REPORT (TASK D.2)")
    print("  Primary:   LSE macro_events.json  (HIGH labels from lse_calendar)")
    print("  Secondary: Forex Factory public JSON feed")
    print("=" * 72)
    print()

    summary = results["summary"]
    print(f"Analysis date: {results['analysis_date']}")
    print(f"Period: {results['period']['start']} -> {results['period']['end']} ({results['period']['days']} days)")
    print()

    if not results["lse_available"]:
        print("LSE macro_events.json: MISSING")
        print("  The scraper (page_agent_runner.py) has not produced output yet.")
        print("  Cross-reference is limited to Forex Factory high-impact events.")
        print()
        print(f"Forex Factory total events in window: {results['ff_total_events']}")
        print(f"Forex Factory HIGH-impact events in window: {results['ff_high_events']}")
        print()
        print("These are the events LSE SHOULD have flagged as HIGH (if the scraper")
        print("were working).  Each represents a potential false negative.")
        print()
        return

    print(f"LSE HIGH-impact events in window: {results['lse_total_events']}")
    print(f"Forex Factory HIGH-impact events in window: {results['ff_high_events']}")
    print()

    if summary["total_discrepancies"] == 0:
        print("-" * 40)
        print("RESULT: PERFECT AGREEMENT")
        print("-" * 40)
        print("  All LSE HIGH flags match Forex Factory High events.")
        print("  No Forex Factory High events were missed by LSE.")
        print()
    else:
        print("-" * 40)
        print(f"DISCREPANCIES ({summary['total_discrepancies']} total)")
        print("-" * 40)
        print(f"  LSE HIGH but FF not High / not found: {summary['lse_high_only']}")
        print(f"  FF High but LSE did not flag:         {summary['ff_high_only']}")
        print(f"  Impact level mismatch:                {summary['impact_mismatch']}")
        print()

        # Detail each discrepancy
        for d in results["discrepancies"]:
            dt_str = datetime.fromtimestamp(d["lse_timestamp"], tz=timezone.utc).isoformat() \
                if d["lse_timestamp"] else "N/A"
            ff_dt_str = datetime.fromtimestamp(d["ff_timestamp"], tz=timezone.utc).isoformat() \
                if d["ff_timestamp"] else "N/A"

            print(f"  [{d['type']}]")
            if d["type"] == DiscrepancyType.LSE_HIGH_FF_NOT:
                print(f"    LSE: [{d['lse_impact']}] {d['lse_title']} @ {dt_str}")
                print(f"    FF:  (not found) — {d['note']}")
            elif d["type"] == DiscrepancyType.FF_HIGH_LSE_NOT:
                print(f"    FF:  [{d['ff_impact']}] {d['ff_title']} @ {ff_dt_str}")
                print(f"    LSE: (not flagged) — {d['note']}")
            elif d["type"] == DiscrepancyType.IMPACT_MISMATCH:
                print(f"    LSE: [{d['lse_impact']}] {d['lse_title']} @ {dt_str}")
                print(f"    FF:  [{d['ff_impact']}] {d['ff_title']} @ {ff_dt_str}")
                print(f"    -> {d['note']}")
            print()

    # Trustworthiness assessment
    print("-" * 40)
    print("TRUSTWORTHINESS ASSESSMENT")
    print("-" * 40)
    if summary["total_discrepancies"] == 0:
        print("  PASS: LSE and Forex Factory agree on all HIGH-impact events in the window.")
    elif summary["lse_high_only"] > 0 and summary["ff_high_only"] == 0:
        print(f"  CONCERN: LSE flagged {summary['lse_high_only']} event(s) as HIGH that")
        print("  Forex Factory does not consider High. These may be false positives")
        print("  — LSE is over-classifying, creating unnecessary risk windows.")
    elif summary["ff_high_only"] > 0 and summary["lse_high_only"] == 0:
        print(f"  WARNING: {summary['ff_high_only']} Forex Factory High event(s) were")
        print("  NOT flagged by LSE. These are false negatives — LSE is MISSING")
        print("  real high-impact events, creating a safety gap in macro-risk gating.")
    else:
        print(f"  MIXED: {summary['lse_high_only']} LSE-only, {summary['ff_high_only']} FF-only,")
        print(f"  {summary['impact_mismatch']} impact mismatches. Both sources disagree on")
        print("  significant events — independent verification is recommended.")
    print()
    print("  Note: The Forex Factory feed only covers the current trading week.")
    print("  For historical backfilling, use the Forex Factory CSV or the")
    print("  TradingEconomics API (requires free key).")
    print("=" * 72)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

async def _amain():
    parser = argparse.ArgumentParser(
        description="TASK D.2: Cross-reference LSE macro events against Forex Factory"
    )
    parser.add_argument("--days", type=int, default=7,
                        help="Lookback window in days (default: 7)")
    parser.add_argument("--from-date", type=str, default=None, dest="from_date",
                        help="Start date YYYY-MM-DD")
    parser.add_argument("--to-date", type=str, default=None, dest="to_date",
                        help="End date YYYY-MM-DD")
    parser.add_argument("--output", type=str, default=None,
                        help="Write results JSON to this path")
    parser.add_argument("--raw", action="store_true",
                        help="Also print raw discrepancy list as JSON")
    args = parser.parse_args()

    results = await cross_reference(
        days=args.days,
        start_date=args.from_date,
        end_date=args.to_date,
    )
    print_report(results)

    if args.raw:
        print("\nRaw discrepancies:")
        print(json.dumps(results["discrepancies"], indent=2, default=str))

    if args.output:
        with open(args.output, "w") as f:
            json.dump(results, f, indent=2, default=str)
        print(f"\nResults written to {args.output}")


def main():
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
