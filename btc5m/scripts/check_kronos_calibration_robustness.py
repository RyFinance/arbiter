#!/usr/bin/env python3
"""Audit Kronos threshold calibration skew and run a real holdout check.

The production threshold update came from 27 checkpoints in
state/kronos_threshold_calibration.json.  This script makes the follow-up
robustness check reproducible:

* summarize whether those 27 rows skew by regime, date/source, or UTC hour
* fetch a bounded out-of-sample BTC/USD candle set from LSE
* run the existing Kronos-small path on holdout checkpoints only
* compare the frozen old thresholds against the current production thresholds

No synthetic checkpoints are generated.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from collections import Counter
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd

from polymarket_arb.clients.lse_client import LSEClient
from polymarket_arb.kronos_session import ATR_HIGH_THRESHOLD, ATR_LOW_THRESHOLD
from scripts.calibrate_kronos_thresholds import (
    KRONOS_MAX_CONTEXT,
    KRONOS_PRED_LEN,
    OLD_HIGH_THRESHOLD,
    OLD_LOW_THRESHOLD,
    Checkpoint,
    build_candidate_checkpoints,
    classify_with_thresholds,
    load_minute_candles,
    run_kronos_for_checkpoint,
    tune_thresholds,
)

DEFAULT_CALIBRATION_REPORT = PROJECT_ROOT / "state" / "kronos_threshold_calibration.json"
DEFAULT_OUTPUT_PATH = PROJECT_ROOT / "state" / "kronos_threshold_robustness.json"
DEFAULT_DOC_PATH = PROJECT_ROOT / "docs" / "kronos_threshold_robustness.md"
DEFAULT_LSE_START = "2026-07-19"
DEFAULT_LSE_END = "2026-07-22"
DEFAULT_LSE_RANGES = (
    # Each range includes eight hours of prior candles so 00:00 and 04:00 UTC
    # holdout checkpoints have a complete 512-minute Kronos context.
    "2026-06-30:2026-07-02",
    "2026-07-07:2026-07-09",
    "2026-07-14:2026-07-16",
    "2026-07-18:2026-07-20",
)
TARGET_HOLDOUT_HOURS_UTC = (0, 4, 16, 20)


def _counts(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    return dict(sorted(Counter(str(row.get(key)) for row in rows).items()))


def _dates(rows: list[dict[str, Any]]) -> dict[str, int]:
    return dict(sorted(Counter(str(row["timestamp"])[:10] for row in rows).items()))


def _top_count_fraction(counts: dict[str, int]) -> float:
    total = sum(counts.values())
    return max(counts.values()) / total if total else 0.0


def analyze_calibration_skew(report: dict[str, Any]) -> dict[str, Any]:
    rows = list(report.get("checkpoints") or [])
    source_counts = _counts(rows, "source_file")
    date_counts = _dates(rows)
    realized_counts = _counts(rows, "realized_regime")
    bucket_counts = _counts(rows, "volatility_bucket")
    hour_counts = _counts(rows, "hour_utc")
    four_hour_counts = dict(
        sorted(
            Counter(f"{int(row['hour_utc']) // 4 * 4:02d}-{int(row['hour_utc']) // 4 * 4 + 3:02d}" for row in rows).items()
        )
    )

    return {
        "checkpoint_count": len(rows),
        "time_span_start": min((row["timestamp"] for row in rows), default=None),
        "time_span_end": max((row["timestamp"] for row in rows), default=None),
        "realized_regime_counts": realized_counts,
        "volatility_sample_bucket_counts": bucket_counts,
        "source_file_counts": source_counts,
        "date_counts": date_counts,
        "hour_utc_counts": hour_counts,
        "four_hour_bucket_counts": four_hour_counts,
        "largest_source_fraction": _top_count_fraction(source_counts),
        "largest_date_fraction": _top_count_fraction(date_counts),
        "largest_realized_regime_fraction": _top_count_fraction(realized_counts),
        "largest_hour_fraction": _top_count_fraction(hour_counts),
        "skew_flags": {
            "realized_regime_dominated": _top_count_fraction(realized_counts) >= 0.70,
            "single_source_dominated": _top_count_fraction(source_counts) >= 0.60,
            "single_date_dominated": _top_count_fraction(date_counts) >= 0.50,
            "single_hour_dominated": _top_count_fraction(hour_counts) >= 0.35,
        },
    }


def normalize_lse_candles(items: list[dict[str, Any]], source_name: str) -> pd.DataFrame:
    rows = []
    for item in items:
        ts = item.get("ts") or item.get("timestamp") or item.get("time")
        if not ts:
            continue
        rows.append(
            {
                "timestamp": pd.Timestamp(ts, tz="UTC") if pd.Timestamp(ts).tzinfo is None else pd.Timestamp(ts).tz_convert("UTC"),
                "open": float(item["open"]),
                "high": float(item["high"]),
                "low": float(item["low"]),
                "close": float(item["close"]),
                "volume": float(item.get("volume") or 0.0),
                "source_file": source_name,
            }
        )
    return pd.DataFrame(rows).drop_duplicates(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)


async def fetch_lse_holdout_candles(start: str, end: str, limit: int) -> list[dict[str, Any]]:
    client = LSEClient()
    try:
        return await client.fetch_candles(
            symbol="BTC/USD",
            timeframe="1m",
            start=start,
            end=end,
            limit=limit,
        )
    finally:
        await client.aclose()


async def fetch_lse_holdout_ranges(ranges: list[tuple[str, str]], limit: int) -> pd.DataFrame:
    frames = []
    for start, end in ranges:
        raw = await fetch_lse_holdout_candles(start, end, limit)
        frame = normalize_lse_candles(raw, f"lse:{start}:{end}")
        if not frame.empty:
            frames.append(frame)
    if not frames:
        return pd.DataFrame()
    return (
        pd.concat(frames, ignore_index=True)
        .drop_duplicates(subset=["timestamp"])
        .sort_values("timestamp")
        .reset_index(drop=True)
    )


def select_holdout_checkpoints(
    candidates: list[dict[str, Any]],
    *,
    count: int,
    excluded_dates: set[str],
) -> list[dict[str, Any]]:
    eligible = [
        row
        for row in candidates
        if row["timestamp"].date().isoformat() not in excluded_dates
    ]
    if len(eligible) < count:
        return []

    ranked = sorted(eligible, key=lambda r: r["realized_atr_daily_equiv"])
    n = len(ranked)
    low_cut = ranked[max(0, math.floor(n / 3) - 1)]["realized_atr_daily_equiv"]
    high_cut = ranked[min(n - 1, math.floor(2 * n / 3))]["realized_atr_daily_equiv"]
    for row in eligible:
        atr = row["realized_atr_daily_equiv"]
        if atr <= low_cut:
            row["volatility_bucket"] = "LOW_SAMPLE"
        elif atr >= high_cut:
            row["volatility_bucket"] = "HIGH_SAMPLE"
        else:
            row["volatility_bucket"] = "MED_SAMPLE"

    selected: list[dict[str, Any]] = []
    by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in eligible:
        by_date[row["timestamp"].date().isoformat()].append(row)

    for date_rows in by_date.values():
        date_rows.sort(key=lambda r: (r["timestamp"].hour % 6, r["timestamp"]))

    dates = sorted(by_date)
    seen_date_hour: set[tuple[str, int]] = set()

    # The old data fetches covered only their first 1,000 minutes. Its
    # round-robin selection therefore put every checkpoint at 08, 12, or 13
    # UTC. Fill from target hours first, rotating through the least-used
    # dates; use the generic date-balanced selection only if necessary.
    while len(selected) < count:
        progressed = False
        for target_hour in TARGET_HOLDOUT_HOURS_UTC:
            available_dates = sorted(
                (
                    date for date in dates
                    if any(
                        row["timestamp"].hour == target_hour
                        and (date, target_hour) not in seen_date_hour
                        for row in by_date[date]
                    )
                ),
                key=lambda candidate_date: (
                    sum(
                        row["timestamp"].date().isoformat() == candidate_date
                        for row in selected
                    ),
                    candidate_date,
                ),
            )
            if not available_dates:
                continue
            date = available_dates[0]
            row = next(
                row for row in by_date[date]
                if row["timestamp"].hour == target_hour
                and (date, target_hour) not in seen_date_hour
            )
            selected.append(row)
            seen_date_hour.add((date, target_hour))
            progressed = True
            if len(selected) >= count:
                return sorted(selected, key=lambda r: r["timestamp"])
        if not progressed:
            break

    while len(selected) < count and dates:
        progressed = False
        for date in sorted(
            dates,
            key=lambda candidate_date: (
                sum(
                    row["timestamp"].date().isoformat() == candidate_date
                    for row in selected
                ),
                candidate_date,
            ),
        ):
            for row in by_date[date]:
                date_hour = (date, row["timestamp"].hour)
                if row in selected or date_hour in seen_date_hour:
                    continue
                selected.append(row)
                seen_date_hour.add(date_hour)
                progressed = True
                break
            if len(selected) >= count:
                return sorted(selected, key=lambda r: r["timestamp"])
        if not progressed:
            break

    for row in sorted(eligible, key=lambda r: r["timestamp"]):
        if row not in selected:
            selected.append(row)
        if len(selected) >= count:
            break
    return sorted(selected, key=lambda r: r["timestamp"])


def evaluate_thresholds(rows: list[dict[str, Any]], low: float, high: float) -> dict[str, Any]:
    valid = [row for row in rows if row.get("kronos_atr_daily_equiv") is not None]
    matches = 0
    for row in valid:
        regime = classify_with_thresholds(float(row["kronos_atr_daily_equiv"]), low, high)
        if regime == row["realized_regime"]:
            matches += 1
    return {
        "low_threshold": low,
        "high_threshold": high,
        "matches": matches,
        "total": len(valid),
        "match_rate": matches / len(valid) if valid else 0.0,
    }


def _write_doc(report: dict[str, Any], path: Path) -> None:
    skew = report["calibration_skew"]
    holdout = report["holdout"]
    lines = [
        "# Kronos Threshold Robustness",
        "",
        "## Calibration Skew",
        f"- Checkpoints: {skew['checkpoint_count']}",
        f"- Time span: {skew['time_span_start']} to {skew['time_span_end']}",
        f"- Realized regimes: `{skew['realized_regime_counts']}`",
        f"- Volatility sample buckets: `{skew['volatility_sample_bucket_counts']}`",
        f"- Source files: `{skew['source_file_counts']}`",
        f"- UTC hours: `{skew['hour_utc_counts']}`",
        f"- Skew flags: `{skew['skew_flags']}`",
        "",
        "## Holdout",
        f"- Source: {holdout['source']}",
        f"- Range: {holdout.get('range_start')} to {holdout.get('range_end')}",
        f"- Candles: {holdout.get('candles')}",
        f"- Checkpoints: {holdout.get('checkpoints')}",
        f"- Status: {holdout['status']}",
    ]
    if holdout.get("old_thresholds"):
        lines.extend(
            [
                f"- Selected checkpoint dates: `{holdout.get('checkpoint_date_counts')}`",
                f"- Selected checkpoint sources: `{holdout.get('checkpoint_source_counts')}`",
                f"- Selected checkpoint UTC hours: `{holdout.get('hour_utc_counts')}`",
                f"- Required target UTC hours: `{holdout.get('target_hours_utc')}`; present: `{holdout.get('target_hours_present')}`",
                f"- Realized regimes: `{holdout.get('realized_regime_counts')}`",
                f"- Old match rate: {holdout['old_thresholds']['matches']}/{holdout['old_thresholds']['total']} = {holdout['old_thresholds']['match_rate']:.2%}",
                f"- New match rate: {holdout['new_thresholds']['matches']}/{holdout['new_thresholds']['total']} = {holdout['new_thresholds']['match_rate']:.2%}",
            ]
        )
    if holdout.get("retuning"):
        lines.append(f"- Retuning: `{holdout['retuning']}`")
    if holdout.get("reason"):
        lines.append(f"- Reason: {holdout['reason']}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")


async def build_report(args: argparse.Namespace) -> dict[str, Any]:
    calibration = json.loads(Path(args.calibration_report).read_text())
    skew = analyze_calibration_skew(calibration)
    excluded_dates = {ts[:10] for ts in _dates(calibration.get("checkpoints") or {})}
    ranges = []
    if args.lse_ranges:
        for item in args.lse_ranges.split(","):
            if not item.strip():
                continue
            start, end = item.strip().split(":", 1)
            ranges.append((start, end))
    else:
        ranges = [(args.lse_start, args.lse_end)]

    holdout: dict[str, Any] = {
        "source": "LSE BTC/USD 1m candles",
        "requested_ranges": ranges,
        "status": "unavailable",
    }
    try:
        candles = await fetch_lse_holdout_ranges(ranges, args.limit)
        holdout["candles"] = len(candles)
        holdout["range_start"] = candles.iloc[0].timestamp.isoformat() if not candles.empty else None
        holdout["range_end"] = candles.iloc[-1].timestamp.isoformat() if not candles.empty else None
        if len(candles) < KRONOS_MAX_CONTEXT + KRONOS_PRED_LEN:
            holdout["reason"] = "Not enough contiguous 1m candles for Kronos context plus realized window."
        else:
            candidates = build_candidate_checkpoints(candles)
            selected = select_holdout_checkpoints(
                candidates,
                count=args.holdout_checkpoints,
                excluded_dates=excluded_dates,
            )
            if len(selected) < args.holdout_checkpoints:
                holdout["reason"] = (
                    f"Only {len(selected)} different-date holdout checkpoints available; "
                    f"needed {args.holdout_checkpoints}."
                )
            else:
                rows = [asdict(run_kronos_for_checkpoint(candles, row)) for row in selected]
                for row in rows:
                    atr = row.get("kronos_atr_daily_equiv")
                    if atr is not None:
                        row["kronos_regime_old"] = classify_with_thresholds(
                            float(atr), OLD_LOW_THRESHOLD, OLD_HIGH_THRESHOLD
                        )
                        row["kronos_regime_new"] = classify_with_thresholds(
                            float(atr), ATR_LOW_THRESHOLD, ATR_HIGH_THRESHOLD
                        )
                        row["old_match"] = row["kronos_regime_old"] == row["realized_regime"]
                        row["new_match"] = row["kronos_regime_new"] == row["realized_regime"]
                valid = [row for row in rows if row.get("kronos_atr_daily_equiv") is not None]
                holdout.update(
                    {
                        "status": "ok" if valid else "no_valid_kronos_predictions",
                        "checkpoints": len(rows),
                        "valid_kronos_predictions": len(valid),
                        "realized_regime_counts": _counts(rows, "realized_regime"),
                        "checkpoint_date_counts": _dates(rows),
                        "checkpoint_source_counts": _counts(rows, "source_file"),
                        "hour_utc_counts": _counts(rows, "hour_utc"),
                        "target_hours_utc": list(TARGET_HOLDOUT_HOURS_UTC),
                        "target_hours_present": sorted(
                            set(TARGET_HOLDOUT_HOURS_UTC)
                            & {int(row["hour_utc"]) for row in rows}
                        ),
                        "old_thresholds": evaluate_thresholds(
                            rows, OLD_LOW_THRESHOLD, OLD_HIGH_THRESHOLD
                        ),
                        "new_thresholds": evaluate_thresholds(
                            rows, ATR_LOW_THRESHOLD, ATR_HIGH_THRESHOLD
                        ),
                        "checkpoints_detail": rows,
                    }
                )
                if holdout["new_thresholds"]["match_rate"] < 0.80:
                    tuned = tune_thresholds(list(calibration.get("checkpoints") or []))
                    holdout["retuning"] = {
                        "status": "evaluated_on_untouched_holdout",
                        "trigger": "production_threshold_match_rate_below_80_percent",
                        "training_source": str(args.calibration_report),
                        "training_checkpoints": len(calibration.get("checkpoints") or []),
                        "candidate_thresholds": tuned,
                        "untouched_holdout_validation": evaluate_thresholds(
                            rows,
                            float(tuned["low_threshold"]),
                            float(tuned["high_threshold"]),
                        ),
                        "changes_production_thresholds": (
                            float(tuned["low_threshold"]) != ATR_LOW_THRESHOLD
                            or float(tuned["high_threshold"]) != ATR_HIGH_THRESHOLD
                        ),
                    }
                else:
                    holdout["retuning"] = {
                        "status": "not_required",
                        "trigger": "production_threshold_match_rate_at_least_80_percent",
                    }
    except Exception as exc:  # pragma: no cover - runtime reporting
        holdout["reason"] = f"{exc.__class__.__name__}: {exc}"

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "synthetic_data_used": False,
        "production_thresholds": {
            "low_threshold": ATR_LOW_THRESHOLD,
            "high_threshold": ATR_HIGH_THRESHOLD,
        },
        "old_baseline_thresholds": {
            "low_threshold": OLD_LOW_THRESHOLD,
            "high_threshold": OLD_HIGH_THRESHOLD,
        },
        "calibration_skew": skew,
        "holdout": holdout,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--calibration-report", default=str(DEFAULT_CALIBRATION_REPORT))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_PATH))
    parser.add_argument("--doc-output", default=str(DEFAULT_DOC_PATH))
    parser.add_argument("--lse-start", default=DEFAULT_LSE_START)
    parser.add_argument("--lse-end", default=DEFAULT_LSE_END)
    parser.add_argument("--lse-ranges", default=",".join(DEFAULT_LSE_RANGES))
    parser.add_argument("--limit", type=int, default=4000)
    parser.add_argument("--holdout-checkpoints", type=int, default=12)
    args = parser.parse_args()

    report = asyncio.run(build_report(args))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n")
    _write_doc(report, Path(args.doc_output))

    holdout = report["holdout"]
    printable = {
        "calibration_skew": report["calibration_skew"],
        "holdout": {
            key: holdout.get(key)
            for key in (
                "status",
                "source",
                "range_start",
                "range_end",
                "candles",
                "checkpoints",
                "valid_kronos_predictions",
                "realized_regime_counts",
                "checkpoint_date_counts",
                "checkpoint_source_counts",
                "hour_utc_counts",
                "target_hours_utc",
                "target_hours_present",
                "old_thresholds",
                "new_thresholds",
                "retuning",
                "reason",
            )
        },
    }
    print(json.dumps(printable, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
