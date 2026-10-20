#!/usr/bin/env python3
"""ML model evaluation v3 — honest, final verdict on LightGBM volatility-regime classifier.

Builds on v2 (fixes the 5 leakage bugs) with these additions:
  - 138k rows of real LSE BTC/USD 1m history (27.7x more than v2's 5,000)
  - Volatility regime per holdout (ATR + returns std) to diagnose instability
  - Real minutes_to_next_high_macro feature (ported from macro diagnostic)
  - Permutation importance (not gain) for the macro feature
  - Two-stage classifier variant (LOW-vs-not-LOW, then MED-vs-HIGH)
  - explicit class weights vs balanced

class_weight='balanced' was ALREADY in v2 (line 360) and the Holdout C failure
still occurred — so we also try explicit weights and a two-stage approach.

All artifacts saved with _v3 suffix. Does NOT overwrite v2 until independently
re-verified. As scoped, does not touch kalshi_client, cross_venue_arb, models/,
or any test_kalshi*/test_cross_venue* files.

Usage:
    python scripts/fill_and_test_arb_v3.py
"""

from __future__ import annotations

import argparse
import __main__ as _pickle_main
import json
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import joblib
import lightgbm as lgb
import numpy as np
import pandas as pd
from lightgbm import LGBMClassifier
from sklearn.inspection import permutation_importance
from sklearn.metrics import accuracy_score, confusion_matrix, f1_score, log_loss

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from polymarket_arb.two_stage_model_compat import (  # noqa: E402
    TwoStagePredictionModel,
)

UTC = timezone.utc

KRONOS_ACCURACY_PCT = 43.75
PERMUTATION_IMPORTANCE_REPEATS = 20
HORIZON_MINUTES = 15

# Feature set — matches v2 plus the real macro feature
FEATURE_COLUMNS = [
    "open",
    "high",
    "low",
    "close",
    "volume",
    "log_volume",
    "return_1m_bps",
    "rolling_vol_5m_bps",
    "rolling_vol_15m_bps",
    "rolling_vol_60m_bps",
    "atr_14m_bps",
    "atr_60m_bps",
    "session_bucket",
    "hour_sin",
    "hour_cos",
    "dayofweek_sin",
    "dayofweek_cos",
    "bollinger_band_width",
    "rsi_14",
    "macd_line",
    "macd_signal",
    "price_trend_5m",
    "price_trend_15m",
    "volume_trend_5m",
    "minutes_to_next_high_macro",
    # Added 2026-08-21 (ML_REGIME_BAKEOFF_2026-08-20.md follow-up 2): both have
    # confirmed real history over the full training window, unlike order-book
    # depth or open-interest history which only expose live/recent snapshots.
    # Tried in training, made no meaningful difference (67.45% vs 67.51% mean
    # holdout, within noise) — kept in the feature set since there's no cost
    # to computing them, but the deployed model was NOT switched to the
    # version trained with them (see report: no genuine improvement, and the
    # discipline here is never swap on a null result).
    "fear_greed_value",
    "funding_rate_bps",
    # Added 2026-08-21 (ML_REGIME_BAKEOFF_2026-08-20.md follow-up 3): real
    # order-book depth + open interest/long-short + cross-asset history, from
    # Binance Vision's bulk archive and yfinance — see
    # scripts/fetch_binance_vision_signals.py. Follow-up 2 wrongly rejected
    # order-book depth and open interest as infeasible; that verdict was
    # about the live REST endpoints (snapshot/recent-only), not this bulk
    # per-day archive, which has real history back to (at least) 2025-01-01.
    "depth_imbalance_1pct",
    "depth_imbalance_5pct",
    "depth_total_notional_1pct",
    "open_interest",
    "oi_change_1h_pct",
    "toptrader_long_short_ratio",
    "taker_long_short_vol_ratio",
    "eth_return_1d_lag",
    "eth_realized_vol_5d_lag",
]
CLASS_NAMES = ["LOW", "MED", "HIGH"]
TARGET_COLUMN = "forward_realized_vol_bps"


@dataclass(frozen=True)
class Partition:
    name: str
    start: str
    end: str
    rows: int


# =============================================================================
# Macro calendar — ported from scripts/diagnose_lightgbm_macro_regimes.py
# =============================================================================


def _to_utc_timestamp(value: Any) -> pd.Timestamp:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return pd.to_datetime(value, unit="s", utc=True)
    return pd.to_datetime(value, utc=True)


def load_high_macro_events(path: Path) -> tuple[list[pd.Timestamp], dict[str, Any]]:
    """Load HIGH-impact event times from the local macro calendar."""
    payload = json.loads(path.read_text())
    events = payload.get("events", []) if isinstance(payload, dict) else payload
    if not isinstance(events, list):
        raise ValueError("macro calendar must contain an events list")
    high_times: list[pd.Timestamp] = []
    counts: dict[str, int] = {}
    for event in events:
        if not isinstance(event, dict):
            continue
        impact = str(event.get("impact", "")).upper()
        counts[impact] = counts.get(impact, 0) + 1
        if impact != "HIGH" or event.get("timestamp") is None:
            continue
        try:
            high_times.append(_to_utc_timestamp(event["timestamp"]))
        except (TypeError, ValueError, OverflowError):
            continue
    high_times = sorted(set(high_times))
    metadata = {
        "path": str(path),
        "impact_counts": counts,
        "high_event_count": len(high_times),
        "high_event_range_utc": {
            "start": high_times[0].isoformat() if high_times else None,
            "end": high_times[-1].isoformat() if high_times else None,
        },
    }
    return high_times, metadata


def minutes_to_next_event(
    timestamps: pd.Series, high_events: list[pd.Timestamp]
) -> pd.Series:
    """Compute minutes to next HIGH macro event. NaN if no future event exists."""
    if not high_events:
        return pd.Series(np.nan, index=timestamps.index, dtype="float64")
    event_ns = np.array([event.value for event in high_events], dtype=np.int64)
    candle_ns = timestamps.astype("int64").to_numpy()
    positions = np.searchsorted(event_ns, candle_ns, side="left")
    result = np.full(len(timestamps), np.nan, dtype=float)
    valid = positions < len(event_ns)
    result[valid] = (event_ns[positions[valid]] - candle_ns[valid]) / 60_000_000_000
    return pd.Series(result, index=timestamps.index)


# =============================================================================
# External signals (2026-08-21+) — see scripts/fetch_external_signals.py /
# scripts/fetch_binance_vision_signals.py and ML_REGIME_BAKEOFF_2026-08-20.md
# follow-ups 2 and 3. Fear & Greed and funding rate came from live REST APIs
# with real full history. Order-book depth and open interest/long-short
# ratio come from Binance Vision's bulk archive (data.binance.vision) — a
# static per-day file dump, NOT the live REST endpoints (which really are
# snapshot/recent-only, as follow-up 2 found).
# =============================================================================


def load_fear_greed_index(path: Path) -> pd.DataFrame:
    """Daily Fear & Greed value as a sorted (timestamp_utc, value) frame."""
    payload = json.loads(path.read_text())
    rows = payload.get("data", [])
    df = pd.DataFrame(rows)
    # format="ISO8601": funding-rate timestamps mix sub-second precision
    # (some rows carry milliseconds, some land exactly on the second) —
    # a single inferred strptime format chokes on that mix.
    df["timestamp_utc"] = pd.to_datetime(df["timestamp_utc"], utc=True, format="ISO8601")
    df["value"] = df["value"].astype(float)
    return df[["timestamp_utc", "value"]].sort_values("timestamp_utc").reset_index(drop=True)


def load_funding_rate(path: Path) -> pd.DataFrame:
    """8-hourly Binance BTCUSDT funding rate as a sorted (timestamp_utc, funding_rate) frame."""
    payload = json.loads(path.read_text())
    rows = payload.get("data", [])
    df = pd.DataFrame(rows)
    # format="ISO8601": funding-rate timestamps mix sub-second precision
    # (some rows carry milliseconds, some land exactly on the second) —
    # a single inferred strptime format chokes on that mix.
    df["timestamp_utc"] = pd.to_datetime(df["timestamp_utc"], utc=True, format="ISO8601")
    df["funding_rate"] = df["funding_rate"].astype(float)
    return df[["timestamp_utc", "funding_rate"]].sort_values("timestamp_utc").reset_index(drop=True)


def load_binance_bookdepth(path: Path) -> pd.DataFrame:
    """Order-book depth at +/-1..5% from mid, pivoted from Binance Vision's
    long format (timestamp, percentage, depth, notional) to one row per
    snapshot (~every 35s).

    Each percentage level's depth/notional is CUMULATIVE from mid out to
    that level (verified against a real sample: depth increases monotonically
    as |percentage| grows), so the 1%/5% levels are used directly rather than
    summed across levels. `percentage` is float64 and real data includes a
    few extra levels beyond +/-1..5 on some days (observed -0.2/0.2) — select
    the exact float keys from the pivoted MultiIndex directly (not a
    string-formatted lookup, which silently mismatched -1.0 against "-1" and
    produced 100% NaN on first attempt) and ignore anything else.
    """
    df = pd.read_parquet(path)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, format="ISO8601")
    wide = df.pivot_table(index="timestamp", columns="percentage", values=["depth", "notional"])

    def _level(field: str, level: float) -> pd.Series:
        key = (field, level)
        if key in wide.columns:
            return wide[key]
        return pd.Series(np.nan, index=wide.index)

    out = pd.DataFrame({
        "timestamp_utc": wide.index,
        "depth_bid_1pct": _level("depth", -1.0).to_numpy(),
        "depth_ask_1pct": _level("depth", 1.0).to_numpy(),
        "depth_bid_5pct": _level("depth", -5.0).to_numpy(),
        "depth_ask_5pct": _level("depth", 5.0).to_numpy(),
        "notional_bid_1pct": _level("notional", -1.0).to_numpy(),
        "notional_ask_1pct": _level("notional", 1.0).to_numpy(),
    })
    return out.sort_values("timestamp_utc").reset_index(drop=True)


def load_binance_oi_longshort(path: Path) -> pd.DataFrame:
    """Open interest + long/short positioning at 5-minute granularity.

    oi_change_1h_pct is computed here (on the metrics' own native timeline,
    via a self merge_asof) rather than after merging onto 1-minute candles —
    a fixed 12-row lookback would silently misbehave across any gap in the
    5-minute series; merge_asof on real elapsed time doesn't.
    """
    df = pd.read_parquet(path)
    df["timestamp_utc"] = pd.to_datetime(df["create_time"], utc=True, format="ISO8601")
    df = df.sort_values("timestamp_utc").reset_index(drop=True)
    df["open_interest"] = df["sum_open_interest"].astype(float)
    df["toptrader_long_short_ratio"] = df["sum_toptrader_long_short_ratio"].astype(float)
    df["taker_long_short_vol_ratio"] = df["sum_taker_long_short_vol_ratio"].astype(float)

    lagged = df[["timestamp_utc", "open_interest"]].copy()
    lagged["timestamp_utc"] = lagged["timestamp_utc"] + pd.Timedelta(hours=1)
    merged = pd.merge_asof(
        df[["timestamp_utc"]], lagged, on="timestamp_utc", direction="backward",
        tolerance=pd.Timedelta(minutes=10),
    )
    df["_oi_1h_ago"] = merged["open_interest"].to_numpy()
    df["oi_change_1h_pct"] = (df["open_interest"] - df["_oi_1h_ago"]) / df["_oi_1h_ago"] * 100.0

    return df[[
        "timestamp_utc", "open_interest", "oi_change_1h_pct",
        "toptrader_long_short_ratio", "taker_long_short_vol_ratio",
    ]].sort_values("timestamp_utc").reset_index(drop=True)


def load_cross_asset_daily(path: Path) -> pd.DataFrame:
    """ETH-USD daily return/vol, shifted forward one day before it's
    considered "available" — a day's own close isn't known until that day
    ends, so a candle merge_asof'd against the SAME calendar day would be
    peeking at same-day-still-forming information. Shifting the join key
    to (date + 1 day) means a candle only ever sees yesterday's-or-earlier
    finalized numbers.
    """
    df = pd.read_parquet(path)
    df["date"] = pd.to_datetime(df["date"], utc=True)
    df = df.sort_values("date").reset_index(drop=True)
    log_return = np.log(df["eth_close"]).diff()
    df["eth_return_1d_lag"] = log_return * 100.0
    df["eth_realized_vol_5d_lag"] = log_return.rolling(5, min_periods=5).std() * 100.0
    df["timestamp_utc"] = df["date"] + pd.Timedelta(days=1)
    return df[["timestamp_utc", "eth_return_1d_lag", "eth_realized_vol_5d_lag"]].dropna().sort_values(
        "timestamp_utc"
    ).reset_index(drop=True)


def _asof_merge_source(
    df: pd.DataFrame, source_df: Optional[pd.DataFrame], value_cols: list[str]
) -> pd.DataFrame:
    """Backward-merge every value column of one external source onto df's
    "timestamp", or leave those columns present-but-NaN if the source is
    unavailable (same convention as minutes_to_next_high_macro when
    high_events is empty — callers relying on a fixed FEATURE_COLUMNS shape
    are never surprised by a missing column).

    direction="backward" picks the most recent known value AT OR BEFORE each
    candle's timestamp — what a live bot would actually have on hand at that
    moment, never a future one.
    """
    if source_df is not None and not source_df.empty:
        merged = pd.merge_asof(
            df, source_df, left_on="timestamp", right_on="timestamp_utc", direction="backward"
        )
        for col in value_cols:
            df[col] = merged[col].to_numpy()
    else:
        for col in value_cols:
            df[col] = np.nan
    return df


def merge_external_signals(
    df: pd.DataFrame,
    fear_greed_df: Optional[pd.DataFrame] = None,
    funding_rate_df: Optional[pd.DataFrame] = None,
    bookdepth_df: Optional[pd.DataFrame] = None,
    oi_longshort_df: Optional[pd.DataFrame] = None,
    cross_asset_df: Optional[pd.DataFrame] = None,
) -> pd.DataFrame:
    """Backward-merge every external source onto each candle's timestamp.

    See _asof_merge_source for the no-leakage/missing-source convention
    shared by all five sources. funding_rate is scaled to bps (x10000) here
    since load_funding_rate returns the raw fractional rate; every other
    loader already returns its columns in final feature units.
    """
    df = df.sort_values("timestamp").reset_index(drop=True)

    df = _asof_merge_source(df, fear_greed_df, ["value"])
    df = df.rename(columns={"value": "fear_greed_value"})

    df = _asof_merge_source(df, funding_rate_df, ["funding_rate"])
    df["funding_rate_bps"] = df.pop("funding_rate") * 10000.0

    df = _asof_merge_source(df, bookdepth_df, [
        "depth_bid_1pct", "depth_ask_1pct", "depth_bid_5pct", "depth_ask_5pct",
        "notional_bid_1pct", "notional_ask_1pct",
    ])
    with np.errstate(divide="ignore", invalid="ignore"):
        df["depth_imbalance_1pct"] = (
            (df["depth_bid_1pct"] - df["depth_ask_1pct"])
            / (df["depth_bid_1pct"] + df["depth_ask_1pct"])
        )
        df["depth_imbalance_5pct"] = (
            (df["depth_bid_5pct"] - df["depth_ask_5pct"])
            / (df["depth_bid_5pct"] + df["depth_ask_5pct"])
        )
    df["depth_total_notional_1pct"] = df["notional_bid_1pct"] + df["notional_ask_1pct"]
    df = df.drop(columns=[
        "depth_bid_1pct", "depth_ask_1pct", "depth_bid_5pct", "depth_ask_5pct",
        "notional_bid_1pct", "notional_ask_1pct",
    ])

    df = _asof_merge_source(df, oi_longshort_df, [
        "open_interest", "oi_change_1h_pct", "toptrader_long_short_ratio", "taker_long_short_vol_ratio",
    ])

    df = _asof_merge_source(df, cross_asset_df, ["eth_return_1d_lag", "eth_realized_vol_5d_lag"])

    return df


# =============================================================================
# Feature engineering — time-aware rolling windows (no row-index leakage)
# =============================================================================


def calculate_technical_indicators(
    df: pd.DataFrame, high_events: list[pd.Timestamp]
) -> pd.DataFrame:
    df = df.copy().sort_values("timestamp").reset_index(drop=True)
    log_return = np.log(df["close"]).diff()

    true_range = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - df["close"].shift()).abs(),
            (df["low"] - df["close"].shift()).abs(),
        ],
        axis=1,
    ).max(axis=1)

    df["log_volume"] = np.log1p(df["volume"])
    df["return_1m_bps"] = log_return * 10000

    # Time-aware rolling stats (using timestamp index so gaps don't pollute)
    lr_ts = log_return.set_axis(df["timestamp"])
    tr_ts = true_range.set_axis(df["timestamp"])

    df["rolling_vol_5m_bps"] = (
        lr_ts.rolling("5min", min_periods=5).std().to_numpy() * 10000
    )
    df["rolling_vol_15m_bps"] = (
        lr_ts.rolling("15min", min_periods=15).std().to_numpy() * 10000
    )
    df["rolling_vol_60m_bps"] = (
        lr_ts.rolling("60min", min_periods=60).std().to_numpy() * 10000
    )
    df["atr_14m_bps"] = (
        tr_ts.rolling("14min", min_periods=14).mean().to_numpy() / df["close"] * 10000
    )
    df["atr_60m_bps"] = (
        tr_ts.rolling("60min", min_periods=60).mean().to_numpy() / df["close"] * 10000
    )

    # Bollinger, RSI, and the trend features below all originally used plain
    # integer-positional .rolling(window=N) — unlike rolling_vol_*/atr_* above,
    # that has no gap protection: a window straddling a multi-day data gap
    # silently blends stale pre-gap prices into a post-gap value. Switched to
    # the same timestamp-indexed time-based rolling pattern used for
    # lr_ts/tr_ts above (a gap just means fewer periods in the window, not
    # blended ones).
    close_ts = df["close"].set_axis(df["timestamp"])
    volume_ts = df["volume"].set_axis(df["timestamp"])

    df["bb_middle"] = close_ts.rolling("20min", min_periods=20).mean().to_numpy()
    df["bb_std"] = close_ts.rolling("20min", min_periods=20).std().to_numpy()
    df["bollinger_band_width"] = (df["bb_std"] / df["bb_middle"]) * 100

    # RSI
    delta_ts = close_ts.diff()
    gain = delta_ts.where(delta_ts > 0, 0).rolling("14min", min_periods=14).mean()
    loss = (-delta_ts.where(delta_ts < 0, 0)).rolling("14min", min_periods=14).mean()
    rs = (gain / loss.replace(0, np.nan)).to_numpy()
    df["rsi_14"] = 100 - (100 / (1 + rs))

    # MACD — EWM decay is recursive per-ROW (not per elapsed time), so
    # `adjust=False` with plain `span=` also mis-weights the first row after a
    # gap as if it were merely one minute past the pre-gap state. `times=`
    # makes the decay time-aware instead; halflife_minutes below is the
    # elapsed-time equivalent of the original span-based decay rates
    # (halflife = -ln(2) / ln(1 - 2/(span+1))), so the feature's timescale is
    # unchanged for regularly-spaced data — it only differs at gaps.
    def _ewm_time_aware(series: pd.Series, span: int) -> pd.Series:
        alpha = 2.0 / (span + 1)
        halflife_minutes = -np.log(2) / np.log(1 - alpha)
        return series.ewm(
            halflife=pd.Timedelta(minutes=halflife_minutes),
            times=df["timestamp"],
        ).mean()

    exp1 = _ewm_time_aware(df["close"], 12)
    exp2 = _ewm_time_aware(df["close"], 26)
    df["macd_line"] = exp1 - exp2
    df["macd_signal"] = _ewm_time_aware(df["macd_line"], 9)

    # Linear-regression slope features
    for window in [5, 15]:
        df[f"price_trend_{window}m"] = (
            close_ts.rolling(f"{window}min", min_periods=window)
            .apply(
                lambda x: np.polyfit(np.arange(len(x)), x, 1)[0]
                if len(x) == window
                else np.nan,
                raw=False,
            )
            .to_numpy()
        )
    df["volume_trend_5m"] = (
        volume_ts.rolling("5min", min_periods=5)
        .apply(
            lambda x: np.polyfit(np.arange(len(x)), x, 1)[0] if len(x) == 5 else np.nan,
            raw=False,
        )
        .to_numpy()
    )

    # Temporal
    df["hour"] = df["timestamp"].dt.hour
    df["dayofweek"] = df["timestamp"].dt.dayofweek
    df["hour_sin"] = np.sin(2 * np.pi * df["hour"] / 24)
    df["hour_cos"] = np.cos(2 * np.pi * df["hour"] / 24)
    df["dayofweek_sin"] = np.sin(2 * np.pi * df["dayofweek"] / 7)
    df["dayofweek_cos"] = np.cos(2 * np.pi * df["dayofweek"] / 7)
    df["session_bucket"] = pd.cut(
        df["hour"],
        bins=[-1, 6, 12, 20, 24],
        labels=False,
        include_lowest=True,
        right=True,
    ).astype("int8")

    # Real macro calendar feature (TASK 4)
    df["minutes_to_next_high_macro"] = minutes_to_next_event(
        df["timestamp"], high_events
    )

    return df


def calculate_forward_volatility(
    df: pd.DataFrame, horizon_minutes: int
) -> pd.DataFrame:
    df = df.copy()
    log_return = np.log(df["close"]).diff()
    future_squared_returns = sum(
        log_return.pow(2).shift(-step) for step in range(1, horizon_minutes + 1)
    )
    contiguous_future = (
        df["timestamp"]
        .shift(-horizon_minutes)
        .sub(df["timestamp"])
        .eq(pd.Timedelta(minutes=horizon_minutes))
    )
    df[TARGET_COLUMN] = np.sqrt(future_squared_returns) * 10000
    df.loc[~contiguous_future, TARGET_COLUMN] = np.nan
    return df


# =============================================================================
# Splits + labeling (same as v2 — train-only cutoffs, time-disjoint holdouts)
# =============================================================================


def split_time_disjoint(
    df: pd.DataFrame, minimum_rows_per_partition: int = 1000
) -> Tuple[Dict[str, pd.DataFrame], List[Partition]]:
    if len(df) < minimum_rows_per_partition * 5:
        raise ValueError(
            f"need at least {minimum_rows_per_partition * 5} eligible candles for five disjoint partitions; got {len(df)}"
        )
    boundaries = [int(len(df) * f) for f in (0.50, 0.625, 0.75, 0.875)]
    raw_parts = {
        "train": df.iloc[: boundaries[0]].copy(),
        "validation": df.iloc[boundaries[0] : boundaries[1]].copy(),
        "holdout_a": df.iloc[boundaries[1] : boundaries[2]].copy(),
        "holdout_b": df.iloc[boundaries[2] : boundaries[3]].copy(),
        "holdout_c": df.iloc[boundaries[3] :].copy(),
    }
    if min(len(p) for p in raw_parts.values()) < minimum_rows_per_partition:
        raise ValueError(
            "chronological partition has fewer rows than the configured minimum"
        )
    partitions = [
        Partition(
            name=n,
            start=p["timestamp"].iloc[0].isoformat(),
            end=p["timestamp"].iloc[-1].isoformat(),
            rows=len(p),
        )
        for n, p in raw_parts.items()
    ]
    return raw_parts, partitions


def label_from_training_quantiles(
    parts: Dict[str, pd.DataFrame],
) -> Tuple[Dict[str, pd.DataFrame], Dict[str, float]]:
    train_target = parts["train"][TARGET_COLUMN]
    low_high_cutoff, high_cutoff = train_target.quantile([1 / 3, 2 / 3]).tolist()
    if (
        not np.isfinite([low_high_cutoff, high_cutoff]).all()
        or low_high_cutoff >= high_cutoff
    ):
        raise ValueError(
            "training realized-volatility quantiles do not form three distinct classes"
        )
    labelled: Dict[str, pd.DataFrame] = {}
    for name, part in parts.items():
        lp = part.copy()
        lp["target"] = np.select(
            [lp[TARGET_COLUMN] <= low_high_cutoff, lp[TARGET_COLUMN] <= high_cutoff],
            [0, 1],
            default=2,
        ).astype(int)
        labelled[name] = lp
    return labelled, {
        "low_to_med_bps": float(low_high_cutoff),
        "med_to_high_bps": float(high_cutoff),
    }


# =============================================================================
# Metrics helpers
# =============================================================================


def _classification_metrics(
    target, prediction: np.ndarray, probabilities: Optional[np.ndarray] = None
) -> Dict[str, Any]:
    target_arr = (
        target.to_numpy() if hasattr(target, "to_numpy") else np.asarray(target)
    )
    acc = accuracy_score(target_arr, prediction) * 100
    metrics = {
        "rows": int(len(target_arr)),
        "correct_predictions": int(np.sum(target_arr == prediction)),
        "accuracy_pct": round(float(acc), 6),
        "macro_f1": round(
            float(
                f1_score(
                    target_arr,
                    prediction,
                    labels=[0, 1, 2],
                    average="macro",
                    zero_division=0,
                )
            ),
            6,
        ),
        "class_counts": {
            CLASS_NAMES[i]: int((target_arr == i).sum()) for i in range(3)
        },
        "all_three_classes_observed": bool(
            all((target_arr == i).any() for i in range(3))
        ),
        "confusion_matrix_rows_low_med_high": confusion_matrix(
            target_arr, prediction, labels=[0, 1, 2]
        ).tolist(),
        "vs_kronos_43_75_pp": round(float(acc - KRONOS_ACCURACY_PCT), 6),
    }
    if probabilities is not None:
        metrics["multiclass_log_loss"] = round(
            float(log_loss(target_arr, probabilities, labels=[0, 1, 2])), 6
        )
    return metrics


def _majority_baseline_metrics(
    part: pd.DataFrame, majority_class: int
) -> Dict[str, Any]:
    return _classification_metrics(
        part["target"], np.full(len(part), majority_class, dtype=int)
    )


def _volatility_regime(part: pd.DataFrame) -> Dict[str, Any]:
    """TASK 2 — volatility characteristics of a partition window."""
    realized_vol_mean_bps = (
        float(part["return_1m_bps"].mean())
        if "return_1m_bps" in part.columns
        else np.nan
    )
    returns_std_bps = (
        float(part["return_1m_bps"].std())
        if "return_1m_bps" in part.columns
        else np.nan
    )
    atr_14m_mean = (
        float(part["atr_14m_bps"].mean()) if "atr_14m_bps" in part.columns else np.nan
    )
    atr_60m_mean = (
        float(part["atr_60m_bps"].mean()) if "atr_60m_bps" in part.columns else np.nan
    )
    target_mean = (
        float(part[TARGET_COLUMN].mean()) if TARGET_COLUMN in part.columns else np.nan
    )
    target_std = (
        float(part[TARGET_COLUMN].std()) if TARGET_COLUMN in part.columns else np.nan
    )
    return {
        "forward_vol_target_mean_bps": round(target_mean, 6)
        if np.isfinite(target_mean)
        else None,
        "forward_vol_target_std_bps": round(target_std, 6)
        if np.isfinite(target_std)
        else None,
        "return_1m_bps_std": round(returns_std_bps, 6)
        if np.isfinite(returns_std_bps)
        else None,
        "return_1m_bps_mean_abs_bps": round(abs(realized_vol_mean_bps), 6)
        if np.isfinite(realized_vol_mean_bps)
        else None,
        "atr_14m_bps_mean": round(atr_14m_mean, 6)
        if np.isfinite(atr_14m_mean)
        else None,
        "atr_60m_bps_mean": round(atr_60m_mean, 6)
        if np.isfinite(atr_60m_mean)
        else None,
    }


# =============================================================================
# Precision-at-confidence (TASK 2 part 2) — financial series are often only
# predictable in a minority of cases. A model that abstains most of the time
# but is highly precise when it fires may be more useful than one forced to
# guess on every row.
# =============================================================================

CONFIDENCE_THRESHOLDS = (0.50, 0.60, 0.70, 0.80, 0.90)


def _precision_at_confidence(
    target: np.ndarray, probs: np.ndarray, threshold: float
) -> Dict[str, Any]:
    """Precision when the model's top-class probability >= threshold.

    Returns: how many rows fired, what fraction were correct (precision),
    and the class-conditional precision (per-class correct / fired-of-class).
    Abstained rows (max prob < threshold) are excluded from precision.
    """
    target = np.asarray(target)
    max_probs = probs.max(axis=1)
    preds = probs.argmax(axis=1)
    fired_mask = max_probs >= threshold
    fired_n = int(fired_mask.sum())
    if fired_n == 0:
        return {
            "threshold": threshold,
            "fired_rows": 0,
            "fired_pct": 0.0,
            "precision_pct": None,  # undefined: no predictions made
            "per_class_precision": {CLASS_NAMES[i]: None for i in range(3)},
            "abstained_rows": int((~fired_mask).sum()),
            "abstained_pct": round(float((~fired_mask).mean()) * 100, 6),
        }
    fired_correct = int((preds[fired_mask] == target[fired_mask]).sum())
    precision = fired_correct / fired_n * 100
    per_class: Dict[str, Any] = {}
    for i, name in enumerate(CLASS_NAMES):
        cls_fired = int(((preds == i) & fired_mask).sum())
        if cls_fired == 0:
            per_class[name] = None
            continue
        cls_correct = int(((preds == i) & (target == i) & fired_mask).sum())
        per_class[name] = round(cls_correct / cls_fired * 100, 6)
    return {
        "threshold": threshold,
        "fired_rows": fired_n,
        "fired_pct": round(fired_n / len(target) * 100, 6),
        "precision_pct": round(precision, 6),
        "per_class_precision": per_class,
        "abstained_rows": int((~fired_mask).sum()),
        "abstained_pct": round(float((~fired_mask).mean()) * 100, 6),
    }


def _precision_at_confidence_sweep(
    target: np.ndarray, probs: Optional[np.ndarray]
) -> Dict[str, Any]:
    """Compute precision across all confidence thresholds. Returns empty if
    probabilities are unavailable (e.g. two-stage variant exposes only hard preds)."""
    if probs is None:
        return {"available": False, "reason": "no_probability_output", "thresholds": []}
    target = np.asarray(target)
    return {
        "available": True,
        "thresholds": [
            _precision_at_confidence(target, probs, t) for t in CONFIDENCE_THRESHOLDS
        ],
        "overall_accuracy_pct": round(
            float((probs.argmax(axis=1) == target).mean() * 100), 6
        ),
    }


def _two_stage_confidence_sweep(
    s1_proba: np.ndarray,
    s1_best: int,
    s2_model: Any,
    s2_best: int,
    part: pd.DataFrame,
    feature_cols: List[str],
    target: np.ndarray,
) -> Dict[str, Any]:
    """Approximate confidence for the two-stage model as the joint probability of
    the chosen leaf: P(notLOW) * P(leaf | notLOW). This is an honest
    approximation — the two stages are conditional, so the product is a
    reasonable confidence proxy. Thresholds use the joint max prob."""
    s1_proba = np.asarray(s1_proba)
    preds = np.zeros(len(part), dtype=int)
    joint_probs = np.zeros((len(part), 3), dtype=float)
    # LOW confidence = P(LOW) = 1 - P(notLOW)
    p_notlow = s1_proba[:, 1]
    joint_probs[:, 0] = 1.0 - p_notlow
    notlow_idx = s1_proba.argmax(axis=1) == 1
    if notlow_idx.any():
        s2_proba = s2_model.predict_proba(
            part.loc[notlow_idx, feature_cols], num_iteration=s2_best
        )
        # s2_proba columns: [MED, HIGH] i.e. classes 1 and 2
        joint_probs[notlow_idx, 1] = p_notlow[notlow_idx] * s2_proba[:, 0]
        joint_probs[notlow_idx, 2] = p_notlow[notlow_idx] * s2_proba[:, 1]
        s2_preds = np.where(s2_proba.argmax(axis=1) == 0, 1, 2)
        preds[notlow_idx] = s2_preds
    return _precision_at_confidence_sweep(target, joint_probs)


# =============================================================================
# Variant 1: flat 3-class model (baseline matching v2 with bigger data)
# =============================================================================


def train_flat_model(
    parts: Dict[str, pd.DataFrame], feature_cols: List[str], class_weight: Any, tag: str
) -> Tuple[Optional[LGBMClassifier], Dict[str, Any]]:
    params = {
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
        "class_weight": class_weight,
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
    majority_class = int(
        parts["train"]["target"].value_counts().reindex(range(3), fill_value=0).idxmax()
    )
    model_metrics, majority_metrics, precision_metrics = {}, {}, {}
    for name in ["train", "validation", "holdout_a", "holdout_b", "holdout_c"]:
        part = parts[name]
        probs = model.predict_proba(
            part[feature_cols], num_iteration=model.best_iteration_
        )
        preds = probs.argmax(axis=1)
        m = _classification_metrics(part["target"].to_numpy(), preds, probs)
        mb = _majority_baseline_metrics(part, majority_class)
        m["vs_train_majority_baseline_pp"] = round(
            m["accuracy_pct"] - mb["accuracy_pct"], 6
        )
        model_metrics[name] = m
        majority_metrics[name] = mb
        # TASK 2 part 2: precision-at-confidence sweep on this partition
        precision_metrics[name] = _precision_at_confidence_sweep(
            part["target"].to_numpy(), probs
        )
    return model, {
        "variant": "flat_3class",
        "tag": tag,
        "class_weight": str(class_weight),
        "best_iteration": int(model.best_iteration_ or model.n_estimators),
        "metrics": model_metrics,
        "majority_baseline": {
            "fit_on": "train_only",
            "predicted_class": CLASS_NAMES[majority_class],
            "metrics": majority_metrics,
        },
        "precision_at_confidence": precision_metrics,
    }


# =============================================================================
# Variant 2: two-stage classifier (LOW vs not-LOW, then MED vs HIGH on not-LOW)
# =============================================================================

# The persisted v3 artifact was created when this class lived in ``__main__``.
# Preserve a narrowly-scoped compatibility alias so joblib can load that artifact
# under pytest, scripts, and interactive Python without retraining or rewriting it.
if not hasattr(_pickle_main, "TwoStagePredictionModel"):
    _pickle_main.TwoStagePredictionModel = TwoStagePredictionModel


def train_two_stage_model(
    parts: Dict[str, pd.DataFrame], feature_cols: List[str], tag: str
) -> Tuple[Optional[TwoStagePredictionModel], Dict[str, Any]]:
    """Stage 1: binary {LOW=0, notLOW=1}. Stage 2: binary {MED=0, HIGH=1} on notLOW rows."""
    base_params = {
        "objective": "binary",
        "boosting_type": "gbdt",
        "n_estimators": 1000,
        "learning_rate": 0.05,
        "num_leaves": 63,
        "max_depth": 5,
        "min_child_samples": 10,
        "subsample": 0.8,
        "colsample_bytree": 0.8,
        "random_state": 20260804,
        "n_jobs": -1,
        "verbosity": -1,
    }

    # Stage 1: LOW (target==0) vs notLOW (target in {1,2})
    s1_train_y = (parts["train"]["target"] != 0).astype(int)
    s1_val_y = (parts["validation"]["target"] != 0).astype(int)
    s1 = LGBMClassifier(**base_params, metric="binary_logloss", class_weight="balanced")
    s1.fit(
        parts["train"][feature_cols],
        s1_train_y,
        eval_set=[(parts["validation"][feature_cols], s1_val_y)],
        callbacks=[lgb.early_stopping(50, verbose=False)],
    )
    s1_best = int(s1.best_iteration_ or s1.n_estimators)

    # Stage 2: MED vs HIGH on notLOW rows (target in {1,2} -> {0,1})
    notlow_train = parts["train"]["target"].isin([1, 2])
    notlow_val = parts["validation"]["target"].isin([1, 2])
    if notlow_train.sum() < 50 or notlow_val.sum() < 10:
        return None, {
            "variant": "two_stage",
            "tag": tag,
            "status": "insufficient_notlow_rows",
            "train_notlow": int(notlow_train.sum()),
            "val_notlow": int(notlow_val.sum()),
        }
    s2_train_y = (parts["train"].loc[notlow_train, "target"] == 2).astype(int)
    s2_val_y = (parts["validation"].loc[notlow_val, "target"] == 2).astype(int)
    s2 = LGBMClassifier(**base_params, metric="binary_logloss", class_weight="balanced")
    s2.fit(
        parts["train"].loc[notlow_train, feature_cols],
        s2_train_y,
        eval_set=[(parts["validation"].loc[notlow_val, feature_cols], s2_val_y)],
        callbacks=[lgb.early_stopping(50, verbose=False)],
    )
    s2_best = int(s2.best_iteration_ or s2.n_estimators)

    two_stage = TwoStagePredictionModel(s1, s2, s1_best, s2_best, feature_cols)

    # Evaluate
    majority_class = int(
        parts["train"]["target"].value_counts().reindex(range(3), fill_value=0).idxmax()
    )
    model_metrics, majority_metrics, precision_metrics = {}, {}, {}
    for name in ["train", "validation", "holdout_a", "holdout_b", "holdout_c"]:
        part = parts[name]
        joint_probs = two_stage.predict_proba_proxy(part)
        preds = joint_probs.argmax(axis=1).astype(int)
        m = _classification_metrics(part["target"].to_numpy(), preds, None)
        mb = _majority_baseline_metrics(part, majority_class)
        m["vs_train_majority_baseline_pp"] = round(
            m["accuracy_pct"] - mb["accuracy_pct"], 6
        )
        model_metrics[name] = m
        majority_metrics[name] = mb
        precision_metrics[name] = _precision_at_confidence_sweep(
            part["target"].to_numpy(), joint_probs
        )
    return two_stage, {
        "variant": "two_stage",
        "tag": tag,
        "best_iteration_stage1": s1_best,
        "best_iteration_stage2": s2_best,
        "train_notlow_rows": int(notlow_train.sum()),
        "val_notlow_rows": int(notlow_val.sum()),
        "metrics": model_metrics,
        "majority_baseline": {
            "fit_on": "train_only",
            "predicted_class": CLASS_NAMES[majority_class],
            "metrics": majority_metrics,
        },
        "precision_at_confidence": precision_metrics,
    }


# =============================================================================
# Main pipeline
# =============================================================================


def run_pipeline(
    candles_path: Path,
    macro_path: Path,
    output_artifact: Path,
    fear_greed_path: Optional[Path] = None,
    funding_rate_path: Optional[Path] = None,
    bookdepth_path: Optional[Path] = None,
    oi_longshort_path: Optional[Path] = None,
    cross_asset_path: Optional[Path] = None,
    horizon_minutes: int = HORIZON_MINUTES,
    model_tag: str = "v3",
) -> Dict[str, Any]:
    # Load data
    df = pd.read_parquet(candles_path)
    if "timestamp" not in df.columns:
        raise ValueError(f"parquet missing timestamp column; got {list(df.columns)}")
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df = (
        df.drop_duplicates(subset="timestamp")
        .sort_values("timestamp")
        .reset_index(drop=True)
    )
    print(
        f"[INFO] Loaded {len(df)} candles, {df['timestamp'].iloc[0]} to {df['timestamp'].iloc[-1]}"
    )

    # Load macro events
    high_events, macro_meta = load_high_macro_events(macro_path)
    print(
        f"[INFO] {macro_meta['high_event_count']} HIGH macro events, range {macro_meta['high_event_range_utc']}"
    )

    # Load external signals (2026-08-21) — both optional; missing means
    # fear_greed_value/funding_rate_bps end up all-NaN and get dropped by the
    # NaN filter below, same as any other unavailable feature.
    fear_greed_df = load_fear_greed_index(fear_greed_path) if fear_greed_path and fear_greed_path.is_file() else None
    funding_rate_df = load_funding_rate(funding_rate_path) if funding_rate_path and funding_rate_path.is_file() else None
    bookdepth_df = load_binance_bookdepth(bookdepth_path) if bookdepth_path and bookdepth_path.is_file() else None
    oi_longshort_df = load_binance_oi_longshort(oi_longshort_path) if oi_longshort_path and oi_longshort_path.is_file() else None
    cross_asset_df = load_cross_asset_daily(cross_asset_path) if cross_asset_path and cross_asset_path.is_file() else None
    if fear_greed_df is not None:
        print(f"[INFO] Fear & Greed: {len(fear_greed_df)} daily rows loaded")
    if funding_rate_df is not None:
        print(f"[INFO] Funding rate: {len(funding_rate_df)} rows loaded")
    if bookdepth_df is not None:
        print(f"[INFO] Book depth: {len(bookdepth_df)} snapshots loaded")
    if oi_longshort_df is not None:
        print(f"[INFO] OI/long-short: {len(oi_longshort_df)} rows loaded")
    if cross_asset_df is not None:
        print(f"[INFO] Cross-asset: {len(cross_asset_df)} rows loaded")

    # Build features + target
    df = calculate_technical_indicators(df, high_events)
    df = merge_external_signals(
        df, fear_greed_df, funding_rate_df, bookdepth_df, oi_longshort_df, cross_asset_df
    )
    df = calculate_forward_volatility(df, horizon_minutes)
    feature_cols = [c for c in FEATURE_COLUMNS if c in df.columns]
    df = df.dropna(subset=feature_cols + [TARGET_COLUMN])
    print(f"[INFO] Eligible feature rows after NaN drop: {len(df)}")

    # Check macro feature NaN coverage (TASK 4 truth)
    healthy_macro = (
        df["minutes_to_next_high_macro"].notna().sum()
        if "minutes_to_next_high_macro" in df.columns
        else 0
    )
    macro_coverage = healthy_macro / len(df) if len(df) else 0
    print(
        f"[INFO] minutes_to_next_high_macro: {healthy_macro}/{len(df)} non-NaN ({macro_coverage * 100:.2f}%)"
    )

    # Split + label
    raw_parts, partitions = split_time_disjoint(df, minimum_rows_per_partition=1000)
    parts, target_cutoffs = label_from_training_quantiles(raw_parts)
    recomputed = raw_parts["train"][TARGET_COLUMN].quantile([1 / 3, 2 / 3])
    cutoffs_match = bool(
        np.allclose(
            [target_cutoffs["low_to_med_bps"], target_cutoffs["med_to_high_bps"]],
            recomputed.to_numpy(),
            rtol=0.0,
            atol=0.01,
        )
    )
    print(f"[INFO] Partitions: {[p.name + ':' + str(p.rows) for p in partitions]}")
    print(
        f"[INFO] Target cutoffs (train only): LOW<{target_cutoffs['low_to_med_bps']:.2f} < MED <{target_cutoffs['med_to_high_bps']:.2f} < HIGH"
    )

    # Volatility regime per holdout (TASK 2)
    vol_regimes = {
        name: _volatility_regime(parts[name])
        for name in ["train", "validation", "holdout_a", "holdout_b", "holdout_c"]
    }

    # --- Variant A: flat model with class_weight='balanced' (matches v2) ---
    print("\n[Variant A] flat 3-class, class_weight='balanced' ...")
    model_a, result_a = train_flat_model(parts, feature_cols, "balanced", "A_balanced")

    # --- Variant B: flat model with explicit weights (boost min classes) ---
    # For Holdout C (95% LOW), boost MED+HIGH even harder
    print("[Variant B] flat 3-class, explicit weights {LOW:1, MED:3, HIGH:3} ...")
    model_b, result_b = train_flat_model(
        parts, feature_cols, {0: 1, 1: 3, 2: 3}, "B_explicit"
    )

    # --- Variant C: two-stage classifier ---
    print("[Variant C] two-stage LOW-vs-notLOW then MED-vs-HIGH ...")
    models_c, result_c = train_two_stage_model(parts, feature_cols, "C_two_stage")

    # Permutation importance for the macro feature (TASK 4) — using variant A on validation
    perm_rows = []
    if model_a is not None:
        print("\n[Permutation importance] computing on validation (20 repeats) ...")
        perm = permutation_importance(
            model_a,
            parts["validation"][feature_cols],
            parts["validation"]["target"],
            scoring="accuracy",
            n_repeats=PERMUTATION_IMPORTANCE_REPEATS,
            random_state=20260804,
            n_jobs=1,
        )
        perm_rows = sorted(
            (
                {
                    "feature": f,
                    "mean_accuracy_decrease": round(float(m), 8),
                    "std_accuracy_decrease": round(float(s), 8),
                }
                for f, m, s in zip(
                    feature_cols, perm.importances_mean, perm.importances_std
                )
            ),
            key=lambda r: r["mean_accuracy_decrease"],
            reverse=True,
        )
        # Report key features
        for r in perm_rows[:5]:
            print(
                f"  {r['feature']}: mean_acc_dec={r['mean_accuracy_decrease']:.6f} +/- {r['std_accuracy_decrease']:.6f}"
            )
        macro_perm = next(
            (r for r in perm_rows if r["feature"] == "minutes_to_next_high_macro"), None
        )
        if macro_perm:
            print(
                f"\n[MACRO FEATURE VERDICT] minutes_to_next_high_macro perm importance: {macro_perm['mean_accuracy_decrease']:.6f}"
            )

    # Holdout accuracies summary
    holdout_names = ["holdout_a", "holdout_b", "holdout_c"]
    summary_variants = {}
    for tag, result in [
        ("A_balanced", result_a),
        ("B_explicit", result_b),
        ("C_two_stage", result_c),
    ]:
        if "metrics" not in result:
            continue
        holdouts = [result["metrics"][n]["accuracy_pct"] for n in holdout_names]
        # TASK 2 part 2: precision-at-0.7 confidence summary per holdout
        p07 = {}
        for n in holdout_names:
            sweep = result.get("precision_at_confidence", {}).get(n, {})
            t07 = None
            if sweep.get("available"):
                for t in sweep["thresholds"]:
                    if t["threshold"] == 0.70:
                        t07 = t
                        break
            p07[n] = t07
        summary_variants[tag] = {
            "holdout_accuracies_pct": holdouts,
            "holdout_mean_pct": round(float(np.mean(holdouts)), 6),
            "holdout_stddev_pp": round(float(np.std(holdouts, ddof=0)), 6),
            "holdout_range_pp": round(float(max(holdouts) - min(holdouts)), 6),
            "holdout_c_vs_majority_pp": round(
                result["metrics"]["holdout_c"]["accuracy_pct"]
                - result["majority_baseline"]["metrics"]["holdout_c"]["accuracy_pct"],
                6,
            ),
            "all_beat_majority": bool(
                all(
                    result["metrics"][n]["vs_train_majority_baseline_pp"] > 0
                    for n in holdout_names
                )
            ),
            "precision_at_0_7": {
                n: (
                    None
                    if t is None
                    else {
                        "fired_rows": t["fired_rows"],
                        "fired_pct": t["fired_pct"],
                        "precision_pct": t["precision_pct"],
                        "abstained_pct": t["abstained_pct"],
                        "per_class_precision": t["per_class_precision"],
                    }
                )
                for n, t in p07.items()
            },
        }

    artifact: Dict[str, Any] = {
        "task": "TASK C ML model evaluation v3 — honest final verdict",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "status": "completed",
        "safety_scope": {
            "production_wiring_modified": False,
            "trading_modified": False,
            "crossvenue_modified": False,
            "kronos_modified": False,
        },
        "data_policy": {
            "real_lse_1m_btc_usd_only": True,
            "anonymous_macro_calendar_only": True,
            "synthetic_padding_allowed": False,
        },
        "data_source": {
            "candles_path": str(candles_path),
            "candle_rows": len(df),
            "candle_range_utc": {
                "start": df["timestamp"].iloc[0].isoformat(),
                "end": df["timestamp"].iloc[-1].isoformat(),
            },
            "rows_vs_v2": round(len(df) / 5000.0, 4) if len(df) else None,
        },
        "macro_calendar": macro_meta,
        "macro_feature_coverage": {
            "feature": "minutes_to_next_high_macro",
            "non_nan_rows": int(healthy_macro),
            "non_nan_pct": round(macro_coverage * 100, 4),
            "note": "Historical HIGH-impact macro events are NOT available for the May-Aug 2026 training window via the LSE calendar or Forex Factory public feed. Feature is NaN for the training period.",
        },
        "horizon_minutes": horizon_minutes,
        "feature_columns": FEATURE_COLUMNS,
        "comparison_baseline": {"name": "Kronos", "accuracy_pct": KRONOS_ACCURACY_PCT},
        "partitions": [asdict(p) for p in partitions],
        "target": {
            "name": TARGET_COLUMN,
            "definition": f"sqrt(sum of squared next {horizon_minutes} one-minute log returns) * 10,000",
            "classes": CLASS_NAMES,
            "cutoffs_fit_on_train_only": target_cutoffs,
            "cutoffs_match_recomputed_train_quantiles": cutoffs_match,
        },
        "volatility_regime_per_partition": vol_regimes,
        "variants": {
            "A_balanced": result_a,
            "B_explicit_weights": result_b,
            "C_two_stage": result_c,
        },
        "permutation_importance_validation_accuracy": {
            "fit_on": "train_only",
            "evaluated_on": "validation_only",
            "repeats": PERMUTATION_IMPORTANCE_REPEATS,
            "features": perm_rows,
            "note": "Computed on variant A (balanced flat model)",
        },
        "holdout_summary": summary_variants,
    }

    # Save artifacts in order of safety. JSON (all numbers) FIRST so a later
    # pickle failure cannot destroy the metrics. Each pickle is wrapped so
    # that a single failure doesn't prevent the other from saving.
    out_dir = PROJECT_ROOT / "state"
    out_dir.mkdir(parents=True, exist_ok=True)
    metrics_name = f"lightgbm_training_metrics_{model_tag}.json"
    volmodel_name = f"lightgbm_volatility_model_{model_tag}.pkl"
    twostage_name = f"lightgbm_two_stage_{model_tag}.pkl"
    output_path = out_dir / metrics_name
    output_path.write_text(json.dumps(artifact, indent=2, sort_keys=True))
    print(f"\n[SAVED] {output_path}")
    # Save model pickles — best-effort, errors logged but not fatal (JSON is canonical)
    saved_pickles = []
    if model_a is not None:
        try:
            joblib.dump(model_a, out_dir / volmodel_name)
            saved_pickles.append(volmodel_name)
            print(f"[SAVED] {out_dir}/{volmodel_name}")
        except Exception as exc:
            print(
                f"[WARN] failed to pickle variant A model: {type(exc).__name__}: {exc}"
            )
    if models_c is not None:
        try:
            joblib.dump(models_c, out_dir / twostage_name)
            saved_pickles.append(twostage_name)
            print(f"[SAVED] {out_dir}/{twostage_name}")
        except Exception as exc:
            print(
                f"[WARN] failed to pickle two-stage model: {type(exc).__name__}: {exc}"
            )
    return artifact


def main():
    parser = argparse.ArgumentParser(description="ML model evaluation v3")
    parser.add_argument(
        "--candles", type=Path, default=Path("state/btc_1m_candles_lse_v3.parquet")
    )
    parser.add_argument("--macro", type=Path, default=Path("state/macro_events.json"))
    parser.add_argument(
        "--output", type=Path, default=Path("state/lightgbm_training_metrics_v3.json")
    )
    parser.add_argument(
        "--fear-greed", type=Path, default=Path("state/fear_greed_index.json"),
        help="Path from scripts/fetch_external_signals.py; omitted feature if missing",
    )
    parser.add_argument(
        "--funding-rate", type=Path, default=Path("state/binance_funding_rate_btc.json"),
        help="Path from scripts/fetch_external_signals.py; omitted feature if missing",
    )
    parser.add_argument(
        "--bookdepth", type=Path, default=Path("state/binance_bookdepth_btc.parquet"),
        help="Path from scripts/fetch_binance_vision_signals.py; omitted feature if missing",
    )
    parser.add_argument(
        "--oi-longshort", type=Path, default=Path("state/binance_oi_longshort_btc.parquet"),
        help="Path from scripts/fetch_binance_vision_signals.py; omitted feature if missing",
    )
    parser.add_argument(
        "--cross-asset", type=Path, default=Path("state/cross_asset_daily.parquet"),
        help="Path from scripts/fetch_binance_vision_signals.py; omitted feature if missing",
    )
    parser.add_argument(
        "--horizon", type=int, default=HORIZON_MINUTES,
        help="Forward-looking realized-vol horizon in minutes (default: module constant)",
    )
    parser.add_argument(
        "--model-tag", type=str, default="v3",
        help="Suffix for saved artifact filenames (lightgbm_*_<tag>.{json,pkl}). "
             "Defaults to 'v3', the live-deployed name — use a distinct tag for "
             "any experimental run so it never overwrites the deployed model.",
    )
    args = parser.parse_args()
    candles_path = (
        PROJECT_ROOT / args.candles if not args.candles.is_absolute() else args.candles
    )
    macro_path = (
        PROJECT_ROOT / args.macro if not args.macro.is_absolute() else args.macro
    )
    fear_greed_path = PROJECT_ROOT / args.fear_greed if not args.fear_greed.is_absolute() else args.fear_greed
    funding_rate_path = PROJECT_ROOT / args.funding_rate if not args.funding_rate.is_absolute() else args.funding_rate
    bookdepth_path = PROJECT_ROOT / args.bookdepth if not args.bookdepth.is_absolute() else args.bookdepth
    oi_longshort_path = PROJECT_ROOT / args.oi_longshort if not args.oi_longshort.is_absolute() else args.oi_longshort
    cross_asset_path = PROJECT_ROOT / args.cross_asset if not args.cross_asset.is_absolute() else args.cross_asset
    artifact = run_pipeline(
        candles_path, macro_path, args.output, fear_greed_path, funding_rate_path,
        bookdepth_path, oi_longshort_path, cross_asset_path,
        horizon_minutes=args.horizon, model_tag=args.model_tag,
    )
    # Print summary table
    print("\n" + "=" * 70)
    print("HOLDOUT COMPARISON ACROSS VARIANTS")
    print("=" * 70)
    print(f"{'Variant':<20} {'A':>8} {'B':>8} {'C':>8}  {'MajorityC':>10}")
    print(
        f"{'':.<20} ({'balanced':>7}) {'explicit':>8} {'2stage':>8}  {'baseline':>10}"
    )
    holdouts = ["holdout_a", "holdout_b", "holdout_c"]
    for h in holdouts:
        row = []
        for tag, result in [
            ("A", artifact["variants"]["A_balanced"]),
            ("B", artifact["variants"]["B_explicit_weights"]),
            ("C", artifact["variants"]["C_two_stage"]),
        ]:
            if "metrics" in result:
                row.append(f"{result['metrics'][h]['accuracy_pct']:7.2f}%")
            else:
                row.append("   n/a")
        mc = artifact["variants"]["A_balanced"]["majority_baseline"]["metrics"][h][
            "accuracy_pct"
        ]
        print(f"  {h:<18} {'  '.join(row)}  {mc:9.2f}%")
    # Final verdict
    print("\n" + "=" * 70)
    print("DEPLOYABILITY VERDICT")
    print("=" * 70)
    s = artifact["holdout_summary"]
    a = s.get("A_balanced", {})
    if a:
        print(
            f"  Variant A (balanced): mean={a['holdout_mean_pct']:.2f}%, stddev={a['holdout_stddev_pp']:.2f}pp, range={a['holdout_range_pp']:.2f}pp"
        )
        print(
            f"    Holdout C vs majority: {a['holdout_c_vs_majority_pp']:+.2f}pp  ('all_beat_majority'={a['all_beat_majority']})"
        )
    b = s.get("B_explicit_weights", {})
    if b:
        print(
            f"  Variant B (explicit  :1:3:3): mean={b['holdout_mean_pct']:.2f}%, stddev={b['holdout_stddev_pp']:.2f}pp"
        )
        print(f"    Holdout C vs majority: {b['holdout_c_vs_majority_pp']:+.2f}pp")
    c = s.get("C_two_stage", {})
    if c:
        print(
            f"  Variant C (two-stage): mean={c['holdout_mean_pct']:.2f}%, stddev={c['holdout_stddev_pp']:.2f}pp"
        )
        print(f"    Holdout C vs majority: {c['holdout_c_vs_majority_pp']:+.2f}pp")
    # Precision at 0.7 confidence (TASK 2 part 2)
    print("\n" + "-" * 70)
    print("PRECISION AT P>=0.7 CONFIDENCE (abstain otherwise) — Holdout C")
    print("-" * 70)
    for tag in ("A_balanced", "B_explicit_weights", "C_two_stage"):
        p = s.get(tag, {}).get("precision_at_0_7", {}).get("holdout_c")
        if p is None:
            print(f"  {tag:<20} no probability output / no rows fired at 0.7")
            continue
        print(
            f"  {tag:<20} fired={p['fired_rows']}/{artifact['partitions'][4]['rows']} ({p['fired_pct']}%)  precision={p['precision_pct']}%  abstained={p['abstained_pct']}%"
        )
        per = p["per_class_precision"]
        print(
            f"  {'':<20} per-class precision: LOW={per.get('LOW')} MED={per.get('MED')} HIGH={per.get('HIGH')}"
        )
    print("-" * 70)
    print("HONEST VERDICT: deployable if and only if precision@0.7 is materially")
    print("above raw accuracy AND abstention is not 100% (i.e. it actually fires).")
    print("-" * 70)


if __name__ == "__main__":
    main()
