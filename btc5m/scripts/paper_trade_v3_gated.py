#!/usr/bin/env python3
"""paper_trade_v3_gated.py — offline replay harness for the persisted
TwoStagePredictionModel (variant C from scripts/fill_and_test_arb_v3.py).

Standalone only. Does NOT touch any real execution path. Loads the pickled
two-stage model, rebuilds features/target/partitions EXACTLY as the training
pipeline (by importing fill_and_test_arb_v3's functions — no reimplementation),
then for each requested holdout:
  - predict_proba_proxy(part) -> (n_rows, 3) joint probs
  - confidence = max of joint probs per row
  - gate at --threshold (max prob >= threshold)
  - fire on gated rows; log JSONL with the fields required by the test suite
  - compute and print summary

The accuracy-over-all-rows reproduction guarantee (vs lightgbm_training_metrics_v3.json
variants.C_two_stage.metrics.<holdout>.accuracy_pct) is asserted by the test suite
within 0.5pp — this confirms feature engineering was reused exactly.

Usage:
    python scripts/paper_trade_v3_gated.py --threshold 0.7
    python scripts/paper_trade_v3_gated.py --no-write --threshold 0.7
    python scripts/paper_trade_v3_gated.py --holdouts holdout_a,holdout_c --threshold 0.8
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import joblib
import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

# Reuse the training pipeline's feature engineering EXACTLY. No reimplementation.
# This is the load-bearing pickaxe for the holdout-reproducibility invariant.
from scripts.fill_and_test_arb_v3 import (  # noqa: E402
    CLASS_NAMES,
    FEATURE_COLUMNS,
    HORIZON_MINUTES,
    TARGET_COLUMN,
    calculate_forward_volatility,
    calculate_technical_indicators,
    label_from_training_quantiles,
    load_binance_bookdepth,
    load_binance_oi_longshort,
    load_cross_asset_daily,
    load_fear_greed_index,
    load_funding_rate,
    load_high_macro_events,
    merge_external_signals,
    split_time_disjoint,
)

UTC = timezone.utc

HOLDOUT_NAMES = ("holdout_a", "holdout_b", "holdout_c")


# ---------------------------------------------------------------------------
# Build the eligible feature frame + labelled partitions exactly like the
# training pipeline (fill_and_test_arb_v3.run_pipeline lines 561-595).
# ---------------------------------------------------------------------------

def build_eligible_frame(
    candles_path: Path,
    macro_path: Path,
    fear_greed_path: Optional[Path] = None,
    funding_rate_path: Optional[Path] = None,
    bookdepth_path: Optional[Path] = None,
    oi_longshort_path: Optional[Path] = None,
    cross_asset_path: Optional[Path] = None,
    restrict_feature_cols: Optional[List[str]] = None,
) -> tuple[pd.DataFrame, List[str]]:
    """Return (eligible_df, feature_cols) post-NaN-drop, identical to run_pipeline.

    ``restrict_feature_cols`` narrows both the returned ``feature_cols`` and
    the NaN-drop subset to a specific model's own trained columns (typically
    a loaded model's ``feature_cols``/``feature_name_``) instead of every
    column in the current ``FEATURE_COLUMNS`` global. Without it, any source
    left unmerged (path is ``None``/missing) still gets an all-NaN placeholder
    column from ``merge_external_signals`` — and any source that IS merged
    still carries its own warmup NaN gap (e.g. cross-asset's ~7 days) — either
    of which silently zeroes out or shrinks the eligible frame for a model
    that was never trained on that column in the first place.
    """
    df = pd.read_parquet(candles_path)
    if "timestamp" not in df.columns:
        raise ValueError(f"parquet missing timestamp column; got {list(df.columns)}")
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df = df.drop_duplicates(subset="timestamp").sort_values("timestamp").reset_index(drop=True)
    high_events, _ = load_high_macro_events(macro_path)
    fear_greed_df = load_fear_greed_index(fear_greed_path) if fear_greed_path and fear_greed_path.is_file() else None
    funding_rate_df = load_funding_rate(funding_rate_path) if funding_rate_path and funding_rate_path.is_file() else None
    bookdepth_df = load_binance_bookdepth(bookdepth_path) if bookdepth_path and bookdepth_path.is_file() else None
    oi_longshort_df = load_binance_oi_longshort(oi_longshort_path) if oi_longshort_path and oi_longshort_path.is_file() else None
    cross_asset_df = load_cross_asset_daily(cross_asset_path) if cross_asset_path and cross_asset_path.is_file() else None
    df = calculate_technical_indicators(df, high_events)
    df = merge_external_signals(
        df, fear_greed_df, funding_rate_df, bookdepth_df, oi_longshort_df, cross_asset_df
    )
    df = calculate_forward_volatility(df, HORIZON_MINUTES)
    source_cols = restrict_feature_cols if restrict_feature_cols is not None else FEATURE_COLUMNS
    feature_cols = [c for c in source_cols if c in df.columns]
    df = df.dropna(subset=feature_cols + [TARGET_COLUMN])
    return df, feature_cols


def build_labelled_partitions(
    eligible_df: pd.DataFrame,
    feature_cols: List[str],
) -> tuple[Dict[str, pd.DataFrame], Dict[str, float]]:
    """Time-disjoint split + train-only labels."""
    raw_parts, _ = split_time_disjoint(eligible_df, minimum_rows_per_partition=1000)
    parts, cutoffs = label_from_training_quantiles(raw_parts)
    return parts, cutoffs


# ---------------------------------------------------------------------------
# Predict one holdout + optional abstention gate. Returns a dict with the
# canonical structure consumed by tests and reported to the user.
# ---------------------------------------------------------------------------

def _max_prob_per_row(probs: np.ndarray) -> np.ndarray:
    return probs.max(axis=1)


def predict_holdout(
    candles_path: Path,
    macro_path: Path,
    model_path: Path,
    holdout_name: str,
    threshold: float,
    model: Optional[Any] = None,
    parts: Optional[Dict[str, pd.DataFrame]] = None,
    feature_cols: Optional[List[str]] = None,
    fear_greed_path: Optional[Path] = None,
    funding_rate_path: Optional[Path] = None,
    bookdepth_path: Optional[Path] = None,
    oi_longshort_path: Optional[Path] = None,
    cross_asset_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Run the harness prediction stack on a single holdout.

    Reproduces fill_and_test_arb_v3.train_two_stage_model's evaluation loop
    (lines 533-543) exactly when threshold=0.0 (gate off). Confirms the
    feature-engineering-reuse invariant: accuracy over ALL rows must match
    lightgbm_training_metrics_v3.json within ~0.5pp.
    """
    if holdout_name not in HOLDOUT_NAMES:
        raise ValueError(f"unknown holdout {holdout_name!r}; expected one of {HOLDOUT_NAMES}")

    if parts is None or feature_cols is None or model is None:
        if model is None:
            model = joblib.load(model_path)
        # Restrict to this model's own trained columns so a model saved
        # before a given external signal existed doesn't get its eligible
        # frame emptied (all-NaN placeholder) or shrunk (a merged source's
        # own warmup gap) by a column it never trained on.
        restrict = getattr(model, "feature_cols", None)
        eligible, fc = build_eligible_frame(
            candles_path, macro_path, fear_greed_path, funding_rate_path,
            bookdepth_path, oi_longshort_path, cross_asset_path,
            restrict_feature_cols=restrict,
        )
        parts, _ = build_labelled_partitions(eligible, fc)
        feature_cols = fc

    part = parts[holdout_name]
    if hasattr(part, "reset_index"):
        part = part.reset_index(drop=True)

    joint_probs = model.predict_proba_proxy(part)
    preds = joint_probs.argmax(axis=1).astype(int)
    targets = part["target"].to_numpy().astype(int)

    rows = int(len(part))
    correct_all = int((preds == targets).sum())
    accuracy_pct_all = round(correct_all / rows * 100, 6) if rows else 0.0
    class_counts = {CLASS_NAMES[i]: int((targets == i).sum()) for i in range(3)}

    max_probs = _max_prob_per_row(joint_probs)
    fired_mask = max_probs >= threshold
    fired_rows = int(fired_mask.sum())
    abstained_rows = int((~fired_mask).sum())

    if fired_rows > 0:
        fired_correct = int((preds[fired_mask] == targets[fired_mask]).sum())
        precision_pct = round(fired_correct / fired_rows * 100, 6)
        per_class: Dict[str, Optional[float]] = {}
        for i, name in enumerate(CLASS_NAMES):
            cls_fired = int(((preds == i) & fired_mask).sum())
            if cls_fired == 0:
                per_class[name] = None
                continue
            cls_correct = int(((preds == i) & (targets == i) & fired_mask).sum())
            per_class[name] = round(cls_correct / cls_fired * 100, 6)
    else:
        precision_pct = None
        per_class = {name: None for name in CLASS_NAMES}

    trades: List[Dict[str, Any]] = []
    if fired_rows > 0:
        timestamps = part["timestamp"].to_numpy()
        for pos in np.nonzero(fired_mask)[0]:
            ts_iso = pd.Timestamp(timestamps[int(pos)]).isoformat()
            trades.append({
                "timestamp_iso": ts_iso,
                "predicted_class": CLASS_NAMES[int(preds[int(pos)])],
                "confidence": round(float(max_probs[int(pos)]), 6),
                "actual_class": CLASS_NAMES[int(targets[int(pos)])],
                "correct": bool(preds[int(pos)] == targets[int(pos)]),
                "partition_name": holdout_name,
                "threshold": float(threshold),
            })

    return {
        "holdout_name": holdout_name,
        "rows": rows,
        "fired_rows": fired_rows,
        "abstained_rows": abstained_rows,
        "accuracy_pct": accuracy_pct_all,
        "precision_pct": precision_pct,
        "per_class_precision": per_class,
        "class_counts": class_counts,
        "trades": trades,
    }


# ---------------------------------------------------------------------------
# Summary aggregation
# ---------------------------------------------------------------------------

def summarize_trades(
    trades: List[Dict[str, Any]],
    abstained_by_holdout: Optional[Dict[str, int]] = None,
    rows_by_holdout: Optional[Dict[str, int]] = None,
) -> Dict[str, Any]:
    """Aggregate fired trades into the summary structure asserted by Test 8.

    Keys: total_fired, total_abstained, precision_pct, per_class_precision
    (LOW/MED/HIGH), broken_out_by_holdout dict.
    """
    abstained_by_holdout = abstained_by_holdout or {}
    rows_by_holdout = rows_by_holdout or {}

    per_class_counts = {name: {"fired": 0, "correct": 0} for name in CLASS_NAMES}
    broken_out: Dict[str, Dict[str, Any]] = {}

    holdouts_seen: Dict[str, Dict[str, int]] = {}
    for t in trades:
        h = t["partition_name"]
        pc = t["predicted_class"]
        per_class_counts[pc]["fired"] += 1
        if t["correct"]:
            per_class_counts[pc]["correct"] += 1
        agg = holdouts_seen.setdefault(h, {"fired": 0, "correct": 0, "abstained": 0})
        agg["fired"] += 1
        if t["correct"]:
            agg["correct"] += 1

    total_fired = len(trades)
    total_correct = sum(1 for t in trades if t["correct"])
    total_abstained = sum(abstained_by_holdout.values())

    per_class_precision: Dict[str, Optional[float]] = {}
    for name in CLASS_NAMES:
        c = per_class_counts[name]
        per_class_precision[name] = round(c["correct"] / c["fired"] * 100, 6) if c["fired"] else 0.0

    precision_pct = round(total_correct / total_fired * 100, 6) if total_fired else 0.0

    for h, agg in holdouts_seen.items():
        fired = agg["fired"]
        correct = agg["correct"]
        abstained = abstained_by_holdout.get(h, 0)
        rows = rows_by_holdout.get(h, fired + abstained)
        broken_out[h] = {
            "fired": fired,
            "abstained": abstained,
            "rows": rows,
            "precision_pct": round(correct / fired * 100, 6) if fired else 0.0,
            "accuracy_pct_all_rows": None,
        }
    # Also surface holdouts that abstained everything (fired==0 may not appear in trades)
    for h, abstained in abstained_by_holdout.items():
        if h in broken_out:
            continue
        rows = rows_by_holdout.get(h, abstained)
        broken_out[h] = {
            "fired": 0,
            "abstained": abstained,
            "rows": rows,
            "precision_pct": 0.0,
            "accuracy_pct_all_rows": None,
        }

    return {
        "total_fired": total_fired,
        "total_abstained": total_abstained,
        "precision_pct": precision_pct,
        "per_class_precision": per_class_precision,
        "broken_out_by_holdout": broken_out,
    }


def summarize_holdout_results(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Combine predict_holdout() result dicts into the summary structure."""
    all_trades: List[Dict[str, Any]] = []
    abstained_by_holdout: Dict[str, int] = {}
    rows_by_holdout: Dict[str, int] = {}
    per_holdout_acc: Dict[str, Optional[float]] = {}
    per_holdout_precision: Dict[str, Optional[float]] = {}
    for r in results:
        h = r["holdout_name"]
        all_trades.extend(r["trades"])
        abstained_by_holdout[h] = r["abstained_rows"]
        rows_by_holdout[h] = r["rows"]
        per_holdout_acc[h] = r["accuracy_pct"]
        per_holdout_precision[h] = r["precision_pct"]

    summary = summarize_trades(
        all_trades,
        abstained_by_holdout=abstained_by_holdout,
        rows_by_holdout=rows_by_holdout,
    )
    # Attach per-holdout accuracy-over-all-rows (the reproducibility key).
    for h, bo in summary["broken_out_by_holdout"].items():
        bo["accuracy_pct_all_rows"] = per_holdout_acc.get(h)
        bo["precision_pct"] = per_holdout_precision.get(h)
    summary["per_holdout_accuracy_all_rows"] = per_holdout_acc
    summary["per_holdout_precision"] = per_holdout_precision
    return summary


# ---------------------------------------------------------------------------
# JSONL writer
# ---------------------------------------------------------------------------

def write_trades(trades: List[Dict[str, Any]], out_path: Path) -> int:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as fh:
        for t in trades:
            fh.write(json.dumps(t, sort_keys=True) + "\n")
    return len(trades)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _resolve(p: Path) -> Path:
    return p if p.is_absolute() else (PROJECT_ROOT / p)


def run_cli(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="paper_trade_v3 offline gated harness")
    parser.add_argument("--candles", type=Path,
                        default=Path("state/btc_1m_candles_lse_v3.parquet"))
    parser.add_argument("--model", type=Path,
                        default=Path("state/lightgbm_two_stage_v3.pkl"))
    parser.add_argument("--macro", type=Path,
                        default=Path("state/macro_events.json"))
    parser.add_argument("--fear-greed", type=Path,
                        default=Path("state/fear_greed_index.json"))
    parser.add_argument("--funding-rate", type=Path,
                        default=Path("state/binance_funding_rate_btc.json"))
    parser.add_argument("--bookdepth", type=Path,
                        default=Path("state/binance_bookdepth_btc.parquet"))
    parser.add_argument("--oi-longshort", type=Path,
                        default=Path("state/binance_oi_longshort_btc.parquet"))
    parser.add_argument("--cross-asset", type=Path,
                        default=Path("state/cross_asset_daily.parquet"))
    parser.add_argument("--threshold", type=float, default=0.7)
    parser.add_argument("--holdouts", type=str,
                        default="holdout_a,holdout_b,holdout_c",
                        help="comma-separated subset of holdout_a,holdout_b,holdout_c")
    parser.add_argument("--output", type=Path,
                        default=Path("state/paper_trades_v3.jsonl"))
    parser.add_argument("--no-write", action="store_true",
                        help="print summary only; do not write JSONL")
    args = parser.parse_args(argv)

    requested = [h.strip() for h in args.holdouts.split(",") if h.strip()]
    for h in requested:
        if h not in HOLDOUT_NAMES:
            parser.error(f"unknown holdout {h!r}; choices: {HOLDOUT_NAMES}")

    candles_path = _resolve(args.candles)
    macro_path = _resolve(args.macro)
    model_path = _resolve(args.model)
    output_path = _resolve(args.output)
    fear_greed_path = _resolve(args.fear_greed)
    funding_rate_path = _resolve(args.funding_rate)
    bookdepth_path = _resolve(args.bookdepth)
    oi_longshort_path = _resolve(args.oi_longshort)
    cross_asset_path = _resolve(args.cross_asset)

    print(f"[INFO] candles={candles_path}")
    print(f"[INFO] model={model_path}")
    print(f"[INFO] macro={macro_path}")
    print(f"[INFO] fear_greed={fear_greed_path}  funding_rate={funding_rate_path}")
    print(f"[INFO] bookdepth={bookdepth_path}  oi_longshort={oi_longshort_path}  cross_asset={cross_asset_path}")
    print(f"[INFO] threshold={args.threshold}  holdouts={requested}  output={output_path}")

    print("[INFO] loading persisted TwoStagePredictionModel ...")
    model = joblib.load(model_path)
    print(f"[INFO]   model.feature_cols: {model.feature_cols}")

    print("[INFO] building eligible feature frame (this takes a moment) ...")
    eligible, feature_cols = build_eligible_frame(
        candles_path, macro_path, fear_greed_path, funding_rate_path,
        bookdepth_path, oi_longshort_path, cross_asset_path,
        restrict_feature_cols=model.feature_cols,
    )
    print(f"[INFO] eligible rows after NaN drop: {len(eligible)}  feature cols: {len(feature_cols)}")
    parts, cutoffs = build_labelled_partitions(eligible, feature_cols)
    print(f"[INFO] cutoffs: LOW<{cutoffs['low_to_med_bps']:.3f}  "
          f"MED<{cutoffs['med_to_high_bps']:.3f}  HIGH")
    for h in HOLDOUT_NAMES:
        print(f"[INFO]   {h}: rows={len(parts[h])}")

    assert set(model.feature_cols).issubset(set(feature_cols)), \
        "model expects feature columns not present in the harness feature frame"

    results: List[Dict[str, Any]] = []
    for h in requested:
        print(f"[INFO] predicting {h} at threshold={args.threshold} ...")
        r = predict_holdout(
            candles_path=candles_path, macro_path=macro_path, model_path=model_path,
            holdout_name=h, threshold=args.threshold,
            model=model, parts=parts, feature_cols=feature_cols,
        )
        results.append(r)
        print(
            f"[INFO] {h}: rows={r['rows']} fired={r['fired_rows']} "
            f"abstained={r['abstained_rows']} "
            f"precision={r['precision_pct']}%  acc_all_rows={r['accuracy_pct']}%"
        )
        print(f"[INFO] {h}: class_counts={r['class_counts']} "
              f"per_class_precision={r['per_class_precision']}")

    summary = summarize_holdout_results(results)

    # Print summary
    print()
    print("=" * 70)
    print("PAPER-TRADE v3 GATED SUMMARY")
    print("=" * 70)
    print(f"threshold: {args.threshold}")
    print(f"holdouts:  {requested}")
    print(f"total fired:     {summary['total_fired']}")
    print(f"total abstained: {summary['total_abstained']}")
    print(f"precision (fired): {summary['precision_pct']}%")
    print(f"per-class precision (fired): {summary['per_class_precision']}")
    print("broken down by holdout:")
    for h in requested:
        bo = summary["broken_out_by_holdout"].get(h, {})
        print(
            f"  {h}: fired={bo.get('fired')} "
            f"abstained={bo.get('abstained')} rows={bo.get('rows')} "
            f"precision={bo.get('precision_pct')}% "
            f"acc_all_rows={bo.get('accuracy_pct_all_rows')}%"
        )
    print()
    print("Per-holdout accuracy over ALL rows (reproducibility check vs JSON):")
    metrics_path = PROJECT_ROOT / "state" / "lightgbm_training_metrics_v3.json"
    if metrics_path.exists():
        metrics = json.loads(metrics_path.read_text())
        c_metrics = metrics.get("variants", {}).get("C_two_stage", {}).get("metrics", {})
        for h in requested:
            harness_acc = summary["per_holdout_accuracy_all_rows"].get(h)
            json_acc = c_metrics.get(h, {}).get("accuracy_pct")
            delta = (harness_acc - json_acc) if (harness_acc is not None and json_acc is not None) else None
            flag = "OK" if (delta is not None and abs(delta) <= 0.5) else "DRIFT"
            print(f"  {h}: harness={harness_acc}%  json={json_acc}%  delta={delta}pp  [{flag}]")
    print("=" * 70)

    # Write JSONL unless suppressed
    all_trades = []
    for r in results:
        all_trades.extend(r["trades"])
    if args.no_write:
        print(f"[INFO] --no-write: not writing {output_path} ({len(all_trades)} trades suppressed)")
    else:
        n = write_trades(all_trades, output_path)
        print(f"[SAVED] {output_path}  ({n} rows)")
        # Sibling summary file
        summary_path = output_path.with_suffix(".summary.json")
        summary_doc = {
            "generated_at_utc": datetime.now(UTC).isoformat(),
            "candles_path": str(candles_path),
            "model_path": str(model_path),
            "macro_path": str(macro_path),
            "threshold": args.threshold,
            "holdouts": requested,
            "summary": summary,
        }
        summary_path.write_text(json.dumps(summary_doc, indent=2, sort_keys=True))
        print(f"[SAVED] {summary_path}")

    return 0


if __name__ == "__main__":
    sys.exit(run_cli())
