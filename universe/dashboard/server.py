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
import time
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

import httpx  # noqa: E402
import botctl  # noqa: E402  (dashboard/botctl.py, same directory)
from push import PushManager  # noqa: E402  (dashboard/push.py, same directory)
from polyuni.live_control import default_live_control  # noqa: E402
from polyuni import venues  # noqa: E402
from polyuni.config import SETTINGS  # noqa: E402

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
    scope: str = "universe"


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
    result = botctl.start_bot(risk=req.risk, scope=req.scope)
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


ORDER_LEDGER_PATH = PROJECT_ROOT / "state" / "order_ledger.jsonl"
CLOB_BASE_URL = SETTINGS.clob_base_url.rstrip("/")
FILLED_STATUSES = {"first_filled", "second_filled", "exited", "filled", "paired"}


@app.get("/api/paper")
def paper_account() -> Dict[str, Any]:
    """Paper bankroll, equity and traded notional from the bot's own ledger."""
    bankroll = float(botctl.load_risk().get("max_total_capital", 0.0))
    pnl = _read_json(PNL_SNAPSHOT_PATH) or {}
    session_pnl = float(pnl.get("session_pnl") or 0.0)
    volume = fees = 0.0
    fills = 0
    day_start = time.time() - (time.time() % 86400)
    volume_today = 0.0
    try:
        with ORDER_LEDGER_PATH.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("status") not in FILLED_STATUSES:
                    continue
                notional = float(row.get("price") or 0.0) * float(row.get("size") or 0.0)
                volume += notional
                fees += float(row.get("fee") or 0.0)
                fills += 1
                if float(row.get("timestamp") or 0.0) >= day_start:
                    volume_today += notional
    except OSError:
        pass
    return {
        "bankroll_usd": bankroll,
        "equity_usd": round(bankroll + session_pnl, 4),
        "session_pnl": session_pnl,
        "deployed_usd": pnl.get("deployed_usd"),
        "volume_usd": round(volume, 2),
        "volume_today_usd": round(volume_today, 2),
        "fees_usd": round(fees, 4),
        "fills": fills,
        "orders_sent": pnl.get("orders_sent"),
        "opportunities": pnl.get("opportunities"),
    }


LIVE_MARKETS_LIMIT = 4
_book_cache: Dict[str, Any] = {}


def _top_of_book(client: "httpx.Client", token_id: str) -> Dict[str, Optional[float]]:
    """Best bid/ask from the public CLOB book, cached ~2 s per token."""
    cached = _book_cache.get(token_id)
    if cached and time.time() - cached[0] < 2.0:
        return cached[1]
    top: Dict[str, Optional[float]] = {"bid": None, "ask": None}
    try:
        book = client.get(f"{CLOB_BASE_URL}/book", params={"token_id": token_id}).json()
        bids = [float(level["price"]) for level in book.get("bids") or []]
        asks = [float(level["price"]) for level in book.get("asks") or []]
        top = {"bid": max(bids) if bids else None, "ask": min(asks) if asks else None}
    except Exception:
        pass
    _book_cache[token_id] = (time.time(), top)
    return top


@app.get("/api/live_markets")
def live_markets() -> Dict[str, Any]:
    """Markets the bot is in or watching: time left and live odds per side."""
    universe = _read_json(UNIVERSE_SNAPSHOT_PATH) or {}
    strategy = _read_json(STRATEGY_SNAPSHOT_PATH) or {}
    open_ids = {str(seq.get("market_id")): seq for seq in strategy.get("open_sequences") or []}
    active = [m for m in universe.get("markets") or [] if m.get("lifecycle_state") == "active"]
    now = time.time()
    active = [m for m in active if not m.get("end_time") or m["end_time"] > now]
    active.sort(key=lambda m: (str(m.get("market_id")) not in open_ids, m.get("end_time") or float("inf")))
    rows = []
    with httpx.Client(timeout=4.0) as client:
        for market in active[:LIVE_MARKETS_LIMIT]:
            yes = _top_of_book(client, market["yes_token_id"]) if market.get("yes_token_id") else {}
            no = _top_of_book(client, market["no_token_id"]) if market.get("no_token_id") else {}
            seq = open_ids.get(str(market.get("market_id")))
            rows.append({
                "market_id": market.get("market_id"),
                "question": market.get("question"),
                "url": f"https://polymarket.com/event/{market['slug']}" if market.get("slug") else None,
                "outcome_yes": market.get("outcome_yes") or "Yes",
                "outcome_no": market.get("outcome_no") or "No",
                "seconds_left": round(market["end_time"] - now, 1) if market.get("end_time") else None,
                "yes": yes,
                "no": no,
                "in_trade": seq is not None,
                "position": {
                    "side": seq.get("first_side"),
                    "entry": seq.get("first_price"),
                    "size": seq.get("size"),
                    "phase": seq.get("phase"),
                } if seq else None,
            })
    return {"generated_at": now, "markets": rows, "tracked_active": len(active)}


# name, default, description, secret? -- secrets are reported only as set / not set
SETUP_SETTINGS = [
    ("DRY_RUN", "true", "Simulated fills on public books when no key is set.", False),
    ("BTC_ONLY", "false", "Track only the rotating BTC up/down windows.", False),
    ("MAX_CONCURRENT_MARKETS", "50", "How many markets the universe scan tracks at once.", False),
    ("ORDER_SIZE", "10", "Shares per leg.", False),
    ("MIN_EDGE", "0.01", "Minimum locked edge per pair after fees.", False),
    ("POLYMARKET_ADDRESS", "", "Public wallet address for balances and positions.", False),
    ("PRIVATE_KEY", "", "Signer key: unlocks paper (authenticated) and live.", True),
    ("PROXY_WALLET", "", "Your Polymarket profile wallet (email/Magic accounts).", False),
    ("POLYMARKET_SIGNATURE_TYPE", "eoa", "eoa, proxy (email accounts) or safe.", False),
    ("ARBITER_LIVE_TRADING", "disabled", "Set to enabled to allow live orders at all.", False),
    ("LIVE_MAX_ORDER_USD", "5", "Live cap per order.", False),
    ("LIVE_MAX_OPEN_USD", "25", "Live cap on open exposure.", False),
    ("LIVE_MAX_SESSION_LOSS_USD", "25", "Live session loss that stops new entries.", False),
    ("LIVE_BROADCAST_APPROVALS", "false", "Send on-chain approvals (costs gas).", False),
    ("KALSHI_API_KEY_ID", "", "Kalshi API key ID (read-only view).", True),
    ("KALSHI_PRIVATE_KEY_PATH", "", "Path to the Kalshi RSA key.", True),
    ("KALSHI_ENV", "prod", "prod or demo.", False),
]


@app.get("/api/setup")
def setup_info() -> Dict[str, Any]:
    """Which settings are in effect. Secret values are never returned."""
    import os
    rows = []
    for name, default, about, secret in SETUP_SETTINGS:
        raw = os.getenv(name, "")
        if secret:
            now = "set" if raw.strip() else "not set"
        else:
            now = raw.strip() or (f"{default} (default)" if default else "not set")
        rows.append({"name": name, "now": now, "default": default or "—", "about": about, "secret": secret})
    return {"settings": rows}


class VenueLinkRequest(BaseModel):
    address: Optional[str] = None
    key_id: Optional[str] = None
    private_key: Optional[str] = None
    environment: Optional[str] = None


@app.get("/api/venues")
def venues_snapshot(refresh: bool = False) -> Dict[str, Any]:
    """Linked Polymarket / Kalshi accounts. Read-only: balances, positions, P&L."""
    return venues.snapshot(force=refresh)


@app.post("/api/venues/{venue}/link")
def venue_link(venue: str, req: VenueLinkRequest) -> Dict[str, Any]:
    try:
        return venues.link(venue, req.model_dump(exclude_none=True))
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown venue: {venue}")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


class VenueModeRequest(BaseModel):
    mode: str
    confirm: str = ""


@app.post("/api/venues/{venue}/mode")
def venue_mode(venue: str, req: VenueModeRequest) -> Dict[str, Any]:
    try:
        return venues.change_mode(venue, req.mode, req.confirm)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown venue: {venue}")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/venues/{venue}/unlink")
def venue_unlink(venue: str) -> Dict[str, Any]:
    try:
        return venues.unlink(venue)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown venue: {venue}")


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
    # no-cache so a redeployed dashboard is picked up without a hard refresh.
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
