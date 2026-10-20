"""Pure file readers over the bot's real state files.

No FastAPI, no pandas, no third-party deps: everything here is stdlib so the
readers stay unit-testable in isolation and never crash the API layer. All
functions tolerate concurrent append/replace of the underlying files: JSONL
reads skip truncated/bad lines, and depth shard scans operate on a temp copy
so a file cannot change size mid-read.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

DEPTH_CACHE_TTL_S = 60.0

TRADE_MSGS = ("missed", "filled", "partial_resolved", "no_fill")

_WINDOW_RE = re.compile(r"^btc-updown-5m-(\d+)$")

_DEPTH_SCAN_CACHE: Dict[str, Any] = {"key": None, "result": None, "at": 0.0}


# ---------------------------------------------------------------------------
# time helpers
# ---------------------------------------------------------------------------

def utc_now() -> float:
    return time.time()


def to_iso(epoch: Optional[float]) -> Optional[str]:
    if epoch is None:
        return None
    try:
        return (
            dt.datetime.fromtimestamp(epoch, tz=dt.timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
    except (OverflowError, OSError, ValueError):
        return None


def parse_bot_ts(t: Any, anchor_epoch: Optional[float] = None) -> Optional[float]:
    """Parse a bot log ``t`` field into a unix epoch float.

    The bot writes either ``"HH:MM:SS.mmm"`` (no date; anchored to the file's
    mtime UTC date with a midnight-rollover correction) or an ISO-8601
    timestamp.
    """
    if not isinstance(t, str) or not t.strip():
        return None
    s = t.strip()
    if "T" in s:
        try:
            return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    m = re.match(r"^(\d{1,2}):(\d{2}):(\d{2})(?:\.(\d+))?$", s)
    if not m:
        return None
    anchor = anchor_epoch if anchor_epoch is not None else utc_now()
    try:
        day = dt.datetime.fromtimestamp(anchor, tz=dt.timezone.utc)
        day0 = day.replace(hour=0, minute=0, second=0, microsecond=0)
        frac = float("0." + m.group(4)) if m.group(4) else 0.0
        base = day0.timestamp() + int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3)) + frac
    except (OverflowError, OSError, ValueError):
        return None
    if base - anchor > 12 * 3600:
        base -= 86400
    return base


# ---------------------------------------------------------------------------
# generic jsonl readers
# ---------------------------------------------------------------------------

def iter_jsonl(path: Path, max_lines: Optional[int] = None) -> Iterator[Dict[str, Any]]:
    """Yield parsed JSON objects; skip blank/broken lines (truncated tails)."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if max_lines is not None and i >= max_lines:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if isinstance(obj, dict):
                    yield obj
    except OSError:
        return


def read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        obj = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def read_heartbeat(data_root: Path) -> Optional[float]:
    try:
        return float((data_root / "state" / "HEARTBEAT").read_text().strip())
    except (OSError, ValueError):
        return None


def read_latency_profile(data_root: Path) -> Dict[str, Any]:
    obj = read_json(data_root / "state" / "latency_profile.json")
    return obj or {}


def read_depth_lock(data_root: Path) -> Optional[Dict[str, Any]]:
    return read_json(data_root / "state" / "cache" / "capture_depth_ws.lock")


def pid_alive(pid: Optional[int]) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


def find_capture_pid(data_root: Path) -> Optional[int]:
    lock = read_depth_lock(data_root)
    if lock:
        pid = lock.get("pid")
        if pid_alive(pid):
            return int(pid)
    try:
        out = subprocess.run(
            ["pgrep", "-f", "capture_depth_ws"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in out.stdout.splitlines():
        try:
            return int(line.strip())
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# bot.jsonl parsing
# ---------------------------------------------------------------------------

def _parse_kv_metrics(msg: str) -> Dict[str, Any]:
    """Parse ``key=value`` tokens out of a session-metrics line.

    Skips noisy tokens like ``modes_by_order/fill[dry_run=0s/0f ...]`` and
    ``bandfills[...]`` which contain ``/``, brackets or non-numeric values.
    """
    out: Dict[str, Any] = {}
    for tok in msg.split():
        if "/" in tok or "[" in tok or "]" in tok:
            continue
        if "=" not in tok:
            continue
        key, _, val = tok.partition("=")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        if re.fullmatch(r"-?\d+", val):
            out[key] = int(val)
        elif re.fullmatch(r"-?\d+\.\d+", val):
            out[key] = float(val)
        elif re.fullmatch(r"-?\d+", val.replace(".", "", 1)):
            continue
    return out


def parse_session_metrics(msg: str) -> Optional[Dict[str, Any]]:
    if not isinstance(msg, str) or "session metrics:" not in msg:
        return None
    parsed = _parse_kv_metrics(msg)
    for key in ("loops", "opps", "sent", "fills", "partial", "resolved", "missed"):
        if key not in parsed:
            return None
    return parsed


def _anchor_for(path: Path) -> Optional[float]:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def extract_session_metrics_rows(bot_path: Path) -> List[Dict[str, Any]]:
    """Every ``session metrics:`` line as a dict with ts_epoch/ts_utc + fields."""
    anchor = _anchor_for(bot_path)
    out: List[Dict[str, Any]] = []
    for row in iter_jsonl(bot_path):
        msg = row.get("msg")
        parsed = parse_session_metrics(msg) if isinstance(msg, str) else None
        if parsed is None:
            continue
        ts = parse_bot_ts(row.get("t"), anchor)
        item = dict(parsed)
        item["ts_epoch"] = ts
        item["ts_utc"] = to_iso(ts)
        out.append(item)
    return out


def extract_trade_rows(bot_path: Path) -> List[Dict[str, Any]]:
    """TRADE rows whose msg is in {missed, filled, partial_resolved, no_fill}."""
    anchor = _anchor_for(bot_path)
    out: List[Dict[str, Any]] = []
    for row in iter_jsonl(bot_path):
        if row.get("event") != "TRADE":
            continue
        msg = row.get("msg")
        if msg not in TRADE_MSGS:
            continue
        data = row.get("data")
        if not isinstance(data, dict):
            data = {}
        ts = parse_bot_ts(row.get("t"), anchor)
        out.append(
            {
                "ts_epoch": ts,
                "ts_utc": to_iso(ts),
                "msg": msg,
                "data": data,
            }
        )
    return out


def find_session_start(bot_path: Path) -> Optional[Dict[str, Any]]:
    """First ``polymarket arb bot starting`` INFO row."""
    anchor = _anchor_for(bot_path)
    for row in iter_jsonl(bot_path):
        msg = row.get("msg")
        if isinstance(msg, str) and "polymarket arb bot starting" in msg:
            ts = parse_bot_ts(row.get("t"), anchor)
            return {"ts_epoch": ts, "ts_utc": to_iso(ts), "row": row}
    return None


def detect_paper_mode(bot_path: Path) -> Dict[str, bool]:
    """Scan startup lines for paper/dry-run flags. Defaults to paper mode."""
    paper: Optional[bool] = None
    dry_run: Optional[bool] = None
    for row in iter_jsonl(bot_path):
        msg = row.get("msg")
        if not isinstance(msg, str):
            continue
        m = re.search(r"paper_trade=(True|False)", msg)
        if m:
            paper = m.group(1) == "True"
        m = re.search(r"dry_run=(True|False)", msg)
        if m:
            dry_run = m.group(1) == "True"
        m = re.search(r"PAPER_TRADE=(true|false)", msg, flags=re.IGNORECASE)
        if m and paper is None:
            paper = m.group(1).lower() == "true"
    return {
        "paper": True if paper is None else paper,
        "dry_run": False if dry_run is None else dry_run,
    }


# ---------------------------------------------------------------------------
# alerts / ledger / archives
# ---------------------------------------------------------------------------

def read_alerts_rows(data_root: Path) -> List[Dict[str, Any]]:
    return list(iter_jsonl(data_root / "state" / "alerts.jsonl"))


def read_order_ledger_rows(data_root: Path) -> List[Dict[str, Any]]:
    return list(iter_jsonl(data_root / "state" / "order_ledger.jsonl"))


def list_session_archives(data_root: Path) -> List[Path]:
    base = data_root / "state" / "paper_session_archives"
    try:
        dirs = [p for p in base.iterdir() if p.is_dir()]
    except OSError:
        return []
    return sorted(dirs, key=lambda p: p.name)


# ---------------------------------------------------------------------------
# depth shards
# ---------------------------------------------------------------------------

def depth_shard_paths(data_root: Path) -> List[Path]:
    cache_dir = data_root / "state" / "cache"
    try:
        paths = sorted(
            cache_dir.glob("polymarket_depth_capture*.jsonl"),
            key=lambda p: p.name,
        )
    except OSError:
        return []
    return paths


def invalidate_depth_cache() -> None:
    _DEPTH_SCAN_CACHE["key"] = None
    _DEPTH_SCAN_CACHE["result"] = None
    _DEPTH_SCAN_CACHE["at"] = 0.0


def _depth_cache_key(paths: List[Path]) -> tuple:
    parts = []
    for p in paths:
        try:
            st = p.stat()
            parts.append((str(p), st.st_mtime_ns, st.st_size))
        except OSError:
            parts.append((str(p), None, None))
    return tuple(parts)


def _stream_copy(path: Path) -> Path:
    """Copy a live-append jsonl to a temp file so size is stable mid-read."""
    tmp = tempfile.NamedTemporaryFile(prefix="dash_depth_", suffix=".jsonl", delete=False)
    tmp.close()
    shutil.copy2(path, tmp.name)
    return Path(tmp.name)


def _window_start(market_id: Any) -> Optional[int]:
    if not isinstance(market_id, str):
        return None
    m = _WINDOW_RE.match(market_id)
    return int(m.group(1)) if m else None


def scan_depth_shards(data_root: Path, now: Optional[float] = None) -> Dict[str, Any]:
    """One expensive pass over all depth shards (on temp copies).

    Result is cached in a module-level cache keyed by per-shard
    (mtime_ns, size) and reused for ``DEPTH_CACHE_TTL_S`` seconds. Reads the
    module global at call time so tests may monkeypatch the TTL.
    """
    paths = depth_shard_paths(data_root)
    key = _depth_cache_key(paths)
    if (
        key == _DEPTH_SCAN_CACHE["key"]
        and _DEPTH_SCAN_CACHE["result"] is not None
        and time.monotonic() - _DEPTH_SCAN_CACHE["at"] < DEPTH_CACHE_TTL_S
    ):
        return dict(_DEPTH_SCAN_CACHE["result"])

    if now is None:
        now = utc_now()
    shards: List[Dict[str, Any]] = []
    snapshot_count = 0
    last_capture_ts: Optional[float] = None
    per_window: Dict[int, Dict[str, List[float]]] = {}

    for path in paths:
        copy_path = None
        rows = 0
        first_ts: Optional[float] = None
        last_ts: Optional[float] = None
        try:
            st = path.stat()
        except OSError:
            continue
        try:
            copy_path = _stream_copy(path)
            for row in iter_jsonl(copy_path):
                rows += 1
                ca = row.get("captured_at")
                if not isinstance(ca, (int, float)):
                    continue
                if first_ts is None:
                    first_ts = float(ca)
                last_ts = float(ca)
                if last_capture_ts is None or float(ca) > last_capture_ts:
                    last_capture_ts = float(ca)
                if row.get("type") == "depth_snapshot":
                    snapshot_count += 1
                    ws = _window_start(row.get("market_id"))
                    if ws is None:
                        continue
                    off = float(ca) - ws
                    if 0 <= off < 300:
                        token = str(row.get("token_id", ""))
                        per_window.setdefault(ws, {}).setdefault(token, []).append(float(ca))
        finally:
            if copy_path is not None:
                try:
                    copy_path.unlink()
                except OSError:
                    pass
        if rows > 0:
            shards.append(
                {
                    "name": path.name,
                    "bytes": st.st_size,
                    "mtime": st.st_mtime,
                    "rows": rows,
                    "first_ts": first_ts,
                    "last_ts": last_ts,
                }
            )

    windows = 0
    complete = 0
    last_30: List[Dict[str, Any]] = []
    for ws in sorted(per_window):
        tokens = per_window[ws]
        entry: Dict[str, Any] = {"window_start": ws, "complete": False}
        if len(tokens) >= 2:
            ok = True
            for captured_list in tokens.values():
                offs = sorted(ca - ws for ca in captured_list)
                gaps = [b - a for a, b in zip(offs, offs[1:])]
                max_gap = max(gaps) if gaps else 0.0
                if not (offs[0] <= 5.0 and offs[-1] >= 295.0 and max_gap <= 60.0):
                    ok = False
                    break
            entry["complete"] = ok
        windows += 1
        if entry["complete"]:
            complete += 1
        last_30.append(entry)
    last_30 = last_30[-30:]
    broken = windows - complete

    result = {
        "shards": shards,
        "snapshot_count": snapshot_count,
        "last_capture_ts": last_capture_ts,
        "continuity": {
            "windows": windows,
            "complete": complete,
            "broken": broken,
            "ratio": (complete / windows) if windows else 0.0,
            "criterion": "both tokens: first<=5s, last>=295s, max gap<=60s",
        },
        "last_30_windows": {
            "windows": len(last_30),
            "complete": sum(1 for e in last_30 if e["complete"]),
        },
    }
    _DEPTH_SCAN_CACHE["key"] = key
    _DEPTH_SCAN_CACHE["result"] = result
    _DEPTH_SCAN_CACHE["at"] = time.monotonic()
    return dict(result)


# ---------------------------------------------------------------------------
# live depth health: fast path over the newest shard + sidecar for full history
# ---------------------------------------------------------------------------

DEPTH_FAST_TTL_S = 120.0
FULL_SCAN_TTL_S = 600.0
SIDECAR_REL = Path("state") / "cache" / "depth_health_sidecar.json"

_NEWEST_SCAN_CACHE: Dict[str, Any] = {"key": None, "result": None, "at": 0.0}
_FULL_SCAN_STATE: Dict[str, Any] = {"running": False}


def depth_shard_metadata(data_root: Path, now: Optional[float] = None) -> List[Dict[str, Any]]:
    """Cheap stat() pass: per-shard bytes/mtime only, no content parsing."""
    if now is None:
        now = utc_now()
    out: List[Dict[str, Any]] = []
    for path in depth_shard_paths(data_root):
        try:
            st = path.stat()
        except OSError:
            continue
        out.append(
            {
                "name": path.name,
                "bytes": st.st_size,
                "mtime": st.st_mtime,
                "age_s": max(0.0, now - st.st_mtime),
            }
        )
    return out


def tail_last_capture_ts(path: Path) -> Optional[float]:
    """Read only the tail of the shard to find the most recent captured_at."""
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 64 * 1024))
            tail = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    last: Optional[float] = None
    for line in tail.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            ca = row.get("captured_at")
            if isinstance(ca, (int, float)):
                last = float(ca)
        except (json.JSONDecodeError, ValueError):
            continue
    return last


def scan_newest_shard(data_root: Path, now: Optional[float] = None) -> Dict[str, Any]:
    """Continuity over the windows of the newest shard only.

    The newest shard is where the live capture is written; it holds roughly
    the last 30 five-minute windows, which is exactly the operational
    continuity gate.  Cached by (mtime_ns, size) for ``DEPTH_FAST_TTL_S``.
    """
    if now is None:
        now = utc_now()
    metadata = depth_shard_metadata(data_root, now=now)
    if not metadata:
        return {
            "snapshot_count": 0,
            "last_capture_ts": None,
            "continuity": {"windows": 0, "complete": 0, "broken": 0, "ratio": 0.0,
                           "criterion": "both tokens: first<=5s, last>=295s, max gap<=60s"},
            "last_30_windows": {"windows": 0, "complete": 0},
        }
    newest = max(metadata, key=lambda item: item["mtime"])
    path = data_root / "state" / "cache" / newest["name"]
    try:
        st = path.stat()
    except OSError:
        st = None
    key = (path.name, st.st_mtime_ns if st else None, st.st_size if st else None)
    if (
        key == _NEWEST_SCAN_CACHE["key"]
        and _NEWEST_SCAN_CACHE["result"] is not None
        and time.monotonic() - _NEWEST_SCAN_CACHE["at"] < DEPTH_FAST_TTL_S
    ):
        return dict(_NEWEST_SCAN_CACHE["result"])

    snapshot_count = 0
    last_capture_ts: Optional[float] = None
    per_window: Dict[int, Dict[str, List[float]]] = {}
    copy_path = None
    try:
        copy_path = _stream_copy(path)
        for row in iter_jsonl(copy_path):
            ca = row.get("captured_at")
            if not isinstance(ca, (int, float)):
                continue
            if last_capture_ts is None or float(ca) > last_capture_ts:
                last_capture_ts = float(ca)
            if row.get("type") != "depth_snapshot":
                continue
            snapshot_count += 1
            ws = _window_start(row.get("market_id"))
            if ws is None:
                continue
            off = float(ca) - ws
            if 0 <= off < 300:
                token = str(row.get("token_id", ""))
                per_window.setdefault(ws, {}).setdefault(token, []).append(float(ca))
    finally:
        if copy_path is not None:
            try:
                copy_path.unlink()
            except OSError:
                pass

    windows = 0
    complete = 0
    entries: List[Dict[str, Any]] = []
    for ws in sorted(per_window):
        tokens = per_window[ws]
        ok = False
        if len(tokens) >= 2:
            ok = True
            for captured_list in tokens.values():
                offs = sorted(ca - ws for ca in captured_list)
                gaps = [b - a for a, b in zip(offs, offs[1:])]
                max_gap = max(gaps) if gaps else 0.0
                if not (offs[0] <= 5.0 and offs[-1] >= 295.0 and max_gap <= 60.0):
                    ok = False
                    break
        windows += 1
        if ok:
            complete += 1
        entries.append({"window_start": ws, "complete": ok})
    result = {
        "shard": newest["name"],
        "snapshot_count": snapshot_count,
        "last_capture_ts": last_capture_ts,
        "continuity": {
            "windows": windows,
            "complete": complete,
            "broken": windows - complete,
            "ratio": (complete / windows) if windows else 0.0,
            "criterion": "both tokens: first<=5s, last>=295s, max gap<=60s",
            "scope": "newest shard only",
        },
        "last_30_windows": {
            "windows": len(entries[-30:]),
            "complete": sum(1 for entry in entries[-30:] if entry["complete"]),
        },
    }
    _NEWEST_SCAN_CACHE["key"] = key
    _NEWEST_SCAN_CACHE["result"] = result
    _NEWEST_SCAN_CACHE["at"] = time.monotonic()
    return dict(result)


def load_depth_sidecar(data_root: Path, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """Load the persisted full-history scan if fresh; None otherwise."""
    if now is None:
        now = utc_now()
    path = data_root / SIDECAR_REL
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        written = raw.get("written_at")
        if isinstance(written, (int, float)) and now - written < FULL_SCAN_TTL_S:
            return raw
    except (OSError, json.JSONDecodeError):
        pass
    return None


def write_depth_sidecar(data_root: Path, result: Dict[str, Any]) -> None:
    """Atomically persist a full-history scan result."""
    path = data_root / SIDECAR_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(result)
    payload["written_at"] = utc_now()
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def refresh_depth_sidecar(data_root: Path) -> Dict[str, Any]:
    """Run the expensive full scan and persist it (blocking; call in a thread)."""
    result = scan_depth_shards(data_root)
    write_depth_sidecar(data_root, result)
    return result


def full_scan_running() -> bool:
    return bool(_FULL_SCAN_STATE["running"])


def start_full_scan_if_needed(data_root: Path, now: Optional[float] = None) -> bool:
    """Kick a background full scan if the sidecar is stale and none is running.

    Returns True when a background scan was started by this call.
    """
    if now is None:
        now = utc_now()
    if load_depth_sidecar(data_root, now=now) is not None:
        return False
    if _FULL_SCAN_STATE["running"]:
        return False
    _FULL_SCAN_STATE["running"] = True
    import threading

    def _work() -> None:
        try:
            refresh_depth_sidecar(data_root)
        except Exception:  # noqa: BLE001 - the sidecar is best-effort
            pass
        finally:
            _FULL_SCAN_STATE["running"] = False

    threading.Thread(target=_work, name="depth-sidecar-refresh", daemon=True).start()
    return True
