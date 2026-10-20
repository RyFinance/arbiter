from __future__ import annotations

import json

from scripts.analyze_cross_leg_capture_skew import analyze


def test_transient_cross_leg_edge_is_identified(tmp_path):
    path = tmp_path / "depth.jsonl"
    market = "btc-updown-5m-1000"
    rows = [
        {"captured_at": 1000.001, "market_id": market, "token_id": "yes", "asks": [[0.55, 10]]},
        {"captured_at": 1000.002, "market_id": market, "token_id": "no", "asks": [[0.46, 10]]},
        {"captured_at": 1000.100, "market_id": market, "token_id": "no", "asks": [[0.43, 10]]},
        {"captured_at": 1000.400, "market_id": market, "token_id": "yes", "asks": [[0.55, 10]]},
        {"captured_at": 1000.402, "market_id": market, "token_id": "no", "asks": [[0.46, 10]]},
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    report = analyze([path], start_at_epoch=1000.0)

    assert report["apparent_edge_candidates"] == 2
    assert report["transient_candidates_within_horizon"] == 1
    transient = next(row for row in report["examples"] if row["transient_within_horizon"])
    assert transient["combined"] == 0.98
    assert transient["corrected_combined"] == 1.01
    assert transient["next_other_delay_ms"] < 3.0
    assert report["settled_edge_candidates"] == 1
    assert report["pair_barrier_edge_candidates"] == 0


def test_point_in_time_bound_and_age_gate(tmp_path):
    path = tmp_path / "depth.jsonl"
    market = "btc-updown-5m-2000"
    rows = [
        {"captured_at": 2000.0, "market_id": market, "token_id": "yes", "asks": [[0.40, 10]]},
        {"captured_at": 2000.001, "market_id": market, "token_id": "no", "asks": [[0.50, 10]]},
        {"captured_at": 2001.0, "market_id": market, "token_id": "yes", "asks": [[0.40, 10]]},
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    report = analyze([path], start_at_epoch=2000.0, max_quote_age_ms=500.0)

    assert report["rows"] == 3
    assert report["apparent_edge_candidates"] == 1
    assert report["inputs"][0]["point_in_time_byte_bound"] is True


def test_ten_ms_settle_removes_adjacent_frame_false_edge(tmp_path):
    path = tmp_path / "depth.jsonl"
    market = "btc-updown-5m-3000"
    rows = [
        {"captured_at": 3000.000, "market_id": market, "token_id": "yes", "asks": [[0.55, 10]]},
        {"captured_at": 3000.001, "market_id": market, "token_id": "no", "asks": [[0.46, 10]]},
        {"captured_at": 3000.100, "market_id": market, "token_id": "yes", "asks": [[0.58, 10]]},
        {"captured_at": 3000.102, "market_id": market, "token_id": "no", "asks": [[0.43, 10]]},
        {"captured_at": 3000.400, "market_id": market, "token_id": "yes", "asks": [[0.55, 10]]},
        {"captured_at": 3000.406, "market_id": market, "token_id": "no", "asks": [[0.46, 10]]},
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    report = analyze([path], start_at_epoch=3000.0, settle_ms=10.0)

    assert report["apparent_edge_candidates"] == 1
    assert report["transient_candidates_within_horizon"] == 1
    assert report["settled_edge_candidates"] == 0
    assert report["pair_barrier_edge_candidates"] == 0
