"""Crash-recovery regression tests for the append-only order ledger."""
from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from polymarket_arb.execution import Execution
from polymarket_arb.metrics import Metrics
from polymarket_arb.models import MarketState
from polymarket_arb.order_ledger import OrderLedger
from polymarket_arb.risk import RiskState


class FakeClient:
    async def is_filled(self, order_id, token_id, gap_ms=0):
        return order_id == "yes-order"


class CrashAfterAppendLedger(OrderLedger):
    """Models SIGKILL immediately after the durable write boundary."""

    def append_pending(self, **kwargs):
        super().append_pending(**kwargs)
        raise SystemExit("simulated process death after fsync")


class ExecutionClient:
    async def submit_order(self, token_id, price, size, side="BUY"):
        return "yes-order" if token_id == "yes-token" else "no-order"

    async def cancel_order(self, token_id, missing_order_id=None):
        return True

    def get_fill_ratio(self, order_id):
        return 1.0

    async def is_filled(self, order_id, token_id, gap_ms=0):
        return order_id == "yes-order"


def _market() -> MarketState:
    now = datetime.now().timestamp()
    return MarketState(
        market_id="market-1", question="ledger crash", yes_token_id="yes-token",
        no_token_id="no-token", best_yes_ask=0.41, best_no_ask=0.53,
        yes_size=100.0, no_size=100.0, last_update_ts=now, yes_ts=now, no_ts=now,
    )


def test_restart_recovers_pending_written_before_pending_stash(tmp_path):
    """A crash after durable IDs, before in-memory stash, must retain risk."""
    ledger = OrderLedger(tmp_path / "orders.jsonl")
    ledger.append_pending(
        market_id="market-1", yes_id="yes-order", no_id="no-order",
        yes_token_id="yes-token", no_token_id="no-token", reserved=9.40,
    )
    # Simulated process death: no Execution._pending entry was ever created.
    recovered = asyncio.run(ledger.reconcile(FakeClient(), RiskState(balance=100)))

    assert recovered.restored_amount == 9.40
    assert recovered.manual_review == []
    assert recovered.resolved == ["market-1"]
    assert any(r["status"] == "resolved" for r in ledger.records())


def test_execution_crash_after_fsynced_ledger_write_reconciles_on_restart(tmp_path):
    """Exercise the exact execution boundary, rather than seeding the ledger."""
    path = tmp_path / "orders.jsonl"
    st = _market()
    risk_before_crash = RiskState(balance=1_000.0)
    execution = Execution(ExecutionClient(), risk_before_crash, Metrics(),
                          ledger=CrashAfterAppendLedger(path))
    execution.register(st, size=10.0)

    with pytest.raises(SystemExit, match="simulated process death"):
        asyncio.run(execution.execute_dual_buy(
            st, combined=0.94, decision_ts=0.0, yes_p=0.41, no_p=0.53,
        ))

    # The process died before either the risk commit or volatile _pending stash.
    assert execution._pending == {}
    assert risk_before_crash.total_deployed == 0.0
    pending = OrderLedger(path).records()
    assert len(pending) == 1
    assert pending[0] == {
        "type": "order", "status": "pending", "market_id": "market-1",
        "yes_id": "yes-order", "no_id": "no-order",
        "yes_token_id": "yes-token", "no_token_id": "no-token",
        "reserved": 9.4, "timestamp": pending[0]["timestamp"],
        "mode": "live",
    }
    assert isinstance(pending[0]["timestamp"], float)

    # New process: actual client state says YES filled, so full reservation is held
    # conservatively and a terminal resolution is durably appended.
    restarted_risk = RiskState(balance=1_000.0)
    recovery = asyncio.run(OrderLedger(path).reconcile(ExecutionClient(), restarted_risk))
    assert recovery.restored_amount == 9.4
    assert restarted_risk.total_deployed == 9.4
    records = OrderLedger(path).records()
    assert records[-1]["type"] == "resolution"
    assert records[-1]["status"] == "resolved"
    assert records[-1]["final_committed_amount"] == 9.4
