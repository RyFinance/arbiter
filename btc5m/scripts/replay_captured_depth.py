"""Replay retained real Polymarket ladders through the strict depth model.

The consolidated capture stores one token per row.  This adapter constructs
causal, as-of pairs for the two tokens in each five-minute market, preserves the
actual receipt-time skew between the legs, and replays each market independently
through ``BacktestEngine(fill_model="depth_snapshot")``.  It never submits a
network order.

The row-count gate and the operational continuity gate are deliberately reported
separately.  A large number of partial-window snapshots is enough to exercise the
fill model, but it is not evidence for the documented requirement of 30 complete
five-minute windows.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sys
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from polymarket_arb.backtest_engine import (  # noqa: E402
    BacktestConfig,
    BacktestEngine,
    depth_snapshot_availability,
)
from polymarket_arb.config import SETTINGS  # noqa: E402


DEFAULT_INPUT = PROJECT_ROOT / "state" / "cache" / "polymarket_depth_consolidated.jsonl"
DEFAULT_OUTPUT = PROJECT_ROOT / "state" / "chunk_b_depth_replay_validation_2026-08-15.json"
MARKET_START_RE = re.compile(r"-(\d+)$")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_prefix(path: Path, byte_limit: int) -> str:
    digest = hashlib.sha256()
    remaining = byte_limit
    with path.open("rb") as handle:
        while remaining > 0:
            chunk = handle.read(min(1024 * 1024, remaining))
            if not chunk:
                break
            digest.update(chunk)
            remaining -= len(chunk)
    return digest.hexdigest()


def _snapshot_lines(path: Path, byte_limit: int):
    """Yield only newline-terminated records inside a point-in-time byte bound."""
    with path.open("rb") as handle:
        while handle.tell() < byte_limit:
            remaining = byte_limit - handle.tell()
            raw = handle.readline(remaining)
            if not raw:
                return
            if not raw.endswith(b"\n"):
                return
            yield raw.decode("utf-8", errors="replace")


def market_window_start(market_id: str) -> int | None:
    match = MARKET_START_RE.search(str(market_id))
    return int(match.group(1)) if match else None


def _valid_levels(value: Any) -> bool:
    if not isinstance(value, list) or not value:
        return False
    for level in value:
        if not isinstance(level, (list, tuple)) or len(level) < 2:
            return False
        try:
            price = float(level[0])
            size = float(level[1])
        except (TypeError, ValueError):
            return False
        if not math.isfinite(price) or not math.isfinite(size) or price < 0 or size < 0:
            return False
    return True


@dataclass
class TokenContinuity:
    first_offset_s: float | None = None
    last_offset_s: float | None = None
    max_gap_s: float = 0.0
    rows: int = 0

    def observe(self, offset_s: float) -> None:
        if self.first_offset_s is None:
            self.first_offset_s = offset_s
        if self.last_offset_s is not None:
            self.max_gap_s = max(self.max_gap_s, offset_s - self.last_offset_s)
        self.last_offset_s = offset_s
        self.rows += 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "first_offset_s": self.first_offset_s,
            "last_offset_s": self.last_offset_s,
            "max_gap_s": self.max_gap_s,
            "rows": self.rows,
        }


@dataclass
class WindowPairer:
    market_id: str
    window_start: int
    max_skew_s: float
    token_ids: list[str] = field(default_factory=list)
    latest: dict[str, dict[str, Any]] = field(default_factory=dict)
    stats: Counter = field(default_factory=Counter)

    def add(self, record: dict[str, Any]) -> dict[str, Any] | None:
        token_id = str(record.get("token_id", ""))
        if not token_id:
            self.stats["missing_token_id"] += 1
            return None
        if token_id not in self.token_ids:
            self.token_ids.append(token_id)
        if len(self.token_ids) > 2:
            self.stats["more_than_two_tokens"] += 1
            return None
        self.latest[token_id] = record
        if len(self.token_ids) < 2:
            self.stats["awaiting_other_token"] += 1
            return None

        first, second = self.token_ids
        yes = self.latest.get(first)
        no = self.latest.get(second)
        if yes is None or no is None:
            self.stats["awaiting_other_token"] += 1
            return None
        self.stats["pair_candidates"] += 1

        yes_ts = float(yes["captured_at"])
        no_ts = float(no["captured_at"])
        skew_s = abs(yes_ts - no_ts)
        if skew_s > self.max_skew_s:
            self.stats["skew_rejected"] += 1
            return None

        yes_asks = yes.get("asks")
        no_asks = no.get("asks")
        if not _valid_levels(yes_asks) or not _valid_levels(no_asks):
            self.stats["missing_or_invalid_ask_ladder"] += 1
            return None
        yes_bids = yes.get("bids") if isinstance(yes.get("bids"), list) else []
        no_bids = no.get("bids") if isinstance(no.get("bids"), list) else []
        timestamp = max(yes_ts, no_ts)
        self.stats["complete_paired_ticks"] += 1
        return {
            "timestamp": timestamp,
            "yes_timestamp": yes_ts,
            "no_timestamp": no_ts,
            "window_start": float(self.window_start),
            "window_close": float(self.window_start + 300),
            "yes_ask": float(yes_asks[0][0]),
            "no_ask": float(no_asks[0][0]),
            "yes_bid": float(yes_bids[0][0]) if yes_bids else None,
            "no_bid": float(no_bids[0][0]) if no_bids else None,
            "yes_size": float(yes_asks[0][1]),
            "no_size": float(no_asks[0][1]),
            "yes_asks": yes_asks,
            "no_asks": no_asks,
            "yes_bids": yes_bids,
            "no_bids": no_bids,
            "source_market_id": self.market_id,
            "pair_skew_ms": skew_s * 1000.0,
        }


class ReplayCollector:
    """In-memory logger used to count exact replay outcomes without log spam."""

    def __init__(self) -> None:
        self.missed_reasons: Counter[str] = Counter()
        self.trade_outcomes: Counter[str] = Counter()
        self.error_counts: Counter[str] = Counter()
        self.critical_counts: Counter[str] = Counter()
        self.errors: list[str] = []

    def debug(self, msg: Any, data: Any = None) -> None:
        return None

    def info(self, msg: Any, data: Any = None) -> None:
        return None

    def warning(self, msg: Any, data: Any = None) -> None:
        return None

    def error(self, msg: Any, data: Any = None) -> None:
        self.error_counts[self._event_key(str(msg))] += 1
        if len(self.errors) < 100:
            self.errors.append(str(msg))

    def critical(self, msg: Any, data: Any = None) -> None:
        self.critical_counts[self._event_key(str(msg))] += 1
        if len(self.errors) < 100:
            self.errors.append(str(msg))

    def trade(self, outcome: str, data: dict[str, Any]) -> None:
        self.trade_outcomes[outcome] += 1
        if outcome == "missed":
            self.missed_reasons[str(data.get("reason", "unknown"))] += 1

    @staticmethod
    def _event_key(message: str) -> str:
        if message.startswith("PARTIAL FILL on "):
            return "partial_fill_management"
        if message.startswith("EXIT FAILED no bid"):
            return "exit_failed_no_bid"
        if message.startswith("EXIT FAILED submit rejected"):
            return "exit_failed_submit_rejected"
        if message.startswith("RISK HALT: partial exit not confirmed"):
            return "risk_halt_partial_exit_not_confirmed"
        if message.startswith("RISK HALT: partial hedge not fully filled"):
            return "risk_halt_partial_hedge_not_fully_filled"
        if message.startswith("PARTIAL LOSS CAP BREACHED"):
            return "partial_loss_cap_breached"
        return message.split(":", 1)[0][:120]


METRIC_FIELDS = (
    "loops",
    "opportunities",
    "orders_sent",
    "fills",
    "partial_fills",
    "partial_resolved",
    "missed",
    "locked_pnl",
    "residual_pnl",
    "session_pnl",
    "realized_pnl",
    "would_lock_pnl",
)


async def _drain_engine(engine: BacktestEngine) -> None:
    """Finish scheduled replay-only fire/fill tasks before reading metrics."""
    for _ in range(8):
        tasks = list(engine.arb._tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=False)
        await asyncio.sleep(0)
        if engine.execution._pending:
            await engine.execution._check_fills()
        if not engine.arb._tasks and not engine.execution._pending:
            return
    if engine.execution._pending:
        raise RuntimeError(
            f"strict replay left {len(engine.execution._pending)} pending fills"
        )


async def replay_window(ticks: list[dict[str, Any]]) -> dict[str, Any]:
    config = BacktestConfig(
        symbol="Polymarket BTC Up/Down retained depth",
        start_date="captured",
        end_date="captured",
        min_edge=SETTINGS.min_edge,
        order_size=SETTINGS.order_size,
        max_quote_age_ms=SETTINGS.max_quote_age_ms,
        no_entry_seconds=SETTINGS.no_entry_seconds,
        replay_speed=1_000_000_000.0,
        use_lse=False,
        max_liquidity=0.0,
        fill_model="depth_snapshot",
        ticks=ticks,
    )
    engine = BacktestEngine(config)
    try:
        result = await engine.run()
        await _drain_engine(engine)
        fill_summary = engine.client.fill_summary()
        buy_history = [row for row in engine.client._fill_history if row.get("side") == "BUY"]
        subfull = sum(
            float(row["filled_size"]) + 1e-12 < float(row["requested_size"])
            for row in buy_history
        )
        return {
            "metrics": {field: getattr(result.metrics, field) for field in METRIC_FIELDS},
            "fills_by_band": dict(result.metrics.fills_by_band),
            "fresh_attempts": dict(result.metrics.fresh_attempts),
            "fill_summary": fill_summary,
            "subfull_buy_orders": subfull,
            "pending_after_drain": len(engine.execution._pending),
            "halted": engine.risk.halted,
        }
    finally:
        await engine.aclose()


def _merge_window_result(total: dict[str, Any], window: dict[str, Any]) -> None:
    for metric, value in window["metrics"].items():
        total["metrics"][metric] += value
    for metric_group in ("fills_by_band", "fresh_attempts"):
        for key, value in window[metric_group].items():
            total[metric_group][key] += value
    for key, value in window["fill_summary"].items():
        if key == "requested_quantity_fill_rate":
            continue
        total["fill_summary"][key] += value
    total["subfull_buy_orders"] += window["subfull_buy_orders"]
    total["pending_after_drain"] += window["pending_after_drain"]
    total["halted_windows"] += int(window["halted"])


def _install_replay_guards(collector: ReplayCollector):
    """Redirect logs and use an explicit neutral historical macro assumption."""
    import polymarket_arb.arb_engine as arb_module
    import polymarket_arb.backtest_engine as backtest_module
    import polymarket_arb.execution as execution_module
    import polymarket_arb.risk as risk_module

    modules = (arb_module, backtest_module, execution_module, risk_module)
    old_logs = [(module, module.log) for module in modules]
    for module in modules:
        module.log = collector
    old_macro_gate = risk_module.RiskState._macro_state_gate
    old_high_macro = risk_module._high_macro_risk
    neutral_state = type(
        "NeutralHistoricalMacroState",
        (),
        {"events": [], "high_risk_windows": [], "last_update": 1.0},
    )()
    risk_module.RiskState._macro_state_gate = lambda self, now: (neutral_state, None)
    risk_module._high_macro_risk = lambda now=None, state=None: False

    def restore() -> None:
        for module, old_log in old_logs:
            module.log = old_log
        risk_module.RiskState._macro_state_gate = old_macro_gate
        risk_module._high_macro_risk = old_high_macro

    return restore


async def run_replay(
    input_path: Path | list[Path],
    max_skew_ms: float,
    start_at_epoch: float | None = None,
) -> dict[str, Any]:
    input_paths = [input_path] if isinstance(input_path, Path) else list(input_path)
    if not input_paths:
        raise ValueError("at least one input path is required")
    input_byte_limits = {path: path.stat().st_size for path in input_paths}
    collector = ReplayCollector()
    restore = _install_replay_guards(collector)
    source_stats: Counter[str] = Counter()
    pair_stats: Counter[str] = Counter()
    continuity: dict[str, dict[str, TokenContinuity]] = defaultdict(
        lambda: defaultdict(TokenContinuity)
    )
    markets_seen: set[str] = set()
    tokens_seen: dict[str, set[str]] = defaultdict(set)
    aggregate: dict[str, Any] = {
        "metrics": {field: 0 for field in METRIC_FIELDS},
        "fills_by_band": Counter(),
        "fresh_attempts": Counter(),
        "fill_summary": Counter(),
        "subfull_buy_orders": 0,
        "pending_after_drain": 0,
        "halted_windows": 0,
    }
    current_pairer: WindowPairer | None = None
    current_ticks: list[dict[str, Any]] = []
    replayed_markets = 0
    replay_schema = {
        "total_ticks": 0,
        "complete_ticks": 0,
        "missing_yes_asks": 0,
        "missing_no_asks": 0,
        "invalid_yes_asks": 0,
        "invalid_no_asks": 0,
    }

    async def flush_window() -> None:
        nonlocal current_ticks, replayed_markets
        if current_pairer is None:
            return
        pair_stats.update(current_pairer.stats)
        if not current_ticks:
            return
        availability = depth_snapshot_availability(current_ticks)
        for key, value in availability.items():
            replay_schema[key] += value
        result = await replay_window(current_ticks)
        _merge_window_result(aggregate, result)
        replayed_markets += 1
        if replayed_markets % 25 == 0:
            print(
                f"[depth-replay] markets={replayed_markets} "
                f"ticks={replay_schema['total_ticks']} "
                f"fills={aggregate['metrics']['fills']}",
                file=sys.stderr,
                flush=True,
            )
        current_ticks = []

    previous_ts = float("-inf")
    try:
        for current_input in input_paths:
            for line_number, line in enumerate(
                _snapshot_lines(current_input, input_byte_limits[current_input]), 1
            ):
                    if not line.strip():
                        continue
                    source_stats["physical_rows"] += 1
                    try:
                        record = json.loads(line)
                        captured_at = float(record["captured_at"])
                    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                        source_stats["malformed"] += 1
                        continue
                    if start_at_epoch is not None and captured_at < start_at_epoch:
                        source_stats["before_start_filter"] += 1
                        continue
                    source_stats["rows"] += 1
                    if captured_at < previous_ts:
                        source_stats["chronology_violations"] += 1
                    previous_ts = captured_at
                    market_id = str(record.get("market_id", ""))
                    start = market_window_start(market_id)
                    if start is None:
                        source_stats["unparsed_market_id"] += 1
                        continue
                    markets_seen.add(market_id)
                    token_id = str(record.get("token_id", ""))
                    if token_id:
                        tokens_seen[market_id].add(token_id)
                    offset_s = captured_at - start
                    if 0.0 <= offset_s < 300.0 and token_id:
                        continuity[market_id][token_id].observe(offset_s)
                    else:
                        source_stats["outside_market_window"] += 1
                        continue

                    if current_pairer is None:
                        current_pairer = WindowPairer(market_id, start, max_skew_ms / 1000.0)
                    elif start > current_pairer.window_start:
                        await flush_window()
                        current_pairer = WindowPairer(market_id, start, max_skew_ms / 1000.0)
                    elif start < current_pairer.window_start:
                        source_stats["late_old_window_rows"] += 1
                        continue
                    elif market_id != current_pairer.market_id:
                        source_stats["same_start_different_market"] += 1
                        continue

                    tick = current_pairer.add(record)
                    if tick is not None:
                        current_ticks.append(tick)
        await flush_window()
    finally:
        restore()

    complete_window_details: list[dict[str, Any]] = []
    for market_id, token_data in sorted(continuity.items()):
        if len(token_data) != 2:
            continue
        details = {token: value.as_dict() for token, value in token_data.items()}
        complete = all(
            value.first_offset_s is not None
            and value.first_offset_s <= 5.0
            and value.last_offset_s is not None
            and value.last_offset_s >= 295.0
            and value.max_gap_s <= 60.0
            for value in token_data.values()
        )
        if complete:
            complete_window_details.append({"market_id": market_id, "tokens": details})

    requested = float(aggregate["fill_summary"].get("requested_buy_quantity", 0.0))
    filled = float(aggregate["fill_summary"].get("filled_buy_quantity", 0.0))
    aggregate["fill_summary"]["requested_quantity_fill_rate"] = (
        filled / requested if requested else 0.0
    )
    complete_ticks = int(replay_schema["complete_ticks"])
    row_threshold = 500_000
    operational_required = 30
    operational_complete = len(complete_window_details)
    replay_completed = (
        replay_schema["total_ticks"] > 0
        and replay_schema["complete_ticks"] == replay_schema["total_ticks"]
        and aggregate["pending_after_drain"] == 0
    )
    strict_gate_passed = bool(
        replay_completed
        and complete_ticks >= row_threshold
        and operational_complete >= operational_required
    )
    source_files = [
        {
            "path": str(path),
            "bytes": input_byte_limits[path],
            "sha256": _sha256_prefix(path, input_byte_limits[path]),
            "point_in_time_byte_bound": True,
        }
        for path in input_paths
    ]

    return {
        "scope": "CHUNK B real Polymarket captured-depth replay",
        "run_at": datetime.now(timezone.utc).isoformat(),
        "paper_only": True,
        "real_orders_submitted": False,
        "historical_input": {
            "source": "real_polymarket_clob_ws_consolidated",
            "path": str(input_paths[0]) if len(input_paths) == 1 else [str(path) for path in input_paths],
            "files": source_files,
            "bytes": sum(item["bytes"] for item in source_files),
            "sha256": source_files[0]["sha256"] if len(source_files) == 1 else None,
            "start_at_epoch": start_at_epoch,
            "physical_source_rows": int(source_stats["physical_rows"]),
            "rows_before_start_filter": int(source_stats["before_start_filter"]),
            "source_rows": int(source_stats["rows"]),
            "markets": len(markets_seen),
            "two_token_markets": sum(len(tokens) == 2 for tokens in tokens_seen.values()),
            "malformed_rows": int(source_stats["malformed"]),
            "chronology_violations": int(source_stats["chronology_violations"]),
            "outside_market_window_rows": int(source_stats["outside_market_window"]),
            "depth_snapshot_coverage": replay_schema,
        },
        "pairing": {
            "method": "causal latest-observation pair; no forward lookup",
            "token_orientation": "first-observed token assigned YES; dual-buy result is symmetric",
            "max_leg_skew_ms": max_skew_ms,
            **{key: int(value) for key, value in sorted(pair_stats.items())},
            "complete_ticks_as_pct_of_source_rows": (
                complete_ticks / source_stats["rows"] * 100.0 if source_stats["rows"] else 0.0
            ),
        },
        "row_count_gate": {
            "required_complete_paired_ticks": row_threshold,
            "observed_complete_paired_ticks": complete_ticks,
            "gap_remaining": max(0, row_threshold - complete_ticks),
            "status": "pass" if complete_ticks >= row_threshold else "fail",
        },
        "operational_window_gate": {
            "required_complete_windows": operational_required,
            "observed_complete_windows": operational_complete,
            "gap_remaining": max(0, operational_required - operational_complete),
            "criterion": "both tokens: first<=5s, last>=295s, max inter-snapshot gap<=60s",
            "status": "pass" if operational_complete >= operational_required else "fail",
            "complete_windows": complete_window_details,
        },
        "depth_snapshot": {
            "status": "verified-pass" if strict_gate_passed else "verified-fail",
            "replay_status": "completed" if replay_completed else "failed",
            "reason": (
                f"Strict model replay completed on {complete_ticks} real paired ticks, "
                f"row gate is {'pass' if complete_ticks >= row_threshold else 'fail'} "
                f"({complete_ticks}/{row_threshold}), and operational continuity is "
                f"{operational_complete}/{operational_required} complete windows "
                f"({'pass' if operational_complete >= operational_required else 'fail'})."
            ),
            "model": "BacktestEngine fill_model=depth_snapshot",
            "settings": {
                "min_edge": SETTINGS.min_edge,
                "order_size": SETTINGS.order_size,
                "max_quote_age_ms": SETTINGS.max_quote_age_ms,
                "no_entry_seconds": SETTINGS.no_entry_seconds,
                "max_position_per_market": SETTINGS.max_position_per_market,
                "max_total_capital": SETTINGS.max_total_capital,
            },
            "macro_gate_mode": "explicit neutral assumption; no point-in-time historical macro snapshot",
            "after_metrics": {
                **aggregate["metrics"],
                "fills_by_band": dict(aggregate["fills_by_band"]),
                "fresh_attempts": dict(aggregate["fresh_attempts"]),
                "replayed_markets": replayed_markets,
                "halted_windows": aggregate["halted_windows"],
                "pending_after_drain": aggregate["pending_after_drain"],
                "missed_reasons": dict(collector.missed_reasons),
                "trade_outcomes": dict(collector.trade_outcomes),
                "error_counts": dict(collector.error_counts),
                "critical_counts": dict(collector.critical_counts),
            },
            "quantity": {
                **dict(aggregate["fill_summary"]),
                "subfull_buy_orders": aggregate["subfull_buy_orders"],
            },
            "critical_events": collector.errors,
        },
        "limitations": [
            (
                "Rows were bounded by the singleton lock acquisition timestamp, but depth records have no capture-instance identifier; "
                "the lock file and live process evidence provide the instance boundary."
                if start_at_epoch is not None
                else "The capture has no capture-instance identifier; exact process provenance cannot be reconstructed from rows alone."
            ),
            f"Measured operational continuity is {operational_complete}/{operational_required} complete windows.",
            "The replay is deterministic passive fill simulation at retained limit prices, not exchange-confirmed historical execution.",
            "No point-in-time historical macro-state snapshot exists, so the depth-only replay uses an explicit neutral macro assumption.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        type=Path,
        action="append",
        help="Input shard; repeat in chronological order (default: consolidated input)",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--max-skew-ms",
        type=float,
        default=float(SETTINGS.max_quote_age_ms),
        help="Maximum causal receipt-time skew allowed between paired token books",
    )
    parser.add_argument(
        "--start-at-epoch",
        type=float,
        help="Ignore records before this UTC epoch (for a supervisor lock-acquisition boundary)",
    )
    args = parser.parse_args()
    inputs = args.input or [DEFAULT_INPUT]
    missing = [path for path in inputs if not path.is_file()]
    if missing:
        parser.error(f"input does not exist: {missing[0]}")
    report = asyncio.run(
        run_replay(
            [path.resolve() for path in inputs],
            args.max_skew_ms,
            start_at_epoch=args.start_at_epoch,
        )
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
    temporary.replace(args.output)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["depth_snapshot"]["replay_status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
