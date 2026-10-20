#!/usr/bin/env python3
"""Chunk B Final: Combined Kronos calibration across all real data sources.

Merges ALL available checkpoint datasets into one comprehensive validation set:
  1. kronos_spotcheck.json (20 checkpoints) — real LSE 1m candles, Jan-Jul 2026
     * Has ground-truth daily regimes (30-day daily ATR%): 2 HIGH, 9 MED, 9 LOW
     * Also has realized 1h regime and realized daily regime
  2. kronos_threshold_calibration.json (27 checkpoints) — real BTC/USD ticks, Jul 26-30
  3. kronos_calibration_chunk_b.json (30 checkpoints) — real BTC/USD ticks, Jul 26-30 (NEW)

Deduplicates by timestamp (13 overlap between #2 and #3), yielding 64 unique
checkpoints spanning 24 dates from Jan 28 to Jul 30, 2026.

Three reference label sets are used for threshold grid search:
  A. Realized 1h regime: 60-min window realized daily-equiv ATR% (noisier, all 64)
  B. Ground-truth daily regime: 30-day daily ATR% (stable, 20 spotcheck checkpoints)
  C. Realized daily regime: 24h daily ATR% realized (20 spotcheck checkpoints)

Grid-searches HIGH/MED/LOW ATR thresholds and reports old vs new with match rates.

Constraints:
  - Does NOT swap to Kronos-base or another model (calibration only)
  - Uses only the local Kronos-small cache
  - No real transactions
"""
from __future__ import annotations

import json
import math
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np

STATE_DIR = PROJECT_ROOT / "state"
SPOTCHECK_PATH = STATE_DIR / "kronos_spotcheck.json"
CALIBRATION_PATH = STATE_DIR / "kronos_threshold_calibration.json"
CHUNK_B_PATH = STATE_DIR / "kronos_calibration_chunk_b.json"
OUTPUT_PATH = STATE_DIR / "kronos_chunk_b_final_calibration.json"

OLD_LOW_THRESHOLD = 0.02
OLD_HIGH_THRESHOLD = 0.05
CURRENT_LOW_THRESHOLD = 0.03
CURRENT_HIGH_THRESHOLD = 0.05


def classify_atr(atr: float, low: float, high: float) -> str:
    if atr >= high:
        return "HIGH"
    if atr >= low:
        return "MED"
    return "LOW"


def classify_arr(arr: np.ndarray, low: float, high: float) -> np.ndarray:
    ranks = np.zeros(len(arr), dtype=int)
    ranks[arr >= high] = 2
    ranks[(arr >= low) & (arr < high)] = 1
    return ranks


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def load_spotcheck() -> list[dict[str, Any]]:
    with open(SPOTCHECK_PATH) as f:
        data = json.load(f)
    results = []
    for r in data.get("detailed_results", []):
        pred = r.get("predicted_atr_pct_daily_equiv")
        if pred is None:
            pred = r.get("atr_pct_daily_equiv")
        results.append({
            "timestamp": r["timestamp"],
            "source_dataset": "spotcheck_real_LSE",
            "hour_utc": datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00")).hour,
            "date": r["timestamp"][:10],
            "predicted_atr_pct_daily_equiv": pred,
            "realized_regime_1h": r.get("realized_regime"),
            "realized_atr_daily_equiv_1h": r.get("realized_atr_pct_daily_equiv"),
            "realized_daily_regime": r.get("realized_daily_regime"),
            "realized_daily_atr_pct": r.get("realized_daily_atr_pct"),
            "ground_truth_regime": r.get("ground_truth_daily", {}).get("regime", "UNKNOWN"),
            "ground_truth_atr_pct": r.get("ground_truth_daily", {}).get("atr_pct"),
        })
    return results


def load_calibration() -> list[dict[str, Any]]:
    with open(CALIBRATION_PATH) as f:
        data = json.load(f)
    results = []
    for r in data.get("checkpoints", []):
        pred = r.get("kronos_atr_daily_equiv")
        if pred is not None:
            results.append({
                "timestamp": r["timestamp"],
                "source_dataset": "calibration_real_ticks",
                "hour_utc": r.get("hour_utc"),
                "date": r["timestamp"][:10],
                "predicted_atr_pct_daily_equiv": pred,
                "realized_regime_1h": r.get("realized_regime"),
                "realized_atr_daily_equiv_1h": r.get("realized_atr_daily_equiv"),
                "realized_daily_regime": None,
                "realized_daily_atr_pct": None,
                "ground_truth_regime": None,
                "ground_truth_atr_pct": None,
                "volatility_bucket": r.get("volatility_bucket"),
            })
    return results


def load_chunk_b() -> list[dict[str, Any]]:
    with open(CHUNK_B_PATH) as f:
        data = json.load(f)
    results = []
    for r in data.get("detailed_results", []):
        pred = r.get("kronos_atr_daily_equiv")
        if pred is None:
            pred = r.get("heuristic_atr_daily_equiv")
        realized_regime = r.get("realized_regime")
        if pred is not None:
            results.append({
                "timestamp": r["timestamp"],
                "source_dataset": "chunk_b_real_ticks",
                "hour_utc": r.get("hour_utc"),
                "date": r["timestamp"][:10],
                "predicted_atr_pct_daily_equiv": pred,
                "realized_regime_1h": realized_regime,
                "realized_atr_daily_equiv_1h": r.get("realized_atr_pct_daily_equiv"),
                "realized_daily_regime": None,
                "realized_daily_atr_pct": None,
                "ground_truth_regime": None,
                "ground_truth_atr_pct": None,
                "volatility_bucket": r.get("volatility_bucket"),
            })
    return results


def combine_datasets(
    spotcheck: list[dict], calibration: list[dict], chunk_b: list[dict],
) -> list[dict[str, Any]]:
    """Combine all datasets, deduplicating by timestamp.

    Priority: chunk_b > calibration > spotcheck (most recent run wins).
    """
    by_ts: dict[str, dict[str, Any]] = {}
    for r in spotcheck:
        by_ts[r["timestamp"]] = r
    for r in calibration:
        by_ts[r["timestamp"]] = r
    for r in chunk_b:
        by_ts[r["timestamp"]] = r
    return list(by_ts.values())


# ---------------------------------------------------------------------------
# Grid search
# ---------------------------------------------------------------------------

def grid_search_thresholds(
    rows: list[dict[str, Any]],
    pred_key: str,
    label_key: str,
    old_low: float,
    old_high: float,
    min_gap: float = 0.01,
) -> dict[str, Any]:
    """Grid-search LOW/HIGH thresholds to maximize match rate.

    Args:
        rows: Checkpoint dicts
        pred_key: Key for predicted ATR% value
        label_key: Key for reference regime label
        old_low, old_high: Current thresholds (for comparison)
        min_gap: Minimum HIGH - LOW gap to keep MED regime meaningful
    """
    valid = [r for r in rows
             if r.get(pred_key) is not None
             and r.get(label_key) in ("HIGH", "MED", "LOW")]
    if not valid:
        return {"error": "No valid data", "valid_samples": 0}

    preds = np.array([float(r[pred_key]) for r in valid])
    labels = [r[label_key] for r in valid]
    regime_order = {"LOW": 0, "MED": 1, "HIGH": 2}
    label_ranks = np.array([regime_order[l] for l in labels])

    # Old thresholds
    old_ranks = classify_arr(preds, old_low, old_high)
    old_matches = int(np.sum(old_ranks == label_ranks))
    old_rate = old_matches / len(valid)

    # Grid search with min_gap constraint
    best_low, best_high = old_low, old_high
    best_rate = old_rate
    best_matches = old_matches
    best_distance = float("inf")
    sweep_results = []

    high_range = np.arange(0.020, 0.160, 0.005)
    low_range = np.arange(0.005, 0.060, 0.005)

    for h in high_range:
        for l in low_range:
            if h - l < min_gap:
                continue
            ranks = classify_arr(preds, l, h)
            matches = int(np.sum(ranks == label_ranks))
            rate = matches / len(valid)
            distance = abs(float(l) - old_low) + abs(float(h) - old_high)
            sweep_results.append({
                "high": round(float(h), 3),
                "low": round(float(l), 3),
                "match_rate": round(rate, 4),
                "matches": matches,
            })
            if rate > best_rate or (rate == best_rate and distance < best_distance):
                best_rate = rate
                best_low = float(l)
                best_high = float(h)
                best_matches = matches
                best_distance = distance

    return {
        "valid_samples": len(valid),
        "old_thresholds": {
            "low": old_low,
            "high": old_high,
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
        "min_gap_enforced": min_gap,
        "sweep_results_top10": sorted(sweep_results, key=lambda x: -x["match_rate"])[:10],
        "sweep_total_combinations": len(sweep_results),
    }


def analyze_distributions(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Analyze predicted ATR% distributions by reference regime."""
    valid = [r for r in rows if r.get("predicted_atr_pct_daily_equiv") is not None]

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

    # By realized 1h regime
    by_1h: dict[str, list[float]] = {"HIGH": [], "MED": [], "LOW": []}
    by_gt: dict[str, list[float]] = {"HIGH": [], "MED": [], "LOW": [], "UNKNOWN": []}
    by_realized_daily: dict[str, list[float]] = {"HIGH": [], "MED": [], "LOW": [], "UNKNOWN": []}

    for r in valid:
        pred = r["predicted_atr_pct_daily_equiv"]
        for key, bucket in [("realized_regime_1h", by_1h),
                            ("ground_truth_regime", by_gt),
                            ("realized_daily_regime", by_realized_daily)]:
            val = r.get(key, "UNKNOWN")
            if val is None:
                val = "UNKNOWN"
            if val in bucket:
                bucket[val].append(pred)

    # Forecast distributions under different threshold sets
    by_old: dict[str, list[float]] = {"HIGH": [], "MED": [], "LOW": []}
    by_current: dict[str, list[float]] = {"HIGH": [], "MED": [], "LOW": []}

    for r in valid:
        pred = r["predicted_atr_pct_daily_equiv"]
        by_old[classify_atr(pred, OLD_LOW_THRESHOLD, OLD_HIGH_THRESHOLD)].append(pred)
        by_current[classify_atr(pred, CURRENT_LOW_THRESHOLD, CURRENT_HIGH_THRESHOLD)].append(pred)

    return {
        "predicted_atr_by_realized_1h_regime": {
            k: dist_summary(v) for k, v in by_1h.items()
        },
        "predicted_atr_by_ground_truth_daily_regime": {
            k: dist_summary(v) for k, v in by_gt.items() if k != "UNKNOWN"
        },
        "predicted_atr_by_realized_daily_regime": {
            k: dist_summary(v) for k, v in by_realized_daily.items() if k != "UNKNOWN"
        },
        "forecast_distribution_old_thresholds": {
            k: len(v) for k, v in by_old.items()
        },
        "forecast_distribution_current_thresholds": {
            k: len(v) for k, v in by_current.items()
        },
        "realized_1h_regime_distribution": {
            k: len(v) for k, v in by_1h.items()
        },
        "ground_truth_regime_distribution": {
            k: len(v) for k, v in by_gt.items() if k != "UNKNOWN"
        },
        "realized_daily_regime_distribution": {
            k: len(v) for k, v in by_realized_daily.items() if k != "UNKNOWN"
        },
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print("=" * 80)
    print("CHUNK B FINAL: Combined Kronos Calibration")
    print("=" * 80)
    print()

    # --- Load datasets ---
    print("Loading datasets...")
    spotcheck = load_spotcheck()
    calibration = load_calibration()
    chunk_b = load_chunk_b()

    print(f"  spotcheck (LSE candles):  {len(spotcheck)} checkpoints")
    print(f"  calibration (real ticks): {len(calibration)} checkpoints")
    print(f"  chunk_b (real ticks, NEW): {len(chunk_b)} checkpoints")
    print()

    # --- Combine & deduplicate ---
    combined = combine_datasets(spotcheck, calibration, chunk_b)
    print(f"Combined unique checkpoints: {len(combined)}")

    ds_counts = Counter(r.get("source_dataset") for r in combined)
    print(f"  By source: {dict(ds_counts)}")

    hours = sorted(set(r.get("hour_utc") for r in combined if r.get("hour_utc") is not None))
    print(f"  Hours UTC: {hours}")

    dates = sorted(set(r["date"] for r in combined))
    print(f"  Dates: {dates[0]} -> {dates[-1]} ({len(dates)} unique dates)")
    print()

    # --- Regime distributions ---
    print("=" * 60)
    print("REGIME DISTRIBUTIONS")
    print("=" * 60)
    realized_1h = Counter(r.get("realized_regime_1h") for r in combined
                          if r.get("realized_regime_1h") in ("HIGH", "MED", "LOW"))
    gt_dist = Counter(r.get("ground_truth_regime") for r in combined
                      if r.get("ground_truth_regime") in ("HIGH", "MED", "LOW"))
    daily_reg = Counter(r.get("realized_daily_regime") for r in combined
                        if r.get("realized_daily_regime") in ("HIGH", "MED", "LOW"))

    print(f"Realized 1h regime: {dict(realized_1h)}")
    print(f"Ground-truth daily regime: {dict(gt_dist)}")
    print(f"Realized daily regime: {dict(daily_reg)}")
    print()

    # --- Distribution analysis ---
    print("=" * 60)
    print("PREDICTED ATR% DISTRIBUTIONS")
    print("=" * 60)
    dist = analyze_distributions(combined)
    valid = [r for r in combined if r.get("predicted_atr_pct_daily_equiv") is not None]
    preds = [r["predicted_atr_pct_daily_equiv"] for r in valid]
    print(f"Valid predictions: {len(valid)}/{len(combined)}")
    print(f"  Mean: {np.mean(preds):.6f}  Median: {np.median(preds):.6f}  "
          f"Std: {np.std(preds):.6f}  Range: [{min(preds):.6f}, {max(preds):.6f}]")
    print()

    for label_name, dist_key in [
        ("realized 1h regime", "predicted_atr_by_realized_1h_regime"),
        ("ground-truth daily regime", "predicted_atr_by_ground_truth_daily_regime"),
    ]:
        print(f"Predicted ATR% by {label_name}:")
        for regime in ("HIGH", "MED", "LOW"):
            d = dist[dist_key][regime]
            if d["count"] > 0:
                print(f"  {regime}: n={d['count']}, mean={d['mean']}, median={d['median']}, "
                      f"range=[{d['min']}, {d['max']}], std={d['std']}")
            else:
                print(f"  {regime}: n=0")
    print()

    print(f"Forecast distribution (OLD thresholds {OLD_LOW_THRESHOLD}/{OLD_HIGH_THRESHOLD}): "
          f"{dist['forecast_distribution_old_thresholds']}")
    print(f"Forecast distribution (CURRENT thresholds {CURRENT_LOW_THRESHOLD}/{CURRENT_HIGH_THRESHOLD}): "
          f"{dist['forecast_distribution_current_thresholds']}")
    print()

    # --- Threshold grid search ---
    print("=" * 60)
    print("THRESHOLD GRID SEARCH")
    print("=" * 60)
    print()

    # Analysis A: vs realized 1h regime (all 64 checkpoints)
    print("--- A: vs realized 1h regime (all checkpoints) ---")
    ts_1h = grid_search_thresholds(
        combined, "predicted_atr_pct_daily_equiv", "realized_regime_1h",
        OLD_LOW_THRESHOLD, OLD_HIGH_THRESHOLD, min_gap=0.01,
    )
    print(f"  Valid: {ts_1h['valid_samples']}")
    print(f"  OLD ({OLD_LOW_THRESHOLD}/{OLD_HIGH_THRESHOLD}): {ts_1h['old_thresholds']['match_rate']} "
          f"({ts_1h['old_thresholds']['matches']}/{ts_1h['valid_samples']})")
    print(f"  NEW ({ts_1h['new_thresholds']['low']}/{ts_1h['new_thresholds']['high']}): "
          f"{ts_1h['new_thresholds']['match_rate']} "
          f"({ts_1h['new_thresholds']['matches']}/{ts_1h['valid_samples']})")
    print(f"  Improvement: {ts_1h['match_rate_improvement']:+.4f}")
    print(f"  Top 5: {ts_1h['sweep_results_top10'][:5]}")
    print()

    # Analysis B: vs ground-truth daily regime (spotcheck only, n=20)
    spotcheck_valid = [r for r in combined
                       if r.get("source_dataset") == "spotcheck_real_LSE"
                       or r.get("ground_truth_regime") in ("HIGH", "MED", "LOW")]
    spotcheck_gt_valid = [r for r in combined
                          if r.get("ground_truth_regime") in ("HIGH", "MED", "LOW")]
    print(f"--- B: vs ground-truth daily regime (spotcheck subset, n={len(spotcheck_gt_valid)}) ---")
    ts_gt = grid_search_thresholds(
        spotcheck_gt_valid, "predicted_atr_pct_daily_equiv", "ground_truth_regime",
        OLD_LOW_THRESHOLD, OLD_HIGH_THRESHOLD, min_gap=0.01,
    )
    print(f"  Valid: {ts_gt['valid_samples']}")
    print(f"  OLD ({OLD_LOW_THRESHOLD}/{OLD_HIGH_THRESHOLD}): {ts_gt['old_thresholds']['match_rate']} "
          f"({ts_gt['old_thresholds']['matches']}/{ts_gt['valid_samples']})")
    print(f"  NEW ({ts_gt['new_thresholds']['low']}/{ts_gt['new_thresholds']['high']}): "
          f"{ts_gt['new_thresholds']['match_rate']} "
          f"({ts_gt['new_thresholds']['matches']}/{ts_gt['valid_samples']})")
    print(f"  Improvement: {ts_gt['match_rate_improvement']:+.4f}")
    print(f"  Top 5: {ts_gt['sweep_results_top10'][:5]}")
    print()

    # Analysis C: vs realized daily regime (spotcheck only, n=20)
    spotcheck_daily_valid = [r for r in combined
                             if r.get("realized_daily_regime") in ("HIGH", "MED", "LOW")]
    print(f"--- C: vs realized daily regime (spotcheck subset, n={len(spotcheck_daily_valid)}) ---")
    ts_daily = grid_search_thresholds(
        spotcheck_daily_valid, "predicted_atr_pct_daily_equiv", "realized_daily_regime",
        OLD_LOW_THRESHOLD, OLD_HIGH_THRESHOLD, min_gap=0.01,
    )
    print(f"  Valid: {ts_daily['valid_samples']}")
    print(f"  OLD ({OLD_LOW_THRESHOLD}/{OLD_HIGH_THRESHOLD}): {ts_daily['old_thresholds']['match_rate']} "
          f"({ts_daily['old_thresholds']['matches']}/{ts_daily['valid_samples']})")
    print(f"  NEW ({ts_daily['new_thresholds']['low']}/{ts_daily['new_thresholds']['high']}): "
          f"{ts_daily['new_thresholds']['match_rate']} "
          f"({ts_daily['new_thresholds']['matches']}/{ts_daily['valid_samples']})")
    print(f"  Improvement: {ts_daily['match_rate_improvement']:+.4f}")
    print(f"  Top 5: {ts_daily['sweep_results_top10'][:5]}")
    print()

    # --- Boundary analysis ---
    print("=" * 60)
    print("BOUNDARY ANALYSIS")
    print("=" * 60)
    realized_data = dist["predicted_atr_by_realized_1h_regime"]
    for pair in [("LOW", "MED"), ("MED", "HIGH")]:
        low_d = realized_data.get(pair[0], {"count": 0})
        high_d = realized_data.get(pair[1], {"count": 0})
        if low_d.get("count", 0) > 0 and high_d.get("count", 0) > 0:
            low_max = low_d.get("max", 0)
            high_min = high_d.get("min", 1)
            if high_min > low_max:
                print(f"  {pair[0]} -> {pair[1]}: GAP (low_max={low_max}, high_min={high_min})")
            else:
                print(f"  {pair[0]} -> {pair[1]}: OVERLAP (overlap={round(low_max - high_min, 4)})")
                # Use mean midpoint as soft boundary
                low_mean = low_d.get("mean", 0)
                high_mean = high_d.get("mean", 1)
                if high_mean > low_mean:
                    print(f"    Mean midpoint boundary: {round((low_mean + high_mean)/2, 4)}")
        else:
            print(f"  {pair[0]} -> {pair[1]}: insufficient data "
                  f"({low_d.get('count',0)}, {high_d.get('count',0)})")
    print()

    # --- Per-dataset match rates with OLD and TUNED thresholds ---
    print("=" * 60)
    print("PER-DATASET MATCH RATES")
    print("=" * 60)

    # Use the best tuned thresholds from ground-truth analysis (most meaningful)
    tuned_low = ts_gt["new_thresholds"]["low"] if ts_gt["new_thresholds"]["low"] else 0.05
    tuned_high = ts_gt["new_thresholds"]["high"] if ts_gt["new_thresholds"]["high"] else 0.10

    for ds in ("spotcheck_real_LSE", "calibration_real_ticks", "chunk_b_real_ticks"):
        ds_rows = [r for r in combined if r.get("source_dataset") == ds]
        ds_valid = [r for r in ds_rows
                    if r.get("predicted_atr_pct_daily_equiv") is not None
                    and r.get("realized_regime_1h") in ("HIGH", "MED", "LOW")]
        if not ds_valid:
            continue
        old_m = sum(1 for r in ds_valid
                    if classify_atr(r["predicted_atr_pct_daily_equiv"], OLD_LOW_THRESHOLD, OLD_HIGH_THRESHOLD)
                    == r["realized_regime_1h"])
        cur_m = sum(1 for r in ds_valid
                    if classify_atr(r["predicted_atr_pct_daily_equiv"], CURRENT_LOW_THRESHOLD, CURRENT_HIGH_THRESHOLD)
                    == r["realized_regime_1h"])
        new_m = sum(1 for r in ds_valid
                    if classify_atr(r["predicted_atr_pct_daily_equiv"], tuned_low, tuned_high)
                    == r["realized_regime_1h"])
        print(f"  {ds:30s}: {len(ds_valid)} valid")
        print(f"    OLD ({OLD_LOW_THRESHOLD}/{OLD_HIGH_THRESHOLD}): {old_m}/{len(ds_valid)} = {old_m/len(ds_valid):.4f}")
        print(f"    CURRENT ({CURRENT_LOW_THRESHOLD}/{CURRENT_HIGH_THRESHOLD}): {cur_m}/{len(ds_valid)} = {cur_m/len(ds_valid):.4f}")
        print(f"    TUNED ({tuned_low}/{tuned_high}): {new_m}/{len(ds_valid)} = {new_m/len(ds_valid):.4f}")
    print()

    # --- Final summary ---
    print("=" * 80)
    print("FINAL SUMMARY")
    print("=" * 80)
    print()
    print(f"Combined validation set: {len(combined)} unique checkpoints")
    print(f"  - 20 from real LSE 1m candles (Jan-Jul 2026, with ground-truth daily regimes)")
    print(f"  - 44 from real BTC ticks (Jul 26-30)")
    print(f"  (13 overlapping timestamps deduplicated)")
    print(f"  - Span: {dates[0]} to {dates[-1]} ({len(dates)} dates)")
    print(f"  - Hours UTC: {hours}")
    print()
    print(f"OLD thresholds (LOW={OLD_LOW_THRESHOLD}, HIGH={OLD_HIGH_THRESHOLD}):")
    print(f"  vs realized 1h:     {ts_1h['old_thresholds']['match_rate']} ({ts_1h['old_thresholds']['matches']}/{ts_1h['valid_samples']})")
    print(f"  vs GT daily:         {ts_gt['old_thresholds']['match_rate']} ({ts_gt['old_thresholds']['matches']}/{ts_gt['valid_samples']})")
    print(f"  vs realized daily:   {ts_daily['old_thresholds']['match_rate']} ({ts_daily['old_thresholds']['matches']}/{ts_daily['valid_samples']})")
    print()
    print(f"TUNED thresholds (LOW={tuned_low}, HIGH={tuned_high}):")
    print(f"  vs realized 1h:     {ts_1h['new_thresholds']['match_rate']} ({ts_1h['new_thresholds']['matches']}/{ts_1h['valid_samples']})  [improvement: {ts_1h['match_rate_improvement']:+.4f}]")
    print(f"  vs GT daily:         {ts_gt['new_thresholds']['match_rate']} ({ts_gt['new_thresholds']['matches']}/{ts_gt['valid_samples']})  [improvement: {ts_gt['match_rate_improvement']:+.4f}]")
    print(f"  vs realized daily:   {ts_daily['new_thresholds']['match_rate']} ({ts_daily['new_thresholds']['matches']}/{ts_daily['valid_samples']})  [improvement: {ts_daily['match_rate_improvement']:+.4f}]")
    print()

    # --- Write output ---
    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "script": "scripts/chunk_b_final_calibration.py",
        "task": "Chunk B: Kronos calibration — combined final report",
        "configuration": {
            "old_low_threshold": OLD_LOW_THRESHOLD,
            "old_high_threshold": OLD_HIGH_THRESHOLD,
            "current_low_threshold": CURRENT_LOW_THRESHOLD,
            "current_high_threshold": CURRENT_HIGH_THRESHOLD,
            "at_r_time_scaling_factor": math.sqrt(1440),
            "at_r_scaling_note": (
                "1-minute ATR% is scaled to daily-equivalent via sqrt(1440)="
                f"{math.sqrt(1440):.4f} before applying thresholds."
            ),
            "kronos_model": "state/kronos/kronos-small (NeoQuasar/Kronos-small, 24.7M params)",
            "model_swap": False,
        },
        "datasets": {
            "spotcheck_real_LSE": {
                "file": "kronos_spotcheck.json",
                "checkpoints": len(spotcheck),
                "data_source": "Real LSE 1m BTC/USD candles (Jan-Jul 2026)",
                "has_ground_truth": True,
            },
            "calibration_real_ticks": {
                "file": "kronos_threshold_calibration.json",
                "checkpoints": len(calibration),
                "data_source": "Real BTC/USD ticks (Jul 26-30)",
            },
            "chunk_b_real_ticks": {
                "file": "kronos_calibration_chunk_b.json",
                "checkpoints": len(chunk_b),
                "data_source": "Real BTC/USD ticks (Jul 26-30, NEW)",
            },
        },
        "combined_summary": {
            "total_unique_checkpoints": len(combined),
            "source_dataset_counts": dict(ds_counts),
            "dates_covered": dates,
            "hours_utc_covered": [int(h) for h in hours],
            "realized_1h_regime_distribution": dict(realized_1h),
            "ground_truth_regime_distribution": dict(gt_dist),
            "realized_daily_regime_distribution": dict(daily_reg),
            "forecast_distribution_old_thresholds": dist["forecast_distribution_old_thresholds"],
            "forecast_distribution_current_thresholds": dist["forecast_distribution_current_thresholds"],
        },
        "threshold_analysis_1h_realized": ts_1h,
        "threshold_analysis_gt_daily": ts_gt,
        "threshold_analysis_daily_realized": ts_daily,
        "regime_distributions": dist,
        "combined_checkpoints": combined,
    }

    out_path = Path(OUTPUT_PATH)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(output, indent=2, default=str) + "\n")
    print(f"Results written to {out_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
