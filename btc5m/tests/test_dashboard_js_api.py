"""FastAPI TestClient tests for dashboard_js.server using synthetic tmp fixtures.

Tests never touch real state: they build a fake data root under tmp_path and
pass it to ``create_app`` (or set ``DASHBOARD_JS_DATA_ROOT``).
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dashboard_js import readers, server

W = 1786851600


def _write_jsonl(path: Path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def build_data_root(tmp_path: Path) -> Path:
    root = tmp_path / "data_root"
    state = root / "state"
    now = time.time()

    _write_jsonl(
        state / "bot.jsonl",
        [
            {"t": "01:00:00.000", "event": "INFO", "msg": "PAPER_TRADE=true with DRY_RUN=false: real auth flow"},
            {"t": "01:00:00.000", "event": "INFO", "msg": "polymarket arb bot starting | dry_run=False | live=True | paper_trade=True"},
            {
                "t": "01:00:01.000",
                "event": "TRADE",
                "msg": "missed",
                "data": {"reason": "stale_quote", "market_id": f"btc-updown-5m-{W}", "combined": 0.97, "age_ms": 650.5, "size": 10.0},
            },
            {
                "t": "01:00:02.000",
                "event": "TRADE",
                "msg": "filled",
                "data": {"market_id": f"btc-updown-5m-{W}", "combined": 0.98, "pnl_locked": 0.2, "size": 10.0, "age_ms": 5.0},
            },
            {
                "t": "01:00:03.000",
                "event": "TRADE",
                "msg": "partial_resolved",
                "data": {"market_id": f"btc-updown-5m-{W}", "residual_pnl": -0.1, "realized_pnl": -0.1, "size": 5.0, "age_ms": 92.8},
            },
            {
                "t": "01:01:00.000",
                "event": "INFO",
                "msg": "session metrics: loops=10 opps=3 sent=2 fills=1 partial=1 resolved=0 missed=1 opp2order_avg_ms=1.2 p50_ms=1.1 p95_ms=2.2 p99_ms=3.3 ack_avg_ms=1.0 locked_pnl=0.2 residual_pnl=-0.1 session_pnl=0.1 would_lock=0.3",
            },
        ],
    )
    _write_jsonl(
        state / "alerts.jsonl",
        [
            {"ts": now - 1000, "kind": "repeated_rejections", "message": "3+ consecutive rejections", "session_id": "sess_old"},
            {"ts": now - 100, "kind": "kill_switch", "message": "Risk halt triggered", "session_id": "sess_new"},
        ],
    )
    _write_jsonl(
        state / "order_ledger.jsonl",
        [
            {"market_id": f"btc-updown-5m-{W}", "status": "pending", "type": "order", "timestamp": now - 60, "reserved": 9.6},
            {"market_id": f"btc-updown-5m-{W}", "status": "resolved", "type": "resolution", "timestamp": now - 59, "final_committed_amount": 9.6},
        ],
    )
    (state / "HEARTBEAT").write_text(f"{now}\n")
    (state / "latency_profile.json").write_text(json.dumps({"fire_count": 5, "stages": {"reserve": {"n": 5, "avg_ms": 1.0}}}))

    cache = state / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    cache.joinpath("capture_depth_ws.lock").write_text(json.dumps({"pid": os.getpid(), "script": "capture_depth_ws.py"}))
    depth_rows = []
    for tok, offs in {
        "tokA": [1.0, 50.0, 100.0, 150.0, 200.0, 250.0, 299.0],
        "tokB": [2.0, 51.0, 101.0, 151.0, 201.0, 251.0, 296.0],
    }.items():
        for off in offs:
            depth_rows.append({"type": "depth_snapshot", "captured_at": W + off, "market_id": f"btc-updown-5m-{W}", "token_id": tok})
    for off in [1.0, 100.0, 299.0]:
        depth_rows.append({"type": "depth_snapshot", "captured_at": W + 300 + off, "market_id": f"btc-updown-5m-{W + 300}", "token_id": "tokA"})
    _write_jsonl(cache / "polymarket_depth_capture.jsonl", depth_rows)

    archive = state / "paper_session_archives" / "exec_stress_20260804T033625Z"
    _write_jsonl(
        archive / "bot.jsonl",
        [
            {
                "t": "20:36:25.835",
                "event": "INFO",
                "msg": "session metrics: loops=100 opps=2 sent=1 fills=0 partial=0 resolved=0 missed=2 opp2order_avg_ms=2.0 p50_ms=2.0 p95_ms=4.0 p99_ms=6.0 ack_avg_ms=2.0 locked_pnl=0.0 residual_pnl=-4.8 session_pnl=-4.8 would_lock=0.0",
            },
        ],
    )
    archive.joinpath("session_report.json").write_text("{}")
    old_mtime = 1785880800.0  # 2026-08-04T22:00:00Z, so "20:36:25" lands Aug 4 (no rollover)
    os.utime(archive / "bot.jsonl", (old_mtime, old_mtime))
    os.utime(archive / "session_report.json", (old_mtime, old_mtime))
    return root


@pytest.fixture()
def client(tmp_path: Path):
    root = build_data_root(tmp_path)
    readers.invalidate_depth_cache()
    app = server.create_app(root=root, static_dir=tmp_path / "missing_static")
    return TestClient(app)


def test_health(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["fetched_at_utc"]
    ds = body["data_sources"]
    for name in ("bot.jsonl", "alerts.jsonl", "order_ledger.jsonl", "HEARTBEAT"):
        assert ds[name]["exists"] is True
        assert ds[name]["bytes"] is not None
        assert ds[name]["mtime_utc"]
        assert isinstance(ds[name]["age_s"], float)
        assert isinstance(ds[name]["fresh"], bool)
    assert len(ds["depth_shards"]) == 1
    shard = ds["depth_shards"][0]
    assert shard["name"] == "polymarket_depth_capture.jsonl"
    assert shard["exists"] is True
    assert body["session_archives"]["count"] == 1
    assert body["session_archives"]["latest"] == "exec_stress_20260804T033625Z"


def test_health_env_var_root(tmp_path: Path, monkeypatch):
    root = build_data_root(tmp_path)
    monkeypatch.setenv("DASHBOARD_JS_DATA_ROOT", str(root))
    readers.invalidate_depth_cache()
    app = server.create_app(static_dir=tmp_path / "missing_static")
    body = TestClient(app).get("/api/health").json()
    assert body["data_sources"]["bot.jsonl"]["exists"] is True


def test_command(client, monkeypatch):
    monkeypatch.delenv("PAPER_TRADE", raising=False)
    resp = client.get("/api/command")
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "PAPER_TRADE"
    assert body["paper_trade"] is True
    assert body["dry_run"] is False
    assert body["bot_log_age_s"] is not None
    assert isinstance(body["bot_log_fresh"], bool)
    assert body["last_log_ts_utc"]
    ks = body["kill_switch"]
    assert ks["state"] == "halted"
    assert ks["last_message"] == "Risk halt triggered"
    assert ks["session_id"] == "sess_new"
    assert ks["last_alert_ts_utc"]
    sess = body["session"]
    assert sess["start_ts_utc"]
    assert sess["loops"] == 10
    assert sess["opps"] == 3
    assert sess["fills"] == 1
    assert sess["elapsed_s"] == 60.0
    assert body["utc_now"]


def test_command_paper_env_var(client, monkeypatch):
    monkeypatch.setenv("PAPER_TRADE", "true")
    body = client.get("/api/command").json()
    assert body["mode"] == "PAPER_TRADE"
    monkeypatch.setenv("PAPER_TRADE", "false")
    body = client.get("/api/command").json()
    assert body["mode"] == "LIVE"


def test_command_no_kill_switch(tmp_path: Path):
    root = build_data_root(tmp_path)
    (root / "state" / "alerts.jsonl").write_text("")
    app = server.create_app(root=root, static_dir=tmp_path / "missing_static")
    body = TestClient(app).get("/api/command").json()
    assert body["kill_switch"]["state"] == "unknown"


def test_ticker(client):
    resp = client.get("/api/ticker?limit=60")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert [i["event"] for i in items] == ["missed", "filled", "partial_resolved"]
    times = [i["ts_utc"] for i in items]
    assert times == sorted(times)
    first = items[0]
    assert first["market_id"] == f"btc-updown-5m-{W}"
    assert abs(first["combined"] - 0.97) < 1e-9
    assert abs(first["edge"] - 0.03) < 1e-9
    assert first["outcome"] == "missed"
    assert first["reason"] == "stale_quote"
    assert first["pnl"] is None
    assert first["size"] == 10.0
    assert first["age_ms"] == 650.5
    filled = items[1]
    assert filled["outcome"] == "filled"
    assert filled["pnl"] == 0.2
    partial = items[2]
    assert partial["outcome"] == "partial"
    assert partial["pnl"] == -0.1


def test_heatmap(client):
    resp = client.get("/api/heatmap?limit=48")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert [(i["kind"], i.get("reason")) for i in items] == [
        ("reject", "stale_quote"),
        ("fill", None),
        ("partial", None),
    ]
    assert items[0]["edge"] == pytest.approx(0.03)
    assert items[1]["pnl"] == 0.2


def test_pnl_merges_archives(client):
    resp = client.get("/api/pnl")
    assert resp.status_code == 200
    body = resp.json()
    assert "series merges" in body["notes"]
    series = body["series"]
    sources = {s["source"] for s in series}
    assert "live" in sources
    assert "archive:exec_stress_20260804T033625Z" in sources
    assert [s["ts_utc"] for s in series] == sorted(s["ts_utc"] for s in series)
    latest = body["latest"]
    assert latest["locked"] == 0.2
    assert latest["residual"] == -0.1
    assert latest["session"] == 0.1
    assert latest["would_lock"] == 0.3
    assert latest["ts_utc"]


def test_pnl_dedupes_identical_ts(tmp_path: Path):
    root = build_data_root(tmp_path)
    archive_bot = root / "state" / "paper_session_archives" / "exec_stress_20260804T033625Z" / "bot.jsonl"
    msg = (
        "session metrics: loops=100 opps=2 sent=1 fills=0 partial=0 resolved=0 missed=2 "
        "opp2order_avg_ms=2.0 p50_ms=2.0 p95_ms=4.0 p99_ms=6.0 ack_avg_ms=2.0 "
        "locked_pnl=9.9 residual_pnl=-4.8 session_pnl=-4.8 would_lock=0.0"
    )
    rows = [json.dumps({"t": "2026-08-04T20:36:25.835Z", "event": "INFO", "msg": msg}) for _ in range(2)]
    archive_bot.write_text("\n".join(rows) + "\n")
    app = server.create_app(root=root, static_dir=tmp_path / "missing_static")
    body = TestClient(app).get("/api/pnl").json()
    # two identical-ts archive rows collapse to one point; live point is separate
    assert len(body["series"]) == 2
    archive_points = [s for s in body["series"] if s["source"].startswith("archive:")]
    assert len(archive_points) == 1
    assert archive_points[0]["locked"] == 9.9


def test_pnl_empty(client, tmp_path):
    app = server.create_app(root=tmp_path / "empty", static_dir=tmp_path / "missing_static")
    body = TestClient(app).get("/api/pnl").json()
    assert body["series"] == []
    assert body["latest"] == {"locked": None, "residual": None, "session": None, "would_lock": None, "ts_utc": None}


def test_alerts_newest_first(client):
    resp = client.get("/api/alerts?limit=50")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert [i["kind"] for i in items] == ["kill_switch", "repeated_rejections"]
    assert items[0]["message"] == "Risk halt triggered"
    assert items[0]["session_id"] == "sess_new"
    assert items[0]["ts_utc"]


def test_latency(client):
    resp = client.get("/api/latency")
    assert resp.status_code == 200
    body = resp.json()
    latest = body["latest"]
    assert latest["opp2order_avg_ms"] == 1.2
    assert latest["p50_ms"] == 1.1
    assert latest["p95_ms"] == 2.2
    assert latest["p99_ms"] == 3.3
    assert latest["ack_avg_ms"] == 1.0
    assert latest["n"] == 10
    assert latest["ts_utc"]
    assert len(body["series"]) == 1
    assert body["series"][0]["p50_ms"] == 1.1
    assert body["profile"]["fire_count"] == 5


def test_depth(client, tmp_path: Path):
    resp = client.get("/api/depth")
    assert resp.status_code == 200
    body = resp.json()
    proc = body["process"]
    assert proc["pid"] == os.getpid()
    assert proc["alive"] is True
    assert proc["lock_held"] is True
    assert len(body["shards"]) == 1
    shard = body["shards"][0]
    assert shard["name"] == "polymarket_depth_capture.jsonl"
    assert shard["bytes"] > 0
    assert shard["age_s"] is not None
    assert body["snapshot_count"] == 17
    assert body["last_capture_age_s"] is not None
    cont = body["continuity"]
    assert cont == {
        "windows": 2,
        "complete": 1,
        "broken": 1,
        "ratio": 0.5,
        "criterion": "both tokens: first<=5s, last>=295s, max gap<=60s",
        "scope": "newest shard only",
    }
    assert body["last_30_windows"] == {"complete": 1, "windows": 2}
    # The full-history sidecar is produced by a background scan; before it
    # completes the rows detail is None (honest pending state).
    assert body["snapshot_count_scope"] in {
        "newest shard only (sidecar pending)",
        "all shards (sidecar)",
    }
    assert "scanning" in body

    # Once the sidecar exists, the shard detail and history block appear.
    from dashboard_js import readers

    readers.refresh_depth_sidecar(build_data_root(tmp_path))
    resp = client.get("/api/depth")
    body = resp.json()
    assert body["snapshot_count_scope"] == "all shards (sidecar)"
    shard = body["shards"][0]
    assert shard["rows"] == 17
    assert shard["first_ts_utc"] and shard["last_ts_utc"]
    assert body["history"]["continuity"]["windows"] == 2
    assert body["history"]["last_30_windows"] == {"complete": 1, "windows": 2}


def test_models_and_checklist_degrade(monkeypatch, client):
    import dashboard_js as dashboard_js_pkg

    original = sys.modules.get("dashboard_js.parsers")
    sys.modules["dashboard_js.parsers"] = None
    had_attr = hasattr(dashboard_js_pkg, "parsers")
    if had_attr:
        delattr(dashboard_js_pkg, "parsers")
    try:
        resp = client.get("/api/models")
        assert resp.status_code == 200
        body = resp.json()
        assert body["error"] == "parsers module unavailable"
        assert body["models"] == []
        assert body["fetched_at_utc"]
        resp = client.get("/api/checklist")
        assert resp.status_code == 200
        body = resp.json()
        assert body["error"] == "parsers module unavailable"
        assert body["checklist"] == []
    finally:
        if original is None:
            sys.modules.pop("dashboard_js.parsers", None)
        else:
            sys.modules["dashboard_js.parsers"] = original
        if had_attr:
            setattr(dashboard_js_pkg, "parsers", original)


def test_static_missing(client):
    resp = client.get("/")
    assert resp.status_code == 404
    assert "static dir not yet written" in resp.text
    resp = client.get("/static/app.js")
    assert resp.status_code == 404
    assert "static dir not yet written" in resp.text


def test_static_present(tmp_path: Path):
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<html>hello dashboard</html>")
    app = server.create_app(root=tmp_path / "data_root", static_dir=static)
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "hello dashboard" in resp.text


def test_broken_bot_jsonl_does_not_crash(tmp_path: Path):
    root = build_data_root(tmp_path)
    (root / "state" / "bot.jsonl").write_text("this is not json\n{\"broken\": \n")
    app = server.create_app(root=root, static_dir=tmp_path / "missing_static")
    client = TestClient(app)
    resp = client.get("/api/ticker")
    assert resp.status_code == 200
    assert resp.json()["items"] == []
    resp = client.get("/api/command")
    assert resp.status_code == 200
    body = resp.json()
    assert body["session"]["start_ts_utc"] is None
    resp = client.get("/api/pnl")
    assert resp.status_code == 200
    sources = [s["source"] for s in resp.json()["series"]]
    assert "live" not in sources  # broken bot.jsonl contributes nothing
    assert any(s.startswith("archive:") for s in sources)


def test_all_endpoints_have_fetched_at_utc(client):
    for path in (
        "/api/health",
        "/api/command",
        "/api/ticker",
        "/api/heatmap",
        "/api/pnl",
        "/api/alerts",
        "/api/latency",
        "/api/depth",
    ):
        body = client.get(path).json()
        assert body["fetched_at_utc"], path
