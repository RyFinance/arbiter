"""polyuni dashboard — FastAPI server.

REST endpoints read either botctl's process-control state or the snapshot
files the bot writes (state/pnl_snapshot.json, state/universe_snapshot.json)
-- the dashboard is a separate process from the bot by design and never
touches bot internals directly. /ws (Phase 5) pushes updates over the same
app by watching those same files for mtime changes (see push.py) -- REST
stays available as the reconnect fallback, not replaced.

Route handlers and their request models are deliberately module-level, not
nested inside a factory function: with `from __future__ import annotations`
(PEP 563), FastAPI resolves parameter type hints via get_type_hints() against
the function's *module* globals -- a Pydantic model class defined inside a
local function only lives in that function's local scope, so FastAPI silently
fails to recognize it as a request body and treats the parameter as a query
param instead. Confirmed live: an earlier version of this file nested
StartRequest inside create_app() and POST /api/botctl/start returned
"field required: query.req" instead of reading the JSON body.
"""
from __future__ import annotations

import asyncio
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(DASHBOARD_DIR))

import botctl  # noqa: E402  (dashboard/botctl.py, same directory)
from push import PushManager  # noqa: E402  (dashboard/push.py, same directory)
from polyuni.live_control import default_live_control  # noqa: E402

STATIC_DIR = DASHBOARD_DIR / "static"
PNL_SNAPSHOT_PATH = PROJECT_ROOT / "state" / "pnl_snapshot.json"
UNIVERSE_SNAPSHOT_PATH = PROJECT_ROOT / "state" / "universe_snapshot.json"
STRATEGY_SNAPSHOT_PATH = PROJECT_ROOT / "state" / "strategy_snapshot.json"

push_manager = PushManager()


@asynccontextmanager
async def lifespan(app: FastAPI):
    push_manager.start(
        PNL_SNAPSHOT_PATH,
        UNIVERSE_SNAPSHOT_PATH,
        botctl.status,
        STRATEGY_SNAPSHOT_PATH,
    )
    yield
    push_manager.stop()


def _read_json(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


class StartRequest(BaseModel):
    risk: Optional[Dict[str, float]] = None


class LiveControlToggleRequest(BaseModel):
    enabled: bool
    reason: str


app = FastAPI(title="polyuni dashboard", lifespan=lifespan)


@app.get("/api/health")
def health() -> Dict[str, Any]:
    return {"ok": True}


@app.get("/api/status")
def status() -> Dict[str, Any]:
    return botctl.status()


@app.post("/api/botctl/start")
def start(req: StartRequest) -> Dict[str, Any]:
    result = botctl.start_bot(risk=req.risk)
    if "error" in result:
        raise HTTPException(status_code=409, detail=result["error"])
    return result


@app.post("/api/botctl/stop")
def stop() -> Dict[str, Any]:
    return botctl.stop_bot()


@app.get("/api/log")
def log(lines: int = 100) -> Dict[str, Any]:
    return botctl.log_tail(lines=lines)


@app.get("/api/pnl")
def pnl() -> Dict[str, Any]:
    snap = _read_json(PNL_SNAPSHOT_PATH)
    if snap is None:
        return {"available": False}
    snap["available"] = True
    return snap


@app.get("/api/universe")
def universe() -> Dict[str, Any]:
    snap = _read_json(UNIVERSE_SNAPSHOT_PATH)
    if snap is None:
        return {"available": False, "markets": []}
    snap["available"] = True
    return snap


@app.get("/api/strategy")
def strategy() -> Dict[str, Any]:
    snap = _read_json(STRATEGY_SNAPSHOT_PATH)
    if snap is None:
        return {"available": False, "mode": "unavailable", "open_sequences": []}
    snap["available"] = True
    return snap


@app.get("/api/live_control")
def live_control_state() -> Dict[str, Any]:
    control = default_live_control()
    state = control.read_state()
    return {
        "operator_enabled": state.operator_enabled,
        "effective_live_submission_enabled": state.effective_live_submission_enabled,
        "hard_block_reason": state.hard_block_reason,
        "revision": state.revision,
        "updated_at": state.updated_at,
        "updated_by": state.updated_by,
    }


@app.post("/api/live_control/toggle")
def live_control_toggle(req: LiveControlToggleRequest) -> Dict[str, Any]:
    if not req.reason.strip():
        raise HTTPException(status_code=400, detail="reason is required for the audit trail")
    control = default_live_control()
    new_state = control.set_operator_enabled(req.enabled, actor="dashboard", reason=req.reason)
    return {
        "operator_enabled": new_state.operator_enabled,
        "effective_live_submission_enabled": new_state.effective_live_submission_enabled,
        "hard_block_reason": new_state.hard_block_reason,
        "revision": new_state.revision,
    }


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    queue = await push_manager.connect(websocket)
    try:
        # Send one immediate snapshot of everything on connect so a client
        # doesn't have to wait up to WATCH_INTERVAL_S for its first paint.
        await websocket.send_json({"type": "bot_status", "data": botctl.status()})
        pnl = _read_json(PNL_SNAPSHOT_PATH)
        if pnl is not None:
            await websocket.send_json({"type": "pnl_update", "data": pnl})
        universe = _read_json(UNIVERSE_SNAPSHOT_PATH)
        if universe is not None:
            await websocket.send_json({"type": "universe_update", "data": universe})
        strategy = _read_json(STRATEGY_SNAPSHOT_PATH)
        if strategy is not None:
            await websocket.send_json({"type": "strategy_update", "data": strategy})

        while True:
            # This connection has nothing to receive from the client -- push
            # is one-directional -- but awaiting recv is what actually
            # detects a client-initiated disconnect (a pure send-only loop
            # would never notice and leak the queue).
            recv_task = asyncio.create_task(websocket.receive_text())
            queue_task = asyncio.create_task(queue.get())
            done, pending = await asyncio.wait(
                {recv_task, queue_task}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            if recv_task in done:
                recv_task.result()  # raises WebSocketDisconnect if that's what happened
            if queue_task in done:
                await websocket.send_text(queue_task.result())
    except WebSocketDisconnect:
        pass
    finally:
        push_manager.disconnect(queue)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
