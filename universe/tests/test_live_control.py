"""live_control tests: control-only (ported verbatim from polymarket_arb, since
live_control.py itself is a verbatim port) plus TradingClient-integration tests
now that polyuni has its own trading_client.py (Phase 2)."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from polyuni.clients.trading_client import TradingClient
from polyuni.live_control import LiveTradingControl


def _live_client(control: LiveTradingControl) -> TradingClient:
    client = TradingClient.__new__(TradingClient)
    client.s = SimpleNamespace(live=True)
    client._enforce_live_control = True
    client._paper_trade = False
    client._live_control = control
    client._clob_client = MagicMock()
    client._order_modes = {}
    return client


def _control(tmp_path):
    return LiveTradingControl(
        tmp_path / "live_control.json",
        tmp_path / "live_control_audit.jsonl",
    )


def test_missing_and_corrupt_state_fail_closed(tmp_path):
    control = _control(tmp_path)
    assert control.read_state().operator_enabled is False
    decision = control.check_submission(mode="live", call_site="test")
    assert decision.allowed is False
    assert decision.reason == "master_live_trading_flag_disabled"

    control.state_path.write_text("{broken", encoding="utf-8")
    assert control.read_state().operator_enabled is False
    assert control.check_submission(mode="live", call_site="test").allowed is False


def test_operator_state_changes_are_atomic_and_audited(tmp_path):
    control = _control(tmp_path)
    enabled = control.set_operator_enabled(True, actor="pytest", reason="panel test")
    assert enabled.operator_enabled is True
    assert enabled.effective_live_submission_enabled is False
    assert enabled.hard_block_reason == "live_exchange_order_endpoint_unbuilt"
    assert enabled.revision == 1

    unchanged = control.set_operator_enabled(True, actor="pytest", reason="repeat")
    assert unchanged.revision == 1
    disabled = control.set_operator_enabled(False, actor="pytest", reason="close test")
    assert disabled.operator_enabled is False
    assert disabled.revision == 2

    audit = control.read_audit()
    changes = [row for row in audit if row["kind"] == "live_control_state_change"]
    assert [row["changed"] for row in changes] == [True, False, True]
    assert [row["actor"] for row in changes] == ["pytest", "pytest", "pytest"]


def test_operator_enabled_cannot_reach_unbuilt_exchange_endpoint(tmp_path):
    """Even with the operator flag armed, live submission stays blocked -- there
    is no exchange order-submission code path in polyuni (control-plane only)."""
    control = _control(tmp_path)
    control.set_operator_enabled(True, actor="pytest", reason="prove hard block")
    decision = control.check_submission(mode="live", call_site="test")
    assert decision.allowed is False
    assert decision.reason == "live_exchange_order_endpoint_unbuilt"


def test_audit_lines_are_independently_parseable_json(tmp_path):
    control = _control(tmp_path)
    control.set_operator_enabled(False, actor="pytest", reason="initial state")
    control.check_submission(mode="paper", call_site="execution.entry")
    lines = control.audit_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert all(isinstance(json.loads(line), dict) for line in lines)


def test_live_submit_is_blocked_when_master_flag_off_and_sdk_is_never_called(tmp_path):
    control = _control(tmp_path)
    client = _live_client(control)

    result = asyncio.run(client.submit_order("token-12345678", 0.40, 10.0))

    assert result is None
    client._clob_client.create_and_post_order.assert_not_called()
    checks = [row for row in control.read_audit() if row["kind"].endswith("submission_check")]
    assert len(checks) == 1
    assert checks[0]["allowed"] is False
    assert checks[0]["reason"] == "master_live_trading_flag_disabled"


def test_operator_enabled_still_cannot_reach_unbuilt_exchange_endpoint_via_client(tmp_path):
    control = _control(tmp_path)
    control.set_operator_enabled(True, actor="pytest", reason="prove hard block")
    client = _live_client(control)

    result = asyncio.run(client.submit_order("token-12345678", 0.40, 10.0))

    assert result is None
    client._clob_client.create_and_post_order.assert_not_called()
    checks = [row for row in control.read_audit() if row["kind"].endswith("submission_check")]
    assert checks[-1]["reason"] == "live_exchange_order_endpoint_unbuilt"


def test_paper_checks_are_logged_but_simulation_remains_available(tmp_path):
    control = _control(tmp_path)
    client = TradingClient.__new__(TradingClient)
    client.s = SimpleNamespace(live=True)
    client._paper_trade = True
    client._paper_fill_source = "decision_snapshot"
    client._paper_fill_details = {}
    client._order_modes = {}
    client._live_control = control
    client._paper_simulate_fill = MagicMock(return_value="paper-order")

    result = asyncio.run(client.submit_order("paper-12345678", 0.40, 10.0))

    assert result == "paper-order"
    checks = [row for row in control.read_audit() if row["kind"].endswith("submission_check")]
    assert checks[-1]["mode"] == "paper"
    assert checks[-1]["allowed"] is True
    assert checks[-1]["reason"] == "simulation_mode_no_exchange_submission"
