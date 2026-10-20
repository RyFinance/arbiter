"""Chronological, offline-only evaluation of the causal BTC proxy-profit label.

This module deliberately has no network or execution-path dependencies.  It
uses the already-audited feature/label builder and evaluates models in one
chronological training block followed by three disjoint future holdouts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    accuracy_score,
    average_precision_score,
    precision_recall_curve,
    precision_score,
    recall_score,
    roc_auc_score,
)
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

try:  # Supports both `python scripts/...` and package-style test imports.
    from scripts.btc_offline_features import CAUSAL_FEATURE_COLUMNS, build_features
    from scripts.diagnose_lightgbm_macro_regimes import (
        DEFAULT_ROUND_TRIP_COST_BPS,
        FEATURE_COLUMNS,
        build_feature_frame,
        load_anonymous_macro_calendar,
    )
except ModuleNotFoundError:  # pragma: no cover - exercised by the CLI subprocess.
    from btc_offline_features import CAUSAL_FEATURE_COLUMNS, build_features  # type: ignore[no-redef]
    from diagnose_lightgbm_macro_regimes import (  # type: ignore[no-redef]
        DEFAULT_ROUND_TRIP_COST_BPS,
        FEATURE_COLUMNS,
        build_feature_frame,
        load_anonymous_macro_calendar,
    )


RANDOM_SEED = 20260811
LABEL_COLUMN = "profitability_target"
RETURN_COLUMN = "forward_net_return_bps"
TIME_COLUMN = "timestamp"
HOLDOUT_NAMES = ("holdout_a", "holdout_b", "holdout_c")
SIZING_POLICY_NAMES = ("binary_f1", "top_decile", "confidence_weighted", "probability_proportional")
EMBARGO_ROWS = 15
EMBARGO_MINUTES = 15
EXPANDED_FEATURE_COLUMNS = tuple(
    dict.fromkeys([*FEATURE_COLUMNS, *(column for column in CAUSAL_FEATURE_COLUMNS if column not in FEATURE_COLUMNS)])
)


def _distribution(labels: pd.Series) -> dict[int, int]:
    """Return a stable, exact count for every observed label."""
    return {int(label): int(count) for label, count in labels.value_counts().sort_index().items()}


def _time_summary(frame: pd.DataFrame) -> dict[str, Any]:
    return {
        "rows": int(len(frame)),
        "start": frame[TIME_COLUMN].iloc[0].isoformat(),
        "end": frame[TIME_COLUMN].iloc[-1].isoformat(),
        "label_distribution": _distribution(frame[LABEL_COLUMN]),
    }


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


def _feature_label_correlations(train: pd.DataFrame, feature_columns: Iterable[str]) -> dict[str, dict[str, float | int | None]]:
    """Report descriptive correlations from train only, never later holdouts."""
    correlations: dict[str, dict[str, float | int | None]] = {}
    for column in feature_columns:
        pairs = train[[column, LABEL_COLUMN]].dropna()
        value = pairs[column].corr(pairs[LABEL_COLUMN])
        correlations[column] = {
            "pearson": float(value) if pd.notna(value) and np.isfinite(value) else None,
            "complete_pairs": int(len(pairs)),
        }
    return correlations


def _metrics(
    actual: np.ndarray, predicted: np.ndarray, probability: np.ndarray, positive_label: int
) -> dict[str, float]:
    return {
        "accuracy": float(accuracy_score(actual, predicted)),
        "auc": float(roc_auc_score(actual, probability)),
        "pr_auc": float(average_precision_score(actual, probability, pos_label=positive_label)),
        "precision": float(precision_score(actual, predicted, pos_label=positive_label, zero_division=0)),
        "recall": float(recall_score(actual, predicted, pos_label=positive_label, zero_division=0)),
    }


def _f1_threshold_from_predictions(
    labels: np.ndarray,
    probabilities: np.ndarray,
    positive_label: int,
) -> float:
    """Choose the first deterministic F1 maximum from supplied predictions."""
    precision, recall, thresholds = precision_recall_curve(
        labels, probabilities, pos_label=positive_label
    )
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


def _train_probability_q90(probabilities: np.ndarray) -> float:
    """Return an observed train-only 90th-percentile cutoff with stable tie handling."""
    values = np.asarray(probabilities, dtype=float)
    if values.ndim != 1 or not len(values):
        raise ValueError("train probabilities must be a non-empty one-dimensional array")
    if not np.isfinite(values).all():
        raise ValueError("train probabilities must all be finite")
    return float(np.quantile(values, 0.90, method="higher"))


def _train_calibration(
    train: pd.DataFrame,
    model: Any,
    feature_columns: list[str],
    positive_label: int,
) -> dict[str, float | str | int]:
    """Freeze both sizing cutoffs from one set of fitted-train predictions."""
    probabilities = model.predict_proba(train[feature_columns])[
        :, list(model.classes_).index(positive_label)
    ]
    return {
        "source": "fitted train predictions only",
        "train_rows": int(len(train)),
        "train_f1_threshold": _f1_threshold_from_predictions(
            train[LABEL_COLUMN].to_numpy(), probabilities, positive_label
        ),
        "train_probability_q90": _train_probability_q90(probabilities),
    }


def _train_f1_threshold(
    train: pd.DataFrame, model: Any, feature_columns: list[str], positive_label: int
) -> float:
    """Backward-compatible train-only F1-threshold helper."""
    return float(
        _train_calibration(train, model, feature_columns, positive_label)[
            "train_f1_threshold"
        ]
    )


def _policy_weights(
    probabilities: np.ndarray,
    f1_threshold: float,
    q90_cutoff: float,
) -> dict[str, np.ndarray]:
    """Apply the three frozen sizing policies without consulting outcomes."""
    values = np.asarray(probabilities, dtype=float)
    if values.ndim != 1 or not np.isfinite(values).all():
        raise ValueError("probabilities must be a finite one-dimensional array")
    if not 0.0 <= f1_threshold <= 1.0 or not 0.0 <= q90_cutoff <= 1.0:
        raise ValueError("probability cutoffs must lie in [0, 1]")
    denominator = 1.0 - f1_threshold
    confidence = np.zeros_like(values, dtype=float)
    if denominator > 0:
        confidence = np.clip((values - f1_threshold) / denominator, 0.0, 1.0)
    return {
        "binary_f1": (values >= f1_threshold).astype(float),
        "top_decile": (values >= q90_cutoff).astype(float),
        "confidence_weighted": confidence,
        "probability_proportional": np.clip(values, 0.0, 1.0),
    }


def _non_overlapping_entry_indices(
    timestamps: pd.Series | pd.DatetimeIndex | np.ndarray,
    weights: np.ndarray,
    horizon_minutes: int,
) -> np.ndarray:
    """Select positive-weight entries while flat, resetting per supplied block."""
    if horizon_minutes < 1:
        raise ValueError("position horizon must be positive")
    times = pd.DatetimeIndex(pd.to_datetime(timestamps, utc=True, errors="raise"))
    sizes = np.asarray(weights, dtype=float)
    if sizes.ndim != 1 or len(times) != len(sizes):
        raise ValueError("timestamps and weights must be aligned one-dimensional arrays")
    if not np.isfinite(sizes).all() or ((sizes < 0) | (sizes > 1)).any():
        raise ValueError("position weights must be finite and lie in [0, 1]")
    if not times.is_monotonic_increasing:
        raise ValueError("timestamps must be chronological")

    selected: list[int] = []
    next_flat_time: pd.Timestamp | None = None
    horizon = pd.Timedelta(minutes=horizon_minutes)
    for index, (timestamp, weight) in enumerate(zip(times, sizes, strict=True)):
        if weight <= 0 or (next_flat_time is not None and timestamp < next_flat_time):
            continue
        selected.append(index)
        next_flat_time = timestamp + horizon
    return np.asarray(selected, dtype=int)


def _simulate_non_overlapping_positions(
    timestamps: pd.Series | pd.DatetimeIndex | np.ndarray,
    forward_net_return_bps: np.ndarray,
    weights: np.ndarray,
    horizon_minutes: int,
) -> dict[str, float | int | None]:
    """Simulate one fixed-horizon BTC-proxy position at a time within a holdout."""
    returns = np.asarray(forward_net_return_bps, dtype=float)
    sizes = np.asarray(weights, dtype=float)
    if returns.ndim != 1 or len(returns) != len(sizes):
        raise ValueError("forward returns and weights must be aligned one-dimensional arrays")
    if not np.isfinite(returns).all():
        raise ValueError("forward net returns must all be finite")

    entry_indices = _non_overlapping_entry_indices(timestamps, sizes, horizon_minutes)
    entry_weights = sizes[entry_indices]
    trade_pnl = entry_weights * returns[entry_indices]
    exposure = float(entry_weights.sum())
    net_pnl = float(trade_pnl.sum())
    if len(trade_pnl):
        equity_curve = np.exp(np.cumsum(trade_pnl) / 10_000.0)
        equity_with_initial = np.concatenate(([1.0], equity_curve))
        running_peak = np.maximum.accumulate(equity_with_initial)
        max_drawdown = float(np.max(1.0 - equity_with_initial / running_peak))
        mean_trade_pnl: float | None = float(trade_pnl.mean())
        median_trade_pnl: float | None = float(np.median(trade_pnl))
    else:
        max_drawdown = 0.0
        mean_trade_pnl = None
        median_trade_pnl = None
    sharpe_per_trade: float | None = None
    sharpe_annualized: float | None = None
    if len(trade_pnl) >= 2:
        standard_deviation = float(trade_pnl.std(ddof=1))
        if standard_deviation > 0:
            sharpe_per_trade = float(trade_pnl.mean()) / standard_deviation
            periods_per_year = (365.0 * 24.0 * 60.0) / float(horizon_minutes)
            sharpe_annualized = sharpe_per_trade * math.sqrt(periods_per_year)
    return {
        "candidate_rows": int(len(sizes)),
        "signal_rows": int(np.count_nonzero(sizes > 0)),
        "actual_entries": int(len(entry_indices)),
        "selection_rate": float(len(entry_indices) / len(sizes)) if len(sizes) else 0.0,
        "exposure_sum": exposure,
        "positive_trades": int(np.count_nonzero(trade_pnl > 0)),
        "negative_trades": int(np.count_nonzero(trade_pnl < 0)),
        "zero_trades": int(np.count_nonzero(trade_pnl == 0)),
        "net_pnl_bps": net_pnl,
        "mean_trade_pnl_bps": mean_trade_pnl,
        "median_trade_pnl_bps": median_trade_pnl,
        "pnl_per_unit_exposure_bps": float(net_pnl / exposure) if exposure > 0 else None,
        "sequential_ending_equity": float(np.exp(net_pnl / 10_000.0)),
        "max_drawdown": max_drawdown,
        "sharpe_per_trade": sharpe_per_trade,
        "sharpe_annualized": sharpe_annualized,
        "trade_pnl_series": [float(value) for value in trade_pnl],
    }


def _summary(values: Iterable[float]) -> dict[str, float | list[float]]:
    array = np.asarray(list(values), dtype=float)
    return {
        "mean": float(array.mean()),
        "sd": float(array.std(ddof=0)),
        "range": [float(array.min()), float(array.max())],
    }


def _aggregate(holdouts: dict[str, dict[str, Any]], model_names: Iterable[str]) -> dict[str, Any]:
    valid = [entry for entry in holdouts.values() if entry["valid"]]
    result: dict[str, Any] = {}
    for model_name in model_names:
        result[model_name] = {
            metric: _summary(
                (
                    entry["majority_baseline"]
                    if model_name == "majority_baseline"
                    else entry["models"][model_name]
                )[metric]
                for entry in valid
            )
            for metric in ("accuracy", "auc", "pr_auc", "precision", "recall")
        }
    return result


SIZING_METRIC_NAMES = (
    "candidate_rows",
    "signal_rows",
    "actual_entries",
    "selection_rate",
    "exposure_sum",
    "positive_trades",
    "negative_trades",
    "zero_trades",
    "net_pnl_bps",
    "mean_trade_pnl_bps",
    "median_trade_pnl_bps",
    "pnl_per_unit_exposure_bps",
    "sequential_ending_equity",
    "max_drawdown",
    "sharpe_per_trade",
    "sharpe_annualized",
    "delta_vs_binary_net_pnl_bps",
)


def _optional_summary(values: Iterable[float | int | None]) -> dict[str, Any]:
    supplied = list(values)
    numeric = [float(value) for value in supplied if value is not None]
    if not numeric:
        return {
            "mean": None,
            "sd": None,
            "range": [None, None],
            "holdouts_with_value": 0,
            "valid_holdouts": int(len(supplied)),
        }
    summary = _summary(numeric)
    summary["holdouts_with_value"] = int(len(numeric))
    summary["valid_holdouts"] = int(len(supplied))
    return summary


def _pooled_sizing(
    holdouts: dict[str, dict[str, Any]], model_names: Iterable[str], horizon_minutes: int
) -> dict[str, Any]:
    """Pool every trade across all valid holdouts, in holdout order.

    The pooled series concatenates per-holdout trade PnL chronologically
    (holdout A, then B, then C).  Pooled Sharpe is the equity-curve Sharpe of
    the combined sequential positions and equals the annualized trade Sharpe
    because each trade contributes ``pnl_bps / 10000`` to the equity curve.
    """
    valid_names = [name for name, entry in holdouts.items() if entry["valid"]]
    pooled: dict[str, Any] = {
        "valid_holdouts": valid_names,
        "holdout_order": HOLDOUT_NAMES,
        "models": {},
    }
    for model_name in model_names:
        model_pooled: dict[str, Any] = {}
        for policy_name in SIZING_POLICY_NAMES:
            series = np.concatenate([
                holdouts[name]["models"][model_name]["sizing_policies"][policy_name][
                    "trade_pnl_series"
                ]
                for name in valid_names
            ])
            exposures = [
                holdouts[name]["models"][model_name]["sizing_policies"][policy_name][
                    "exposure_sum"
                ]
                for name in valid_names
            ]
            entries = [
                holdouts[name]["models"][model_name]["sizing_policies"][policy_name][
                    "actual_entries"
                ]
                for name in valid_names
            ]
            net_pnl = float(series.sum())
            exposure = float(sum(exposures))
            entry: dict[str, Any] = {
                "total_trades": int(len(series)),
                "per_holdout_entries": [int(value) for value in entries],
                "total_exposure": exposure,
                "total_net_pnl_bps": net_pnl,
                "pnl_per_unit_exposure_bps": float(net_pnl / exposure) if exposure > 0 else None,
                "sharpe_per_trade": None,
                "sharpe_annualized": None,
                "max_drawdown": 0.0,
                "sequential_ending_equity": float(np.exp(net_pnl / 10_000.0)),
            }
            if len(series) >= 2:
                standard_deviation = float(series.std(ddof=1))
                if standard_deviation > 0:
                    entry["sharpe_per_trade"] = float(series.mean()) / standard_deviation
                    entry["sharpe_annualized"] = entry["sharpe_per_trade"] * math.sqrt(
                        (365.0 * 24.0 * 60.0) / float(horizon_minutes)
                    )
                if len(series):
                    equity = np.exp(np.cumsum(series) / 10_000.0)
                    equity_with_initial = np.concatenate(([1.0], equity))
                    running_peak = np.maximum.accumulate(equity_with_initial)
                    entry["max_drawdown"] = float(np.max(1.0 - equity_with_initial / running_peak))
            model_pooled[policy_name] = entry
        pooled["models"][model_name] = model_pooled
    return pooled


def _aggregate_sizing(
    holdouts: dict[str, dict[str, Any]], model_names: Iterable[str]
) -> dict[str, Any]:
    """Aggregate sizing results from valid holdouts only."""
    valid_names = [name for name, entry in holdouts.items() if entry["valid"]]
    invalid_names = [name for name, entry in holdouts.items() if not entry["valid"]]
    result: dict[str, Any] = {
        "valid_holdouts": valid_names,
        "invalid_holdouts_excluded": invalid_names,
        "valid_holdout_count": int(len(valid_names)),
        "all_three_holdouts_valid": len(valid_names) == len(HOLDOUT_NAMES),
        "models": {},
    }
    for model_name in model_names:
        model_result: dict[str, Any] = {}
        for policy_name in SIZING_POLICY_NAMES:
            per_holdout = [
                holdouts[name]["models"][model_name]["sizing_policies"][policy_name]
                for name in valid_names
            ]
            deltas = [float(item["delta_vs_binary_net_pnl_bps"]) for item in per_holdout]
            delta_signs = [int(np.sign(delta)) for delta in deltas]
            model_result[policy_name] = {
                metric: _optional_summary(item[metric] for item in per_holdout)
                for metric in SIZING_METRIC_NAMES
            }
            model_result[policy_name]["stability"] = {
                "all_three_holdouts_valid": len(valid_names) == len(HOLDOUT_NAMES),
                "net_pnl_positive_on_all_valid_holdouts": bool(per_holdout)
                and all(float(item["net_pnl_bps"]) > 0 for item in per_holdout),
                "delta_vs_binary_same_sign_on_all_valid_holdouts": bool(delta_signs)
                and len(set(delta_signs)) == 1,
                "delta_vs_binary_positive_on_all_valid_holdouts": bool(deltas)
                and all(delta > 0 for delta in deltas),
                "delta_vs_binary_positive_on_all_three_holdouts": len(valid_names)
                == len(HOLDOUT_NAMES)
                and all(delta > 0 for delta in deltas),
            }
        result["models"][model_name] = model_result
    return result


def _fit_models(train: pd.DataFrame, feature_columns: list[str]) -> dict[str, Any]:
    features = train[feature_columns]
    labels = train[LABEL_COLUMN].to_numpy()
    models = {
        "logistic_regression": Pipeline(
            [
                ("scale", StandardScaler()),
                ("model", LogisticRegression(max_iter=2_000, random_state=RANDOM_SEED)),
            ]
        ).fit(features, labels),
        "gradient_boosted_trees": HistGradientBoostingClassifier(
            learning_rate=0.05,
            max_iter=250,
            max_leaf_nodes=15,
            l2_regularization=1.0,
            random_state=RANDOM_SEED,
        ).fit(features, labels),
    }
    return models


def evaluate_frame(
    frame: pd.DataFrame,
    feature_columns: Iterable[str] = EXPANDED_FEATURE_COLUMNS,
    embargo_rows: int = EMBARGO_ROWS,
    embargo_minutes: int = EMBARGO_MINUTES,
) -> dict[str, Any]:
    """Evaluate causal features and label using the prescribed chronological protocol."""
    columns = list(feature_columns)
    required = [TIME_COLUMN, LABEL_COLUMN, RETURN_COLUMN, *columns]
    missing = [column for column in required if column not in frame.columns]
    if missing:
        raise ValueError(f"frame is missing required columns: {missing}")

    clean = frame.loc[:, required].copy()
    clean[TIME_COLUMN] = pd.to_datetime(clean[TIME_COLUMN], utc=True, errors="raise")
    clean[LABEL_COLUMN] = pd.to_numeric(clean[LABEL_COLUMN], errors="raise").astype(int)
    clean[RETURN_COLUMN] = pd.to_numeric(clean[RETURN_COLUMN], errors="raise").astype(float)
    clean = clean.dropna(subset=required).sort_values(TIME_COLUMN, kind="stable").reset_index(drop=True)
    labels = sorted(int(value) for value in clean[LABEL_COLUMN].unique())
    if len(labels) != 2:
        raise ValueError(f"the proxy label must have exactly two classes; observed {labels}")

    if not clean[TIME_COLUMN].is_unique:
        raise ValueError("the chronological evaluator requires unique timestamps")
    partitions = chronological_partitions(clean, embargo_rows=embargo_rows, embargo_minutes=embargo_minutes)
    retained_rows = sum(len(partition) for partition in partitions.values())
    expected_classes = set(labels)
    train = partitions["train"]
    if set(train[LABEL_COLUMN]) != expected_classes:
        raise ValueError("the chronological train block is missing a proxy-label class")

    positive_label = max(labels)
    majority_label = int(train[LABEL_COLUMN].value_counts().idxmax())
    models = _fit_models(train, columns)
    train_calibration = {
        model_name: _train_calibration(train, model, columns, positive_label)
        for model_name, model in models.items()
    }
    holdouts: dict[str, dict[str, Any]] = {}
    for name in HOLDOUT_NAMES:
        holdout = partitions[name]
        distribution = _distribution(holdout[LABEL_COLUMN])
        missing_classes = sorted(expected_classes.difference(distribution))
        entry: dict[str, Any] = {
            **_time_summary(holdout),
            "valid": not missing_classes,
            "missing_classes": missing_classes,
        }
        if missing_classes:
            entry["invalid_reason"] = "holdout is missing required proxy-label classes"
            holdouts[name] = entry
            continue

        actual = holdout[LABEL_COLUMN].to_numpy()
        baseline_prediction = np.full(len(holdout), majority_label, dtype=int)
        baseline_probability = np.full(
            len(holdout), float(train[LABEL_COLUMN].eq(positive_label).mean()), dtype=float
        )
        entry["majority_baseline"] = _metrics(
            actual, baseline_prediction, baseline_probability, positive_label
        )
        entry["majority_baseline"]["f1_threshold"] = float(baseline_probability[0])
        entry["majority_baseline"]["calibrated"] = entry["majority_baseline"].copy()
        entry["models"] = {}
        for model_name, model in models.items():
            probabilities = model.predict_proba(holdout[columns])
            class_position = list(model.classes_).index(positive_label)
            positive_probabilities = probabilities[:, class_position]
            default_metrics = _metrics(
                actual,
                model.predict(holdout[columns]),
                positive_probabilities,
                positive_label,
            )
            calibration = train_calibration[model_name]
            threshold = float(calibration["train_f1_threshold"])
            q90_cutoff = float(calibration["train_probability_q90"])
            calibrated_prediction = (positive_probabilities >= threshold).astype(int)
            default_metrics["train_f1_threshold"] = threshold
            default_metrics["train_probability_q90"] = q90_cutoff
            default_metrics["calibrated"] = _metrics(
                actual, calibrated_prediction, positive_probabilities, positive_label
            )
            sizing_policies = {
                policy_name: _simulate_non_overlapping_positions(
                    holdout[TIME_COLUMN],
                    holdout[RETURN_COLUMN].to_numpy(dtype=float),
                    weights,
                    embargo_minutes,
                )
                for policy_name, weights in _policy_weights(
                    positive_probabilities, threshold, q90_cutoff
                ).items()
            }
            binary_pnl = float(sizing_policies["binary_f1"]["net_pnl_bps"])
            for policy_result in sizing_policies.values():
                policy_result["delta_vs_binary_net_pnl_bps"] = (
                    float(policy_result["net_pnl_bps"]) - binary_pnl
                )
            default_metrics["sizing_policies"] = sizing_policies
            entry["models"][model_name] = default_metrics
        holdouts[name] = entry

    valid_holdout_count = sum(entry["valid"] for entry in holdouts.values())
    if not valid_holdout_count:
        raise ValueError("all later holdouts are invalid because at least one class is missing")

    model_names = tuple(models)
    aggregate = _aggregate(holdouts, ("majority_baseline", *model_names))
    sizing_aggregate = _aggregate_sizing(holdouts, model_names)
    pooled_sizing = _pooled_sizing(holdouts, model_names, embargo_minutes)
    models_beating_all_valid_holdouts = [
        model_name
        for model_name in model_names
        if all(
            entry["models"][model_name]["pr_auc"] > entry["majority_baseline"]["pr_auc"]
            and entry["models"][model_name]["calibrated"]["accuracy"]
            > entry["majority_baseline"]["calibrated"]["accuracy"]
            for entry in holdouts.values()
            if entry["valid"]
        )
    ]
    if models_beating_all_valid_holdouts:
        sequence_model = {
            "status": "eligible_but_not_run",
            "reason": "a baseline cleared every valid holdout; sequence work requires a separately scoped design",
            "qualifying_baselines": models_beating_all_valid_holdouts,
        }
    else:
        sequence_model = {
            "status": "skipped",
            "reason": "no baseline beat the majority PR-AUC and train-calibrated accuracy on every valid holdout",
            "qualifying_baselines": [],
        }

    return {
        "protocol": {
            "offline_only": True,
            "random_seed": RANDOM_SEED,
            "train_fraction": 0.55,
            "feature_columns": columns,
            "label_column": LABEL_COLUMN,
            "forward_return_column": RETURN_COLUMN,
            "classes": labels,
            "positive_label": positive_label,
            "majority_label_from_train": majority_label,
            "standard_deviation": "population (ddof=0) across valid holdouts",
            "embargo": {
                "rows": embargo_rows,
                "minutes": embargo_minutes,
                "rule": "the preceding block ends before its forward-label window can reach the next block",
            },
            "sizing": {
                "calibration_scope": "fitted train predictions only; frozen before all holdouts",
                "policies": {
                    "binary_f1": "unit size when probability >= train F1-maximizing threshold",
                    "top_decile": "unit size when probability >= train probability q90 (higher order statistic)",
                    "confidence_weighted": "clip((probability - train F1 threshold) / (1 - train F1 threshold), 0, 1)",
                    "probability_proportional": "size equals the predicted positive-class probability, clipped to [0, 1]",
                },
                "position_horizon_minutes": embargo_minutes,
                "overlap_rule": "enter only while flat; equality at the prior exit timestamp is eligible; reset flat at each holdout boundary",
                "cost_handling": "forward_net_return_bps already includes the configured round-trip cost; no additional cost is deducted",
                "sharpe": {
                    "sharpe_per_trade": "mean(trade_pnl_bps) / std(trade_pnl_bps, ddof=1); undefined (None) with fewer than two trades or zero standard deviation",
                    "sharpe_annualized": "sharpe_per_trade * sqrt((365*24*60) / position_horizon_minutes); equals the equity-curve Sharpe because equity returns are proportional to trade PnL in bps",
                },
            },
        },
        "partitions": {name: _time_summary(partition) for name, partition in partitions.items()},
        "sample_accounting": {
            "input_rows": int(len(frame)),
            "feature_complete_labelled_rows": int(len(clean)),
            "rows_removed_for_missing_features": int(len(frame) - len(clean)),
            "rows_removed_by_embargo": int(len(clean) - retained_rows),
            "retained_rows_after_embargo": int(retained_rows),
        },
        "feature_label_correlations": {
            "scope": "train only after embargo",
            "method": "Pearson correlation over complete pairs",
            "values": _feature_label_correlations(train, columns),
        },
        "train_calibration": train_calibration,
        "holdouts": holdouts,
        "aggregate": aggregate,
        "sequence_model": sequence_model,
        "pnl": {
            "status": "available_btc_proxy_only",
            "warning": "This is BTC-proxy strategy PnL, not executable Polymarket PnL.",
            "return_column": RETURN_COLUMN,
            "aggregate": sizing_aggregate,
            "pooled": pooled_sizing,
            "executable_polymarket_pnl": {
                "status": "unavailable",
                "reason": "the proxy frame has no executable Polymarket quotes, depth, fees, fills, or strict-gate outcomes required by the arbitrage backtest",
            },
        },
    }


def build_evaluation_frame(
    candles: pd.DataFrame,
    high_events: list[pd.Timestamp],
    horizon_minutes: int,
    round_trip_cost_bps: float,
) -> pd.DataFrame:
    """Join the existing proxy frame to the expanded, causal offline features."""
    proxy_frame = build_feature_frame(candles, high_events, horizon_minutes, round_trip_cost_bps)
    causal_features, _ = build_features(candles, horizon=horizon_minutes)
    causal_frame = causal_features.reset_index().rename(columns={"index": TIME_COLUMN})
    causal_frame[TIME_COLUMN] = pd.to_datetime(causal_frame[TIME_COLUMN], utc=True, errors="raise")
    proxy_frame = proxy_frame.copy()
    proxy_frame[TIME_COLUMN] = pd.to_datetime(proxy_frame[TIME_COLUMN], utc=True, errors="raise")
    added_columns = [column for column in CAUSAL_FEATURE_COLUMNS if column not in FEATURE_COLUMNS]
    return proxy_frame.merge(
        causal_frame[[TIME_COLUMN, *added_columns]],
        on=TIME_COLUMN,
        how="inner",
        validate="one_to_one",
    )


def evaluate_candles(
    candles: pd.DataFrame,
    high_events: list[pd.Timestamp],
    horizon_minutes: int = 15,
    round_trip_cost_bps: float = DEFAULT_ROUND_TRIP_COST_BPS,
) -> dict[str, Any]:
    """Build the proxy label plus the expanded causal features, then evaluate."""
    frame = build_evaluation_frame(candles, high_events, horizon_minutes, round_trip_cost_bps)
    artifact = evaluate_frame(
        frame,
        embargo_rows=horizon_minutes,
        embargo_minutes=horizon_minutes,
    )
    artifact["input"] = {
        "candles_rows": int(len(candles)),
        "proxy_labelled_rows": int(len(frame)),
        "horizon_minutes": horizon_minutes,
        "round_trip_cost_bps": round_trip_cost_bps,
        "high_event_count": int(len(high_events)),
    }
    return artifact


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json_exclusive(path: Path, artifact: dict[str, Any]) -> None:
    """Create a new artifact atomically with respect to accidental overwrites."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(artifact, handle, indent=2, sort_keys=True)
        handle.write("\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candles", type=Path, required=True, help="local normalized OHLCV parquet")
    macro_group = parser.add_mutually_exclusive_group(required=True)
    macro_group.add_argument(
        "--macro-calendar",
        type=Path,
        help="local anonymous macro JSON (legacy-compatible input name)",
    )
    macro_group.add_argument(
        "--macro-snapshot",
        type=Path,
        help="immutable dated anonymous macro JSON snapshot",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="new artifact JSON path; an existing file is never overwritten",
    )
    parser.add_argument("--horizon-minutes", type=int, default=15)
    parser.add_argument("--round-trip-cost-bps", type=float, default=DEFAULT_ROUND_TRIP_COST_BPS)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    macro_path = args.macro_snapshot or args.macro_calendar
    candles_sha256 = _sha256_file(args.candles)
    macro_sha256 = _sha256_file(macro_path)
    candles = pd.read_parquet(args.candles)
    high_events, macro_metadata = load_anonymous_macro_calendar(macro_path)
    if _sha256_file(args.candles) != candles_sha256:
        raise RuntimeError("candles input changed while the evaluator was reading it")
    if _sha256_file(macro_path) != macro_sha256:
        raise RuntimeError("macro input changed while the evaluator was reading it")
    artifact = evaluate_candles(
        candles,
        high_events,
        horizon_minutes=args.horizon_minutes,
        round_trip_cost_bps=args.round_trip_cost_bps,
    )
    artifact["input"]["candles_path"] = str(args.candles.resolve())
    artifact["input"]["macro_path"] = str(macro_path.resolve())
    artifact["input"]["macro_input_mode"] = (
        "immutable_snapshot" if args.macro_snapshot is not None else "legacy_calendar_path"
    )
    artifact["input"]["sha256"] = {
        "candles": candles_sha256,
        "macro": macro_sha256,
    }
    artifact["input"]["input_unchanged_during_read"] = True
    artifact["input"]["macro_calendar"] = macro_metadata
    artifact["input"]["output_path"] = str(args.output.resolve())
    artifact["input"]["output_overwrite_allowed"] = False
    _write_json_exclusive(args.output, artifact)
    print(json.dumps(artifact, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
