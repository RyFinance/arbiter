"""Permanent per-stage latency telemetry report.

Reads a session's bot.jsonl (or any archive copy) and outputs avg/p50/p95 per
stage, per freshness band, over any date range.  This is the permanent
operational tool for latency post-hoc analysis — NOT a one-off diagnostic.

The stage timings are persisted in every TRADE event's ``data.stage_timings``
dict (milliseconds) by execution.py.  This script parses them and produces
percentile summaries grouped by stage and freshness band.

Usage:
    python scripts/latency_report.py                                  # state/bot.jsonl
    python scripts/latency_report.py state/paper_session_archives/execution_stress_20260805T211659Z/bot.jsonl
    python scripts/latency_report.py --date 2026-08-05                              # date filter on archive logs
    python scripts/latency_report.py --date 2026-08-05 --band fresh                 # restrict to one freshness band
    python scripts/latency_report.py --stage reserve                                # one stage only
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

# Stages recorded in stage_timings (milliseconds each).
KNOWN_STAGES = [
    "reserve",       # atomic precheck + reservation (risk lock)
    "dispatch",      # from reserve done to first leg submitted
    "dual_submit",   # concurrent dual gather (both submit coroutines)
    "paper_leg_max", # max of the two paper-leg submit durations
    "post_submit",   # commit + pending stash after submission
    "execute_total", # reserve_done..post_submit (full fire latency)
]

# Also report opp_to_order_ms and ack_ms if present.
KNOWN_META = ["opp_to_order_ms", "ack_ms"]


def _parse_log_line(line: str) -> dict | None:
    """Parse one JSONL line, returning the record dict or None."""
    line = line.strip()
    if not line:
        return None
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return None


def _extract_stage_timings(data: dict) -> dict:
    """Extract stage_timings dict from a TRADE event's data, defaulting to empty."""
    stages = data.get("stage_timings") or {}
    # Only keep known stage keys; ignore anything unexpected.
    return {k: v for k, v in stages.items() if k in KNOWN_STAGES}


def _extract_meta(data: dict) -> dict:
    """Extract opp_to_order_ms and ack_ms from a TRADE event's data."""
    result = {}
    for k in KNOWN_META:
        v = data.get(k)
        if v is not None:
            result[k] = float(v)
    return result


def _percentile(sorted_vals: list[float], p: float) -> float:
    """Nearest-rank percentile (lower convention, matching metrics.py)."""
    n = len(sorted_vals)
    if n == 0:
        return 0.0
    idx = min(n - 1, max(0, int((n - 1) * p)))
    return sorted_vals[idx]


def _stats(vals: list[float]) -> dict:
    """Compute avg/p50/p95/max for a list of millisecond values."""
    if not vals:
        return {"n": 0, "avg_ms": 0.0, "p50_ms": 0.0, "p95_ms": 0.0, "p99_ms": 0.0, "max_ms": 0.0}
    vals_sorted = sorted(vals)
    n = len(vals_sorted)
    return {
        "n": n,
        "avg_ms": round(sum(vals_sorted) / n, 3),
        "p50_ms": round(_percentile(vals_sorted, 0.50), 3),
        "p95_ms": round(_percentile(vals_sorted, 0.95), 3),
        "p99_ms": round(_percentile(vals_sorted, 0.99), 3),
        "max_ms": round(vals_sorted[-1], 3),
    }


def parse_bot_jsonl(path: Path) -> dict:
    """Parse a bot.jsonl file and extract all TRADE events with stage_timings.

    Returns a dict with:
      - trade_events: list of {t, msg, data, freshness_band, stage_timings, meta}
    """
    events = []
    with open(path, "r") as f:
        for line in f:
            rec = _parse_log_line(line)
            if rec is None:
                continue
            if rec.get("event") != "TRADE":
                continue
            data = rec.get("data", {})
            band = data.get("freshness_band", "unknown")
            stages = _extract_stage_timings(data)
            meta = _extract_meta(data)
            events.append({
                "t": rec.get("t", ""),
                "msg": rec.get("msg", ""),
                "band": band,
                "stages": stages,
                "meta": meta,
                "data": data,
            })
    return {"trade_events": events}


def _archive_date(path: Path) -> str | None:
    """Extract YYYY-MM-DD from an archive directory path.

    Archive dirs are named like execution_stress_20260805T211659Z.
    """
    # Walk up to find a directory component matching the pattern.
    for part in path.parts:
        if part.startswith("execution_stress_") or part.startswith("paper_session_"):
            # execution_stress_20260805T211659Z -> 20260805
            suffix = part.split("_", 2)[-1] if "stress" in part else part.split("_", 2)[-1]
            # Take first 8 chars (YYYYMMDD)
            date_str = suffix[:8]
            if len(date_str) == 8 and date_str.isdigit():
                y, m, d = date_str[:4], date_str[4:6], date_str[6:8]
                return f"{y}-{m}-{d}"
    return None


def collect_trades(jsonl_path: Path, date_filter: str | None) -> list[dict]:
    """Parse a bot.jsonl (or walk an archive dir) and return all TRADE events.

    If jsonl_path is a directory, walk it for bot.jsonl files.
    If date_filter is set, only include archives whose name encodes that date.
    """
    events = []

    if jsonl_path.is_dir():
        # Walk directory tree for bot.jsonl files
        for root, _dirs, files in os.walk(jsonl_path):
            if "bot.jsonl" in files:
                bot_path = Path(root) / "bot.jsonl"
                arch_date = _archive_date(bot_path)
                if date_filter and arch_date != date_filter:
                    continue
                parsed = parse_bot_jsonl(bot_path)
                events.extend(parsed["trade_events"])
    else:
        parsed = parse_bot_jsonl(jsonl_path)
        events.extend(parsed["trade_events"])

    return events


def build_report(events: list[dict], stage_filter: str | None, band_filter: str | None) -> dict:
    """Build the latency report from parsed TRADE events.

    Returns per-stage and per-(stage,band) percentile stats, plus meta fields.
    """
    # Collect per-stage values (all bands).
    all_stage_vals: dict[str, list[float]] = defaultdict(list)
    # Collect per-stage-per-band values.
    staged_band_vals: dict[tuple[str, str], list[float]] = defaultdict(list)
    # Collect meta values.
    all_meta_vals: dict[str, list[float]] = defaultdict(list)
    meta_band_vals: dict[tuple[str, str], list[float]] = defaultdict(list)

    # Track which stages actually appear across all events.
    seen_stages: set[str] = set()
    seen_meta: set[str] = set()

    for ev in events:
        band = ev["band"]
        stages = ev["stages"]
        meta = ev["meta"]

        # Track which stages have any data at all.
        for stage in stages:
            seen_stages.add(stage)
        for k in meta:
            seen_meta.add(k)

        if not stages and not meta:
            continue  # event has no stage_timings (e.g. pre-reserve missed)

        # Stage data
        for stage, val in stages.items():
            all_stage_vals[stage].append(val)
            staged_band_vals[(stage, band)].append(val)

        # Meta data
        for k, v in meta.items():
            all_meta_vals[k].append(v)
            meta_band_vals[(k, band)].append(v)

    # Build per-stage report (all bands combined).
    stage_report: dict[str, dict] = {}
    for stage in list(seen_stages):
        vals = all_stage_vals.get(stage, [])
        if stage_filter and stage != stage_filter:
            continue
        if vals:
            stage_report[stage] = _stats(vals)

    # Meta fields
    meta_report: dict[str, dict] = {}
    for k in list(seen_meta):
        vals = all_meta_vals.get(k, [])
        if vals:
            meta_report[k] = _stats(vals)

    # Per-band breakdown (only for stages with data).
    band_breakdowns: dict[str, dict[str, dict]] = {}
    bands_seen = set()
    for (stage, band), vals in staged_band_vals.items():
        if stage_filter and stage != stage_filter:
            continue
        bands_seen.add(band)

    for band in sorted(bands_seen):
        if band_filter and band != band_filter:
            continue
        band_data = {}
        for stage in seen_stages:
            vals = staged_band_vals.get((stage, band), [])
            if vals:
                band_data[stage] = _stats(vals)
        # Also include meta per-band.
        for k in seen_meta:
            vals = meta_band_vals.get((k, band), [])
            if vals:
                band_data[k] = _stats(vals)
        if band_data:
            band_breakdowns[band] = band_data

    total_trade_events = len(events)
    events_with_stages = sum(1 for ev in events if ev["stages"] or ev["meta"])

    return {
        "total_trade_events": total_trade_events,
        "events_with_stage_timings": events_with_stages,
        "events_without_stage_timings": total_trade_events - events_with_stages,
        "stages": stage_report,
        "meta": meta_report,
        "by_freshness_band": band_breakdowns,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Per-stage latency report from bot.jsonl TRADE events."
    )
    parser.add_argument(
        "path", nargs="?", default="state/bot.jsonl",
        help="Path to bot.jsonl or an archive directory (default: state/bot.jsonl)",
    )
    parser.add_argument("--date", default=None,
                        help="Filter to archives dated YYYY-MM-DD")
    parser.add_argument("--stage", default=None,
                        help="Limit to one stage (reserve, dispatch, dual_submit, etc.)")
    parser.add_argument("--band", default=None,
                        help="Limit to one freshness band (fresh, 0.5-2s, 2-10s, stale)")
    parser.add_argument("--output", "-o", default=None,
                        help="Write JSON report to this path (default: stdout)")
    args = parser.parse_args(argv)

    jsonl_path = Path(args.path)

    if not jsonl_path.exists():
        print(f"ERROR: path not found: {jsonl_path}", file=sys.stderr)
        return 1

    events = collect_trades(jsonl_path, args.date)
    if not events:
        print(f"No TRADE events found in {jsonl_path}", file=sys.stderr)
        return 1

    report = build_report(events, args.stage, args.band)

    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, indent=2, default=str))
        print(f"Report written to {out_path}")
    else:
        print(json.dumps(report, indent=2, default=str))

    # Human-readable summary to stderr.
    print(f"\n{'='*60}", file=sys.stderr)
    print(f"Latency Report: {jsonl_path}", file=sys.stderr)
    if args.date:
        print(f"  Date filter: {args.date}", file=sys.stderr)
    if args.stage:
        print(f"  Stage filter: {args.stage}", file=sys.stderr)
    if args.band:
        print(f"  Band filter: {args.band}", file=sys.stderr)
    print(f"  Total TRADE events: {report['total_trade_events']}", file=sys.stderr)
    print(f"  Events with stage_timings: {report['events_with_stage_timings']}", file=sys.stderr)
    print(f"  Events without stage_timings (pre-reserve misses): {report['events_without_stage_timings']}", file=sys.stderr)
    print(f"{'='*60}", file=sys.stderr)

    for stage, stats in report["stages"].items():
        print(f"\n  {stage:>16s}:", file=sys.stderr)
        print(f"    n={stats['n']:4d}  avg={stats['avg_ms']:.3f}ms  "
              f"p50={stats['p50_ms']:.3f}ms  p95={stats['p95_ms']:.3f}ms  "
              f"p99={stats['p99_ms']:.3f}ms  max={stats['max_ms']:.3f}ms",
              file=sys.stderr)

    for k, v in report["meta"].items():
        print(f"\n  {k:>16s}:", file=sys.stderr)
        print(f"    n={v['n']:4d}  avg={v['avg_ms']:.3f}ms  "
              f"p50={v['p50_ms']:.3f}ms  p95={v['p95_ms']:.3f}ms", file=sys.stderr)

    if report["by_freshness_band"]:
        print(f"\n  By freshness band:", file=sys.stderr)
        for band, stages in report["by_freshness_band"].items():
            print(f"    {band}:", file=sys.stderr)
            for stage, stats in stages.items():
                if stage in KNOWN_STAGES:
                    print(f"      {stage:>16s}: avg={stats['avg_ms']:.3f}ms "
                          f"p50={stats['p50_ms']:.3f}ms p95={stats['p95_ms']:.3f}ms "
                          f"n={stats['n']}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
