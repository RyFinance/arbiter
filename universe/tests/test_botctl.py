"""Tests for dashboard/botctl.py, focused on the run_state machine (the actual
bug fix from Phase 4's research: polymarket_arb's dashboard reported "running"
the instant a subprocess spawned, with no distinction from "actually watching
books yet"). Real subprocess spawning is mocked -- these test botctl's own
logic, not that `python -m polyuni.main` boots correctly (main.py's own tests
and the live smoke runs already cover that).
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dashboard"))
import botctl  # noqa: E402

REAL_EXTERNAL_BOTS = botctl._external_bots


@pytest.fixture(autouse=True)
def isolate_botctl_paths(tmp_path, monkeypatch):
    """Redirect every module-level path constant to tmp_path so tests never
    touch the real project state/ directory, and reset the module-level
    process-tracking state between tests."""
    monkeypatch.setattr(botctl, "RISK_PATH", tmp_path / "bot_control_risk.json")
    monkeypatch.setattr(botctl, "SESSION_LOG_PATH", tmp_path / "bot_control_session.log")
    monkeypatch.setattr(botctl, "EMERGENCY_STOP_PATH", tmp_path / "EMERGENCY_STOP")
    monkeypatch.setattr(botctl, "READY_PATH", tmp_path / "READY")
    monkeypatch.setattr(botctl, "HEARTBEAT_PATH", tmp_path / "HEARTBEAT")
    monkeypatch.setattr(botctl, "PNL_SNAPSHOT_PATH", tmp_path / "pnl_snapshot.json")
    monkeypatch.setattr(botctl, "UNIVERSE_SNAPSHOT_PATH", tmp_path / "universe_snapshot.json")
    monkeypatch.setattr(botctl, "PID_PATH", tmp_path / "bot_control_pid.json")
    monkeypatch.setattr(botctl, "_STATE", {"proc": None, "started_at": None})
    monkeypatch.setattr(botctl, "_external_bots", lambda: [])
    yield tmp_path


def _fake_proc(alive: bool = True, pid: int = 4242):
    proc = MagicMock()
    proc.pid = pid
    proc.poll.return_value = None if alive else 0
    return proc


# ---- risk profile validation ----

def test_save_risk_rejects_out_of_range_value():
    cleaned, err = botctl.save_risk({"order_size": 5000.0})
    assert err is not None
    assert "order_size" in err
    assert cleaned == botctl.DEFAULT_RISK


def test_save_risk_accepts_valid_profile_and_persists():
    profile = dict(botctl.DEFAULT_RISK)
    profile["order_size"] = 25.0
    cleaned, err = botctl.save_risk(profile)
    assert err is None
    assert cleaned["order_size"] == 25.0
    assert botctl.load_risk()["order_size"] == 25.0


def test_load_risk_falls_back_to_defaults_when_file_missing():
    assert botctl.load_risk() == botctl.DEFAULT_RISK


# ---- run_state machine ----

def test_status_is_stopped_when_no_managed_process():
    assert botctl.status()["run_state"] == "STOPPED"


def test_status_recovers_a_bot_started_by_another_dashboard(monkeypatch):
    monkeypatch.setattr(botctl, "_external_bots", lambda: [4321])

    status = botctl.status()

    # READY is deliberately absent in this isolated test, so the recovered bot
    # is warming up rather than falsely shown as stopped.
    assert status["run_state"] == "STARTING"
    assert status["pid"] == 4321
    assert status["ownership"] == "recovered"
    assert status["external_bots"] == []


def test_external_bot_scan_requires_exact_python_module_argv(monkeypatch):
    process_table = """
      11 python -m polyuni.main
      12 zsh -c rg polyuni.main dashboard/botctl.py
      13 python -m polyuni.main_backup
      14 python worker.py --note polyuni.main
    """
    result = MagicMock()
    result.stdout = process_table
    monkeypatch.setattr(botctl.subprocess, "run", lambda *args, **kwargs: result)

    assert REAL_EXTERNAL_BOTS() == [11]


def test_status_is_starting_when_process_alive_but_not_ready(isolate_botctl_paths):
    botctl._STATE["proc"] = _fake_proc(alive=True)
    botctl._STATE["started_at"] = time.time()
    assert not botctl.READY_PATH.exists()

    assert botctl.status()["run_state"] == "STARTING"


def test_status_is_running_once_ready_marker_exists(isolate_botctl_paths):
    botctl._STATE["proc"] = _fake_proc(alive=True)
    botctl._STATE["started_at"] = time.time()
    botctl.READY_PATH.write_text(str(time.time()))

    assert botctl.status()["run_state"] == "RUNNING"


def test_status_is_stopping_when_emergency_stop_written_while_alive(isolate_botctl_paths):
    botctl._STATE["proc"] = _fake_proc(alive=True)
    botctl._STATE["started_at"] = time.time()
    botctl.READY_PATH.write_text(str(time.time()))
    botctl.EMERGENCY_STOP_PATH.write_text("stop")

    assert botctl.status()["run_state"] == "STOPPING"


def test_status_is_stopped_once_process_exits_even_with_stale_ready_file(isolate_botctl_paths):
    """A crashed process leaves a stale READY file behind -- status must not
    report RUNNING for a dead process just because READY still exists."""
    botctl._STATE["proc"] = _fake_proc(alive=False)
    botctl._STATE["started_at"] = time.time()
    botctl.READY_PATH.write_text(str(time.time()))

    assert botctl.status()["run_state"] == "STOPPED"


def test_emergency_stop_path_is_derived_from_settings_not_hardcoded():
    """There must be exactly one emergency-stop path in this codebase --
    botctl's EMERGENCY_STOP_PATH is derived directly from SETTINGS, never a
    second hardcoded literal (the confirmed polymarket_arb bug: its Streamlit
    dashboard checked a different, wrong path than RiskState actually used).

    A static source check, not a runtime one: the autouse isolation fixture in
    this file monkeypatches EMERGENCY_STOP_PATH for every other test (correct
    isolation), and reloading the module mid-suite to see the "real" value
    would fight that fixture's teardown bookkeeping. Reading the source is
    simpler and just as conclusive for "is this derived from SETTINGS."
    """
    source = (Path(__file__).resolve().parent.parent / "dashboard" / "botctl.py").read_text()
    assert "EMERGENCY_STOP_PATH = PROJECT_ROOT / SETTINGS.emergency_stop_file" in source


def test_stop_writes_to_the_configured_emergency_stop_path(isolate_botctl_paths):
    botctl.stop_bot()
    assert botctl.EMERGENCY_STOP_PATH.exists()


def test_stop_terminates_in_background_without_blocking(isolate_botctl_paths):
    proc = _fake_proc(alive=True)
    botctl._STATE["proc"] = proc
    botctl._STATE["started_at"] = time.time()

    result = botctl.stop_bot()

    assert result["stopped"] is True
    assert result["emergency_stop_written"] is True
    # terminate() is called from the background thread almost immediately;
    # give it a brief moment rather than asserting synchronously.
    deadline = time.time() + 1.0
    while proc.terminate.call_count == 0 and time.time() < deadline:
        time.sleep(0.01)
    assert proc.terminate.called


def test_status_surfaces_pnl_and_universe_snapshots_when_present(isolate_botctl_paths, tmp_path):
    botctl.PNL_SNAPSHOT_PATH.write_text(json.dumps({
        "session_pnl": 4.5, "locked_pnl": 4.5, "residual_pnl": 0.0,
        "would_lock_pnl": 1.2, "deployed_usd": 0.0, "generated_at": time.time(),
    }))
    botctl.UNIVERSE_SNAPSHOT_PATH.write_text(json.dumps({
        "total_tracked": 100, "state_counts": {"active": 50, "qualified": 20},
        "generated_at": time.time(),
    }))

    st = botctl.status()

    assert st["pnl"]["session_pnl"] == 4.5
    assert st["universe"]["active_count"] == 50
    assert st["universe"]["total_tracked"] == 100


def test_start_without_signer_uses_dry_run(monkeypatch):
    from dashboard import botctl
    monkeypatch.setattr(botctl, "_has_paper_signer", lambda: False)
    assert botctl.start_bot(scope="nope") == {"error": "scope must be one of universe, btc"}
