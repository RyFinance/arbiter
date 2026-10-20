"""Tests for dashboard/server.py's FastAPI routes.

test_start_accepts_json_body_not_query_params locks in a real bug found via a
live server run (not caught by any unit test until this one was added): the
StartRequest Pydantic model was originally defined *inside* create_app()
rather than at module level. With `from __future__ import annotations` in
effect, FastAPI resolves parameter annotations via get_type_hints() against
the function's module globals -- a class scoped inside another function isn't
visible there, so FastAPI silently treated `req: StartRequest` as an
unrecognized type and fell back to expecting `req` as a query parameter.
POST /api/botctl/start returned 422 "field required: query.req" instead of
reading the JSON body. Moving the models to module level fixed it; this test
starts a real bot (mocked) via a real JSON POST body so a regression back to
the broken shape fails loudly.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dashboard"))
import botctl  # noqa: E402
import server  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    # Isolate botctl's on-disk state the same way test_botctl.py does.
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
    monkeypatch.setattr(server, "PNL_SNAPSHOT_PATH", tmp_path / "pnl_snapshot.json")
    monkeypatch.setattr(server, "UNIVERSE_SNAPSHOT_PATH", tmp_path / "universe_snapshot.json")
    monkeypatch.setattr(server, "STRATEGY_SNAPSHOT_PATH", tmp_path / "strategy_snapshot.json")
    return TestClient(server.app)


def test_health(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_status_reports_stopped_with_no_managed_process(client):
    resp = client.get("/api/status")
    assert resp.status_code == 200
    assert resp.json()["run_state"] == "STOPPED"


def test_start_accepts_json_body_not_query_params(client, monkeypatch):
    """Regression test for the real body-vs-query-param bug described in the
    module docstring."""
    monkeypatch.setattr(botctl, "start_bot", lambda risk=None, scope="universe", venue="polymarket": {"started": True, "pid": 1234, "risk": {}})

    resp = client.post("/api/botctl/start", json={})

    assert resp.status_code == 200, resp.text
    assert resp.json()["started"] is True
    assert resp.json()["pid"] == 1234


def test_start_passes_risk_overrides_through_the_body(client, monkeypatch):
    captured = {}

    def fake_start_bot(risk=None, scope="universe", venue="polymarket"):
        captured["risk"] = risk
        return {"started": True, "pid": 1}

    monkeypatch.setattr(botctl, "start_bot", fake_start_bot)

    resp = client.post("/api/botctl/start", json={"risk": {"order_size": 42.0}})

    assert resp.status_code == 200
    assert captured["risk"] == {"order_size": 42.0}


def test_start_returns_409_when_botctl_reports_an_error(client, monkeypatch):
    monkeypatch.setattr(botctl, "start_bot", lambda risk=None, scope="universe", venue="polymarket": {"error": "already running", "pid": 99})

    resp = client.post("/api/botctl/start", json={})

    assert resp.status_code == 409
    assert "already running" in resp.json()["detail"]


def test_stop_calls_botctl_stop(client, monkeypatch):
    monkeypatch.setattr(botctl, "stop_bot", lambda: {"stopped": True, "emergency_stop_written": True})

    resp = client.post("/api/botctl/stop")

    assert resp.status_code == 200
    assert resp.json()["stopped"] is True


def test_pnl_reports_unavailable_when_no_snapshot_exists(client):
    resp = client.get("/api/pnl")
    assert resp.json() == {"available": False}


def test_pnl_returns_snapshot_contents_when_present(client, tmp_path):
    import json
    snapshot = {"session_pnl": 3.5, "locked_pnl": 3.5, "per_market": {}}
    (tmp_path / "pnl_snapshot.json").write_text(json.dumps(snapshot))

    resp = client.get("/api/pnl")

    assert resp.json()["available"] is True
    assert resp.json()["session_pnl"] == 3.5


def test_universe_reports_unavailable_when_no_snapshot_exists(client):
    resp = client.get("/api/universe")
    assert resp.json() == {"available": False, "markets": []}


def test_strategy_reports_unavailable_when_no_snapshot_exists(client):
    resp = client.get("/api/strategy")
    assert resp.status_code == 200
    assert resp.json() == {
        "available": False,
        "mode": "unavailable",
        "open_sequences": [],
    }


def test_strategy_returns_sequential_snapshot_contents(client, tmp_path):
    import json
    snapshot = {
        "mode": "sequential",
        "phase_counts": {"watching": 3, "seeking_second": 1, "consumed": 2, "manual_review": 1},
        "open_exposure_usd": 4.82,
        "completed_pairs": 2,
        "timeouts": 1,
        "exits": 1,
        "rejections": {"volatility_below_threshold": 19},
        "lightgbm": {
            "regime": "HIGH",
            "confidence": 0.72,
            "source": "lightgbm_v3",
            "status": "fresh",
            "age_s": 4.0,
            "usable": True,
        },
        "open_sequences": [{"sequence_id": "seq-1", "phase": "seeking_second"}],
        "manual_review_sequences": [{"sequence_id": "recovered-1", "side": "YES", "cost": 4.82}],
    }
    (tmp_path / "strategy_snapshot.json").write_text(json.dumps(snapshot))

    resp = client.get("/api/strategy")

    assert resp.status_code == 200
    assert resp.json()["available"] is True
    assert resp.json()["open_exposure_usd"] == 4.82
    assert resp.json()["lightgbm"]["source"] == "lightgbm_v3"
    assert resp.json()["phase_counts"]["consumed"] == 2
    assert resp.json()["manual_review_sequences"][0]["sequence_id"] == "recovered-1"


def test_live_control_reports_fail_closed_default(client):
    resp = client.get("/api/live_control")
    body = resp.json()
    assert body["operator_enabled"] is False
    assert body["effective_live_submission_enabled"] is False
    assert body["hard_block_reason"] == "live_exchange_order_endpoint_unbuilt"


def test_live_control_toggle_requires_a_reason(client):
    resp = client.post("/api/live_control/toggle", json={"enabled": True, "reason": ""})
    assert resp.status_code == 400


def test_live_control_toggle_records_operator_intent_but_stays_hard_blocked(client, tmp_path, monkeypatch):
    import polyuni.live_control as live_control_module
    control = live_control_module.LiveTradingControl(
        tmp_path / "live_control.json", tmp_path / "live_control_audit.jsonl",
    )
    monkeypatch.setattr(live_control_module, "_DEFAULT_CONTROL", control)

    resp = client.post("/api/live_control/toggle", json={"enabled": True, "reason": "dashboard test"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["operator_enabled"] is True
    # Arming the operator flag NEVER flips effective submission -- there is no
    # exchange order-submission code path in this codebase.
    assert body["effective_live_submission_enabled"] is False


def test_index_serves_html(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "polyuni" in resp.text.lower()


def test_static_assets_are_served(client):
    resp = client.get("/static/app.js")
    assert resp.status_code == 200
    assert "poll" in resp.text


def test_console_exposes_sequential_telemetry_without_market_listing(client):
    html = client.get("/").text
    script = client.get("/static/app.js").text

    assert "strategy-phase" in html
    assert "FIRST-LEG EXPOSURE" in html
    assert "LightGBM regime" in html
    assert "PAPER-ONLY SAFETY BOUNDARY" in html
    assert "universe-table" not in html
    assert 'fetchJson("/api/strategy")' in script
    assert 'message.type === "strategy_update"' in script
    assert "external_bots" in script
    assert 'phase === "CONSUMED"' in script
    assert "manual_review_sequences" in script
    assert "MANUAL REVIEW" in script


def test_setup_endpoint_never_returns_secret_values(client, monkeypatch):
    monkeypatch.setenv("PRIVATE_KEY", "0x" + "ab" * 32)
    body = client.get("/api/setup").json()
    row = next(r for r in body["settings"] if r["name"] == "PRIVATE_KEY")
    assert row["now"] == "set"
    assert "ab" * 32 not in str(body)
