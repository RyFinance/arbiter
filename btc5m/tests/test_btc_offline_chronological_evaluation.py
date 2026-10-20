from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import scripts.evaluate_btc_offline_profitability as evaluator
from scripts.diagnose_lightgbm_macro_regimes import FEATURE_COLUMNS, build_feature_frame
from scripts.evaluate_btc_offline_profitability import (
    EMBARGO_MINUTES,
    EMBARGO_ROWS,
    EXPANDED_FEATURE_COLUMNS,
    HOLDOUT_NAMES,
    RETURN_COLUMN,
    SIZING_POLICY_NAMES,
    _non_overlapping_entry_indices,
    _policy_weights,
    _sha256_file,
    _simulate_non_overlapping_positions,
    _train_probability_q90,
    _write_json_exclusive,
    build_evaluation_frame,
    chronological_partitions,
    evaluate_frame,
)


def _candles(rows: int = 2_400) -> tuple[pd.DataFrame, list[pd.Timestamp]]:
    timestamps = pd.date_range("2026-01-01", periods=rows, freq="min", tz="UTC")
    steps = np.arange(rows)
    close = 90_000 + np.cumsum(22 * np.sin(steps / 7) + 14 * np.cos(steps / 29) + (steps % 17 - 8))
    candles = pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": close - 3,
            "high": close + 7,
            "low": close - 7,
            "close": close,
            "volume": 30 + steps % 11,
        }
    )
    events = list(pd.date_range("2026-01-01 00:30", periods=60, freq="60min", tz="UTC"))
    return candles, events


class _DeterministicProbabilityModel:
    classes_ = np.array([0, 1])

    def predict_proba(self, features: pd.DataFrame) -> np.ndarray:
        positive = features["signal"].to_numpy(dtype=float)
        return np.column_stack((1.0 - positive, positive))

    def predict(self, features: pd.DataFrame) -> np.ndarray:
        return (features["signal"].to_numpy(dtype=float) >= 0.5).astype(int)


def _small_proxy_frame(rows: int = 240) -> pd.DataFrame:
    timestamps = pd.date_range("2026-03-01", periods=rows, freq="min", tz="UTC")
    signal = np.resize([0.1, 0.2, 0.8, 0.9], rows)
    labels = np.resize([0, 0, 1, 1], rows)
    return pd.DataFrame(
        {
            "timestamp": timestamps,
            "profitability_target": labels,
            RETURN_COLUMN: np.where(labels == 1, 7.5, -4.0),
            "signal": signal,
        }
    )


def test_evaluation_uses_causal_frame_and_reports_all_required_holdouts() -> None:
    candles, events = _candles()
    frame = build_evaluation_frame(candles, events, horizon_minutes=15, round_trip_cost_bps=20)
    artifact = evaluate_frame(frame)

    assert set(artifact["protocol"]["feature_columns"]) == set(EXPANDED_FEATURE_COLUMNS)
    assert set(FEATURE_COLUMNS) < set(artifact["protocol"]["feature_columns"])
    assert set(artifact["protocol"]["classes"]) == {0, 1}
    assert set(artifact["holdouts"]) == set(HOLDOUT_NAMES)
    assert RETURN_COLUMN in frame
    assert artifact["protocol"]["forward_return_column"] == RETURN_COLUMN
    assert artifact["pnl"]["status"] == "available_btc_proxy_only"
    assert artifact["pnl"]["warning"] == (
        "This is BTC-proxy strategy PnL, not executable Polymarket PnL."
    )
    assert artifact["pnl"]["executable_polymarket_pnl"]["status"] == "unavailable"
    assert artifact["sequence_model"]["status"] in {"skipped", "eligible_but_not_run"}
    assert artifact["protocol"]["embargo"] == {
        "rows": EMBARGO_ROWS,
        "minutes": EMBARGO_MINUTES,
        "rule": "the preceding block ends before its forward-label window can reach the next block",
    }
    correlations = artifact["feature_label_correlations"]
    assert correlations["scope"] == "train only after embargo"
    assert set(correlations["values"]) == set(EXPANDED_FEATURE_COLUMNS)
    accounting = artifact["sample_accounting"]
    assert accounting["input_rows"] == (
        accounting["feature_complete_labelled_rows"] + accounting["rows_removed_for_missing_features"]
    )
    assert accounting["rows_removed_by_embargo"] == EMBARGO_ROWS * 3
    for name in HOLDOUT_NAMES:
        holdout = artifact["holdouts"][name]
        assert holdout["valid"]
        assert set(holdout["label_distribution"]) == {0, 1}
        assert set(holdout["models"]) == {"logistic_regression", "gradient_boosted_trees"}
        for metrics in [holdout["majority_baseline"], *holdout["models"].values()]:
            assert {"accuracy", "auc", "pr_auc", "precision", "recall"} <= set(metrics)
            assert all(0 <= metrics[key] <= 1 for key in ("accuracy", "auc", "pr_auc", "precision", "recall"))
        for model_name in ("logistic_regression", "gradient_boosted_trees"):
            model_result = holdout["models"][model_name]
            calibrated = model_result["calibrated"]
            assert {"accuracy", "auc", "pr_auc", "precision", "recall"} <= set(calibrated)
            assert set(model_result["sizing_policies"]) == set(SIZING_POLICY_NAMES)
            for policy in model_result["sizing_policies"].values():
                assert {
                    "candidate_rows",
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
                    "delta_vs_binary_net_pnl_bps",
                } <= set(policy)
    for model in ("majority_baseline", "logistic_regression", "gradient_boosted_trees"):
        assert set(artifact["aggregate"][model]) == {"accuracy", "auc", "pr_auc", "precision", "recall"}
        assert set(artifact["aggregate"][model]["accuracy"]) == {"mean", "sd", "range"}
    assert artifact["pnl"]["aggregate"]["valid_holdout_count"] == 3
    assert artifact["pnl"]["aggregate"]["all_three_holdouts_valid"] is True
    assert set(artifact["train_calibration"]) == {
        "logistic_regression",
        "gradient_boosted_trees",
    }
    assert all(
        calibration["source"] == "fitted train predictions only"
        for calibration in artifact["train_calibration"].values()
    )


def test_holdout_missing_a_class_is_invalidated() -> None:
    timestamps = pd.date_range("2026-02-01", periods=120, freq="min", tz="UTC")
    labels = np.resize([0, 1], 120)
    labels[66:84] = 0  # 55% train ends at 66; the first later block is holdout A.
    frame = pd.DataFrame({"timestamp": timestamps, "profitability_target": labels})
    frame[RETURN_COLUMN] = np.where(labels == 1, 10.0, -10.0)
    for number, column in enumerate(FEATURE_COLUMNS):
        frame[column] = number + np.arange(len(frame)) / 100

    artifact = evaluate_frame(frame, feature_columns=FEATURE_COLUMNS)

    assert artifact["holdouts"]["holdout_a"]["valid"] is False
    assert artifact["holdouts"]["holdout_a"]["missing_classes"] == [1]
    assert "models" not in artifact["holdouts"]["holdout_a"]
    assert artifact["holdouts"]["holdout_b"]["valid"] is True
    assert artifact["holdouts"]["holdout_c"]["valid"] is True
    sizing_aggregate = artifact["pnl"]["aggregate"]
    assert sizing_aggregate["valid_holdouts"] == ["holdout_b", "holdout_c"]
    assert sizing_aggregate["invalid_holdouts_excluded"] == ["holdout_a"]
    assert sizing_aggregate["valid_holdout_count"] == 2
    assert sizing_aggregate["all_three_holdouts_valid"] is False
    candidate_summary = sizing_aggregate["models"]["logistic_regression"]["binary_f1"][
        "candidate_rows"
    ]
    assert candidate_summary == {
        "mean": 10.5,
        "sd": 7.5,
        "range": [3.0, 18.0],
        "holdouts_with_value": 2,
        "valid_holdouts": 2,
    }


def test_train_label_windows_do_not_overlap_any_holdout_after_embargo() -> None:
    timestamps = pd.date_range("2026-02-01", periods=200, freq="min", tz="UTC")
    frame = pd.DataFrame({"timestamp": timestamps, "profitability_target": np.resize([0, 1], len(timestamps))})
    partitions = chronological_partitions(frame)
    first_holdout_time = min(
        partition["timestamp"].iloc[0] for name, partition in partitions.items() if name in HOLDOUT_NAMES
    )
    train_label_window_end = partitions["train"]["timestamp"] + pd.Timedelta(minutes=15)

    assert (train_label_window_end < first_holdout_time).all()
    assert len(partitions["train"]) == int(np.floor(len(frame) * 0.55)) - EMBARGO_ROWS


def test_holdout_partitioning_preserves_dataframe_columns_and_dtypes() -> None:
    timestamps = pd.date_range("2026-02-01", periods=120, freq="min", tz="UTC")
    frame = pd.DataFrame(
        {
            "timestamp": timestamps,
            "profitability_target": np.resize([0, 1], len(timestamps)),
            "category": pd.Series(["low", "high"] * 60, dtype="category"),
            "nullable_count": pd.Series(range(120), dtype="Int64"),
        }
    )

    partitions = chronological_partitions(frame, embargo_rows=1, embargo_minutes=1)

    assert all(isinstance(partition, pd.DataFrame) for partition in partitions.values())
    assert all(list(partition.columns) == list(frame.columns) for partition in partitions.values())
    assert all(partition["category"].dtype == frame["category"].dtype for partition in partitions.values())
    assert all(partition["nullable_count"].dtype == frame["nullable_count"].dtype for partition in partitions.values())
    assert [len(partitions[name]) for name in HOLDOUT_NAMES] == [17, 17, 18]


def test_f1_and_q90_calibration_are_frozen_from_train_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        evaluator,
        "_fit_models",
        lambda _train, _columns: {
            "logistic_regression": _DeterministicProbabilityModel(),
            "gradient_boosted_trees": _DeterministicProbabilityModel(),
        },
    )
    original = _small_proxy_frame()
    changed_holdouts = original.copy(deep=True)
    raw_train_end = int(np.floor(len(original) * 0.55))
    later = changed_holdouts.index >= raw_train_end
    changed_holdouts.loc[later, "profitability_target"] = (
        1 - changed_holdouts.loc[later, "profitability_target"]
    )
    changed_holdouts.loc[later, RETURN_COLUMN] *= -100.0
    changed_holdouts.loc[later, "signal"] = 1.0 - changed_holdouts.loc[later, "signal"]

    first = evaluate_frame(original, feature_columns=["signal"])
    second = evaluate_frame(changed_holdouts, feature_columns=["signal"])

    assert first["train_calibration"] == second["train_calibration"]
    retained_train = chronological_partitions(original)["train"]
    expected_q90 = _train_probability_q90(retained_train["signal"].to_numpy())
    for model_name, calibration in first["train_calibration"].items():
        assert calibration["train_rows"] == len(retained_train)
        assert calibration["train_probability_q90"] == expected_q90
        for holdout_name in HOLDOUT_NAMES:
            model_result = first["holdouts"][holdout_name]["models"][model_name]
            assert model_result["train_f1_threshold"] == calibration["train_f1_threshold"]
            assert model_result["train_probability_q90"] == calibration["train_probability_q90"]


def test_q90_ties_are_deterministic_and_use_an_observed_train_probability() -> None:
    probabilities = np.array([0.1, 0.2, 0.3, 0.7, 0.7, 0.7, 0.7, 0.7, 0.7, 0.7])

    cutoffs = [_train_probability_q90(probabilities.copy()) for _ in range(5)]
    weights = _policy_weights(probabilities, f1_threshold=0.3, q90_cutoff=cutoffs[0])

    assert cutoffs == [0.7] * 5
    assert cutoffs[0] in probabilities
    np.testing.assert_array_equal(
        weights["top_decile"],
        np.array([0.0, 0.0, 0.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0]),
    )


def test_policy_weights_are_monotonic_bounded_and_binary_matches_calibrated_prediction() -> None:
    probabilities = np.array([0.0, 0.19, 0.2, 0.5, 0.9, 1.0])

    weights = _policy_weights(probabilities, f1_threshold=0.2, q90_cutoff=0.9)

    np.testing.assert_array_equal(
        weights["binary_f1"], (probabilities >= 0.2).astype(float)
    )
    np.testing.assert_array_equal(
        weights["top_decile"], (probabilities >= 0.9).astype(float)
    )
    assert np.all(np.diff(weights["confidence_weighted"]) >= 0)
    assert np.all((0.0 <= weights["confidence_weighted"]) & (weights["confidence_weighted"] <= 1.0))
    np.testing.assert_allclose(
        weights["confidence_weighted"],
        np.clip((probabilities - 0.2) / 0.8, 0.0, 1.0),
        rtol=0,
        atol=0,
    )
    np.testing.assert_array_equal(
        _policy_weights(probabilities, f1_threshold=1.0, q90_cutoff=1.0)[
            "confidence_weighted"
        ],
        np.zeros_like(probabilities),
    )


def test_non_overlapping_simulation_matches_hand_calculated_pnl_and_drawdown() -> None:
    timestamps = pd.DatetimeIndex(
        pd.to_datetime(
            [
                "2026-04-01T00:00:00Z",
                "2026-04-01T00:05:00Z",
                "2026-04-01T00:15:00Z",
                "2026-04-01T00:31:00Z",
            ]
        )
    )
    returns = np.array([100.0, 999.0, -50.0, 20.0])
    weights = np.array([1.0, 1.0, 0.5, 1.0])

    indices = _non_overlapping_entry_indices(timestamps, weights, horizon_minutes=15)
    result = _simulate_non_overlapping_positions(
        timestamps, returns, weights, horizon_minutes=15
    )

    np.testing.assert_array_equal(indices, np.array([0, 2, 3]))
    assert result["candidate_rows"] == 4
    assert result["signal_rows"] == 4
    assert result["actual_entries"] == 3
    assert result["selection_rate"] == 0.75
    assert result["exposure_sum"] == 2.5
    assert result["positive_trades"] == 2
    assert result["negative_trades"] == 1
    assert result["zero_trades"] == 0
    assert result["net_pnl_bps"] == 95.0
    assert result["mean_trade_pnl_bps"] == pytest.approx(95.0 / 3.0)
    assert result["median_trade_pnl_bps"] == 20.0
    assert result["pnl_per_unit_exposure_bps"] == 38.0
    assert result["sequential_ending_equity"] == pytest.approx(np.exp(95.0 / 10_000.0))
    assert result["max_drawdown"] == pytest.approx(1.0 - np.exp(-25.0 / 10_000.0))


def test_each_holdout_starts_flat_and_pre_net_cost_is_not_deducted_twice() -> None:
    first_time = pd.DatetimeIndex([pd.Timestamp("2026-04-01T00:00:00Z")])
    second_time = pd.DatetimeIndex([pd.Timestamp("2026-04-01T00:01:00Z")])

    first = _simulate_non_overlapping_positions(
        first_time, np.array([12.3456789]), np.array([1.0]), horizon_minutes=15
    )
    second = _simulate_non_overlapping_positions(
        second_time, np.array([3.25]), np.array([1.0]), horizon_minutes=15
    )

    assert first["actual_entries"] == 1
    assert second["actual_entries"] == 1
    assert first["net_pnl_bps"] == 12.3456789
    assert second["net_pnl_bps"] == 3.25


def test_input_hashing_and_output_creation_are_reproducible_and_non_overwriting(tmp_path) -> None:
    input_path = tmp_path / "input.bin"
    output_path = tmp_path / "artifact.json"
    input_path.write_bytes(b"abc")

    assert _sha256_file(input_path) == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )
    _write_json_exclusive(output_path, {"exact": 1.25})
    original = output_path.read_text()

    with pytest.raises(FileExistsError):
        _write_json_exclusive(output_path, {"exact": 9.0})

    assert output_path.read_text() == original
