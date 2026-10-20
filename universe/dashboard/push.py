"""WebSocket push layer for the dashboard.

The bot (polyuni.main) and the dashboard are deliberately separate processes
-- botctl spawns the bot as a subprocess, and the dashboard reads whatever
state it publishes to disk (state/pnl_snapshot.json, state/universe_snapshot.json).
That separation means push can't be "the bot calls into the dashboard's
WebSocket server directly" without breaking it (the dashboard must be able to
restart independently of the bot, and vice versa). Instead, PushManager
polls the same snapshot files main.py already writes atomically, by mtime,
and broadcasts a message only when a file actually changed -- cheap (a stat
call, not a re-read, on every idle tick) and needs zero changes to main.py.

Connected clients are asyncio Queues, matching Starlette's WebSocket accept
loop: `await websocket.send_json` is only ever called from the connection's
own task, never from the background broadcaster, so a slow client can't block
delivery to the others (the broadcaster only ever enqueues), and this stays
correct if a client disconnects mid-broadcast (Queue.put_nowait doesn't care).
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, Optional, Set

from fastapi import WebSocket

# How often the background watcher checks file mtimes / recomputes bot status.
# Deliberately shorter than the old REST poll interval (2.5s) -- the whole
# point of this layer is lower latency than polling, and a stat() call is
# nearly free.
WATCH_INTERVAL_S = 1.0
# bot_status is pushed on this cadence regardless of mtime (there's no status
# file to watch -- botctl.status() is cheap to recompute every tick).
STATUS_PUSH_EVERY_N_TICKS = 2  # ~2s at WATCH_INTERVAL_S=1.0


class PushManager:
    def __init__(self):
        self._clients: Set[asyncio.Queue] = set()
        self._mtimes: Dict[str, float] = {}
        self._watch_task: Optional[asyncio.Task] = None
        self._tick = 0

    async def connect(self, websocket: WebSocket) -> asyncio.Queue:
        await websocket.accept()
        queue: asyncio.Queue = asyncio.Queue(maxsize=64)
        self._clients.add(queue)
        return queue

    def disconnect(self, queue: asyncio.Queue) -> None:
        self._clients.discard(queue)

    def broadcast(self, message: Dict[str, Any]) -> None:
        payload = json.dumps(message)
        for queue in list(self._clients):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                # A client that isn't draining its queue is already stale;
                # drop the oldest pending message rather than block or grow
                # unbounded memory for a dead/slow connection.
                try:
                    queue.get_nowait()
                    queue.put_nowait(payload)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

    def _changed(self, key: str, path: Path) -> bool:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return False
        if self._mtimes.get(key) == mtime:
            return False
        self._mtimes[key] = mtime
        return True

    async def _watch_loop(
        self,
        pnl_path: Path,
        universe_path: Path,
        status_fn,
        strategy_path: Optional[Path] = None,
    ) -> None:
        while True:
            try:
                if self._changed("pnl", pnl_path):
                    try:
                        data = json.loads(pnl_path.read_text(encoding="utf-8"))
                        self.broadcast({"type": "pnl_update", "data": data})
                    except (OSError, json.JSONDecodeError):
                        pass
                if self._changed("universe", universe_path):
                    try:
                        data = json.loads(universe_path.read_text(encoding="utf-8"))
                        self.broadcast({"type": "universe_update", "data": data})
                    except (OSError, json.JSONDecodeError):
                        pass
                if strategy_path is not None and self._changed("strategy", strategy_path):
                    try:
                        data = json.loads(strategy_path.read_text(encoding="utf-8"))
                        self.broadcast({"type": "strategy_update", "data": data})
                    except (OSError, json.JSONDecodeError):
                        pass
                self._tick += 1
                if self._tick % STATUS_PUSH_EVERY_N_TICKS == 0:
                    self.broadcast({"type": "bot_status", "data": status_fn()})
            except Exception:
                pass  # the watcher must never die from one bad tick
            await asyncio.sleep(WATCH_INTERVAL_S)

    def start(
        self,
        pnl_path: Path,
        universe_path: Path,
        status_fn,
        strategy_path: Optional[Path] = None,
    ) -> None:
        if self._watch_task is None:
            self._watch_task = asyncio.create_task(
                self._watch_loop(pnl_path, universe_path, status_fn, strategy_path),
                name="dashboard-push-watcher",
            )

    def stop(self) -> None:
        if self._watch_task is not None:
            self._watch_task.cancel()
            self._watch_task = None
