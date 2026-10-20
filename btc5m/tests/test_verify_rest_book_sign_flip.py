"""Tests for the independent verification script added 2026-08-15.

Covers pairing orientation, the continuity criterion, the rest_book replay
semantics, and the convergence accounting against hand-built windows.
"""
from __future__ import annotations

import pytest

from scripts.verify_rest_book_sign_flip import (
    Continuity,
    analyze_window,
    convergence_of,
    pair_rows,
    replay_window_rest_book,
)


def _row(captured_at: float, token_id: str, ask: float, bid: float) -> dict:
    return {
        "captured_at": captured_at,
        "token_id": token_id,
        "offset_s": captured_at,
        "asks": [[ask, 10.0]],
        "bids": [[bid, 10.0]],
    }


def test_pair_rows_assign_first_observed_token_as_yes() -> None:
    rows = [
        _row(1.0, "tokenA", ask=0.05, bid=0.04),
        _row(1.1, "tokenB", ask=0.92, bid=0.91),
    ]
    ticks, stats, order = pair_rows(rows)

    assert order == ["tokenA", "tokenB"]
    assert stats["complete_paired_ticks"] == 1
    assert ticks[0]["yes_ask"] == 0.05
    assert ticks[0]["no_ask"] == 0.92


def test_pair_rows_rejects_skew_beyond_limit() -> None:
    rows = [
        _row(1.0, "tokenA", ask=0.5, bid=0.49),
        _row(1.9, "tokenB", ask=0.5, bid=0.49),
    ]
    _, stats, _ = pair_rows(rows)

    assert stats.get("complete_paired_ticks", 0) == 0
    assert stats["skew_rejected"] == 1


def test_continuity_criterion_exact_boundaries() -> None:
    passing = Continuity()
    for offset in (5.0, 65.0, 125.0, 185.0, 245.0, 295.0):
        passing.observe(offset)
    assert passing.complete() is True

    failing_first = Continuity()
    for offset in (5.1, 65.0, 125.0, 185.0, 245.0, 295.0):
        failing_first.observe(offset)
    assert failing_first.complete() is False

    failing_gap = Continuity()
    failing_gap.observe(0.0)
    failing_gap.observe(60.1)
    failing_gap.observe(295.0)
    assert failing_gap.complete() is False


def test_replay_second_leg_moves_away_produces_protective_exit_loss() -> None:
    """The published sign-flip mechanism: YES fills, NO ask moves up, exit loses."""
    rows = [
        _row(10.00, "tokenA", ask=0.05, bid=0.04),
        _row(10.01, "tokenB", ask=0.92, bid=0.91),
        _row(10.03, "tokenA", ask=0.05, bid=0.03),
        _row(10.04, "tokenB", ask=0.95, bid=0.94),
        _row(10.30, "tokenA", ask=0.06, bid=0.03),
    ]
    window = analyze_window(0, "btc-updown-5m-0", rows)
    replay = replay_window_rest_book(
        window["ticks"], window["rows"], "immediate", window["token_order"]
    )

    assert len(replay["outcomes"]) == 1
    outcome = replay["outcomes"][0]
    assert outcome["outcome"] == "partial_protective_exit"
    assert outcome["decision_yes_ask"] == 0.05
    assert outcome["decision_no_ask"] == 0.92
    assert outcome["expected_pnl"] == pytest.approx(0.30)
    # YES fills at its decision ask (next ask still 0.05), NO ask moved to 0.95.
    assert outcome["yes_fill_price"] == pytest.approx(0.05)
    assert outcome["no_fill_price"] is None
    # Protective exit at the next YES bid of 0.03.
    assert outcome["exit_bid"] == pytest.approx(0.03)
    assert outcome["realized_pnl"] == pytest.approx((0.03 - 0.05) * 10.0)


def test_replay_clean_fill_when_both_legs_remain_marketable() -> None:
    rows = [
        _row(10.00, "tokenA", ask=0.33, bid=0.32),
        _row(10.01, "tokenB", ask=0.63, bid=0.62),
        _row(10.02, "tokenA", ask=0.33, bid=0.32),
        _row(10.03, "tokenB", ask=0.63, bid=0.62),
    ]
    window = analyze_window(0, "btc-updown-5m-0", rows)
    replay = replay_window_rest_book(
        window["ticks"], window["rows"], "immediate", window["token_order"]
    )

    assert len(replay["outcomes"]) == 1
    outcome = replay["outcomes"][0]
    assert outcome["outcome"] == "clean"
    assert outcome["realized_pnl"] == pytest.approx((1.0 - 0.33 - 0.63) * 10.0)
    assert outcome["expected_pnl"] == pytest.approx(0.40)


def test_replay_respects_no_entry_zone_and_debounce() -> None:
    rows = [
        _row(296.0, "tokenA", ask=0.33, bid=0.32),
        _row(296.1, "tokenB", ask=0.63, bid=0.62),
    ]
    window = analyze_window(0, "btc-updown-5m-0", rows)
    replay = replay_window_rest_book(
        window["ticks"], window["rows"], "immediate", window["token_order"]
    )

    assert replay["outcomes"] == []


def test_convergence_of_matches_published_accounting() -> None:
    outcomes = [
        {
            "horizon": "immediate",
            "outcome": "partial_protective_exit",
            "t": 1.0,
            "decision_combined": 0.97,
            "expected_pnl": 0.3,
            "realized_pnl": -0.2,
        },
        {
            "horizon": "immediate",
            "outcome": "partial_protective_exit",
            "t": 2.0,
            "decision_combined": 0.98,
            "expected_pnl": 0.2,
            "realized_pnl": -0.4,
        },
    ]
    convergence = convergence_of(outcomes, "immediate")

    assert convergence["expected_committed_pnl"] == pytest.approx(0.5)
    assert convergence["realized_committed_pnl"] == pytest.approx(-0.6)
    assert convergence["convergence_pct"] == pytest.approx(-120.0)
    assert convergence["deviation_pct"] == pytest.approx(-220.0)
    assert convergence["opportunities"] == 2
    assert convergence["one_leg_partials"] == 2
