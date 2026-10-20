from __future__ import annotations

import hashlib
import json

import pytest

from polymarket_arb.demo_replay import (
    discover_demo_sessions,
    load_demo_events,
    replay_snapshot,
    verify_archive,
)


def _write_session(root, *, tamper: bool = False):
    directory = root / "task_demo_20260815T000000Z"
    directory.mkdir()
    report = {
        "session_id": directory.name,
        "started_at": "2026-08-15T00:00:00+00:00",
        "finished_at": "2026-08-15T00:05:00+00:00",
        "duration_seconds": 300.0,
        "metrics": {"event_count": 4},
    }
    (directory / "session_report.json").write_text(json.dumps(report) + "\n", encoding="utf-8")
    events = [
        {"t": "23:59:59.000", "event": "INFO", "msg": "start"},
        {"t": "23:59:59.500", "event": "INFO", "msg": "FIRE market=m1"},
        {"t": "00:00:00.250", "event": "TRADE", "msg": "filled", "data": {}},
        {"t": "00:04:59.000", "event": "INFO", "msg": "session metrics: fills=1"},
    ]
    (directory / "bot.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
    )
    seal_lines = []
    for name in ("bot.jsonl", "session_report.json"):
        digest = hashlib.sha256((directory / name).read_bytes()).hexdigest()
        seal_lines.append(f"{digest}  {name}")
    (directory / "SHA256SUMS").write_text("\n".join(seal_lines) + "\n", encoding="utf-8")
    if tamper:
        (directory / "bot.jsonl").write_text("tampered\n", encoding="utf-8")
    return directory


def test_discovery_requires_and_verifies_sealed_real_archive(tmp_path):
    directory = _write_session(tmp_path)
    sessions = discover_demo_sessions(tmp_path)
    assert len(sessions) == 1
    assert sessions[0].directory == directory
    assert sessions[0].seal_valid is True
    assert verify_archive(directory) == (True, ())


def test_tampered_archive_is_not_replayable(tmp_path):
    _write_session(tmp_path, tamper=True)
    session = discover_demo_sessions(tmp_path)[0]
    assert session.seal_valid is False
    assert any("hash mismatch" in error for error in session.seal_errors)
    with pytest.raises(ValueError, match="seal is invalid"):
        load_demo_events(session)


def test_replay_is_deterministic_offline_and_handles_midnight(tmp_path):
    _write_session(tmp_path)
    session = discover_demo_sessions(tmp_path)[0]
    events = load_demo_events(session)
    assert events[2]["_elapsed_s"] == pytest.approx(1.25)

    early = replay_snapshot(session, 0.005)
    final = replay_snapshot(session, 1.0)
    assert early["network_used"] is False
    assert early["fires"] == 1
    assert early["trade_events"] == 1
    assert final["visible_event_count"] == 4
    assert final["trade_outcomes"] == {"filled": 1}
    assert final["latest_summary"] == "session metrics: fills=1"
