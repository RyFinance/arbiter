from __future__ import annotations

from datetime import date, timezone

import numpy as np
import pandas as pd

from scripts.diagnose_lightgbm_macro_regimes import (
    CLASS_NAMES,
    FEATURE_COLUMNS,
    InsufficientHistoryError,
    build_feature_frame,
    label_hypothetical_profitability,
    label_from_training_quantiles,
    normalize_lse_candles,
    run_diagnostic,
    split_time_disjoint,
)


UTC = timezone.utc


def _candles(rows: int = 720) -> pd.DataFrame:
    timestamps = pd.date_range("2026-08-01", periods=rows, freq="min", tz=UTC)
    moves = np.sin(np.arange(rows) / 5) * 0.001 + np.cos(np.arange(rows) / 17) * 0.0004
    close = 100_000 * np.exp(np.cumsum(moves))
    return pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": np.roll(close, 1),
            "high": close * 1.001,
            "low": close * 0.999,
            "close": close,
            "volume": 2 + np.abs(np.sin(np.arange(rows))),
        }
    )


def test_normalize_lse_candles_keeps_observed_rows_without_padding() -> None:
    normalized = normalize_lse_candles(
        [
            {"ts": "2026-08-01 00:00:00", "open": 1, "high": 2, "low": 1, "close": 2, "volume": 3},
            {"ts": "2026-08-01 00:02:00", "open": 2, "high": 3, "low": 2, "close": 3, "volume": 4},
        ]
    )
    assert len(normalized) == 2
    assert normalized["timestamp"].diff().iloc[-1] == pd.Timedelta(minutes=2)


def test_features_include_requested_inputs_and_only_next_high_macro_time() -> None:
    candles = _candles()
    high_event = [pd.Timestamp("2026-08-01 10:00:00", tz=UTC)]
    feature_frame = build_feature_frame(candles, high_event, horizon_minutes=15)

    assert set(FEATURE_COLUMNS).issubset(feature_frame.columns)
    first = feature_frame.iloc[0]
    assert first["minutes_to_next_high_macro"] > 0
    assert feature_frame["session_bucket"].between(0, 3).all()
    assert feature_frame["forward_realized_vol_bps"].notna().all()


def test_profitability_label_is_exactly_t_plus_one_through_t_plus_k() -> None:
    candles = _candles(rows=40)
    labelled = label_hypothetical_profitability(candles, horizon_minutes=3, round_trip_cost_bps=20.0)
    expected = (np.log(candles["close"].shift(-3) / candles["close"]) * 10_000 - 20.0).iloc[:-3].to_numpy()
    np.testing.assert_allclose(labelled["forward_net_return_bps"].to_numpy(), expected)
    assert labelled["future_window_end"].sub(labelled["feature_cutoff_t"]).eq(pd.Timedelta(minutes=3)).all()


def test_future_mutation_cannot_change_features_at_t() -> None:
    candles = _candles(rows=80)
    baseline = build_feature_frame(candles, [], horizon_minutes=5)
    changed = candles.copy()
    changed.loc[changed.index >= 40, ["open", "high", "low", "close", "volume"]] *= 2
    mutated = build_feature_frame(changed, [], horizon_minutes=5)
    feature_columns = [column for column in FEATURE_COLUMNS if column != "minutes_to_next_high_macro"]
    pd.testing.assert_frame_equal(baseline.loc[:30, feature_columns], mutated.loc[:30, feature_columns])


def test_five_partitions_are_strictly_time_disjoint_and_train_defines_labels() -> None:
    feature_frame = build_feature_frame(_candles(rows=1000), [pd.Timestamp("2026-08-02", tz=UTC)], 15)
    parts, partitions = split_time_disjoint(feature_frame)
    labelled, cutoffs = label_from_training_quantiles(parts)

    assert [partition.name for partition in partitions] == ["train", "validation", "holdout_a", "holdout_b", "holdout_c"]
    assert partitions[0].end < partitions[1].start < partitions[2].start < partitions[3].start < partitions[4].start
    assert cutoffs["low_to_med_bps"] < cutoffs["med_to_high_bps"]
    assert set(labelled["train"]["target"].unique()) == {0, 1, 2}
    assert CLASS_NAMES == ["LOW", "MED", "HIGH"]


def test_insufficient_history_returns_an_exact_non_training_artifact() -> None:
    artifact = run_diagnostic(
        candles=_candles(rows=90),
        high_events=[],
        macro_metadata={"event_count": 0},
        requested_start=date(2026, 8, 1),
        requested_end=date(2026, 8, 2),
    )

    assert artifact["status"] == "insufficient_history"
    assert artifact["source"]["synthetic_rows_added"] == 0
    assert "need at least" in artifact["insufficient_history_reason"]


def test_diagnostic_trains_with_three_disjoint_holdouts_and_diagnostic_baselines() -> None:
    artifact = run_diagnostic(
        candles=_candles(rows=900),
        high_events=[pd.Timestamp("2026-08-02 00:00:00", tz=UTC)],
        macro_metadata={"event_count": 1, "high_event_count": 1},
        requested_start=date(2026, 8, 1),
        requested_end=date(2026, 8, 2),
    )

    assert artifact["status"] == "completed"
    assert set(artifact["metrics"]) == {"train", "validation", "holdout_a", "holdout_b", "holdout_c"}
    assert len(artifact["top_10_feature_importances_gain"]) == min(10, len(FEATURE_COLUMNS))
    assert len(artifact["permutation_importance_validation_accuracy"]["features"]) == len(FEATURE_COLUMNS)
    assert artifact["majority_baseline"]["fit_on"] == "train_only"
    assert "vs_kronos_43_75_pp" in artifact["metrics"]["holdout_a"]
    assert "vs_kronos_43_75_pp" in artifact["metrics"]["holdout_b"]
    assert "vs_kronos_43_75_pp" in artifact["metrics"]["holdout_c"]
    assert artifact["stability_vs_kronos"]["holdout_names"] == ["holdout_a", "holdout_b", "holdout_c"]
