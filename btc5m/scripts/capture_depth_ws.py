"""
Standalone depth-capture engine for Polymarket CLOB WebSocket.

Subscribes to the Polymarket CLOB WebSocket (``wss://ws-subscriptions-clob.polymarket.com/ws/market``)
for order-book depth on the currently-active BTC Up/Down 5-min rolling-window markets.

Features:
  * Append-only snapshots of the full depth ladder (all price levels, not just top-of-book).
  * Auto re-resolves token IDs across 5-minute window boundaries — reconnects seamlessly
    to the next window's market without a restart.
  * Reconnect-on-disconnect with exponential backoff.
  * Heartbeat log line every HEARTBEAT_INTERVAL snapshots to confirm the process is alive.
  * Disk-space + file-growth monitoring for unattended overnight running: warns when free
    disk drops below threshold, logs periodic self-checks (total rows, file size, snapshot
    rate, time since last snapshot).

Read-only data capture only — no order placement, no execution logic, no writes to any
file that execution.py or backtest_engine.py depend on.

Output: ``state/cache/polymarket_depth_capture.jsonl`` (append-only JSONL, one snapshot
per line).

JSON Schema (one object per line):
{
    "type":           "depth_snapshot",          # constant: record type tag
    "captured_at":    float,                     # epoch seconds (time.time()) when this snapshot was received
    "captured_at_iso": str,                      # ISO-8601 UTC timestamp, e.g. "2026-08-11T11:45:00.123456+00:00"
    "market_id":      str,                       # Polymarket market_id (window slug, e.g. "btc-updown-5m-1786463100")
    "token_id":       str,                       # CLOB asset_id (the token this book update is for)
    "sequence":       int,                       # monotonically-increasing per-session update counter
    "bids":           [[price, size], ...],      # sorted descending by price (best bid first)
    "asks":           [[price, size], ...],      # sorted ascending by price (best ask first)
    "source":         "clob_ws"                  # constant: data source identifier
}

Usage:
    # Run against the current BTC Up/Down window (auto-resolves tokens from Gamma API).
    python scripts/capture_depth_ws.py

    # Run for a fixed duration (testing):
    python scripts/capture_depth_ws.py --duration 1800

    # Subscribe to specific token IDs (overrides window resolution):
    python scripts/capture_depth_ws.py --token <yes_token_id> --token <no_token_id>
"""
from __future__ import annotations

import asyncio
import fcntl
import json
import os
import shutil
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import websockets

# Ensure polymarket_arb package is importable when run from scripts/.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from polymarket_arb.config import SETTINGS  # noqa: E402

# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

WS_URL = f"{SETTINGS.clob_ws_url.rstrip('/')}/ws/market"
GAMMA_BASE_URL = SETTINGS.gamma_base_url
WINDOW_SECONDS = 300  # 5-minute rolling windows (matches SETTINGS.window_seconds)
WINDOW_RECHECK_INTERVAL_S = 30.0  # how often to check whether the window has rolled over
WINDOW_ROLLOVER_POLL_S = 1.0  # hard upper bound for noticing a busy-feed boundary
WINDOW_PRE_RESOLVE_S = 30.0
WINDOW_PRE_RESOLVE_RETRY_S = 5.0
HEARTBEAT_INTERVAL = 100  # log a heartbeat every N snapshots
BACKOFF_INITIAL = 0.5
BACKOFF_MAX = 10.0
MAX_RECONNECT_FAILURES = 10

# Path to the append-only capture file (Source A).
CACHE_DIR = PROJECT_ROOT / "state" / "cache"
OUTPUT_PATH = CACHE_DIR / "polymarket_depth_capture.jsonl"
DEFAULT_LOCK_PATH = CACHE_DIR / "capture_depth_ws.lock"
LOCK_FILE_ENV = "DEPTH_CAPTURE_LOCK_FILE"
LOCK_CONTENTION_EXIT_CODE = 75  # sysexits.h EX_TEMPFAIL

# Rotation: rotate shard at 500 MB or UTC midnight, whichever first.
ROTATION_MAX_SIZE_BYTES = 500 * 1024 * 1024  # 500 MB

# Disk floor policy: warn at 5 GB free, hard-stop at 2 GB.
DISK_WARN_GB = 5
DISK_HARDSTOP_GB = 2
DISK_MIN_FREE_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB — warn below this (legacy compat)

# Status sidecar: atomically overwritten every SELF_CHECK_INTERVAL snapshots
# and after every window rollover.  Dashboard panel reads this file.
STATUS_FILE = CACHE_DIR / "sta_status.json"

# Self-check interval: every N snapshots, log a self-check line + write status.
SELF_CHECK_INTERVAL = 100


# --------------------------------------------------------------------------- #
# Process singleton                                                           #
# --------------------------------------------------------------------------- #

class CaptureLockUnavailable(RuntimeError):
    """Raised when another depth-capture process owns the singleton lock."""

    def __init__(self, lock_path: Path, owner_metadata: str):
        self.lock_path = Path(lock_path)
        self.owner_metadata = owner_metadata
        super().__init__(f"lock={self.lock_path} owner={owner_metadata}")


class CaptureInstanceLock:
    """Lifetime-held advisory lock for the depth-capture process.

    The lock file is intentionally persistent.  Kernel ``flock`` ownership,
    not file existence, is authoritative; the JSON payload is operator-facing
    metadata describing the process that most recently acquired it.
    """

    def __init__(self, lock_path: Path, fd: int):
        self.lock_path = Path(lock_path)
        self._fd = fd

    @classmethod
    def acquire(cls, lock_path: str | Path) -> "CaptureInstanceLock":
        path = Path(lock_path).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)

        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as exc:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                raw_owner = os.read(fd, 16 * 1024).decode("utf-8", "replace").strip()
            except OSError:
                raw_owner = ""
            os.close(fd)
            owner = raw_owner or "metadata unavailable"
            raise CaptureLockUnavailable(path, owner) from exc

        metadata = {
            "pid": os.getpid(),
            "ppid": os.getppid(),
            "acquired_at": datetime.now(timezone.utc).isoformat(),
            "script": str(Path(__file__).resolve()),
            "lock_path": str(path),
            "argv": list(sys.argv),
        }
        payload = (json.dumps(metadata, separators=(",", ":")) + "\n").encode("utf-8")
        try:
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, payload)
            os.fsync(fd)
        except BaseException:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
            raise
        return cls(path, fd)

    @property
    def fileno(self) -> int:
        return self._fd

    def close(self) -> None:
        if self._fd < 0:
            return
        fd, self._fd = self._fd, -1
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> "CaptureInstanceLock":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


# --------------------------------------------------------------------------- #
# Window helpers                                                              #
# --------------------------------------------------------------------------- #

def current_window_ts(now: float | None = None) -> int:
    """Floor ``now`` to the current 5-minute UTC window boundary."""
    if now is None:
        now = time.time()
    n = int(now)
    return n - (n % WINDOW_SECONDS)


def window_slug(window_ts: int) -> str:
    """Deterministic slug for a rolling-window market."""
    return f"{SETTINGS.window_asset}-updown-{SETTINGS.window_timeframe}-{window_ts}"


async def resolve_window_tokens(window_ts_val: int) -> list[str] | None:
    """Resolve YES + NO token IDs for a given BTC Up/Down window via the Gamma API.

    Uses httpx (same client the rest of the codebase uses). Returns the two
    token IDs as a list, or None if the window isn't published yet.
    """
    import httpx
    slug = window_slug(window_ts_val)
    try:
        async with httpx.AsyncClient(base_url=GAMMA_BASE_URL, timeout=20.0) as session:
            resp = await session.get("/events", params={"slug": slug})
            resp.raise_for_status()
            payload = resp.json()
    except Exception as e:
        print(f"[capture_depth_ws] Gamma resolve for {slug} failed: {e!r}",
              file=sys.stderr, flush=True)
        return None

    # payload is a list of event objects, each with a nested "markets" array.
    events = payload if isinstance(payload, list) else payload.get("data", [])
    for ev in events:
        if not isinstance(ev, dict):
            continue
        markets = ev.get("markets") or []
        for m in markets:
            clob = m.get("clobTokenIds")
            if isinstance(clob, str):
                try:
                    clob = json.loads(clob)
                except (ValueError, TypeError):
                    pass
            if isinstance(clob, list) and len(clob) >= 2:
                return [str(clob[0]), str(clob[1])]
            # Fallback: tokens[] array.
            tokens = m.get("tokens") or []
            if isinstance(tokens, list) and len(tokens) >= 2:
                up_tok = next((t for t in tokens if str(t.get("outcome", "")).lower() == "up"), None)
                down_tok = next((t for t in tokens if str(t.get("outcome", "")).lower() == "down"), None)
                if up_tok and down_tok:
                    return [str(up_tok.get("token_id", "")), str(down_tok.get("token_id", ""))]
    return None


# --------------------------------------------------------------------------- #
# Depth record writer                                                         #
# --------------------------------------------------------------------------- #

class DepthCaptureWriter:
    """Append-only writer for depth snapshots with date+size rotation.

    Writes to ``state/cache/polymarket_depth_capture.jsonl`` (active shard).
    Rotates to a new shard when the current file exceeds ``max_size_bytes``
    (default 500 MB) or when the UTC date changes (midnight rollover).

    Shard naming:
      polymarket_depth_capture.jsonl               (active shard)
      polymarket_depth_capture.2026-08-12.jsonl    (date-rolled on midnight)
      polymarket_depth_capture.2026-08-12.1.jsonl  (size-rolled within a day)

    Never touches or deletes the file — only appends JSONL lines. The file is
    opened in append mode for every write (so the file handle is never held open
    across reconnects, and a crash never loses buffered data).
    """

    def __init__(self, output_path: Path = OUTPUT_PATH,
                 max_size_bytes: int = ROTATION_MAX_SIZE_BYTES,
                 status_file: Path = STATUS_FILE):
        self._base_output_path = Path(output_path)
        self.output_path = self._base_output_path
        self.max_size_bytes = max_size_bytes
        self.status_file = status_file
        self._total_snapshots = 0
        self._last_snapshot_time: float = 0.0
        self._current_date_str = self._today_str()
        self._file_rotations = 0

    @staticmethod
    def _today_str() -> str:
        """Return today's date as YYYY-MM-DD in UTC."""
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _should_rotate(self) -> bool:
        """Check if the current shard should be rotated (date change or size)."""
        today = self._today_str()
        if today != self._current_date_str:
            return True
        try:
            if self.output_path.exists() and \
               self.output_path.stat().st_size >= self.max_size_bytes:
                return True
        except OSError:
            return True
        return False

    def _rotate_shard(self) -> None:
        """Rotate to a new shard file based on date or size."""
        today = self._today_str()
        base = self._base_output_path.parent
        stem = self._base_output_path.stem
        suffix = self._base_output_path.suffix
        old_date = self._current_date_str

        if old_date != today:
            # Date changed — use date-suffixed shard name.
            shard_name = f"{stem}.{today}{suffix}"
        else:
            # Same date, size exceeded — enumerate same-day shards.
            seq = 1
            while True:
                shard_name = f"{stem}.{today}.{seq}{suffix}"
                shard_path = base / shard_name
                if not shard_path.exists() or shard_path.stat().st_size < self.max_size_bytes:
                    break
                seq += 1

        self.output_path = base / shard_name
        self._current_date_str = today
        self._file_rotations += 1
        print(f"[capture_depth_ws] FILE ROTATION: new shard -> {self.output_path.name} "
              f"(file rotations: {self._file_rotations})",
              file=sys.stderr, flush=True)

    def _check_disk(self) -> tuple[float, bool]:
        """Check free disk space. Returns (free_gb, ok_to_write)."""
        try:
            usage = shutil.disk_usage(self.output_path.parent)
            free_gb = usage.free / (1024 ** 3)
        except OSError:
            return -1.0, True

        if free_gb < DISK_HARDSTOP_GB:
            print(f"[capture_depth_ws] DISK FLOOR: {free_gb:.2f} GB free < "
                  f"{DISK_HARDSTOP_GB} GB hard-stop. Pausing writes.",
                  file=sys.stderr, flush=True)
            return free_gb, False
        if free_gb < DISK_WARN_GB:
            print(f"[capture_depth_ws] DISK WARN: {free_gb:.2f} GB free < "
                  f"{DISK_WARN_GB} GB threshold.",
                  file=sys.stderr, flush=True)
        return free_gb, True

    def _file_size_mb(self) -> float:
        """Return current shard file size in MB."""
        try:
            if self.output_path.exists():
                return round(self.output_path.stat().st_size / (1024 * 1024), 2)
        except OSError:
            pass
        return 0.0

    def _write_status(self, current_window: str = "", rollovers: int = 0) -> None:
        """Atomically overwrite sta_status.json sidecar.

        Schema (MUST remain stable — dashboard reads this file):
            {
              "alive": true,
              "last_snapshot_epoch": float,
              "snapshots_total": int,
              "current_window": str,
              "rollovers": int,
              "disk_free_gb": float,
              "file_today_mb": float
            }
        """
        free_gb, _ = self._check_disk()
        status = {
            "alive": True,
            "last_snapshot_epoch": self._last_snapshot_time,
            "snapshots_total": self._total_snapshots,
            "current_window": current_window,
            "rollovers": rollovers,
            "disk_free_gb": round(free_gb, 2),
            "file_today_mb": self._file_size_mb(),
        }
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.status_file.with_suffix(".json.tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(status, f, separators=(",", ":"))
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(str(tmp), str(self.status_file))
        except OSError:
            pass

    def write_snapshot(self, market_id: str, token_id: str, sequence: int,
                       bids: list, asks: list) -> None:
        """Write one depth snapshot as a single JSONL line.

        Handles rotation, disk floor, and SELF-CHECK status sidecar.
        """
        now = time.time()
        record = {
            "type": "depth_snapshot",
            "captured_at": now,
            "captured_at_iso": datetime.now(timezone.utc).isoformat(),
            "market_id": market_id,
            "token_id": token_id,
            "sequence": sequence,
            "bids": bids,
            "asks": asks,
            "source": "clob_ws",
        }

        # --- Rotate if date changed or file too large ---
        if self._should_rotate():
            self._rotate_shard()

        # --- Disk floor check ---
        _, ok_to_write = self._check_disk()
        if not ok_to_write:
            self._last_snapshot_time = now
            return

        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, separators=(",", ":"))
        with open(self.output_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
        self._total_snapshots += 1
        self._last_snapshot_time = now

    def count_lines(self) -> int:
        """Count existing lines in the output file (for reporting)."""
        if not self.output_path.exists():
            return 0
        try:
            with open(self.output_path, "r") as f:
                return sum(1 for line in f if line.strip())
        except OSError:
            return 0

    def self_check(self) -> dict:
        """Return a self-check summary for monitoring."""
        file_size = self.output_path.stat().st_size if self.output_path.exists() else 0
        usage = shutil.disk_usage(self.output_path.parent)
        free_gb = usage.free / (1024 ** 3)
        total_gb = usage.total / (1024 ** 3)
        elapsed_since_last = (time.time() - self._last_snapshot_time
                             if self._last_snapshot_time else 0)
        return {
            "total_snapshots_written": self._total_snapshots,
            "total_lines_on_disk": self.count_lines(),
            "file_size_bytes": file_size,
            "file_size_mb": round(file_size / (1024 ** 2), 2),
            "time_since_last_snapshot_s": round(elapsed_since_last, 3),
            "disk_free_gb": round(free_gb, 2),
            "disk_total_gb": round(total_gb, 2),
            "disk_low": free_gb < DISK_WARN_GB,
            "file_rotations": self._file_rotations,
        }


# --------------------------------------------------------------------------- #
# Level parsing                                                               #
# --------------------------------------------------------------------------- #

def _parse_levels(levels) -> list[tuple[float, float]]:
    """Parse ask/bid levels into [(price, size), ...] tuples.

    Handles both dict format ({'price': ..., 'size': ...}) and list format
    ([price, size]). Filters out malformed levels.
    """
    result: list[tuple[float, float]] = []
    if not levels:
        return result
    for level in levels:
        try:
            if isinstance(level, dict):
                price = float(level["price"])
                size = float(level["size"])
            elif isinstance(level, (list, tuple)) and len(level) >= 2:
                price = float(level[0])
                size = float(level[1])
            else:
                continue
            if price > 0 and size >= 0:
                result.append((price, size))
        except (TypeError, ValueError, KeyError, IndexError):
            continue
    return result


# --------------------------------------------------------------------------- #
# Depth capture client                                                        #
# --------------------------------------------------------------------------- #

class DepthCapture:
    """WebSocket client that captures full depth ladders from the CLOB feed.

    Auto re-resolves token IDs across 5-minute window boundaries — when the
    current BTC Up/Down window closes, the client resolves the next window's
    tokens and reconnects to the new market seamlessly (no restart needed).
    """

    def __init__(self, token_ids: list[str] | None = None,
                 writer: DepthCaptureWriter | None = None,
                 duration_s: float | None = None):
        self.token_ids: list[str] = list(token_ids) if token_ids else []
        self.writer = writer or DepthCaptureWriter()
        self.duration_s = duration_s
        self._stop = False
        self._sequence = 0  # per-session update counter
        self._rollovers_survived = 0
        self._current_window_ts: int | None = None
        self.token_to_market: dict[str, str] = {}
        self._prepared_window_ts: int | None = None
        self._prepared_tokens: list[str] = []
        self._last_prepare_attempt: float = 0.0

    def stop(self):
        self._stop = True

    def _signal_handler(self, signum, frame):
        print(f"\n[capture_depth_ws] Received signal {signum}, shutting down...",
              file=sys.stderr, flush=True)
        self._stop = True

    def _is_window_rollover(self) -> bool:
        """Check whether the current 5-minute window has ended."""
        if self._current_window_ts is None:
            return False
        now = time.time()
        w = current_window_ts(now)
        return w != self._current_window_ts

    def _set_current_window(self, window_ts_val: int):
        self._current_window_ts = window_ts_val

    def _current_market_id(self) -> str:
        if self._current_window_ts is not None:
            return window_slug(self._current_window_ts)
        return ""

    async def _resolve_current_tokens(self) -> list[str]:
        """Resolve token IDs for the current window via Gamma API."""
        now = time.time()
        w = current_window_ts(now)
        self._set_current_window(w)
        tokens = await resolve_window_tokens(w)
        if tokens is None:
            print(f"[capture_depth_ws] Window {w} (slug={window_slug(w)}) not yet published; "
                  f"retrying in 10s...", file=sys.stderr, flush=True)
            return []
        print(f"[capture_depth_ws] Resolved window {w} (slug={window_slug(w)}) "
              f"tokens: {tokens}", file=sys.stderr, flush=True)
        return tokens

    async def _maybe_prepare_next_window(self, now: float | None = None) -> None:
        """Resolve the next window before the boundary while the old WS stays live."""
        if self._current_window_ts is None:
            return
        now = time.time() if now is None else now
        next_window = self._current_window_ts + WINDOW_SECONDS
        if next_window - now > WINDOW_PRE_RESOLVE_S:
            return
        if self._prepared_window_ts == next_window and len(self._prepared_tokens) == 2:
            return
        if now - self._last_prepare_attempt < WINDOW_PRE_RESOLVE_RETRY_S:
            return
        self._last_prepare_attempt = now
        tokens = await resolve_window_tokens(next_window)
        if tokens:
            self._prepared_window_ts = next_window
            self._prepared_tokens = list(tokens)
            print(
                f"[capture_depth_ws] Pre-resolved next window {next_window} "
                f"tokens: {tokens}",
                file=sys.stderr,
                flush=True,
            )

    async def _subscribe(self, ws):
        """Send the subscription frame for all current token IDs."""
        sub = {
            "type": "market",
            "markets": [],
            "assets_ids": self.token_ids,
            "initial_dump": True,
        }
        await ws.send(json.dumps(sub))
        print(f"[capture_depth_ws] Subscribed to {len(self.token_ids)} tokens: "
              f"{self.token_ids}", file=sys.stderr, flush=True)

    def _handle(self, raw):
        """Parse a WS frame and persist depth snapshots for each book update."""
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", "replace")
        raw_str = raw.strip()
        if not raw_str or raw_str.upper() == "PONG":
            return

        try:
            msg = json.loads(raw_str)
        except ValueError as e:
            print(f"[capture_depth_ws] _handle json parse failed: {e!r}",
                  file=sys.stderr, flush=True)
            return

        books = msg if isinstance(msg, list) else [msg]
        for book in books:
            if not isinstance(book, dict):
                continue
            token = book.get("asset_id") or book.get("market")
            if token is None:
                continue

            self._sequence += 1
            bids_raw = book.get("bids") or []
            asks_raw = book.get("asks") or []
            bids = _parse_levels(bids_raw)
            asks = _parse_levels(asks_raw)
            # Sort bids descending (best/highest first), asks ascending (best/lowest first).
            bids.sort(key=lambda x: x[0], reverse=True)
            asks.sort(key=lambda x: x[0])

            if not bids and not asks:
                continue

            market_id = self.token_to_market.get(token, "")
            if not market_id:
                market_id = self._current_market_id()

            self.writer.write_snapshot(
                market_id=market_id,
                token_id=str(token),
                sequence=self._sequence,
                bids=[[p, s] for p, s in bids],
                asks=[[p, s] for p, s in asks],
            )

            # Heartbeat + self-check every N snapshots.
            if self.writer._total_snapshots % HEARTBEAT_INTERVAL == 0:
                self._log_heartbeat()
            if self.writer._total_snapshots % SELF_CHECK_INTERVAL == 0:
                self._log_self_check()

    def _log_heartbeat(self):
        """Emit a heartbeat log line to confirm the process is alive."""
        now_iso = datetime.now(timezone.utc).isoformat()
        print(f"[capture_depth_ws] HEARTBEAT ts={now_iso} "
              f"snapshots={self.writer._total_snapshots} "
              f"rollovers={self._rollovers_survived} "
              f"tokens={self.token_ids} "
              f"market={self._current_market_id()}",
              file=sys.stderr, flush=True)

    def _log_self_check(self):
        """Emit a self-check line with disk-space + file-growth monitoring.

        Also atomically writes the sta_status.json sidecar.
        """
        info = self.writer.self_check()
        # Disk-space warning (warn at 5 GB, hard-stop at 2 GB).
        if info["disk_low"]:
            print(f"[capture_depth_ws] WARNING: low disk space! "
                  f"free={info['disk_free_gb']}GB total={info['disk_total_gb']}GB",
                  file=sys.stderr, flush=True)
        # Stall detection: if more than 30s since last snapshot.
        if info["time_since_last_snapshot_s"] > 30:
            print(f"[capture_depth_ws] WARNING: stalled capture! "
                  f"last_snapshot={info['time_since_last_snapshot_s']}s ago",
                  file=sys.stderr, flush=True)
        print(f"[capture_depth_ws] SELF-CHECK snapshots={info['total_snapshots_written']} "
              f"disk_size={info['file_size_mb']}MB "
              f"free_disk={info['disk_free_gb']}GB "
              f"time_since_last={info['time_since_last_snapshot_s']}s",
              file=sys.stderr, flush=True)

        # --- Write status sidecar (atomic overwrite) ---
        current_window = self._current_market_id()
        self.writer._write_status(
            current_window=current_window,
            rollovers=self._rollovers_survived,
        )

    async def _run_inner(self, ws):
        """Capture messages while checking the wall-clock boundary on every loop.

        The prior implementation checked rollover only after a five-second receive
        timeout.  A busy old-window feed therefore never took that branch and stayed
        subscribed 65-90 seconds into the next window.  This loop checks before and
        after every receive and caps the receive wait at one second.
        """
        deadline = time.monotonic() + self.duration_s if self.duration_s else None
        while not self._stop:
            if deadline and time.monotonic() >= deadline:
                break
            now = time.time()
            if self._is_window_rollover():
                print(
                    f"[capture_depth_ws] Window rollover detected at {now:.6f}",
                    file=sys.stderr,
                    flush=True,
                )
                return
            await self._maybe_prepare_next_window(now)
            # Poll for messages with a timeout (non-blocking — allows periodic checks).
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=WINDOW_ROLLOVER_POLL_S)
            except asyncio.TimeoutError:
                continue
            except websockets.ConnectionClosed:
                print("[capture_depth_ws] WebSocket closed unexpectedly.",
                      file=sys.stderr, flush=True)
                return  # signal outer loop to reconnect
            # The boundary may have crossed while recv() was pending. Do not label
            # a late old-token message as belonging to the new window.
            if self._is_window_rollover():
                now = time.time()
                print(
                    f"[capture_depth_ws] Window rollover detected at {now:.6f}",
                    file=sys.stderr,
                    flush=True,
                )
                return
            self._handle(raw)

    async def run(self) -> dict:
        """Connect and capture depth until stop() or duration expires.

        Auto-resolves token IDs each time it (re)connects, and re-resolves again
        when the 5-minute window boundary crosses. Returns a summary dict.
        """
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        # Ensure output directory exists.
        self.writer.output_path.parent.mkdir(parents=True, exist_ok=True)

        backoff = BACKOFF_INITIAL
        reconnect_count = 0

        print("[capture_depth_ws] Starting depth capture.", file=sys.stderr, flush=True)
        print(f"[capture_depth_ws] WS_URL={WS_URL}", file=sys.stderr, flush=True)
        print(f"[capture_depth_ws] OUTPUT={self.writer.output_path}", file=sys.stderr, flush=True)

        while not self._stop:
            # Resolve tokens for the current window (or use provided tokens).
            if not self.token_ids:
                tokens = await self._resolve_current_tokens()
                if not tokens:
                    print("[capture_depth_ws] No tokens resolved; sleeping 10s...",
                          file=sys.stderr, flush=True)
                    await asyncio.sleep(10)
                    continue
                self.token_ids = tokens
                # Build token -> market_id mapping.
                self.token_to_market = {}
                mid = self._current_market_id()
                for t in tokens:
                    self.token_to_market[t] = mid

            if not self.token_ids:
                print("[capture_depth_ws] No tokens to subscribe to.",
                      file=sys.stderr, flush=True)
                await asyncio.sleep(10)
                continue

            # Check disk space before connecting.
            usage = shutil.disk_usage(self.writer.output_path.parent)
            free_gb = usage.free / (1024 ** 3)
            if free_gb < DISK_MIN_FREE_BYTES / (1024 ** 3):
                print(f"[capture_depth_ws] WARNING: low disk! free={free_gb:.2f}GB "
                      f"threshold={DISK_MIN_FREE_BYTES / (1024**3):.2f}GB",
                      file=sys.stderr, flush=True)

            try:
                async with websockets.connect(
                    WS_URL,
                    ping_interval=10,
                    ping_timeout=10,
                    close_timeout=1,
                    max_queue=8192,
                ) as ws:
                    await self._subscribe(ws)
                    print("[capture_depth_ws] Connected.", file=sys.stderr, flush=True)
                    backoff = BACKOFF_INITIAL

                    # Inner loop — exits when window rolls over or WS closes.
                    await self._run_inner(ws)

                    # Check if the rollover was the cause.
                    if self._stop:
                        break
                    if self._is_window_rollover():
                        self._rollovers_survived += 1
                        print(f"[capture_depth_ws] Window rolled over. "
                              f"Total rollovers survived: {self._rollovers_survived}",
                              file=sys.stderr, flush=True)
                        # Use the pre-resolved next tokens when available, avoiding
                        # Gamma latency after the boundary. Fall back to a current
                        # resolve if pre-resolution was unavailable.
                        new_window = current_window_ts()
                        if (
                            self._prepared_window_ts == new_window
                            and len(self._prepared_tokens) == 2
                        ):
                            self._set_current_window(new_window)
                            tokens = list(self._prepared_tokens)
                            print(
                                f"[capture_depth_ws] Using pre-resolved window "
                                f"{new_window} tokens",
                                file=sys.stderr,
                                flush=True,
                            )
                        else:
                            tokens = await self._resolve_current_tokens()
                        if tokens:
                            self.token_ids = tokens
                            mid = self._current_market_id()
                            self.token_to_market = {t: mid for t in tokens}
                        self._prepared_window_ts = None
                        self._prepared_tokens = []
                        # Reset sequence for the new window.
                        self._sequence = 0
                        continue

            except Exception as e:
                print(f"[capture_depth_ws] WS error ({type(e).__name__}: {e!r}); "
                      f"reconnecting in {backoff:.1f}s...",
                      file=sys.stderr, flush=True)
                reconnect_count += 1
                if reconnect_count > MAX_RECONNECT_FAILURES:
                    print(f"[capture_depth_ws] Exceeded {MAX_RECONNECT_FAILURES} reconnect "
                          f"failures; giving up.", file=sys.stderr, flush=True)
                    break
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX)

        # Final self-check summary.
        info = self.writer.self_check()
        summary = {
            "total_snapshots": self.writer._total_snapshots,
            "total_lines_on_disk": info["total_lines_on_disk"],
            "file_size_bytes": info["file_size_bytes"],
            "reconnects": reconnect_count,
            "rollovers_survived": self._rollovers_survived,
            "disk_free_gb": info["disk_free_gb"],
        }
        print(f"[capture_depth_ws] Final summary: {json.dumps(summary, indent=2, default=str)}",
              file=sys.stderr, flush=True)
        return summary


# --------------------------------------------------------------------------- #
# Entry point                                                                 #
# --------------------------------------------------------------------------- #

def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Capture Polymarket CLOB depth ladders to state/cache/polymarket_depth_capture.jsonl"
    )
    parser.add_argument("--duration", type=float, default=None,
                        help="Stop after N seconds (for testing)")
    parser.add_argument("--token", action="append", default=[],
                        help="Token ID to subscribe to (may repeat). If omitted, "
                             "auto-resolves from the current BTC Up/Down window.")
    parser.add_argument("--output", type=str, default=str(OUTPUT_PATH),
                        help="Output JSONL file path")
    parser.add_argument(
        "--lock-file",
        type=str,
        default=os.environ.get(LOCK_FILE_ENV, str(DEFAULT_LOCK_PATH)),
        help=("Singleton lock path (default: state/cache/capture_depth_ws.lock; "
              f"override with {LOCK_FILE_ENV})"),
    )
    args = parser.parse_args()

    # Acquire before constructing the writer or initializing any network client.
    # Keep the descriptor live until the entire capture process has exited.
    try:
        instance_lock = CaptureInstanceLock.acquire(args.lock_file)
    except CaptureLockUnavailable as exc:
        print(
            "[capture_depth_ws] SINGLETON LOCK CONTENTION: "
            f"{exc}; exiting {LOCK_CONTENTION_EXIT_CODE}",
            file=sys.stderr,
            flush=True,
        )
        return LOCK_CONTENTION_EXIT_CODE

    try:
        output_path = Path(args.output)
        writer = DepthCaptureWriter(output_path=output_path)

        token_ids = list(args.token) if args.token else None
        capture = DepthCapture(token_ids=token_ids, writer=writer, duration_s=args.duration)

        asyncio.run(capture.run())
        return 0
    finally:
        instance_lock.close()


if __name__ == "__main__":
    sys.exit(main())
