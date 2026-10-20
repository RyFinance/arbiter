"""Completeness report for captured depth-ladder data.

Analyzes the JSONL files under ``state/depth_ladders/<market_id>/`` and reports:
  - total snapshots captured
  - per-file snapshot counts, time spans, and detected gaps (> 60s)
  - per-token update counts
  - overall completeness percentage

Usage:
    python scripts/verify_depth_capture.py
    python scripts/verify_depth_capture.py --market-id <market_id>
    python scripts/verify_depth_capture.py --depth-root state/depth_ladders
    python scripts/verify_depth_capture.py --print
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DEPTH_ROOT = PROJECT_ROOT / "state" / "depth_ladders"
GAP_THRESHOLD_S = 60.0  # gaps larger than this are "dropped updates"


def verify_depth_capture(depth_root: str | Path = DEFAULT_DEPTH_ROOT,
                         market_id: str | None = None) -> dict:
    """Analyze captured depth files and report completeness.

    Returns a dict with:
      - total_snapshots: count of book update lines across all files
      - file_count: number of .jsonl files scanned
      - files: list of per-file stats {path, snapshots, duration_s, gap_count, max_gap_s}
      - total_gaps: count of gaps > GAP_THRESHOLD_S
      - gaps: first 50 gap records (truncated for report)
      - tokens: per-token update counts
      - completeness_pct: fraction of files with zero gaps
    """
    root = Path(depth_root)
    if not root.exists():
        return {
            "error": f"depth root does not exist: {root}",
            "total_snapshots": 0,
            "file_count": 0,
            "files": [],
            "total_gaps": 0,
            "gaps": [],
            "tokens": {},
            "completeness_pct": 0.0,
        }

    market_dirs = [root / market_id] if market_id else [
        d for d in root.iterdir() if d.is_dir()
    ]

    all_files = []
    all_snapshots = 0
    all_gaps = []
    token_counts: dict[str, int] = defaultdict(int)

    for mdir in sorted(market_dirs):
        if not mdir.is_dir():
            continue
        for jf in sorted(mdir.rglob("*.jsonl")):
            snapshots = []
            try:
                with open(jf) as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                            ts = rec.get("ts", 0)
                            token = rec.get("token_id", "")
                            snapshots.append((ts, token))
                            if token:
                                token_counts[token] += 1
                        except json.JSONDecodeError:
                            continue
            except OSError:
                continue

            if not snapshots:
                continue

            timestamps = sorted([t for t, _ in snapshots if t > 0])
            first_ts = timestamps[0]
            last_ts = timestamps[-1]
            duration_s = last_ts - first_ts

            # Detect gaps > threshold.
            gaps = []
            for i in range(1, len(timestamps)):
                gap = timestamps[i] - timestamps[i - 1]
                if gap > GAP_THRESHOLD_S:
                    gaps.append({
                        "from_ts": round(timestamps[i - 1], 3),
                        "to_ts": round(timestamps[i], 3),
                        "gap_s": round(gap, 3),
                        "from_wall": datetime.fromtimestamp(timestamps[i - 1], tz=timezone.utc).isoformat(),
                        "to_wall": datetime.fromtimestamp(timestamps[i], tz=timezone.utc).isoformat(),
                    })

            max_gap = max([g["gap_s"] for g in gaps], default=0)
            try:
                rel_path = str(jf.relative_to(PROJECT_ROOT))
            except ValueError:
                rel_path = str(jf)
            all_files.append({
                "path": rel_path,
                "snapshots": len(snapshots),
                "duration_s": round(duration_s, 1),
                "max_gap_s": round(max_gap, 3),
                "gap_count": len(gaps),
            })
            all_snapshots += len(snapshots)
            all_gaps.extend(gaps)

    # Completeness: fraction of files with zero gaps.
    completeness = 0.0
    if all_files:
        intact = sum(1 for f in all_files if f["gap_count"] == 0)
        completeness = intact / len(all_files) * 100.0

    return {
        "total_snapshots": all_snapshots,
        "file_count": len(all_files),
        "files": all_files,
        "total_gaps": len(all_gaps),
        "gaps": all_gaps[:50],
        "tokens": dict(token_counts),
        "completeness_pct": round(completeness, 2),
    }


def main(argv: list[str] | None = None) -> int:
    argv = argv or sys.argv[1:]
    parser = argparse.ArgumentParser(
        description="Verify depth-ladder capture completeness."
    )
    parser.add_argument("--depth-root", default=str(DEFAULT_DEPTH_ROOT),
                        help="Root directory of captured depth files")
    parser.add_argument("--market-id", default=None,
                        help="Restrict to one market_id")
    parser.add_argument("--print", action="store_true",
                        help="Print report to stdout in addition to returning it")
    args = parser.parse_args(argv)

    report = verify_depth_capture(args.depth_root, args.market_id)

    if args.print or not sys.stdout.isatty():
        print(json.dumps(report, indent=2, default=str))

    print(f"\n{'='*60}")
    print(f"Depth Capture Completeness Report")
    print(f"{'='*60}")
    print(f"  Files scanned:       {report['file_count']}")
    print(f"  Total snapshots:     {report['total_snapshots']}")
    print(f"  Total gaps (>60s):   {report['total_gaps']}")
    print(f"  Completeness:        {report['completeness_pct']}% of files with zero gaps")
    if report.get("tokens"):
        print(f"  Tokens captured:")
        for tok, count in sorted(report["tokens"].items()):
            print(f"    {tok[:20]}...: {count} updates")
    if report.get("error"):
        print(f"  ERROR: {report['error']}")
    print(f"{'='*60}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
