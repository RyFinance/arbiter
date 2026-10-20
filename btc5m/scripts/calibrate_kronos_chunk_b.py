#!/usr/bin/env python3
"""Chunk B: Kronos calibration — expanded validation set + threshold retuning.

This script builds a large validation set of 20+ regime checkpoints from REAL BTC
tick data (state/btc_ticks_real.parquet), spanning different times of day and
volatility regimes. For each checkpoint it:

  1. Aggregates 512 minutes of 1-minute OHLCV candles as Kronos input (context window)
  2. Runs the REAL Kronos-small model to get the forecast regime + predicted ATR%
  3. Aggregates the following 60-minute window of candles to compute realized ATR%
  4. Classifies the realized ATR% into HIGH/MED/LOW using the current thresholds

After all checkpoints are processed, it grid-searches LOW/HIGH ATR thresholds
against the combined forecast-vs-realized comparison and reports:

  - Old vs new threshold values
  - Match-rate improvement (or lack thereof)
  - Regime-match rate on the expanded validation set

Constraints:
  - Does NOT swap to Kronos-base or another model (calibration only)
  - Does NOT modify model weights or architecture
  - Uses only the local Kronos-small cache at state/kronos/kronos-small
  - No real transactions

Usage:
    python scripts/calibrate_kronos_chunk_b.py [--minimum-checkpoints N]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np
import pandas as pd

from polymarket_arb.kronos_session import (
    KRONOS_MAX_CONTEXT,
    KRONOS_PRED_LEN,
    ATR_HIGH_THRESHOLD,
    ATR_LOW_THRESHOLD,
    ATR_TIME_SCALING_FACTOR,
    MINUTES_PER_DAY,
    _compute_atr,
    _normalize_atr_to_pct,
    _scale_atr_to_daily,
    _regime_from_atr,
    _regime_params,
    _try_kronos,
    _load_kronos_model,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Freeze the pre-recalibration production baseline so the report is reproducible.
OLD_LOW_THRESHOLD = 0.02
OLD_HIGH_THRESHOLD = 0.05

# Real tick data file (7.5M ticks, July 26-30, 2026)
TICK_FILE = PROJECT_ROOT / "state" / "btc_ticks_real.parquet"

OUTPUT_PATH = PROJECT_ROOT / "state" / "kronos_calibration_chunk_b.json"


# ---------------------------------------------------------------------------
# Data loading & candle aggregation
# ---------------------------------------------------------------------------

def load_minute_candles(path: Path) -> pd.DataFrame:
    """Load real BTC tick data and aggregate into 1-minute OHLCV candles."""
    df = pd.read_parquet(path, columns=["ts", "price", "volume", "ask"])
    df = df.dropna(subset=["ts", "price"])
    df["minute"] = df["ts"].dt.floor("min")

    candles = (
        df.groupby("minute")
        .agg(
            open=("price", "first"),
            high=("price", "max"),
            low=("price", "min"),
            close=("price", "last"),
            volume=("volume", "sum"),
        )
        .reset_index()
        .rename(columns={"minute": "timestamp"})
    )
    candles = candles.drop_duplicates(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
    candles["hour_utc"] = candles["timestamp"].dt.hour
    candles["date"] = candles["timestamp"].dt.date
    return candles


def candles_to_records(df: pd.DataFrame) -> list[dict[str, Any]]:
    """Convert a candle DataFrame to list-of-dicts for kronos_session functions."""
    rows = []
    for row in df.itertuples(index=False):
        rows.append({
            "timestamp": row.timestamp.isoformat().replace("+00:00", "Z"),
            "open": float(row.open),
            "high": float(row.high),
            "low": float(row.low),
            "close": float(row.close),
            "volume": float(row.volume),
        })
    return rows


# ---------------------------------------------------------------------------
# Checkpoint selection
# ---------------------------------------------------------------------------

@dataclass
class CheckpointSpec:
    """Specification for a single validation checkpoint."""
    idx: int                          # index in the candle DataFrame
    timestamp: datetime               # UTC checkpoint timestamp
    hour_utc: int                     # hour of day (0-23)
    date_str: str                     # YYYY-MM-DD
    realized_atr_daily_equiv: float   # realized daily-equiv ATR% in following window
    realized_regime: str              # HIGH/MED/LOW (using old thresholds)
    volatility_bucket: str            # HIGH_SAMPLE / MED_SAMPLE / LOW_SAMPLE
    source_note: str = ""             # documentation


def compute_realized_atr_daily_equiv(candles: pd.DataFrame, period: int = 14) -> float:
    """Compute daily-equivalent ATR% from a DataFrame of candles."""
    records = candles_to_records(candles)
    raw_atr = _compute_atr(records, period=period)
    atr_pct = _normalize_atr_to_pct(raw_atr, records)
    return _scale_atr_to_daily(atr_pct)


def classify_threshold(atr: float, low: float = OLD_LOW_THRESHOLD, high: float = OLD_HIGH_THRESHOLD) -> str:
    """Classify ATR% into regime label."""
    if atr >= high:
        return "HIGH"
    if atr >= low:
        return "MED"
    return "LOW"


def build_candidate_checkpoints(
    candles: pd.DataFrame,
    context_minutes: int = KRONOS_MAX_CONTEXT,
    realized_minutes: int = KRONOS_PRED_LEN,
    step_minutes: int = 15,
) -> list[dict[str, Any]]:
    """Build candidate checkpoints from real candle data.

    Each checkpoint has:
      - 512 minutes of context before it (for Kronos input)
      - 60 minutes of realized data after it (for ground truth)

    Slides by step_minutes to produce dense coverage.
    """
    candidates = []
    n = len(candles)

    for idx in range(context_minutes, n - realized_minutes, step_minutes):
        context = candles.iloc[idx - context_minutes:idx]
        realized = candles.iloc[idx:idx + realized_minutes]

        if len(context) < context_minutes or len(realized) < realized_minutes:
            continue

        # Check temporal contiguity
        context_span = (context.iloc[-1].timestamp - context.iloc[0].timestamp).total_seconds() / 60
        realized_span = (realized.iloc[-1].timestamp - realized.iloc[0].timestamp).total_seconds() / 60

        # Allow some tolerance for minute gaps (e.g. market closed)
        if context_span < context_minutes * 0.8 or realized_span < realized_minutes * 0.8:
            continue

        atr_realized = compute_realized_atr_daily_equiv(realized)
        regime = classify_threshold(atr_realized)

        candidates.append({
            "idx": idx,
            "timestamp": candles.iloc[idx].timestamp,
            "hour_utc": int(candles.iloc[idx].timestamp.hour),
            "date_str": str(candles.iloc[idx].timestamp.date()),
            "realized_atr_daily_equiv": round(atr_realized, 8),
            "realized_regime": regime,
        })

    return candidates


def assign_volatility_buckets(candidates: list[dict[str, Any]]) -> None:
    """Assign HIGH_SAMPLE / MED_SAMPLE / LOW_SAMPLE based on realized ATR% terciles."""
    if not candidates:
        return

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


def select_checkpoints(
    candidates: list[dict[str, Any]],
    minimum: int = 30,
) -> list[dict[str, Any]]:
    """Select checkpoints spanning different hours of day and volatility regimes.

    Strategy: Round-robin selection across volatility buckets (HIGH/MED/LOW)
    and time-of-day buckets (4-hour windows), ensuring diverse coverage.
    """
    if len(candidates) < minimum:
        print(f"  [WARN] Only {len(candidates)} candidates; need {minimum}")

    assign_volatility_buckets(candidates)

    selected: list[dict[str, Any]] = []
    seen_slots: set[tuple[str, int, str]] = set()

    # Round-robin by volatility bucket and hour-bucket to ensure diversity
    for bucket in ("HIGH_SAMPLE", "MED_SAMPLE", "LOW_SAMPLE"):
        bucket_rows = sorted(
            [r for r in candidates if r["volatility_bucket"] == bucket],
            key=lambda r: (
                r["hour_utc"] // 4,    # group by 4-hour window
                r["hour_utc"] % 4,     # then sub-hour ordering
                r["timestamp"],         # then chronological
            ),
        )

        for row in bucket_rows:
            slot = (bucket, row["hour_utc"] // 4, row["date_str"])
            if slot in seen_slots:
                continue
            selected.append(row)
            seen_slots.add(slot)

            # Cap per bucket to desired count
            bucket_count = sum(1 for s in selected if s["volatility_bucket"] == bucket)
            if bucket_count >= minimum // 3 + (1 if minimum % 3 else 0):
                break

    # Fill remaining from all candidates if needed
    if len(selected) < minimum:
        for row in sorted(candidates, key=lambda r: (r["timestamp"], r["volatility_bucket"])):
            if row not in selected:
                selected.append(row)
            if len(selected) >= minimum:
                break

    return sorted(selected[:minimum], key=lambda r: r["timestamp"])


# ---------------------------------------------------------------------------
# Kronos inference per checkpoint
# ---------------------------------------------------------------------------

@dataclass
class CheckpointResult:
    """Full result for a single checkpoint."""
    timestamp: str
    hour_utc: int
    date: str
    volatility_bucket: str
    realized_atr_daily_equiv: float
    realized_regime: str
    context_candles: int
    realized_candles: int
    kronos_atr_daily_equiv: Optional[float] = None
    kronos_regime: Optional[str] = None
    kronos_confidence: float = 0.0
    kronos_source: str = ""
    kronos_inference_time_seconds: float = 0.0
    heuristic_atr_daily_equiv: Optional[float] = None
    heuristic_regime: Optional[str] = None
    regime_match: Optional[bool] = None
    rank_diff: int = 0
    error: Optional[str] = None
    kronos_regime_old: Optional[str] = None
    kronos_regime_new: Optional[str] = None
    old_match: Optional[bool] = None
    new_match: Optional[bool] = None


def run_kronos_checkpoint(
    candles: pd.DataFrame,
    row: dict[str, Any],
    predictor_available: bool,
) -> CheckpointResult:
    """Run Kronos inference on a single checkpoint."""
    idx = int(row["idx"])
    context = candles.iloc[idx - KRONOS_MAX_CONTEXT:idx]
    realized = candles.iloc[idx:idx + KRONOS_PRED_LEN]

    result = CheckpointResult(
        timestamp=candles.iloc[idx].timestamp.isoformat().replace("+00:00", "Z"),
        hour_utc=int(row["hour_utc"]),
        date=str(row["date_str"]),
        volatility_bucket=row["volatility_bucket"],
        realized_atr_daily_equiv=round(float(row["realized_atr_daily_equiv"]), 8),
        realized_regime=row["realized_regime"],
        context_candles=len(context),
        realized_candles=len(realized),
    )

    # --- Heuristic baseline (ATR on context window) ---
    ctx_records = candles_to_records(context)
    raw_atr = _compute_atr(ctx_records, period=14)
    atr_pct = _normalize_atr_to_pct(raw_atr, ctx_records)
    atr_scaled = _scale_atr_to_daily(atr_pct)
    heur_regime, heur_conf = _regime_from_atr(atr_scaled)

    result.heuristic_atr_daily_equiv = round(atr_scaled, 6)
    result.heuristic_regime = heur_regime

    # --- REAL Kronos model inference ---
    t0 = time.time()
    if predictor_available:
        try:
            vr = _try_kronos(ctx_records)
            elapsed = time.time() - t0

            if vr is not None:
                result.kronos_atr_daily_equiv = round(vr.atr_14d, 6)
                result.kronos_regime = vr.regime
                result.kronos_confidence = round(vr.confidence, 4)
                result.kronos_source = vr.source
                result.kronos_inference_time_seconds = round(elapsed, 2)

                # Classify with old and new thresholds
                if vr.atr_14d is not None:
                    result.kronos_regime_old = classify_threshold(vr.atr_14d, OLD_LOW_THRESHOLD, OLD_HIGH_THRESHOLD)
                    result.old_match = result.kronos_regime_old == result.realized_regime
            else:
                result.error = "Kronos returned None"
        except Exception as exc:
            elapsed = time.time() - t0
            result.kronos_inference_time_seconds = round(elapsed, 2)
            result.error = f"{exc.__class__.__name__}: {exc}"
            result.kronos_regime = None
    else:
        result.error = "Kronos model not available"

    # --- Determine forecast regime (Kronos if available, else heuristic) ---
    if result.kronos_regime:
        forecast_regime = result.kronos_regime
        forecast_atr = result.kronos_atr_daily_equiv
    else:
        forecast_regime = heur_regime
        forecast_atr = atr_scaled

    # --- Comparison: forecast vs realized ---
    result.regime_match = forecast_regime == result.realized_regime

    regime_order = {"LOW": 0, "MED": 1, "HIGH": 2}
    fc_rank = regime_order.get(forecast_regime, -1)
    rl_rank = regime_order.get(result.realized_regime, -1)
    result.rank_diff = fc_rank - rl_rank

    # Classify with new thresholds (will be filled after tuning)
    if forecast_atr is not None:
        result.kronos_regime_new = classify_threshold(forecast_atr, OLD_LOW_THRESHOLD, OLD_HIGH_THRESHOLD)

    return result


# ---------------------------------------------------------------------------
# Threshold grid search
# ---------------------------------------------------------------------------

def classify_atr(atr: float, low: float, high: float) -> str:
    if atr >= high:
        return "HIGH"
    if atr >= low:
        return "MED"
    return "LOW"


def grid_search_thresholds(
    results: list[CheckpointResult],
) -> dict[str, Any]:
    """Grid-search LOW/HIGH thresholds to maximize match rate."""
    valid = [r for r in results if r.kronos_atr_daily_equiv is not None]
    if not valid:
        return {"error": "No valid Kronos predictions"}

    preds = np.array([r.kronos_atr_daily_equiv for r in valid])
    realized = np.array([r.realized_regime for r in valid])
    regime_order = {"LOW": 0, "MED": 1, "HIGH": 2}
    realized_ranks = np.array([regime_order[r] for r in realized])

    def classify_arr(arr: np.ndarray, low: float, high: float) -> np.ndarray:
        ranks = np.zeros(len(arr), dtype=int)
        ranks[arr >= high] = 2
        ranks[(arr >= low) & (arr < high)] = 1
        return ranks

    # Old thresholds
    old_ranks = classify_arr(preds, OLD_LOW_THRESHOLD, OLD_HIGH_THRESHOLD)
    old_matches = int(np.sum(old_ranks == realized_ranks))
    old_rate = old_matches / len(valid)

    # Grid search
    best: tuple[float, float, float, int] | None = None
    sweep_results = []

    # Conservative grid: high in [0.025, 0.12], low in [0.005, 0.05]
    high_range = np.arange(0.025, 0.125, 0.005)
    low_range = np.arange(0.005, 0.055, 0.005)

    for h in high_range:
        for l in low_range:
            if h <= l:
                continue
            ranks = classify_arr(preds, l, h)
            matches = int(np.sum(ranks == realized_ranks))
            rate = matches / len(valid)
            sweep_results.append({
                "high": round(h, 3),
                "low": round(l, 3),
                "match_rate": round(rate, 4),
                "matches": matches,
            })
            distance = abs(l - OLD_LOW_THRESHOLD) + abs(h - OLD_HIGH_THRESHOLD)
            candidate = (rate, -distance, l, h, matches)
            if best is None or candidate > best:
                best = candidate

    assert best is not None
    best_rate, _neg_dist, best_low, best_high, best_matches = best

    # Apply best thresholds
    for r in valid:
        r.kronos_regime_new = classify_atr(r.kronos_atr_daily_equiv, best_low, best_high)
        r.new_match = r.kronos_regime_new == r.realized_regime
        r.old_match = r.kronos_regime_old == r.realized_regime if r.kronos_regime_old else False

    return {
        "valid_samples": len(valid),
        "old_thresholds": {
            "low": OLD_LOW_THRESHOLD,
            "high": OLD_HIGH_THRESHOLD,
            "match_rate": round(old_rate, 4),
            "matches": old_matches,
            "mismatches": len(valid) - old_matches,
        },
        "new_thresholds": {
            "low": round(best_low, 3),
            "high": round(best_high, 3),
            "match_rate": round(best_rate, 4),
            "matches": best_matches,
            "mismatches": len(valid) - best_matches,
        },
        "match_rate_improvement": round(best_rate - old_rate, 4),
        "sweep_results_top10": sorted(sweep_results, key=lambda x: -x["match_rate"])[:10],
    }


# ---------------------------------------------------------------------------
# Regime distribution analysis
# ---------------------------------------------------------------------------

def analyze_regime_distributions(
    results: list[CheckpointResult],
) -> dict[str, Any]:
    """Analyze predicted vs realized distributions by regime."""
    valid = [r for r in results if r.kronos_atr_daily_equiv is not None]

    # Group by realized regime
    by_realized: dict[str, list[float]] = {"HIGH": [], "MED": [], "LOW": []}
    by_forecast: dict[str, list[float]] = {"HIGH": [], "MED": [], "LOW": []}

    for r in valid:
        by_realized[r.realized_regime].append(r.kronos_atr_daily_equiv)
        if r.kronos_regime:
            by_forecast[r.kronos_regime].append(r.kronos_atr_daily_equiv)

    def dist_summary(atrs: list[float]) -> dict:
        if not atrs:
            return {"count": 0}
        arr = np.array(atrs)
        return {
            "count": len(arr),
            "mean": round(float(arr.mean()), 6),
            "median": round(float(np.median(arr)), 6),
            "min": round(float(arr.min()), 6),
            "max": round(float(arr.max()), 6),
            "std": round(float(arr.std()), 6),
        }

    return {
        "predicted_atr_by_realized_regime": {
            regime: dist_summary(atrs) for regime, atrs in by_realized.items()
        },
        "predicted_atr_by_forecast_regime": {
            regime: dist_summary(atrs) for regime, atrs in by_forecast.items()
        },
        "forecast_regime_distribution": {
            regime: sum(1 for r in valid if r.kronos_regime == regime)
            for regime in ("HIGH", "MED", "LOW")
        },
        "realized_regime_distribution": {
            regime: sum(1 for r in valid if r.realized_regime == regime)
            for regime in ("HIGH", "MED", "LOW")
        },
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Chunk B: Kronos calibration with real data")
    parser.add_argument("--minimum-checkpoints", type=int, default=30,
                        help="Minimum number of checkpoints to select (default 30)")
    parser.add_argument("--output", default=str(OUTPUT_PATH))
    parser.add_argument("--skip-kronos", action="store_true",
                        help="Select checkpoints only; skip Kronos inference for speed")
    args = parser.parse_args()

    print("=" * 80)
    print("CHUNK B: KRONOS CALIBRATION — Expanded Validation Set")
    print("=" * 80)
    print(f"Data source: {TICK_FILE}")
    print(f"Kronos model: state/kronos/kronos-small (NeoQuasar/Kronos-small, 24.7M params)")
    print(f"ATR thresholds (current): HIGH >= {ATR_HIGH_THRESHOLD}, LOW < {ATR_LOW_THRESHOLD}")
    print(f"Time scaling: 1-min ATR% -> daily-equiv via sqrt(1440) = {ATR_TIME_SCALING_FACTOR:.4f}")
    print()

    # --- Load real tick data ---
    print("Loading real BTC tick data...")
    candles = load_minute_candles(TICK_FILE)
    print(f"  Loaded {len(candles)} 1-minute candles")
    print(f"  Time range: {candles['timestamp'].min()} -> {candles['timestamp'].max()}")
    print(f"  Price range: {candles['low'].min():.2f} - {candles['high'].max():.2f}")
    print()

    # --- Build candidate checkpoints ---
    print(f"Building candidate checkpoints (step={15}min, context={KRONOS_MAX_CONTEXT}min, realized={KRONOS_PRED_LEN}min)...")
    candidates = build_candidate_checkpoints(candles, step_minutes=15)
    print(f"  {len(candidates)} candidate checkpoints generated")

    # --- Select diverse checkpoints ---
    selected = select_checkpoints(candidates, minimum=args.minimum_checkpoints)
    print(f"  {len(selected)} checkpoints selected (spans {selected[0]['date_str']} to {selected[-1]['date_str']})")

    # Print hour distribution
    hour_counts = {}
    for s in selected:
        h = s["hour_utc"]
        hour_counts[h] = hour_counts.get(h, 0) + 1
    print(f"  Hours UTC covered: {sorted(hour_counts.keys())}")
    print(f"  Hour distribution: {hour_counts}")

    # Print volatility bucket distribution
    bucket_counts = {}
    for s in selected:
        b = s["volatility_bucket"]
        bucket_counts[b] = bucket_counts.get(b, 0) + 1
    print(f"  Volatility buckets: {bucket_counts}")

    # Print realized regime distribution
    regime_counts = {}
    for s in selected:
        r = s["realized_regime"]
        regime_counts[r] = regime_counts.get(r, 0) + 1
    print(f"  Realized regimes: {regime_counts}")
    print()

    # --- Load Kronos model ---
    print("Loading Kronos-small model...")
    t0 = time.time()
    predictor = _load_kronos_model()
    load_time = time.time() - t0
    model_available = predictor is not None
    print(f"  Loaded in {load_time:.2f}s, available={model_available}")
    print()

    # --- Run checkpoints ---
    results: list[CheckpointResult] = []
    source_counts = {"kronos": 0, "heuristic": 0, "error": 0}

    for i, row in enumerate(selected, 1):
        cp_label = f"[{i}/{len(selected)}] {row['timestamp'].strftime('%Y-%m-%d %H:%M UTC')} "
        cp_label += f"(hour={row['hour_utc']}, bucket={row['volatility_bucket']}, "
        cp_label += f"realized_ATR%={row['realized_atr_daily_equiv']:.4f}, realized={row['realized_regime']})"

        result = run_kronos_checkpoint(candles, row, model_available and not args.skip_kronos)
        results.append(result)

        if result.kronos_regime:
            source_counts["kronos"] += 1
            match_str = "MATCH" if result.regime_match else "MISMATCH"
            print(f"  {cp_label}")
            print(f"    Kronos: {result.kronos_regime} (ATR%={result.kronos_atr_daily_equiv}, "
                  f"conf={result.kronos_confidence:.2f}, {result.kronos_inference_time_seconds}s) "
                  f"-> {match_str}")
        elif result.error:
            source_counts["error"] += 1
            print(f"  {cp_label}")
            print(f"    Error: {result.error}")
        else:
            source_counts["heuristic"] += 1
            match_str = "MATCH" if result.regime_match else "MISMATCH"
            print(f"  {cp_label}")
            print(f"    Heuristic: {result.heuristic_regime} (ATR%={result.heuristic_atr_daily_equiv}) -> {match_str}")

    print()

    # --- Threshold analysis ---
    print("=" * 60)
    print("THRESHOLD RETUNING ANALYSIS")
    print("=" * 60)

    threshold_results = grid_search_thresholds(results)
    print(f"Valid Kronos predictions: {threshold_results['valid_samples']}/{len(results)}")
    print(f"OLD thresholds: LOW={threshold_results['old_thresholds']['low']}, "
          f"HIGH={threshold_results['old_thresholds']['high']} "
          f"-> match_rate={threshold_results['old_thresholds']['match_rate']} "
          f"({threshold_results['old_thresholds']['matches']}/{threshold_results['valid_samples']})")
    print(f"NEW thresholds: LOW={threshold_results['new_thresholds']['low']}, "
          f"HIGH={threshold_results['new_thresholds']['high']} "
          f"-> match_rate={threshold_results['new_thresholds']['match_rate']} "
          f"({threshold_results['new_thresholds']['matches']}/{threshold_results['valid_samples']})")
    print(f"Match rate improvement: {threshold_results['match_rate_improvement']:+.4f}")
    print()

    # --- Regime distribution analysis ---
    print("=" * 60)
    print("REGIME DISTRIBUTION ANALYSIS")
    print("=" * 60)
    dist_analysis = analyze_regime_distributions(results)
    print("Predicted ATR% by realized regime:")
    for regime in ("HIGH", "MED", "LOW"):
        d = dist_analysis["predicted_atr_by_realized_regime"][regime]
        if d["count"] > 0:
            print(f"  REALIZED {regime}: n={d['count']}, mean={d['mean']}, "
                  f"median={d['median']}, range=[{d['min']}, {d['max']}], std={d['std']}")
        else:
            print(f"  REALIZED {regime}: n=0")
    print()
    print("Predicted ATR% by forecast regime:")
    for regime in ("HIGH", "MED", "LOW"):
        d = dist_analysis["predicted_atr_by_forecast_regime"][regime]
        if d["count"] > 0:
            print(f"  FORECAST {regime}: n={d['count']}, mean={d['mean']}, "
                  f"median={d['median']}, range=[{d['min']}, {d['max']}], std={d['std']}")
        else:
            print(f"  FORECAST {regime}: n=0")
    print()
    print(f"Forecast regime distribution: {dist_analysis['forecast_regime_distribution']}")
    print(f"Realized regime distribution: {dist_analysis['realized_regime_distribution']}")
    print()

    # --- Boundary analysis ---
    print("=" * 60)
    print("BOUNDARY ANALYSIS")
    print("=" * 60)
    realized_data = dist_analysis["predicted_atr_by_realized_regime"]

    # Find natural gaps between regimes
    for pair in [("LOW", "MED"), ("MED", "HIGH")]:
        low_reg, high_reg = pair
        low_data = realized_data.get(low_reg, {})
        high_data = realized_data.get(high_reg, {})
        if low_data.get("count", 0) > 0 and high_data.get("count", 0) > 0:
            low_max = low_data.get("max", 0)
            high_min = high_data.get("min", 1)
            gap = "GAP EXISTS" if high_min > low_max else "OVERLAP"
            midpoint = (low_max + high_min) / 2 if high_min > low_max else None
            print(f"  {low_reg} -> {high_reg}: low_max={low_max}, high_min={high_min}, {gap}", end="")
            if midpoint:
                print(f", suggested boundary={round(midpoint, 4)}")
            else:
                print()
    print()

    # --- Final match rates ---
    valid = [r for r in results if r.kronos_atr_daily_equiv is not None]
    total_valid = len(valid)
    old_matches = sum(1 for r in valid if r.old_match)
    new_matches = sum(1 for r in valid if r.new_match)

    print("=" * 80)
    print("FINAL SUMMARY")
    print("=" * 80)
    print(f"Total checkpoints: {len(results)}")
    print(f"Valid Kronos predictions: {total_valid}")
    print(f"Source breakdown: {source_counts}")
    print()
    print(f"OLD thresholds (LOW={OLD_LOW_THRESHOLD}, HIGH={OLD_HIGH_THRESHOLD}):")
    print(f"  Match rate: {round(old_matches / total_valid, 4) if total_valid else 0} "
          f"({old_matches}/{total_valid})")
    print()
    print(f"NEW thresholds (LOW={threshold_results['new_thresholds']['low']}, "
          f"HIGH={threshold_results['new_thresholds']['high']}):")
    print(f"  Match rate: {round(new_matches / total_valid, 4) if total_valid else 0} "
          f"({new_matches}/{total_valid})")
    print()
    improvement = (new_matches / total_valid - old_matches / total_valid) if total_valid else 0
    print(f"Match rate improvement: {improvement:+.4f} ({improvement*100:+.1f}%)")
    print()

    # --- Write output ---
    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "script": "scripts/calibrate_kronos_chunk_b.py",
        "task": "Chunk B: Kronos calibration",
        "configuration": {
            "tick_data_source": str(TICK_FILE),
            "tick_data_range": f"{candles['timestamp'].min()} -> {candles['timestamp'].max()}",
            "total_candles": len(candles),
            "context_minutes": KRONOS_MAX_CONTEXT,
            "realized_minutes": KRONOS_PRED_LEN,
            "step_minutes": 15,
            "at_r_period": 14,
            "old_atr_high_threshold": OLD_HIGH_THRESHOLD,
            "old_atr_low_threshold": OLD_LOW_THRESHOLD,
            "current_atr_high_threshold": ATR_HIGH_THRESHOLD,
            "current_atr_low_threshold": ATR_LOW_THRESHOLD,
            "at_r_normalization": (
                f"1-minute ATR normalized to percentage via mean-close, then "
                f"scaled to daily-equivalent via sqrt({MINUTES_PER_DAY})="
                f"{ATR_TIME_SCALING_FACTOR:.4f} before applying daily-calibrated thresholds."
            ),
            "kronos_model": "state/kronos/kronos-small (NeoQuasar/Kronos-small, 24.7M params)",
        },
        "summary": {
            "total_checkpoints": len(results),
            "valid_kronos_predictions": total_valid,
            "source_breakdown": source_counts,
            "kronos_model_load_time_seconds": round(load_time, 2),
            "kronos_model_available": model_available,
            "forecast_regime_distribution": dist_analysis["forecast_regime_distribution"],
            "realized_regime_distribution": dist_analysis["realized_regime_distribution"],
            "hour_utc_distribution": hour_counts,
            "volatility_bucket_distribution": bucket_counts,
        },
        "threshold_analysis": threshold_results,
        "regime_distributions": dist_analysis,
        "detailed_results": [asdict(r) for r in results],
    }

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(output, indent=2, default=str) + "\n")
    print(f"\nResults written to {out_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
