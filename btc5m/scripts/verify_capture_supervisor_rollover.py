#!/usr/bin/env python3
"""Observe the depth-capture process across a real five-minute rollover.

This script is deliberately read-only.  It samples ``ps`` and the newest depth
record, then writes one atomic JSON evidence file.  It never starts, stops, or
signals the capture process.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import time
from datetime import datetime, timezone
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CAPTURE_GLOB = "polymarket_depth_capture*.jsonl"
DEFAULT_OUTPUT = ROOT / "state" / "capture_supervisor_rollover_2026-08-15.json"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def capture_processes() -> list[dict[str, Any]]:
    """Return actual capture workers, excluding this observer and grep tools."""
    result = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,lstart=,etime=,command="],
        check=True,
        capture_output=True,
        text=True,
    )
    rows: list[dict[str, Any]] = []
    for raw in result.stdout.splitlines():
        if "scripts/capture_depth_ws.py" not in raw:
            continue
        parts = raw.strip().split(None, 8)
        if len(parts) != 9:
            continue
        rows.append(
            {
                "pid": int(parts[0]),
                "ppid": int(parts[1]),
                "started": " ".join(parts[2:7]),
                "elapsed": parts[7],
                "command": parts[8],
            }
        )
    return rows


def _last_nonempty_line(path: Path) -> str:
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        end = handle.tell()
        if end == 0:
            return ""
        cursor = end - 1
        while cursor >= 0:
            handle.seek(cursor)
            if handle.read(1) == b"\n" and cursor != end - 1:
                break
            cursor -= 1
        handle.seek(cursor + 1)
        return handle.read(end - cursor - 1).decode("utf-8", errors="replace").strip()


def newest_depth_record(cache_dir: Path, pattern: str) -> dict[str, Any] | None:
    candidates = [path for path in cache_dir.glob(pattern) if path.is_file()]
    if not candidates:
        return None
    path = max(candidates, key=lambda candidate: candidate.stat().st_mtime_ns)
    line = _last_nonempty_line(path)
    if not line:
        return None
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        return {"path": str(path), "parse_error": True}
    return {
        "path": str(path),
        "market_id": record.get("market_id"),
        "captured_at": record.get("captured_at"),
        "captured_at_iso": record.get("captured_at_iso"),
        "sequence": record.get("sequence"),
    }


def observe(
    duration_s: float,
    interval_s: float,
    cache_dir: Path,
    pattern: str = DEFAULT_CAPTURE_GLOB,
) -> dict[str, Any]:
    started_monotonic = time.monotonic()
    started_at = _utc_now()
    samples: list[dict[str, Any]] = []
    deadline = started_monotonic + duration_s
    while True:
        now_monotonic = time.monotonic()
        processes = capture_processes()
        depth = newest_depth_record(cache_dir, pattern)
        samples.append(
            {
                "observed_at": _utc_now(),
                "elapsed_s": now_monotonic - started_monotonic,
                "process_count": len(processes),
                "processes": processes,
                "latest_depth": depth,
            }
        )
        if now_monotonic >= deadline:
            break
        time.sleep(min(interval_s, max(0.0, deadline - now_monotonic)))

    counts = [sample["process_count"] for sample in samples]
    market_ids = [
        sample["latest_depth"].get("market_id")
        for sample in samples
        if isinstance(sample.get("latest_depth"), dict)
        and sample["latest_depth"].get("market_id")
    ]
    unique_markets = list(dict.fromkeys(market_ids))
    pid_sets = [
        tuple(process["pid"] for process in sample["processes"])
        for sample in samples
    ]
    elapsed_s = time.monotonic() - started_monotonic
    passed = bool(
        elapsed_s >= 300.0
        and counts
        and min(counts) == 1
        and max(counts) == 1
        and len(unique_markets) >= 2
    )
    return {
        "scope": "live read-only capture supervisor verification",
        "started_at": started_at,
        "finished_at": _utc_now(),
        "duration_s": elapsed_s,
        "sample_interval_s": interval_s,
        "sample_count": len(samples),
        "paper_only": True,
        "mutated_capture_process": False,
        "minimum_process_count": min(counts) if counts else None,
        "maximum_process_count": max(counts) if counts else None,
        "duplicate_process_samples": sum(count > 1 for count in counts),
        "zero_process_samples": sum(count == 0 for count in counts),
        "unique_pid_sets": [list(pids) for pids in dict.fromkeys(pid_sets)],
        "markets_observed": unique_markets,
        "window_rollovers_observed": max(0, len(unique_markets) - 1),
        "status": "verified-pass" if passed else "verified-fail",
        "criterion": "duration>=300s, exactly one capture process at every sample, >=1 market rollover",
        "samples": samples,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration-seconds", type=float, default=330.0)
    parser.add_argument("--interval-seconds", type=float, default=5.0)
    parser.add_argument("--cache-dir", type=Path, default=ROOT / "state" / "cache")
    parser.add_argument("--capture-glob", default=DEFAULT_CAPTURE_GLOB)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    if args.duration_seconds < 300.0:
        parser.error("duration-seconds must be at least 300 for a full five-minute rollover")
    if not 0.1 <= args.interval_seconds <= 60.0:
        parser.error("interval-seconds must be between 0.1 and 60")
    report = observe(
        args.duration_seconds,
        args.interval_seconds,
        args.cache_dir.resolve(),
        args.capture_glob,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(args.output)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "verified-pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
