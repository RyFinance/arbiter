"""Paper-only bot process control for the dashboard.

Manages exactly ONE paper-trading session at a time, end to end:

  * ``start`` spawns ``scripts/run_task_2_4_paper_session.py`` (paper-only
    by construction: it never submits an exchange order) with the saved
    risk profile exported as env overrides, and captures its output to
    ``state/bot_control_session.log``;
  * ``stop`` writes the emergency-stop file (``state/EMERGENCY_STOP``, the
    same file ``RiskState.emergency_triggered()`` checks) and then
    SIGTERM/SIGKILLs the managed process;
  * ``status`` reports pid/aliveness/uptime/session-id plus the latest
    session metrics and log tail;
  * the risk profile lives in ``state/bot_control_risk.json`` with
    validated ranges.

Hard rules: this controller can only ever launch the paper session runner;
it never touches the live control flag, never bypasses geoblocking, and
refuses to start while an unmanaged paper bot is already running.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RISK_PATH = PROJECT_ROOT / "state" / "bot_control_risk.json"
SESSION_LOG_PATH = PROJECT_ROOT / "state" / "bot_control_session.log"
EMERGENCY_STOP_PATH = PROJECT_ROOT / "state" / "EMERGENCY_STOP"
RUNNER_SCRIPT = "scripts/run_task_2_4_paper_session.py"

RISK_LIMITS = {
    "order_size": (1.0, 100.0),
    "min_edge": (0.001, 0.100),
    "max_position_per_market": (10.0, 1000.0),
    "max_total_capital": (10.0, 5000.0),
    "daily_kill_switch_usd": (1.0, 5000.0),
    "no_entry_seconds": (0.0, 30.0),
    "max_quote_age_ms": (100.0, 5000.0),
}

DEFAULT_RISK: Dict[str, float] = {
    "order_size": 10.0,
    "min_edge": 0.03,
    "max_position_per_market": 200.0,
    "max_total_capital": 1000.0,
    "daily_kill_switch_usd": 500.0,
    "no_entry_seconds": 5.0,
    "max_quote_age_ms": 500.0,
}

_STATE: Dict[str, Any] = {"proc": None, "started_at": None, "duration": None, "fill_source": None}
_STATE_LOCK = threading.Lock()


def load_risk() -> Dict[str, float]:
    try:
        raw = json.loads(RISK_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return dict(DEFAULT_RISK)
    profile = dict(DEFAULT_RISK)
    for key in DEFAULT_RISK:
        value = raw.get(key)
        if isinstance(value, (int, float)):
            profile[key] = float(value)
    return profile


def save_risk(profile: Dict[str, Any]) -> tuple[Dict[str, float], Optional[str]]:
    cleaned: Dict[str, float] = {}
    for key, default in DEFAULT_RISK.items():
        value = profile.get(key, default)
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return load_risk(), f"{key} must be numeric"
        low, high = RISK_LIMITS[key]
        if not low <= numeric <= high:
            return load_risk(), f"{key}={numeric} outside allowed range [{low}, {high}]"
        cleaned[key] = numeric
    RISK_PATH.parent.mkdir(parents=True, exist_ok=True)
    RISK_PATH.write_text(json.dumps(cleaned, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return cleaned, None


def _external_paper_bots() -> list[int]:
    try:
        out = subprocess.run(
            ["pgrep", "-f", "run_task_2_4_paper_session"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        return [int(p) for p in out.split() if p.isdigit()]
    except Exception:
        return []


def managed_proc() -> Optional[subprocess.Popen]:
    with _STATE_LOCK:
        return _STATE["proc"]


def emergency_stop_exists() -> bool:
    return EMERGENCY_STOP_PATH.exists()


def write_emergency_stop(reason: str) -> None:
    EMERGENCY_STOP_PATH.write_text(
        f"emergency stop written by dashboard bot control\nreason: {reason}\n"
        f"at: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n",
        encoding="utf-8",
    )


def clear_emergency_stop() -> None:
    EMERGENCY_STOP_PATH.unlink(missing_ok=True)


def start_bot(duration_minutes: float, fill_source: str, risk: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    with _STATE_LOCK:
        if _STATE["proc"] is not None and _STATE["proc"].poll() is None:
            return {"error": "managed paper bot already running", "pid": _STATE["proc"].pid}
        external = _external_paper_bots()
        if external:
            return {"error": f"unmanaged paper bot already running (pids={external}); stop it first", "pids": external}
        if duration_minutes < 15:
            return {"error": "duration_minutes must be >= 15"}
        if fill_source not in ("rest_book", "decision_snapshot"):
            return {"error": "fill_source must be rest_book or decision_snapshot"}
        profile = load_risk()
        if risk:
            saved, err = save_risk(risk)
            if err:
                return {"error": err}
            profile = saved
        # A leftover emergency-stop file would block every reserve in the new
        # session (risk.emergency_triggered). A fresh start clears it.
        clear_emergency_stop()
        env = dict(os.environ)
        env.update(
            {
                "PAPER_TRADE": "true",
                "DRY_RUN": "false",
                "LIVE_SIGNING_VERIFIED": "true",
                "PAPER_FILL_SOURCE": fill_source,
                "ORDER_SIZE": str(profile["order_size"]),
                "MIN_EDGE": str(profile["min_edge"]),
                "MAX_POSITION_PER_MARKET": str(profile["max_position_per_market"]),
                "MAX_TOTAL_CAPITAL": str(profile["max_total_capital"]),
                "DAILY_KILL_SWITCH_USD": str(profile["daily_kill_switch_usd"]),
                "NO_ENTRY_SECONDS": str(profile["no_entry_seconds"]),
                "MAX_QUOTE_AGE_MS": str(int(profile["max_quote_age_ms"])),
            }
        )
        log_handle = SESSION_LOG_PATH.open("a", buffering=1, encoding="utf-8")
        log_handle.write(
            f"--- dashboard bot control session start {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} "
            f"duration={duration_minutes}m fill_source={fill_source} risk={json.dumps(profile)} ---\n"
        )
        proc = subprocess.Popen(
            [
                str(PROJECT_ROOT / ".venv" / "bin" / "python"),
                RUNNER_SCRIPT,
                "--duration-minutes", str(duration_minutes),
                "--paper-fill-source", fill_source,
            ],
            cwd=str(PROJECT_ROOT),
            env=env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
        )
        _STATE.update({"proc": proc, "started_at": time.time(), "duration": duration_minutes, "fill_source": fill_source})
        return {
            "started": True,
            "pid": proc.pid,
            "duration_minutes": duration_minutes,
            "fill_source": fill_source,
            "risk": profile,
            "note": "PAPER TRADE ONLY — no real orders can be placed by this runner",
        }


def _terminate_in_background(proc: subprocess.Popen) -> None:
    """Terminate without blocking the dashboard request.

    Stop commands used to hold the HTTP response open until the process
    exited (up to ~15s of kill grace), which made the panel feel laggy.
    The actual termination runs on a daemon thread; the status poll
    observes `proc.poll()` and reports STOPPED the moment it exits.
    """

    def _kill() -> None:
        try:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=_kill, daemon=True).start()


def stop_bot() -> Dict[str, Any]:
    write_emergency_stop("dashboard stop command")
    with _STATE_LOCK:
        proc = _STATE["proc"]
        if proc is None:
            return {"stopped": False, "note": "no managed bot running", "emergency_stop_written": True}
        if proc.poll() is None:
            _terminate_in_background(proc)
            return {
                "stopped": True,
                "pid": proc.pid,
                "emergency_stop_written": True,
                "note": "stop requested — terminating in the background",
            }
        return {"stopped": True, "exit_code": proc.poll(), "emergency_stop_written": True}


def _session_id_for(started_at: Optional[float]) -> Optional[str]:
    if not started_at:
        return None
    archives = PROJECT_ROOT / "state" / "paper_session_archives"
    try:
        candidates = [p for p in archives.iterdir() if p.is_dir() and p.stat().st_mtime >= started_at - 5]
    except OSError:
        return None
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime).name


def _session_log_path(session_id: Optional[str]) -> Optional[Path]:
    if not session_id:
        return None
    return PROJECT_ROOT / "state" / "paper_session_archives" / session_id / "bot.jsonl"


def status() -> Dict[str, Any]:
    from dashboard_js import readers  # lazy — readers are cheap

    with _STATE_LOCK:
        proc = _STATE["proc"]
        started_at = _STATE["started_at"]
        duration = _STATE["duration"]
        fill_source = _STATE["fill_source"]
    managed_pid = proc.pid if proc is not None else None
    external = [pid for pid in _external_paper_bots() if pid != managed_pid]
    session_id = _session_id_for(started_at) if started_at else None
    resp: Dict[str, Any] = {
        "managed_running": False,
        "pid": managed_pid,
        "exit_code": proc.poll() if proc is not None else None,
        "uptime_s": round(time.time() - started_at, 1) if started_at else None,
        "started_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started_at)) if started_at else None,
        "duration_minutes": duration,
        "fill_source": fill_source,
        "session_id": session_id,
        "risk": load_risk(),
        "emergency_stop": emergency_stop_exists(),
        "external_paper_bots": external,
        "metrics": {},
        "paper_only": True,
        "real_orders_possible": False,
    }
    if proc is not None:
        resp["managed_running"] = proc.poll() is None
    # Mid-run, state/bot.jsonl still holds the PREVIOUS session (the runner
    # writes the live log into the session archive and publishes it at the
    # end), so prefer the managed session's own archive when known.
    metrics_source = _session_log_path(session_id) if session_id else None
    if metrics_source is None or not metrics_source.is_file():
        metrics_source = PROJECT_ROOT / "state" / "bot.jsonl"
    resp["metrics_source"] = str(metrics_source)
    try:
        metrics_rows = readers.extract_session_metrics_rows(metrics_source)
        if metrics_rows:
            resp["metrics"] = {
                "loops": metrics_rows[-1].get("loops"),
                "opps": metrics_rows[-1].get("opps"),
                "sent": metrics_rows[-1].get("sent"),
                "fills": metrics_rows[-1].get("fills"),
                "partial": metrics_rows[-1].get("partial"),
                "resolved": metrics_rows[-1].get("resolved"),
                "missed": metrics_rows[-1].get("missed"),
                "session_pnl": metrics_rows[-1].get("session_pnl"),
                "locked_pnl": metrics_rows[-1].get("locked_pnl"),
                "residual_pnl": metrics_rows[-1].get("residual_pnl"),
                "ts_utc": metrics_rows[-1].get("ts_utc"),
            }
    except Exception:  # noqa: BLE001
        pass
    return resp


def log_tail(lines: int = 60) -> Dict[str, Any]:
    try:
        text = SESSION_LOG_PATH.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    control_lines = text.splitlines()
    session_lines: list[str] = []
    session_id = _session_id_for(_STATE["started_at"]) if _STATE["started_at"] else None
    source = _session_log_path(session_id)
    if source is not None and source.is_file():
        try:
            session_lines = source.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            session_lines = []
        header = f"--- live session log ({session_id}/bot.jsonl) ---"
    else:
        header = "--- live bot.jsonl (no managed session yet) ---"
        try:
            session_lines = (PROJECT_ROOT / "state" / "bot.jsonl").read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()
        except OSError:
            session_lines = []
    combined = control_lines + [header] + session_lines
    return {"lines": combined[-lines:], "path": str(SESSION_LOG_PATH)}
