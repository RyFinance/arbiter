"""TASK F2 — Unit tests for the Chunk A metrics scripts.

Covers the pure-logic of:
  * scripts/calc_quote_freshness.py  -> calc_freshness, calc_fire_latency, parse_bot_jsonl
  * scripts/calc_pnl_convergence.py  -> compute_pnl_convergence, analyze_by_band
  * scripts/preflight_check.py       -> Erc1155CheckError, CheckResult PASS/FAIL/WARN shaping

All inputs are synthetic fixtures — no network, no live data. These tests lock the
threshold/contract semantics so later sessions can't silently drift the PASS/FAIL
cutoffs (>=95% freshness, +/-10% PnL convergence, real RPC-derived balances).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import List

import pytest

# The scripts/ directory is not a package on sys.path by default; add it so we
# can import the script modules directly.
SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import calc_quote_freshness as cqf  # noqa: E402
import calc_pnl_convergence as cpnc  # noqa: E402
import freshness_pnl_report as fpr  # noqa: E402
from preflight_check import (  # noqa: E402
    Erc1155CheckError,
    CheckResult,
    encode_is_approval_for_all,
)
from polymarket_arb.clients.trading_client import TradingClient  # noqa: E402


def _make_client():
    """Build a TradingClient without touching env/settings/network."""
    tc = TradingClient.__new__(TradingClient)
    return tc


# ---------------------------------------------------------------------------
# calc_quote_freshness.calc_freshness
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("ages,total,target_ms,expected_pct,expected_pass", [
    ([0, 100, 200, 300, 499], 5, 500, 100.0, True),
    ([0, 100, 200, 300, 501], 5, 500, 80.0, False),
    ([], 0, 500, 0.0, False),
    ([10, 20, 30, 40, 50, 60, 70, 80, 90, 501], 10, 500, 90.0, False),  # 9/10 within, 90% < 95% -> FAIL
])
def test_calc_freshness(ages, total, target_ms, expected_pct, expected_pass):
    fills = [{"age_ms": a, "freshness_band": "fresh" if a <= target_ms else "0.5-2s"} for a in ages]
    result = cqf.calc_freshness(fills, target_ms=target_ms)
    assert result["total_fills"] == total
    assert result["pct_within_target"] == expected_pct
    assert result["PASS"] is expected_pass
    assert result["target_ms"] == target_ms
    assert result["target_pct"] == 95.0  # documented target

def test_calc_freshness_p50_p95_sorted_correctly():
    fills = [{"age_ms": a, "freshness_band": "fresh"} for a in [100, 200, 300, 400, 500]]
    r = cqf.calc_freshness(fills, target_ms=500)
    # nearest-rank LOWER: p50 = vals[int(4*0.5)] = vals[2] = 300; p95 = vals[int(4*0.95)] = vals[3] = 400
    assert r["p50_age_ms"] == 300.0
    assert r["p95_age_ms"] == 400.0

def test_calc_freshness_default_target_500ms():
    """The >=95% within-500ms target from the go-live checklist must be the default."""
    r = cqf.calc_freshness([{"age_ms": 600, "freshness_band": "fresh"}])
    assert r["PASS"] is False  # 600ms > 500ms target


# ---------------------------------------------------------------------------
# calc_quote_freshness.calc_fire_latency
# --------------------------------------------------------------------------- #

def test_calc_fire_latency_p95_within_500ms():
    lats = [{"latency_ms": v} for v in [1.0, 2.0, 3.0, 4.0, 1.78, 0.99]]
    r = cqf.calc_fire_latency(lats)
    assert r["total_fires"] == 6
    # nearest-rank LOWER: p50 = vals[int(5*0.5)] = 1.78; p95 = vals[int(5*0.95)] = 3.0
    assert r["p50_ms"] == 1.78
    assert r["p95_ms"] == 3.0
    assert r["within_500ms"] == 6  # all under 500ms


# ---------------------------------------------------------------------------
# calc_quote_freshness.parse_bot_jsonl
# --------------------------------------------------------------------------- #

def test_parse_bot_jsonl_extracts_fills_and_missed(tmp_path):
    log = tmp_path / "bot.jsonl"
    log.write_text(
        json.dumps({"event": "TRADE", "msg": "filled", "t": "10:00",
                    "data": {"age_ms": 100, "combined": 0.98, "pnl_locked": 0.20,
                             "freshness_band": "fresh"}})
        + "\n"
        + json.dumps({"event": "TRADE", "msg": "missed", "t": "10:01",
                      "data": {"age_ms": 600, "combined": 0.97,
                               "reason": "stale_quote", "freshness_band": "0.5-2s"}})
        + "\n"
    )
    d = cqf.parse_bot_jsonl(log)
    assert len(d["fills"]) == 1
    assert d["fills"][0]["age_ms"] == 100
    assert d["fills"][0]["pnl_locked"] == 0.20
    assert len(d["missed"]) == 1
    assert d["missed"][0]["reason"] == "stale_quote"


def test_live_log_without_summary_does_not_use_prior_console_metrics(tmp_path, monkeypatch):
    """In-progress dashboard reporting must not borrow a prior session's PnL."""
    bot_log = tmp_path / "bot.jsonl"
    bot_log.write_text(json.dumps({"event": "TRADE", "msg": "filled", "data": {}}) + "\n")
    (tmp_path / "session_old.log").write_text(
        "session metrics: opps=8 fills=8 missed=221 locked_pnl=1.9 would_lock=373.3\n"
    )
    monkeypatch.setattr(fpr, "BOT_LOG", bot_log)
    monkeypatch.setattr(fpr, "STATE_DIR", tmp_path)

    source, metrics = fpr._find_latest_metrics_line()

    assert source == "bot.jsonl_pending_summary"
    assert metrics is None


def test_dashboard_convergence_gate_uses_committed_baseline(monkeypatch):
    """Raw missed edge is descriptive; it must not become the dashboard gate."""
    monkeypatch.setattr(
        fpr, "_find_latest_metrics_line",
        lambda: ("synthetic", {"locked_pnl": 1.80, "would_lock": 0.70, "opps": 4, "session_pnl": 1.80}),
    )
    result = fpr.compute_pnl_convergence()
    assert result["expected_raw_pnl"] == pytest.approx(2.50)
    assert result["expected_committed_pnl"] == pytest.approx(1.80)
    assert result["convergence_pct"] == pytest.approx(100.0)
    assert result["PASS"] is True
    assert result["sample_available"] is True


def test_dashboard_convergence_zero_denominator_is_unavailable(monkeypatch):
    monkeypatch.setattr(
        fpr, "_find_latest_metrics_line",
        lambda: ("synthetic", {"locked_pnl": 0.0, "would_lock": 0.0, "opps": 0, "session_pnl": 0.0}),
    )

    result = fpr.compute_pnl_convergence()

    assert result["expected_committed_pnl"] == 0.0
    assert result["sample_available"] is False
    assert result["PASS"] is False


def test_dashboard_does_not_compare_noncomparable_structural_backtest(
    tmp_path, monkeypatch
):
    report = tmp_path / "backtest_validation_report.json"
    report.write_text(
        json.dumps(
            {
                "comparable_to_outcome_books": False,
                "backtest": {"session_pnl": 34.4875, "opportunities": 391},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(fpr, "BACKTEST_REPORT", report)
    monkeypatch.setattr(
        fpr, "_find_latest_metrics_line",
        lambda: ("synthetic", {"locked_pnl": 1.0, "would_lock": 0.0, "opps": 1, "session_pnl": 1.0}),
    )

    result = fpr.compute_pnl_convergence()

    assert result["backtest_pnl"] is None
    assert result["backtest_opps"] is None
    assert result["backtest_per_opp"] is None


# ---------------------------------------------------------------------------
# calc_pnl_convergence.compute_pnl_convergence
# --------------------------------------------------------------------------- #

def test_pnl_convergence_within_10pct_is_pass():
    """If real locked_pnl == expected_pnl, deviation is 0 -> PASS."""
    fills = [{"combined": 0.98, "pnl_locked": 0.20}] * 5   # expected = 5*(1-0.98)*10 = 1.00
    missed: List[dict] = []
    r = cpnc.compute_pnl_convergence(fills, missed, order_size=10.0)
    assert abs(r["expected_pnl"] - 1.00) < 0.001
    assert abs(r["real_locked_pnl"] - 1.00) < 0.001
    assert r["PASS"] is True
    assert abs(r["deviation_pct"]) < 10.0

def test_pnl_convergence_far_off_is_fail():
    """A submitted order that realizes far below its expected edge fails."""
    fills = [{"combined": 0.98, "pnl_locked": 0.10}]      # expected submitted edge = 0.20
    missed = [{"combined": 0.98, "would_lock": 0.20}] * 5  # 5 missed
    r = cpnc.compute_pnl_convergence(fills, missed, order_size=10.0)
    assert r["PASS"] is False
    assert r["deviation_pct"] < -10.0  # real << expected

def test_pnl_convergence_empty_no_crash():
    """An empty session must be unavailable, never a vacuous convergence pass."""
    r = cpnc.compute_pnl_convergence([], [], order_size=10.0)
    assert r["expected_pnl"] == 0.0
    assert r["real_locked_pnl"] == 0.0
    assert r["total_opportunities"] == 0
    assert r["sample_available"] is False
    assert r["PASS"] is False


# ---------------------------------------------------------------------------
# preflight_check: Erc1155CheckError + get_fill_ratio + CheckResult
# --------------------------------------------------------------------------- #

def test_erc1155_check_error_is_runtime_error():
    """The CTF revert must surface as a distinct exception, not a generic RPC error."""
    assert issubclass(Erc1155CheckError, RuntimeError)

def test_get_fill_ratio_none_is_zero():
    """A None order id (no liquidity / rejected) must map to 0% fill."""
    tc = _make_client()
    assert tc.get_fill_ratio(None) == 0.0

def test_get_fill_ratio_confirmed_is_one():
    """A non-None order id from _paper_simulate_fill or the live SDK is a 100% fill."""
    tc = _make_client()
    assert tc.get_fill_ratio("0x" + "ab" * 32) == 1.0

def test_check_result_statuses():
    p = CheckResult("x", "PASS", detail="ok", value="v")
    assert p.status == "PASS"
    w = CheckResult("x", "WARN", detail="?", value="?")
    assert w.status == "WARN"

def test_encode_is_approval_for_all_selector():
    """The ERC-1155 isApprovalForAll(address,address) selector must be correct."""
    owner = "0x" + "85" + "6D" + "93"
    data = encode_is_approval_for_all(owner, "0x" + "e1" * 20)
    # selector 0xebd1d359 is the standard ERC-1155 isApprovalForAll
    assert data.startswith("0xebd1d359")
