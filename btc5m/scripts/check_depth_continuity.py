#!/usr/bin/env python3
"""Quick point-in-time continuity check for active Polymarket depth shards."""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.replay_captured_depth import (  # noqa: E402
    TokenContinuity,
    _sha256_prefix,
    _snapshot_lines,
    market_window_start,
)


def analyze(
    paths: list[Path],
    *,
    start_at_epoch: float | None,
    observed_at_epoch: float | None = None,
) -> dict:
    observed = time.time() if observed_at_epoch is None else observed_at_epoch
    limits = {path: path.stat().st_size for path in paths}
    continuity: dict[str, dict[str, TokenContinuity]] = defaultdict(
        lambda: defaultdict(TokenContinuity)
    )
    rows = malformed = before_start = outside = 0
    for path in paths:
        for line in _snapshot_lines(path, limits[path]):
            try:
                record = json.loads(line)
                captured = float(record["captured_at"])
                market_id = str(record["market_id"])
                token_id = str(record["token_id"])
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                malformed += 1
                continue
            if start_at_epoch is not None and captured < start_at_epoch:
                before_start += 1
                continue
            start = market_window_start(market_id)
            if start is None or not token_id:
                malformed += 1
                continue
            offset = captured - start
            if not 0.0 <= offset < 300.0:
                outside += 1
                continue
            rows += 1
            continuity[market_id][token_id].observe(offset)

    details = []
    for market_id, tokens in continuity.items():
        start = market_window_start(market_id)
        if start is None:
            continue
        closed = observed >= start + 300
        token_details = {token: value.as_dict() for token, value in tokens.items()}
        continuity_failures: list[str] = []
        if closed:
            if len(tokens) != 2:
                continuity_failures.append(f"token_count={len(tokens)}_expected=2")
            for token, value in tokens.items():
                prefix = f"token={token}"
                if value.first_offset_s is None:
                    continuity_failures.append(f"{prefix}:first_offset_missing")
                elif value.first_offset_s > 5.0:
                    continuity_failures.append(
                        f"{prefix}:first_offset={value.first_offset_s}>5.0"
                    )
                if value.last_offset_s is None:
                    continuity_failures.append(f"{prefix}:last_offset_missing")
                elif value.last_offset_s < 295.0:
                    continuity_failures.append(
                        f"{prefix}:last_offset={value.last_offset_s}<295.0"
                    )
                if value.max_gap_s > 60.0:
                    continuity_failures.append(
                        f"{prefix}:max_gap={value.max_gap_s}>60.0"
                    )
        complete = closed and not continuity_failures
        details.append(
            {
                "market_id": market_id,
                "window_start": start,
                "closed": closed,
                "complete": complete,
                "continuity_failures": continuity_failures,
                "token_count": len(tokens),
                "tokens": token_details,
            }
        )
    details.sort(key=lambda item: item["window_start"])
    closed = [item for item in details if item["closed"]]
    complete = [item for item in closed if item["complete"]]
    incomplete = [item for item in closed if not item["complete"]]
    longest_streak = 0
    current_streak = 0
    current_streak_start: int | None = None
    previous_start: int | None = None
    for item in closed:
        window_start = int(item["window_start"])
        contiguous = previous_start is not None and window_start == previous_start + 300
        if item["complete"]:
            if not contiguous or current_streak == 0:
                current_streak = 1
                current_streak_start = window_start
            else:
                current_streak += 1
            longest_streak = max(longest_streak, current_streak)
        else:
            current_streak = 0
            current_streak_start = None
        previous_start = window_start
    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "observed_at_epoch": observed,
        "scope": "point-in-time live depth continuity progress",
        "paper_only": True,
        "real_orders_submitted": False,
        "criterion": "both tokens: first<=5s, last>=295s, max inter-snapshot gap<=60s",
        "required_complete_windows": 30,
        "observed_complete_windows": len(complete),
        "complete_closed_window_pct": (
            len(complete) / len(closed) * 100.0 if closed else None
        ),
        "longest_consecutive_complete_windows": longest_streak,
        "current_consecutive_complete_windows": current_streak,
        "current_streak_start_epoch": current_streak_start,
        "current_streak_end_epoch": (
            int(closed[-1]["window_start"]) if current_streak and closed else None
        ),
        "gap_remaining": max(0, 30 - len(complete)),
        "status": "verified-pass" if len(complete) >= 30 else "verified-fail",
        "closed_windows": len(closed),
        "incomplete_closed_windows": len(closed) - len(complete),
        "active_windows": len(details) - len(closed),
        "rows": rows,
        "malformed_rows": malformed,
        "rows_before_start_filter": before_start,
        "outside_window_rows": outside,
        "start_at_epoch": start_at_epoch,
        "files": [
            {
                "path": str(path),
                "bytes": limits[path],
                "sha256": _sha256_prefix(path, limits[path]),
                "point_in_time_byte_bound": True,
            }
            for path in paths
        ],
        "complete_windows": complete,
        "incomplete_windows": incomplete,
        "all_windows": details,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, action="append", required=True)
    parser.add_argument("--start-at-epoch", type=float)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = analyze(args.input, start_at_epoch=args.start_at_epoch)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_suffix(args.output.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(args.output)
    print(json.dumps({key: report[key] for key in (
        "checked_at",
        "rows",
        "closed_windows",
        "observed_complete_windows",
        "complete_closed_window_pct",
        "longest_consecutive_complete_windows",
        "current_consecutive_complete_windows",
        "incomplete_closed_windows",
        "active_windows",
        "gap_remaining",
        "status",
    )}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
