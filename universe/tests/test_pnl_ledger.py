"""Tests for PnlLedger's per-market tracking and periodic snapshot/publish
(Phase 3 -- fixes polymarket_arb's "PnL only ever logged once, at shutdown" gap
and its lack of a per-market breakdown)."""
from __future__ import annotations

import json

from polyuni.execution.pnl_ledger import PnlLedger
from polyuni.metrics import Metrics
from polyuni.models import MarketState
from polyuni.risk import RiskState


def _state(market_id="m1") -> MarketState:
    return MarketState(
        market_id=market_id, question="Q", yes_token_id="y", no_token_id="n",
        best_yes_ask=0.4, best_no_ask=0.5,
    )


def test_clean_fill_bumps_per_market_locked_pnl():
    metrics, risk = Metrics(), RiskState()
    ledger = PnlLedger(metrics, risk)
    st = _state("m1")

    ledger.record_clean_fill(
        st, mode="paper", snapshot_combined=0.94, fill_yes_p=0.41, fill_no_p=0.53,
        fill_yes_source="rest_book", fill_no_source="rest_book",
        effective_size=10.0, yes_id="y1", no_id="n1", yes_p=0.41, no_p=0.53,
        age_ms=10.0, band="fresh", stage_timings=None, opp_to_order_ms=1.0, ack_ms=1.0,
    )

    snap = ledger.snapshot()
    assert snap["locked_pnl"] == snap["per_market"]["m1"]["locked_pnl"]
    assert snap["per_market"]["m1"]["locked_pnl"] > 0


def test_two_markets_accumulate_independently():
    metrics, risk = Metrics(), RiskState()
    ledger = PnlLedger(metrics, risk)

    for market_id, combined in (("m1", 0.94), ("m2", 0.90)):
        ledger.record_clean_fill(
            _state(market_id), mode="paper", snapshot_combined=combined,
            fill_yes_p=combined / 2, fill_no_p=combined / 2,
            fill_yes_source="rest_book", fill_no_source="rest_book",
            effective_size=10.0, yes_id="y", no_id="n",
            yes_p=combined / 2, no_p=combined / 2, age_ms=10.0, band="fresh",
            stage_timings=None, opp_to_order_ms=1.0, ack_ms=1.0,
        )

    snap = ledger.snapshot()
    assert set(snap["per_market"]) == {"m1", "m2"}
    # m2's wider discount (combined=0.90) locks strictly more pnl per unit than m1's
    assert snap["per_market"]["m2"]["locked_pnl"] > snap["per_market"]["m1"]["locked_pnl"]
    assert snap["locked_pnl"] == round(
        snap["per_market"]["m1"]["locked_pnl"] + snap["per_market"]["m2"]["locked_pnl"], 4
    )


def test_residual_from_exit_bumps_per_market_residual_not_locked():
    metrics, risk = Metrics(), RiskState()
    ledger = PnlLedger(metrics, risk)
    st = _state("m1")

    ledger.after_residual(
        st, mode="paper", combined=0.94, size=10.0, residual=-2.5,
        age_ms=10.0, band="fresh", action="exit", released_cost=4.1,
    )

    snap = ledger.snapshot()
    assert snap["per_market"]["m1"]["locked_pnl"] == 0.0
    assert snap["per_market"]["m1"]["residual_pnl"] == -2.5
    assert snap["per_market"]["m1"]["session_pnl"] == -2.5
    assert snap["residual_pnl"] == -2.5


def test_snapshot_includes_deployed_and_pending_capital_per_market():
    metrics, risk = Metrics(), RiskState()
    risk.deployed_per_market["m1"] = 12.3
    risk.pending_per_market["m2"] = 7.0
    ledger = PnlLedger(metrics, risk)

    snap = ledger.snapshot()

    assert snap["per_market"]["m1"]["deployed_usd"] == 12.3
    assert snap["per_market"]["m2"]["pending_usd"] == 7.0
    # A market with only deployed/pending capital and no resolved trade yet
    # still appears with zeroed pnl fields, not omitted.
    assert snap["per_market"]["m1"]["locked_pnl"] == 0.0


def test_publish_writes_atomically_and_is_valid_json(tmp_path):
    metrics, risk = Metrics(), RiskState()
    ledger = PnlLedger(metrics, risk)
    st = _state("m1")
    ledger.record_clean_fill(
        st, mode="paper", snapshot_combined=0.94, fill_yes_p=0.41, fill_no_p=0.53,
        fill_yes_source="rest_book", fill_no_source="rest_book",
        effective_size=10.0, yes_id="y1", no_id="n1", yes_p=0.41, no_p=0.53,
        age_ms=10.0, band="fresh", stage_timings=None, opp_to_order_ms=1.0, ack_ms=1.0,
    )

    out_path = tmp_path / "state" / "pnl_snapshot.json"
    ledger.publish(str(out_path))

    assert out_path.exists()
    assert not out_path.with_suffix(".json.tmp").exists()  # temp file cleaned up (renamed, not left behind)
    loaded = json.loads(out_path.read_text())
    assert loaded["per_market"]["m1"]["locked_pnl"] > 0
    assert "generated_at" in loaded


def test_publish_overwrites_on_second_call(tmp_path):
    metrics, risk = Metrics(), RiskState()
    ledger = PnlLedger(metrics, risk)
    out_path = tmp_path / "pnl_snapshot.json"

    ledger.publish(str(out_path))
    first = json.loads(out_path.read_text())

    st = _state("m1")
    ledger.record_clean_fill(
        st, mode="paper", snapshot_combined=0.94, fill_yes_p=0.41, fill_no_p=0.53,
        fill_yes_source="rest_book", fill_no_source="rest_book",
        effective_size=10.0, yes_id="y1", no_id="n1", yes_p=0.41, no_p=0.53,
        age_ms=10.0, band="fresh", stage_timings=None, opp_to_order_ms=1.0, ack_ms=1.0,
    )
    ledger.publish(str(out_path))
    second = json.loads(out_path.read_text())

    assert first["locked_pnl"] == 0.0
    assert second["locked_pnl"] > 0.0
