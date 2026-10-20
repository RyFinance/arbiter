#!/usr/bin/env python3
"""BTC direction classifier — Phase 0 of the arb->directional-bot pivot
(2026-08-22, see docs/BTC_DIRECTION_SIGNAL_2026-08-22.md and the approved
plan in this session).

Separate from scripts/fill_and_test_arb_v3.py (the volatility-regime
pipeline) by design — different target, different feature philosophy,
different downstream use. Reuses split_time_disjoint from that script
directly rather than reimplementing the leak-free chronological split.

Context this exists to test honestly: this project already tried a
directional BTC signal once (docs/btc_offline_chronological_evaluation_
2026-08-11.md, docs/btc_offline_sizing_sharpe_2026-08-15.md) using only
price/technical-indicator features (ATR, rolling vol, Bollinger width) and
lost money in every holdout x sizing-policy combination (pooled Sharpe
-0.14 to -0.60). This script tests a genuinely different feature set never
tried there: multi-window momentum/mean-reversion at explicit lookbacks,
plus (optionally) order-flow imbalance/CVD from real Binance tick trades
(scripts/fetch_binance_tick_trades.py) -- not a rerun of the same features
with more columns.

Trains ONE model per timeframe (5min / 60min / 1440min forward direction),
independently -- the vol-regime horizon experiment already found a 6.2pp
accuracy swing from horizon alone, direction is expected to be at least as
sensitive. No model here is wired into any live code path; this is
research/validation only per the approved plan's Phase 0 gate.

PnL is a clearly-labeled PROXY (mirrors the prior failed study's own
methodology for direct comparability): treats a correct/incorrect call as
capturing/losing the underlying's own forward return, net of a flat 20bps
round-trip cost. This is NOT a simulation of Polymarket's actual binary
$0/$1 settlement against real historical order-book prices (which aren't
available for this full window) -- same caveat the prior study already
carried (its own "executable_polymarket_pnl.status=unavailable").

Usage:
    python scripts/fill_and_test_direction_v1.py
    python scripts/fill_and_test_direction_v1.py --horizons 5,60,1440
    python scripts/fill_and_test_direction_v1.py --taker-flow state/binance_taker_flow_1m_btc.parquet
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import joblib
import numpy as np
import pandas as pd
from lightgbm import LGBMClassifier
from sklearn.inspection import permutation_importance
from sklearn.metrics import accuracy_score, roc_auc_score

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.fill_and_test_arb_v3 import split_time_disjoint  # noqa: E402

ROUND_TRIP_COST_BPS = 20.0  # matches docs/btc_offline_sizing_sharpe_2026-08-15.md for comparability
PERMUTATION_IMPORTANCE_REPEATS = 20
CONFIDENCE_THRESHOLDS = [0.52, 0.55, 0.60, 0.65]

MOMENTUM_LOOKBACKS_MIN = [1, 5, 15, 60, 240, 1440]

BASE_FEATURE_COLUMNS = (
    [f"mom_{l}m_bps" for l in MOMENTUM_LOOKBACKS_MIN]
    + [f"mrev_{l}m_z" for l in (5, 15, 60)]
    + ["hour_sin", "hour_cos", "dayofweek_sin", "dayofweek_cos", "log_volume", "rsi_14"]
)
ORDER_FLOW_COLUMNS = [
    "order_flow_imbalance_1m",
    "order_flow_imbalance_5m",
    "order_flow_imbalance_15m",
]


def load_candles(path: Path) -> pd.DataFrame:
    df = pd.read_parquet(path)
    if "timestamp" not in df.columns:
        raise ValueError(f"parquet missing timestamp column; got {list(df.columns)}")
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df = df.drop_duplicates(subset="timestamp").sort_values("timestamp").reset_index(drop=True)
    return df


def calculate_momentum_features(df: pd.DataFrame) -> pd.DataFrame:
    """Multi-window momentum + mean-reversion features, time-indexed so a
    data gap can't blend stale pre-gap prices into a post-gap value (same
    discipline as fill_and_test_arb_v3.py's rolling-window fix)."""
    df = df.copy().sort_values("timestamp").reset_index(drop=True)
    close_ts = df["close"].set_axis(df["timestamp"])

    for l in MOMENTUM_LOOKBACKS_MIN:
        shifted = close_ts.shift(freq=pd.Timedelta(minutes=l)).reindex(close_ts.index, method=None)
        # shift(freq=...) reindexes by time, not row count -- exact lookback
        # even across gaps; falls back to NaN where no candle exists at t-l.
        mom = (close_ts.to_numpy() - shifted.to_numpy()) / shifted.to_numpy() * 10000.0
        df[f"mom_{l}m_bps"] = mom

    log_return = np.log(df["close"]).diff()
    lr_ts = log_return.set_axis(df["timestamp"])
    for l in (5, 15, 60):
        roll_mean = lr_ts.rolling(f"{l}min", min_periods=max(2, l // 2)).mean()
        roll_std = lr_ts.rolling(f"{l}min", min_periods=max(2, l // 2)).std()
        z = (log_return.to_numpy() - roll_mean.to_numpy()) / roll_std.to_numpy()
        df[f"mrev_{l}m_z"] = z

    df["log_volume"] = np.log1p(df["volume"])
    ts = df["timestamp"]
    df["hour_sin"] = np.sin(2 * np.pi * ts.dt.hour / 24.0)
    df["hour_cos"] = np.cos(2 * np.pi * ts.dt.hour / 24.0)
    df["dayofweek_sin"] = np.sin(2 * np.pi * ts.dt.dayofweek / 7.0)
    df["dayofweek_cos"] = np.cos(2 * np.pi * ts.dt.dayofweek / 7.0)

    delta = df["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    gain_ts = gain.set_axis(df["timestamp"])
    loss_ts = loss.set_axis(df["timestamp"])
    avg_gain = gain_ts.rolling("14min", min_periods=14).mean()
    avg_loss = loss_ts.rolling("14min", min_periods=14).mean()
    rs = avg_gain.to_numpy() / avg_loss.to_numpy()
    df["rsi_14"] = 100 - (100 / (1 + rs))

    return df


def load_taker_flow(path: Path) -> Optional[pd.DataFrame]:
    if not path.is_file():
        return None
    df = pd.read_parquet(path)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df = df.sort_values("timestamp").reset_index(drop=True)
    return df


def merge_order_flow(df: pd.DataFrame, flow_df: Optional[pd.DataFrame]) -> pd.DataFrame:
    df = df.sort_values("timestamp").reset_index(drop=True)
    if flow_df is None or flow_df.empty:
        for col in ORDER_FLOW_COLUMNS:
            df[col] = np.nan
        return df
    # merge_asof requires identical datetime units; the candle cache and the
    # taker-flow parquet were built at different times with different
    # underlying precisions (us vs ms), so normalize both explicitly.
    flow_df = flow_df.copy()
    flow_df["timestamp"] = flow_df["timestamp"].astype("datetime64[us, UTC]")
    df = df.copy()
    df["timestamp"] = df["timestamp"].astype("datetime64[us, UTC]")
    merged = pd.merge_asof(
        df, flow_df, left_on="timestamp", right_on="timestamp", direction="backward"
    )
    buy = merged["taker_buy_quote_vol"]
    sell = merged["taker_sell_quote_vol"]
    buy_ts = buy.set_axis(merged["timestamp"])
    sell_ts = sell.set_axis(merged["timestamp"])
    for l in (1, 5, 15):
        b = buy_ts.rolling(f"{l}min", min_periods=1).sum()
        s = sell_ts.rolling(f"{l}min", min_periods=1).sum()
        total = b.to_numpy() + s.to_numpy()
        with np.errstate(divide="ignore", invalid="ignore"):
            imbalance = (b.to_numpy() - s.to_numpy()) / total
        df[f"order_flow_imbalance_{l}m"] = imbalance
    return df


def build_target(df: pd.DataFrame, horizon_min: int) -> pd.DataFrame:
    df = df.copy()
    close_ts = df["close"].set_axis(df["timestamp"])
    future = close_ts.shift(freq=pd.Timedelta(minutes=-horizon_min)).reindex(close_ts.index)
    forward_return_bps = (future.to_numpy() - close_ts.to_numpy()) / close_ts.to_numpy() * 10000.0
    df["forward_return_bps"] = forward_return_bps
    df["target"] = (df["forward_return_bps"] > 0).astype("Int64")
    return df


def _proxy_pnl_at_threshold(
    proba_up: np.ndarray, forward_return_bps: np.ndarray, target: np.ndarray, threshold: float
) -> Dict[str, Any]:
    fired_up = proba_up >= threshold
    fired_down = proba_up <= (1.0 - threshold)
    fired = fired_up | fired_down
    n_fired = int(fired.sum())
    if n_fired == 0:
        return {
            "fired_pct": 0.0, "fired_rows": 0, "accuracy_pct": None,
            "mean_pnl_bps": None, "sharpe_per_trade": None, "total_pnl_bps": None,
        }
    pred = np.where(fired_up, 1, 0)
    correct = (pred[fired] == target[fired])
    accuracy_pct = round(float(correct.mean()) * 100, 4)

    signed_pnl = np.where(fired_up, forward_return_bps, -forward_return_bps) - ROUND_TRIP_COST_BPS
    trade_pnl = signed_pnl[fired]
    mean_pnl = float(np.mean(trade_pnl))
    std_pnl = float(np.std(trade_pnl, ddof=1)) if n_fired > 1 else float("nan")
    sharpe = mean_pnl / std_pnl if std_pnl and np.isfinite(std_pnl) and std_pnl > 0 else None

    return {
        "fired_pct": round(n_fired / len(proba_up) * 100, 4),
        "fired_rows": n_fired,
        "accuracy_pct": accuracy_pct,
        "mean_pnl_bps": round(mean_pnl, 4),
        "sharpe_per_trade": round(sharpe, 4) if sharpe is not None else None,
        "total_pnl_bps": round(float(np.sum(trade_pnl)), 4),
    }


def train_and_evaluate_horizon(
    df: pd.DataFrame, horizon_min: int, feature_cols: List[str]
) -> Dict[str, Any]:
    labelled = build_target(df, horizon_min)
    eligible = labelled.dropna(subset=feature_cols + ["target", "forward_return_bps"]).copy()
    eligible["target"] = eligible["target"].astype(int)
    print(f"[INFO] horizon={horizon_min}m: {len(eligible)} eligible rows "
          f"(from {len(df)} total)")

    parts, partitions = split_time_disjoint(eligible, minimum_rows_per_partition=1000)

    train_target = parts["train"]["target"]
    majority_baseline_pct = round(max(train_target.mean(), 1 - train_target.mean()) * 100, 4)

    model = LGBMClassifier(
        num_leaves=63, max_depth=5, min_child_samples=10, learning_rate=0.05,
        n_estimators=500, class_weight="balanced", random_state=42, verbosity=-1,
    )
    model.fit(
        parts["train"][feature_cols], parts["train"]["target"],
        eval_set=[(parts["validation"][feature_cols], parts["validation"]["target"])],
        callbacks=[],
    )

    holdout_results = {}
    for name in ("holdout_a", "holdout_b", "holdout_c"):
        part = parts[name]
        proba = model.predict_proba(part[feature_cols])[:, 1]
        pred = (proba >= 0.5).astype(int)
        target_arr = part["target"].to_numpy()
        acc = round(accuracy_score(target_arr, pred) * 100, 4)
        try:
            auc = round(roc_auc_score(target_arr, proba), 4)
        except ValueError:
            auc = None
        fwd_ret = part["forward_return_bps"].to_numpy()
        thresholds = {
            str(t): _proxy_pnl_at_threshold(proba, fwd_ret, target_arr, t)
            for t in CONFIDENCE_THRESHOLDS
        }
        holdout_results[name] = {
            "rows": int(len(part)),
            "accuracy_pct": acc,
            "majority_baseline_pct": majority_baseline_pct,
            "vs_baseline_pp": round(acc - majority_baseline_pct, 4),
            "auc": auc,
            "at_threshold": thresholds,
        }

    mean_acc = round(
        np.mean([holdout_results[h]["accuracy_pct"] for h in holdout_results]), 4
    )

    perm = permutation_importance(
        model, parts["validation"][feature_cols], parts["validation"]["target"],
        n_repeats=PERMUTATION_IMPORTANCE_REPEATS, random_state=42, n_jobs=-1,
    )
    perm_rows = sorted(
        [
            {"feature": c, "mean_accuracy_decrease": round(float(m), 6)}
            for c, m in zip(feature_cols, perm.importances_mean)
        ],
        key=lambda r: -r["mean_accuracy_decrease"],
    )

    return {
        "horizon_minutes": horizon_min,
        "feature_columns": feature_cols,
        "eligible_rows": int(len(eligible)),
        "majority_baseline_pct": majority_baseline_pct,
        "mean_holdout_accuracy_pct": mean_acc,
        "holdouts": holdout_results,
        "permutation_importance": perm_rows,
        "partitions": [
            {"name": p.name, "start": p.start, "end": p.end, "rows": p.rows}
            for p in partitions
        ],
    }, model


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candles", type=Path, default=Path("state/btc_1m_candles_real_2025_2026.parquet"))
    parser.add_argument("--taker-flow", type=Path, default=Path("state/binance_taker_flow_1m_btc.parquet"))
    parser.add_argument("--horizons", type=str, default="5,60,1440")
    parser.add_argument("--output-dir", type=Path, default=Path("state"))
    args = parser.parse_args()

    candles_path = PROJECT_ROOT / args.candles if not args.candles.is_absolute() else args.candles
    flow_path = PROJECT_ROOT / args.taker_flow if not args.taker_flow.is_absolute() else args.taker_flow
    out_dir = PROJECT_ROOT / args.output_dir if not args.output_dir.is_absolute() else args.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    horizons = [int(h) for h in args.horizons.split(",") if h.strip()]

    print(f"[INFO] Loading candles from {candles_path} ...")
    df = load_candles(candles_path)
    print(f"[INFO] {len(df)} candles, {df['timestamp'].iloc[0]} -> {df['timestamp'].iloc[-1]}")

    df = calculate_momentum_features(df)

    flow_df = load_taker_flow(flow_path)
    if flow_df is not None:
        print(f"[INFO] Taker flow: {len(flow_df)} 1-minute rows loaded from {flow_path}")
        df = merge_order_flow(df, flow_df)
        feature_cols = BASE_FEATURE_COLUMNS + ORDER_FLOW_COLUMNS
    else:
        print(f"[WARN] Taker flow not found at {flow_path} -- training momentum-only features")
        feature_cols = BASE_FEATURE_COLUMNS

    results = {}
    for h in horizons:
        metrics, model = train_and_evaluate_horizon(df, h, feature_cols)
        results[f"{h}min"] = metrics
        model_path = out_dir / f"btc_direction_model_{h}min_v1.pkl"
        joblib.dump(model, model_path)
        print(f"[SAVED] {model_path}")
        print(f"[RESULT] horizon={h}min mean_holdout_acc={metrics['mean_holdout_accuracy_pct']}% "
              f"vs majority={metrics['majority_baseline_pct']}%")

    out_path = out_dir / "btc_direction_training_metrics_v1.json"
    out_path.write_text(json.dumps({
        "round_trip_cost_bps": ROUND_TRIP_COST_BPS,
        "confidence_thresholds": CONFIDENCE_THRESHOLDS,
        "has_order_flow_features": flow_df is not None,
        "horizons": results,
    }, indent=2, sort_keys=True))
    print(f"[SAVED] {out_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
