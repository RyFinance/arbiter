"""Isolated paper loop-bot control for the dashboard.

Manages the CLOSED-LOOP paper test bot (``scripts/run_loop_bot.py``) which
watches ALL updown timeframes (5m/15m/30m) simultaneously with its own
isolated PnL, events, and decision chart data — completely separate from the
main paper session controller (``botctl.py``):

  * its own config (``state/loop_bot/config.json``), stop file
    (``state/loop_bot/STOP``), events, and state — it never touches the main
    bot's files and the main bot's emergency-stop never touches it;
  * the runner is paper-only by construction (read-only public book polling,
    no signing, no trading client).

Hard rules: one loop bot at a time, paper only.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = PROJECT_ROOT / "state" / "loop_bot"
CONFIG_PATH = STATE_DIR / "config.json"
STOP_PATH = STATE_DIR / "STOP"
EVENTS_PATH = STATE_DIR / "events.jsonl"
STATE_PATH = STATE_DIR / "state.json"
LOG_PATH = STATE_DIR / "bot.log"
RUNNER_SCRIPT = "scripts/run_loop_bot.py"
FINNHUB_KEY_ENV = "POLYMARKET_ARB_LOOPBOT_FINNHUB_KEY"

ALL_TIMEFRAMES = ("5m", "15m", "30m")

CONFIG_LIMITS = {
    "order_size": (1.0, 100.0),
    "min_edge": (0.001, 0.100),
    "no_entry_seconds": (0.0, 30.0),
    "entry_window_seconds": (5.0, 300.0),
    "first_price_min": (0.10, 0.50),
    "first_price_max": (0.30, 0.60),
    "hedge_timeout_seconds": (2.0, 300.0),
    "force_exit_before_close_seconds": (1.0, 120.0),
    "local_vol_lookback_seconds": (4.0, 300.0),
    "local_vol_threshold": (0.001, 0.200),
    "regime_refresh_seconds": (10.0, 3600.0),
    "regime_forecast_interval_seconds": (300.0, 86400.0),
    "regime_max_age_seconds": (300.0, 172800.0),
    "regime_min_confidence": (0.0, 1.0),
}

DEFAULT_CONFIG: Dict[str, Any] = {
    "mode": "auto",  # auto: pre-tuned defaults (locked in the UI); manual: editable fields
    "strategy_schema": "sequential_v2",
    "order_size": 5.0,
    "min_edge": 0.003,
    "no_entry_seconds": 0.5,
    "entry_window_seconds": 60.0,
    "first_price_min": 0.45,
    "first_price_max": 0.49,
    "hedge_timeout_seconds": 45.0,
    "force_exit_before_close_seconds": 10.0,
    "local_vol_lookback_seconds": 30.0,
    "local_vol_threshold": 0.01,
    "regime_refresh_seconds": 60.0,
    "regime_forecast_interval_seconds": 600.0,
    "regime_max_age_seconds": 900.0,
    "regime_min_confidence": 0.45,
    "timeframes": ["5m", "15m", "30m"],
}

_STATE: Dict[str, Any] = {"proc": None, "started_at": None}
_STATE_LOCK = threading.Lock()

# status() is polled every ~2s from the dashboard and used to shell out to
# `pgrep` on every single call — cheap in isolation, but it's still a
# subprocess spawn on a FastAPI worker thread shared with every other route
# (config-save, provider fetches, ...), and it adds up under frequent polling.
# Cache the result briefly; start_bot()'s own external-process guard bypasses
# the cache (fresh=True) since it's a rare, deliberate action where staleness
# could let two loop bots run at once.
_PID_CACHE: Dict[str, Any] = {"lock": threading.Lock(), "t": 0.0, "pids": []}
_PID_CACHE_TTL_S = 3.0


def load_config() -> Dict[str, Any]:
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return dict(DEFAULT_CONFIG)
    config = dict(DEFAULT_CONFIG)
    legacy = raw.get("strategy_schema") != "sequential_v2"
    if raw.get("mode") in ("auto", "manual"):
        config["mode"] = str(raw["mode"])
    for key in CONFIG_LIMITS:
        value = raw.get(key)
        if legacy and key == "min_edge":
            # The old value was a gross simultaneous-edge threshold. It is not
            # comparable to sequential_v2's fee-aware net edge.
            continue
        if isinstance(value, (int, float)):
            config[key] = float(value)
    tfs = raw.get("timeframes")
    if isinstance(tfs, list):
        cleaned = [tf for tf in tfs if tf in ALL_TIMEFRAMES]
        if cleaned:
            config["timeframes"] = cleaned
    return config


def save_config(profile: Dict[str, Any]) -> tuple[Dict[str, Any], Optional[str]]:
    cleaned: Dict[str, Any] = {}
    mode = profile.get("mode", "auto")
    if mode not in ("auto", "manual"):
        return load_config(), "mode must be auto or manual"
    cleaned["mode"] = str(mode)
    cleaned["strategy_schema"] = "sequential_v2"
    for key, default in DEFAULT_CONFIG.items():
        if key in {"mode", "strategy_schema"}:
            continue
        if key == "timeframes":
            tfs = profile.get("timeframes", default)
            if not isinstance(tfs, list) or not tfs:
                return load_config(), "timeframes must be a non-empty list"
            unknown = [tf for tf in tfs if tf not in ALL_TIMEFRAMES]
            if unknown:
                return load_config(), f"unknown timeframes: {unknown} (allowed {list(ALL_TIMEFRAMES)})"
            cleaned[key] = list(tfs)
            continue
        value = profile.get(key, default)
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return load_config(), f"{key} must be numeric"
        low, high = CONFIG_LIMITS[key]
        if not low <= numeric <= high:
            return load_config(), f"{key}={numeric} outside allowed range [{low}, {high}]"
        cleaned[key] = numeric
    if cleaned["first_price_min"] > cleaned["first_price_max"]:
        return load_config(), "first_price_min must be <= first_price_max"
    if cleaned["no_entry_seconds"] >= cleaned["entry_window_seconds"]:
        return load_config(), "no_entry_seconds must be less than entry_window_seconds"
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    # Atomic write: the running bot hot-reloads this file on every poll, so a
    # partial in-place write could be parsed mid-write and the change silently
    # lost. Write to a temp file and os.replace so readers only ever see the
    # complete JSON payload.
    tmp_path = CONFIG_PATH.with_name(".config.json.tmp")
    tmp_path.write_text(json.dumps(cleaned, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp_path, CONFIG_PATH)
    return cleaned, None


def managed_proc() -> Optional[subprocess.Popen]:
    with _STATE_LOCK:
        return _STATE["proc"]


def stop_file_exists() -> bool:
    return STOP_PATH.exists()


def _process_cwd(pid: int) -> Optional[Path]:
    """Resolve another process's cwd on Linux (/proc) or macOS (lsof)."""
    proc_cwd = Path(f"/proc/{pid}/cwd")
    try:
        if proc_cwd.exists():
            return proc_cwd.resolve()
    except OSError:
        pass
    try:
        result = subprocess.run(
            ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.splitlines():
            if line.startswith("n") and len(line) > 1:
                return Path(line[1:]).resolve()
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def _argv_runs_this_loopbot(pid: int, command: str) -> bool:
    """Match the exact Python program argv, never a shell/pytest substring."""
    try:
        argv = shlex.split(command)
    except ValueError:
        return False
    if len(argv) < 2 or not Path(argv[0]).name.lower().startswith("python"):
        return False
    # The controller starts ``python scripts/run_loop_bot.py ...``. Support a
    # small set of no-value interpreter flags, but reject -c/-m and shell
    # wrappers whose command text merely happens to mention the filename.
    index = 1
    while index < len(argv) and argv[index] in {"-u", "-B", "-O", "-OO", "-s", "-S", "-E", "-I", "-q"}:
        index += 1
    if index >= len(argv) or argv[index].startswith("-"):
        return False
    arg = argv[index]
    if not arg.endswith("run_loop_bot.py"):
        return False
    target = (PROJECT_ROOT / RUNNER_SCRIPT).resolve()
    candidate = Path(arg)
    if candidate.is_absolute():
        try:
            return candidate.resolve() == target
        except OSError:
            return False
    cwd = _process_cwd(pid)
    if cwd is None:
        return False
    try:
        return (cwd / candidate).resolve() == target
    except OSError:
        return False


def _loop_bot_pids(fresh: bool = False) -> list[int]:
    now = time.time()
    with _PID_CACHE["lock"]:
        if not fresh and now - _PID_CACHE["t"] < _PID_CACHE_TTL_S:
            return _PID_CACHE["pids"]
    try:
        out = subprocess.run(
            ["ps", "-axo", "pid=,command="],
            capture_output=True, text=True, timeout=5,
        ).stdout
        pids = []
        for line in out.splitlines():
            fields = line.strip().split(None, 1)
            if len(fields) != 2 or not fields[0].isdigit():
                continue
            pid = int(fields[0])
            if pid != os.getpid() and _argv_runs_this_loopbot(pid, fields[1]):
                pids.append(pid)
    except (OSError, subprocess.SubprocessError):
        pids = []
    with _PID_CACHE["lock"]:
        _PID_CACHE["t"] = now
        _PID_CACHE["pids"] = pids
    return pids


def start_bot(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    with _STATE_LOCK:
        if _STATE["proc"] is not None and _STATE["proc"].poll() is None:
            return {"error": "loop bot already running", "pid": _STATE["proc"].pid}
        # Hard rule: one loop bot at a time. A bot started before this server
        # (re)started is invisible to _STATE["proc"]; spawning another would
        # have two bots polling/writing the same state files. fresh=True:
        # this guard must never act on a stale cached pid list.
        external = _loop_bot_pids(fresh=True)
        if external:
            return {
                "started": False, "already_running": True, "pids": external,
                "note": "An externally-started loop bot is already active and has been adopted for status. "
                        "Use STOP before starting a replacement.",
            }
        profile = load_config()
        if config:
            saved, err = save_config(config)
            if err:
                return {"error": err}
            profile = saved
        else:
            # load_config() can migrate a legacy simultaneous-strategy profile
            # in memory (notably gross min_edge -> v2 net min_edge). Persist
            # that complete profile before spawning so the runner's first hot
            # reload cannot resurrect values from the legacy file.
            saved, err = save_config(profile)
            if err:
                return {"error": err}
            profile = saved
        STOP_PATH.unlink(missing_ok=True)
        try:
            from dashboard_js import envs

            finnhub_key = envs.finnhub_api_key() or ""
        except Exception:  # noqa: BLE001
            finnhub_key = ""
        log_handle = LOG_PATH.open("a", buffering=1, encoding="utf-8")
        log_handle.write(
            f"--- loop bot start {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} "
            f"config={json.dumps(profile)} ---\n"
        )
        command = [
                str(PROJECT_ROOT / ".venv" / "bin" / "python"),
                RUNNER_SCRIPT,
                "--order-size", str(profile["order_size"]),
                "--min-edge", str(profile["min_edge"]),
                "--no-entry-seconds", str(profile["no_entry_seconds"]),
                "--entry-window-seconds", str(profile["entry_window_seconds"]),
                "--first-price-min", str(profile["first_price_min"]),
                "--first-price-max", str(profile["first_price_max"]),
                "--hedge-timeout-seconds", str(profile["hedge_timeout_seconds"]),
                "--force-exit-before-close-seconds", str(profile["force_exit_before_close_seconds"]),
                "--local-vol-lookback-seconds", str(profile["local_vol_lookback_seconds"]),
                "--local-vol-threshold", str(profile["local_vol_threshold"]),
                "--regime-refresh-seconds", str(profile["regime_refresh_seconds"]),
                "--regime-forecast-interval-seconds", str(profile["regime_forecast_interval_seconds"]),
                "--regime-max-age-seconds", str(profile["regime_max_age_seconds"]),
                "--regime-min-confidence", str(profile["regime_min_confidence"]),
                "--timeframes", ",".join(profile["timeframes"]),
        ]
        child_env = os.environ.copy()
        if finnhub_key:
            child_env[FINNHUB_KEY_ENV] = finnhub_key
        proc = subprocess.Popen(
            command,
            cwd=str(PROJECT_ROOT),
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            env=child_env,
        )
        _STATE.update({"proc": proc, "started_at": time.time()})
        return {
            "started": True,
            "pid": proc.pid,
            "config": profile,
            "note": "ISOLATED PAPER LOOP BOT — reads public books only; no real orders possible; "
                    "independent state/PnL in state/loop_bot/",
        }


def _terminate_in_background(proc: subprocess.Popen) -> None:
    """Terminate a managed bot without blocking the dashboard request.

    Stop commands used to hold the HTTP response open until the process
    exited (up to ~17s of kill grace), which made the panel feel laggy.
    The actual termination now runs on a daemon thread; the status poll
    observes `proc.poll()` and reports STOPPED the moment it exits.
    """

    def _kill() -> None:
        try:
            proc.terminate()
            try:
                proc.wait(timeout=12)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=_kill, daemon=True).start()


def stop_bot() -> Dict[str, Any]:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STOP_PATH.write_text(
        "loop bot stop requested from dashboard\n"
        f"at: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n",
        encoding="utf-8",
    )
    with _STATE_LOCK:
        proc = _STATE["proc"]
        if proc is None:
            external = _loop_bot_pids(fresh=True)
            if external:
                return {
                    "stopped": True, "pids": external,
                    "note": "stop requested for adopted external loop bot; it will exit after its current poll",
                }
            return {"stopped": False, "note": "no loop bot running (stop file written anyway)"}
        if proc.poll() is None:
            _terminate_in_background(proc)
            return {"stopped": True, "pid": proc.pid, "note": "stop requested — terminating in the background"}
        return {"stopped": True, "exit_code": proc.poll(), "note": "process already exited"}


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def status() -> Dict[str, Any]:
    with _STATE_LOCK:
        proc = _STATE["proc"]
        started_at = _STATE["started_at"]
    pid = proc.pid if proc is not None else None
    external = [p for p in _loop_bot_pids() if p != pid]
    running = proc is not None and proc.poll() is None
    process_running = bool(running) or bool(external)
    raw_live = _read_json(STATE_PATH)
    legacy_snapshot: Optional[Dict[str, Any]] = None
    if raw_live and raw_live.get("strategy_schema") != "sequential_v2":
        legacy_snapshot = {
            "strategy_schema": "legacy_simultaneous_v1",
            "totals": raw_live.get("totals", {}),
            "started_at_utc": raw_live.get("started_at_utc"),
            "note": "retained for audit only; excluded from sequential_v2 performance",
        }
        live: Dict[str, Any] = {}
    else:
        live = raw_live
    # The state file's "running" flag goes stale whenever the bot dies without
    # a graceful shutdown (SIGTERM without a handler, crash, kill -9) — it can
    # say "running" for days after the process is gone. Trust the process, not
    # the file: if no process is alive, report stopped so the UI enables START.
    live["running"] = process_running
    # The state file carries the RUNNER's last-known config/uptime — when a
    # bot was stopped (or a different one runs) those are stale and must NOT
    # override the live controller values.
    for key in ("config", "uptime_s", "started_at_utc"):
        live.pop(key, None)
    resp: Dict[str, Any] = {
        # Kept as the UI's historical run-state key; controller_owned makes
        # ownership explicit when a dashboard restart adopts an external PID.
        "managed_running": process_running,
        "controller_owned": running,
        "external_pids": external,
        "pid": pid if pid is not None else (external[0] if external else None),
        "exit_code": proc.poll() if proc is not None else None,
        "uptime_s": round(time.time() - started_at, 1) if started_at else None,
        "started_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started_at)) if started_at else None,
        "stop_file": stop_file_exists(),
        "config": load_config(),
        "performance": performance(),
        "strategy_schema": "sequential_v2",
        "legacy_metrics_included": False,
        "legacy_snapshot": legacy_snapshot,
        "totals": {
            "realized_pnl": 0.0, "open_locked_pnl": 0.0, "entries": 0,
            "exits": 0, "fees": 0.0, "open_exposure": 0.0,
        },
        "timeframes": {},
        "paper_only": True,
        "real_orders_possible": False,
        **live,
    }
    return resp


def events(limit: int = 80) -> Dict[str, Any]:
    rows: list[Dict[str, Any]] = []
    try:
        with EVENTS_PATH.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return {"events": rows[-max(1, min(limit, 5000)):], "total": len(rows)}


def performance() -> Dict[str, Any]:
    """Fee/depth-aware sequential_v2 performance; legacy fantasy PnL excluded."""
    rows = events(limit=5000)["events"]
    trades: list[Dict[str, Any]] = []
    legacy_excluded = 0
    for row in rows:
        if row.get("schema") != "sequential_v2":
            if row.get("kind") in {"enter", "exit"}:
                legacy_excluded += 1
            continue
        if row.get("kind") not in {"pair_completed", "protective_exit"}:
            continue
        try:
            cost = float(row.get("capital_deployed") or 0.0)
            pnl = float(row.get("pnl") or 0.0)
        except (TypeError, ValueError):
            continue
        if cost > 0:
            trades.append({"tf": row.get("tf"), "cost": cost, "pnl": pnl})
    payload: Dict[str, Any] = {
        "trades": len(trades),
        "total_cost": round(sum(t["cost"] for t in trades), 4),
        "total_pnl": round(sum(t["pnl"] for t in trades), 4),
        "roi_pct": None,
        "mean_trade_ret_pct": None,
        "std_trade_ret_pct": None,
        "sharpe": None,
        "strategy_schema": "sequential_v2",
        "legacy_events_excluded": legacy_excluded,
    }
    if trades:
        if payload["total_cost"] > 0:
            payload["roi_pct"] = round(payload["total_pnl"] / payload["total_cost"] * 100.0, 2)
        rets = [t["pnl"] / t["cost"] for t in trades]
        payload["mean_trade_ret_pct"] = round(sum(rets) / len(rets) * 100.0, 2)
        if len(rets) > 1:
            mean = sum(rets) / len(rets)
            var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
            std = var ** 0.5
            payload["std_trade_ret_pct"] = round(std * 100.0, 2)
            if std > 0:
                payload["sharpe"] = round(mean / std, 3)
    return payload


def chart() -> Dict[str, Any]:
    """Decision chart for sequential_v2; older enter/exit rows stay excluded."""
    rows = events(limit=5000)["events"]
    btc: list[Dict[str, Any]] = []
    entries: list[Dict[str, Any]] = []
    exits: list[Dict[str, Any]] = []
    for row in rows:
        kind = row.get("kind")
        t = row.get("t")
        if kind == "btc" and isinstance(row.get("price"), (int, float)):
            btc.append({"t": t, "price": row["price"]})
            continue
        if row.get("schema") != "sequential_v2":
            continue
        if kind == "first_filled":
            entries.append({
                "t": t, "tf": row.get("tf"), "slug": row.get("slug"),
                "combined": None, "size": row.get("size"), "side": row.get("side"),
                "vwap": row.get("vwap"), "fee": row.get("fee"), "btc": row.get("btc"),
            })
        elif kind in {"pair_completed", "protective_exit"}:
            exits.append({
                "t": t, "tf": row.get("tf"), "slug": row.get("slug"),
                "pnl": row.get("pnl"), "combined": row.get("combined"),
                "btc": row.get("btc"), "exit_kind": kind, "fees": row.get("fees"),
            })
    live = _read_json(STATE_PATH)
    totals = live.get("totals", {})
    tf_totals: Dict[str, Dict[str, Any]] = {}
    for row in entries:
        tf = row["tf"]
        bucket = tf_totals.setdefault(tf, {"entries": 0, "exits": 0, "realized": 0.0})
        bucket["entries"] += 1
    for row in exits:
        tf = row["tf"]
        bucket = tf_totals.setdefault(tf, {"entries": 0, "exits": 0, "realized": 0.0})
        bucket["exits"] += 1
        bucket["realized"] = round(bucket["realized"] + float(row.get("pnl") or 0.0), 4)
    return {
        "btc": btc[-400:],
        "entries": entries[-200:],
        "exits": exits[-200:],
        "totals": totals,
        "per_tf": tf_totals,
        "performance": performance(),
    }


def log_tail(lines: int = 40) -> Dict[str, Any]:
    try:
        text = LOG_PATH.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    return {"lines": text.splitlines()[-max(1, min(lines, 200)):], "path": str(LOG_PATH)}
