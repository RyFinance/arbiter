"""Live depth-ladder capture engine.

Long-running process that subscribes to the Polymarket CLOB WebSocket for the
markets currently traded (or a fixed token list) and persists full depth-ladder
snapshots (all price levels, not just best bid/ask) to
``state/depth_ladders/<market_id>/<date>.jsonl`` on every book update.

This is the permanent fix for the depth-data gap that has blocked backtest_gine.py's
strict depth-replay mode. Once it has been run long enough to accumulate data,
the backtest's strict depth-replay model can consume real historical order-book
depth instead of only top-of-book.

Features:
  * Reuses the reconnect/backoff pattern proven in ClobBookFeed.run().
  * File rotation: rotates when a file exceeds a max size (default 50 MB) or
    at midnight UTC, so files never grow unbounded.
  * Each line is a JSON object: {ts, event, market_id, token_id, asks, bids}.
  * ``--duration`` for testing (auto-stops after N seconds).
  * ``--token`` for subscribing to a specific token instead of the full window.

Usage:
    # Run against the current traded markets (reads state/market_cache.json).
    python scripts/capture_live_depth.py

    # Run for a specific token only (debug).
    python scripts/capture_live_depth.py --token 10526729713767195206184238927622562763255114981117487747987838443758

    # Run for 30 seconds (testing).
    python scripts/capture_live_depth.py --duration 30
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import websockets

# Ensure polymarket_arb package is importable when run from scripts/.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from polymarket_arb.config import SETTINGS  # noqa: E402
from polymarket_arb.models import MarketMeta  # noqa: E402

# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

DEFAULT_OUTPUT_ROOT = "state/depth_ladders"
MAX_FILE_SIZE_BYTES = 50 * 1024 * 1024  # 50 MB per file before rotation
WS_URL = f"{SETTINGS.clob_ws_url.rstrip('/')}/ws/market"


# --------------------------------------------------------------------------- #
# Depth snapshot type                                                         #
# --------------------------------------------------------------------------- #

@dataclass
class DepthSnapshot:
    """A full depth-ladder snapshot for one market at one moment."""
    market_id: str
    token_id: str
    ts: float  # epoch seconds (time.time)
    wall_time: str  # ISO timestamp
    asks: list[tuple[float, float]]  # sorted ascending: [(price, size), ...]
    bids: list[tuple[float, float]]  # sorted descending: [(price, size), ...]
    update_id: int = 0  # sequence number from the feed
    received_at: float = 0.0  # when we received it (time.time)

    def to_dict(self) -> dict:
        # Ensure asks are ascending by price, bids descending by price in the
        # serialized output (defensive — callers should already sort, but this
        # guarantees the on-disk format is always canonical).
        sorted_asks = sorted(self.asks, key=lambda x: x[0])
        sorted_bids = sorted(self.bids, key=lambda x: x[0], reverse=True)
        return {
            "ts": self.ts,
            "wall_time": self.wall_time,
            "event": "book_update",
            "market_id": self.market_id,
            "token_id": self.token_id,
            "update_id": self.update_id,
            "received_at": self.received_at,
            "asks": [[p, s] for p, s in sorted_asks],
            "bids": [[p, s] for p, s in sorted_bids],
        }


# --------------------------------------------------------------------------- #
# Level parsing                                                              #
# --------------------------------------------------------------------------- #

def parse_levels(levels) -> list[tuple[float, float]]:
    """Parse ask/bid levels into [(price, size), ...] tuples."""
    result = []
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
            result.append((price, size))
        except (TypeError, ValueError, KeyError, IndexError):
            continue
    return result


def parse_levels_directional(asks_raw, bids_raw) -> tuple[list, list]:
    """Parse and sort ask/bid levels.

    Asks sorted ascending by price, bids sorted descending by price.
    """
    sorted_asks = sorted(parse_levels(asks_raw), key=lambda x: x[0])
    sorted_bids = sorted(parse_levels(bids_raw), key=lambda x: x[0], reverse=True)
    return sorted_asks, sorted_bids


# --------------------------------------------------------------------------- #
# Token resolution                                                            #
# --------------------------------------------------------------------------- #

def _resolve_token_ids() -> dict[str, list[str]]:
    """Resolve the currently-traded markets' token IDs.

    Reads state/market_cache.json for market metadata. In window mode, also
    resolves the current BTC Up/Down 5-min window via Gamma.

    Returns a dict mapping market_id -> [yes_token_id, no_token_id].
    """
    cache_path = PROJECT_ROOT / "state" / "market_cache.json"
    token_map: dict[str, list[str]] = {}

    if cache_path.exists():
        try:
            with open(cache_path) as f:
                cache = json.load(f)
            for m in cache.get("markets", []):
                mid = m.get("market_id") or m.get("id")
                yt = m.get("yes_token_id")
                nt = m.get("no_token_id")
                if mid and yt and nt:
                    token_map[mid] = [yt, nt]
        except (json.JSONDecodeError, OSError):
            pass

    # In window mode, resolve the current window's tokens via Gamma.
    if SETTINGS.window_mode:
        try:
            from polymarket_arb.window_tracker import WindowTracker
            from polymarket_arb.clients.gamma_client import get_gamma_session, resolve_window_market
            wt = WindowTracker(SETTINGS)
            window_slug = wt.current_slug()
            if window_slug:
                async def _resolve_window():
                    session = await get_gamma_session()
                    meta = await resolve_window_market(session, window_slug)
                    await session.aclose()
                    return meta
                meta = asyncio.run(_resolve_window())
                if meta and meta.market_id:
                    token_map[meta.market_id] = [meta.yes_token_id, meta.no_token_id]
        except Exception as e:
            print(f"WARNING: window token resolution failed: {e!r}", file=sys.stderr)

    if not token_map:
        print("WARNING: No markets found in market_cache.json. "
              "Use --token to specify token IDs manually.", file=sys.stderr)

    return token_map


def _token_to_market(token_map: dict[str, list[str]]) -> dict[str, str]:
    """Build a reverse mapping token_id -> market_id."""
    rev = {}
    for mid, toks in token_map.items():
        for t in toks:
            rev[t] = mid
    return rev


# --------------------------------------------------------------------------- #
# Depth file writer with rotation                                             #
# --------------------------------------------------------------------------- #

class DepthFileWriter:
    """Writes depth snapshots to ``state/depth_ladders/<market_id>/<date>.jsonl``.

    Rotates files when they exceed ``max_size_bytes`` or when the date changes
    (midnight UTC).
    """

    def __init__(self, output_root: str = DEFAULT_OUTPUT_ROOT,
                 max_size: int = MAX_FILE_SIZE_BYTES):
        self.output_root = Path(output_root)
        self.max_size = max_size
        # current_files: market_id -> (file_handle, current_date_str, current_size)
        self._files: dict[str, tuple] = {}

    def _date_str(self) -> str:
        """Return YYYY-MM-DD in UTC."""
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _ensure_dir(self, market_id: str) -> Path:
        """Ensure the output directory exists for this market."""
        market_dir = self.output_root / market_id
        market_dir.mkdir(parents=True, exist_ok=True)
        return market_dir

    def _open_file(self, market_id: str) -> tuple:
        """Open (or reopen) the current file for a market, rotating if needed."""
        market_dir = self._ensure_dir(market_id)
        date_str = self._date_str()
        base_path = market_dir / f"{date_str}.jsonl"

        # If the file exists and is too big, rotate by appending a sequence number.
        if base_path.exists() and base_path.stat().st_size >= self.max_size:
            seq = 1
            while True:
                rotated = market_dir / f"{date_str}.{seq}.jsonl"
                if not rotated.exists() or rotated.stat().st_size < self.max_size:
                    base_path = rotated
                    break
                seq += 1

        fh = open(base_path, "a", encoding="utf-8")
        return fh, date_str, 0

    def _get_file(self, market_id: str) -> tuple:
        """Get or create the file handle for a market, rotating if needed."""
        now_date = self._date_str()
        entry = self._files.get(market_id)
        if entry is None:
            entry = self._open_file(market_id)
            self._files[market_id] = entry
            return entry

        fh, date_str, size = entry
        # Rotate on date change.
        if date_str != now_date:
            fh.close()
            entry = self._open_file(market_id)
            self._files[market_id] = entry
            return entry
        # Rotate on size.
        if size >= self.max_size:
            fh.close()
            entry = self._open_file(market_id)
            self._files[market_id] = entry
            return entry
        return entry

    def write_snapshot(self, snapshot: DepthSnapshot) -> None:
        """Write one depth snapshot to the appropriate file."""
        fh, date_str, size = self._get_file(snapshot.market_id)
        line = json.dumps(snapshot.to_dict(), separators=(",", ":"))
        fh.write(line + "\n")
        fh.flush()
        self._files[snapshot.market_id] = (fh, date_str, size + len(line) + 1)

    def close(self) -> None:
        """Close all open file handles."""
        for fh, _, _ in self._files.values():
            try:
                fh.close()
            except Exception:
                pass
        self._files.clear()


# --------------------------------------------------------------------------- #
# WebSocket depth capture client                                              #
# --------------------------------------------------------------------------- #

class DepthCaptureClient:
    """WebSocket client that captures full depth ladders from the CLOB feed.

    Reuses the reconnect/backoff pattern proven in ClobBookFeed.run().
    """

    def __init__(self, token_ids: list[str], writer: DepthFileWriter,
                 token_to_market: dict[str, str], on_snapshot=None):
        self.token_ids = token_ids
        self.writer = writer
        self.token_to_market = token_to_market
        self.on_snapshot = on_snapshot
        self._stop = False
        self._ws = None
        self._sequence = 0  # per-session update counter
        self._updates_per_token: dict[str, int] = defaultdict(int)

    def stop(self):
        self._stop = True

    async def run(self, duration_s: float | None = None) -> dict:
        """Connect and capture depth until stop() or duration expires.

        Returns a summary dict with update counts, reconnect count, etc.
        """
        backoff = 0.5
        max_backoff = 8.0
        attempts = 0
        max_attempts = 3
        reconnect_count = 0

        if not self.token_ids:
            print("ERROR: No token IDs to subscribe to.", file=sys.stderr)
            return {"reconnects": 0, "updates": 0, "error": "no_token_ids"}

        deadline = time.monotonic() + duration_s if duration_s else None

        while not self._stop and attempts < max_attempts:
            if deadline and time.monotonic() >= deadline:
                break

            try:
                async with websockets.connect(
                    WS_URL,
                    ping_interval=10,
                    ping_timeout=10,
                    close_timeout=1,
                    max_queue=8192,
                ) as ws:
                    self._ws = ws
                    await self._subscribe(ws)
                    print(f"WS connected, subscribed to {len(self.token_ids)} tokens",
                          file=sys.stderr)
                    backoff = 0.5
                    attempts = 0

                    async for raw in ws:
                        if self._stop:
                            break
                        if deadline and time.monotonic() >= deadline:
                            break
                        self._handle(raw)

                    if self._stop:
                        break
                    if deadline and time.monotonic() >= deadline:
                        break
                    # Socket closed unexpectedly.
                    print("WS closed unexpectedly; reconnecting...",
                          file=sys.stderr)
                    reconnect_count += 1

            except Exception as e:
                self._ws = None
                if self._stop:
                    break
                attempts += 1
                print(f"WS feed unavailable ({type(e).__name__}: {e!r}); "
                      f"retrying (attempt {attempts}/{max_attempts})",
                      file=sys.stderr)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)

        if attempts >= max_attempts and not self._stop:
            print("WS disabled after retries; giving up.", file=sys.stderr)

        total_updates = sum(self._updates_per_token.values())
        return {
            "reconnects": reconnect_count,
            "updates": total_updates,
            "updates_per_token": dict(self._updates_per_token),
        }

    async def _subscribe(self, ws):
        """Send the subscription frame for all token IDs."""
        await ws.send(json.dumps({
            "type": "market",
            "markets": [],
            "assets_ids": self.token_ids,
            "initial_dump": True,
        }))
        await ws.send("PING")  # keepalive

    def _handle(self, raw):
        """Parse a WS frame and persist depth snapshots for each book update."""
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", "replace")
        raw = raw.strip()
        if not raw or raw.upper() == "PONG":
            return

        try:
            msg = json.loads(raw)
        except ValueError as e:
            print(f"_handle json parse failed: {e!r}", file=sys.stderr)
            return

        books = msg if isinstance(msg, list) else [msg]
        now = time.time()
        wall_time = datetime.now(timezone.utc).isoformat()

        for book in books:
            if not isinstance(book, dict):
                continue
            token = book.get("asset_id") or book.get("market")
            if token is None:
                continue

            mid = self.token_to_market.get(token, token)
            self._sequence += 1
            self._updates_per_token[token] += 1

            sorted_asks, sorted_bids = parse_levels_directional(
                book.get("asks") or [], book.get("bids") or [])

            if not sorted_asks and not sorted_bids:
                continue

            snapshot = DepthSnapshot(
                market_id=mid,
                token_id=token,
                ts=now,
                wall_time=wall_time,
                asks=sorted_asks,
                bids=sorted_bids,
                update_id=self._sequence,
                received_at=now,
            )
            self.writer.write_snapshot(snapshot)
            if self.on_snapshot:
                self.on_snapshot(snapshot)


# --------------------------------------------------------------------------- #
# Entry point                                                                 #
# --------------------------------------------------------------------------- #

def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Capture live Polymarket CLOB depth ladders to state/depth_ladders/"
    )
    parser.add_argument("--token", action="append", default=[],
                        help="Token ID to subscribe to (may repeat). If omitted, "
                             "auto-resolves from market_cache.json + window mode.")
    parser.add_argument("--output-root", default=DEFAULT_OUTPUT_ROOT,
                        help="Output directory root (default: state/depth_ladders)")
    parser.add_argument("--duration", type=float, default=None,
                        help="Stop after N seconds (for testing)")
    args = parser.parse_args()

    # Resolve tokens.
    token_map: dict[str, list[str]] = {}
    if not args.token:
        print("Resolving traded markets from market_cache.json...", file=sys.stderr)
        token_map = _resolve_token_ids()
        if not token_map:
            print("ERROR: No markets or tokens specified. Use --token to specify tokens.",
                  file=sys.stderr)
            return 1
    else:
        for t in args.token:
            token_map[t] = [t]

    all_tokens = sorted(set(args.token) | {t for toks in token_map.values() for t in toks})
    token_to_market = _token_to_market(token_map)

    print(f"Depth capture starting: {len(all_tokens)} tokens -> {args.output_root}",
          file=sys.stderr)
    print(f"  Markets: {list(token_map.keys())}", file=sys.stderr)
    print(f"  Tokens: {all_tokens}", file=sys.stderr)

    writer = DepthFileWriter(output_root=args.output_root)
    capture = DepthCaptureClient(
        token_ids=all_tokens,
        writer=writer,
        token_to_market=token_to_market,
    )

    # Handle SIGTERM/SIGINT for clean shutdown.
    def _shutdown(signum, frame):
        print(f"\nReceived signal {signum}, shutting down...", file=sys.stderr)
        capture.stop()
    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        summary = asyncio.run(capture.run(duration_s=args.duration))
    finally:
        writer.close()

    print(f"\nCapture summary: {json.dumps(summary, indent=2, default=str)}",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
