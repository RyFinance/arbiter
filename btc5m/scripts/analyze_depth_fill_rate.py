"""Instrumented clean-fill-rate investigation for the strict depth replay.

Replays the sealed consolidated capture (SHA-256
b615af838729b7d754bd20a72f9b7a476c694058c6aa3073610d684aba3ef98b) through the
SAME BacktestEngine path as ``replay_captured_depth.py``, but captures
structured per-event context at four hook points:

  * decision (fire): combined ask, decision prices, edge, window offset,
    top-of-book size, and total depth at/below the limit for both legs at
    the decision tick;
  * leg submit: which tick the submit coroutine actually resolved against,
    the ladder state at that tick, filled size / VWAP / rejection;
  * partial management: which leg was missing, exit/hedge submission result;
  * halt events: reason strings and per-window halt state.

The headline counters must reproduce the published combined-run numbers
(28,407 opportunities / 8,904 orders / 77 clean fills / 690 partials /
590 resolved partials / 86 halted windows) before the detailed breakdown
is trusted.
"""
from __future__ import annotations

import argparse
import asyncio
import contextvars
import json
import sys
import time as real_time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(PROJECT_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from replay_captured_depth import (  # noqa: E402
    METRIC_FIELDS,
    ReplayCollector,
    WindowPairer,
    _install_replay_guards,
    market_window_start,
)
from polymarket_arb.backtest_engine import (  # noqa: E402
    BacktestConfig,
    BacktestEngine,
    cumulative_limit_fill,
)
from polymarket_arb.config import SETTINGS  # noqa: E402

DEFAULT_INPUT = PROJECT_ROOT / "state" / "cache" / "polymarket_depth_consolidated.jsonl"
EXPECTED = {
    "loops": 975259,
    "opportunities": 28407,
    "orders_sent": 8904,
    "fills": 77,
    "partial_fills": 690,
    "partial_resolved": 590,
    "halted_windows": 86,
}

decision_ctx: contextvars.ContextVar = contextvars.ContextVar("decision_seq", default=None)


class InstrumentedCollector(ReplayCollector):
    """ReplayCollector that also retains halt/exit reason strings verbatim."""

    def __init__(self) -> None:
        super().__init__()
        self.retained: List[str] = []

    def critical(self, msg: Any, data: Any = None) -> None:
        super().critical(msg, data)
        self.retained.append(str(msg))

    def error(self, msg: Any, data: Any = None) -> None:
        super().error(msg, data)
        if str(msg).startswith("EXIT FAILED") or str(msg).startswith("RISK HALT"):
            self.retained.append(str(msg))


class Capture:
    """Global capture bucket filled by the monkeypatch hooks."""

    def __init__(self) -> None:
        self.decisions: List[Dict[str, Any]] = []
        self.submits: List[Dict[str, Any]] = []
        self.partials: List[Dict[str, Any]] = []
        self.resolutions: List[Dict[str, Any]] = []
        self.halt_events: List[Dict[str, Any]] = []
        self.window_ids: Dict[int, str] = {}
        self.seq = 0
        self.collector = InstrumentedCollector()

    def current_tick_holder(self) -> Dict[str, Any]:
        return self._tick_holder

    _tick_holder: Dict[str, Any] = {"index": -1, "tick": None}


CAPTURE = Capture()


def _depth_at_limit(levels: Any, limit: float) -> float:
    """Total size on the ladder at or below/above the limit (BUY-style: <= limit)."""
    if not isinstance(levels, list):
        return 0.0
    total = 0.0
    for level in levels:
        if not isinstance(level, (list, tuple)) or len(level) < 2:
            continue
        price = float(level[0])
        size = float(level[1])
        if price > limit:
            break
        total += size
    return total


def _install_hooks() -> None:
    import polymarket_arb.backtest_engine as backtest_module
    import polymarket_arb.execution as execution_module
    import polymarket_arb.risk as risk_module

    orig_set_state = backtest_module._BacktestTradingClient.set_market_state

    def set_state(self, st, tick=None):
        CAPTURE._tick_holder["index"] += 1
        CAPTURE._tick_holder["tick"] = tick
        CAPTURE._tick_holder["state"] = st
        return orig_set_state(self, st, tick)

    backtest_module._BacktestTradingClient.set_market_state = set_state

    orig_submit = backtest_module._BacktestTradingClient.submit_order

    async def submit(self, token_id, price, size, side="BUY"):
        seq = decision_ctx.get()
        holder = CAPTURE._tick_holder
        tick = holder.get("tick") or {}
        st = holder.get("state")
        tick_index = holder.get("index", -1)
        order_id = await orig_submit(self, token_id, price, size, side)
        leg = None
        if st is not None:
            if token_id == st.yes_token_id:
                leg = "yes"
            elif token_id == st.no_token_id:
                leg = "no"
        is_exit = seq is None and side == "SELL" and leg is not None
        ladder = None
        limit_depth = None
        if tick and leg:
            ladder = tick.get(f"{leg}_asks") if side == "BUY" else tick.get(f"{leg}_bids")
            limit_depth = _depth_at_limit(ladder, price) if ladder else None
        filled = 0.0
        vwap = None
        if order_id:
            ratio = float(self.get_fill_ratio(order_id))
            filled = ratio * size
            vwap = self.get_fill_vwap(order_id)
        CAPTURE.submits.append(
            {
                "seq": seq,
                "leg": leg,
                "side": side,
                "limit": float(price),
                "size": float(size),
                "order_id": order_id,
                "filled": filled,
                "vwap": vwap,
                "tick_index": tick_index,
                "tick_ts": tick.get("timestamp") if tick else None,
                "best_ask_yes": tick.get("yes_ask") if tick else None,
                "best_ask_no": tick.get("no_ask") if tick else None,
                "best_bid_yes": tick.get("yes_bid") if tick else None,
                "best_bid_no": tick.get("no_bid") if tick else None,
                "yes_ask_size": tick.get("yes_size") if tick else None,
                "no_ask_size": tick.get("no_size") if tick else None,
                "limit_depth": limit_depth,
                "is_exit": is_exit,
            }
        )
        return order_id

    backtest_module._BacktestTradingClient.submit_order = submit

    orig_manage = execution_module.Execution._manage_partial

    async def manage(self, st, tpl, yes_id, no_id, yes_filled, no_filled, size,
                     snapshot_combined, fill_yes_p, fill_no_p, age_ms, band,
                     decision_ts, **kwargs):
        seq = decision_ctx.get()
        CAPTURE.partials.append(
            {
                "seq": seq,
                "yes_filled": bool(yes_filled),
                "no_filled": bool(no_filled),
                "decision_yes_p": fill_yes_p if yes_filled else None,
                "decision_no_p": fill_no_p if no_filled else None,
                "size": float(size),
                "combined": float(snapshot_combined),
                "age_ms": age_ms,
                "band": band,
            }
        )
        return await orig_manage(
            self, st, tpl, yes_id, no_id, yes_filled, no_filled, size,
            snapshot_combined, fill_yes_p, fill_no_p, age_ms, band, decision_ts, **kwargs
        )

    execution_module.Execution._manage_partial = manage

    orig_resolve = execution_module.Execution._resolve_one

    async def resolve_one(self, entry, key):
        metrics = self.metrics
        fills_before = metrics.fills
        partial_before = metrics.partial_fills
        result = await orig_resolve(self, entry, key)
        if result:
            if metrics.partial_fills > partial_before:
                branch = "partial"
            elif metrics.fills > fills_before:
                branch = "clean"
            else:
                branch = "no_fill_or_manual"
            CAPTURE.resolutions.append(
                {
                    "market_id": entry.get("st", None).market_id if entry.get("st") else None,
                    "yes_filled": bool(entry.get("yes_filled")),
                    "no_filled": bool(entry.get("no_filled")),
                    "branch": branch,
                }
            )
        return result

    execution_module.Execution._resolve_one = resolve_one

    orig_halt = risk_module.RiskState.halt

    def halt(self, reason="", **kwargs):
        CAPTURE.halt_events.append(
            {"reason": str(reason), "monotonic": real_time.monotonic()}
        )
        return orig_halt(self, reason, **kwargs)

    risk_module.RiskState.halt = halt


def _wrap_on_fire(engine: BacktestEngine, window_id: str, window_start: int) -> None:
    original = engine.arb.on_fire

    async def wrapped(st, combined, decision_ts, age_ms, band, yes_p, no_p):
        CAPTURE.seq += 1
        seq = CAPTURE.seq
        tick = CAPTURE._tick_holder.get("tick") or {}
        tick_index = CAPTURE._tick_holder.get("index", -1)
        ts = tick.get("timestamp")
        offset = ts - window_start if ts is not None else None
        # Collision probe: with the per-fire order-id suffix (backtest_engine.py
        # fix 2026-08-16) every pending key is unique, so this should read
        # False for every fire.  Match on the id prefix to stay compatible
        # with both id formats.
        execution = engine.execution
        tpl = execution._templates.get(st.market_id) if hasattr(execution, "_templates") else None
        yes_token = getattr(tpl, "yes_token_id", None) if tpl else None
        expected_prefix = f"bt-buy-{yes_token[-8:]}" if yes_token else "?"
        had_pending = any(
            str(k[0]) == st.market_id and str(k[1]).startswith(expected_prefix)
            for k in execution._pending
        )
        decision = {
            "seq": seq,
            "window_id": window_id,
            "window_start": window_start,
            "offset_s": round(offset, 3) if offset is not None else None,
            "combined": float(combined),
            "yes_p": float(yes_p),
            "no_p": float(no_p),
            "edge": round(1.0 - float(combined), 6),
            "age_ms": age_ms,
            "band": band,
            "tick_index": tick_index,
            "tick_ts": ts,
            "yes_ask_size": tick.get("yes_size"),
            "no_ask_size": tick.get("no_size"),
            "yes_depth_at_limit": _depth_at_limit(tick.get("yes_asks"), float(yes_p)) if tick.get("yes_asks") else None,
            "no_depth_at_limit": _depth_at_limit(tick.get("no_asks"), float(no_p)) if tick.get("no_asks") else None,
            "yes_bid": tick.get("yes_bid"),
            "no_bid": tick.get("no_bid"),
            "pending_collision_at_fire": had_pending,
            "pending_size_at_fire": len(execution._pending),
        }
        CAPTURE.decisions.append(decision)
        token = decision_ctx.set(seq)
        try:
            return await original(st, combined, decision_ts, age_ms, band, yes_p, no_p)
        finally:
            decision_ctx.reset(token)

    engine.arb.on_fire = wrapped


async def replay_window_instrumented(ticks: List[Dict[str, Any]], window_id: str, window_start: int) -> Dict[str, Any]:
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
    CAPTURE._tick_holder["index"] = -1
    CAPTURE._tick_holder["tick"] = None
    _wrap_on_fire(engine, window_id, window_start)
    try:
        result = await engine.run()
        for _ in range(8):
            tasks = list(engine.arb._tasks)
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=False)
            await asyncio.sleep(0)
            if engine.execution._pending:
                await engine.execution._check_fills()
            if not engine.arb._tasks and not engine.execution._pending:
                break
        return {
            "metrics": {field: getattr(result.metrics, field) for field in METRIC_FIELDS},
            "halted": engine.risk.halted,
        }
    finally:
        await engine.aclose()


def stream_windows(path: Path):
    current_start: int | None = None
    current_market: str | None = None
    pairer: WindowPairer | None = None
    ticks: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                captured_at = float(record["captured_at"])
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                continue
            market_id = str(record.get("market_id", ""))
            start = market_window_start(market_id)
            if start is None:
                continue
            offset_s = captured_at - start
            if not 0.0 <= offset_s < 300.0 or not record.get("token_id"):
                continue  # mirrors outside_market_window in the driver
            if current_start is None:
                current_start = start
                current_market = market_id
                pairer = WindowPairer(market_id, start, 0.5)
            elif start > current_start:
                yield current_start, current_market, ticks
                ticks = []
                current_start = start
                current_market = market_id
                pairer = WindowPairer(market_id, start, 0.5)
            elif start < current_start:
                continue  # mirrors late_old_window_rows
            elif market_id != current_market:
                continue  # mirrors same_start_different_market
            tick = pairer.add(record)
            if tick is not None:
                ticks.append(tick)
    if current_start is not None:
        yield current_start, current_market, ticks


async def run_all(path: Path) -> Dict[str, Any]:
    totals = {field: 0 for field in METRIC_FIELDS}
    halted = 0
    windows = 0
    for start, market_id, ticks in stream_windows(path):
        CAPTURE.window_ids[start] = market_id
        windows += 1
        window = await replay_window_instrumented(ticks, market_id, start)
        for field in METRIC_FIELDS:
            totals[field] += window["metrics"][field]
        halted += int(window["halted"])
        if windows % 50 == 0:
            print(f"[instrumented] windows={windows} ticks={totals['loops']} "
                  f"fills={totals['fills']} partials={totals['partial_fills']} "
                  f"halted={halted}", flush=True)
    return {"totals": totals, "halted_windows": halted, "windows": windows}


def analyze() -> Dict[str, Any]:
    decisions = CAPTURE.decisions
    submits = CAPTURE.submits
    partials = CAPTURE.partials

    decision_map = {d["seq"]: d for d in decisions}
    submits_by_seq: Dict[Any, List[Dict[str, Any]]] = {}
    for s in submits:
        submits_by_seq.setdefault(s["seq"], []).append(s)

    pair_outcomes: List[Dict[str, Any]] = []
    for seq, decision in decision_map.items():
        legs = submits_by_seq.get(seq, [])
        yes_leg = next((s for s in legs if s["leg"] == "yes" and not s["is_exit"]), None)
        no_leg = next((s for s in legs if s["leg"] == "no" and not s["is_exit"]), None)
        yes_filled = bool(yes_leg and yes_leg["order_id"] and yes_leg["filled"] >= yes_leg["size"] - 1e-12)
        no_filled = bool(no_leg and no_leg["order_id"] and no_leg["filled"] >= no_leg["size"] - 1e-12)
        yes_partial = bool(yes_leg and yes_leg["order_id"] and 0 < yes_leg["filled"] < yes_leg["size"] - 1e-12)
        no_partial = bool(no_leg and no_leg["order_id"] and 0 < no_leg["filled"] < no_leg["size"] - 1e-12)
        if yes_filled and no_filled:
            outcome = "clean"
        elif (yes_filled or yes_partial) and (no_filled or no_partial):
            outcome = "clean_partial_ratio"
        elif (yes_filled or yes_partial) or (no_filled or no_partial):
            outcome = "one_leg"
        elif yes_leg is None and no_leg is None:
            outcome = "no_submits"
        else:
            outcome = "both_rejected"
        pair_outcomes.append(
            {
                "seq": seq,
                "outcome": outcome,
                "decision": decision,
                "yes_leg": yes_leg,
                "no_leg": no_leg,
            }
        )

    one_leg = [p for p in pair_outcomes if p["outcome"] == "one_leg"]
    missing_price_move: List[float] = []
    missing_leg_counter: Counter = Counter()
    for p in one_leg:
        d = p["decision"]
        yes_leg = p["yes_leg"]
        no_leg = p["no_leg"]
        if yes_leg and yes_leg["order_id"]:
            missing_leg_counter["no"] += 1
            if no_leg:
                move = (no_leg["best_ask_no"] - d["no_p"]) if no_leg["best_ask_no"] is not None else None
                if move is not None:
                    missing_price_move.append(move)
        else:
            missing_leg_counter["yes"] += 1
            if yes_leg:
                move = (yes_leg["best_ask_yes"] - d["yes_p"]) if yes_leg["best_ask_yes"] is not None else None
                if move is not None:
                    missing_price_move.append(move)

    resolve_lag_ticks = []
    for p in pair_outcomes:
        d = p["decision"]
        for key in ("yes_leg", "no_leg"):
            leg = p[key]
            if leg:
                resolve_lag_ticks.append(leg["tick_index"] - d["tick_index"])

    edge_buckets: Dict[str, List[Dict[str, Any]]] = {}
    for p in pair_outcomes:
        edge = p["decision"]["edge"]
        bucket = (
            "0.010-0.020" if edge < 0.02 else
            "0.020-0.030" if edge < 0.03 else
            "0.030-0.050" if edge < 0.05 else
            "0.050-0.100" if edge < 0.10 else
            ">=0.100"
        )
        edge_buckets.setdefault(bucket, []).append(p)

    bucket_summary = {}
    for bucket, items in sorted(edge_buckets.items()):
        total = len(items)
        clean = sum(1 for p in items if p["outcome"] == "clean")
        one = sum(1 for p in items if p["outcome"] == "one_leg")
        both = sum(1 for p in items if p["outcome"] == "both_rejected")
        ratio = sum(1 for p in items if p["outcome"] == "clean_partial_ratio")
        no_sub = sum(1 for p in items if p["outcome"] == "no_submits")
        bucket_summary[bucket] = {
            "fired_pairs": total,
            "clean": clean,
            "clean_pct": round(100.0 * clean / total, 3) if total else 0.0,
            "one_leg": one,
            "both_rejected": both,
            "partial_ratio": ratio,
            "no_submits": no_sub,
        }

    offset_clean = {"clean": [], "not_clean": []}
    for p in pair_outcomes:
        off = p["decision"].get("offset_s")
        if off is None:
            continue
        offset_clean["clean" if p["outcome"] == "clean" else "not_clean"].append(off)

    def quantiles(values: List[float]) -> Dict[str, float]:
        if not values:
            return {"n": 0}
        ordered = sorted(values)
        n = len(ordered)
        return {
            "n": n,
            "min": ordered[0],
            "p25": ordered[n // 4],
            "p50": ordered[n // 2],
            "p75": ordered[3 * n // 4],
            "p90": ordered[9 * n // 10],
            "max": ordered[-1],
        }

    partial_sample = []
    for p in one_leg[:25]:
        d = p["decision"]
        missing = "no" if (p["yes_leg"] and p["yes_leg"]["order_id"]) else "yes"
        missing_leg = p["no_leg"] if missing == "no" else p["yes_leg"]
        filled_leg = p["yes_leg"] if missing == "no" else p["no_leg"]
        partial_sample.append(
            {
                "seq": p["seq"],
                "window": d["window_id"],
                "offset_s": d["offset_s"],
                "edge": d["edge"],
                "decision_yes": d["yes_p"],
                "decision_no": d["no_p"],
                "missing_leg": missing,
                "missing_limit_depth_at_decision": (
                    d["no_depth_at_limit"] if missing == "no" else d["yes_depth_at_limit"]
                ),
                "missing_best_ask_at_resolve": (
                    missing_leg["best_ask_no"] if missing == "no" else missing_leg["best_ask_yes"]
                ) if missing_leg else None,
                "missing_resolve_lag_ticks": (
                    missing_leg["tick_index"] - d["tick_index"] if missing_leg else None
                ),
                "filled_leg_vwap": filled_leg["vwap"] if filled_leg else None,
                "filled_leg_filled_size": filled_leg["filled"] if filled_leg else None,
            }
        )

    halt_reasons = Counter()
    for event in CAPTURE.halt_events:
        halt_reasons[event["reason"] or "unknown"] += 1

    resolution_branches = Counter()
    for r in CAPTURE.resolutions:
        resolution_branches[(r["branch"], r["yes_filled"], r["no_filled"])] += 1

    collision_stats = Counter()
    dropped_clean = 0
    dropped_one_leg = 0
    for p in pair_outcomes:
        collided = bool(p["decision"].get("pending_collision_at_fire"))
        collision_stats[(p["outcome"], collided)] += 1
        if collided:
            if p["outcome"] == "clean":
                dropped_clean += 1
            elif p["outcome"] == "one_leg":
                dropped_one_leg += 1

    fired = [p for p in pair_outcomes if p["outcome"] != "no_submits"]
    both_filled_total = len([p for p in fired if p["outcome"] in ("clean", "clean_partial_ratio")])

    def quantiles_of(items, key, filter_fn=None):
        values = []
        for p in items:
            d = p["decision"]
            value = d.get(key)
            if value is None:
                continue
            if filter_fn and not filter_fn(p):
                continue
            values.append(float(value))
        return quantiles(values)

    liquidity_by_outcome = {}
    for outcome in ("clean", "clean_partial_ratio", "one_leg", "both_rejected"):
        subset = [p for p in fired if p["outcome"] == outcome]
        liquidity_by_outcome[outcome] = {
            "pairs": len(subset),
            "yes_ask_size": quantiles_of(subset, "yes_ask_size"),
            "no_ask_size": quantiles_of(subset, "no_ask_size"),
            "min_top_size": quantiles_of(subset, None) if False else None,
        }
    for outcome in ("clean", "clean_partial_ratio", "one_leg", "both_rejected"):
        subset = [p for p in fired if p["outcome"] == outcome]
        min_top = []
        depth_yes = []
        depth_no = []
        for p in subset:
            d = p["decision"]
            ys = d.get("yes_ask_size")
            ns = d.get("no_ask_size")
            if ys is not None and ns is not None:
                min_top.append(min(ys, ns))
            if d.get("yes_depth_at_limit") is not None:
                depth_yes.append(d["yes_depth_at_limit"])
            if d.get("no_depth_at_limit") is not None:
                depth_no.append(d["no_depth_at_limit"])
        liquidity_by_outcome[outcome]["min_top_of_book_size"] = quantiles(min_top)
        liquidity_by_outcome[outcome]["yes_depth_at_limit"] = quantiles(depth_yes)
        liquidity_by_outcome[outcome]["no_depth_at_limit"] = quantiles(depth_no)

    window_rows: Dict[str, Dict[str, Any]] = {}
    for p in fired:
        wid = p["decision"]["window_id"]
        row = window_rows.setdefault(wid, {
            "window_id": wid, "window_start": p["decision"]["window_start"],
            "fired": 0, "both_filled": 0, "one_leg": 0, "both_rejected": 0, "collided": 0,
        })
        row["fired"] += 1
        if p["outcome"] in ("clean", "clean_partial_ratio"):
            row["both_filled"] += 1
        elif p["outcome"] == "one_leg":
            row["one_leg"] += 1
        else:
            row["both_rejected"] += 1
        if p["decision"].get("pending_collision_at_fire"):
            row["collided"] += 1
    window_list = sorted(window_rows.values(), key=lambda r: -r["fired"])
    windows_with_fills = [r for r in window_rows.values() if r["both_filled"]]
    windows_with_partials = [r for r in window_rows.values() if r["one_leg"]]

    return {
        "headline": {
            "windows": None,
            "decisions_captured": len(decisions),
            "submits_captured": len(submits),
            "fired_pairs_captured": len(fired),
            "pair_outcome_distribution": dict(Counter(p["outcome"] for p in pair_outcomes)),
            "fired_pair_outcome_distribution": dict(Counter(p["outcome"] for p in fired)),
            "pending_collision_at_fire": dict(Counter(
                bool(p["decision"].get("pending_collision_at_fire")) for p in pair_outcomes
            )),
            "pending_collision_fired_only": dict(Counter(
                bool(p["decision"].get("pending_collision_at_fire")) for p in fired
            )),
            "outcome_x_collision": {f"{o}/{c}": n for (o, c), n in collision_stats.items()},
            "dropped_both_filled_pairs": dropped_clean,
            "dropped_one_leg_pairs": dropped_one_leg,
            "both_filled_total_at_submit": both_filled_total,
            "clean_rate_pct_all_opportunities": round(100.0 * 77 / len(pair_outcomes), 3),
            "clean_rate_pct_fired_pairs": round(100.0 * 77 / len(fired), 3) if fired else 0.0,
            "clean_rate_pct_fired_pairs_submit_semantics": round(
                100.0 * both_filled_total / len(fired), 3
            ) if fired else 0.0,
            "collision_free_clean_rate_pct_fired_pairs": round(
                100.0 * both_filled_total / len(fired), 3
            ) if fired else 0.0,
            "missing_leg_distribution": dict(missing_leg_counter),
            "missing_leg_price_move_pct": quantiles([v for v in missing_price_move if v is not None]),
            "resolve_lag_ticks": quantiles([float(v) for v in resolve_lag_ticks if v is not None]),
            "resolution_branches": {f"{b}/{y}/{n}": c for (b, y, n), c in resolution_branches.items()},
            "liquidity_by_outcome": liquidity_by_outcome,
            "edge_buckets": bucket_summary,
            "offset_clean_vs_not": {
                key: quantiles(values) for key, values in offset_clean.items()
            },
            "window_clustering": {
                "windows_with_any_fire": len(window_rows),
                "windows_with_clean_fills": len(windows_with_fills),
                "windows_with_one_leg_fires": len(windows_with_partials),
                "top_windows_by_fired": window_list[:10],
                "top_windows_by_both_filled": sorted(
                    window_list, key=lambda r: -r["both_filled"]
                )[:8],
            },
            "partial_sample": partial_sample,
            "halt_reasons": dict(halt_reasons),
            "halt_events_total": len(CAPTURE.halt_events),
            "retained_critical_strings": CAPTURE.collector.retained[:400],
            "collector_counts": {
                "critical": dict(CAPTURE.collector.critical_counts),
                "errors": dict(CAPTURE.collector.error_counts),
            },
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    _install_hooks()
    _install_replay_guards(CAPTURE.collector)
    started = real_time.time()
    result = asyncio.run(run_all(args.input))
    elapsed = real_time.time() - started
    detail = analyze()
    detail["headline"]["windows"] = result["windows"]
    detail["replay_totals"] = result["totals"]
    detail["replay_totals"]["halted_windows"] = result["halted_windows"]
    detail["expected"] = EXPECTED
    detail["elapsed_s"] = round(elapsed, 1)
    output = args.output
    if output.exists():
        raise FileExistsError(f"refusing to overwrite {output}")
    output.write_text(json.dumps(detail, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(
        {k: detail[k] for k in ("expected", "replay_totals", "elapsed_s")}, indent=2))


if __name__ == "__main__":
    main()
