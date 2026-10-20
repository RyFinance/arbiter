from scripts.run_kronos_final_lse_investigation import (
    CALIBRATION_CHECKPOINTS,
    FINAL_HOLDOUT_CHECKPOINTS,
    ORIGINAL_CALIBRATION_DATES,
    PRIOR_HOLDOUT_DATES,
    checkpoint_dates,
    evaluate_thresholds,
    _checkpoint_candidate,
    validate_split,
)

import pandas as pd


def test_final_split_is_larger_and_disjoint_from_all_prior_dates():
    calibration_dates = checkpoint_dates(CALIBRATION_CHECKPOINTS)
    holdout_dates = checkpoint_dates(FINAL_HOLDOUT_CHECKPOINTS)

    assert len(CALIBRATION_CHECKPOINTS) == 40
    assert len(FINAL_HOLDOUT_CHECKPOINTS) == 16
    assert not calibration_dates & holdout_dates
    assert not (calibration_dates | holdout_dates) & (ORIGINAL_CALIBRATION_DATES | PRIOR_HOLDOUT_DATES)
    validate_split()


def test_final_calibration_spans_all_utc_four_hour_buckets():
    buckets = {hour // 4 for _day, hour in CALIBRATION_CHECKPOINTS}
    assert buckets == {0, 1, 2, 3, 4, 5}


def test_holdout_evaluation_uses_the_single_supplied_threshold_pair():
    rows = [
        {"kronos_atr_daily_equiv": 0.01, "realized_regime": "LOW"},
        {"kronos_atr_daily_equiv": 0.04, "realized_regime": "MED"},
        {"kronos_atr_daily_equiv": 0.07, "realized_regime": "HIGH"},
        {"kronos_atr_daily_equiv": None, "realized_regime": "LOW"},
    ]
    result = evaluate_thresholds(rows, 0.03, 0.05)

    assert result == {
        "low_threshold": 0.03,
        "high_threshold": 0.05,
        "matches": 3,
        "total": 3,
        "match_rate": 1.0,
    }


def test_exact_checkpoint_selection_does_not_depend_on_15_minute_candidate_grid():
    timestamps = pd.date_range("2026-01-01T00:00:00Z", periods=1_600, freq="min")
    candles = pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": [90_000 + index for index in range(len(timestamps))],
            "high": [90_002 + index for index in range(len(timestamps))],
            "low": [89_998 + index for index in range(len(timestamps))],
            "close": [90_001 + index for index in range(len(timestamps))],
            "volume": 1.0,
            "source_file": "lse:test",
        }
    )

    candidate = _checkpoint_candidate(candles, "2026-01-02", 0)

    assert candidate["idx"] == 1_440
    assert candidate["timestamp"] == pd.Timestamp("2026-01-02T00:00:00Z")
