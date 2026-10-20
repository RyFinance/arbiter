"""Unit tests for the paper-only bot controller (no processes spawned)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from dashboard_js import botctl


@pytest.fixture(autouse=True)
def isolate_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(botctl, "RISK_PATH", tmp_path / "bot_control_risk.json")
    monkeypatch.setattr(botctl, "SESSION_LOG_PATH", tmp_path / "bot_control_session.log")
    monkeypatch.setattr(botctl, "EMERGENCY_STOP_PATH", tmp_path / "EMERGENCY_STOP")
    monkeypatch.setattr(botctl, "_STATE", {"proc": None, "started_at": None, "duration": None, "fill_source": None})
    yield


def test_default_risk_when_file_missing():
    assert botctl.load_risk() == botctl.DEFAULT_RISK


def test_save_and_load_risk_roundtrip():
    saved, error = botctl.save_risk({"order_size": 25.0, "min_edge": 0.02})
    assert error is None
    assert saved["order_size"] == 25.0
    assert saved["min_edge"] == 0.02
    assert saved["max_position_per_market"] == 200.0  # default preserved
    assert botctl.load_risk()["order_size"] == 25.0


def test_save_risk_rejects_out_of_range():
    _, error = botctl.save_risk({"order_size": 5000.0})
    assert error and "outside allowed range" in error
    _, error = botctl.save_risk({"min_edge": 0.5})
    assert error and "outside allowed range" in error
    _, error = botctl.save_risk({"order_size": "abc"})
    assert error and "must be numeric" in error


def test_emergency_stop_write_and_clear():
    assert not botctl.emergency_stop_exists()
    botctl.write_emergency_stop("test")
    assert botctl.emergency_stop_exists()
    text = botctl.EMERGENCY_STOP_PATH.read_text()
    assert "test" in text
    botctl.clear_emergency_stop()
    assert not botctl.emergency_stop_exists()


def test_stop_with_no_managed_bot_is_honest():
    result = botctl.stop_bot()
    assert result["stopped"] is False
    assert result["emergency_stop_written"] is True


def test_status_reports_paper_only_invariants():
    status = botctl.status()
    assert status["paper_only"] is True
    assert status["real_orders_possible"] is False
    assert status["managed_running"] is False
    assert "risk" in status
    assert status["risk"]["order_size"] == 10.0


def test_log_tail_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(botctl, "PROJECT_ROOT", tmp_path)
    result = botctl.log_tail()
    assert result["lines"] == [] or all(isinstance(line, str) for line in result["lines"])
    assert "path" in result
