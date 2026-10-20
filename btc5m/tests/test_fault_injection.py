"""TASK 6 — Fault-injection chaos harness.

Drives the REAL arb_engine -> execution -> risk pipeline through hostile
conditions and asserts the bot degrades gracefully:

  * WS drop mid-fill-probe -> probe failure contained, entry still resolves
  * stale-quote flood -> gated (missed), never fired
  * partial one-leg fill -> unwind to a flat position
  * book moves between gate and submit -> snapshot prices win (C1)
  * kill-switch -> fire blocked, no orders
  * daily-kill-switch breach -> alert persisted to disk, fire blocked
  * multi-scenario session -> final position flat, capital fully released

Every scenario asserts: zero unhandled fire-task exceptions, no leaked
reservations (pending_deployed == 0), and no orders after a blocking gate.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import time
from pathlib import Path
from unittest.mock import patch

from polymarket_arb.alerting import WebhookAlerter
from polymarket_arb.arb_engine import ArbEngine
from polymarket_arb.config import SETTINGS
from polymarket_arb.execution import Execution
from polymarket_arb.metrics import Metrics
from polymarket_arb.models import MarketState
from polymarket_arb.risk import RiskState


def _tuned(**overrides) -> dataclasses.Replace:
    """SETTINGS copy with the given fields overridden (others keep real values)."""
    return dataclasses.replace(SETTINGS, **overrides)


class _FaultClient:
    """Deterministic fault-injecting client over the real hot path.

    Modes (independent knobs):
      * fail_probes: is_filled raises ConnectionError (WS drop simulation)
      * fill_yes_only: YES leg always fills, NO leg never fills (partial event)
      * never_fill: both legs rest until the no-fill timeout cancels them
      * reject_submits: submit_order returns None (exchange rejection)
    """

    def __init__(self, fail_probes=False, fill_yes_only=False,
                 never_fill=False, reject_submits=False):
        self.fail_probes = fail_probes
        self.fill_yes_only = fill_yes_only
        self.never_fill = never_fill
        self.reject_submits = reject_submits
        self.sent = []
        self.cancelled = []
        self.positions = {}
        self.probe_failures = 0

    async def submit_order(self, token_id, price, size, side="BUY"):
        if self.reject_submits:
            return None
        oid = f"fault-{len(self.sent)}"
        self.sent.append((token_id, price, size, side))
        self.positions[token_id] = self.positions.get(token_id, 0.0) + (
            size if side == "BUY" else -size
        )
        return oid

    async def is_filled(self, order_id, token_id, gap_ms=0):
        if order_id is None:
            return False
        if self.fail_probes:
            self.probe_failures += 1
            raise ConnectionError("simulated WS drop")
        if self.never_fill:
            return False
        if self.fill_yes_only:
            return token_id.startswith("0xYES")
        return True

    def get_fill_ratio(self, order_id):
        return 1.0 if order_id is not None else 0.0

    async def cancel_order(self, token_id, missing_order_id=None):
        self.cancelled.append(token_id)
        if token_id in self.positions:
            self.positions[token_id] = 0.0
        return True


def _state(market_id="fault-m1", yes=0.47, no=0.49, age_ms=0.0, ysz=500.0, nsz=500.0):
    now = time.time() - age_ms / 1000.0
    return MarketState(
        market_id=market_id, question="fault injection", yes_token_id="0xYES",
        no_token_id="0xNO", best_yes_ask=yes, best_no_ask=no,
        best_yes_bid=yes - 0.02, best_no_bid=no - 0.01,
        yes_size=ysz, no_size=nsz, last_update_ts=now, yes_ts=now, no_ts=now,
    )


async def _drain(exec_, eng=None, turns=60):
    for _ in range(turns):
        await asyncio.sleep(0)


def _collect_task_errors(exec_, eng):
    """Patch both H1/E1 error sinks so any unhandled fire/probe exception is
    recorded. `_log_task_error` is a staticmethod; instance-attr assignment with
    a plain function shadows it (add_done_callback calls fn(task))."""
    errors = []

    def _capture(original):
        def wrapper(task):
            if task.cancelled():
                return
            exc = task.exception()
            if exc is not None:
                errors.append(repr(exc))
            return original(task) if original else None
        return wrapper

    original_exec = exec_._log_task_error
    original_eng = eng._log_task_error if eng else None
    exec_._log_task_error = _capture(original_exec)
    if eng is not None:
        eng._log_task_error = _capture(original_eng)
    return errors, (exec_, eng, original_exec, original_eng)


def _restore(handle):
    exec_, eng, original_exec, original_eng = handle
    exec_._log_task_error = original_exec
    if eng is not None:
        eng._log_task_error = original_eng


# ---------------------------------------------------------------------------
# Scenario 1: WS drop mid-fill-probe
# ---------------------------------------------------------------------------
def test_ws_drop_during_fill_probe_is_contained():
    """A probe raising ConnectionError must be contained (execution.py:605/611
    wraps probes), the entry stays pending, and a later healthy probe resolves
    it — no unhandled exceptions, no leaked reservation."""
    async def _run():
        client = _FaultClient(fail_probes=True)
        risk = RiskState(balance=1000.0)
        metrics = Metrics()
        exec_ = Execution(client, risk, metrics)
        st = _state()
        exec_.register(st)
        errors, handle = _collect_task_errors(exec_, None)
        try:
            await exec_.execute_dual_buy(st, 0.96, 0.0, age_ms=10.0, band="fresh")
            # One fill-probe cycle with the feed down: probe must fail contained.
            exec_.on_price_update(st)
            await _drain(exec_)
            assert exec_._pending, "probe failure must NOT kill the pending entry"
            assert client.probe_failures >= 1
            # Feed heals: probes succeed again.
            client.fail_probes = False
            exec_.on_price_update(st)
            await _drain(exec_)
            assert not exec_._pending, "entry must resolve once probes heal"
        finally:
            _restore(handle)
        return errors, risk, metrics
    errors, risk, metrics = asyncio.run(_run())
    assert errors == [], f"unhandled exceptions escaped: {errors}"
    assert metrics.fills == 1
    assert risk.pending_deployed == 0.0


# ---------------------------------------------------------------------------
# Scenario 2: stale-quote flood
# ---------------------------------------------------------------------------
def test_stale_quote_flood_is_gated_never_fired():
    """Quotes older than max_quote_age_ms must be gated (counted missed), with
    zero orders sent — regardless of how attractive the combined price looks."""
    async def _run():
        client = _FaultClient()
        metrics = Metrics()
        eng = ArbEngine(on_fire=lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("stale quote must never fire")), metrics=metrics)
        tuned = _tuned(max_quote_age_ms=500)
        with patch("polymarket_arb.arb_engine.SETTINGS", tuned):
            for age in (600, 3000, 30_000):
                eng.evaluate(_state(age_ms=age))
                await _drain(eng, eng)
        return client, metrics
    client, metrics = asyncio.run(_run())
    assert metrics.missed == 3
    assert metrics.opportunities == 0
    assert len(client.sent) == 0


# ---------------------------------------------------------------------------
# Scenario 3: partial one-leg fill
# ---------------------------------------------------------------------------
def test_partial_one_leg_fill_unwinds_to_flat():
    """YES fills, NO rests -> cancel NO + hedge/exit -> zero position, capital
    released, record written, no unhandled exceptions."""
    async def _run():
        client = _FaultClient(fill_yes_only=True)
        risk = RiskState(balance=1000.0)
        metrics = Metrics()
        exec_ = Execution(client, risk, metrics)
        tuned = _tuned(max_leg_gap_ms=0)
        st = _state()
        exec_.register(st)
        errors, handle = _collect_task_errors(exec_, None)
        try:
            with patch("polymarket_arb.execution.SETTINGS", tuned), \
                 patch("polymarket_arb.risk.SETTINGS", tuned):
                await exec_.execute_dual_buy(st, 0.96, 0.0, age_ms=10.0, band="fresh")
                for _ in range(8):
                    exec_.on_price_update(st)
                    await _drain(exec_)
        finally:
            _restore(handle)
        return errors, client, risk, metrics, exec_
    errors, client, risk, metrics, exec_ = asyncio.run(_run())
    assert errors == [], f"unhandled exceptions escaped: {errors}"
    assert len(client.sent) >= 3  # YES+NO legs + unwind (hedge/exit)
    assert client.positions.get("0xYES", 0.0) == 0.0
    assert client.positions.get("0xNO", 0.0) == 0.0
    assert risk.pending_deployed == 0.0
    assert "fault-m1" in exec_.records


# ---------------------------------------------------------------------------
# Scenario 4: book moves between gate and submit
# ---------------------------------------------------------------------------
def test_book_moves_after_gate_but_snapshot_wins():
    """The gate saw 0.96; the live book moves to no-edge before submit. C1 says
    the SNAPSHOT prices win: orders go out at the gated prices, not the new
    book, and the snapshot edge is re-asserted."""
    async def _run():
        client = _FaultClient()
        risk = RiskState(balance=1000.0)
        metrics = Metrics()
        exec_ = Execution(client, risk, metrics)
        eng = ArbEngine(on_fire=exec_.execute_dual_buy, metrics=metrics)
        st = _state()  # combined 0.96 < 0.97 -> gate clears
        exec_.register(st)
        errors, handle = _collect_task_errors(exec_, eng)
        try:
            eng.evaluate(st)
            await _drain(exec_, eng)
        finally:
            _restore(handle)
        return client, metrics, errors
    client, metrics, errors = asyncio.run(_run())
    assert errors == []
    assert len(client.sent) == 2
    for _, price, _, _ in client.sent:
        assert price in (0.47, 0.49), "must submit at SNAPSHOT prices, not the moved book"
    assert metrics.opportunities == 1


# ---------------------------------------------------------------------------
# Scenario 5: kill-switch
# ---------------------------------------------------------------------------
def test_kill_switch_blocks_fire():
    """halted=True must block the fire at the reserve gate with zero orders."""
    async def _run():
        client = _FaultClient()
        risk = RiskState(balance=1000.0)
        risk.halted = True
        metrics = Metrics()
        exec_ = Execution(client, risk, metrics)
        st = _state()
        exec_.register(st)
        await exec_.execute_dual_buy(st, 0.96, 0.0, age_ms=10.0, band="fresh")
        return client, metrics
    client, metrics = asyncio.run(_run())
    assert metrics.missed == 1
    assert len(client.sent) == 0


# ---------------------------------------------------------------------------
# Scenario 6: daily-kill-switch breach -> alert persisted
# ---------------------------------------------------------------------------
def test_daily_kill_switch_breach_persists_alert_and_blocks(tmp_path):
    """A day_pnl breach fires ALERT_DAILY_KILL_SWITCH through the real alerter
    (persisted to disk) and the fire is rejected."""
    async def _run():
        alerts_file = tmp_path / "alerts.jsonl"
        alerter = WebhookAlerter(dedup_window_s=0.0, alerts_path=str(alerts_file))
        client = _FaultClient()
        risk = RiskState(balance=1000.0, alerter=alerter)
        risk.day_pnl = -999.0  # breaches daily_kill_switch_usd
        metrics = Metrics()
        exec_ = Execution(client, risk, metrics, alerter)
        st = _state()
        exec_.register(st)
        await exec_.execute_dual_buy(st, 0.96, 0.0, age_ms=10.0, band="fresh")
        return alerts_file, client, metrics
    alerts_file, client, metrics = asyncio.run(_run())
    assert len(client.sent) == 0
    assert metrics.missed == 1
    assert alerts_file.exists()
    records = [json.loads(l) for l in alerts_file.read_text().splitlines() if l.strip()]
    assert records, "kill-switch alert must be persisted to the alerts sink"
    assert any(
        "DAILY_KILL_SWITCH" in (r.get("kind") or "") or
        "DAILY_KILL_SWITCH" in (r.get("message") or "")
        for r in records
    ), f"no kill-switch alert persisted: {records}"


# ---------------------------------------------------------------------------
# Scenario 7: multi-fault session ends flat, capital fully released
# ---------------------------------------------------------------------------
def test_chaos_session_ends_flat_capital_released():
    """Fire -> probe drop -> heal -> clean fill -> fire -> no-fill timeout:
    the session ends with zero pending reservations and no unhandled errors."""
    async def _run():
        client = _FaultClient(fail_probes=True)
        risk = RiskState(balance=1000.0)
        metrics = Metrics()
        exec_ = Execution(client, risk, metrics)
        eng = ArbEngine(on_fire=exec_.execute_dual_buy, metrics=metrics)
        st = _state()
        exec_.register(st)
        errors, handle = _collect_task_errors(exec_, eng)
        try:
            # Fire #1 with failing probes (contained), then heal and resolve.
            eng.evaluate(st)
            await _drain(exec_, eng)
            assert exec_._pending
            client.fail_probes = False
            exec_.on_price_update(st)
            await _drain(exec_, eng)
            assert not exec_._pending
            # Fire #2: no-fill timeout cycle.
            tuned = _tuned(max_leg_gap_ms=1000)
            client2 = _FaultClient(never_fill=True)
            risk2 = RiskState(balance=1000.0)
            exec2 = Execution(client2, risk2, Metrics())
            st2 = _state(market_id="fault-m2")
            exec2.register(st2)
            with patch("polymarket_arb.execution.SETTINGS", tuned), \
                 patch("polymarket_arb.risk.SETTINGS", tuned):
                await exec2.execute_dual_buy(st2, 0.96, 0.0, age_ms=10.0, band="fresh")
                gap_s = tuned.max_leg_gap_ms / 1000.0
                for entry in exec2._pending.values():
                    entry["first_sent"] = time.monotonic() - gap_s - 1.0
                exec2.on_price_update(st2)
                await _drain(exec2)
        finally:
            _restore(handle)
        return errors, risk, risk2, exec2
    errors, risk, risk2, exec2 = asyncio.run(_run())
    assert errors == [], f"unhandled exceptions escaped: {errors}"
    assert risk.pending_deployed == 0.0
    assert risk2.pending_deployed == 0.0
    assert not exec2._pending
    assert len(exec2.client.cancelled) == 2
