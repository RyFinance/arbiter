#!/usr/bin/env python3
"""Hyperparameter search for the LightGBM regime classifier — 2026-08-20.

fill_and_test_arb_v3.py's hyperparameters (num_leaves=63, max_depth=5,
min_child_samples=10, learning_rate=0.05) were tuned via search on the
4,783-row dataset (LIGHTGBM_KRONOS_TRAINING_REPORT.md, 2026-08-04) and never
revisited after the 2026-08-20 retrain moved to 845,516 real rows — a
model sized for that much data is very likely underfitting on 175x more.

Selection discipline (per docs/chunk_e_walk_forward_retraining_policy_2026-08-11.md,
"Not defensible: retraining or selecting hyperparameters against the same
holdout repeatedly"): every candidate is scored on the VALIDATION partition
only. Holdouts A/B/C are touched exactly once, at the very end, to report the
winning candidate's real numbers — never used to pick among candidates.

Reuses fill_and_test_arb_v3's data loading, feature engineering, and split/
label functions unchanged (no reimplementation, no risk of silently
diverging from the already-correct, leakage-free methodology).

Usage:
    python scripts/tune_lightgbm_hyperparams.py \
        --candles state/btc_1m_candles_real_2025_2026.parquet
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import lightgbm as lgb
import numpy as np
import pandas as pd
from lightgbm import LGBMClassifier

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from fill_and_test_arb_v3 import (  # noqa: E402
    FEATURE_COLUMNS,
    TARGET_COLUMN,
    _classification_metrics,
    _majority_baseline_metrics,
    calculate_forward_volatility,
    calculate_technical_indicators,
    label_from_training_quantiles,
    load_high_macro_events,
    split_time_disjoint,
)

UTC = timezone.utc
HORIZON_MINUTES = 15

# Baseline is the current production config (num_leaves capped by max_depth=5
# at <=32 effective leaves regardless of the 63 setting — i.e. genuinely
# shallow). Candidates progressively relax depth/leaves/min_child_samples on
# the hypothesis that 845K rows supports far more capacity than 4,783 did,
# while raising min_child_samples alongside depth to control overfitting risk
# on a dataset two orders of magnitude bigger.
CANDIDATES: List[Dict[str, Any]] = [
    {"tag": "baseline_current", "num_leaves": 63, "max_depth": 5, "min_child_samples": 10, "learning_rate": 0.05, "drop_dead_macro_feature": False},
    {"tag": "deeper_moderate", "num_leaves": 127, "max_depth": 7, "min_child_samples": 20, "learning_rate": 0.05, "drop_dead_macro_feature": False},
    {"tag": "deeper_regularized", "num_leaves": 255, "max_depth": 8, "min_child_samples": 100, "learning_rate": 0.05, "drop_dead_macro_feature": False},
    {"tag": "deep_slow_lr", "num_leaves": 255, "max_depth": 8, "min_child_samples": 50, "learning_rate": 0.03, "drop_dead_macro_feature": False},
    {"tag": "unbounded_depth", "num_leaves": 511, "max_depth": -1, "min_child_samples": 100, "learning_rate": 0.03, "drop_dead_macro_feature": False},
    {"tag": "deeper_moderate_no_deadfeat", "num_leaves": 127, "max_depth": 7, "min_child_samples": 20, "learning_rate": 0.05, "drop_dead_macro_feature": True},
]


def build_parts(candles_path: Path, macro_path: Path) -> Dict[str, pd.DataFrame]:
    df = pd.read_parquet(candles_path)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df = df.drop_duplicates(subset="timestamp").sort_values("timestamp").reset_index(drop=True)
    print(f"[INFO] Loaded {len(df)} candles, {df['timestamp'].iloc[0]} to {df['timestamp'].iloc[-1]}")

    high_events, _meta = load_high_macro_events(macro_path)
    df = calculate_technical_indicators(df, high_events)
    df = calculate_forward_volatility(df, HORIZON_MINUTES)
    feature_cols = [c for c in FEATURE_COLUMNS if c in df.columns]
    df = df.dropna(subset=feature_cols + [TARGET_COLUMN])
    print(f"[INFO] Eligible feature rows after NaN drop: {len(df)}")

    raw_parts, partitions = split_time_disjoint(df, minimum_rows_per_partition=1000)
    parts, cutoffs = label_from_training_quantiles(raw_parts)
    print(f"[INFO] Partitions: {[p.name + ':' + str(p.rows) for p in partitions]}")
    print(f"[INFO] Target cutoffs (train only): {cutoffs}")
    return parts


def train_one(parts: Dict[str, pd.DataFrame], candidate: Dict[str, Any]) -> Dict[str, Any]:
    feature_cols = [c for c in FEATURE_COLUMNS if c in parts["train"].columns]
    if candidate["drop_dead_macro_feature"]:
        feature_cols = [c for c in feature_cols if c != "minutes_to_next_high_macro"]

    params = {
        "objective": "multiclass",
        "num_class": 3,
        "boosting_type": "gbdt",
        "metric": ["multi_logloss", "multi_error"],
        "n_estimators": 1000,
        "learning_rate": candidate["learning_rate"],
        "num_leaves": candidate["num_leaves"],
        "max_depth": candidate["max_depth"],
        "min_child_samples": candidate["min_child_samples"],
        "subsample": 0.8,
        "colsample_bytree": 0.8,
        "class_weight": "balanced",
        "random_state": 20260804,
        "n_jobs": -1,
        "verbosity": -1,
    }
    model = LGBMClassifier(**params)
    model.fit(
        parts["train"][feature_cols],
        parts["train"]["target"],
        eval_set=[(parts["validation"][feature_cols], parts["validation"]["target"])],
        eval_metric="multi_logloss",
        callbacks=[lgb.early_stopping(stopping_rounds=50, verbose=False)],
    )

    val_probs = model.predict_proba(parts["validation"][feature_cols], num_iteration=model.best_iteration_)
    val_preds = val_probs.argmax(axis=1)
    val_metrics = _classification_metrics(parts["validation"]["target"], val_preds, val_probs)

    return {
        "candidate": candidate,
        "feature_cols": feature_cols,
        "best_iteration": int(model.best_iteration_ or model.n_estimators),
        "validation_accuracy_pct": val_metrics["accuracy_pct"],
        "validation_macro_f1": val_metrics["macro_f1"],
        "_model": model,
    }


def evaluate_on_holdouts(model, feature_cols: List[str], parts: Dict[str, pd.DataFrame]) -> Dict[str, Any]:
    majority_class = int(
        parts["train"]["target"].value_counts().reindex(range(3), fill_value=0).idxmax()
    )
    out = {}
    for name in ["holdout_a", "holdout_b", "holdout_c"]:
        part = parts[name]
        probs = model.predict_proba(part[feature_cols], num_iteration=model.best_iteration_)
        preds = probs.argmax(axis=1)
        m = _classification_metrics(part["target"], preds, probs)
        mb = _majority_baseline_metrics(part, majority_class)
        m["majority_baseline_pct"] = mb["accuracy_pct"]
        m["vs_majority_pp"] = round(m["accuracy_pct"] - mb["accuracy_pct"], 4)
        out[name] = m
    accs = [out[h]["accuracy_pct"] for h in out]
    out["_mean_pct"] = round(float(np.mean(accs)), 4)
    out["_all_beat_majority"] = all(out[h]["vs_majority_pp"] > 0 for h in ["holdout_a", "holdout_b", "holdout_c"])
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candles", type=Path, default=Path("state/btc_1m_candles_real_2025_2026.parquet"))
    parser.add_argument("--macro", type=Path, default=Path("state/macro_events.json"))
    parser.add_argument("--output", type=Path, default=Path("state/lightgbm_hyperparam_search.json"))
    args = parser.parse_args()

    candles_path = PROJECT_ROOT / args.candles if not args.candles.is_absolute() else args.candles
    macro_path = PROJECT_ROOT / args.macro if not args.macro.is_absolute() else args.macro

    parts = build_parts(candles_path, macro_path)

    results = []
    for candidate in CANDIDATES:
        print(f"\n[TRAIN] {candidate['tag']}: {candidate}")
        r = train_one(parts, candidate)
        print(f"  -> validation_accuracy={r['validation_accuracy_pct']:.4f}% "
              f"macro_f1={r['validation_macro_f1']:.4f} best_iter={r['best_iteration']}")
        results.append(r)

    # Selection: validation accuracy only. Holdouts are not consulted here.
    winner = max(results, key=lambda r: r["validation_accuracy_pct"])
    print(f"\n[SELECTED] {winner['candidate']['tag']} "
          f"(validation_accuracy={winner['validation_accuracy_pct']:.4f}%) "
          "-- chosen by validation, now scoring holdouts once for the honest number")

    holdout_metrics = evaluate_on_holdouts(winner["_model"], winner["feature_cols"], parts)
    print("\n" + "=" * 70)
    print(f"WINNER: {winner['candidate']['tag']}")
    print("=" * 70)
    for h in ["holdout_a", "holdout_b", "holdout_c"]:
        m = holdout_metrics[h]
        print(f"  {h}: accuracy={m['accuracy_pct']:.2f}% macro_f1={m['macro_f1']:.4f} "
              f"vs_majority={m['vs_majority_pp']:+.2f}pp")
    print(f"  mean={holdout_metrics['_mean_pct']:.2f}%  all_beat_majority={holdout_metrics['_all_beat_majority']}")

    artifact = {
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "selection_discipline": "candidates ranked by validation accuracy only; holdouts scored once for the winner",
        "candidates": [
            {k: v for k, v in r.items() if k != "_model"} for r in results
        ],
        "winner_tag": winner["candidate"]["tag"],
        "winner_holdout_metrics": {k: v for k, v in holdout_metrics.items() if not k.startswith("_") or k in ("_mean_pct", "_all_beat_majority")},
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(artifact, indent=2, default=str) + "\n", encoding="utf-8")
    print(f"\n[SAVED] {args.output}")

    if winner["candidate"]["tag"] != "baseline_current":
        import joblib
        model_path = PROJECT_ROOT / "state" / "lightgbm_volatility_model_v5_tuned.pkl"
        joblib.dump(winner["_model"], model_path)
        print(f"[SAVED] {model_path} (feature_cols={winner['feature_cols']})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
