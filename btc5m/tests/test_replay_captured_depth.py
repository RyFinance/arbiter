from __future__ import annotations

from scripts.replay_captured_depth import TokenContinuity, WindowPairer, market_window_start


def _record(token: str, ts: float, asks=None):
    return {
        "captured_at": ts,
        "market_id": "btc-updown-5m-1000",
        "token_id": token,
        "bids": [[0.39, 20.0]],
        "asks": asks if asks is not None else [[0.40, 10.0], [0.41, 20.0]],
    }


def test_pairer_is_causal_and_preserves_leg_timestamps_and_full_ladders():
    pairer = WindowPairer("btc-updown-5m-1000", 1000, max_skew_s=0.5)

    assert pairer.add(_record("first", 1001.0)) is None
    tick = pairer.add(_record("second", 1001.2, [[0.58, 7.0], [0.59, 30.0]]))

    assert tick is not None
    assert tick["timestamp"] == 1001.2
    assert tick["yes_timestamp"] == 1001.0
    assert tick["no_timestamp"] == 1001.2
    assert tick["pair_skew_ms"] == 200.00000000004547
    assert tick["yes_asks"] == [[0.40, 10.0], [0.41, 20.0]]
    assert tick["no_asks"] == [[0.58, 7.0], [0.59, 30.0]]
    assert tick["yes_bids"] == [[0.39, 20.0]]
    assert tick["no_bids"] == [[0.39, 20.0]]
    assert tick["window_close"] == 1300.0


def test_pairer_rejects_stale_or_missing_ladders_without_fabrication():
    stale = WindowPairer("btc-updown-5m-1000", 1000, max_skew_s=0.5)
    stale.add(_record("first", 1001.0))
    assert stale.add(_record("second", 1001.6)) is None
    assert stale.stats["skew_rejected"] == 1

    missing = WindowPairer("btc-updown-5m-1000", 1000, max_skew_s=0.5)
    missing.add(_record("first", 1001.0, []))
    assert missing.add(_record("second", 1001.1)) is None
    assert missing.stats["missing_or_invalid_ask_ladder"] == 1


def test_continuity_and_market_parser_are_exact():
    continuity = TokenContinuity()
    continuity.observe(1.0)
    continuity.observe(5.5)
    continuity.observe(9.0)

    assert market_window_start("btc-updown-5m-1786787700") == 1786787700
    assert market_window_start("invalid") is None
    assert continuity.as_dict() == {
        "first_offset_s": 1.0,
        "last_offset_s": 9.0,
        "max_gap_s": 4.5,
        "rows": 3,
    }
