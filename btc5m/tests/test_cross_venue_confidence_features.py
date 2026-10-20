from __future__ import annotations

from models.cross_venue_confidence_features import (
    extract_feature_row,
    training_readiness,
)


def sample_record(match_label=None):
    record = {
        "observed_at": "2026-08-03T07:15:00+00:00",
        "status": "ok",
        "oracle_mismatch_risk": True,
        "best_gap": 0.12,
        "poly_up_proxy": 0.55,
        "poly_down_proxy": 0.45,
        "kalshi_yes_ask": 0.40,
        "kalshi_no_ask": 0.61,
        "kalshi": {
            "ticker": "KXBTC15M-TEST",
            "status": "active",
            "open_time": "2026-08-03T07:15:00+00:00",
            "close_time": "2026-08-03T07:30:00+00:00",
            "yes_ask": 0.40,
            "yes_bid": 0.39,
            "no_ask": 0.61,
            "no_bid": 0.60,
            "settlement_sources": [{"name": "CF Benchmarks"}],
        },
        "polymarket_windows": [
            {
                "start_ts": 1785741300 + i * 300,
                "end_ts": 1785741600 + i * 300,
                "resolution_source": "https://data.chain.link/streams/btc-usd",
                "accepting_orders": True,
                "active": True,
                "closed": False,
                "up_ask": 0.55,
                "up_bid": 0.54,
                "down_ask": 0.45,
                "down_bid": 0.44,
            }
            for i in range(3)
        ],
    }
    if match_label is not None:
        record["match_label"] = match_label
    return record


def test_extract_feature_row_from_real_log_shape():
    row = extract_feature_row(sample_record())

    assert row.kalshi_ticker == "KXBTC15M-TEST"
    assert row.label is None
    assert row.features["window_overlap_pct"] == 1.0
    assert row.features["full_mapping"] == 1.0
    assert row.features["oracle_mismatch_risk"] == 1.0
    assert row.features["settlement_source_match"] == 0.0
    assert row.features["both_open"] == 1.0


def test_training_readiness_refuses_unlabeled_data():
    rows = [extract_feature_row(sample_record()) for _ in range(3)]
    readiness = training_readiness(rows)

    assert readiness["can_train"] is False
    assert readiness["labeled_records"] == 0
    assert readiness["additional_windows_for_minimum"] == 30


def test_training_readiness_requires_two_label_classes():
    rows = [
        extract_feature_row(sample_record(match_label=1.0))
        for _ in range(35)
    ]
    readiness = training_readiness(rows)

    assert readiness["can_train"] is False
    assert readiness["label_values"] == [1.0]
