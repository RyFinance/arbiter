"""Tests for dashboard/push.py's PushManager -- the file-mtime-watch broadcaster
that lets the dashboard push updates without the bot process knowing anything
about a WebSocket server (they stay separate processes by design)."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dashboard"))
from push import PushManager  # noqa: E402


class _FakeQueue:
    """Minimal stand-in so tests don't need a real asyncio.Queue's maxsize
    semantics for the happy-path broadcast tests."""

    def __init__(self, maxsize=0):
        self.items = []

    def put_nowait(self, item):
        self.items.append(item)


def test_broadcast_delivers_to_every_connected_client():
    manager = PushManager()
    q1, q2 = _FakeQueue(), _FakeQueue()
    manager._clients = {q1, q2}

    manager.broadcast({"type": "pnl_update", "data": {"session_pnl": 1.0}})

    assert len(q1.items) == 1
    assert len(q2.items) == 1
    assert json.loads(q1.items[0])["type"] == "pnl_update"


def test_disconnect_removes_client_from_broadcast_set():
    manager = PushManager()
    q1, q2 = _FakeQueue(), _FakeQueue()
    manager._clients = {q1, q2}

    manager.disconnect(q1)
    manager.broadcast({"type": "bot_status", "data": {}})

    assert q1.items == []
    assert len(q2.items) == 1


def test_changed_detects_mtime_delta_and_dedupes_unchanged(tmp_path):
    manager = PushManager()
    path = tmp_path / "pnl_snapshot.json"
    path.write_text("{}")

    assert manager._changed("pnl", path) is True   # first observation is always a change
    assert manager._changed("pnl", path) is False  # unchanged mtime -> no change

    path.write_text('{"x": 1}')
    assert manager._changed("pnl", path) is True    # mtime moved -> change again


def test_changed_returns_false_for_missing_file(tmp_path):
    manager = PushManager()
    assert manager._changed("pnl", tmp_path / "does_not_exist.json") is False


@pytest.mark.asyncio
async def test_watch_loop_broadcasts_pnl_update_when_file_changes(tmp_path, monkeypatch):
    import push as push_module
    monkeypatch.setattr(push_module, "WATCH_INTERVAL_S", 0.01)

    manager = PushManager()
    q = _FakeQueue()
    manager._clients = {q}

    pnl_path = tmp_path / "pnl_snapshot.json"
    universe_path = tmp_path / "universe_snapshot.json"
    pnl_path.write_text(json.dumps({"session_pnl": 9.0}))

    task = asyncio.create_task(manager._watch_loop(pnl_path, universe_path, lambda: {"run_state": "RUNNING"}))
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    types = [json.loads(item)["type"] for item in q.items]
    assert "pnl_update" in types
    pnl_messages = [json.loads(item) for item in q.items if json.loads(item)["type"] == "pnl_update"]
    assert pnl_messages[0]["data"]["session_pnl"] == 9.0


@pytest.mark.asyncio
async def test_watch_loop_does_not_rebroadcast_unchanged_file(tmp_path, monkeypatch):
    import push as push_module
    monkeypatch.setattr(push_module, "WATCH_INTERVAL_S", 0.01)
    monkeypatch.setattr(push_module, "STATUS_PUSH_EVERY_N_TICKS", 10_000)  # suppress status noise

    manager = PushManager()
    q = _FakeQueue()
    manager._clients = {q}

    pnl_path = tmp_path / "pnl_snapshot.json"
    universe_path = tmp_path / "universe_snapshot.json"
    pnl_path.write_text(json.dumps({"session_pnl": 1.0}))

    task = asyncio.create_task(manager._watch_loop(pnl_path, universe_path, lambda: {}))
    await asyncio.sleep(0.05)  # several ticks over an unchanged file
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    pnl_broadcasts = [item for item in q.items if json.loads(item)["type"] == "pnl_update"]
    assert len(pnl_broadcasts) == 1  # only the initial observation, not once per tick


@pytest.mark.asyncio
async def test_watch_loop_broadcasts_strategy_snapshot(tmp_path, monkeypatch):
    import push as push_module
    monkeypatch.setattr(push_module, "WATCH_INTERVAL_S", 0.01)

    manager = PushManager()
    q = _FakeQueue()
    manager._clients = {q}
    pnl_path = tmp_path / "pnl.json"
    universe_path = tmp_path / "universe.json"
    strategy_path = tmp_path / "strategy.json"
    strategy_path.write_text(json.dumps({"mode": "sequential", "open_sequences": []}))

    task = asyncio.create_task(manager._watch_loop(
        pnl_path, universe_path, lambda: {}, strategy_path,
    ))
    await asyncio.sleep(0.04)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    messages = [json.loads(item) for item in q.items]
    strategy = [item for item in messages if item["type"] == "strategy_update"]
    assert len(strategy) == 1
    assert strategy[0]["data"]["mode"] == "sequential"
