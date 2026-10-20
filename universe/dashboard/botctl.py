"""Bot process control for the dashboard.

Manages exactly ONE `python -m polyuni.main` process at a time:

  * ``start`` spawns the bot with a validated risk-profile env override,
    captures its output to ``state/bot_control_session.log``;
  * ``stop`` writes SETTINGS.emergency_stop_file (the SAME path
    RiskState.emergency_triggered() checks -- there is exactly one emergency-
    stop path in this codebase, never a second hardcoded one) and then
    SIGTERM/SIGKILLs the managed process, non-blocking;
  * ``status`` reports a real STOPPED/STARTING/RUNNING/STOPPING state derived
    from process liveness + state/READY (see main.py's readiness watcher),
    not just "process alive" -- polymarket_arb's dashboard reported "running"
    the instant the subprocess spawned, before the feed/WS were actually
    watching anything;
  * paper only: this controller can only ever launch polyuni.main, which has
    no real exchange order-submission code path anywhere in the codebase.
"""
from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
from polyuni.config import SETTINGS  # noqa: E402

RISK_PATH = PROJECT_ROOT / "state" / "bot_control_risk.json"
SESSION_LOG_PATH = PROJECT_ROOT / "state" / "bot_control_session.log"
EMERGENCY_STOP_PATH = PROJECT_ROOT / SETTINGS.emergency_stop_file
READY_PATH = PROJECT_ROOT / "state" / "READY"
HEARTBEAT_PATH = PROJECT_ROOT / SETTINGS.alert_heartbeat_file
PNL_SNAPSHOT_PATH = PROJECT_ROOT / "state" / "pnl_snapshot.json"
UNIVERSE_SNAPSHOT_PATH = PROJECT_ROOT / "state" / "universe_snapshot.json"
PID_PATH = PROJECT_ROOT / "state" / "bot_control_pid.json"

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
    "min_edge": 0.01,
    "max_position_per_market": 200.0,
    "max_total_capital": 1000.0,
    "daily_kill_switch_usd": 500.0,
    "no_entry_seconds": 5.0,
    "max_quote_age_ms": 500.0,
}

_STATE: Dict[str, Any] = {"proc": None, "started_at": None}
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


def _external_bots() -> list[int]:
    """Return only real ``python -m polyuni.main`` processes.

    ``pgrep -f polyuni.main`` also matched shell commands, diagnostics, and test
    runners whose argument text happened to contain that string.  Those brief
    false positives made the dashboard reject valid start requests.  Parse the
    process table and require the exact ``-m polyuni.main`` argv pair instead.
    """
    try:
        out = subprocess.run(
            ["ps", "-axo", "pid=,command="],
            capture_output=True, text=True, timeout=5, check=False,
        ).stdout
    except Exception:
        return []
    found: list[int] = []
    for line in out.splitlines():
        fields = line.strip().split(maxsplit=1)
        if len(fields) != 2 or not fields[0].isdigit():
            continue
        try:
            argv = shlex.split(fields[1])
        except ValueError:
            continue
        if any(
            argv[index] == "-m" and argv[index + 1] == "polyuni.main"
            for index in range(len(argv) - 1)
        ):
            found.append(int(fields[0]))
    return found


def _pid_is_live_bot(pid: int) -> bool:
    if pid <= 0 or pid == os.getpid():
        return False
    return pid in _external_bots()


def _read_pid_record() -> Optional[dict[str, Any]]:
    try:
        record = json.loads(PID_PATH.read_text(encoding="utf-8"))
        pid = int(record["pid"])
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None
    if _pid_is_live_bot(pid):
        return {"pid": pid, "started_at": record.get("started_at")}
    PID_PATH.unlink(missing_ok=True)
    return None


def _write_pid_record(pid: int, started_at: Optional[float]) -> None:
    PID_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"pid": pid, "started_at": started_at, "module": "polyuni.main"}
    PID_PATH.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")


def _recover_bot_record() -> Optional[dict[str, Any]]:
    """Recover ownership across dashboard restarts or multiple dashboard ports."""
    record = _read_pid_record()
    if record is not None:
        return record
    candidates = [pid for pid in _external_bots() if pid != os.getpid()]
    if len(candidates) != 1:
        return None
    record = {"pid": candidates[0], "started_at": None}
    _write_pid_record(record["pid"], record["started_at"])
    return record


def managed_proc() -> Optional[subprocess.Popen]:
    with _STATE_LOCK:
        return _STATE["proc"]


def emergency_stop_exists() -> bool:
    return EMERGENCY_STOP_PATH.exists()


def clear_emergency_stop() -> None:
    EMERGENCY_STOP_PATH.unlink(missing_ok=True)


def start_bot(risk: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    with _STATE_LOCK:
        if _STATE["proc"] is not None and _STATE["proc"].poll() is None:
            return {"error": "managed bot already running", "pid": _STATE["proc"].pid}
        recovered = _recover_bot_record()
        if recovered is not None:
            return {
                "error": f"bot already running (pid={recovered['pid']}); dashboard attached to it",
                "pid": recovered["pid"],
            }
        external = [pid for pid in _external_bots() if pid != os.getpid()]
        if external:
            return {"error": f"multiple bot processes detected (pids={external}); stop duplicates first", "pids": external}
        profile = load_risk()
        if risk:
            saved, err = save_risk(risk)
            if err:
                return {"error": err}
            profile = saved
        # A leftover emergency-stop file would block every reserve in the new
        # run (RiskState.emergency_triggered()). A fresh start clears it.
        clear_emergency_stop()
        READY_PATH.unlink(missing_ok=True)
        env = dict(os.environ)
        env.update({
            "PAPER_TRADE": "true",
            "DRY_RUN": "false",
            "LIVE_SIGNING_VERIFIED": "true",
            "ORDER_SIZE": str(profile["order_size"]),
            "MIN_EDGE": str(profile["min_edge"]),
            "MAX_POSITION_PER_MARKET": str(profile["max_position_per_market"]),
            "MAX_TOTAL_CAPITAL": str(profile["max_total_capital"]),
            "DAILY_KILL_SWITCH_USD": str(profile["daily_kill_switch_usd"]),
            "NO_ENTRY_SECONDS": str(profile["no_entry_seconds"]),
            "MAX_QUOTE_AGE_MS": str(int(profile["max_quote_age_ms"])),
        })
        SESSION_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        log_handle = SESSION_LOG_PATH.open("a", buffering=1, encoding="utf-8")
        log_handle.write(
            f"--- dashboard bot control session start "
            f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} risk={json.dumps(profile)} ---\n"
        )
        python_bin = PROJECT_ROOT / ".venv" / "bin" / "python"
        proc = subprocess.Popen(
            [str(python_bin) if python_bin.exists() else sys.executable, "-m", "polyuni.main"],
            cwd=str(PROJECT_ROOT), env=env, stdout=log_handle, stderr=subprocess.STDOUT,
        )
        started_at = time.time()
        _STATE.update({"proc": proc, "started_at": started_at})
        _write_pid_record(proc.pid, started_at)
        return {
            "started": True, "pid": proc.pid, "risk": profile,
            "note": "PAPER TRADE ONLY — no real orders can be placed by this process",
        }


def _terminate_in_background(proc: subprocess.Popen) -> None:
    """Terminate without blocking the dashboard request -- runs on a daemon
    thread so the HTTP response returns immediately; status polling observes
    proc.poll() and reports STOPPED the moment it exits."""

    def _kill() -> None:
        try:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        except Exception:
            pass

    threading.Thread(target=_kill, daemon=True).start()


def _terminate_pid_in_background(pid: int) -> None:
    """Safely stop a recovered bot after re-verifying its exact command line."""
    def _kill() -> None:
        if not _pid_is_live_bot(pid):
            return
        try:
            os.kill(pid, signal.SIGTERM)
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline and _pid_is_live_bot(pid):
                time.sleep(0.1)
            if _pid_is_live_bot(pid):
                os.kill(pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
        finally:
            PID_PATH.unlink(missing_ok=True)

    threading.Thread(target=_kill, daemon=True).start()


def stop_bot() -> Dict[str, Any]:
    EMERGENCY_STOP_PATH.parent.mkdir(parents=True, exist_ok=True)
    EMERGENCY_STOP_PATH.write_text(
        f"emergency stop written by dashboard bot control\n"
        f"at: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n",
        encoding="utf-8",
    )
    with _STATE_LOCK:
        proc = _STATE["proc"]
        if proc is None:
            recovered = _recover_bot_record()
            if recovered is None:
                return {"stopped": False, "note": "no bot running", "emergency_stop_written": True}
            pid = int(recovered["pid"])
            _terminate_pid_in_background(pid)
            return {
                "stopped": True, "pid": pid, "recovered": True,
                "emergency_stop_written": True,
                "note": "recovered bot stop requested — terminating in the background",
            }
        if proc.poll() is None:
            _terminate_in_background(proc)
            return {
                "stopped": True, "pid": proc.pid, "emergency_stop_written": True,
                "note": "stop requested — terminating in the background",
            }
        return {"stopped": True, "exit_code": proc.poll(), "emergency_stop_written": True}


def _read_json(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def status() -> Dict[str, Any]:
    with _STATE_LOCK:
        proc = _STATE["proc"]
        started_at = _STATE["started_at"]
    recovered = None if proc is not None and proc.poll() is None else _recover_bot_record()
    managed_pid = proc.pid if proc is not None and proc.poll() is None else (
        int(recovered["pid"]) if recovered is not None else None
    )
    if started_at is None and recovered is not None:
        recovered_started_at = recovered.get("started_at")
        started_at = float(recovered_started_at) if recovered_started_at is not None else None
    proc_alive = proc is not None and proc.poll() is None
    recovered_alive = recovered is not None and _pid_is_live_bot(int(recovered["pid"]))
    bot_alive = proc_alive or recovered_alive
    emergency_stop = emergency_stop_exists()
    ready = READY_PATH.exists()

    if not bot_alive:
        run_state = "STOPPED"
    elif emergency_stop:
        run_state = "STOPPING"
    elif ready:
        run_state = "RUNNING"
    else:
        run_state = "STARTING"

    heartbeat_age_s = None
    try:
        heartbeat_age_s = round(time.time() - HEARTBEAT_PATH.stat().st_mtime, 1)
    except OSError:
        pass

    pnl = _read_json(PNL_SNAPSHOT_PATH) or {}
    universe = _read_json(UNIVERSE_SNAPSHOT_PATH) or {}

    return {
        "run_state": run_state,
        "pid": managed_pid,
        "exit_code": proc.poll() if proc is not None else None,
        "uptime_s": round(time.time() - started_at, 1) if started_at else None,
        "started_at_utc": (
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started_at)) if started_at else None
        ),
        "heartbeat_age_s": heartbeat_age_s,
        "emergency_stop": emergency_stop,
        "ownership": "managed" if proc_alive else ("recovered" if recovered_alive else "none"),
        "external_bots": [pid for pid in _external_bots() if pid != managed_pid],
        "risk": load_risk(),
        "paper_only": True,
        "real_orders_possible": False,
        "pnl": {
            "session_pnl": pnl.get("session_pnl"),
            "locked_pnl": pnl.get("locked_pnl"),
            "residual_pnl": pnl.get("residual_pnl"),
            "would_lock_pnl": pnl.get("would_lock_pnl"),
            "deployed_usd": pnl.get("deployed_usd"),
            "generated_at": pnl.get("generated_at"),
        },
        "universe": {
            "total_tracked": universe.get("total_tracked"),
            "state_counts": universe.get("state_counts"),
            "active_count": (universe.get("state_counts") or {}).get("active"),
            "generated_at": universe.get("generated_at"),
        },
    }


def log_tail(lines: int = 100) -> Dict[str, Any]:
    try:
        text = SESSION_LOG_PATH.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    return {"lines": text.splitlines()[-lines:], "path": str(SESSION_LOG_PATH)}
