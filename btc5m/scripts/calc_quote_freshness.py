"""A2: Calculate real quote freshness across all logged sessions.

Parses state/bot.jsonl (JSONL) and/or console logs (session_*.log) for TRADE
events (filled + missed) and computes the real percentage of fills within
500ms of quote age.

Also parses FIRE latency from console logs when bot.jsonl is incomplete
(e.g., during a live session that hasn't finished).

Target: >=95% of fills must be within the freshness gate (500ms).
Reports PASS/FAIL based on the real number.

Usage:
    .venv/bin/python scripts/calc_quote_freshness.py [jsonl_path] [console_log_path]
    .venv/bin/python scripts/calc_quote_freshness.py state/bot.jsonl state/session_2026_08_05.log
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path


def parse_bot_jsonl(path: Path) -> dict:
    """Parse bot.jsonl and extract all TRADE events with their freshness data."""
    fills = []
    missed = []

    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue

            if rec.get("event") != "TRADE":
                continue

            msg = rec.get("msg", "")
            data = rec.get("data", {})
            ts = rec.get("t", "")

            entry = {
                "t": ts,
                "outcome": msg,
                "market_id": data.get("market_id", ""),
                "age_ms": data.get("age_ms", 0),
                "freshness_band": data.get("freshness_band", "unknown"),
            }

            if msg == "filled":
                entry["pnl_locked"] = data.get("pnl_locked", 0)
                entry["combined"] = data.get("combined", 0)
                fills.append(entry)
            elif msg == "missed":
                entry["reason"] = data.get("reason", "unknown")
                entry["would_lock"] = data.get("would_lock", 0)
                missed.append(entry)

    return {"fills": fills, "missed": missed}


def parse_console_log(path: Path) -> dict:
    """Parse a console log (session_*.log) for FIRE latency and TRADE events.

    Console format:
      [HH:MM:SS.mmm] FIRE market=... latency_ms=X.XX band=...
      [HH:MM:SS.mmm] EDGE ... age_ms=... band=...
      [HH:MM:SS.mmm] filled
      [HH:MM:SS.mmm] missed
      [HH:MM:SS.mmm] session metrics: ...
    """
    fills = []
    missed = []
    fire_latencies = []
    summary = None

    fire_re = re.compile(
        r"\[.*?\] FIRE market=(\S+) latency_ms=([\d.]+) band=(\S+)"
    )
    edge_re = re.compile(
        r"\[.*?\] (\S+) YES ([\d.]+) NO ([\d.]+) SUM ([\d.]+) EDGE ([\d.]+) age_ms=([\d.]+) band=(\S+)"
    )
    filled_re = re.compile(r"\[.*?\] (filled|missed)")
    summary_re = re.compile(r"session metrics:.*")

    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue

            # Parse FIRE latency
            m = fire_re.search(line)
            if m:
                fire_latencies.append({
                    "market_id": m.group(1),
                    "latency_ms": float(m.group(2)),
                    "band": m.group(3),
                })
                continue

            # Parse EDGE events for age_ms
            m = edge_re.search(line)
            if m:
                # This is an opportunity detected with an edge
                continue

            # Parse summary line
            if "session metrics:" in line:
                summary = line
                continue

    # The console log has "filled" and "missed" as bare words after timestamps.
    # These correspond to log.trade("filled", ...) and log.trade("missed", ...)
    # but the structured data (age_ms, combined) is only in the JSONL.
    # We can extract FIRE latencies and summary metrics from the console log.
    result = {
        "fills": fills,
        "missed": missed,
        "fire_latencies": fire_latencies,
        "summary": summary,
    }
    return result


def parse_summary_line(line: str) -> dict:
    """Parse the 'session metrics:' summary line."""
    if not line:
        return {}
    patterns = {
        "loops": r"loops=(\d+)",
        "opps": r"opps=(\d+)",
        "sent": r"sent=(\d+)",
        "fills": r"fills=(\d+)",
        "partial": r"partial=(\d+)",
        "resolved": r"resolved=(\d+)",
        "missed": r"missed=(\d+)",
        "opp2order_avg_ms": r"opp2order_avg_ms=([\d.]+)",
        "p50_ms": r"p50_ms=([\d.]+)",
        "ack_avg_ms": r"ack_avg_ms=([\d.]+)",
        "locked_pnl": r"locked_pnl=([\d.]+)",
        "residual_pnl": r"residual_pnl=([\d.]+)",
        "session_pnl": r"session_pnl=([\d.]+)",
        "would_lock": r"would_lock=([\d.]+)",
        "fresh_fills": r"fresh_fills=(\d+)",
        "fresh_attempts": r"fresh_attempts=(\d+)",
        "window_rotations": r"window_rotations=(\d+)",
    }
    metrics = {}
    for key, pat in patterns.items():
        match = re.search(pat, line)
        if match:
            val = match.group(1)
            if key in ("opps", "sent", "fills", "partial", "resolved",
                       "missed", "fresh_fills", "fresh_attempts",
                       "window_rotations", "loops"):
                metrics[key] = int(val)
            else:
                metrics[key] = float(val)
    # Parse bandfills
    band_match = re.search(r"bandfills\[([^\]]+)\]", line)
    if band_match:
        metrics["bandfills"] = {}
        for part in band_match.group(1).split():
            if "=" in part:
                band_name, val = part.split("=")
                if "/" in val:
                    filled_str, attempt_str = val.split("/")
                    metrics["bandfills"][band_name] = {
                        "filled": int(filled_str),
                        "attempts": int(attempt_str),
                    }
    return metrics


def calc_freshness(fills: list, target_ms: int = 500) -> dict:
    """Calculate freshness statistics for filled trades."""
    total = len(fills)
    if total == 0:
        return {
            "total_fills": 0,
            "within_target": 0,
            "pct_within_target": 0.0,
            "PASS": False,
            "avg_age_ms": 0.0,
            "p50_age_ms": 0.0,
            "p95_age_ms": 0.0,
            "target_ms": target_ms,
            "target_pct": 95.0,
            "bands": {},
        }

    ages = sorted(f["age_ms"] for f in fills)
    within = sum(1 for a in ages if a <= target_ms)
    pct = (within / total) * 100.0

    # Band distribution
    bands = Counter(f["freshness_band"] for f in fills)

    # Percentiles use the nearest-rank LOWER convention (index = floor((n-1)*p)),
    # matching numpy.percentile(method="lower"). This gives the lower median for
    # even n and the lower nearest-rank for p95/p99 — the convention the test
    # fixtures encode.
    def _pct(sorted_vals, p: float) -> float:
        n = len(sorted_vals)
        if n == 0:
            return 0.0
        idx = min(n - 1, max(0, int((n - 1) * p)))
        return sorted_vals[idx]

    p50 = _pct(ages, 0.50)
    p95 = _pct(ages, 0.95)

    avg = sum(ages) / total

    # PASS if >= target_pct of fills are within the freshness window
    target_pct = 95.0

    return {
        "total_fills": total,
        "within_target": within,
        "pct_within_target": round(pct, 2),
        "PASS": pct >= target_pct,
        "avg_age_ms": round(avg, 2),
        "p50_age_ms": round(p50, 2),
        "p95_age_ms": round(p95, 2),
        "target_ms": target_ms,
        "target_pct": target_pct,
        "bands": dict(bands),
    }


def calc_fire_latency(latencies: list) -> dict:
    """Calculate latency statistics from FIRE latency_ms values."""
    total = len(latencies)
    if total == 0:
        return {
            "total_fires": 0,
            "avg_ms": 0.0,
            "p50_ms": 0.0,
            "p95_ms": 0.0,
            "p99_ms": 0.0,
            "max_ms": 0.0,
            "within_500ms": 0,
            "pct_within_500ms": 0.0,
        }

    vals = sorted(l["latency_ms"] for l in latencies)
    avg = sum(vals) / total
    # nearest-rank LOWER convention (see calc_freshness._pct).
    p50 = vals[min(total - 1, max(0, int((total - 1) * 0.50)))]
    p95 = vals[min(total - 1, max(0, int((total - 1) * 0.95)))]
    p99 = vals[min(total - 1, max(0, int((total - 1) * 0.99)))]
    within_500 = sum(1 for v in vals if v <= 500)

    return {
        "total_fires": total,
        "avg_ms": round(avg, 2),
        "p50_ms": round(p50, 2),
        "p95_ms": round(p95, 2),
        "p99_ms": round(p99, 2),
        "max_ms": round(vals[-1], 2),
        "within_500ms": within_500,
        "pct_within_500ms": round(within_500 / total * 100, 2),
    }


def main():
    jsonl_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("state/bot.jsonl")
    console_path = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("state/session_2026_08_05.log")

    print("=" * 70)
    print("A2: Quote Freshness Analysis")
    print("=" * 70)
    print(f"JSONL source: {jsonl_path} ({'exists' if jsonl_path.exists() else 'NOT FOUND'})")
    print(f"Console source: {console_path} ({'exists' if console_path.exists() else 'NOT FOUND'})")
    print()

    # Parse JSONL data
    all_fills = []
    all_missed = []
    all_latencies = []
    all_summaries = []
    bandfills = {}

    if jsonl_path.exists():
        data = parse_bot_jsonl(jsonl_path)
        all_fills.extend(data["fills"])
        all_missed.extend(data["missed"])
        print(f"From {jsonl_path.name}: {len(data['fills'])} fills, {len(data['missed'])} missed")

    if console_path.exists():
        console_data = parse_console_log(console_path)
        all_latencies.extend(console_data["fire_latencies"])
        if console_data["summary"]:
            sm = parse_summary_line(console_data["summary"])
            all_summaries.append((str(console_path), sm))
            if "bandfills" in sm:
                bandfills = sm["bandfills"]
            print(f"From {console_path.name}: {len(console_data['fire_latencies'])} FIRE events, summary found")
            if sm.get("opps"):
                print(f"  Summary: opps={sm.get('opps')} fills={sm.get('fills')} "
                      f"missed={sm.get('missed')} avg_ms={sm.get('opp2order_avg_ms')} "
                      f"p50_ms={sm.get('p50_ms')}")

    print()

    # If no JSONL fills, try to reconstruct from summary metrics
    if not all_fills and all_summaries:
        print("NOTE: No structured TRADE events found in JSONL. Using session summary metrics.")
        sm = all_summaries[0][1]
        # From the session metrics, we know the bandfills breakdown
        total_fills = sm.get("fills", 0)
        print(f"  From summary: {total_fills} fills, bandfills={bandfills}")
        # Calculate freshness from bandfills
        if bandfills:
            fresh_total = sum(b["filled"] for b in bandfills.values())
            fresh_fills = bandfills.get("fresh", {}).get("filled", 0)
            pct = (fresh_fills / fresh_total * 100) if fresh_total > 0 else 0
            print(f"  Fills within 500ms (fresh band): {fresh_fills}/{fresh_total} = {pct:.1f}%")
            print(f"  Status: {'PASS' if pct >= 95 else 'FAIL'}")
            print()
            print(f"  Band breakdown:")
            for band, stats in bandfills.items():
                print(f"    {band}: {stats['filled']}/{stats['attempts']} filled")
            print()

    # FIRE latency analysis (from console log)
    if all_latencies:
        lat = calc_fire_latency(all_latencies)
        print(f"--- FIRE Latency (opp->order, from console log) ---")
        print(f"  Total FIRE events:   {lat['total_fires']}")
        print(f"  Average:             {lat['avg_ms']:.2f}ms")
        print(f"  P50:                 {lat['p50_ms']:.2f}ms")
        print(f"  P95:                 {lat['p95_ms']:.2f}ms")
        print(f"  P99:                 {lat['p99_ms']:.2f}ms")
        print(f"  Max:                 {lat['max_ms']:.2f}ms")
        print(f"  Within 500ms:        {lat['within_500ms']}/{lat['total_fires']} ({lat['pct_within_500ms']}%)")
        print()

    # Freshness analysis on fills (from JSONL)
    if all_fills:
        result = calc_freshness(all_fills, target_ms=500)
        print(f"--- Filled Trades Freshness (from JSONL) ---")
        print(f"Total fills:        {result['total_fills']}")
        print(f"Within 500ms:       {result['within_target']}")
        print(f"Percentage:         {result['pct_within_target']}%")
        print(f"Target:             >={result['target_pct']}%")
        print(f"Status:             {'PASS' if result['PASS'] else 'FAIL'}")
        print()
    else:
        # Use summary data
        if all_summaries and bandfills:
            fresh_fills = sum(b["filled"] for b in bandfills.values())
            within = bandfills.get("fresh", {}).get("filled", 0)
            pct = (within / fresh_fills * 100) if fresh_fills > 0 else 0
            print(f"--- Filled Trades Freshness (from summary) ---")
            print(f"Total fills:        {fresh_fills}")
            print(f"Within 500ms:       {within}")
            print(f"Percentage:         {pct:.2f}%")
            print(f"Target:             >=95%")
            print(f"Status:             {'PASS' if pct >= 95 else 'FAIL'}")
            print()

    # Missed breakdown
    if all_missed:
        missed_bands = Counter(m["freshness_band"] for m in all_missed)
        missed_reasons = Counter(m["reason"] for m in all_missed)

        print(f"--- Missed Trades Breakdown ---")
        print(f"Total missed: {len(all_missed)}")
        print()
        print(f"By reason:")
        for reason, count in sorted(missed_reasons.items(), key=lambda x: -x[1]):
            print(f"  {reason}: {count}")
        print()
        print(f"By freshness band:")
        for band, count in sorted(missed_bands.items()):
            print(f"  {band}: {count}")
        print()

    # Summary-based stats
    if all_summaries:
        sm = all_summaries[0][1]
        total_opps = sm.get("opps", 0)
        total_missed = sm.get("missed", 0)
        total_attempts = total_opps + total_missed

        # Freshness gate effectiveness
        staleness_rejections = total_missed
        if total_attempts > 0:
            staleness_hit_rate = staleness_rejections / total_attempts * 100
        else:
            staleness_hit_rate = 0

        print(f"--- Freshness Gate Effectiveness ---")
        print(f"  Opportunities: {total_opps}")
        print(f"  Missed (rejected): {total_missed}")
        print(f"  Bandfills: {bandfills}")

        # Freshness breakdown
        fresh_total = sum(b["filled"] for b in bandfills.values())
        fresh_fills = bandfills.get("fresh", {}).get("filled", 0)
        fresh_missed = bandfills.get("fresh", {}).get("attempts", 0) - fresh_fills
        if fresh_total > 0:
            fill_rate_fresh = fresh_fills / fresh_total * 100
        else:
            fill_rate_fresh = 0

        print(f"  Fresh band (<=500ms): {fresh_total} attempts, {fresh_fills} filled ({fill_rate_fresh:.1f}% fill rate)")
        print()

    print("=" * 70)


if __name__ == "__main__":
    main()
