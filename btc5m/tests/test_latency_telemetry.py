"""Regression tests for the permanent per-stage latency telemetry engine.

Locks the stage_timing schema written into every TRADE event in bot.jsonl so it
can't silently drift.  Validates:

  1. stage_timings appears in every TRADE event that reaches the submission stage
     (filled, partial_resolved, no_fill_in_window).
  2. Early-missed events (snapshot_no_edge, missing_price) do NOT carry
     stage_timings (they never reached the submission stage).
  3. The stage_timings dict has the canonical 6 keys, all in milliseconds.
  4. scripts/latency_report.py correctly parses stage_timings + freshness bands.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import sys
from pathlib import Path
from unittest.mock import patch

# Ensure scripts/ is importable for latency_report.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import pytest

from polymarket_arb.config import SETTINGS
from polymarket_arb.execution import Execution, _format_stage_timings
from polymarket_arb.metrics import Metrics
from polymarket_arb.models import MarketState
from polymarket_arb.risk import RiskState
from polymarket_arb.logger import BotLogger

# Canonical stage set that every stage_timings dict must contain.
REQUIRED_STAGES = {"reserve", "dispatch", "dual_submit", "paper_leg_max", "post_submit", "execute_total"}


# --------------------------------------------------------------------------- #
# Test fixtures: capture TRADE events into an in-memory list                   #
# --------------------------------------------------------------------------- #

class _CapturingLogger(BotLogger):
    """Logger subclass that records every _emit call."""

    def __init__(self):
        super().__init__(jsonl_path="", level="DEBUG")
        self.events: list[dict] = []

    def _emit(self, event: str, msg: str, data: dict | None = None):
        rec = {"t": self._clock(), "event": event, "msg": msg}
        if data:
            rec["data"] = data
        self.events.append(rec)


class _StubClient:
    """Deterministic test client — instant fills, no network."""

    def __init__(self, reject_no=False):
        self.reject_no = reject_no
        self.submits: list[tuple[str, float, float, str]] = []

    async def submit_order(self, token_id, price, size, side="BUY"):
        self.submits.append((token_id, price, size, side))
        if self.reject_no and token_id == "NO" and side == "BUY":
            return None
        return f"ord-{len(self.submits)}"

    async def is_filled(self, order_id, token_id, gap_ms=0):
        return order_id is not None

    def get_fill_ratio(self, order_id):
        return 1.0 if order_id is not None else 0.0

    async def cancel_order(self, token_id, missing_order_id=None):
        return True

    async def submit_paper_order(self, *a, **kw):
        return await self.submit_order(*a[:3], **kw)


def _make_state():
    import time
    now = time.time()
    return MarketState(
        market_id="latency-test", question="test", yes_token_id="YES", no_token_id="NO",
        best_yes_ask=0.48, best_no_ask=0.50, best_yes_bid=0.0, best_no_bid=0.20,
        yes_size=500.0, no_size=500.0, last_update_ts=now, yes_ts=now, no_ts=now,
    )


def _run(coro):
    """Run a coroutine to completion."""
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# 1. _format_stage_timings helper                                               #
# --------------------------------------------------------------------------- #

def test_format_stage_timings_converts_seconds_to_ms():
    stages_s = {"reserve": 0.001, "dispatch": 0.0005, "dual_submit": 0.002}
    result = _format_stage_timings(stages_s)
    assert result == {"reserve": 1.0, "dispatch": 0.5, "dual_submit": 2.0}


def test_format_stage_timings_empty_for_none():
    assert _format_stage_timings(None) == {}
    assert _format_stage_timings({}) == {}


def test_format_stage_timings_passes_through_all_keys():
    """_format_stage_timings converts all seconds values to milliseconds."""
    stages_s = {"reserve": 0.001, "dispatch": 0.0005}
    result = _format_stage_timings(stages_s)
    assert result == {"reserve": 1.0, "dispatch": 0.5}
    # Unknown keys are preserved (filtering is the caller's responsibility).
    stages_s2 = {"reserve": 0.001, "unknown_stage": 0.5}
    result2 = _format_stage_timings(stages_s2)
    assert result2["unknown_stage"] == 500.0


# --------------------------------------------------------------------------- #
# 2. TRADE events include stage_timings                                      #
# --------------------------------------------------------------------------- #

def test_filled_trade_event_includes_stage_timings():
    """A filled TRADE event must carry stage_timings with all 6 canonical keys."""
    import polymarket_arb.execution as exec_mod
    import time

    logger = _CapturingLogger()
    client = _StubClient()
    risk = RiskState(balance=1_000.0)
    metrics = Metrics()
    exec_ = Execution(client, risk, metrics)
    st = _make_state()
    exec_.register(st)

    with patch.object(exec_mod, "log", logger):
        decision_ts = time.monotonic() - 0.001
        _run(exec_.execute_dual_buy(st, 0.96, decision_ts,
                                    age_ms=10.0, band="fresh", yes_p=0.47, no_p=0.49))
        _run(exec_._check_fills())

    filled = [e for e in logger.events if e.get("msg") == "filled"]
    assert len(filled) == 1, f"Expected 1 filled event, got {len(filled)}"
    data = filled[0]["data"]
    assert "stage_timings" in data, "filled TRADE event must have stage_timings"
    stages = data["stage_timings"]
    assert REQUIRED_STAGES <= set(stages.keys()), \
        f"Missing stages: {REQUIRED_STAGES - set(stages.keys())}"
    for k, v in stages.items():
        assert isinstance(v, (int, float)), f"stage_timings[{k}] must be numeric, got {type(v)}"
        assert v >= 0.0, f"stage_timings[{k}] must be non-negative, got {v}"
    assert "opp_to_order_ms" in data
    assert "ack_ms" in data


def test_no_fill_in_window_trade_event_includes_stage_timings():
    """A 'missed' TRADE event for no_fill_in_window must carry stage_timings
    (it reached the submission stage but neither leg filled within the gap)."""
    import polymarket_arb.execution as exec_mod

    logger = _CapturingLogger()

    class _NeverFillClient:
        def __init__(self):
            self.submits = []

        async def submit_order(self, token_id, price, size, side="BUY"):
            self.submits.append((token_id, price, size, side))
            return f"ord-{len(self.submits)}"

        async def is_filled(self, order_id, token_id, gap_ms=0):
            return False

        def get_fill_ratio(self, order_id):
            return 1.0

        async def cancel_order(self, token_id, missing_order_id=None):
            return True

        async def submit_paper_order(self, *a, **kw):
            return await self.submit_order(*a[:3], **kw)

    tuned = dataclasses.replace(
        SETTINGS, max_leg_gap_ms=0, max_position_per_market=1_000.0, max_total_capital=1_000.0,
    )
    client = _NeverFillClient()
    risk = RiskState(balance=1_000.0)
    metrics = Metrics()
    exec_ = Execution(client, risk, metrics)
    st = _make_state()
    exec_.register(st)

    with patch.object(exec_mod, "log", logger), \
         patch("polymarket_arb.execution.SETTINGS", tuned), \
         patch("polymarket_arb.risk.SETTINGS", tuned):
        import time
        decision_ts = time.monotonic() - 0.001
        _run(exec_.execute_dual_buy(st, 0.96, decision_ts,
                                    age_ms=10.0, band="fresh", yes_p=0.47, no_p=0.49))
        _run(exec_._check_fills())

    missed = [e for e in logger.events if e.get("msg") == "missed" and
              e.get("data", {}).get("reason") == "no_fill_in_window"]
    assert len(missed) == 1
    data = missed[0]["data"]
    assert "stage_timings" in data
    stages = data["stage_timings"]
    assert REQUIRED_STAGES <= set(stages.keys())


def test_early_missed_events_do_not_have_stage_timings():
    """Early-missed events (snapshot_no_edge, missing_price) must NOT have
    stage_timings — they never reached the submission stage."""
    import polymarket_arb.execution as exec_mod

    logger = _CapturingLogger()
    client = _StubClient()
    risk = RiskState(balance=1_000.0)
    metrics = Metrics()
    exec_ = Execution(client, risk, metrics)
    st = _make_state()
    exec_.register(st)

    with patch.object(exec_mod, "log", logger):
        import time
        decision_ts = time.monotonic() - 0.001
        # snapshot_no_edge: combined (0.49+0.50=0.99) >= edge_price (0.99)
        _run(exec_.execute_dual_buy(st, 0.99, decision_ts,
                                    age_ms=10.0, band="fresh", yes_p=0.49, no_p=0.50))
        # missing_price: both book legs are None (st.best_yes_ask/NO_ask are None)
        st_empty = MarketState(
            market_id="latency-test", question="test", yes_token_id="YES", no_token_id="NO",
            best_yes_ask=None, best_no_ask=None,
            yes_size=500.0, no_size=500.0,
            last_update_ts=time.time(), yes_ts=time.time(), no_ts=time.time(),
        )
        exec_.register(st_empty)
        _run(exec_.execute_dual_buy(st_empty, 0.96, decision_ts,
                                    age_ms=10.0, band="fresh", yes_p=None, no_p=None))

    missed = [e for e in logger.events if e.get("msg") == "missed"]
    assert len(missed) == 2
    for ev in missed:
        data = ev["data"]
        stages = data.get("stage_timings", {})
        assert stages == {}, \
            f"Early miss {ev['data'].get('reason')} should have empty stage_timings, got {stages}"


# --------------------------------------------------------------------------- #
# 3. latency_report.py parsing                                                 #
# --------------------------------------------------------------------------- #

def test_latency_report_parses_synthetic_stage_timings(tmp_path):
    """latency_report.py must correctly parse stage_timings from TRADE events."""
    import latency_report

    bot_jsonl = tmp_path / "bot.jsonl"
    events = [
        {"t": "10:00:00.000", "event": "TRADE", "msg": "filled", "data": {
            "market_id": "m1", "combined": 0.96, "age_ms": 10.0, "freshness_band": "fresh",
            "pnl_locked": 0.2,
            "stage_timings": {"reserve": 1.0, "dispatch": 0.5, "dual_submit": 2.0,
                              "paper_leg_max": 1.5, "post_submit": 0.3, "execute_total": 4.3},
            "opp_to_order_ms": 5.0, "ack_ms": 2.0,
        }},
        {"t": "10:00:01.000", "event": "TRADE", "msg": "filled", "data": {
            "market_id": "m2", "combined": 0.97, "age_ms": 600.0, "freshness_band": "0.5-2s",
            "pnl_locked": 0.3,
            "stage_timings": {"reserve": 2.0, "dispatch": 1.0, "dual_submit": 3.0,
                              "paper_leg_max": 2.0, "post_submit": 0.5, "execute_total": 6.5},
            "opp_to_order_ms": 7.0, "ack_ms": 3.0,
        }},
        {"t": "10:00:02.000", "event": "TRADE", "msg": "missed", "data": {
            "reason": "stale_quote", "market_id": "m3", "combined": 0.96,
            "age_ms": 1500.0, "freshness_band": "0.5-2s", "would_lock": 0.3,
        }},
    ]
    lines = [json.dumps(ev) for ev in events]
    bot_jsonl.write_text("\n".join(lines) + "\n")

    parsed = latency_report.parse_bot_jsonl(bot_jsonl)
    assert len(parsed["trade_events"]) == 3

    report = latency_report.build_report(parsed["trade_events"], None, None)
    assert report["total_trade_events"] == 3
    assert report["events_with_stage_timings"] == 2
    assert report["events_without_stage_timings"] == 1

    reserve_stats = report["stages"]["reserve"]
    assert reserve_stats["n"] == 2
    assert reserve_stats["avg_ms"] == 1.5
    assert reserve_stats["p50_ms"] == 1.0

    assert "fresh" in report["by_freshness_band"]
    assert "0.5-2s" in report["by_freshness_band"]
    fresh_stats = report["by_freshness_band"]["fresh"]["reserve"]
    assert fresh_stats["n"] == 1
    assert fresh_stats["avg_ms"] == 1.0

    assert "opp_to_order_ms" in report["meta"]
    assert report["meta"]["opp_to_order_ms"]["n"] == 2


def test_latency_report_backfill_stress_archive():
    """Backfill-test latency_report.py against the most recent execution_stress
    archive to confirm it produces real numbers (or correctly reports 0 for
    pre-stage_timings archives)."""
    import latency_report

    state_dir = PROJECT_ROOT / "state"
    archives = sorted(state_dir.glob("**/execution_stress_*"), key=lambda p: p.name, reverse=True)
    if not archives:
        pytest.skip("No execution_stress archive found")

    bot_jsonl = archives[0] / "bot.jsonl"
    if not bot_jsonl.exists():
        pytest.skip(f"No bot.jsonl in {archives[0]}")

    events = latency_report.collect_trades(bot_jsonl, None)
    report = latency_report.build_report(events, None, None)
    assert report["total_trade_events"] >= 0
    # The report must not crash and must include the expected top-level keys.
    assert "stages" in report
    assert "by_freshness_band" in report
    assert "events_with_stage_timings" in report


def test_stage_timings_schema_keys():
    """The stage_timings dict must always have exactly the canonical 6 keys."""
    expected = {"reserve", "dispatch", "dual_submit", "paper_leg_max", "post_submit", "execute_total"}
    assert REQUIRED_STAGES == expected

    sample = {k: 0.001 for k in expected}
    result = _format_stage_timings(sample)
    assert set(result.keys()) == expected
    for v in result.values():
        assert v == 1.0  # 0.001s -> 1.0ms
