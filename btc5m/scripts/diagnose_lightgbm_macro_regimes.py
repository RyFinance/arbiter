#!/usr/bin/env python3
"""Diagnostic-only LightGBM volatility-regime experiment.

This module intentionally has no production imports beyond the cold-path LSE
client. It does not write trading configuration, invoke Kronos, or call any
cross-venue component.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import lightgbm as lgb
import numpy as np
import pandas as pd
from lightgbm import LGBMClassifier
from sklearn.inspection import permutation_importance
from sklearn.metrics import accuracy_score, confusion_matrix, f1_score, log_loss

from polymarket_arb.clients.lse_client import LSEClient


UTC = timezone.utc


KRONOS_ACCURACY_PCT = 43.75
PERMUTATION_IMPORTANCE_REPEATS = 20
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
    "minutes_to_next_high_macro",
    "session_bucket",
]
CLASS_NAMES = ["LOW", "MED", "HIGH"]
DEFAULT_ROUND_TRIP_COST_BPS = 20.0
DEFAULT_HYPERPARAMETERS = {
    "objective": "multiclass",
    "num_class": 3,
    "n_estimators": 300,
    "learning_rate": 0.04,
    "num_leaves": 15,
    "max_depth": -1,
    "min_child_samples": 30,
    "subsample": 0.9,
    "colsample_bytree": 0.9,
    "reg_alpha": 0.1,
    "reg_lambda": 0.2,
    "class_weight": "balanced",
    "random_state": 20260803,
    "n_jobs": 1,
    "verbosity": -1,
}


@dataclass(frozen=True)
class Partition:
    name: str
    start: str
    end: str
    rows: int


class InsufficientHistoryError(RuntimeError):
    """Raised when genuine data cannot support the prescribed split."""


def _to_utc_timestamp(value: Any) -> pd.Timestamp:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return pd.to_datetime(value, unit="s", utc=True)
    return pd.to_datetime(value, utc=True)


def load_anonymous_macro_calendar(path: Path) -> tuple[list[pd.Timestamp], dict[str, Any]]:
    """Load only scheduled HIGH-impact event times from the local anonymous calendar."""
    payload = json.loads(path.read_text())
    events = payload.get("events", []) if isinstance(payload, dict) else payload
    if not isinstance(events, list):
        raise ValueError("macro calendar must contain an events list")

    high_times: list[pd.Timestamp] = []
    impact_counts: dict[str, int] = {}
    for event in events:
        if not isinstance(event, dict):
            continue
        impact = str(event.get("impact", "")).upper()
        impact_counts[impact] = impact_counts.get(impact, 0) + 1
        if impact != "HIGH" or event.get("timestamp") is None:
            continue
        try:
            high_times.append(_to_utc_timestamp(event["timestamp"]))
        except (TypeError, ValueError, OverflowError):
            continue

    high_times = sorted(set(high_times))
    metadata = {
        "path": str(path),
        "event_count": len(events),
        "high_event_count": len(high_times),
        "impact_counts": impact_counts,
        "event_range_utc": _range_dict(high_times),
        "uses_event_titles": False,
        "uses_event_descriptions": False,
    }
    return high_times, metadata


def normalize_lse_candles(rows: Iterable[dict[str, Any]]) -> pd.DataFrame:
    """Normalize LSE 1m BTC/USD candles without resampling, imputation, or padding."""
    frame = pd.DataFrame(list(rows)).copy()
    if frame.empty:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])

    timestamp_column = "ts" if "ts" in frame.columns else "timestamp"
    required = {timestamp_column, "open", "high", "low", "close", "volume"}
    missing = sorted(required.difference(frame.columns))
    if missing:
        raise ValueError(f"LSE candle response is missing required fields: {missing}")

    result = frame[[timestamp_column, "open", "high", "low", "close", "volume"]].rename(
        columns={timestamp_column: "timestamp"}
    )
    result["timestamp"] = pd.to_datetime(result["timestamp"], utc=True, errors="coerce")
    for column in ("open", "high", "low", "close", "volume"):
        result[column] = pd.to_numeric(result[column], errors="coerce")
    result = result.dropna().drop_duplicates(subset="timestamp").sort_values("timestamp")
    result = result[(result["close"] > 0) & (result["high"] > 0) & (result["low"] > 0)]
    return result.reset_index(drop=True)


async def fetch_lse_candles(start: date | None = None, end: date | None = None) -> pd.DataFrame:
    """Fetch an LSE BTC/USD 1m range, or the unbounded vault response when omitted."""
    if (start is None) != (end is None):
        raise ValueError("start and end must be supplied together for a bounded LSE request")
    client = LSEClient()
    try:
        rows = await client.fetch_candles(
            symbol="BTC/USD",
            timeframe="1m",
            start=start.isoformat() if start else None,
            end=end.isoformat() if end else None,
            limit=10000,
        )
    finally:
        await client.aclose()
    return normalize_lse_candles(rows)


def _session_bucket(hour: pd.Series) -> pd.Series:
    # UTC sessions: Asia, Europe, US, and Pacific/late US.
    return pd.cut(
        hour,
        bins=[-1, 6, 12, 20, 24],
        labels=False,
        include_lowest=True,
        right=True,
    ).astype("int8")


def _minutes_to_next_event(timestamps: pd.Series, high_events: list[pd.Timestamp]) -> pd.Series:
    if not high_events:
        return pd.Series(np.nan, index=timestamps.index, dtype="float64")
    event_ns = np.array([event.value for event in high_events], dtype=np.int64)
    candle_ns = timestamps.astype("int64").to_numpy()
    positions = np.searchsorted(event_ns, candle_ns, side="left")
    result = np.full(len(timestamps), np.nan, dtype=float)
    valid = positions < len(event_ns)
    result[valid] = (event_ns[positions[valid]] - candle_ns[valid]) / 60_000_000_000
    return pd.Series(result, index=timestamps.index)


def build_feature_frame(
    candles: pd.DataFrame, high_events: list[pd.Timestamp], horizon_minutes: int,
    round_trip_cost_bps: float = DEFAULT_ROUND_TRIP_COST_BPS,
) -> pd.DataFrame:
    """Create t-cutoff features and an auditable hypothetical profitability label."""
    if horizon_minutes < 2:
        raise ValueError("horizon_minutes must be at least 2")
    frame = candles.copy().sort_values("timestamp").reset_index(drop=True)
    if frame.empty:
        return frame

    log_return = np.log(frame["close"]).diff()
    true_range = pd.concat(
        [
            frame["high"] - frame["low"],
            (frame["high"] - frame["close"].shift()).abs(),
            (frame["low"] - frame["close"].shift()).abs(),
        ],
        axis=1,
    ).max(axis=1)
    frame["log_volume"] = np.log1p(frame["volume"])
    frame["return_1m_bps"] = log_return * 10_000
    frame["rolling_vol_5m_bps"] = log_return.set_axis(frame["timestamp"]).rolling("5min", min_periods=5).std().to_numpy() * 10_000
    frame["rolling_vol_15m_bps"] = log_return.set_axis(frame["timestamp"]).rolling("15min", min_periods=15).std().to_numpy() * 10_000
    frame["rolling_vol_60m_bps"] = log_return.set_axis(frame["timestamp"]).rolling("60min", min_periods=60).std().to_numpy() * 10_000
    frame["atr_14m_bps"] = (
        true_range.set_axis(frame["timestamp"]).rolling("14min", min_periods=14).mean().to_numpy() / frame["close"] * 10_000
    )
    frame["atr_60m_bps"] = (
        true_range.set_axis(frame["timestamp"]).rolling("60min", min_periods=60).mean().to_numpy() / frame["close"] * 10_000
    )
    frame["minutes_to_next_high_macro"] = _minutes_to_next_event(frame["timestamp"], high_events)
    frame["session_bucket"] = _session_bucket(frame["timestamp"].dt.hour)

    future_squared_returns = sum(log_return.pow(2).shift(-step) for step in range(1, horizon_minutes + 1))
    future_log_return_bps = sum(log_return.shift(-step) for step in range(1, horizon_minutes + 1)) * 10_000
    contiguous_future = frame["timestamp"].shift(-horizon_minutes).sub(frame["timestamp"]).eq(
        pd.Timedelta(minutes=horizon_minutes)
    )
    frame["forward_realized_vol_bps"] = np.sqrt(future_squared_returns) * 10_000
    frame["forward_net_return_bps"] = future_log_return_bps - float(round_trip_cost_bps)
    frame["profitability_target"] = (frame["forward_net_return_bps"] > 0).astype("int8")
    frame.loc[~contiguous_future, "forward_realized_vol_bps"] = np.nan
    frame.loc[~contiguous_future, ["forward_net_return_bps", "profitability_target"]] = np.nan
    return frame.dropna(subset=FEATURE_COLUMNS + ["forward_realized_vol_bps"]).reset_index(drop=True)


def label_hypothetical_profitability(
    candles: pd.DataFrame, horizon_minutes: int, round_trip_cost_bps: float
) -> pd.DataFrame:
    """Label BTC proxy profitability using only the future window t+1..t+K."""
    if horizon_minutes < 1 or round_trip_cost_bps < 0:
        raise ValueError("horizon_minutes must be positive and cost must be non-negative")
    frame = candles.sort_values("timestamp").reset_index(drop=True).copy()
    log_return = np.log(pd.to_numeric(frame["close"], errors="coerce")).diff()
    future = sum(log_return.shift(-step) for step in range(1, horizon_minutes + 1)) * 10_000
    contiguous = frame["timestamp"].shift(-horizon_minutes).sub(frame["timestamp"]).eq(
        pd.Timedelta(minutes=horizon_minutes)
    )
    result = frame[["timestamp"]].copy()
    result["feature_cutoff_t"] = result["timestamp"]
    result["future_window_end"] = result["timestamp"] + pd.Timedelta(minutes=horizon_minutes)
    result["future_log_return_bps"] = future
    result["round_trip_cost_bps"] = float(round_trip_cost_bps)
    result["forward_net_return_bps"] = future - float(round_trip_cost_bps)
    result["profitability_target"] = (result["forward_net_return_bps"] > 0).astype("int8")
    result.loc[~contiguous, ["future_log_return_bps", "forward_net_return_bps", "profitability_target"]] = np.nan
    return result.dropna(subset=["future_log_return_bps"]).reset_index(drop=True)


def split_time_disjoint(frame: pd.DataFrame, minimum_rows_per_partition: int = 100) -> tuple[dict[str, pd.DataFrame], list[Partition]]:
    """Split data chronologically into train, validation, and three disjoint holdouts."""
    if len(frame) < minimum_rows_per_partition * 5:
        raise InsufficientHistoryError(
            f"need at least {minimum_rows_per_partition * 5} eligible real candles for five disjoint partitions; got {len(frame)}"
        )
    boundaries = [int(len(frame) * fraction) for fraction in (0.50, 0.625, 0.75, 0.875)]
    raw_parts = {
        "train": frame.iloc[: boundaries[0]].copy(),
        "validation": frame.iloc[boundaries[0] : boundaries[1]].copy(),
        "holdout_a": frame.iloc[boundaries[1] : boundaries[2]].copy(),
        "holdout_b": frame.iloc[boundaries[2] : boundaries[3]].copy(),
        "holdout_c": frame.iloc[boundaries[3] :].copy(),
    }
    if min(len(part) for part in raw_parts.values()) < minimum_rows_per_partition:
        raise InsufficientHistoryError("chronological partition has fewer rows than the configured minimum")
    partitions = [
        Partition(
            name=name,
            start=part["timestamp"].iloc[0].isoformat(),
            end=part["timestamp"].iloc[-1].isoformat(),
            rows=len(part),
        )
        for name, part in raw_parts.items()
    ]
    return raw_parts, partitions


def label_from_training_quantiles(parts: dict[str, pd.DataFrame]) -> tuple[dict[str, pd.DataFrame], dict[str, float]]:
    train_target = parts["train"]["forward_realized_vol_bps"]
    low_high_cutoff, high_cutoff = train_target.quantile([1 / 3, 2 / 3]).tolist()
    if not np.isfinite([low_high_cutoff, high_cutoff]).all() or low_high_cutoff >= high_cutoff:
        raise InsufficientHistoryError("training realized-volatility quantiles do not form three distinct classes")

    labelled: dict[str, pd.DataFrame] = {}
    for name, part in parts.items():
        labelled_part = part.copy()
        labelled_part["target"] = np.select(
            [
                labelled_part["forward_realized_vol_bps"] <= low_high_cutoff,
                labelled_part["forward_realized_vol_bps"] <= high_cutoff,
            ],
            [0, 1],
            default=2,
        ).astype(int)
        labelled[name] = labelled_part
    return labelled, {"low_to_med_bps": float(low_high_cutoff), "med_to_high_bps": float(high_cutoff)}


def _classification_metrics(
    target: pd.Series, prediction: np.ndarray, probabilities: np.ndarray | None = None
) -> dict[str, Any]:
    correct_predictions = int(np.sum(target.to_numpy() == prediction))
    accuracy_pct = accuracy_score(target, prediction) * 100
    metrics = {
        "rows": int(len(target)),
        "correct_predictions": correct_predictions,
        "accuracy_pct": round(float(accuracy_pct), 6),
        "macro_f1": round(float(f1_score(target, prediction, labels=[0, 1, 2], average="macro", zero_division=0)), 6),
        "class_counts": {CLASS_NAMES[index]: int((target == index).sum()) for index in range(3)},
        "all_three_classes_observed": bool(all((target == index).any() for index in range(3))),
        "confusion_matrix_rows_low_med_high": confusion_matrix(target, prediction, labels=[0, 1, 2]).tolist(),
        "vs_kronos_43_75_pp": round(float(accuracy_pct - KRONOS_ACCURACY_PCT), 6),
    }
    if probabilities is not None:
        metrics["multiclass_log_loss"] = round(float(log_loss(target, probabilities, labels=[0, 1, 2])), 6)
    return metrics


def _metrics(model: LGBMClassifier, part: pd.DataFrame) -> dict[str, Any]:
    probabilities = model.predict_proba(part[FEATURE_COLUMNS], num_iteration=model.best_iteration_)
    return _classification_metrics(part["target"], probabilities.argmax(axis=1), probabilities)


def _majority_baseline_metrics(part: pd.DataFrame, majority_class: int) -> dict[str, Any]:
    return _classification_metrics(part["target"], np.full(len(part), majority_class, dtype=int))


def _range_dict(values: list[pd.Timestamp]) -> dict[str, str | None]:
    return {
        "start": values[0].isoformat() if values else None,
        "end": values[-1].isoformat() if values else None,
    }


def _pip_check() -> dict[str, Any]:
    result = subprocess.run([sys.executable, "-m", "pip", "check"], capture_output=True, text=True, check=False)
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    return {"exit_code": result.returncode, "output": lines or ["No broken requirements found."]}


def _source_summary(candles: pd.DataFrame, requested_start: date | None, requested_end: date | None) -> dict[str, Any]:
    timestamps = candles["timestamp"].tolist() if not candles.empty else []
    gaps = candles["timestamp"].diff().gt(pd.Timedelta(minutes=1)).sum() if not candles.empty else 0
    return {
        "provider": "LSE",
        "symbol": "BTC/USD",
        "timeframe": "1m",
        "request_mode": "bounded_utc_range" if requested_start else "unbounded_lse_vault_request",
        "requested_start": requested_start.isoformat() if requested_start else None,
        "requested_end_exclusive": requested_end.isoformat() if requested_end else None,
        "requested_limit": 10000,
        "provider_response_may_cap_rows": True,
        "fetched_rows": int(len(candles)),
        "fetched_range_utc": _range_dict(timestamps),
        "observed_gaps_over_one_minute": int(gaps),
        "synthetic_rows_added": 0,
        "resampling_or_padding": False,
    }


def run_diagnostic(
    candles: pd.DataFrame,
    high_events: list[pd.Timestamp],
    macro_metadata: dict[str, Any],
    requested_start: date | None,
    requested_end: date | None,
    horizon_minutes: int = 15,
) -> dict[str, Any]:
    """Run the complete diagnostic, returning an artifact for either outcome."""
    artifact: dict[str, Any] = {
        "task": "TASK C diagnostic-only LightGBM volatility-regime classifier",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "status": "insufficient_history",
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
        "lgbm_api": {
            "package": "lightgbm",
            "version": lgb.__version__,
            "estimator": "lightgbm.LGBMClassifier",
            "hyperparameters": DEFAULT_HYPERPARAMETERS,
        },
        "environment": {"python": sys.version.split()[0], "pip_check": _pip_check()},
        "source": _source_summary(candles, requested_start, requested_end),
        "macro_calendar": macro_metadata,
        "horizon_minutes": horizon_minutes,
        "research_target": {
            "name": "profitability_target",
            "definition": "1 iff future BTC close-to-close log return over t+1..t+K exceeds round_trip_cost_bps",
            "cost_assumption_bps": DEFAULT_ROUND_TRIP_COST_BPS,
            "feature_cutoff": "t; labels use only t+1..t+K",
            "venue_profitability_feasible": False,
            "limitation": "retained data lacks paired Polymarket executable quotes, fills, fees, and latency-linked outcomes",
        },
        "feature_columns": FEATURE_COLUMNS,
        "comparison_baseline": {"name": "Kronos", "accuracy_pct": KRONOS_ACCURACY_PCT},
    }
    feature_frame = build_feature_frame(candles, high_events, horizon_minutes)
    artifact["eligible_feature_rows"] = int(len(feature_frame))
    try:
        raw_parts, partitions = split_time_disjoint(feature_frame)
        parts, target_cutoffs = label_from_training_quantiles(raw_parts)
        recomputed_train_cutoffs = raw_parts["train"]["forward_realized_vol_bps"].quantile([1 / 3, 2 / 3])
        cutoffs_match_recomputed_train_quantiles = bool(
            np.allclose(
                [target_cutoffs["low_to_med_bps"], target_cutoffs["med_to_high_bps"]],
                recomputed_train_cutoffs.to_numpy(),
                rtol=0.0,
                atol=0.01,
            )
        )
    except InsufficientHistoryError as error:
        artifact["insufficient_history_reason"] = str(error)
        return artifact

    model = LGBMClassifier(**DEFAULT_HYPERPARAMETERS)
    model.fit(
        parts["train"][FEATURE_COLUMNS],
        parts["train"]["target"],
        eval_X=parts["validation"][FEATURE_COLUMNS],
        eval_y=parts["validation"]["target"],
        eval_metric="multi_logloss",
        callbacks=[lgb.early_stopping(stopping_rounds=50, verbose=False)],
    )
    importances = pd.DataFrame(
        {
            "feature": FEATURE_COLUMNS,
            "gain": model.booster_.feature_importance(importance_type="gain"),
            "split_count": model.booster_.feature_importance(importance_type="split"),
        }
    ).sort_values(["gain", "split_count"], ascending=False)
    majority_class = int(parts["train"]["target"].value_counts().reindex(range(3), fill_value=0).idxmax())
    model_metrics = {name: _metrics(model, part) for name, part in parts.items()}
    majority_metrics = {name: _majority_baseline_metrics(part, majority_class) for name, part in parts.items()}
    for name in model_metrics:
        model_metrics[name]["vs_train_majority_baseline_pp"] = round(
            model_metrics[name]["accuracy_pct"] - majority_metrics[name]["accuracy_pct"], 6
        )

    permutation = permutation_importance(
        model,
        parts["validation"][FEATURE_COLUMNS],
        parts["validation"]["target"],
        scoring="accuracy",
        n_repeats=PERMUTATION_IMPORTANCE_REPEATS,
        random_state=DEFAULT_HYPERPARAMETERS["random_state"],
        n_jobs=1,
    )
    permutation_rows = sorted(
        (
            {
                "feature": feature,
                "mean_accuracy_decrease": round(float(mean), 8),
                "std_accuracy_decrease": round(float(std), 8),
            }
            for feature, mean, std in zip(FEATURE_COLUMNS, permutation.importances_mean, permutation.importances_std)
        ),
        key=lambda row: row["mean_accuracy_decrease"],
        reverse=True,
    )
    holdout_metrics = [model_metrics[name] for name in ("holdout_a", "holdout_b", "holdout_c")]
    holdout_accuracies = np.array([metric["accuracy_pct"] for metric in holdout_metrics], dtype=float)
    artifact.update(
        {
            "status": "completed",
            "partitions": [asdict(partition) for partition in partitions],
            "target": {
                "name": "forward_realized_vol_bps",
                "definition": "sqrt(sum of squared next 15 one-minute log returns) * 10,000",
                "classes": CLASS_NAMES,
                "cutoffs_fit_on_train_only": target_cutoffs,
                "cutoffs_match_recomputed_train_quantiles": cutoffs_match_recomputed_train_quantiles,
            },
            "best_iteration": int(model.best_iteration_ or model.n_estimators),
            "metrics": model_metrics,
            "majority_baseline": {
                "fit_on": "train_only",
                "predicted_class": CLASS_NAMES[majority_class],
                "metrics": majority_metrics,
            },
            "permutation_importance_validation_accuracy": {
                "fit_on": "train_only",
                "evaluated_on": "validation_only",
                "repeats": PERMUTATION_IMPORTANCE_REPEATS,
                "features": permutation_rows,
            },
            "stability_vs_kronos": {
                "reference": "fixed 43.75% Kronos accuracy; Kronos was not invoked",
                "holdout_names": ["holdout_a", "holdout_b", "holdout_c"],
                "holdout_accuracy_pct": holdout_accuracies.tolist(),
                "mean_holdout_accuracy_pct": round(float(holdout_accuracies.mean()), 6),
                "holdout_accuracy_stddev_pct_points": round(float(holdout_accuracies.std(ddof=0)), 6),
                "holdout_accuracy_range_pct_points": round(float(holdout_accuracies.max() - holdout_accuracies.min()), 6),
                "all_holdouts_beat_kronos": bool(all(metric["vs_kronos_43_75_pp"] > 0 for metric in holdout_metrics)),
                "all_holdouts_have_all_three_classes": bool(
                    all(metric["all_three_classes_observed"] for metric in holdout_metrics)
                ),
            },
            "top_10_feature_importances_gain": [
                {"feature": row.feature, "gain": round(float(row.gain), 6), "split_count": int(row.split_count)}
                for row in importances.head(10).itertuples(index=False)
            ],
        }
    )
    return artifact


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--calendar", type=Path, default=Path("state/macro_events.json"))
    parser.add_argument("--output", type=Path, default=Path("state/lightgbm_task_c_diagnostic.json"))
    parser.add_argument("--start", type=date.fromisoformat, help="UTC start date, inclusive")
    parser.add_argument("--end", type=date.fromisoformat, help="UTC end date, exclusive")
    parser.add_argument("--horizon-minutes", type=int, default=15)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    high_events, macro_metadata = load_anonymous_macro_calendar(args.calendar)
    if (args.start is None) != (args.end is None):
        raise ValueError("--start and --end must be supplied together")
    if args.start is not None and args.end <= args.start:
        raise ValueError("--end must be after --start")
    candles = asyncio.run(fetch_lse_candles(args.start, args.end))
    artifact = run_diagnostic(candles, high_events, macro_metadata, args.start, args.end, args.horizon_minutes)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"status": artifact["status"], "output": str(args.output)}, sort_keys=True))
    return 0 if artifact["status"] == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
