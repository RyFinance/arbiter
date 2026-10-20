#!/usr/bin/env python3
"""Recalibrate Kronos ATR regime thresholds from real BTC tick checkpoints.

This script uses only observed tick data from state/btc_ticks_*.parquet. It
aggregates ticks into 1-minute OHLCV candles, samples real checkpoints across
time-of-day and realized-volatility quantiles, runs the existing Kronos-small
forecast path, then grid-searches LOW/HIGH ATR thresholds against the realized
next-session regime labels.

No synthetic checkpoints are used here.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from dataclasses import asdict, dataclass
from datetime import timezone
from pathlib import Path
from typing import Any, Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd

from polymarket_arb.kronos_session import (
    KRONOS_MAX_CONTEXT,
    KRONOS_PRED_LEN,
    _compute_atr,
    _normalize_atr_to_pct,
    _scale_atr_to_daily,
    _try_kronos,
)

# Freeze the pre-recalibration production baseline so this script remains
# reproducible after polymarket_arb.kronos_session is updated.
OLD_LOW_THRESHOLD = 0.02
OLD_HIGH_THRESHOLD = 0.05

DEFAULT_TICK_FILES = [
    PROJECT_ROOT / "state" / "btc_ticks_jul26-28.parquet",
    PROJECT_ROOT / "state" / "btc_ticks_jul29.parquet",
    PROJECT_ROOT / "state" / "btc_ticks_jul30-31.parquet",
]
OUTPUT_PATH = PROJECT_ROOT / "state" / "kronos_threshold_calibration.json"


@dataclass(frozen=True)
class Checkpoint:
    timestamp: str
    hour_utc: int
    source_file: str
    volatility_bucket: str
    context_candles: int
    realized_candles: int
    realized_atr_daily_equiv: float
    realized_regime: str
    kronos_atr_daily_equiv: float | None = None
    kronos_regime_old: str | None = None
    kronos_source: str | None = None
    error: str | None = None


def classify_with_thresholds(atr: float, low: float, high: float) -> str:
    if atr >= high:
        return "HIGH"
    if atr >= low:
        return "MED"
    return "LOW"


def match_rate(rows: Iterable[dict[str, Any]], low: float, high: float) -> float:
    valid = [r for r in rows if r.get("kronos_atr_daily_equiv") is not None]
    if not valid:
        return 0.0
    matches = sum(
        classify_with_thresholds(float(r["kronos_atr_daily_equiv"]), low, high)
        == r["realized_regime"]
        for r in valid
    )
    return matches / len(valid)


def tune_thresholds(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Grid-search only LOW/HIGH threshold constants.

    The grid is intentionally conservative: it searches plausible daily-equivalent
    ATR% values around the current calibrated thresholds and preserves low < high.
    Ties prefer thresholds closest to the current production constants.
    """
    valid = [r for r in rows if r.get("kronos_atr_daily_equiv") is not None]
    if not valid:
        return {
            "low_threshold": OLD_LOW_THRESHOLD,
            "high_threshold": OLD_HIGH_THRESHOLD,
            "match_rate": 0.0,
            "matches": 0,
            "total": 0,
        }

    best: tuple[float, float, float, int, float] | None = None
    for low_i in range(8, 41):
        low = low_i / 1000.0
        for high_i in range(max(low_i + 5, 20), 91):
            high = high_i / 1000.0
            matches = sum(
                classify_with_thresholds(float(r["kronos_atr_daily_equiv"]), low, high)
                == r["realized_regime"]
                for r in valid
            )
            rate = matches / len(valid)
            distance = abs(low - OLD_LOW_THRESHOLD) + abs(high - OLD_HIGH_THRESHOLD)
            candidate = (rate, -distance, low, high, matches)
            if best is None or candidate > best:
                best = candidate

    assert best is not None
    rate, _neg_distance, low, high, matches = best
    return {
        "low_threshold": low,
        "high_threshold": high,
        "match_rate": rate,
        "matches": int(matches),
        "total": len(valid),
    }


def load_minute_candles(paths: list[Path]) -> pd.DataFrame:
    frames = []
    for path in paths:
        df = pd.read_parquet(path, columns=["ts", "price", "volume"])
        df = df.dropna(subset=["ts", "price"])
        df["source_file"] = path.name
        frames.append(df)
    ticks = pd.concat(frames, ignore_index=True).sort_values("ts")
    ticks["minute"] = ticks["ts"].dt.floor("min")
    candles = (
        ticks.groupby("minute")
        .agg(
            open=("price", "first"),
            high=("price", "max"),
            low=("price", "min"),
            close=("price", "last"),
            volume=("volume", "sum"),
            source_file=("source_file", "last"),
        )
        .reset_index()
        .rename(columns={"minute": "timestamp"})
    )
    return candles.drop_duplicates(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)


def candles_to_records(df: pd.DataFrame) -> list[dict[str, Any]]:
    rows = []
    for row in df.itertuples(index=False):
        rows.append(
            {
                "timestamp": row.timestamp.isoformat().replace("+00:00", "Z"),
                "open": float(row.open),
                "high": float(row.high),
                "low": float(row.low),
                "close": float(row.close),
                "volume": float(row.volume),
            }
        )
    return rows


def realized_atr_daily_equiv(candles: pd.DataFrame) -> float:
    records = candles_to_records(candles)
    raw_atr = _compute_atr(records, period=14)
    atr_pct = _normalize_atr_to_pct(raw_atr, records)
    return _scale_atr_to_daily(atr_pct)


def build_candidate_checkpoints(
    candles: pd.DataFrame,
    context_minutes: int = KRONOS_MAX_CONTEXT,
    realized_minutes: int = KRONOS_PRED_LEN,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for idx in range(context_minutes, len(candles) - realized_minutes, 15):
        context = candles.iloc[idx - context_minutes : idx]
        realized = candles.iloc[idx : idx + realized_minutes]
        if len(context) < context_minutes or len(realized) < realized_minutes:
            continue
        # Require contiguous enough data for a real 1-minute regime checkpoint.
        context_span = (context.iloc[-1].timestamp - context.iloc[0].timestamp).total_seconds() / 60
        realized_span = (realized.iloc[-1].timestamp - realized.iloc[0].timestamp).total_seconds() / 60
        if context_span > context_minutes + 5 or realized_span > realized_minutes + 5:
            continue
        atr = realized_atr_daily_equiv(realized)
        candidates.append(
            {
                "idx": idx,
                "timestamp": candles.iloc[idx].timestamp,
                "hour_utc": int(candles.iloc[idx].timestamp.hour),
                "source_file": str(candles.iloc[idx].source_file),
                "realized_atr_daily_equiv": atr,
                "realized_regime": classify_with_thresholds(
                    atr, OLD_LOW_THRESHOLD, OLD_HIGH_THRESHOLD
                ),
            }
        )
    return candidates


def select_checkpoints(candidates: list[dict[str, Any]], minimum: int = 27) -> list[dict[str, Any]]:
    if len(candidates) < minimum:
        raise ValueError(f"Need at least {minimum} candidate checkpoints; found {len(candidates)}")

    ranked = sorted(candidates, key=lambda r: r["realized_atr_daily_equiv"])
    n = len(ranked)
    low_cut = ranked[max(0, math.floor(n / 3) - 1)]["realized_atr_daily_equiv"]
    high_cut = ranked[min(n - 1, math.floor(2 * n / 3))]["realized_atr_daily_equiv"]
    for row in candidates:
        atr = row["realized_atr_daily_equiv"]
        if atr <= low_cut:
            row["volatility_bucket"] = "LOW_SAMPLE"
        elif atr >= high_cut:
            row["volatility_bucket"] = "HIGH_SAMPLE"
        else:
            row["volatility_bucket"] = "MED_SAMPLE"

    selected: list[dict[str, Any]] = []
    seen_slots: set[tuple[str, int, str]] = set()
    # Round-robin by volatility bucket and hour bucket to avoid one quiet period
    # dominating the set.
    for bucket in ("HIGH_SAMPLE", "MED_SAMPLE", "LOW_SAMPLE"):
        bucket_rows = sorted(
            [r for r in candidates if r["volatility_bucket"] == bucket],
            key=lambda r: (r["timestamp"].hour % 8, r["timestamp"], -r["realized_atr_daily_equiv"]),
        )
        for row in bucket_rows:
            slot = (bucket, row["timestamp"].hour // 4, row["timestamp"].date().isoformat())
            if slot in seen_slots:
                continue
            selected.append(row)
            seen_slots.add(slot)
            if len([r for r in selected if r["volatility_bucket"] == bucket]) >= minimum // 3:
                break

    if len(selected) < minimum:
        for row in sorted(candidates, key=lambda r: (r["timestamp"], r["volatility_bucket"])):
            if row not in selected:
                selected.append(row)
            if len(selected) >= minimum:
                break
    return sorted(selected[:minimum], key=lambda r: r["timestamp"])


def run_kronos_for_checkpoint(candles: pd.DataFrame, row: dict[str, Any]) -> Checkpoint:
    idx = int(row["idx"])
    context = candles.iloc[idx - KRONOS_MAX_CONTEXT : idx]
    realized = candles.iloc[idx : idx + KRONOS_PRED_LEN]
    base = Checkpoint(
        timestamp=row["timestamp"].isoformat().replace("+00:00", "Z"),
        hour_utc=int(row["hour_utc"]),
        source_file=str(row["source_file"]),
        volatility_bucket=str(row["volatility_bucket"]),
        context_candles=len(context),
        realized_candles=len(realized),
        realized_atr_daily_equiv=round(float(row["realized_atr_daily_equiv"]), 8),
        realized_regime=str(row["realized_regime"]),
    )
    try:
        result = _try_kronos(candles_to_records(context))
        if result is None:
            return Checkpoint(**{**asdict(base), "error": "Kronos returned None"})
        kronos_atr = float(result.atr_14d)
        return Checkpoint(
            **{
                **asdict(base),
                "kronos_atr_daily_equiv": round(kronos_atr, 8),
                "kronos_regime_old": classify_with_thresholds(
                    kronos_atr, OLD_LOW_THRESHOLD, OLD_HIGH_THRESHOLD
                ),
                "kronos_source": result.source,
            }
        )
    except Exception as exc:  # pragma: no cover - defensive runtime reporting
        return Checkpoint(**{**asdict(base), "error": repr(exc)})


def summarize(rows: list[dict[str, Any]], tuned: dict[str, Any]) -> dict[str, Any]:
    valid = [r for r in rows if r.get("kronos_atr_daily_equiv") is not None]
    old_rate = match_rate(rows, OLD_LOW_THRESHOLD, OLD_HIGH_THRESHOLD)
    new_rate = match_rate(rows, tuned["low_threshold"], tuned["high_threshold"])
    for row in rows:
        atr = row.get("kronos_atr_daily_equiv")
        if atr is not None:
            row["kronos_regime_new"] = classify_with_thresholds(
                float(atr), tuned["low_threshold"], tuned["high_threshold"]
            )
            row["kronos_regime_old"] = classify_with_thresholds(
                float(atr), OLD_LOW_THRESHOLD, OLD_HIGH_THRESHOLD
            )
            row["old_match"] = row["kronos_regime_old"] == row["realized_regime"]
            row["new_match"] = row["kronos_regime_new"] == row["realized_regime"]

    return {
        "generated_at": pd.Timestamp.now(tz=timezone.utc).isoformat(),
        "data_source": "real BTC/USD ticks from state/btc_ticks_*.parquet",
        "synthetic_data_used": False,
        "configuration": {
            "context_minutes": KRONOS_MAX_CONTEXT,
            "realized_minutes": KRONOS_PRED_LEN,
            "old_low_threshold": OLD_LOW_THRESHOLD,
            "old_high_threshold": OLD_HIGH_THRESHOLD,
        },
        "validation_set": {
            "checkpoints": len(rows),
            "valid_kronos_predictions": len(valid),
            "time_span_start": min(r["timestamp"] for r in rows) if rows else None,
            "time_span_end": max(r["timestamp"] for r in rows) if rows else None,
            "source_files": sorted({r["source_file"] for r in rows}),
            "volatility_buckets": {
                bucket: sum(1 for r in rows if r["volatility_bucket"] == bucket)
                for bucket in ("HIGH_SAMPLE", "MED_SAMPLE", "LOW_SAMPLE")
            },
            "realized_regimes_old_thresholds": {
                regime: sum(1 for r in rows if r["realized_regime"] == regime)
                for regime in ("HIGH", "MED", "LOW")
            },
            "hours_utc": sorted({r["hour_utc"] for r in rows}),
        },
        "old_thresholds": {
            "low_threshold": OLD_LOW_THRESHOLD,
            "high_threshold": OLD_HIGH_THRESHOLD,
            "match_rate": old_rate,
            "matches": sum(1 for r in rows if r.get("old_match")),
            "total": len(valid),
        },
        "new_thresholds": tuned | {"match_rate": new_rate},
        "match_rate_change": new_rate - old_rate,
        "checkpoints": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=str(OUTPUT_PATH))
    parser.add_argument("--minimum-checkpoints", type=int, default=27)
    parser.add_argument("--skip-kronos", action="store_true", help="Select checkpoints only; for fast diagnostics")
    args = parser.parse_args()

    candles = load_minute_candles(DEFAULT_TICK_FILES)
    candidates = build_candidate_checkpoints(candles)
    selected = select_checkpoints(candidates, minimum=args.minimum_checkpoints)

    if args.skip_kronos:
        rows = []
        for row in selected:
            rows.append(
                asdict(
                    Checkpoint(
                        timestamp=row["timestamp"].isoformat().replace("+00:00", "Z"),
                        hour_utc=int(row["hour_utc"]),
                        source_file=str(row["source_file"]),
                        volatility_bucket=str(row["volatility_bucket"]),
                        context_candles=KRONOS_MAX_CONTEXT,
                        realized_candles=KRONOS_PRED_LEN,
                        realized_atr_daily_equiv=round(float(row["realized_atr_daily_equiv"]), 8),
                        realized_regime=str(row["realized_regime"]),
                        error="Kronos skipped by --skip-kronos",
                    )
                )
            )
    else:
        rows = [asdict(run_kronos_for_checkpoint(candles, row)) for row in selected]

    tuned = tune_thresholds(rows)
    report = summarize(rows, tuned)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n")

    print(json.dumps({k: report[k] for k in ("validation_set", "old_thresholds", "new_thresholds", "match_rate_change")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
