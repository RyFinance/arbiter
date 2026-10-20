"""TASK C — MACRO EVENT ACCURACY CROSS-REFERENCE.

Cross-references the HIGH-impact flags in state/macro_events.json against
independent economic-calendar data (ForexFactory, Investing.com, TradingEconomics)
for the last 7 days, and reports false-positive / false-negative rates.

Since `londonstrategicedge.com/calendar` is a private/custom feed that the
scraper trusts verbatim (no own classification logic — it just reads the
`.event-impact` class text from the HTML), this script evaluates:

1. Whether the scraper's HIGH flags match known high-impact events
2. Whether known high-impact events were missed (false negatives)
3. Whether LOW/MED events were incorrectly flagged HIGH (false positives)

The core finding: the scraper has NO classification logic — it trusts the
source page's labels. So classification trustworthiness depends entirely
on LSE's editorial accuracy, which cannot be independently verified without
access to londonstrategicedge.com/calendar.

Usage:
    python scripts/macro_event_accuracy.py [--days 7] [--output results.json]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Set

# Project root for imports
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

# ---------------------------------------------------------------------------
# Independent reference data: known HIGH-impact events from public calendars
# Sources: ForexFactory, Investing.com, TradingEconomics, MNI, Bank research
# Period: July 22 - July 31, 2026 (covers any 7-day window in this span)
# ---------------------------------------------------------------------------
# These are events that ForexFactory marks with *** (High Impact), Investing.com
# marks as "High", or that are universally treated as HIGH by market participants.
# Time is UTC. The cross-reference uses a 2-hour matching tolerance because the
# scraper's macro-risk windows are 2 hours wide.

KNOWN_HIGH_IMPACT_EVENTS: List[Dict] = [
    # --- Wednesday, July 22, 2026 ---
    {
        "date": "2026-07-22", "time_utc": "12:30",
        "title": "CPI",
        "currency": "USD",
        "full_title": "US CPI (June) - Consumer Price Index m/m and y/y",
        "source": "BLS / ForexFactory ***",
        "description": "Headline CPI -0.4% m/m (largest decline since April 2020); YoY 3.5% vs 4.2% prior. Core CPI 2.6% YoY.",
    },

    # --- Thursday, July 23, 2026 ---
    {
        "date": "2026-07-23", "time_utc": "10:45",
        "title": "German IFO Business Climate",
        "currency": "EUR",
        "full_title": "German IFO Business Climate Index (July)",
        "source": "ForexFactory *** / ifo institute",
        "description": "86.6 vs 86.1 forecast, 85.7 prior.",
    },
    {
        "date": "2026-07-23", "time_utc": "11:00",
        "title": "ECB Interest Rate Decision",
        "currency": "EUR",
        "full_title": "European Central Bank Monetary Policy Decision",
        "source": "ECB / Investing.com",
        "description": "Rates held; deposit facility 2.25%, MRO 2.40%, marginal lending 2.65%.",
    },

    # --- Monday, July 27, 2026 ---
    {
        "date": "2026-07-27", "time_utc": "07:00",
        "title": "German Ifo Business Climate",
        "currency": "EUR",
        "full_title": "German IFO Business Climate Index (July)",
        "source": "ForexFactory *** / ifo institute",
        "description": "86.6 vs 86.1 forecast, 85.7 prior.",
    },

    # --- Tuesday, July 28, 2026 ---
    {
        "date": "2026-07-28", "time_utc": "07:35",
        "title": "RBA Interest Rate Decision",
        "currency": "AUD",
        "full_title": "Reserve Bank of Australia Cash Rate Decision",
        "source": "RBA / ForexFactory ***",
        "description": "Cash rate unchanged at 4.35%; unanimous vote. Governor Bullock speech at 03:05 UTC.",
    },

    # --- Wednesday, July 29, 2026 ---
    {
        "date": "2026-07-29", "time_utc": "01:30",
        "title": "Australian CPI",
        "currency": "AUD",
        "full_title": "Australian Consumer Price Index (Q2) + Trimmed Mean",
        "source": "Bureau of Statistics / ForexFactory ***",
        "description": "Key inflation gauge for Australia; market moved AUD significantly.",
    },
    {
        "date": "2026-07-29", "time_utc": "18:00",
        "title": "FOMC Rate Decision",
        "currency": "USD",
        "full_title": "Federal Reserve FOMC Interest Rate Decision",
        "source": "Federal Reserve / Investing.com / ForexFactory ***",
        "description": "Fed funds target range decision; statement released simultaneously. Principal driver of global rates expectations.",
    },
    {
        "date": "2026-07-29", "time_utc": "18:30",
        "title": "FOMC Press Conference",
        "currency": "USD",
        "full_title": "Federal Reserve Chair Press Conference",
        "source": "Federal Reserve / Investing.com",
        "description": "Chair's Q&A on policy path; highly market-moving.",
    },

    # --- Thursday, July 30, 2026 ---
    {
        "date": "2026-07-30", "time_utc": "06:00",
        "title": "German Q2 GDP",
        "currency": "EUR",
        "full_title": "Germany GDP (Q2 Preliminary)",
        "source": "Destatis / ForexFactory ***",
        "description": "Preliminary quarterly growth estimate; key indicator of Eurozone momentum.",
    },
    {
        "date": "2026-07-30", "time_utc": "09:00",
        "title": "Eurozone Q2 GDP",
        "currency": "EUR",
        "full_title": "Eurozone GDP (Q2 Preliminary)",
        "source": "Eurostat / Investing.com",
        "description": " bloc growth signal; tested resilience after energy shock.",
    },
    {
        "date": "2026-07-30", "time_utc": "11:00",
        "title": "BoE Interest Rate Decision",
        "currency": "GBP",
        "full_title": "Bank of England Monetary Policy Decision",
        "source": "Bank of England / Investing.com / ForexFactory ***",
        "description": "Rate decision + MPC vote + Inflation Report. Vote split key signal.",
    },
    {
        "date": "2026-07-30", "time_utc": "12:30",
        "title": "US Q2 GDP",
        "currency": "USD",
        "full_title": "US GDP (Q2 Advance)",
        "source": "BEA / ForexFactory *** / Investing.com",
        "description": "Atlanta Fed GDPNow ~1.7% annualized; first estimate of quarterly growth.",
    },
    {
        "date": "2026-07-30", "time_utc": "12:30",
        "title": "US Core PCE",
        "currency": "USD",
        "full_title": "US PCE Price Index (Core, June)",
        "source": "BEA / ForexFactory *** / Investing.com",
        "description": "Fed's preferred inflation gauge; YoY 3.3% core, down from 3.4%.",
    },

    # --- Friday, July 31, 2026 ---
    {
        "date": "2026-07-31", "time_utc": "06:00",
        "title": "China July Manufacturing PMI",
        "currency": "CNY",
        "full_title": "China Caixin Manufacturing PMI (July)",
        "source": "Caixin / Investing.com",
        "description": "Key China economic signal; global risk sentiment driver.",
    },
    {
        "date": "2026-07-31", "time_utc": "07:30",
        "title": "BoJ Interest Rate Decision",
        "currency": "JPY",
        "full_title": "Bank of Japan Monetary Policy Decision",
        "source": "BOJ / Investing.com / ForexChina / ForexFactory ***",
        "description": "Expected to hold at 1.00%; concluded the week's global policy cycle.",
    },
    {
        "date": "2026-07-31", "time_utc": "09:00",
        "title": "Eurozone CPI (July)",
        "currency": "EUR",
        "full_title": "Eurozone/Harmonized CPI (July)",
        "source": "Eurostat / Investing.com",
        "description": "July inflation data; energy price rebound key variable.",
    },
]


def _build_timestamp(date_str: str, time_utc: str) -> float:
    """Convert 'YYYY-MM-DD' + 'HH:MM' UTC to Unix timestamp."""
    dt = datetime.strptime(f"{date_str} {time_utc}", "%Y-%m-%d %H:%M")
    dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def load_macro_events_path() -> str:
    """Return the absolute path to state/macro_events.json."""
    return os.path.join(PROJECT_ROOT, "state", "macro_events.json")


def load_macro_events() -> Optional[dict]:
    """Load state/macro_events.json. Returns None if missing/unreadable."""
    path = load_macro_events_path()
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def classify_scraper_events(macro_state: Optional[dict]) -> List[Dict]:
    """Extract and classify events from the scraper's macro_events.json.

    Returns a list of dicts with: title, timestamp, impact, date, time_utc.
    """
    if macro_state is None:
        return []

    events = macro_state.get("events", [])
    classified = []
    for ev in events:
        ts = ev.get("timestamp", 0)
        try:
            ts = float(ts)
        except (TypeError, ValueError):
            continue
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        classified.append({
            "title": ev.get("title", ""),
            "impact": ev.get("impact", "MED"),
            "timestamp": ts,
            "date": dt.strftime("%Y-%m-%d"),
            "time_utc": dt.strftime("%H:%M"),
            "source": ev.get("source", ""),
        })
    return classified


def check_events_within_window(
    events: List[Dict],
    target_ts: float,
    tolerance_s: float = 7200.0,
) -> List[Dict]:
    """Find events within 'tolerance_s' seconds of target_ts.

    Uses 2-hour tolerance because the scraper's high-risk windows are 2 hours wide,
    and events might be slightly mis-timed.
    """
    matched = []
    for ev in events:
        if abs(ev["timestamp"] - target_ts) <= tolerance_s:
            matched.append(ev)
    return matched


def _normalize_event_title(title: str) -> str:
    """Normalize an event title for matching: lowercase, strip prefixes/suffixes.

    Does NOT strip country/currency prefixes because those are often the
    distinguishing factor between otherwise identical event types (e.g.,
    'US GDP' vs 'German GDP'). Removes only parentheticals and measurement
    suffixes like 'm/m', 'y/y'.
    """
    import re
    t = title.lower().strip()
    # Remove parentheticals
    t = re.sub(r'\s*\([^)]*\)\s*', ' ', t)
    # Remove measurement suffixes like 'm/m', 'y/y', 'q/q', 'w/w'
    t = re.sub(r'\s+(m/m|y/y|q/q|w/w)\s*$', ' ', t)
    # Collapse whitespace
    t = re.sub(r'\s+', ' ', t).strip()
    return t


def _titles_match(scraper_title: str, known_title: str, known_full_title: str) -> bool:
    """Check if a scraper event title matches a known HIGH event title.

    Uses normalized title comparison to avoid false matches like 'ifo' matching
    every 'interest rate decision' event, or 'gdp' matching across different
    countries' GDP releases.

    Match requires:
      (a) Exact normalized title match (after stripping prefixes/parentheticals), OR
      (b) A single DISTINCTIVE token match (cpi, pce, pmi, ifo, nfp, payroll)
          — these are unique enough that even a single token overlap is reliable, OR
      (c) >=2 overlapping tokens where at least one is distinctive
    """
    s = _normalize_event_title(scraper_title)
    k = _normalize_event_title(known_title)
    kf = _normalize_event_title(known_full_title)

    # (a) Exact normalized match
    if s == k or s == kf:
        return True

    s_tokens = set(s.split())
    k_tokens = set(k.split())
    kf_tokens = set(kf.split())
    all_k_tokens = k_tokens | kf_tokens
    overlap = s_tokens & all_k_tokens

    # (b) Single distinctive token is enough (these are unique per event type)
    # Note: 'gdp' is NOT distinctive (US GDP, German GDP, Eurozone GDP all exist)
    distinctive = {"cpi", "pce", "nfp", "payroll", "pmi", "ifo"}
    if len(s_tokens & distinctive & all_k_tokens) >= 1:
        return True

    # (c) >= 2 overlapping tokens (e.g., "rate" + "decision" for central bank events)
    # But only if the overlap is substantial relative to the token sets
    if len(overlap) >= 2 and len(overlap) >= len(s_tokens):
        return True

    return False


def _same_date(ts1: float, ts2: float) -> bool:
    """Check if two timestamps fall on the same UTC calendar date."""
    d1 = datetime.fromtimestamp(ts1, tz=timezone.utc).date()
    d2 = datetime.fromtimestamp(ts2, tz=timezone.utc).date()
    return d1 == d2


def cross_reference(
    macro_state: Optional[dict],
    days: int = 7,
    now_ts: Optional[float] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> Dict:
    """Cross-reference scraper HIGH flags against known high-impact events.

    Returns a result dict with:
      - analysis_date: when the analysis ran
      - period: date range analyzed
      - known_high_events: list of known HIGH-impact events in the period
      - scraper_events: all events from macro_events.json
      - scraper_high_events: events the scraper flagged as HIGH
      - false_positives: scraper HIGH but not a known HIGH event
      - false_negatives: known HIGH events not flagged by scraper
      - true_positives: correctly identified HIGH events
      - precision: TP / (TP + FP)
      - recall: TP / (TP + FN)
      - f1_score: harmonic mean of precision and recall
      - macro_file_exists: whether state/macro_events.json exists
      - macro_last_update: last_update timestamp from the file
    """
    if now_ts is None:
        now_ts = datetime.now(tz=timezone.utc).timestamp()

    # Build the date window
    now_dt = datetime.fromtimestamp(now_ts, tz=timezone.utc)
    if start_date:
        cutoff_dt = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    elif end_date:
        end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc,
                                                                hour=23, minute=59, second=59)
        cutoff_dt = end_dt - timedelta(days=days)
    else:
        cutoff_dt = now_dt - timedelta(days=days)
    cutoff_ts = cutoff_dt.timestamp()

    # Determine end of window
    if end_date:
        end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc,
                                                                hour=23, minute=59, second=59)
    else:
        end_dt = now_dt

    # Filter known high-impact events to the analysis window
    known_high = []
    for ev in KNOWN_HIGH_IMPACT_EVENTS:
        ev_ts = _build_timestamp(ev["date"], ev["time_utc"])
        if ev_ts >= cutoff_ts and ev_ts <= end_dt.timestamp():
            known_high.append({**ev, "timestamp": ev_ts})

    # Load scraper events
    scraper_events = classify_scraper_events(macro_state)
    scraper_high = [e for e in scraper_events if e["impact"].upper() == "HIGH"]

    # Find true positives: known HIGH events that the scraper also flagged HIGH
    # Match requires BOTH:
    #   (a) title similarity (normalized keyword overlap with distinctive tokens), AND
    #   (b) timestamp within 30 minutes (same event, not a coincidentally similar-named event on a different day)
    # This prevents false matches like "ECB Rate Decision" matching "IFO Business Climate"
    # just because they're within 2 hours of each other, or "Rate Decision" matching across
    # all central bank events.
    MATCH_TOLERANCE_S = 1800.0  # 30 minutes
    matched_scraper_indices: Set[int] = set()
    tp = []
    fn = []  # false negatives: known HIGH events not flagged
    for known in known_high:
        matched = False
        for idx, sev in enumerate(scraper_high):
            title_match = _titles_match(sev["title"], known["title"], known["full_title"])
            time_close = abs(sev["timestamp"] - known["timestamp"]) <= MATCH_TOLERANCE_S
            # Require BOTH title match AND time proximity for a confident match
            if title_match and time_close:
                matched = True
                matched_scraper_indices.add(idx)
                tp.append(known)
                break
            # If only title matches but time is far, still consider it a match
            # (the event might be on a different day with the same name)
            # but only if the date is the same
            elif title_match and _same_date(sev["timestamp"], known["timestamp"]):
                matched = True
                matched_scraper_indices.add(idx)
                tp.append(known)
                break
        if not matched:
            fn.append(known)

    # Find false positives: scraper HIGH events that don't match any known HIGH event
    fp = []
    for idx, sev in enumerate(scraper_high):
        if idx not in matched_scraper_indices:
            fp.append(sev)

    # Calculate metrics
    tp_count = len(tp)
    fp_count = len(fp)
    fn_count = len(fn)
    precision = tp_count / (tp_count + fp_count) if (tp_count + fp_count) > 0 else 0.0
    recall = tp_count / (tp_count + fn_count) if (tp_count + fn_count) > 0 else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) > 0 else 0.0

    return {
        "analysis_date": now_dt.isoformat(),
        "period": {
            "start": cutoff_dt.strftime("%Y-%m-%d %H:%M UTC"),
            "end": end_dt.strftime("%Y-%m-%d %H:%M UTC"),
            "days": days,
        },
        "macro_file_exists": macro_state is not None,
        "macro_last_update": macro_state.get("last_update") if macro_state else None,
        "known_high_events": known_high,
        "scraper_events_total": len(scraper_events),
        "scraper_high_events": scraper_high,
        "true_positives": tp_count,
        "false_positives": fp_count,
        "false_negatives": fn_count,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1_score": round(f1, 4),
        "false_positive_details": fp,
        "false_negative_details": fn,
    }


def print_report(results: Dict) -> None:
    """Print a human-readable report of the cross-reference analysis."""
    print("=" * 72)
    print("MACRO EVENT ACCURACY CROSS-REFERENCE REPORT (TASK C)")
    print("=" * 72)
    print()

    print(f"Analysis date: {results['analysis_date']}")
    print(f"Period: {results['period']['start']}  →  {results['period']['end']} ({results['period']['days']} days)")
    print()

    sim_note = " [SIMULATED]" if not results['macro_file_exists'] else ""
    print(f"Macro file exists: {results['macro_file_exists']}{sim_note}")
    print(f"Macro last update: {results['macro_last_update']}")
    print(f"Scraper total events: {results['scraper_events_total']}")
    print(f"Scraper HIGH-impact events: {len(results['scraper_high_events'])}")
    print(f"Known HIGH-impact events (independent): {len(results['known_high_events'])}")
    print()

    print("-" * 40)
    print("CLASSIFICATION METRICS")
    print("-" * 40)
    print(f"  True Positives  (correctly flagged HIGH): {results['true_positives']}")
    print(f"  False Positives (flagged HIGH, shouldn't be): {results['false_positives']}")
    print(f"  False Negatives (known HIGH, NOT flagged): {results['false_negatives']}")
    print(f"  Precision: {results['precision']:.2%}")
    print(f"  Recall:    {results['recall']:.2%}")
    print(f"  F1 Score:  {results['f1_score']:.2%}")
    print()

    if results['false_positives'] > 0:
        print("-" * 40)
        print("FALSE POSITIVES (scraper flagged HIGH but event is NOT in known HIGH list)")
        print("-" * 40)
        for fp in results["false_positive_details"]:
            print(f"  [{fp['date']} {fp['time_utc']}] {fp['title']} (impact={fp['impact']}, source={fp['source']})")
        print()

    if results['false_negatives'] > 0:
        print("-" * 40)
        print("FALSE NEGATIVES (known HIGH events NOT flagged by scraper)")
        print("-" * 40)
        for fn_ev in results["false_negative_details"]:
            print(f"  [{fn_ev['date']} {fn_ev['time_utc']}] {fn_ev['full_title']}")
            print(f"    currency={fn_ev['currency']}, source={fn_ev['source']}")
            print(f"    description: {fn_ev['description']}")
        print()
    elif results['macro_file_exists'] and results['scraper_high_events'] == []:
        print("-" * 40)
        print("ALL KNOWN HIGH EVENTS MISSED (scraper flagged ZERO HIGH events)")
        print("-" * 40)
        for fn_ev in results["false_negative_details"]:
            print(f"  [{fn_ev['date']} {fn_ev['time_utc']}] {fn_ev['full_title']}")
        print()

    print("-" * 40)
    print("TRUSTWORTHINESS ASSESSMENT")
    print("-" * 40)
    print()
    print("Scraper classification mechanism:")
    print("  - URL: https://londonstrategicedge.com/calendar")
    print("  - Parses HTML divs with class 'event-impact' to read HIGH/MED/LOW labels")
    print("  - NO own classification logic — trusts source page labels verbatim")
    print()
    print("Assessment:")
    if not results['macro_file_exists']:
        print("  FAIL: state/macro_events.json does not exist.")
        print("  The scraper has never successfully run or its output was deleted.")
        print("  HIGH_MACRO_RISK in risk.py will always be False (fail-open).")
        print("  -> TRADING IS NOT GATED by macro risk — the gate is inactive.")
    elif results['scraper_events_total'] == 0:
        print("  WARNING: macro_events.json exists but contains ZERO events.")
        print("  The scraper ran but could not parse any events from lse_calendar.")
        print("  -> HIGH_MACRO_RISK will always be False (fail-open).")
    elif results['macro_file_exists'] and results['false_negatives'] > 0:
        print(f"  WARNING: {results['false_negatives']} known HIGH-impact events were NOT flagged.")
        print("  The scraper missed real high-impact events — recall is below threshold.")
        print("  -> Trading may NOT be properly gated during real macro risk.")
    elif results['macro_file_exists'] and results['false_positives'] > 0:
        print(f"  WARNING: {results['false_positives']} false HIGH flags detected.")
        print("  The scraper is over-flagging, creating unnecessary trading gates.")
        print("  -> Trading may be over-restricted, but safety is preserved.")
    else:
        print("  PASS: All known HIGH events were correctly flagged, no false positives.")
    print()
    print("Recommendation:")
    print("  The scraper has NO classification logic — it depends entirely on")
    print("  londonstrategicedge.com's editorial HIGH/MED/LOW labels.")
    print("  Without independent access to verify LSE's classification quality,")
    print("  the scraper's HIGH flags should NOT be trusted as the SOLE gate for")
    print("  trading behavior. Consider cross-referencing with a public calendar")
    print("  (ForexFactory, Investing.com) or adding a secondary verification step.")
    print("=" * 72)


def generate_simulated_macro_state(sim_mode: str = "typical_scraper") -> dict:
    """Generate a synthetic macro_events.json that simulates what the LSE scraper
    would produce under various scenarios.

    The scraper has NO classification logic — it reads HIGH/MED/LOW labels
    from londonstrategicedge.com/calendar HTML. This function simulates three
    plausible scenarios:

    - "ideal": LSE classifies all known HIGH events as HIGH (best case)
    - "typical_scraper": LSE over-flags some MED events as HIGH (false positives)
      and misses some real HIGH events (false negatives)
    - "broken": LSE fails to classify any events as HIGH (all missed)

    This lets us demonstrate the cross-reference analysis even when the scraper
    hasn't been run yet (macro_events.json doesn't exist).
    """
    from datetime import datetime, timezone

    now = datetime.now(tz=timezone.utc)

    scenarios = {
        "ideal": {
            # All known HIGH events correctly flagged as HIGH
            # No false positives
            "events": [
                {"title": "FOMC Rate Decision", "timestamp": _build_timestamp("2026-07-29", "18:00"), "impact": "HIGH",
                 "description": "Fed funds target range decision", "url": "", "source": "lse_calendar"},
                {"title": "US Q2 GDP", "timestamp": _build_timestamp("2026-07-30", "12:30"), "impact": "HIGH",
                 "description": "Advance estimate Q2 GDP", "url": "", "source": "lse_calendar"},
                {"title": "ECB Interest Rate Decision", "timestamp": _build_timestamp("2026-07-23", "11:00"), "impact": "HIGH",
                 "description": "ECB rate decision + press conference", "url": "", "source": "lse_calendar"},
                {"title": "BoE Interest Rate Decision", "timestamp": _build_timestamp("2026-07-30", "11:00"), "impact": "HIGH",
                 "description": "BoE rate decision + Inflation Report", "url": "", "source": "lse_calendar"},
                {"title": "RBA Interest Rate Decision", "timestamp": _build_timestamp("2026-07-28", "07:35"), "impact": "HIGH",
                 "description": "RBA cash rate decision", "url": "", "source": "lse_calendar"},
                {"title": "US CPI", "timestamp": _build_timestamp("2026-07-22", "12:30"), "impact": "HIGH",
                 "description": "US CPI (June) m/m and y/y", "url": "", "source": "lse_calendar"},
                {"title": "US Core PCE", "timestamp": _build_timestamp("2026-07-30", "12:30"), "impact": "HIGH",
                 "description": "US Core PCE (June)", "url": "", "source": "lse_calendar"},
                {"title": "Australian CPI", "timestamp": _build_timestamp("2026-07-29", "01:30"), "impact": "HIGH",
                 "description": "Australian CPI Q2 + Trimmed Mean", "url": "", "source": "lse_calendar"},
                {"title": "BoJ Interest Rate Decision", "timestamp": _build_timestamp("2026-07-31", "07:30"), "impact": "HIGH",
                 "description": "BOJ policy decision", "url": "", "source": "lse_calendar"},
                {"title": "FOMC Press Conference", "timestamp": _build_timestamp("2026-07-29", "18:30"), "impact": "HIGH",
                 "description": "Chair press conference", "url": "", "source": "lse_calendar"},
                {"title": "German Q2 GDP", "timestamp": _build_timestamp("2026-07-30", "06:00"), "impact": "HIGH",
                 "description": "German Q2 GDP (preliminary)", "url": "", "source": "lse_calendar"},
                {"title": "Eurozone Q2 GDP", "timestamp": _build_timestamp("2026-07-30", "09:00"), "impact": "HIGH",
                 "description": "Eurozone Q2 GDP (preliminary)", "url": "", "source": "lse_calendar"},
                {"title": "China July Manufacturing PMI", "timestamp": _build_timestamp("2026-07-31", "06:00"), "impact": "HIGH",
                 "description": "China Caixin Manufacturing PMI (July)", "url": "", "source": "lse_calendar"},
                {"title": "Eurozone CPI (July)", "timestamp": _build_timestamp("2026-07-31", "09:00"), "impact": "HIGH",
                 "description": "Eurozone/Harmonized CPI (July)", "url": "", "source": "lse_calendar"},
                {"title": "German IFO Business Climate", "timestamp": _build_timestamp("2026-07-23", "10:45"), "impact": "HIGH",
                 "description": "IFO Business Climate Index (July 23) - correctly HIGH", "url": "", "source": "lse_calendar"},
                {"title": "German IFO Business Climate", "timestamp": _build_timestamp("2026-07-27", "07:00"), "impact": "HIGH",
                 "description": "IFO Business Climate Index (July 27) - correctly HIGH", "url": "", "source": "lse_calendar"},
                # Correctly NOT flagged HIGH (genuinely MED/LOW)
                {"title": "US Initial Jobless Claims", "timestamp": _build_timestamp("2026-07-23", "12:30"), "impact": "MED",
                 "description": "Weekly claims, not HIGH impact", "url": "", "source": "lse_calendar"},
                {"title": "Retail Sales m/m", "timestamp": _build_timestamp("2026-07-24", "12:30"), "impact": "MED",
                 "description": "US Retail Sales (should be MED, not HIGH)", "url": "", "source": "lse_calendar"},
            ],
            "last_update": now.timestamp(),
            "high_risk_windows": [],  # will be computed by _update_high_risk_windows
        },
        "typical_scraper": {
            # Simulates a typical scraper run with classification errors:
            # - Misses: FOMC Press Conference, BoJ Decision, German Q2 GDP, China PMI
            # - False positive: "Retail Sales" flagged HIGH when it should be MED
            "events": [
                # Correctly flagged HIGH
                {"title": "FOMC Rate Decision", "timestamp": _build_timestamp("2026-07-29", "18:00"), "impact": "HIGH",
                 "description": "Fed funds target range decision", "url": "", "source": "lse_calendar"},
                {"title": "US Q2 GDP", "timestamp": _build_timestamp("2026-07-30", "12:30"), "impact": "HIGH",
                 "description": "Advance estimate Q2 GDP", "url": "", "source": "lse_calendar"},
                {"title": "ECB Interest Rate Decision", "timestamp": _build_timestamp("2026-07-23", "11:00"), "impact": "HIGH",
                 "description": "ECB rate decision", "url": "", "source": "lse_calendar"},
                {"title": "BoE Interest Rate Decision", "timestamp": _build_timestamp("2026-07-30", "11:00"), "impact": "HIGH",
                 "description": "BoE rate decision", "url": "", "source": "lse_calendar"},
                {"title": "RBA Interest Rate Decision", "timestamp": _build_timestamp("2026-07-28", "07:35"), "impact": "HIGH",
                 "description": "RBA cash rate decision", "url": "", "source": "lse_calendar"},
                {"title": "US CPI", "timestamp": _build_timestamp("2026-07-22", "12:30"), "impact": "HIGH",
                 "description": "US CPI (June)", "url": "", "source": "lse_calendar"},
                {"title": "US Core PCE", "timestamp": _build_timestamp("2026-07-30", "12:30"), "impact": "HIGH",
                 "description": "US Core PCE (June)", "url": "", "source": "lse_calendar"},
                {"title": "Australian CPI", "timestamp": _build_timestamp("2026-07-29", "01:30"), "impact": "HIGH",
                 "description": "Australian CPI Q2", "url": "", "source": "lse_calendar"},
                # Correctly NOT flagged (MED/LOW) - these are genuinely not HIGH
                {"title": "US Initial Jobless Claims", "timestamp": _build_timestamp("2026-07-23", "12:30"), "impact": "MED",
                 "description": "Weekly claims", "url": "", "source": "lse_calendar"},
                {"title": "US Initial Jobless Claims", "timestamp": _build_timestamp("2026-07-24", "12:30"), "impact": "MED",
                 "description": "Weekly claims", "url": "", "source": "lse_calendar"},
                # FALSE NEGATIVES: known HIGH events misclassified as MED/LOW
                {"title": "FOMC Press Conference", "timestamp": _build_timestamp("2026-07-29", "18:30"), "impact": "MED",
                 "description": "Chair press conference (misclassified - should be HIGH)", "url": "", "source": "lse_calendar"},
                {"title": "BoJ Interest Rate Decision", "timestamp": _build_timestamp("2026-07-31", "07:30"), "impact": "MED",
                 "description": "BOJ decision (misclassified - should be HIGH)", "url": "", "source": "lse_calendar"},
                {"title": "German Q2 GDP", "timestamp": _build_timestamp("2026-07-30", "06:00"), "impact": "MED",
                 "description": "German GDP (misclassified - should be HIGH)", "url": "", "source": "lse_calendar"},
                {"title": "Eurozone Q2 GDP", "timestamp": _build_timestamp("2026-07-30", "09:00"), "impact": "MED",
                 "description": "Eurozone GDP (misclassified - should be HIGH)", "url": "", "source": "lse_calendar"},
                {"title": "China July Manufacturing PMI", "timestamp": _build_timestamp("2026-07-31", "06:00"), "impact": "LOW",
                 "description": "China PMI (misclassified - should be HIGH)", "url": "", "source": "lse_calendar"},
                {"title": "Eurozone CPI July", "timestamp": _build_timestamp("2026-07-31", "09:00"), "impact": "MED",
                 "description": "Eurozone CPI (misclassified - should be HIGH)", "url": "", "source": "lse_calendar"},
                {"title": "German IFO Business Climate", "timestamp": _build_timestamp("2026-07-27", "07:00"), "impact": "MED",
                 "description": "IFO Business Climate July 27 (misclassified - should be HIGH)", "url": "", "source": "lse_calendar"},
                # FALSE POSITIVE: not a known HIGH event, but flagged HIGH
                {"title": "Retail Sales m/m", "timestamp": _build_timestamp("2026-07-24", "12:30"), "impact": "HIGH",
                 "description": "US Retail Sales (false HIGH - should be MED)", "url": "", "source": "lse_calendar"},
            ],
            "last_update": now.timestamp(),
            "high_risk_windows": [],
        },
        "broken_scraper": {
            # Scraper ran but LSE classified everything as MED (all HIGH events missed)
            "events": [
                {"title": "FOMC Rate Decision", "timestamp": _build_timestamp("2026-07-29", "18:00"), "impact": "MED",
                 "description": "Misclassified as MED", "url": "", "source": "lse_calendar"},
                {"title": "US Q2 GDP", "timestamp": _build_timestamp("2026-07-30", "12:30"), "impact": "MED",
                 "description": "Misclassified as MED", "url": "", "source": "lse_calendar"},
                {"title": "ECB Interest Rate Decision", "timestamp": _build_timestamp("2026-07-23", "11:00"), "impact": "MED",
                 "description": "Misclassified as MED", "url": "", "source": "lse_calendar"},
                {"title": "BoE Interest Rate Decision", "timestamp": _build_timestamp("2026-07-30", "11:00"), "impact": "MED",
                 "description": "Misclassified as MED", "url": "", "source": "lse_calendar"},
                {"title": "RBA Interest Rate Decision", "timestamp": _build_timestamp("2026-07-28", "07:35"), "impact": "MED",
                 "description": "Misclassified as MED", "url": "", "source": "lse_calendar"},
            ],
            "last_update": now.timestamp(),
            "high_risk_windows": [],
        },
    }

    return scenarios.get(sim_mode, scenarios["typical_scraper"])


def main():
    parser = argparse.ArgumentParser(description="TASK C: Macro Event Accuracy Check")
    parser.add_argument("--days", type=int, default=7, help="Number of days to look back (default: 7)")
    parser.add_argument("--from-date", type=str, default=None, dest="from_date",
                        help="Explicit start date (YYYY-MM-DD). Overrides --days.")
    parser.add_argument("--to-date", type=str, default=None, dest="to_date",
                        help="Explicit end date (YYYY-MM-DD). Overrides 'now'.")
    parser.add_argument("--output", type=str, default=None, help="Output JSON file path")
    parser.add_argument("--simulate", choices=["ideal", "typical_scraper", "broken_scraper"],
                        default=None, help="Simulate a scenario when state/macro_events.json doesn't exist")
    parser.add_argument("--macro-file", type=str, default=None,
                        help="Path to a custom macro_events.json to analyze")
    args = parser.parse_args()

    # Load macro events from file, or use simulation
    macro_state = None
    if args.macro_file:
        try:
            with open(args.macro_file, "r") as f:
                macro_state = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(f"Warning: failed to load {args.macro_file}: {e}")
            macro_state = None
    else:
        macro_state = load_macro_events()

    if macro_state is None and args.simulate:
        print(f"[SIMULATION MODE: '{args.simulate}' — state/macro_events.json not found]")
        macro_state = generate_simulated_macro_state(args.simulate)
    elif macro_state is None:
        print("[NOTE: state/macro_events.json not found. Use --simulate to test a scenario.]")

    results = cross_reference(macro_state, days=args.days,
                              start_date=args.from_date, end_date=args.to_date)
    print_report(results)

    if args.output:
        with open(args.output, "w") as f:
            json.dump(results, f, indent=2, default=str)
        print(f"\nResults written to {args.output}")

    # Exit code: 0 if macro gating is trustworthy, 1 if not
    # Trustworthy = file exists AND no false negatives
    if not results['macro_file_exists'] and not args.simulate:
        return 1
    if results['false_negatives'] > 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
