#!/usr/bin/env python3
"""Adversarial validation of the reported 66.13% LightGBM BTC volatility-regime accuracy.

Independent, adversarial re-implementation. The claim under test:

  "Mistral BTC model" = LightGBM volatility-regime classifier trained by
  scripts/fill_and_test_arb.py (v1, SUPERSEDED). Reported 66.13% accuracy
  (vs prior 54.08%), "signal rate 32.20%", "ready for live testing".

Parts:
  1. Faithful reproduction of the v1 run on state/btc_1m_candles_lse.parquet (5,000 rows)
  2. Corrected (Codex-equivalent) chronological evaluation on the same 5,000-row frame
  3. Corrected full-frame evaluation on state/btc_1m_candles_lse_v3.parquet (139,523 rows)
  4. Leakage audit (verbatim code evidence)
  5. Verdict

All numbers printed to stdout and dumped to /tmp/adversarial_validation_results.json.
No network calls. No writes to state/ or protected files.
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
import time
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from lightgbm import LGBMClassifier
from sklearn.metrics import accuracy_score, average_precision_score, f1_score
from sklearn.preprocessing import StandardScaler

PROJECT_ROOT = Path(__file__).resolve().parents[1]
HEADLINE_ACCURACY = 0.6612576064908722  # state/lightgbm_training_metrics.json
HEADLINE_SIGNAL_RATE_PCT = 32.20
STORE_DIR = PROJECT_ROOT / "state"

# =============================================================================
# V1 CODE -- copied VERBATIM from scripts/fill_and_test_arb.py (SUPERSEDED v1)
# source: fill_and_test_arb.py:56-69
# =============================================================================

# Feature engineering (fill_and_test_arb.py:56-66)
HORIZON_MINUTES = 15
FEATURE_COLUMNS = [
    "open", "high", "low", "close", "volume",
    "log_volume", "return_1m_bps", "rolling_vol_5m_bps",
    "rolling_vol_15m_bps", "rolling_vol_60m_bps", "atr_14m_bps",
    "atr_60m_bps", "minutes_to_next_high_macro", "session_bucket",
    "hour_sin", "hour_cos", "dayofweek_sin", "dayofweek_cos",
    "bollinger_band_width", "rsi_14", "macd_line", "macd_signal",
    "price_trend_5m", "price_trend_15m", "volume_trend_5m",
]

CLASS_NAMES = ["LOW", "MED", "HIGH"]
TARGET_COLUMN = "forward_realized_vol_bps"


# VERBATIM from fill_and_test_arb.py:94-168
def calculate_technical_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Calculate all technical indicators for the dataframe."""
    df = df.copy()

    # Log returns
    df['return_1m_bps'] = (np.log(df['close'] / df['close'].shift(1))) * 10000

    # Rolling volatilities
    for window in [5, 15, 60]:
        df[f'rolling_vol_{window}m_bps'] = df['return_1m_bps'].rolling(window=window).std()

    # ATR (Average True Range)
    df['tr'] = np.maximum(
        df['high'] - df['low'],
        np.maximum(
            abs(df['high'] - df['close'].shift(1)),
            abs(df['low'] - df['close'].shift(1))
        )
    )
    df['atr_14m_bps'] = df['tr'].rolling(window=14).mean() * 10000
    df['atr_60m_bps'] = df['tr'].rolling(window=60).mean() * 10000

    # Log volume
    df['log_volume'] = np.log1p(df['volume'])

    # Bollinger Bands
    df['bb_middle'] = df['close'].rolling(window=20).mean()
    df['bb_std'] = df['close'].rolling(window=20).std()
    df['bollinger_band_width'] = (df['bb_std'] / df['bb_middle']) * 100

    # RSI
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss
    df['rsi_14'] = 100 - (100 / (1 + rs))

    # MACD
    exp1 = df['close'].ewm(span=12, adjust=False).mean()
    exp2 = df['close'].ewm(span=26, adjust=False).mean()
    df['macd_line'] = exp1 - exp2
    df['macd_signal'] = df['macd_line'].ewm(span=9, adjust=False).mean()

    # Price trends (linear regression slope)
    for window in [5, 15]:
        x = np.arange(len(df))
        y = df['close'].values
        # Simple rolling linear regression
        df[f'price_trend_{window}m'] = df['close'].rolling(window=window).apply(
            lambda x: np.polyfit(np.arange(len(x)), x, 1)[0] if len(x) == window else np.nan, raw=False
        )

    # Volume trend
    df['volume_trend_5m'] = df['volume'].rolling(window=5).apply(
        lambda x: np.polyfit(np.arange(len(x)), x, 1)[0] if len(x) == 5 else np.nan, raw=False
    )

    # Temporal features
    df['hour'] = df.index.hour
    df['dayofweek'] = df.index.dayofweek
    df['hour_sin'] = np.sin(2 * np.pi * df['hour'] / 24)
    df['hour_cos'] = np.cos(2 * np.pi * df['hour'] / 24)
    df['dayofweek_sin'] = np.sin(2 * np.pi * df['dayofweek'] / 7)
    df['dayofweek_cos'] = np.cos(2 * np.pi * df['dayofweek'] / 7)

    # Session buckets (simplified)
    df['session_bucket'] = df.index.hour % 24
    df['session_bucket'] = df['session_bucket'].apply(
        lambda h: 0 if 0 <= h < 6 else (1 if 6 <= h < 12 else (2 if 12 <= h < 18 else 3))
    )

    # Macro event proximity (placeholder - would need real macro calendar)
    df['minutes_to_next_high_macro'] = 0

    return df


# VERBATIM from fill_and_test_arb.py:171-196 (leakage deliberately kept for Part 1)
def calculate_forward_volatility(df: pd.DataFrame, horizon_minutes: int = 15) -> pd.DataFrame:
    """Calculate forward realized volatility as target variable."""
    df = df.copy()

    # Calculate forward realized volatility for each candle
    forward_vols = []
    for i in range(len(df)):
        end_idx = min(i + horizon_minutes, len(df))
        if end_idx > i + 1:
            returns = df['return_1m_bps'].iloc[i:end_idx].values
            vol = np.sqrt(np.sum(returns**2)) * 10000
            forward_vols.append(vol)
        else:
            forward_vols.append(np.nan)

    df[TARGET_COLUMN] = forward_vols

    # Create volatility regime classes (terciles)
    cutoffs = df[TARGET_COLUMN].quantile([0.333, 0.667]).values
    df['volatility_regime'] = pd.cut(
        df[TARGET_COLUMN],
        bins=[-np.inf, cutoffs[0], cutoffs[1], np.inf],
        labels=['LOW', 'MED', 'HIGH']
    )

    return df, cutoffs


# VERBATIM from fill_and_test_arb.py:326-342
V1_MODEL_PARAMS = {
    "objective": "multiclass",
    "num_class": 3,
    "boosting_type": "gbdt",
    "metric": ["multi_logloss", "multi_error"],
    "n_estimators": 1000,
    "learning_rate": 0.05,
    "num_leaves": 63,
    "max_depth": 5,
    "min_child_samples": 10,
    "subsample": 0.8,
    "colsample_bytree": 0.8,
    "class_weight": "balanced",
    "random_state": 20260804,
    "n_jobs": -1,
    "verbosity": -1,
}

# =============================================================================
# CODEX DISCIPLINE -- copied VERBATIM from scripts/evaluate_btc_offline_profitability.py
# source: evaluate_btc_offline_profitability.py:48-53, 73-132
# =============================================================================

RANDOM_SEED = 20260811
TIME_COLUMN = "timestamp"
HOLDOUT_NAMES = ("holdout_a", "holdout_b", "holdout_c")
EMBARGO_ROWS = 15
EMBARGO_MINUTES = 15


# VERBATIM from evaluate_btc_offline_profitability.py:73-89
def _purge_before_boundary(
    block: pd.DataFrame,
    next_block_start: pd.Timestamp,
    embargo_rows: int,
    embargo_minutes: int,
) -> pd.DataFrame:
    """Remove a row/time embargo so no retained label reaches the next block."""
    if len(block) <= embargo_rows:
        raise ValueError("partition is too small to retain rows after the embargo")
    purged = block.iloc[:-embargo_rows].copy()
    label_end = purged[TIME_COLUMN] + pd.Timedelta(minutes=embargo_minutes)
    while len(purged) and label_end.iloc[-1] >= next_block_start:
        purged = purged.iloc[:-1].copy()
        label_end = purged[TIME_COLUMN] + pd.Timedelta(minutes=embargo_minutes)
    if purged.empty:
        raise ValueError("partition has no rows after enforcing the label embargo")
    return purged.reset_index(drop=True)


# VERBATIM from evaluate_btc_offline_profitability.py:92-132
def chronological_partitions(
    frame: pd.DataFrame,
    train_fraction: float = 0.55,
    embargo_rows: int = EMBARGO_ROWS,
    embargo_minutes: int = EMBARGO_MINUTES,
) -> dict[str, pd.DataFrame]:
    """Split rows and purge every boundary from the preceding label block."""
    if not 0 < train_fraction < 1:
        raise ValueError("train_fraction must be strictly between zero and one")
    if embargo_rows < 1 or embargo_minutes < 1:
        raise ValueError("embargo rows and minutes must both be positive")
    if len(frame) < 8:
        raise ValueError("need at least eight labelled rows for train plus three holdouts")

    ordered = frame.sort_values(TIME_COLUMN, kind="stable").reset_index(drop=True)
    train_end = int(np.floor(len(ordered) * train_fraction))
    if train_end < 2 or len(ordered) - train_end < 3:
        raise ValueError("insufficient rows after applying the chronological split")

    raw_blocks = {"train": ordered.iloc[:train_end].copy()}
    later_rows = ordered.iloc[train_end:]
    rows_per_holdout, extra_rows = divmod(len(later_rows), len(HOLDOUT_NAMES))
    start = 0
    for index, name in enumerate(HOLDOUT_NAMES):
        stop = start + rows_per_holdout + (index < extra_rows)
        raw_blocks[name] = later_rows.iloc[start:stop].copy().reset_index(drop=True)
        start = stop
    names = ("train", *HOLDOUT_NAMES)
    partitions: dict[str, pd.DataFrame] = {}
    for index, name in enumerate(names):
        block = raw_blocks[name]
        if index == len(names) - 1:
            partitions[name] = block.reset_index(drop=True)
        else:
            partitions[name] = _purge_before_boundary(
                block,
                raw_blocks[names[index + 1]][TIME_COLUMN].iloc[0],
                embargo_rows,
                embargo_minutes,
            )
    return partitions


# =============================================================================
# Shared helpers
# =============================================================================

CLASS_CODE = {"LOW": 0, "MED": 1, "HIGH": 2}  # v1 pd.cut label order -> category codes


def build_v1_frame(candles: pd.DataFrame) -> pd.DataFrame:
    """Rebuild v1 features + raw forward-vol target (NO labels, NO full-frame cutoffs).

    Mirrors fill_and_test_arb.py train_lightgbm() minus the (leaky) labeling step.
    Returns rows complete for all 25 features + target, with a `timestamp` column.
    """
    df = candles.copy()
    if df.index.name not in (None, "ts") or not isinstance(df.index, pd.DatetimeIndex):
        df = df.set_index("timestamp").sort_index()
    df = df[~df.index.duplicated(keep="first")].sort_index()
    df = calculate_technical_indicators(df)          # verbatim v1
    df, _ = calculate_forward_volatility(df, HORIZON_MINUTES)  # verbatim v1 (target only)
    df = df.reset_index()
    df = df.rename(columns={"ts": TIME_COLUMN, "index": TIME_COLUMN})
    if TIME_COLUMN not in df.columns:
        df[TIME_COLUMN] = pd.to_datetime(df.index)
    df[TIME_COLUMN] = pd.to_datetime(df[TIME_COLUMN], utc=True, errors="raise")
    feature_cols = [c for c in FEATURE_COLUMNS if c in df.columns]
    df = df.dropna(subset=feature_cols + [TARGET_COLUMN])
    df = df.sort_values(TIME_COLUMN, kind="stable").reset_index(drop=True)
    return df


def label_terciles(target: pd.Series, cutoffs: np.ndarray) -> pd.Series:
    """Label with fixed cutoffs using v1's exact pd.cut call (labels LOW/MED/HIGH)."""
    return pd.cut(
        target,
        bins=[-np.inf, cutoffs[0], cutoffs[1], np.inf],
        labels=CLASS_NAMES,
    )


def label_codes(labels: pd.Series) -> np.ndarray:
    """v1 codes: LOW=0, MED=1, HIGH=2 (pd.cut labels preserve given order)."""
    return labels.astype("category").cat.codes.to_numpy()


def v1_model(n_estimators: int | None = None) -> LGBMClassifier:
    params = dict(V1_MODEL_PARAMS)
    if n_estimators is not None:
        params["n_estimators"] = n_estimators
    return LGBMClassifier(**params)


def majority_fraction(labels: np.ndarray) -> float:
    counts = np.bincount(labels.astype(int), minlength=3)
    return float(counts.max() / len(labels))


def per_class_pr_auc(y_true: np.ndarray, proba: np.ndarray) -> dict:
    """One-vs-rest average_precision_score per class vs prevalence baseline."""
    out = {}
    for c, name in enumerate(CLASS_NAMES):
        bin_true = (y_true == c).astype(int)
        out[name] = {
            "prevalence": float(bin_true.mean()),
            "pr_auc": float(average_precision_score(bin_true, proba[:, c])),
        }
    return out


def class_counts(labels: np.ndarray) -> dict:
    counts = np.bincount(labels.astype(int), minlength=3)
    return {CLASS_NAMES[i]: int(counts[i]) for i in range(3)}


def train_f1_threshold_binary(y_true: np.ndarray, proba_high: np.ndarray) -> float:
    """F1-maximizing threshold on P(HIGH) vs not-HIGH, train only (Codex-style)."""
    from sklearn.metrics import precision_recall_curve

    pos = (y_true == 2).astype(int)
    precision, recall, thresholds = precision_recall_curve(pos, proba_high)
    if len(thresholds) == 0:
        return 0.5
    f1 = np.divide(
        2 * precision[:-1] * recall[:-1],
        precision[:-1] + recall[:-1],
        out=np.zeros_like(thresholds),
        where=(precision[:-1] + recall[:-1]) > 0,
    )
    best = np.flatnonzero(f1 == np.nanmax(f1))
    return float(thresholds[best[0]]) if len(best) else 0.5


def fit_v1_pipeline(train_frame: pd.DataFrame, n_estimators: int | None = None):
    """Scaler + v1-identical model fitted on a (purged) train partition."""
    feature_cols = [c for c in FEATURE_COLUMNS if c in train_frame.columns]
    X = train_frame[feature_cols].to_numpy()
    y = label_codes(train_frame["regime"])
    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)
    model = v1_model(n_estimators=n_estimators)
    model.fit(X_scaled, y)
    return scaler, model, feature_cols


def evaluate_partition(
    partition: pd.DataFrame, scaler: StandardScaler, model: LGBMClassifier,
    feature_cols: list, headline_delta: float | None = None,
) -> dict:
    """Full metric block for one partition (accuracy, baseline, PR-AUC, signal rate)."""
    X = partition[feature_cols].to_numpy()
    y = label_codes(partition["regime"])
    proba = model.predict_proba(scaler.transform(X))
    preds = model.predict(scaler.transform(X))
    acc = float(accuracy_score(y, preds))
    entry = {
        "rows": int(len(partition)),
        "class_distribution": class_counts(y),
        "accuracy": acc,
        "majority_baseline_accuracy": majority_fraction(y),
        "per_class_pr_auc": per_class_pr_auc(y, proba),
        "high_signal_rate_pct": float((preds == 2).mean() * 100),
        "train_majority_fraction_on_holdout": None,
    }
    if headline_delta is not None:
        entry["accuracy_delta_vs_headline_66_13pp"] = (acc - headline_delta) * 100
    return entry


def fmt_pct(x: float) -> str:
    return f"{x * 100:.2f}%"


def print_header(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


# =============================================================================
# PART 1 -- faithful reproduction of the 66.13% run (leakage replicated as-is)
# =============================================================================

def run_part1(n_estimators: int) -> dict:
    print_header("PART 1 -- FAITHFUL REPRODUCTION OF THE V1 66.13% RUN (5,000-row frame)")
    candles = pd.read_parquet(STORE_DIR / "btc_1m_candles_lse.parquet")
    print(f"Loaded state/btc_1m_candles_lse.parquet: {len(candles)} rows, "
          f"range {candles.index[0]} -> {candles.index[-1]}, cols={list(candles.columns)}")

    # --- exact v1 training path (train_lightgbm, fill_and_test_arb.py:286-345) ---
    df = calculate_technical_indicators(candles)
    df, cutoffs = calculate_forward_volatility(df, HORIZON_MINUTES)   # full-frame quantiles (leak)
    feature_cols = [c for c in FEATURE_COLUMNS if c in df.columns]
    df = df.dropna(subset=feature_cols + [TARGET_COLUMN])

    X = df[feature_cols].values
    y = df['volatility_regime'].astype('category').cat.codes.values

    # exact v1 split (fill_and_test_arb.py:312-316): single final-10% slice
    test_size = int(0.1 * len(X))
    X_train, X_test = X[:-test_size], X[-test_size:]
    y_train, y_test = y[:-test_size], y[-test_size:]

    scaler = StandardScaler()
    X_train_scaled = scaler.fit_transform(X_train)
    X_test_scaled = scaler.transform(X_test)

    model = v1_model(n_estimators=n_estimators)
    t0 = time.time()
    model.fit(X_train_scaled, y_train)
    fit_seconds = time.time() - t0

    y_pred = model.predict(X_test_scaled)
    proba_test = model.predict_proba(X_test_scaled)
    accuracy = accuracy_score(y_test, y_pred)
    f1_macro = f1_score(y_test, y_pred, average='macro')
    majority_base = majority_fraction(y_test)

    result = {
        "frame_rows_raw": int(len(candles)),
        "valid_rows_after_dropna": int(len(df)),
        "train_size": int(len(X_train)),
        "test_size": int(len(X_test)),
        "full_frame_cutoffs_bps": [float(c) for c in cutoffs],
        "test_class_distribution": class_counts(y_test),
        "test_class_distribution_pct": {
            k: float(v / len(y_test)) for k, v in class_counts(y_test).items()
        },
        "model_accuracy": float(accuracy),
        "model_accuracy_pct": float(accuracy * 100),
        "f1_macro": float(f1_macro),
        "majority_baseline_accuracy_pct": float(majority_base * 100),
        "model_beats_majority_pp": float((accuracy - majority_base) * 100),
        "per_class_pr_auc": per_class_pr_auc(y_test, proba_test),
        "fit_seconds": fit_seconds,
    }
    print(f"valid rows after dropna: {len(df)}  (expected 4939)")
    print(f"train={len(X_train)}  test={len(X_test)}")
    print(f"full-frame tercile cutoffs (bps): {[f'{c:.2f}' for c in cutoffs]}  "
          f"(stored run: 110410.06 / 161186.49)")
    print(f"test class distribution: {result['test_class_distribution']}  "
          f"({ {k: f'{v*100:.2f}%' for k, v in result['test_class_distribution_pct'].items()} })")
    print(f"MODEL TEST ACCURACY: {fmt_pct(accuracy)}   (headline: 66.13%)")
    print(f"F1 macro: {f1_macro:.4f}   (stored: 0.3375)")
    print(f"MAJORITY-CLASS BASELINE ON TEST: {fmt_pct(majority_base)}")
    print(f"model vs majority baseline: {result['model_beats_majority_pp']:+.2f} pp")
    print("per-class PR-AUC (one-vs-rest) vs prevalence baseline:")
    for k, v in result["per_class_pr_auc"].items():
        print(f"  {k}: PR-AUC={v['pr_auc']:.4f}  prevalence={v['prevalence']*100:.2f}%  "
              f"({'+' if v['pr_auc'] > v['prevalence'] else ''}{v['pr_auc']-v['prevalence']:+.4f})")

    # --- v1 signal-rate path (test_arbitrage_signals, fill_and_test_arb.py:616-643, 687-688) ---
    df2 = calculate_technical_indicators(candles)
    df2, _ = calculate_forward_volatility(df2, HORIZON_MINUTES)
    df2 = df2.dropna(subset=[c for c in FEATURE_COLUMNS if c in df2.columns] + [TARGET_COLUMN])
    X_full = scaler.transform(df2[feature_cols].values)
    full_preds = model.predict(X_full)
    high_count = int((full_preds == 2).sum())
    signal_rate = high_count / len(candles) * 100  # v1 divides by RAW frame size
    result["signal"] = {
        "high_count_on_valid_rows": high_count,
        "valid_rows_scored": int(len(df2)),
        "denominator_raw_frame_rows": int(len(candles)),
        "signal_rate_pct_v1_definition": float(signal_rate),
        "signal_rate_pct_honest_definition": float(high_count / len(df2) * 100),
        "headline_signal_rate_pct": HEADLINE_SIGNAL_RATE_PCT,
    }
    print(f"\nsignal-rate replication (v1 definition, /{len(candles)} raw rows): "
          f"{high_count}/{len(candles)} = {signal_rate:.2f}%   (headline 32.20%, stored 1610/5000)")

    # --- saved-model check (state/lightgbm_volatility_model.pkl) ---
    print("\nsaved-model check:")
    try:
        saved_model = joblib.load(STORE_DIR / "lightgbm_volatility_model.pkl")
        saved_scaler = joblib.load(STORE_DIR / "lightgbm_scaler.pkl")
        n_features = int(saved_model.n_features_in_)
        print(f"  saved model n_features_in_={n_features} (claim: 25)")
        result["saved_model"] = {"n_features_in_": n_features}
        X_test_saved = saved_scaler.transform(X_test)
        acc_saved = float(accuracy_score(y_test, saved_model.predict(X_test_saved)))
        preds_full_saved = saved_model.predict(saved_scaler.transform(X_full))
        dist_full = np.bincount(preds_full_saved, minlength=3)
        result["saved_model"]["test_accuracy"] = acc_saved
        result["saved_model"]["full_frame_pred_distribution"] = {
            "LOW": int(dist_full[0]), "MED": int(dist_full[1]), "HIGH": int(dist_full[2])
        }
        print(f"  saved model accuracy on same test slice: {fmt_pct(acc_saved)}")
        print(f"  saved model full-frame prediction distribution: "
              f"LOW={int(dist_full[0])}, MED={int(dist_full[1])}, HIGH={int(dist_full[2])}")
    except Exception as e:  # report and continue; retrained model is authoritative
        result["saved_model"] = {"error": f"{type(e).__name__}: {e}"}
        print(f"  saved-model scoring ERRORED: {type(e).__name__}: {e} (continuing)")

    return result


# =============================================================================
# PARTS 2 & 3 -- corrected pipeline (train-only cutoffs, embargoed chronological splits)
# =============================================================================

def run_corrected(
    candles: pd.DataFrame,
    label: str,
    n_estimators: int,
    headline_delta: float,
) -> dict:
    print_header(f"CORRECTED CHRONOLOGICAL EVALUATION -- {label} ({len(candles)} raw rows)")
    frame = build_v1_frame(candles)
    print(f"feature-complete rows: {len(frame)}")

    partitions = chronological_partitions(frame, 0.55, EMBARGO_ROWS, EMBARGO_MINUTES)
    for name, part in partitions.items():
        print(f"  {name}: {len(part)} rows  ({part[TIME_COLUMN].iloc[0]} -> {part[TIME_COLUMN].iloc[-1]})")

    # THE DOCUMENTED FIX: tercile cutoffs from TRAIN ONLY (per FIXED_TRAINING_REPORT_V2.md)
    train_target = partitions["train"][TARGET_COLUMN]
    cutoffs = train_target.quantile([1 / 3, 2 / 3]).values
    for name, part in partitions.items():
        part["regime"] = label_terciles(part[TARGET_COLUMN], cutoffs)

    scaler, model, feature_cols = fit_v1_pipeline(partitions["train"], n_estimators=n_estimators)
    train_labels = label_codes(partitions["train"]["regime"])
    train_majority = int(np.bincount(train_labels, minlength=3).argmax())
    train_majority_name = CLASS_NAMES[train_majority]

    proba_train_high = model.predict_proba(scaler.transform(partitions["train"][feature_cols].to_numpy()))[:, 2]
    threshold = train_f1_threshold_binary(train_labels, proba_train_high)

    result = {
        "label": label,
        "feature_complete_rows": int(len(frame)),
        "train_only_cutoffs_bps": [float(c) for c in cutoffs],
        "train_majority_class": train_majority_name,
        "train_high_f1_threshold": threshold,
        "partitions": {},
        "holdouts": {},
        "aggregate": {},
    }
    for name, part in partitions.items():
        entry = evaluate_partition(part, scaler, model, feature_cols, headline_delta=headline_delta)
        entry["utc_start"] = part[TIME_COLUMN].iloc[0].isoformat()
        entry["utc_end"] = part[TIME_COLUMN].iloc[-1].isoformat()
        result["partitions"][name] = entry

    # binary HIGH-vs-rest threshold test (Codex-style) + aggregates
    high_accs, high_bases = [], []
    for name in HOLDOUT_NAMES:
        part = partitions[name]
        y = label_codes(part["regime"])
        proba_high = model.predict_proba(scaler.transform(part[feature_cols].to_numpy()))[:, 2]
        thr_preds = (proba_high >= threshold).astype(int)
        y_bin = (y == 2).astype(int)
        thr_acc = float(accuracy_score(y_bin, thr_preds))
        bin_base = max(float(y_bin.mean()), 1 - float(y_bin.mean()))
        entry = {
            "binary_high_threshold_accuracy": thr_acc,
            "binary_high_majority_baseline": bin_base,
            "binary_high_beats_baseline_pp": (thr_acc - bin_base) * 100,
            "high_class_fraction": float(y_bin.mean()),
        }
        result["holdouts"][name] = entry
        result["partitions"][name].update(entry)
        high_accs.append(thr_acc)
        high_bases.append(bin_base)
        print(f"\n[{name}] rows={len(part)}  "
              f"dist={result['partitions'][name]['class_distribution']}")
        print(f"  accuracy={fmt_pct(result['partitions'][name]['accuracy'])}  "
              f"majority baseline={fmt_pct(result['partitions'][name]['majority_baseline_accuracy'])}  "
              f"delta vs headline={result['partitions'][name]['accuracy_delta_vs_headline_66_13pp']:+.2f}pp")
        for k, v in result["partitions"][name]["per_class_pr_auc"].items():
            print(f"    {k}: PR-AUC={v['pr_auc']:.4f} vs prevalence {v['prevalence']*100:.2f}%  "
                  f"({'+' if v['pr_auc'] > v['prevalence'] else ''}{(v['pr_auc']-v['prevalence'])*100:+.2f}pp)")
        print(f"  high signal rate (P(HIGH) argmax): {result['partitions'][name]['high_signal_rate_pct']:.2f}%  "
              f"| thresholded: acc={entry['binary_high_threshold_accuracy']*100:.2f}% vs "
              f"baseline {entry['binary_high_majority_baseline']*100:.2f}%")

    accs = [result["partitions"][h]["accuracy"] for h in HOLDOUT_NAMES]
    bases = [result["partitions"][h]["majority_baseline_accuracy"] for h in HOLDOUT_NAMES]
    result["aggregate"] = {
        "mean_holdout_accuracy_pct": float(np.mean(accs) * 100),
        "mean_majority_baseline_pct": float(np.mean(bases) * 100),
        "beats_majority_on_all_holdouts": bool(all(a > b for a, b in zip(accs, bases))),
        "beats_majority_on_any_holdout": bool(any(a > b for a, b in zip(accs, bases))),
        "binary_threshold_beats_majority_on_all": bool(all(x > y for x, y in zip(high_accs, high_bases))),
        "binary_threshold_beats_majority_on_any": bool(any(x > y for x, y in zip(high_accs, high_bases))),
        "high_pr_auc_above_prevalence_on_all": bool(all(
            result["partitions"][h]["per_class_pr_auc"]["HIGH"]["pr_auc"]
            > result["partitions"][h]["per_class_pr_auc"]["HIGH"]["prevalence"]
            for h in HOLDOUT_NAMES
        )),
    }
    print(f"\naggregate: mean holdout accuracy={result['aggregate']['mean_holdout_accuracy_pct']:.2f}%  "
          f"mean majority baseline={result['aggregate']['mean_majority_baseline_pct']:.2f}%")
    print(f"beats majority on any holdout: {result['aggregate']['beats_majority_on_any_holdout']}  "
          f"all: {result['aggregate']['beats_majority_on_all_holdouts']}")
    return result


# =============================================================================
# PART 4 -- leakage audit (programmatic evidence)
# =============================================================================

def extract_feature_list(path: Path) -> list[str]:
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "FEATURE_COLUMNS" for t in node.targets
        ):
            return [elt.value for elt in node.value.elts]  # type: ignore[attr-defined]
    return []


def negative_shift_usage(path: Path) -> list[str]:
    """Scan for shift(-n) with negative n inside calculate_technical_indicators."""
    tree = ast.parse(path.read_text())
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "calculate_technical_indicators":
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) and sub.func.attr == "shift":
                    for arg in sub.args:
                        if isinstance(arg, ast.UnaryOp) and isinstance(arg.op, ast.USub):
                            found.append(ast.unparse(sub))
    return found


def run_part4(part2: dict, part3: dict) -> dict:
    print_header("PART 4 -- LEAKAGE AUDIT (programmatic evidence)")
    v1_path = PROJECT_ROOT / "scripts" / "fill_and_test_arb.py"
    comp_path = PROJECT_ROOT / "scripts" / "train_lightgbm_comprehensive.py"

    feats_v1 = extract_feature_list(v1_path)
    feats_comp = extract_feature_list(comp_path)
    audit = {
        "feature_list_66pct_v1": feats_v1,
        "feature_list_54pct_comprehensive": feats_comp,
        "feature_lists_identical": feats_v1 == feats_comp,
        "feature_count": len(feats_v1),
        "negative_shift_in_v1_feature_builder": negative_shift_usage(v1_path),
    }
    print(f"v1 (66% run) FEATURE_COLUMNS ({len(feats_v1)}): {feats_v1}")
    print(f"comprehensive (54% run) FEATURE_COLUMNS ({len(feats_comp)}): {feats_comp}")
    print(f"feature lists identical: {audit['feature_lists_identical']}")
    print(f"negative-shift (future-looking) usage in v1 feature builder: "
          f"{audit['negative_shift_in_v1_feature_builder'] or 'none'}")

    # constant-feature verification
    candles = pd.read_parquet(STORE_DIR / "btc_1m_candles_lse.parquet")
    feat = calculate_technical_indicators(candles)
    const_check = {
        "minutes_to_next_high_macro_unique_values": sorted(
            feat["minutes_to_next_high_macro"].dropna().unique().tolist()
        ),
        "minutes_to_next_high_macro_is_constant_zero": bool(
            (feat["minutes_to_next_high_macro"] == 0).all()
        ),
    }
    audit["constant_feature_check"] = const_check
    print(f"minutes_to_next_high_macro unique values: {const_check['minutes_to_next_high_macro_unique_values']} "
          f"(v1 hardcodes 0 at fill_and_test_arb.py:166)")

    # stored artifacts
    with open(STORE_DIR / "lightgbm_training_metrics.json") as f:
        stored_metrics = json.load(f)
    with open(STORE_DIR / "arbitrage_test_results.json") as f:
        stored_arb = json.load(f)
    audit["stored_artifacts"] = {
        "lightgbm_training_metrics.json": {
            "accuracy": stored_metrics["accuracy"],
            "accuracy_pct": round(stored_metrics["accuracy"] * 100, 2),
            "f1_macro": stored_metrics["f1_macro"],
            "training_samples": stored_metrics["training_samples"],
            "test_samples": stored_metrics["test_samples"],
            "cutoffs_bps": stored_metrics["cutoffs"],
            "timestamp": stored_metrics["timestamp"],
        },
        "arbitrage_test_results.json": {
            "lightgbm_signals": stored_arb["lightgbm_signals"],
            "total_candles": stored_arb["total_candles"],
            "signal_rate_pct": round(stored_arb["lightgbm_signals"] / stored_arb["total_candles"] * 100, 2),
        },
    }
    print(f"stored accuracy: {stored_metrics['accuracy']:.4f} "
          f"({stored_metrics['accuracy']*100:.2f}%)  train={stored_metrics['training_samples']} "
          f"test={stored_metrics['test_samples']}  cutoffs={stored_metrics['cutoffs']}")
    print(f"stored signals: {stored_arb['lightgbm_signals']}/{stored_arb['total_candles']} "
          f"= {stored_arb['lightgbm_signals']/stored_arb['total_candles']*100:.2f}%")

    return audit


# =============================================================================
# Main
# =============================================================================

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--estimators", type=int, default=1000,
                        help="LightGBM n_estimators for Parts 2-3 (deviation must be stated if !=1000)")
    parser.add_argument("--fast", action="store_true", help="skip Part 3 (v3 full frame)")
    args = parser.parse_args()

    print(f"venv python: {sys.version.split()[0]}  |  "
          f"n_estimators: {args.estimators}  (v1 used 1000)")

    part1 = run_part1(n_estimators=args.estimators)

    candles_lse = pd.read_parquet(STORE_DIR / "btc_1m_candles_lse.parquet")
    part2 = run_corrected(candles_lse, "5,000-row lse frame", args.estimators,
                          headline_delta=HEADLINE_ACCURACY)

    part3 = None
    if not args.fast:
        candles_v3 = pd.read_parquet(STORE_DIR / "btc_1m_candles_lse_v3.parquet")
        part3 = run_corrected(candles_v3, "139,523-row v3 full frame", args.estimators,
                              headline_delta=HEADLINE_ACCURACY)

    part4 = run_part4(part2, part3)

    # ---- Part 5: verdict ----
    print_header("PART 5 -- VERDICT")
    reproduced_acc = part1["model_accuracy"]
    baseline_p1 = part1["majority_baseline_accuracy_pct"]
    part2_any = part2["aggregate"]["beats_majority_on_any_holdout"]
    part2_all = part2["aggregate"]["beats_majority_on_all_holdouts"]
    high_signal_part1 = part1["per_class_pr_auc"]["HIGH"]
    verdict_parts = {
        "part1_reproduced_accuracy_pct": round(reproduced_acc * 100, 2),
        "part1_majority_baseline_pct": round(baseline_p1, 2),
        "part2_holds_after_fix": None,
        "verdict": None,
    }
    if part3 is not None:
        p3_any = part3["aggregate"]["beats_majority_on_any_holdout"]
        p3_all = part3["aggregate"]["beats_majority_on_all_holdouts"]
        verdict_parts.update({
            "part3_beats_majority_any": p3_any,
            "part3_beats_majority_all": p3_all,
            "part3_high_pr_auc_above_prevalence_all": part3["aggregate"]["high_pr_auc_above_prevalence_on_all"],
        })
        if reproduced_acc - baseline_p1 / 100 > 0.0 and not part2_all:
            verdict_parts["part2_holds_after_fix"] = False
            verdict_parts["verdict"] = (
                "NOT REAL: the 66.13% headline is a leakage + broken-split artifact. "
                "Faithful reproduction gives ~66.13% on a test slice that is ~90% one class "
                "(majority baseline ~89.66%), and the corrected chronological evaluation does not "
                "beat the majority baseline on all holdouts."
            )
        else:
            verdict_parts["verdict"] = "INCONCLUSIVE - see doc for per-holdout tables"
    print(json.dumps(verdict_parts, indent=2))

    summary = {
        "generated_utc": pd.Timestamp.utcnow().isoformat(),
        "n_estimators": args.estimators,
        "deviation_note": (
            f"n_estimators={args.estimators} used for Parts 2-3 "
            f"({'same as v1' if args.estimators == 1000 else 'DIFFERENT from v1 1000 - explicit deviation'})"
        ),
        "part1_faithful_reproduction": part1,
        "part2_corrected_5000_row_frame": part2,
        "part3_corrected_139523_row_v3_frame": part3,
        "part4_leakage_audit": part4,
        "part5_verdict": verdict_parts,
    }
    out_path = Path("/tmp/adversarial_validation_results.json")
    out_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(f"\nJSON summary written to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
