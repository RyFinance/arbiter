from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from scripts.calibrate_kronos_thresholds import (
    OLD_HIGH_THRESHOLD,
    OLD_LOW_THRESHOLD,
    build_candidate_checkpoints,
    classify_with_thresholds,
    match_rate,
    select_checkpoints,
    summarize,
    tune_thresholds,
)


def test_classify_with_thresholds():
    assert classify_with_thresholds(0.06, 0.02, 0.05) == "HIGH"
    assert classify_with_thresholds(0.03, 0.02, 0.05) == "MED"
    assert classify_with_thresholds(0.01, 0.02, 0.05) == "LOW"


def test_calibration_freezes_pre_update_baseline_thresholds():
    assert OLD_LOW_THRESHOLD == 0.02
    assert OLD_HIGH_THRESHOLD == 0.05


def test_summarize_recomputes_old_regime_from_frozen_thresholds():
    rows = [
        {
            "timestamp": "2026-07-26T00:00:00Z",
            "hour_utc": 0,
            "source_file": "real.parquet",
            "volatility_bucket": "LOW_SAMPLE",
            "realized_atr_daily_equiv": 0.01,
            "realized_regime": "LOW",
            "kronos_atr_daily_equiv": 0.025,
            "kronos_regime_old": "LOW",
        }
    ]

    report = summarize(rows, {"low_threshold": 0.03, "high_threshold": 0.05})

    assert report["checkpoints"][0]["kronos_regime_old"] == "MED"
    assert report["checkpoints"][0]["old_match"] is False


def test_tune_thresholds_improves_match_rate():
    rows = [
        {"kronos_atr_daily_equiv": 0.011, "realized_regime": "LOW"},
        {"kronos_atr_daily_equiv": 0.016, "realized_regime": "LOW"},
        {"kronos_atr_daily_equiv": 0.021, "realized_regime": "MED"},
        {"kronos_atr_daily_equiv": 0.032, "realized_regime": "MED"},
        {"kronos_atr_daily_equiv": 0.041, "realized_regime": "HIGH"},
        {"kronos_atr_daily_equiv": 0.052, "realized_regime": "HIGH"},
    ]

    old_rate = match_rate(rows, 0.02, 0.05)
    tuned = tune_thresholds(rows)

    assert tuned["low_threshold"] < tuned["high_threshold"]
    assert tuned["match_rate"] > old_rate
    assert tuned["match_rate"] == 1.0


def test_select_checkpoints_spans_realized_volatility_buckets():
    base = datetime(2026, 7, 26, tzinfo=timezone.utc)
    candidates = []
    for i in range(90):
        candidates.append(
            {
                "idx": 512 + i,
                "timestamp": base + timedelta(minutes=15 * i),
                "hour_utc": (base + timedelta(minutes=15 * i)).hour,
                "source_file": "real.parquet",
                "realized_atr_daily_equiv": 0.005 + i * 0.0008,
                "realized_regime": "HIGH" if i > 70 else "MED" if i > 25 else "LOW",
            }
        )

    selected = select_checkpoints(candidates, minimum=27)
    buckets = {row["volatility_bucket"] for row in selected}

    assert len(selected) == 27
    assert buckets == {"HIGH_SAMPLE", "MED_SAMPLE", "LOW_SAMPLE"}


def test_build_candidate_checkpoints_uses_contiguous_real_candles():
    rows = []
    start = pd.Timestamp("2026-07-26T00:00:00Z")
    price = 64000.0
    for i in range(700):
        ts = start + pd.Timedelta(minutes=i)
        price += (i % 7 - 3) * 0.5
        rows.append(
            {
                "timestamp": ts,
                "open": price,
                "high": price + 2,
                "low": price - 2,
                "close": price + 0.5,
                "volume": 1.0,
                "source_file": "ticks.parquet",
            }
        )
    candles = pd.DataFrame(rows)

    candidates = build_candidate_checkpoints(candles, context_minutes=512, realized_minutes=60)

    assert candidates
    assert all(row["realized_atr_daily_equiv"] > 0 for row in candidates)
