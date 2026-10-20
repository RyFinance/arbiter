#!/usr/bin/env python3
"""Measure transient cross-leg arbitrage signals in captured real order books.

This is a read-only diagnostic.  It replays each full-depth snapshot in capture
order, reproduces the bot's "latest quote from each leg" view, and then checks
whether an apparent edge disappears on the next update for the older leg.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.replay_captured_depth import market_window_start  # noqa: E402


def _summary(values: list[float]) -> dict[str, Any]:
    values = sorted(value for value in values if math.isfinite(value))
    if not values:
        return {"n": 0, "avg": None, "min": None, "p50": None, "p95": None,
                "p99": None, "max": None}

    def percentile(q: float) -> float:
        return values[min(len(values) - 1, int(len(values) * q))]

    return {
        "n": len(values),
        "avg": sum(values) / len(values),
        "min": values[0],
        "p50": percentile(0.50),
        "p95": percentile(0.95),
        "p99": percentile(0.99),
        "max": values[-1],
    }


def _best_ask(record: dict[str, Any]) -> float | None:
    prices: list[float] = []
    for level in record.get("asks") or []:
        try:
            price = float(level[0] if isinstance(level, (list, tuple)) else level["price"])
        except (IndexError, KeyError, TypeError, ValueError):
            continue
        if math.isfinite(price):
            prices.append(price)
    return min(prices) if prices else None


def _sha256_prefix(path: Path, limit: int) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        remaining = limit
        while remaining:
            block = handle.read(min(1024 * 1024, remaining))
            if not block:
                break
            digest.update(block)
            remaining -= len(block)
    return digest.hexdigest()


def analyze(
    paths: list[Path],
    *,
    start_at_epoch: float | None,
    edge_price: float = 0.99,
    max_quote_age_ms: float = 500.0,
    transient_horizon_ms: float = 50.0,
    settle_ms: float = 10.0,
) -> dict[str, Any]:
    limits = {path: path.stat().st_size for path in paths}
    events: dict[str, list[dict[str, Any]]] = defaultdict(list)
    malformed = before_start = outside_window = rows = 0
    for path in paths:
        with path.open("rb") as handle:
            while handle.tell() < limits[path]:
                line = handle.readline()
                if not line or handle.tell() > limits[path]:
                    break
                try:
                    record = json.loads(line)
                    captured_at = float(record["captured_at"])
                    market_id = str(record["market_id"])
                    token_id = str(record["token_id"])
                    ask = _best_ask(record)
                except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                    malformed += 1
                    continue
                if start_at_epoch is not None and captured_at < start_at_epoch:
                    before_start += 1
                    continue
                start = market_window_start(market_id)
                if start is None or not 0.0 <= captured_at - start < 300.0:
                    outside_window += 1
                    continue
                if ask is None or not token_id:
                    malformed += 1
                    continue
                rows += 1
                events[market_id].append(
                    {"captured_at": captured_at, "token_id": token_id, "ask": ask}
                )

    candidate_skews: list[float] = []
    paired_arrival_skews: list[float] = []
    transient_delays: list[float] = []
    candidates: list[dict[str, Any]] = []
    settled_candidates: list[dict[str, Any]] = []
    pair_barrier_candidates: list[dict[str, Any]] = []
    pair_barrier_incomplete_timeouts = 0
    token_counts: dict[str, int] = {}
    for market_id, market_events in sorted(events.items()):
        market_events.sort(key=lambda event: event["captured_at"])
        token_ids = sorted({event["token_id"] for event in market_events})
        token_counts[market_id] = len(token_ids)
        if len(token_ids) != 2:
            continue
        next_index: dict[str, int | None] = {token: None for token in token_ids}
        next_for_row: list[int | None] = [None] * len(market_events)
        for index in range(len(market_events) - 1, -1, -1):
            token = market_events[index]["token_id"]
            other = token_ids[0] if token == token_ids[1] else token_ids[1]
            next_for_row[index] = next_index[other]
            next_index[token] = index

        latest: dict[str, dict[str, Any]] = {}
        previous: dict[str, Any] | None = None
        for index, event in enumerate(market_events):
            if previous is not None and previous["token_id"] != event["token_id"]:
                arrival_gap = (event["captured_at"] - previous["captured_at"]) * 1000.0
                if 0.0 <= arrival_gap <= transient_horizon_ms:
                    paired_arrival_skews.append(arrival_gap)
            previous = event
            latest[event["token_id"]] = event
            if len(latest) != 2:
                continue
            two = [latest[token] for token in token_ids]
            combined = two[0]["ask"] + two[1]["ask"]
            older = min(two, key=lambda quote: quote["captured_at"])
            newer = max(two, key=lambda quote: quote["captured_at"])
            skew_ms = (newer["captured_at"] - older["captured_at"]) * 1000.0
            age_ms = (event["captured_at"] - older["captured_at"]) * 1000.0
            if combined >= edge_price or age_ms > max_quote_age_ms:
                continue
            candidate_skews.append(skew_ms)
            next_other_index = next_for_row[index]
            next_other = (
                market_events[next_other_index] if next_other_index is not None else None
            )
            delay_ms = (
                (next_other["captured_at"] - event["captured_at"]) * 1000.0
                if next_other is not None
                else None
            )
            corrected_combined = (
                event["ask"] + next_other["ask"] if next_other is not None else None
            )
            transient = bool(
                delay_ms is not None
                and 0.0 <= delay_ms <= transient_horizon_ms
                and corrected_combined is not None
                and corrected_combined >= edge_price
            )
            if transient and delay_ms is not None:
                transient_delays.append(delay_ms)
            candidates.append(
                {
                    "market_id": market_id,
                    "captured_at": event["captured_at"],
                    "updated_token": event["token_id"],
                    "updated_ask": event["ask"],
                    "older_token": older["token_id"],
                    "older_ask": older["ask"],
                    "combined": combined,
                    "pair_skew_ms": skew_ms,
                    "next_other_delay_ms": delay_ms,
                    "next_other_ask": next_other["ask"] if next_other else None,
                    "corrected_combined": corrected_combined,
                    "transient_within_horizon": transient,
                }
            )

        # Reproduce the current runtime's fixed 10ms coalescing timer. The first
        # update opens a settle bucket; adjacent updates replace state without
        # extending its deadline. Evaluation occurs once at the deadline.
        settled_latest: dict[str, dict[str, Any]] = {}
        deadline: float | None = None

        def evaluate_settled(at: float) -> None:
            if len(settled_latest) != 2:
                return
            two = [settled_latest[token] for token in token_ids]
            combined = two[0]["ask"] + two[1]["ask"]
            older = min(two, key=lambda quote: quote["captured_at"])
            age_ms = (at - older["captured_at"]) * 1000.0
            if combined < edge_price and age_ms <= max_quote_age_ms:
                settled_candidates.append(
                    {
                        "market_id": market_id,
                        "evaluated_at": at,
                        "combined": combined,
                        "age_ms": age_ms,
                        "pair_skew_ms": abs(
                            two[0]["captured_at"] - two[1]["captured_at"]
                        ) * 1000.0,
                    }
                )

        for event in market_events:
            if deadline is not None and deadline <= event["captured_at"]:
                evaluate_settled(deadline)
                deadline = None
            settled_latest[event["token_id"]] = event
            if deadline is None:
                deadline = event["captured_at"] + settle_ms / 1000.0
        if deadline is not None:
            evaluate_settled(deadline)

        # Reproduce the final runtime barrier: no strategy evaluation is emitted
        # until both distinct token books have updated inside the settle envelope.
        # A one-sided update set is discarded at timeout rather than combined with
        # the prior opposite-leg quote.
        barrier_state: dict[str, dict[str, Any]] = {}
        barrier_tokens: set[str] = set()
        barrier_deadline: float | None = None
        for event in market_events:
            if barrier_deadline is not None and barrier_deadline <= event["captured_at"]:
                if len(barrier_tokens) < 2:
                    pair_barrier_incomplete_timeouts += 1
                barrier_tokens.clear()
                barrier_deadline = None
            barrier_state[event["token_id"]] = event
            barrier_tokens.add(event["token_id"])
            barrier_deadline = event["captured_at"] + settle_ms / 1000.0
            if len(barrier_tokens) < 2:
                continue
            two = [barrier_state[token] for token in token_ids]
            combined = two[0]["ask"] + two[1]["ask"]
            older = min(two, key=lambda quote: quote["captured_at"])
            age_ms = (event["captured_at"] - older["captured_at"]) * 1000.0
            if combined < edge_price and age_ms <= max_quote_age_ms:
                pair_barrier_candidates.append(
                    {
                        "market_id": market_id,
                        "evaluated_at": event["captured_at"],
                        "combined": combined,
                        "age_ms": age_ms,
                        "pair_skew_ms": abs(
                            two[0]["captured_at"] - two[1]["captured_at"]
                        ) * 1000.0,
                    }
                )
            barrier_tokens.clear()
            barrier_deadline = None
        if barrier_tokens:
            pair_barrier_incomplete_timeouts += 1

    transient_count = sum(row["transient_within_horizon"] for row in candidates)
    over_horizon_skew = sum(
        float(row["pair_skew_ms"]) > transient_horizon_ms for row in candidates
    )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": "read-only real-depth cross-leg skew diagnostic",
        "paper_only": True,
        "real_orders_submitted": False,
        "configuration": {
            "edge_price": edge_price,
            "max_quote_age_ms": max_quote_age_ms,
            "transient_horizon_ms": transient_horizon_ms,
            "settle_ms": settle_ms,
            "start_at_epoch": start_at_epoch,
        },
        "inputs": [
            {
                "path": str(path),
                "bytes": limits[path],
                "sha256": _sha256_prefix(path, limits[path]),
                "point_in_time_byte_bound": True,
            }
            for path in paths
        ],
        "rows": rows,
        "malformed_rows": malformed,
        "rows_before_start_filter": before_start,
        "outside_window_rows": outside_window,
        "markets": len(events),
        "market_token_counts": token_counts,
        "paired_arrival_skew_ms": _summary(paired_arrival_skews),
        "apparent_edge_candidates": len(candidates),
        "candidate_pair_skew_ms": _summary(candidate_skews),
        "candidates_over_horizon_skew": over_horizon_skew,
        "transient_candidates_within_horizon": transient_count,
        "transient_candidate_pct": (
            transient_count / len(candidates) * 100.0 if candidates else None
        ),
        "transient_resolution_delay_ms": _summary(transient_delays),
        "persistent_or_unresolved_candidates": len(candidates) - transient_count,
        "settled_edge_candidates": len(settled_candidates),
        "settled_candidate_examples": settled_candidates[:100],
        "pair_barrier_edge_candidates": len(pair_barrier_candidates),
        "pair_barrier_candidate_examples": pair_barrier_candidates[:100],
        "pair_barrier_incomplete_timeouts": pair_barrier_incomplete_timeouts,
        "examples": candidates[:100],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, action="append", required=True)
    parser.add_argument("--start-at-epoch", type=float)
    parser.add_argument("--edge-price", type=float, default=0.99)
    parser.add_argument("--max-quote-age-ms", type=float, default=500.0)
    parser.add_argument("--transient-horizon-ms", type=float, default=50.0)
    parser.add_argument("--settle-ms", type=float, default=10.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = analyze(
        args.input,
        start_at_epoch=args.start_at_epoch,
        edge_price=args.edge_price,
        max_quote_age_ms=args.max_quote_age_ms,
        transient_horizon_ms=args.transient_horizon_ms,
        settle_ms=args.settle_ms,
    )
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_suffix(args.output.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(args.output)
    print(json.dumps({
        "rows": report["rows"],
        "markets": report["markets"],
        "apparent_edge_candidates": report["apparent_edge_candidates"],
        "transient_candidates_within_horizon": report["transient_candidates_within_horizon"],
        "transient_candidate_pct": report["transient_candidate_pct"],
        "settled_edge_candidates": report["settled_edge_candidates"],
        "pair_barrier_edge_candidates": report["pair_barrier_edge_candidates"],
        "candidate_pair_skew_ms": report["candidate_pair_skew_ms"],
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
