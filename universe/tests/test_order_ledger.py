"""Phase 0 smoke tests for order_ledger.py (copied verbatim from polymarket_arb)."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from polyuni.order_ledger import OrderLedger


def test_append_pending_then_resolved_clears_from_pending_latest(tmp_path):
    ledger = OrderLedger(tmp_path / "ledger.jsonl")
    ledger.append_pending(
        market_id="m1", yes_id="y1", no_id="n1",
        yes_token_id="yt", no_token_id="nt", reserved=9.0, mode="paper",
    )
    assert len(ledger.pending_latest()) == 1

    entry = ledger.pending_latest()[0]
    ledger.append_resolved(entry, status="resolved", final_committed_amount=9.0)
    assert ledger.pending_latest() == []
    assert len(ledger.records()) == 2


def test_ledger_survives_a_torn_final_line(tmp_path):
    path = tmp_path / "ledger.jsonl"
    ledger = OrderLedger(path)
    ledger.append_pending(
        market_id="m1", yes_id="y1", no_id="n1",
        yes_token_id="yt", no_token_id="nt", reserved=5.0,
    )
    with open(path, "a") as f:
        f.write('{"type": "order", "status": "pend')  # torn, no trailing newline
    records = ledger.records()
    assert len(records) == 1  # torn line dropped, not crashed on


def test_reconcile_restores_deployed_capital_on_probe_error(tmp_path):
    ledger = OrderLedger(tmp_path / "ledger.jsonl")
    ledger.append_pending(
        market_id="m1", yes_id="y1", no_id="n1",
        yes_token_id="yt", no_token_id="nt", reserved=12.0,
    )
    client = AsyncMock()
    client.is_filled.side_effect = RuntimeError("network down")
    risk = MagicMock()

    outcome = asyncio.run(ledger.reconcile(client, risk))

    assert outcome.restored_amount == 12.0
    assert outcome.manual_review == ["m1"]
    risk.restore_deployed.assert_called_once_with("m1", 12.0)


def test_reconcile_marks_resolved_when_neither_leg_filled(tmp_path):
    ledger = OrderLedger(tmp_path / "ledger.jsonl")
    ledger.append_pending(
        market_id="m1", yes_id="y1", no_id="n1",
        yes_token_id="yt", no_token_id="nt", reserved=12.0,
    )
    client = AsyncMock()
    client.is_filled.return_value = False
    risk = MagicMock()

    outcome = asyncio.run(ledger.reconcile(client, risk))

    assert outcome.restored_amount == 0.0
    assert outcome.resolved == ["m1"]
    risk.restore_deployed.assert_not_called()


def test_reconcile_restores_and_halts_on_unresolved_sequential_first_leg(tmp_path):
    ledger = OrderLedger(tmp_path / "ledger.jsonl")
    ledger.append_sequence_event(
        sequence_id="seq-1", market_id="m1", status="first_filled",
        side="YES", token_id="yt", order_id="y1", price=0.48,
        size=10, fee=0.17472,
    )
    risk = MagicMock()

    outcome = asyncio.run(ledger.reconcile(AsyncMock(), risk))

    assert outcome.restored_amount == 4.97472
    assert outcome.manual_review == ["seq-1"]
    assert outcome.sequential_manual_review[0]["cost"] == 4.97472
    risk.restore_deployed.assert_called_once_with("m1", 4.97472)
    risk.halt.assert_called_once()


def test_second_fill_or_exit_resolves_sequential_first_leg_for_restart(tmp_path):
    for terminal_status in ("second_filled", "exited"):
        ledger = OrderLedger(tmp_path / f"{terminal_status}.jsonl")
        common = dict(
            sequence_id="seq-1", market_id="m1", side="YES", token_id="yt",
            order_id="o1", price=0.48, size=10, fee=0.17472,
        )
        ledger.append_sequence_event(status="first_filled", **common)
        ledger.append_sequence_event(status=terminal_status, **common)
        assert ledger.unresolved_sequential_first_legs() == []
