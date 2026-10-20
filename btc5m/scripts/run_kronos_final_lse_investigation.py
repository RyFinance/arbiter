#!/usr/bin/env python3
"""Run the single capped final Kronos calibration investigation.

This runner deliberately has one fixed calibration/holdout split.  It uses
only real LSE BTC/USD one-minute candles, derives a threshold pair once from
the calibration split, and evaluates that pair once on a date-disjoint
holdout.  It does not edit production thresholds or initiate another tuning
cycle.
"""
from __future__ import annotations

import asyncio
import json
import sys
from collections import Counter
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from polymarket_arb.clients.lse_client import LSEClient
from polymarket_arb.kronos_session import ATR_HIGH_THRESHOLD, ATR_LOW_THRESHOLD
from scripts.calibrate_kronos_thresholds import (
    KRONOS_MAX_CONTEXT,
    KRONOS_PRED_LEN,
    OLD_HIGH_THRESHOLD,
    OLD_LOW_THRESHOLD,
    classify_with_thresholds,
    realized_atr_daily_equiv,
    run_kronos_for_checkpoint,
    tune_thresholds,
)

OUTPUT_PATH = PROJECT_ROOT / "state" / "kronos_final_lse_investigation.json"

# The original 27 checkpoints were drawn from these tick-data dates.
ORIGINAL_CALIBRATION_DATES = frozenset({"2026-07-26", "2026-07-27", "2026-07-29", "2026-07-30"})

# The previous 12-row diversified LSE holdout.  These dates must not enter the
# final calibration or final holdout.
PRIOR_HOLDOUT_DATES = frozenset(
    {
        "2026-06-30",
        "2026-07-01",
        "2026-07-07",
        "2026-07-08",
        "2026-07-14",
        "2026-07-15",
        "2026-07-18",
        "2026-07-19",
    }
)


def _specs(dates: tuple[str, ...], hour_pairs: tuple[tuple[int, int], ...]) -> tuple[tuple[str, int], ...]:
    return tuple((day, hour) for day, hours in zip(dates, hour_pairs) for hour in hours)


# Forty fixed, real-LSE checkpoints across five months and all six four-hour
# UTC buckets.  They are selected before any final-run labels are observed.
CALIBRATION_CHECKPOINTS = _specs(
    (
        "2026-01-05", "2026-01-12", "2026-01-19", "2026-01-26",
        "2026-02-02", "2026-02-09", "2026-02-16", "2026-02-23",
        "2026-03-02", "2026-03-09", "2026-03-16", "2026-03-23",
        "2026-04-06", "2026-04-13", "2026-04-20", "2026-04-27",
        "2026-05-04", "2026-05-11", "2026-05-18", "2026-05-25",
    ),
    ((0, 12), (4, 16), (8, 20), (0, 12), (4, 16), (8, 20), (0, 12), (4, 16),
     (8, 20), (0, 12), (4, 16), (8, 20), (0, 12), (4, 16), (8, 20), (0, 12),
     (4, 16), (8, 20), (0, 12), (4, 16)),
)

# This is intentionally untouched while calibration runs.  The dates are
# disjoint from the original 27 rows, the prior eight-date holdout, and the
# final calibration set.
FINAL_HOLDOUT_CHECKPOINTS = _specs(
    (
        "2026-06-04", "2026-06-07", "2026-06-11", "2026-06-14",
        "2026-06-18", "2026-06-21", "2026-06-25", "2026-06-28",
    ),
    ((0, 12), (4, 16), (8, 20), (0, 12), (4, 16), (8, 20), (0, 12), (4, 16)),
)


def checkpoint_dates(specifications: Iterable[tuple[str, int]]) -> set[str]:
    return {day for day, _hour in specifications}


def validate_split() -> None:
    calibration_dates = checkpoint_dates(CALIBRATION_CHECKPOINTS)
    holdout_dates = checkpoint_dates(FINAL_HOLDOUT_CHECKPOINTS)
    if len(CALIBRATION_CHECKPOINTS) <= 27:
        raise ValueError("Final calibration must be larger than the original 27 checkpoints.")
    if len(FINAL_HOLDOUT_CHECKPOINTS) < 12:
        raise ValueError("Final holdout must contain at least 12 checkpoints.")
    if calibration_dates & holdout_dates:
        raise ValueError("Final calibration and holdout dates overlap.")
    forbidden = ORIGINAL_CALIBRATION_DATES | PRIOR_HOLDOUT_DATES
    if (calibration_dates | holdout_dates) & forbidden:
        raise ValueError("Final investigation reuses an excluded date.")


def normalize_lse_candles(items: list[dict[str, Any]], source_name: str) -> pd.DataFrame:
    rows = []
    for item in items:
        timestamp = item.get("ts") or item.get("timestamp") or item.get("time")
        if not timestamp:
            continue
        parsed = pd.Timestamp(timestamp)
        if parsed.tzinfo is None:
            parsed = parsed.tz_localize("UTC")
        else:
            parsed = parsed.tz_convert("UTC")
        rows.append(
            {
                "timestamp": parsed,
                "open": float(item["open"]),
                "high": float(item["high"]),
                "low": float(item["low"]),
                "close": float(item["close"]),
                "volume": float(item.get("volume") or 0.0),
                "source_file": source_name,
            }
        )
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).drop_duplicates(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)


async def fetch_day_frame(client: LSEClient, day: str) -> pd.DataFrame:
    target_day = date.fromisoformat(day)
    start = (target_day - timedelta(days=1)).isoformat()
    end = (target_day + timedelta(days=1)).isoformat()
    raw = await client.fetch_candles("BTC/USD", "1m", start=start, end=end, limit=5000)
    return normalize_lse_candles(raw, f"lse:{start}:{end}")


def _checkpoint_candidate(candles: pd.DataFrame, day: str, hour: int) -> dict[str, Any]:
    target = pd.Timestamp(f"{day}T{hour:02d}:00:00Z")
    matches = candles.index[candles["timestamp"] == target]
    if len(matches) != 1:
        raise ValueError(f"LSE did not return exactly one candle at {target.isoformat()}")

    idx = int(matches[0])
    context = candles.iloc[idx - KRONOS_MAX_CONTEXT : idx]
    realized = candles.iloc[idx : idx + KRONOS_PRED_LEN]
    if len(context) != KRONOS_MAX_CONTEXT or len(realized) != KRONOS_PRED_LEN:
        raise ValueError(f"Insufficient context or realized candles at {target.isoformat()}")
    context_span = (context.iloc[-1].timestamp - context.iloc[0].timestamp).total_seconds() / 60
    realized_span = (realized.iloc[-1].timestamp - realized.iloc[0].timestamp).total_seconds() / 60
    if context_span > KRONOS_MAX_CONTEXT + 5 or realized_span > KRONOS_PRED_LEN + 5:
        raise ValueError(f"Non-contiguous LSE candles at {target.isoformat()}")

    atr = realized_atr_daily_equiv(realized)
    return {
        "idx": idx,
        "timestamp": target,
        "hour_utc": hour,
        "source_file": str(candles.iloc[idx].source_file),
        "volatility_bucket": "PREDECLARED_FIXED_CHECKPOINT",
        "realized_atr_daily_equiv": atr,
        "realized_regime": classify_with_thresholds(atr, OLD_LOW_THRESHOLD, OLD_HIGH_THRESHOLD),
    }


async def evaluate_checkpoints(
    client: LSEClient,
    specifications: tuple[tuple[str, int], ...],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    frames: dict[str, pd.DataFrame] = {}
    for day in sorted(checkpoint_dates(specifications)):
        frames[day] = await fetch_day_frame(client, day)

    rows = []
    candle_counts = {}
    for day, hour in specifications:
        candles = frames[day]
        candle_counts[day] = len(candles)
        candidate = _checkpoint_candidate(candles, day, hour)
        rows.append(asdict(run_kronos_for_checkpoint(candles, candidate)))
    return rows, candle_counts


def evaluate_thresholds(rows: list[dict[str, Any]], low: float, high: float) -> dict[str, Any]:
    valid = [row for row in rows if row.get("kronos_atr_daily_equiv") is not None]
    matches = sum(
        classify_with_thresholds(float(row["kronos_atr_daily_equiv"]), low, high) == row["realized_regime"]
        for row in valid
    )
    return {
        "low_threshold": low,
        "high_threshold": high,
        "matches": matches,
        "total": len(valid),
        "match_rate": matches / len(valid) if valid else 0.0,
    }


def counts(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    return dict(sorted(Counter(str(row.get(key)) for row in rows).items()))


def split_summary(rows: list[dict[str, Any]], candles_by_date: dict[str, int]) -> dict[str, Any]:
    return {
        "checkpoints_requested": len(rows),
        "valid_kronos_predictions": sum(row.get("kronos_atr_daily_equiv") is not None for row in rows),
        "dates": dict(sorted(Counter(row["timestamp"][:10] for row in rows).items())),
        "utc_hours": counts(rows, "hour_utc"),
        "four_hour_buckets": dict(sorted(Counter(f"{int(row['hour_utc']) // 4 * 4:02d}-{int(row['hour_utc']) // 4 * 4 + 3:02d}" for row in rows).items())),
        "realized_regimes": counts(rows, "realized_regime"),
        "volatility_buckets": counts(rows, "volatility_bucket"),
        "lse_candles_by_date": dict(sorted(candles_by_date.items())),
        "errors": [row["error"] for row in rows if row.get("error")],
    }


async def run_investigation() -> dict[str, Any]:
    validate_split()
    client = LSEClient()
    try:
        calibration_rows, calibration_candles = await evaluate_checkpoints(client, CALIBRATION_CHECKPOINTS)
        # The sole threshold derivation in this final investigation.
        tuned = tune_thresholds(calibration_rows)
        calibration_result = evaluate_thresholds(
            calibration_rows, float(tuned["low_threshold"]), float(tuned["high_threshold"])
        )
        holdout_rows, holdout_candles = await evaluate_checkpoints(client, FINAL_HOLDOUT_CHECKPOINTS)
    finally:
        await client.aclose()

    holdout_result = evaluate_thresholds(
        holdout_rows, float(tuned["low_threshold"]), float(tuned["high_threshold"])
    )
    diagnostic_only = holdout_result["match_rate"] <= 0.50
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "phase": "Phase 1 capped final Kronos investigation",
        "synthetic_data_used": False,
        "data_source": "LSE BTC/USD 1m candles",
        "method": {
            "context_minutes": KRONOS_MAX_CONTEXT,
            "realized_minutes": KRONOS_PRED_LEN,
            "realized_label_thresholds": {"low_threshold": OLD_LOW_THRESHOLD, "high_threshold": OLD_HIGH_THRESHOLD},
            "threshold_derivation_cycles": 1,
            "production_thresholds_before": {"low_threshold": ATR_LOW_THRESHOLD, "high_threshold": ATR_HIGH_THRESHOLD},
            "production_thresholds_changed": False,
        },
        "exclusions": {
            "original_calibration_dates": sorted(ORIGINAL_CALIBRATION_DATES),
            "prior_holdout_dates": sorted(PRIOR_HOLDOUT_DATES),
        },
        "calibration": {
            "specifications": [{"date": day, "hour_utc": hour} for day, hour in CALIBRATION_CHECKPOINTS],
            "summary": split_summary(calibration_rows, calibration_candles),
            "derived_thresholds": tuned,
            "result_at_derived_thresholds": calibration_result,
            "checkpoints": calibration_rows,
        },
        "fresh_holdout": {
            "specifications": [{"date": day, "hour_utc": hour} for day, hour in FINAL_HOLDOUT_CHECKPOINTS],
            "summary": split_summary(holdout_rows, holdout_candles),
            "result_at_derived_thresholds": holdout_result,
            "checkpoints": holdout_rows,
        },
        "verdict": (
            "DIAGNOSTIC-ONLY, NOT A TRADING SIGNAL. Fresh holdout did not exceed 50%; "
            "this capped investigation is final and no further threshold cycles are permitted."
            if diagnostic_only
            else "Fresh holdout exceeded 50%, but this Phase 1 investigation does not change production thresholds."
        ),
        "diagnostic_only": diagnostic_only,
    }


def main() -> int:
    report = asyncio.run(run_investigation())
    OUTPUT_PATH.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({
        "calibration": report["calibration"]["result_at_derived_thresholds"],
        "fresh_holdout": report["fresh_holdout"]["result_at_derived_thresholds"],
        "verdict": report["verdict"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
