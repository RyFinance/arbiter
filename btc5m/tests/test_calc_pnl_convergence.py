"""Tests for scripts/calc_pnl_convergence.py.

Validates the PnL convergence calculations that feed the dashboard PnL convergence tile:
  * parse_bot_jsonl — extracts TRADE events with combined + pnl
  * compute_pnl_convergence — expected vs real PnL, deviation, PASS/FAIL
  * compute_pnl_from_summary — fallback using session summary metrics
  * analyze_by_band — per-breadth-band breakdown
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Make scripts/ importable
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from calc_pnl_convergence import (
    parse_bot_jsonl,
    compute_pnl_convergence,
    compute_pnl_from_summary,
    analyze_by_band,
)


# ---------------------------------------------------------------------------
# parse_bot_jsonl
# ---------------------------------------------------------------------------
def test_parse_bot_jsonl_extracts_fills_and_missed():
    """TRADE events are split into fills and missed with combined prices."""
    events = [
        {"t": "00:00:01.000", "event": "TRADE", "msg": "filled",
         "data": {"combined": 0.98, "pnl_locked": 0.20, "age_ms": 100,
                  "freshness_band": "fresh"}},
        {"t": "00:00:02.000", "event": "TRADE", "msg": "missed",
         "data": {"combined": 0.99, "would_lock": 0.10, "reason": "stale_quote",
                  "age_ms": 5000, "freshness_band": "2-10s"}},
        {"t": "00:00:02.500", "event": "TRADE", "msg": "partial_resolved",
         "data": {"market_id": "m2", "action": "exit", "decision_combined": 0.97,
                  "filled_entry_price": 0.14, "exit_price": 0.13, "size": 10.0,
                  "expected_pnl": 0.30, "residual_pnl": -0.10,
                  "age_ms": 90, "freshness_band": "fresh"}},
        {"t": "00:00:03.000", "event": "INFO", "msg": "boot"},  # ignored
    ]
    path = Path("/tmp/_test_conv.jsonl")
    with path.open("w") as fh:
        for ev in events:
            fh.write(json.dumps(ev) + "\n")
    try:
        result = parse_bot_jsonl(path)
    finally:
        path.unlink(missing_ok=True)

    assert len(result["fills"]) == 1
    assert result["fills"][0]["combined"] == 0.98
    assert result["fills"][0]["pnl_locked"] == 0.20
    assert len(result["missed"]) == 1
    assert result["missed"][0]["combined"] == 0.99
    assert len(result["partials"]) == 1
    assert result["partials"][0]["expected_pnl"] == 0.30
    assert result["partials"][0]["realized_pnl"] == -0.10


# ---------------------------------------------------------------------------
# compute_pnl_convergence
# ---------------------------------------------------------------------------
def test_pnl_convergence_pass_within_10_percent():
    """When real PnL is within ±10% of expected, PASS."""
    fills = [{"combined": 0.98, "pnl_locked": 0.20, "age_ms": 100, "freshness_band": "fresh"}]
    missed = [{"combined": 0.99, "would_lock": 0.10, "reason": "stale", "age_ms": 5000,
               "freshness_band": "2-10s"}]
    result = compute_pnl_convergence(fills, missed, order_size=10.0)
    # Raw expected includes the rejected edge, but the execution gate compares
    # the filled order's actual locked PnL to its submitted expected edge.
    assert result["expected_pnl"] == pytest.approx(0.3, abs=0.001)
    assert result["real_locked_pnl"] == pytest.approx(0.2, abs=0.001)
    assert result["expected_committed_pnl"] == pytest.approx(0.2, abs=0.001)
    assert result["deviation_pct"] == pytest.approx(0.0, abs=0.1)
    assert result["PASS"] is True


def test_pnl_convergence_perfect_convergence():
    """When every opportunity fills, real == expected -> 0% deviation -> PASS."""
    fills = [
        {"combined": 0.98, "pnl_locked": 0.20, "age_ms": 100, "freshness_band": "fresh"},
        {"combined": 0.97, "pnl_locked": 0.30, "age_ms": 50, "freshness_band": "fresh"},
    ]
    missed = []  # no misses
    result = compute_pnl_convergence(fills, missed, order_size=10.0)
    # expected = 0.2 + 0.3 = 0.5
    # real = 0.2 + 0.3 = 0.5
    assert result["real_locked_pnl"] == pytest.approx(0.5, abs=0.001)
    assert result["expected_pnl"] == pytest.approx(0.5, abs=0.001)
    assert result["deviation_pct"] == 0.0
    assert result["PASS"] is True


def test_pnl_convergence_no_opportunities():
    """No fills or misses -> zeroed defaults."""
    result = compute_pnl_convergence([], [], order_size=10.0)
    assert result["expected_pnl"] == 0.0
    assert result["real_locked_pnl"] == 0.0
    assert result["convergence_pct"] == 0.0
    # No submitted expected PnL means there is no convergence sample.
    assert result["sample_available"] is False
    assert result["PASS"] is False


def test_pnl_convergence_uses_pnl_locked_not_combined():
    """real_locked_pnl comes from pnl_locked field, not recomputed from combined."""
    fills = [{"combined": 0.98, "pnl_locked": 0.15, "age_ms": 100, "freshness_band": "fresh"}]
    result = compute_pnl_convergence(fills, [], order_size=10.0)
    # real_locked should be 0.15 (the pnl_locked value), NOT (1-0.98)*10 = 0.2
    assert result["real_locked_pnl"] == pytest.approx(0.15, abs=0.001)


def test_raw_decomposition_reconciles_stale_misses_but_gate_uses_committed():
    """Three $0.70 stale misses explain $2.50 expected vs $1.80 realized.

    The misses belong in raw opportunity accounting, not the submitted-order
    convergence gate.  This is deliberately synthetic and paper-only.
    """
    fills = [{"combined": 0.82, "pnl_locked": 1.80}]
    missed = [
        {"combined": 0.98, "would_lock": 0.20, "reason": "stale_quote"},
        {"combined": 0.98, "would_lock": 0.20, "reason": "stale_quote"},
        {"combined": 0.97, "would_lock": 0.30, "reason": "stale_quote"},
    ]

    result = compute_pnl_convergence(fills, missed, order_size=10.0)

    assert result["expected_raw_pnl"] == pytest.approx(2.50)
    assert result["realized_locked"] == pytest.approx(1.80)
    assert result["missed_stale_quote"] == pytest.approx(0.70)
    assert result["missed_other"] == pytest.approx(0.0)
    assert result["expected_raw_pnl"] == pytest.approx(
        result["realized_locked"] + result["missed_stale_quote"] + result["missed_other"]
    )
    assert result["expected_committed_pnl"] == pytest.approx(1.80)
    assert result["PASS"] is True


def test_historical_threshold_surplus_is_not_treated_as_gross_would_lock():
    """The Aug-11 stale records logged surplus above the 1% entry gate.

    Gross forgone binary-arb PnL must be recomputed from combined and size.
    """
    fills = [{"combined": 0.82, "pnl_locked": 1.80}]
    missed = [
        {"combined": 0.96, "would_lock": 0.30, "reason": "stale_quote"},
        {"combined": 0.97, "would_lock": 0.20, "reason": "stale_quote"},
        {"combined": 0.97, "would_lock": 0.20, "reason": "stale_quote"},
    ]

    result = compute_pnl_convergence(fills, missed, order_size=10.0)

    assert result["logged_would_lock_pnl"] == pytest.approx(0.70)
    assert result["missed_stale_quote"] == pytest.approx(1.00)
    assert result["expected_raw_pnl"] == pytest.approx(2.80)
    assert result["expected_committed_pnl"] == pytest.approx(1.80)


def test_fill_breakdown_separates_decision_and_fill_prices():
    fills = [{
        "t": "01:02:03.004", "market_id": "m1", "size": 10.0,
        "decision_combined": 0.98, "combined": 0.97,
        "decision_yes_ask": 0.48, "decision_no_ask": 0.50,
        "fill_yes_price": 0.47, "fill_no_price": 0.50,
        "fill_yes_source": "rest_book", "fill_no_source": "rest_book",
        "expected_pnl": 0.20, "pnl_locked": 0.30,
    }]

    result = compute_pnl_convergence(fills, [], order_size=10.0)

    assert result["expected_committed_pnl"] == pytest.approx(0.20)
    assert result["real_locked_pnl"] == pytest.approx(0.30)
    assert result["fill_breakdown"] == [{
        "t": "01:02:03.004", "market_id": "m1", "size": 10.0,
        "decision_combined": 0.98,
        "decision_yes_ask": 0.48, "decision_no_ask": 0.50,
        "fill_combined": 0.97,
        "fill_yes_price": 0.47, "fill_no_price": 0.50,
        "fill_yes_source": "rest_book", "fill_no_source": "rest_book",
        "expected_pnl": 0.20, "realized_pnl": 0.30, "pnl_delta": 0.10,
    }]


def test_submitted_no_fill_remains_in_committed_gate():
    """An order that was sent but did not fill is execution loss, not a pre-submit miss."""
    result = compute_pnl_convergence(
        [], [{"combined": 0.98, "would_lock": 0.20, "reason": "no_fill_in_window"}],
        order_size=10.0,
    )
    assert result["expected_committed_pnl"] == pytest.approx(0.20)
    assert result["realized_locked"] == 0.0
    assert result["PASS"] is False


def test_resolved_partial_is_in_committed_convergence_and_breakdown():
    partials = [{
        "t": "02:03:36.987", "market_id": "m1", "action": "exit",
        "decision_combined": 0.97, "filled_entry_price": 0.14,
        "filled_entry_source": "rest_book", "exit_price": 0.13,
        "size": 10.0, "expected_pnl": 0.30, "realized_pnl": -0.10,
        "freshness_band": "fresh", "age_ms": 92.8,
    }]

    result = compute_pnl_convergence(
        [], [], order_size=10.0, partials=partials,
    )

    assert result["fills"] == 0
    assert result["partials"] == 1
    assert result["expected_committed_pnl"] == pytest.approx(0.30)
    assert result["realized_committed_pnl"] == pytest.approx(-0.10)
    assert result["convergence_pct"] == pytest.approx(-33.33)
    assert result["deviation_pct"] == pytest.approx(-133.33)
    assert result["PASS"] is False
    assert result["partial_breakdown"][0]["pnl_delta"] == pytest.approx(-0.40)


def test_legacy_partial_without_decision_edge_is_not_falsely_scored():
    result = compute_pnl_convergence(
        [], [], order_size=10.0,
        partials=[{"realized_pnl": -0.10, "freshness_band": "fresh"}],
    )
    assert result["realized_committed_pnl"] == pytest.approx(-0.10)
    assert result["unknown_partial_expected_count"] == 1
    assert result["sample_available"] is False
    assert result["PASS"] is False


# ---------------------------------------------------------------------------
# compute_pnl_from_summary
# ---------------------------------------------------------------------------
def test_pnl_from_summary_basic():
    """Summary-based convergence: locked + would_lock = expected."""
    sm = {"locked_pnl": 55.68, "would_lock": 87.34, "opps": 164,
          "fills": 140, "missed": 202, "sent": 280}
    result = compute_pnl_from_summary(sm)
    # Summary-only reports have no submitted-price detail, so locked PnL is its
    # committed baseline and would-lock remains a separate raw missed bucket.
    assert result["expected_pnl"] == pytest.approx(143.02, abs=0.01)
    assert result["real_locked_pnl"] == pytest.approx(55.68, abs=0.01)
    assert result["expected_committed_pnl"] == pytest.approx(55.68, abs=0.01)
    assert result["deviation_pct"] == pytest.approx(0.0, abs=0.1)
    assert result["PASS"] is True
    assert result["total_opportunities"] == 366


def test_pnl_from_summary_perfect():
    """When would_lock is 0 (no misses), deviation is 0%."""
    sm = {"locked_pnl": 10.0, "would_lock": 0.0, "opps": 10,
          "fills": 10, "missed": 0, "sent": 20}
    result = compute_pnl_from_summary(sm)
    assert result["expected_pnl"] == 10.0
    assert result["real_locked_pnl"] == 10.0
    assert result["deviation_pct"] == 0.0
    assert result["PASS"] is True


# ---------------------------------------------------------------------------
# analyze_by_band
# ---------------------------------------------------------------------------
def test_analyze_by_band():
    """Per-band breakdown returns a result for each non-empty band."""
    fills = [
        {"combined": 0.98, "pnl_locked": 0.20, "age_ms": 100, "freshness_band": "fresh"},
        {"combined": 0.98, "pnl_locked": 0.20, "age_ms": 600, "freshness_band": "0.5-2s"},
        {"combined": 0.98, "pnl_locked": 0.20, "age_ms": 5000, "freshness_band": "2-10s"},
    ]
    missed = [{"combined": 0.99, "would_lock": 0.10, "reason": "stale", "age_ms": 5000,
               "freshness_band": "2-10s"}]
    result = analyze_by_band(fills, missed, order_size=10.0)
    band_names = [r["band"] for r in result]
    assert "fresh" in band_names
    assert "0.5-2s" in band_names
    assert "2-10s" in band_names


def test_analyze_by_band_skips_empty_bands():
    """Bands with no fills or misses are omitted."""
    fills = [{"combined": 0.98, "pnl_locked": 0.20, "age_ms": 100, "freshness_band": "fresh"}]
    result = analyze_by_band(fills, [], order_size=10.0)
    band_names = [r["band"] for r in result]
    assert band_names == ["fresh"]
