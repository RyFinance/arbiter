#!/usr/bin/env python3
"""Summarize independent paper-session archives without treating paper fills as live proof."""
from __future__ import annotations

import argparse
import json
import re
import statistics
from pathlib import Path
from typing import Any


PAIR_FILL_RE = re.compile(
    r"\bYES\s+(?P<yes>[\d.]+)\s+NO\s+(?P<no>[\d.]+)\s+SUM\s+(?P<combined>[\d.]+)\s+EDGE\s+(?P<edge>[\d.]+)"
)


def _events(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _summary(events: list[dict[str, Any]]) -> dict[str, float | int]:
    message = next((str(row.get("msg", "")) for row in reversed(events)
                    if "session metrics:" in str(row.get("msg", ""))), "")
    patterns = {
        "opportunities": r"opps=(\d+)", "fills": r"fills=(\d+)",
        "missed": r"missed=(\d+)", "locked_pnl": r"locked_pnl=(-?[\d.]+)",
        "would_lock": r"would_lock=(-?[\d.]+)", "opp_to_order_avg_ms": r"opp2order_avg_ms=([\d.]+)",
        "opp_to_order_p50_ms": r"p50_ms=([\d.]+)", "opp_to_order_p95_ms": r"p95_ms=([\d.]+)",
        "opp_to_order_p99_ms": r"p99_ms=([\d.]+)",
    }
    result: dict[str, float | int] = {}
    for key, pattern in patterns.items():
        match = re.search(pattern, message)
        if match:
            result[key] = int(match.group(1)) if key in {"opportunities", "fills", "missed"} else float(match.group(1))
    return result


def fill_samples(events: list[dict[str, Any]], order_size: float = 10.0) -> list[dict[str, Any]]:
    """Pair each clean pair-fill with its detector line and intervening paper legs."""
    detected: list[dict[str, Any]] = []
    samples: list[dict[str, Any]] = []
    legs: list[float] = []
    for row in events:
        message = str(row.get("msg", ""))
        match = PAIR_FILL_RE.search(message)
        if match:
            detected.append({
                "market_id": re.search(r"^(\S+)\s+YES", message).group(1),
                "yes": float(match.group("yes")), "no": float(match.group("no")),
                "combined": float(match.group("combined")), "edge": float(match.group("edge")),
            })
            legs = []
            continue
        if row.get("event") != "TRADE" or message != "filled":
            continue
        data = row.get("data") or {}
        market_id = str(data.get("market_id", ""))
        if data.get("side") == "BUY":
            legs.append(float(data.get("fill_price", data.get("combined", 0.0))))
            continue
        if not market_id.startswith("btc-updown-"):
            continue
        detector = next((item for item in reversed(detected) if item["market_id"] == market_id), None)
        if detector is None:
            continue
        captured_combined = sum(legs[-2:]) if len(legs) >= 2 else None
        samples.append({
            "market_id": market_id,
            "detected_legs": [detector["yes"], detector["no"]],
            "detected_combined": detector["combined"],
            "detected_edge_dollars": round((1.0 - detector["combined"]) * order_size, 4),
            "paper_fill_legs": legs[-2:],
            "captured_combined": round(captured_combined, 4) if captured_combined is not None else None,
            "captured_edge_dollars": round((1.0 - captured_combined) * order_size, 4) if captured_combined is not None else None,
            "recorded_locked_pnl": float(data.get("pnl_locked", 0.0)),
            "paper_fill_price_matches_detection": captured_combined is not None and abs(captured_combined - detector["combined"]) < 1e-9,
        })
    return samples


def session_report(label: str, path: Path) -> dict[str, Any]:
    events = _events(path)
    summary = _summary(events)
    miss_reasons: dict[str, int] = {}
    for event in events:
        if event.get("event") == "TRADE" and event.get("msg") == "missed":
            reason = str((event.get("data") or {}).get("reason", "unspecified"))
            miss_reasons[reason] = miss_reasons.get(reason, 0) + 1
    locked = float(summary.get("locked_pnl", 0.0))
    would_lock = float(summary.get("would_lock", 0.0))
    expected = locked + would_lock
    convergence = 100.0 * locked / expected if expected else None
    return {
        "label": label,
        "source": str(path),
        "summary": summary,
        "miss_reasons": miss_reasons,
        "all_detected_edge_capture_pct": round(convergence, 2) if convergence is not None else None,
        "within_10pct_target": convergence is not None and abs(convergence - 100.0) <= 10.0,
        "fill_samples": fill_samples(events),
    }


def aggregate(sessions: list[dict[str, Any]]) -> dict[str, Any]:
    def numbers(key: str) -> list[float]:
        return [float(session["summary"][key]) for session in sessions if key in session["summary"]]

    locked = numbers("locked_pnl")
    would_lock = numbers("would_lock")
    expected = sum(locked) + sum(would_lock)
    convergence_values = [float(session["all_detected_edge_capture_pct"]) for session in sessions
                          if session["all_detected_edge_capture_pct"] is not None]
    return {
        "session_count": len(sessions),
        "opportunities": int(sum(numbers("opportunities"))),
        "fills": int(sum(numbers("fills"))),
        "missed": int(sum(numbers("missed"))),
        "locked_pnl": round(sum(locked), 4),
        "would_lock_pnl": round(sum(would_lock), 4),
        "all_detected_edge_capture_pct": round(100.0 * sum(locked) / expected, 2) if expected else None,
        "within_10pct_target": bool(expected) and abs(100.0 * sum(locked) / expected - 100.0) <= 10.0,
        "convergence_session_mean_pct": round(statistics.mean(convergence_values), 2) if convergence_values else None,
        "convergence_session_variance_pct2": round(statistics.pvariance(convergence_values), 4) if convergence_values else None,
        "locked_pnl_session_variance": round(statistics.pvariance(locked), 6) if locked else None,
        "opp_to_order_p95_ms_values": numbers("opp_to_order_p95_ms"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", action="append", required=True, metavar="LABEL=JSONL")
    parser.add_argument("--output", type=Path, default=Path("state/paper_session_aggregate_report.json"))
    args = parser.parse_args()
    sessions = []
    for spec in args.session:
        label, separator, raw_path = spec.partition("=")
        if not separator:
            raise ValueError("--session must be LABEL=JSONL")
        sessions.append(session_report(label, Path(raw_path)))
    report = {
        "paper_only": True,
        "live_execution_proof": False,
        "target_claim": "not_proven_by paper-only simulation",
        "sessions": sessions,
        "aggregate": aggregate(sessions),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
