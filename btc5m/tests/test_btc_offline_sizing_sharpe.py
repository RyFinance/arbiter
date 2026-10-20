"""Tests for the sizing-policy extension added 2026-08-15.

Covers the probability_proportional policy and the Sharpe metrics added to
``_simulate_non_overlapping_positions``.  Existing policy behavior is pinned by
``tests/test_btc_offline_chronological_evaluation.py`` and must remain
unchanged; these tests only assert the additive surface.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from scripts.evaluate_btc_offline_profitability import (
    RETURN_COLUMN,
    SIZING_POLICY_NAMES,
    _policy_weights,
    _simulate_non_overlapping_positions,
    evaluate_frame,
)
from scripts.diagnose_lightgbm_macro_regimes import FEATURE_COLUMNS


def test_probability_proportional_policy_is_size_equal_to_probability() -> None:
    probabilities = np.array([0.0, 0.19, 0.2, 0.5, 0.9, 1.0])
    weights = _policy_weights(probabilities, f1_threshold=0.2, q90_cutoff=0.9)

    assert "probability_proportional" in SIZING_POLICY_NAMES
    np.testing.assert_array_equal(
        weights["probability_proportional"], probabilities
    )
    assert weights["probability_proportional"].max() <= 1.0
    assert weights["probability_proportional"].min() >= 0.0


def test_probability_proportional_is_invariant_to_frozen_cutoffs() -> None:
    probabilities = np.array([0.1, 0.4, 0.6, 0.95])
    first = _policy_weights(probabilities, f1_threshold=0.2, q90_cutoff=0.7)
    second = _policy_weights(probabilities, f1_threshold=0.9, q90_cutoff=0.99)

    np.testing.assert_array_equal(
        first["probability_proportional"], second["probability_proportional"]
    )
    np.testing.assert_array_equal(
        first["probability_proportional"], probabilities
    )


def test_existing_three_policies_unchanged_by_the_extension() -> None:
    probabilities = np.array([0.0, 0.19, 0.2, 0.5, 0.9, 1.0])
    weights = _policy_weights(probabilities, f1_threshold=0.2, q90_cutoff=0.9)

    np.testing.assert_array_equal(
        weights["binary_f1"], (probabilities >= 0.2).astype(float)
    )
    np.testing.assert_array_equal(
        weights["top_decile"], (probabilities >= 0.9).astype(float)
    )
    np.testing.assert_allclose(
        weights["confidence_weighted"],
        np.clip((probabilities - 0.2) / 0.8, 0.0, 1.0),
        rtol=0,
        atol=0,
    )


def test_sharpe_matches_hand_calculated_values() -> None:
    timestamps = pd.DatetimeIndex(
        pd.to_datetime(
            [
                "2026-04-01T00:00:00Z",
                "2026-04-01T00:05:00Z",
                "2026-04-01T00:15:00Z",
                "2026-04-01T00:31:00Z",
                "2026-04-01T00:47:00Z",
            ]
        )
    )
    returns = np.array([100.0, 50.0, -20.0, 10.0, 30.0])
    weights = np.array([1.0, 1.0, 1.0, 1.0, 1.0])
    result = _simulate_non_overlapping_positions(
        timestamps, returns, weights, horizon_minutes=15
    )

    trade_pnl = np.array([100.0, -20.0, 10.0, 30.0])
    expected_sharpe = float(trade_pnl.mean()) / float(trade_pnl.std(ddof=1))
    periods_per_year = (365.0 * 24.0 * 60.0) / 15.0
    assert result["sharpe_per_trade"] == pytest.approx(expected_sharpe)
    assert result["sharpe_annualized"] == pytest.approx(
        expected_sharpe * np.sqrt(periods_per_year)
    )


def test_sharpe_is_none_with_fewer_than_two_trades_or_zero_std() -> None:
    single = pd.DatetimeIndex([pd.Timestamp("2026-04-01T00:00:00Z")])
    one_trade = _simulate_non_overlapping_positions(
        single, np.array([5.0]), np.array([1.0]), horizon_minutes=15
    )
    assert one_trade["sharpe_per_trade"] is None
    assert one_trade["sharpe_annualized"] is None

    times = pd.DatetimeIndex(
        pd.to_datetime(["2026-04-01T00:00:00Z", "2026-04-01T00:16:00Z"])
    )
    zero_std = _simulate_non_overlapping_positions(
        times, np.array([4.0, 4.0]), np.array([1.0, 1.0]), horizon_minutes=15
    )
    assert zero_std["sharpe_per_trade"] is None
    assert zero_std["sharpe_annualized"] is None


def test_weighted_sharpe_uses_actual_trade_sizes() -> None:
    timestamps = pd.DatetimeIndex(
        pd.to_datetime(
            [
                "2026-04-01T00:00:00Z",
                "2026-04-01T00:05:00Z",
                "2026-04-01T00:15:00Z",
            ]
        )
    )
    returns = np.array([100.0, -100.0, 0.0])
    weights = np.array([1.0, 0.25, 1.0])
    result = _simulate_non_overlapping_positions(
        timestamps, returns, weights, horizon_minutes=15
    )

    trade_pnl = np.array([100.0, 0.0])
    expected = float(trade_pnl.mean()) / float(trade_pnl.std(ddof=1))
    assert result["sharpe_per_trade"] == pytest.approx(expected)
    assert result["sharpe_per_trade"] > 0


def test_trade_pnl_series_is_returned_for_pooling() -> None:
    timestamps = pd.DatetimeIndex(
        pd.to_datetime(
            [
                "2026-04-01T00:00:00Z",
                "2026-04-01T00:05:00Z",
                "2026-04-01T00:15:00Z",
            ]
        )
    )
    result = _simulate_non_overlapping_positions(
        timestamps, np.array([100.0, -100.0, 0.0]), np.array([1.0, 0.25, 1.0]),
        horizon_minutes=15,
    )

    assert result["trade_pnl_series"] == [100.0, 0.0]


def test_pooled_sizing_block_is_present_and_reconciles_with_holdouts() -> None:
    timestamps = pd.date_range("2026-02-01", periods=480, freq="min", tz="UTC")
    labels = np.resize([0, 1], 480)
    frame = pd.DataFrame({"timestamp": timestamps, "profitability_target": labels})
    frame[RETURN_COLUMN] = np.where(labels == 1, 12.0, -6.0)
    for number, column in enumerate(FEATURE_COLUMNS):
        frame[column] = number + np.arange(len(frame)) / 100

    artifact = evaluate_frame(frame, feature_columns=FEATURE_COLUMNS)
    pooled = artifact["pnl"]["pooled"]

    assert set(pooled["models"]) == {"logistic_regression", "gradient_boosted_trees"}
    for model_name, model_pooled in pooled["models"].items():
        assert set(model_pooled) == set(SIZING_POLICY_NAMES)
        for policy_name, entry in model_pooled.items():
            per_holdout = [
                artifact["holdouts"][name]["models"][model_name]["sizing_policies"][policy_name]
                for name in pooled["valid_holdouts"]
            ]
            assert entry["total_trades"] == sum(item["actual_entries"] for item in per_holdout)
            assert entry["total_exposure"] == pytest.approx(
                sum(item["exposure_sum"] for item in per_holdout)
            )
            assert entry["total_net_pnl_bps"] == pytest.approx(
                sum(item["net_pnl_bps"] for item in per_holdout)
            )
            assert entry["sequential_ending_equity"] == pytest.approx(
                np.exp(entry["total_net_pnl_bps"] / 10_000.0)
            )
