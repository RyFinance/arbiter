"""Lightweight latency profile runner.

Runs PAPER_TRADE mode long enough to capture >=50 FIRE events with per-stage
timing from the execution.py EXEC_TIMING log lines. Extracts avg/p50/p95
per stage and writes results to state/latency_profile.json.

Usage:
    PAPER_TRADE=true DRY_RUN=false python -m polymarket_arb.main &
    python scripts/run_latency_profile.py --duration 120
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

def parse_exec_timing(log_path: Path) -> dict:
    """Parse EXEC_TIMING lines and compute per-stage percentiles."""
    stages = {
        "opp_to_order": [],
        "reserve": [],
        "dispatch": [],
        "dual_submit": [],
        "paper_leg_max": [],
        "post_submit": [],
    }
    fire_count = 0
    with open(log_path) as f:
        for line in f:
            if "EXEC_TIMING" in line:
                m = re.search(
                    r'opp2first_ms=([\d.]+).*reserve_ms=([\d.]+)'
                    r'(?:.*dispatch_ms=([\d.]+))?'
                    r'.*dual_submit_ms=([\d.]+).*paper_leg_max_ms=([\d.]+).*post_submit_ms=([\d.]+)',
                    line
                )
                if m:
                    stages["opp_to_order"].append(float(m.group(1)))
                    stages["reserve"].append(float(m.group(2)))
                    if m.group(3) is not None:
                        stages["dispatch"].append(float(m.group(3)))
                    stages["dual_submit"].append(float(m.group(4)))
                    stages["paper_leg_max"].append(float(m.group(5)))
                    stages["post_submit"].append(float(m.group(6)))
            if '"msg":"FIRE' in line:
                fire_count += 1

    result = {"fire_count": fire_count, "stages": {}}
    for name, vals in stages.items():
        if vals:
            vals_sorted = sorted(vals)
            n = len(vals)
            result["stages"][name] = {
                "n": n,
                "avg_ms": round(sum(vals) / n, 3),
                "p50_ms": round(vals_sorted[int(n * 0.50) - 1] if n > 0 else 0.0, 3),
                "p95_ms": round(vals_sorted[int(n * 0.95) - 1] if n > 0 else 0.0, 3),
                "max_ms": round(max(vals), 3),
            }
    return result


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=int, default=120,
                       help="Seconds to wait for log data")
    parser.add_argument("--log", default="state/bot.jsonl")
    parser.add_argument("--output", default="state/latency_profile.json")
    args = parser.parse_args()

    print(f"Waiting up to {args.duration}s for FIRE events in {args.log}...")
    start = time.time()
    while time.time() - start < args.duration:
        log_path = Path(args.log)
        if log_path.exists():
            result = parse_exec_timing(log_path)
            if result["fire_count"] >= 1:
                print(f"  Captured {result['fire_count']} FIRE events")
                for name, stats in result["stages"].items():
                    print(f"    {name:>16s}: n={stats['n']:4d} "
                          f"avg={stats['avg_ms']:.3f}ms "
                          f"p50={stats['p50_ms']:.3f}ms "
                          f"p95={stats['p95_ms']:.3f}ms "
                          f"max={stats['max_ms']:.3f}ms")
                output = Path(args.output)
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(json.dumps(result, indent=2))
                print(f"\nWritten to {args.output}")
                if result["fire_count"] >= 5:
                    return
        time.sleep(2)

    # Final read
    log_path = Path(args.log)
    if log_path.exists():
        result = parse_exec_timing(log_path)
        print(f"\nFinal: {result['fire_count']} FIRE events captured")
        for name, stats in result["stages"].items():
            print(f"  {name:>16s}: n={stats['n']:4d} "
                  f"avg={stats['avg_ms']:.3f}ms p50={stats['p50_ms']:.3f}ms "
                  f"p95={stats['p95_ms']:.3f}ms max={stats['max_ms']:.3f}ms")
        Path(args.output).write_text(json.dumps(result, indent=2))
    else:
        print(f"No log file found at {args.log}")


if __name__ == "__main__":
    main()
