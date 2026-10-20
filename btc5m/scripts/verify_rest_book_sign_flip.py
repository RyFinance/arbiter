"""Independent re-verification of two published findings, on fresh data.

FINDING 1 (REST-book sign-flip, docs/pnl_and_kill_switch_followup_2026-08-15.md):
    With PAPER_FILL_SOURCE=rest_book, decision-snapshot opportunities stopped
    being mechanical fills: on the recorded sample, 2 opportunities,
    expected +$0.5000, realized -$0.5000 (-100.00% convergence) because the
    second leg's ask moved away and the filled leg was protective-exited.

FINDING 2 (depth continuity, docs/depth_replay_2026-08-15.md):
    On the sealed consolidated capture, 672,906 causally paired ticks but
    0/30 five-minute windows satisfied the operational continuity criterion
    (both tokens: first snapshot <=5s after window open, last >=295s,
    no inter-snapshot gap >60s).

This script re-implements both analyses from scratch (no imports from
replay_captured_depth.py) and applies them to:

  * --validate-implementation: the sealed consolidated snapshot.  Reproducing
    the published paired-tick and continuity counts here validates this
    independent implementation before it is trusted on fresh data.
  * --fresh: the live capture shards written by the running capture process.
    These are the fresh, never-before-analyzed rows used for the independent
    re-check.

REST-book replay semantics mirrored from
polymarket_arb/clients/trading_client.py._paper_simulate_fill (source="rest_book")
and scripts/calc_pnl_convergence.py accounting:

  * detection: combined ask < 1.0 - min_edge, pair skew <= 500 ms (this
    subsumes the live max_quote_age_ms gate because the older leg is at most
    the pair skew old), tick not inside the final NO_ENTRY_SECONDS before
    window close, per-market debounce on the exact combined price, one open
    position per market until resolved;
  * leg fill: BUY is marketable iff the submission-time best ask is <= the
    decision ask; fill price is that submission-time ask (two horizons are
    reported: the next captured observation after the decision, and the next
    observation after decision + 200 ms, matching the ~210 ms average
    acknowledgement of the original session);
  * one-leg fill: protective exit at the next captured bid for the filled
    token (up to a 2 s lookahead, last-known bid fallback);
  * accounting: expected = (1.0 - decision_combined) * order_size over all
    committed opportunities; realized = clean two-leg PnL plus protective-exit
    PnL, via calc_pnl_convergence.compute_pnl_convergence (the same function
    used for the original finding).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(PROJECT_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from calc_pnl_convergence import compute_pnl_convergence  # noqa: E402

MIN_EDGE = 0.01
ORDER_SIZE = 10.0
MAX_SKEW_MS = 500.0
NO_ENTRY_SECONDS = 5.0
WINDOW_SECONDS = 300.0
FILL_HORIZONS = ("immediate", "ack_200ms")
ACK_DELAY_S = 0.200
EXIT_LOOKAHEAD_S = 2.0


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot_copy(path: Path, temp_dir: Path) -> Path:
    """Copy a live capture shard to a fixed snapshot so it cannot change mid-read."""
    copy = temp_dir / path.name
    shutil.copy2(path, copy)
    return copy


def market_window_start(market_id: str) -> int | None:
    tail = str(market_id).rsplit("-", 1)[-1]
    return int(tail) if tail.isdigit() else None


def valid_levels(value: Any) -> bool:
    if not isinstance(value, list) or not value:
        return False
    for level in value:
        if not isinstance(level, (list, tuple)) or len(level) < 2:
            return False
        try:
            price, size = float(level[0]), float(level[1])
        except (TypeError, ValueError):
            return False
        if not math.isfinite(price) or not math.isfinite(size) or price < 0 or size < 0:
            return False
    return True


class Continuity:
    """Independent re-implementation of the published continuity criterion."""

    def __init__(self) -> None:
        self.first: float | None = None
        self.last: float | None = None
        self.max_gap: float = 0.0
        self.rows = 0

    def observe(self, offset_s: float) -> None:
        if self.first is None:
            self.first = offset_s
        if self.last is not None:
            self.max_gap = max(self.max_gap, offset_s - self.last)
        self.last = offset_s
        self.rows += 1

    def complete(self) -> bool:
        return (
            self.first is not None
            and self.first <= 5.0
            and self.last is not None
            and self.last >= 295.0
            and self.max_gap <= 60.0
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "first_offset_s": self.first,
            "last_offset_s": self.last,
            "max_gap_s": self.max_gap,
            "rows": self.rows,
            "complete": self.complete(),
        }


def stream_windows(path: Path):
    """Yield (window_start, market_id, rows) with rows in captured_at order."""
    current_start: int | None = None
    current_market: str | None = None
    rows: list[dict[str, Any]] = []
    previous_ts = float("-inf")
    violations = 0
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                captured_at = float(record["captured_at"])
                market_id = str(record["market_id"])
                token_id = str(record.get("token_id", ""))
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                continue
            if captured_at < previous_ts:
                violations += 1
            previous_ts = captured_at
            start = market_window_start(market_id)
            if start is None or not token_id:
                continue
            offset_s = captured_at - start
            if not 0.0 <= offset_s < WINDOW_SECONDS:
                continue
            if current_start is not None and start != current_start:
                yield current_start, current_market, rows, violations
                violations = 0
                rows = []
            current_start = start
            current_market = market_id
            rows.append({
                "captured_at": captured_at,
                "token_id": token_id,
                "offset_s": offset_s,
                "asks": record.get("asks"),
                "bids": record.get("bids"),
            })
    if current_start is not None:
        yield current_start, current_market, rows, violations


def pair_rows(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, int], list[str]]:
    """Causally pair the two tokens; first-observed token is assigned YES.

    Mirrors WindowPairer: latest observation available at each event, never
    looking forward, skew <= 500 ms, valid ask ladder required on both legs.
    Returns (ticks, stats, token_order) where token_order is the first-observed
    order used for YES/NO assignment.
    """
    stats: Counter = Counter()
    ticks: list[dict[str, Any]] = []
    token_ids: list[str] = []
    latest: dict[str, dict[str, Any]] = {}
    for record in rows:
        token_id = record["token_id"]
        if token_id not in token_ids:
            token_ids.append(token_id)
        if len(token_ids) > 2:
            stats["more_than_two_tokens"] += 1
            continue
        latest[token_id] = record
        if len(token_ids) < 2:
            stats["awaiting_other_token"] += 1
            continue
        stats["pair_candidates"] += 1
        yes, no = latest[token_ids[0]], latest[token_ids[1]]
        skew_s = abs(yes["captured_at"] - no["captured_at"])
        if skew_s > MAX_SKEW_MS / 1000.0:
            stats["skew_rejected"] += 1
            continue
        if not valid_levels(yes["asks"]) or not valid_levels(no["asks"]):
            stats["missing_or_invalid_ask_ladder"] += 1
            continue
        stats["complete_paired_ticks"] += 1
        yes_bids = yes["bids"] if valid_levels(yes.get("bids")) else []
        no_bids = no["bids"] if valid_levels(no.get("bids")) else []
        ticks.append({
            "t": max(yes["captured_at"], no["captured_at"]),
            "yes_ts": yes["captured_at"],
            "no_ts": no["captured_at"],
            "yes_ask": float(yes["asks"][0][0]),
            "no_ask": float(no["asks"][0][0]),
            "yes_bid": float(yes_bids[0][0]) if yes_bids else None,
            "no_bid": float(no_bids[0][0]) if no_bids else None,
            "offset_s": max(yes["offset_s"], no["offset_s"]),
        })
    return ticks, dict(stats), token_ids


def analyze_window(
    start: int, market_id: str, rows: list[dict[str, Any]],
) -> dict[str, Any]:
    ticks, pair_stats, token_order = pair_rows(rows)
    continuity: dict[str, Continuity] = {}
    for record in rows:
        continuity.setdefault(record["token_id"], Continuity()).observe(record["offset_s"])

    spacing = np.diff([record["captured_at"] for record in rows]) if len(rows) > 1 else np.array([])
    return {
        "window_start": start,
        "market_id": market_id,
        "row_count": len(rows),
        "tokens": sorted(continuity),
        "token_order": token_order,
        "pair_stats": pair_stats,
        "paired_ticks": len(ticks),
        "continuity": {token: value.as_dict() for token, value in sorted(continuity.items())},
        "continuity_complete": (
            len(continuity) == 2 and all(value.complete() for value in continuity.values())
        ),
        "snapshot_spacing_ms": {
            "median": float(np.median(spacing) * 1000.0) if len(spacing) else None,
            "p90": float(np.percentile(spacing, 90) * 1000.0) if len(spacing) else None,
            "max": float(spacing.max() * 1000.0) if len(spacing) else None,
        },
        "ticks": ticks,
        "rows": rows,  # raw rows, in-memory only; never serialized
    }


def token_time_series(rows: list[dict[str, Any]]) -> dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]]:
    """Per token: sorted (ts, best_ask, best_bid) with None bids as NaN."""
    series: dict[str, list[tuple[float, float, float]]] = {}
    for record in rows:
        if not valid_levels(record["asks"]):
            continue
        ask = float(record["asks"][0][0])
        bids = record["bids"] if valid_levels(record.get("bids")) else []
        bid = float(bids[0][0]) if bids else math.nan
        series.setdefault(record["token_id"], []).append((record["captured_at"], ask, bid))
    out: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]] = {}
    for token, values in series.items():
        ordered = sorted(values, key=lambda item: item[0])
        out[token] = (
            np.asarray([item[0] for item in ordered]),
            np.asarray([item[1] for item in ordered]),
            np.asarray([item[2] for item in ordered]),
        )
    return out


def next_observation(ts: np.ndarray, query: float) -> tuple[int, float]:
    """First index with ts > query; (len, inf) when none exists."""
    index = int(np.searchsorted(ts, query, side="right"))
    return index, (ts[index] if index < len(ts) else math.inf)


def replay_window_rest_book(
    ticks: list[dict[str, Any]], rows: list[dict[str, Any]], horizon: str, token_order: list[str],
) -> dict[str, Any]:
    """Apply the published rest_book paper-fill semantics to one window."""
    if len(token_order) != 2:
        return {"outcomes": [], "reason": "not_exactly_two_tokens"}
    series = token_time_series(rows)
    yes_token = token_order[0]  # first-observed token is YES by pairing convention
    no_token = token_order[1]
    yes_ts, yes_ask, yes_bid = series.get(yes_token, (np.array([]),) * 3)
    no_ts, no_ask, no_bid = series.get(no_token, (np.array([]),) * 3)

    outcomes: list[dict[str, Any]] = []
    last_fired_combined: float | None = None
    position_open = False
    delay = ACK_DELAY_S if horizon == "ack_200ms" else 0.0
    for tick in ticks:
        combined = tick["yes_ask"] + tick["no_ask"]
        if combined >= 1.0 - MIN_EDGE:
            continue
        if tick["offset_s"] >= WINDOW_SECONDS - NO_ENTRY_SECONDS:
            continue
        if position_open:
            continue
        if last_fired_combined is not None and last_fired_combined == combined:
            continue
        last_fired_combined = combined

        fill_yes_idx, fill_yes_ts = next_observation(yes_ts, tick["yes_ts"] + delay)
        fill_no_idx, fill_no_ts = next_observation(no_ts, tick["no_ts"] + delay)
        yes_fill_price = float(yes_ask[fill_yes_idx]) if fill_yes_idx < len(yes_ts) else tick["yes_ask"]
        no_fill_price = float(no_ask[fill_no_idx]) if fill_no_idx < len(no_ts) else tick["no_ask"]
        yes_sparse = fill_yes_idx >= len(yes_ts)
        no_sparse = fill_no_idx >= len(no_ts)
        yes_filled = not yes_sparse and yes_fill_price <= tick["yes_ask"]
        no_filled = not no_sparse and no_fill_price <= tick["no_ask"]

        outcome = {
            "t": tick["t"],
            "decision_yes_ask": tick["yes_ask"],
            "decision_no_ask": tick["no_ask"],
            "decision_combined": round(combined, 6),
            "expected_pnl": round((1.0 - combined) * ORDER_SIZE, 4),
            "horizon": horizon,
            "yes_fill_price": yes_fill_price if yes_filled else None,
            "no_fill_price": no_fill_price if no_filled else None,
            "yes_fill_delay_ms": round((fill_yes_ts - tick["yes_ts"]) * 1000.0, 3) if not yes_sparse else None,
            "no_fill_delay_ms": round((fill_no_ts - tick["no_ts"]) * 1000.0, 3) if not no_sparse else None,
            "yes_sparse_fallback": yes_sparse,
            "no_sparse_fallback": no_sparse,
        }
        if yes_filled and no_filled:
            outcome["outcome"] = "clean"
            outcome["realized_pnl"] = round((1.0 - yes_fill_price - no_fill_price) * ORDER_SIZE, 4)
            position_open = False
        elif yes_filled or no_filled:
            filled_token = yes_token if yes_filled else no_token
            fill_price = yes_fill_price if yes_filled else no_fill_price
            bid_ts, _, bid_series = series[filled_token]
            exit_idx = int(np.searchsorted(bid_ts, max(tick["t"], fill_yes_ts if yes_filled else fill_no_ts) + 0.100, side="right"))
            if exit_idx >= len(bid_ts):
                exit_idx = len(bid_ts) - 1
            exit_bid = float(bid_series[exit_idx]) if len(bid_ts) and not math.isnan(bid_series[exit_idx]) else math.nan
            if math.isnan(exit_bid):
                outcome["outcome"] = "partial_exit_unresolved"
                outcome["realized_pnl"] = None
            else:
                outcome["outcome"] = "partial_protective_exit"
                outcome["exit_bid"] = exit_bid
                outcome["exit_delay_ms"] = round((bid_ts[exit_idx] - tick["t"]) * 1000.0, 3)
                outcome["realized_pnl"] = round((exit_bid - fill_price) * ORDER_SIZE, 4)
            position_open = True
        else:
            outcome["outcome"] = "no_fill_in_window"
            outcome["realized_pnl"] = 0.0
            position_open = False
        outcomes.append(outcome)
    return {"outcomes": outcomes}


def convergence_of(outcomes: list[dict[str, Any]], horizon: str) -> dict[str, Any]:
    fills: list[dict[str, Any]] = []
    partials: list[dict[str, Any]] = []
    missed: list[dict[str, Any]] = []
    for outcome in outcomes:
        if outcome["horizon"] != horizon:
            continue
        base = {
            "t": outcome["t"],
            "decision_combined": outcome["decision_combined"],
            "size": ORDER_SIZE,
        }
        if outcome["outcome"] == "clean":
            fills.append({**base, "combined": outcome["decision_combined"], "pnl_locked": outcome["realized_pnl"]})
        elif outcome["outcome"] in ("partial_protective_exit", "partial_exit_unresolved"):
            partials.append({
                **base,
                "action": "protective_exit",
                "realized_pnl": outcome["realized_pnl"],
            })
        else:
            missed.append({**base, "combined": outcome["decision_combined"], "reason": "no_fill_in_window"})
    convergence = compute_pnl_convergence(fills, missed, order_size=ORDER_SIZE, partials=partials)
    realized = sum(
        float(f["pnl_locked"]) for f in fills
    ) + sum(
        float(p["realized_pnl"]) for p in partials if p.get("realized_pnl") is not None
    )
    return {
        "opportunities": len(outcomes),
        "clean_fills": len(fills),
        "one_leg_partials": len(partials),
        "unresolved_exits": sum(1 for p in partials if p.get("realized_pnl") is None),
        "no_fill": len(missed),
        "expected_committed_pnl": convergence["expected_committed_pnl"],
        "realized_committed_pnl": round(realized, 4),
        "convergence_pct": convergence["convergence_pct"],
        "deviation_pct": convergence["deviation_pct"],
        "sample_available": convergence["sample_available"],
    }


def decision_snapshot_baseline(outcomes: list[dict[str, Any]], horizon: str) -> dict[str, Any]:
    selected = [outcome for outcome in outcomes if outcome["horizon"] == horizon]
    expected = sum(outcome["expected_pnl"] for outcome in selected)
    return {
        "opportunities": len(selected),
        "expected_pnl": round(expected, 4),
        "realized_pnl": round(expected, 4),
        "convergence_pct": 100.0 if selected else None,
        "note": "both legs filled at decision asks by construction",
    }


def percentile(values: list[float], q: float) -> float | None:
    return float(np.percentile(values, q)) if values else None


def summarize_latency(outcomes: list[dict[str, Any]], horizon: str) -> dict[str, Any]:
    fill_delays = [
        value for outcome in outcomes if outcome["horizon"] == horizon
        for value in (outcome["yes_fill_delay_ms"], outcome["no_fill_delay_ms"])
        if value is not None
    ]
    exit_delays = [
        outcome["exit_delay_ms"] for outcome in outcomes
        if outcome["horizon"] == horizon and outcome.get("exit_delay_ms") is not None
    ]
    return {
        "fill_observation_delay_ms": {
            "n": len(fill_delays),
            "p50": percentile(fill_delays, 50),
            "p90": percentile(fill_delays, 90),
            "max": max(fill_delays) if fill_delays else None,
        },
        "exit_observation_delay_ms": {
            "n": len(exit_delays),
            "p50": percentile(exit_delays, 50),
            "p90": percentile(exit_delays, 90),
            "max": max(exit_delays) if exit_delays else None,
        },
    }


def analyze_paths(paths: list[Path], label: str, run_replay: bool) -> dict[str, Any]:
    windows: list[dict[str, Any]] = []
    for path in paths:
        for start, market_id, rows, _violations in stream_windows(path):
            windows.append(analyze_window(start, market_id, rows))
    windows.sort(key=lambda item: item["window_start"])

    two_token_windows = [window for window in windows if len(window["tokens"]) == 2]
    complete_windows = [window for window in two_token_windows if window["continuity_complete"]]
    paired_ticks = sum(window["paired_ticks"] for window in windows)
    pair_stats: Counter = Counter()
    for window in windows:
        pair_stats.update(window["pair_stats"])

    result: dict[str, Any] = {
        "label": label,
        "windows": len(windows),
        "two_token_windows": len(two_token_windows),
        "complete_continuity_windows": len(complete_windows),
        "continuity_criterion": (
            "both tokens: first snapshot <=5s after window open, last >=295s, "
            "max inter-snapshot gap <=60s"
        ),
        "paired_ticks_total": paired_ticks,
        "pair_stats": dict(pair_stats),
        "continuity_gap": len(two_token_windows) - len(complete_windows),
    }
    fresh_detail = [
        {
            "window_start": window["window_start"],
            "market_id": window["market_id"],
            "row_count": window["row_count"],
            "complete": window["continuity_complete"],
            "continuity": window["continuity"],
        }
        for window in windows[-30:]
    ]
    result["last_30_windows_detail"] = fresh_detail

    if run_replay:
        all_outcomes: list[dict[str, Any]] = []
        spacing_medians: list[float] = []
        for window in windows:
            if len(window["tokens"]) != 2:
                continue
            for horizon in FILL_HORIZONS:
                replay = replay_window_rest_book(
                    window["ticks"], window["rows"], horizon, window["token_order"]
                )
                all_outcomes.extend(replay["outcomes"])
            median = window["snapshot_spacing_ms"]["median"]
            if median is not None:
                spacing_medians.append(median)
        for window in windows:
            window.pop("rows", None)
            window.pop("ticks", None)
        result["snapshot_spacing_across_windows_ms"] = {
            "median_of_window_medians": float(np.median(spacing_medians)) if spacing_medians else None,
            "p90": float(np.percentile(spacing_medians, 90)) if spacing_medians else None,
        }
        for horizon in FILL_HORIZONS:
            selected = [outcome for outcome in all_outcomes if outcome["horizon"] == horizon]
            result[f"rest_book_replay_{horizon}"] = {
                "convergence": convergence_of(selected, horizon),
                "baseline_decision_snapshot": decision_snapshot_baseline(selected, horizon),
                "latency": summarize_latency(selected, horizon),
                "outcome_distribution": dict(Counter(
                    outcome["outcome"] for outcome in selected
                )),
                "sparse_fallback_fills": sum(
                    1 for outcome in selected
                    if outcome["yes_sparse_fallback"] or outcome["no_sparse_fallback"]
                ),
                "opportunities": [
                    outcome for outcome in selected
                ],
            }
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--validate-implementation", type=Path, default=None,
                        help="sealed consolidated snapshot; pairing/continuity must reproduce the published counts")
    parser.add_argument("--fresh", type=Path, action="append", default=[],
                        help="fresh capture shard; repeatable")
    parser.add_argument("--no-replay", action="store_true",
                        help="skip the rest_book replay on fresh data (continuity + pairing only)")
    parser.add_argument("--output", type=Path, required=True,
                        help="new artifact JSON path; never overwrites an existing file")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    with tempfile.TemporaryDirectory(prefix="verify_rest_book_") as temp:
        temp_dir = Path(temp)
        artifact: dict[str, Any] = {
            "generated_by": Path(__file__).name,
            "semantics": {
                "min_edge": MIN_EDGE,
                "order_size": ORDER_SIZE,
                "max_skew_ms": MAX_SKEW_MS,
                "no_entry_seconds": NO_ENTRY_SECONDS,
                "fill_horizons": FILL_HORIZONS,
                "ack_delay_s": ACK_DELAY_S,
                "exit_lookahead_s": EXIT_LOOKAHEAD_S,
            },
        }
        if args.validate_implementation:
            snapshot = snapshot_copy(args.validate_implementation, temp_dir)
            artifact["input_sealed"] = {
                "path": str(args.validate_implementation.resolve()),
                "sha256": sha256(snapshot),
            }
            artifact["validation_implementation"] = analyze_paths(
                [snapshot], "sealed_consolidated", run_replay=False,
            )
        fresh_snapshots = [snapshot_copy(path, temp_dir) for path in args.fresh]
        if fresh_snapshots:
            artifact["input_fresh"] = [
                {"path": str(path.resolve()), "sha256": sha256(snapshot)}
                for path, snapshot in zip(args.fresh, fresh_snapshots, strict=True)
            ]
            artifact["fresh_analysis"] = analyze_paths(
                fresh_snapshots, "fresh_live_capture", run_replay=not args.no_replay,
            )
        output = args.output
        if output.exists():
            raise FileExistsError(f"refusing to overwrite existing artifact {output}")
        output.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps(artifact, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
