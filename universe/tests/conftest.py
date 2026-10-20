"""Shared pytest fixtures for the polyuni test suite.

Keeps tests hermetic by sandboxing on-disk sinks (alerts log, live-control state
+ audit) to a per-test tmp_path, the same pattern polymarket_arb's conftest.py
uses -- that pattern exists because early polymarket_arb tests leaked writes to
the real project state/ directory, causing nondeterministic counts across runs.
Carried forward here from day one instead of discovering it the hard way again.
"""
from __future__ import annotations

from dataclasses import replace

import pytest


@pytest.fixture(autouse=True)
def isolate_state(tmp_path, monkeypatch):
    """Redirect alerting.ALERTS_LOG_FILE and the default LiveTradingControl to a
    per-test tmp_path so no test touches the real project state/ directory.
    """
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    import polyuni.alerting as _alerting
    monkeypatch.setattr(_alerting, "ALERTS_LOG_FILE", str(state_dir / "alerts.jsonl"))

    import polyuni.live_control as _live_control
    test_control = _live_control.LiveTradingControl(
        state_dir / "live_control.json",
        state_dir / "live_control_audit.jsonl",
    )
    monkeypatch.setenv("LIVE_CONTROL_STATE_PATH", str(test_control.state_path))
    monkeypatch.setenv("LIVE_CONTROL_AUDIT_PATH", str(test_control.audit_path))
    monkeypatch.setattr(_live_control, "_DEFAULT_CONTROL", test_control)

    # RiskState reads its module-level frozen settings object. Without isolating
    # this path, a real operator stop file left by the dashboard makes unrelated
    # unit tests fail as "halted/emergency-stop" and, worse, encourages deleting a
    # genuine safety signal just to run tests.
    import polyuni.risk as _risk
    production_stop_path = str(_risk.SETTINGS.emergency_stop_file)
    original_emergency_triggered = _risk.RiskState.emergency_triggered
    monkeypatch.setattr(
        _risk,
        "SETTINGS",
        replace(_risk.SETTINGS, emergency_stop_file=str(state_dir / "EMERGENCY_STOP")),
    )

    def isolated_emergency_triggered(self):
        configured = str(_risk.SETTINGS.emergency_stop_file)
        if configured == production_stop_path:
            return (state_dir / "EMERGENCY_STOP").exists()
        # Tests that deliberately configure their own stop path still exercise the
        # production implementation verbatim.
        return original_emergency_triggered(self)

    monkeypatch.setattr(
        _risk.RiskState, "emergency_triggered", isolated_emergency_triggered
    )

    yield state_dir
