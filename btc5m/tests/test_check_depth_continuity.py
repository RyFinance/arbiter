from __future__ import annotations

import json

from scripts.check_depth_continuity import analyze


def _row(market: str, token: str, captured: float) -> str:
    return json.dumps(
        {
            "market_id": market,
            "token_id": token,
            "captured_at": captured,
            "asks": [[0.4, 10]],
            "bids": [[0.3, 10]],
        }
    ) + "\n"


def test_continuity_requires_both_tokens_and_full_closed_window(tmp_path):
    start = 1_700_000_100
    market = f"btc-updown-5m-{start}"
    path = tmp_path / "depth.jsonl"
    path.write_text(
        "".join(
            _row(market, token, start + offset)
            for offset in (1.0, 100.0, 299.0)
            for token in ("yes", "no")
        ),
        encoding="utf-8",
    )
    report = analyze(
        [path],
        start_at_epoch=start,
        observed_at_epoch=start + 301,
    )
    assert report["closed_windows"] == 1
    assert report["observed_complete_windows"] == 0
    # The 199-second hole violates the <=60-second continuity criterion.
    assert report["incomplete_closed_windows"] == 1
    assert len(report["incomplete_windows"]) == 1
    assert all(
        ":max_gap=199.0>60.0" in reason
        for reason in report["incomplete_windows"][0]["continuity_failures"]
    )
    assert report["complete_closed_window_pct"] == 0.0
    assert report["longest_consecutive_complete_windows"] == 0
    assert report["current_consecutive_complete_windows"] == 0


def test_continuity_passes_dense_complete_window(tmp_path):
    start = 1_700_000_100
    market = f"btc-updown-5m-{start}"
    path = tmp_path / "depth.jsonl"
    path.write_text(
        "".join(
            _row(market, token, start + offset)
            for offset in (1.0, 50.0, 100.0, 150.0, 200.0, 250.0, 299.0)
            for token in ("yes", "no")
        ),
        encoding="utf-8",
    )
    report = analyze(
        [path],
        start_at_epoch=start,
        observed_at_epoch=start + 301,
    )
    assert report["observed_complete_windows"] == 1
    assert report["complete_windows"][0]["market_id"] == market
    assert report["complete_windows"][0]["continuity_failures"] == []
    assert report["complete_closed_window_pct"] == 100.0
    assert report["longest_consecutive_complete_windows"] == 1
    assert report["current_consecutive_complete_windows"] == 1
    assert report["current_streak_start_epoch"] == start
    assert report["current_streak_end_epoch"] == start
