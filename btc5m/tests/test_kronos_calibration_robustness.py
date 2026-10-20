from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from scripts.check_kronos_calibration_robustness import (
    _write_doc,
    analyze_calibration_skew,
    evaluate_thresholds,
    normalize_lse_candles,
    select_holdout_checkpoints,
)


def test_analyze_calibration_skew_flags_dominant_regime_and_source():
    rows = []
    base = datetime(2026, 7, 26, tzinfo=timezone.utc)
    for i in range(10):
        rows.append(
            {
                "timestamp": (base + timedelta(hours=i)).isoformat().replace("+00:00", "Z"),
                "source_file": "dominant.parquet" if i < 7 else "other.parquet",
                "realized_regime": "LOW" if i < 8 else "MED",
                "volatility_bucket": ["LOW_SAMPLE", "MED_SAMPLE", "HIGH_SAMPLE"][i % 3],
                "hour_utc": i,
            }
        )

    skew = analyze_calibration_skew({"checkpoints": rows})

    assert skew["checkpoint_count"] == 10
    assert skew["largest_realized_regime_fraction"] == 0.8
    assert skew["largest_source_fraction"] == 0.7
    assert skew["skew_flags"]["realized_regime_dominated"] is True
    assert skew["skew_flags"]["single_source_dominated"] is True
    assert skew["skew_flags"]["single_hour_dominated"] is False


def test_normalize_lse_candles_accepts_ts_and_adds_source():
    df = normalize_lse_candles(
        [
            {
                "ts": "2026-07-19 00:00:00.000000",
                "open": 100,
                "high": 102,
                "low": 99,
                "close": 101,
                "volume": 3,
            }
        ],
        "lse:test",
    )

    assert len(df) == 1
    assert df.iloc[0].timestamp == pd.Timestamp("2026-07-19T00:00:00Z")
    assert df.iloc[0].source_file == "lse:test"


def test_select_holdout_checkpoints_excludes_calibration_dates_and_spans_hours():
    rows = []
    base = datetime(2026, 7, 19, tzinfo=timezone.utc)
    for i in range(20):
        rows.append(
            {
                "idx": 512 + i,
                "timestamp": base + timedelta(hours=i),
                "realized_atr_daily_equiv": 0.001 + i * 0.001,
            }
        )
    rows.append(
        {
            "idx": 999,
            "timestamp": datetime(2026, 7, 26, 0, tzinfo=timezone.utc),
            "realized_atr_daily_equiv": 0.5,
        }
    )

    selected = select_holdout_checkpoints(
        rows,
        count=5,
        excluded_dates={"2026-07-26"},
    )

    assert len(selected) == 5
    assert all(row["timestamp"].date().isoformat() != "2026-07-26" for row in selected)
    assert len({row["timestamp"].hour for row in selected}) == 5
    assert all(row["volatility_bucket"] in {"LOW_SAMPLE", "MED_SAMPLE", "HIGH_SAMPLE"} for row in selected)


def test_select_holdout_checkpoints_can_span_dates():
    rows = []
    for day in (1, 8):
        base = datetime(2026, 7, day, tzinfo=timezone.utc)
        for i in range(10):
            rows.append(
                {
                    "idx": 512 + i,
                    "timestamp": base + timedelta(hours=i),
                    "realized_atr_daily_equiv": 0.001 + i * 0.001 + day * 0.0001,
                }
            )

    selected = select_holdout_checkpoints(rows, count=12, excluded_dates=set())

    assert len(selected) == 12
    assert {row["timestamp"].date().isoformat() for row in selected} == {
        "2026-07-01",
        "2026-07-08",
    }


def test_select_holdout_checkpoints_spreads_across_available_dates():
    rows = []
    for day in (1, 8, 15, 19):
        base = datetime(2026, 7, day, tzinfo=timezone.utc)
        for i in range(12):
            rows.append(
                {
                    "idx": 512 + i,
                    "timestamp": base + timedelta(hours=i),
                    "realized_atr_daily_equiv": 0.001 + i * 0.001 + day * 0.0001,
                }
            )

    selected = select_holdout_checkpoints(rows, count=12, excluded_dates=set())
    counts = {}
    for row in selected:
        date = row["timestamp"].date().isoformat()
        counts[date] = counts.get(date, 0) + 1

    assert counts == {
        "2026-07-01": 3,
        "2026-07-08": 3,
        "2026-07-15": 3,
        "2026-07-19": 3,
    }


def test_select_holdout_checkpoints_includes_target_hours_across_dates():
    rows = []
    for day in (1, 8, 15, 19):
        base = datetime(2026, 7, day, tzinfo=timezone.utc)
        for hour in range(24):
            rows.append(
                {
                    "idx": 512 + len(rows),
                    "timestamp": base + timedelta(hours=hour),
                    "realized_atr_daily_equiv": 0.001 + hour * 0.001 + day * 0.0001,
                }
            )

    selected = select_holdout_checkpoints(rows, count=12, excluded_dates=set())
    target_hours = {0, 4, 16, 20}
    selected_targets = [row for row in selected if row["timestamp"].hour in target_hours]

    assert target_hours <= {row["timestamp"].hour for row in selected}
    assert len({row["timestamp"].date().isoformat() for row in selected_targets}) >= 2
    assert {row["timestamp"].hour for row in selected} == target_hours


def test_write_doc_reports_actual_holdout_checkpoint_counts(tmp_path):
    report = {
        "calibration_skew": {
            "checkpoint_count": 27,
            "time_span_start": "2026-07-26T08:32:00Z",
            "time_span_end": "2026-07-30T12:41:00Z",
            "realized_regime_counts": {"LOW": 22, "MED": 5},
            "volatility_sample_bucket_counts": {"LOW_SAMPLE": 9, "MED_SAMPLE": 9, "HIGH_SAMPLE": 9},
            "source_file_counts": {"btc_ticks_jul26-28.parquet": 17},
            "hour_utc_counts": {"8": 5},
            "skew_flags": {"realized_regime_dominated": True},
        },
        "holdout": {
            "source": "LSE BTC/USD 1m candles",
            "range_start": "2026-07-01T00:00:00+00:00",
            "range_end": "2026-07-19T16:39:00+00:00",
            "candles": 4000,
            "checkpoints": 12,
            "status": "ok",
            "checkpoint_date_counts": {
                "2026-07-01": 3,
                "2026-07-08": 3,
                "2026-07-15": 3,
                "2026-07-19": 3,
            },
            "checkpoint_source_counts": {
                "lse:2026-07-01:2026-07-02": 3,
                "lse:2026-07-08:2026-07-09": 3,
            },
            "hour_utc_counts": {"8": 4},
            "realized_regime_counts": {"LOW": 4, "MED": 7, "HIGH": 1},
            "old_thresholds": {"matches": 6, "total": 12, "match_rate": 0.5},
            "new_thresholds": {"matches": 7, "total": 12, "match_rate": 7 / 12},
        },
    }

    path = tmp_path / "report.md"
    _write_doc(report, path)

    text = path.read_text()
    assert "Selected checkpoint dates" in text
    assert "2026-07-15" in text
    assert "Selected checkpoint sources" in text


def test_evaluate_thresholds_reports_real_counts():
    rows = [
        {"kronos_atr_daily_equiv": 0.01, "realized_regime": "LOW"},
        {"kronos_atr_daily_equiv": 0.025, "realized_regime": "LOW"},
        {"kronos_atr_daily_equiv": 0.055, "realized_regime": "HIGH"},
        {"kronos_atr_daily_equiv": None, "realized_regime": "LOW"},
    ]

    old = evaluate_thresholds(rows, 0.02, 0.05)
    new = evaluate_thresholds(rows, 0.03, 0.05)

    assert old == {
        "low_threshold": 0.02,
        "high_threshold": 0.05,
        "matches": 2,
        "total": 3,
        "match_rate": 2 / 3,
    }
    assert new["matches"] == 3
    assert new["match_rate"] == 1.0
