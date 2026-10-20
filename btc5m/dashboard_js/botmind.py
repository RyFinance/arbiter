"""Live narrative engine — what the bot is looking at, doing, and whether it's getting through.

Consumes the bot's real session log (managed session archive when one is
running, otherwise the newest sealed archive, otherwise state/bot.jsonl),
the alert log, and the bot-control state, and renders an honest, tagged
narrative: current focus (market window + book state), a live thought
stream converted from real events, the opportunity pipeline funnel with
real counts, and a plain-language verdict.  No fabricated thoughts: every
line is derived from a real log/alert/metric row.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from dashboard_js import botctl, readers

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = PROJECT_ROOT / "state"
ARCHIVES_DIR = STATE_DIR / "paper_session_archives"
ALERTS_PATH = STATE_DIR / "alerts.jsonl"
REGIME_STATE_PATH = STATE_DIR / "regime_active.json"

WINDOW_RE = re.compile(r"WINDOW ACTIVE (btc-updown-5m-\d+) close=(\d+)")
COALESCED_RE = re.compile(r"CROSS_LEG_UPDATE_COALESCED market=([\w-]+) legs=\d+ combined=([\d.]+)")
INCOMPLETE_RE = re.compile(
    r"CROSS_LEG_UPDATE_INCOMPLETE market=([\w-]+) legs=(\d) settle_ms=[\d.]+ "
    r"combined=([\d.]+) evaluated_with_last_known_leg=True"
)
EDGE_RE = re.compile(r"([\w-]+) YES ([\d.]+) NO ([\d.]+) SUM ([\d.]+) EDGE ([\d.]+) age_ms=([\d.]+) band=(\w+)")
FIRE_RE = re.compile(r"FIRE market=([\w-]+) latency_ms=([\d.]+)")
WS_RECOVER_RE = re.compile(r"WebSocket recovered after ([\d.]+)s")
MACRO_RE = re.compile(r"macro events: (\d+) total, (\d+) high-risk windows")


def _current_log_path() -> tuple[Optional[Path], str]:
    """Pick the log to narrate: managed archive > newest archive > live bot.jsonl."""
    managed = botctl.status()
    session_id = managed.get("session_id")
    if session_id:
        path = ARCHIVES_DIR / session_id / "bot.jsonl"
        if path.is_file():
            return path, f"managed session {session_id}"
    try:
        archives = sorted(ARCHIVES_DIR.glob("*/bot.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        archives = []
    if archives:
        return archives[0], "newest sealed session " + archives[0].parent.name
    live = STATE_DIR / "bot.jsonl"
    if live.is_file():
        return live, "live bot.jsonl"
    return None, "no session data yet"


def _rows(path: Path, limit: int = 4000) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rows.append(row)
    except OSError:
        pass
    return rows[-limit:]


def _tagged_thought(row: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """Convert one real log row into a tagged thought line (or None)."""
    msg = str(row.get("msg", ""))
    data = row.get("data") or {}
    event = row.get("event")
    ts = str(row.get("t", ""))[:8]

    if event == "TRADE" and msg == "missed":
        return {
            "tag": "MISS",
            "text": f"{ts} missed: {data.get('reason', '?')} market={str(data.get('market_id', ''))[-16:]} combined={data.get('combined', '?')}",
        }
    if event == "TRADE" and msg == "filled":
        pnl = data.get("pnl_locked")
        return {
            "tag": "FILL",
            "text": f"{ts} CLEAN FILL market={str(data.get('market_id', ''))[-16:]} yes={data.get('fill_yes_price')} no={data.get('fill_no_price')} pnl={float(pnl):+.4f}" if pnl is not None else f"{ts} clean fill market={str(data.get('market_id', ''))[-16:]}",
        }
    if event == "TRADE" and msg == "partial_resolved":
        pnl = data.get("realized_pnl", data.get("residual_pnl"))
        action = data.get("action", "exit")
        return {
            "tag": "EXIT",
            "text": f"{ts} partial {action} market={str(data.get('market_id', ''))[-16:]} pnl={float(pnl):+.4f}" if pnl is not None else f"{ts} partial {action} market={str(data.get('market_id', ''))[-16:]}",
        }

    edge = EDGE_RE.search(msg)
    if edge and event == "INFO":
        return {
            "tag": "EDGE",
            "text": f"{ts} EDGE {edge.group(1)[-16:]} YES {edge.group(2)} NO {edge.group(3)} sum {edge.group(4)} edge {edge.group(5)} age {edge.group(6)}ms",
        }
    fire = FIRE_RE.search(msg)
    if fire and event == "INFO":
        return {"tag": "FIRE", "text": f"{ts} FIRING market={fire.group(1)[-16:]} latency {fire.group(2)}ms"}
    coalesced = COALESCED_RE.search(msg)
    if coalesced and event == "INFO":
        return {"tag": "WATCH", "text": f"{ts} watching {coalesced.group(1)[-16:]} — combined {coalesced.group(2)} (no edge)"}
    incomplete = INCOMPLETE_RE.search(msg)
    if incomplete and event == "INFO":
        return {"tag": "WATCH", "text": f"{ts} single-leg update on {incomplete.group(1)[-16:]} (legs={incomplete.group(2)}) — combined {incomplete.group(3)} (evaluated)"}
    recovered = WS_RECOVER_RE.search(msg)
    if recovered and event == "INFO":
        return {"tag": "NET", "text": f"{ts} websocket recovered after {recovered.group(1)}s"}
    macro = MACRO_RE.search(msg)
    if macro and event == "INFO":
        return {"tag": "MACRO", "text": f"{ts} macro gate: {macro.group(1)} events, {macro.group(2)} high-risk windows"}
    if msg.startswith("PARTIAL FILL"):
        return {"tag": "PARTIAL", "text": f"{ts} {msg[:120]}"}
    if msg.startswith("RISK HALT"):
        return {"tag": "HALT", "text": f"{ts} {msg[:120]}"}
    if msg.startswith("EXIT FAILED"):
        return {"tag": "EXITFAIL", "text": f"{ts} {msg[:120]}"}
    return None


def focus(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """What the bot is looking at: last window, last book combined, macro gate."""
    window: Optional[str] = None
    close: Optional[str] = None
    combined: Optional[float] = None
    macro_total: Optional[int] = None
    macro_high: Optional[int] = None
    for row in reversed(rows):
        msg = str(row.get("msg", ""))
        window_match = WINDOW_RE.search(msg)
        if window_match and window is None:
            window = window_match.group(1)
            close = window_match.group(2)
        combined_match = COALESCED_RE.search(msg) or INCOMPLETE_RE.search(msg)
        if combined_match and combined is None:
            combined = float(combined_match.group(2) if combined_match.re is COALESCED_RE else combined_match.group(3))
        macro_match = MACRO_RE.search(msg)
        if macro_match and macro_total is None:
            macro_total = int(macro_match.group(1))
            macro_high = int(macro_match.group(2))
    return {
        "window": window,
        "close_epoch": int(close) if close else None,
        "combined": combined,
        "macro_total": macro_total,
        "macro_high": macro_high,
    }


def funnel(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Opportunity pipeline from the last 'session metrics:' line."""
    metrics: List[Dict[str, Any]] = []
    for row in reversed(rows):
        if "session metrics:" in str(row.get("msg", "")):
            parsed = readers.parse_session_metrics(str(row["msg"]))
            if parsed:
                metrics = [parsed]
                break
    if not metrics:
        try:
            metrics = readers.extract_session_metrics_rows(STATE_DIR / "bot.jsonl")
        except Exception:  # noqa: BLE001
            metrics = []
    if not metrics:
        return {"available": False}
    m = metrics[-1]
    opps = int(m.get("opps", 0) or 0)
    sent = int(m.get("sent", 0) or 0)
    fills = int(m.get("fills", 0) or 0)
    partial = int(m.get("partial", 0) or 0)
    resolved = int(m.get("resolved", 0) or 0)
    missed = int(m.get("missed", 0) or 0)
    fires = sent // 2
    return {
        "available": True,
        "opps": opps,
        "fires": fires,
        "sent": sent,
        "fills": fills,
        "partial": partial,
        "resolved": resolved,
        "missed": missed,
        "fire_rate_pct": round(100.0 * fires / opps, 2) if opps else None,
        "clean_rate_pct": round(100.0 * fills / opps, 2) if opps else None,
        "partial_rate_pct": round(100.0 * partial / opps, 2) if opps else None,
        "session_pnl": m.get("session_pnl"),
        "locked_pnl": m.get("locked_pnl"),
        "residual_pnl": m.get("residual_pnl"),
        "ts_utc": m.get("ts_utc"),
    }


def verdict(managed: Dict[str, Any], funnel_data: Dict[str, Any], focus_data: Dict[str, Any],
            kill_state: str, emergency: bool) -> Dict[str, str]:
    """Plain-language: is the bot running, what is it doing, is it getting through."""
    if managed.get("managed_running"):
        if managed.get("emergency_stop"):
            return {"level": "halt", "text": "EMERGENCY STOP FILE ACTIVE — the bot is refusing every new position until it is cleared"}
        if kill_state == "halted":
            return {"level": "halt", "text": "KILL SWITCH HALTED — no further trades will be sent"}
        if emergency:
            return {"level": "halt", "text": "EMERGENCY STOP ACTIVE — reserves are blocked"}
        window = focus_data.get("window")
        combined = focus_data.get("combined")
        if window:
            combined_note = f" — current book combined {combined}" if combined is not None else ""
            risk = managed.get("risk") or {}
            min_edge = float(risk.get("min_edge", 0.03))
            return {"level": "ok", "text": f"RUNNING, watching {window}{combined_note}. Looking for YES+NO < {1.0 - min_edge:.2f}."}
        return {"level": "ok", "text": "RUNNING — connected and watching the book for edge"}
    if managed.get("external_paper_bots"):
        return {"level": "warn", "text": f"an external paper bot is running (pids={managed.get('external_paper_bots')}) — not managed from this dashboard"}
    if managed.get("emergency_stop"):
        return {"level": "halt", "text": "STOPPED — emergency-stop file active; a fresh start clears it"}
    if kill_state == "halted":
        return {"level": "halt", "text": "last session ended HALTED (kill switch) — see alerts wire"}
    f = funnel_data
    if f.get("available") and f["opps"]:
        if f["fills"] == 0 and f["fires"] and f["fires"] > 0:
            return {
                "level": "fail",
                "text": f"STOPPED. Last session: {f['opps']} opportunities, {f['fires']} fired, 0 clean fills — the edge decays within one tick on this market (see depth_replay_fill_rate_investigation).",
            }
        return {
            "level": "warn",
            "text": f"STOPPED. Last session: {f['opps']} opps, {f['fires']} fired, {f['fills']} clean fills ({f['clean_rate_pct']}%), {f['partial']} partials, pnl {f['session_pnl']}. Start the bot in panel 14.",
        }
    return {"level": "idle", "text": "STOPPED — no session data yet. Start the bot from panel 14."}


def thoughts(rows: List[Dict[str, Any]], limit: int = 30) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    seen: set = set()
    for row in reversed(rows):
        thought = _tagged_thought(row)
        if thought is None:
            continue
        key = thought["text"]
        if key in seen:
            continue
        seen.add(key)
        out.append(thought)
        if len(out) >= limit:
            break
    return out


def active_regime() -> Optional[Dict[str, Any]]:
    """The Kronos/ATR-heuristic regime applied at the current session's startup.

    Was previously only visible by grepping the raw log for 'Applied regime'
    — this reads the small state file kronos_session.py writes alongside it
    so the dashboard can show what's actually gating entry timing right now.
    """
    try:
        return json.loads(REGIME_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def mind() -> Dict[str, Any]:
    managed = botctl.status()
    path, source = _current_log_path()
    rows = _rows(path) if path else []
    # Kill-switch state must be scoped to the CURRENT session — alerts.jsonl
    # accumulates alerts from every past session.
    session_id = managed.get("session_id")
    if not session_id:
        try:
            archives = sorted(ARCHIVES_DIR.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            archives = []
        session_id = archives[0].name if archives else None
    kill_state = "unknown"
    try:
        alerts = readers.read_alerts_rows(PROJECT_ROOT)
        for alert in reversed(alerts):
            if alert.get("kind") == "kill_switch" and (
                not alert.get("session_id") or alert.get("session_id") == session_id
            ):
                kill_state = "halted"
                break
    except Exception:  # noqa: BLE001
        pass
    focus_data = focus(rows)
    funnel_data = funnel(rows)
    verdict_data = verdict(managed, funnel_data, focus_data, kill_state, managed.get("emergency_stop", False))
    return {
        "updated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": source,
        "managed": {k: managed.get(k) for k in ("managed_running", "pid", "session_id", "fill_source", "duration_minutes", "uptime_s", "emergency_stop", "external_paper_bots")},
        "kill_switch": kill_state,
        "risk": managed.get("risk", {}),
        "regime": active_regime(),
        "focus": focus_data,
        "funnel": funnel_data,
        "verdict": verdict_data,
        "thoughts": thoughts(rows),
        "paper_only": True,
    }
