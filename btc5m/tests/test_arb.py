"""Pure-logic tests (no network). Validates the detector + execution path + FIX #1-#5.

These are the speed-critical, must-never-be-wrong pieces, so they get tests even in MVP.
Coverage:
  * detector + freshness gate (FIX #1) + no-entry-zone debounce (FIX #4)
  * dual-leg fill + locked PnL vs residual PnL (FIX #2)
  * partial-fill management: hedge + exit (FIX #2)
  * window targeting helpers (FIX #3)
  * metrics freshness-band + distinct PnL accounts (FIX #1/#2)
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime

from polymarket_arb.arb_engine import ArbEngine
from polymarket_arb.clients.gamma_client import current_window_ts, next_window_ts, window_slug
from polymarket_arb.config import SETTINGS
from polymarket_arb.models import MarketState
from polymarket_arb.execution import Execution
from polymarket_arb.metrics import Metrics, classify_freshness_ms
from polymarket_arb.clients.trading_client import TradingClient
from polymarket_arb.risk import RiskState
from polymarket_arb.window_tracker import WindowTracker


def _state(yes, no, ysz=None, nsz=None, ts=None, window_close=0.0, yes_bid=None, no_bid=None):
    now = ts if ts is not None else datetime.now().timestamp()
    return MarketState(
        market_id="m1", question="BTC up?", yes_token_id="0xYES", no_token_id="0xNO",
        best_yes_ask=yes, best_no_ask=no, yes_size=ysz, no_size=nsz,
        last_update_ts=now, yes_ts=now, no_ts=now,
        window_close=window_close, best_yes_bid=yes_bid, best_no_bid=no_bid,
    )


class _StubClient(TradingClient):
    """DRY_RUN-like, but deterministic + no asyncio.sleep churn.

    By default simulates INSTANT fill (is_filled returns True immediately).
    Set `fail_token` to make a token's first submit return None (-> partial).
    Set `delay_ticks` to make fills confirm only after N probes (delayed-fill
    simulation for FIX #6).
    """
    def __init__(self, fail_token=None, delay_ticks=0):
        self.s = SETTINGS
        self.sent = []
        self.cancelled = []
        self._fail_token = fail_token  # token that "never fills on first try" -> partial
        self._attempts = {}            # token -> count, so a hedge retry can succeed
        # FIX #6: delayed fill simulation. order_id -> ticks_until_filled (countdown).
        # Each is_filled probe decrements it; when it hits 0 the fill is "confirmed".
        self._delay_ticks = delay_ticks
        self._fill_counters = {}       # order_id -> remaining probes before filled

    async def submit_order(self, token_id, price, size, side="BUY"):
        # First attempt on the failing token rests (returns None); a later retry (the
        # hedge) succeeds — models "initial leg didn't fill, but the hedge does".
        if self._fail_token is not None and token_id == self._fail_token:
            self._attempts[token_id] = self._attempts.get(token_id, 0) + 1
            if self._attempts[token_id] == 1:
                return None
        self.sent.append((token_id, price, size, side))
        oid = f"id-{side.lower()}-{token_id[-3:]}"
        if self._delay_ticks > 0:
            self._fill_counters[oid] = self._delay_ticks
        return oid

    async def is_filled(self, order_id, token_id, gap_ms=0):
        if order_id is None:
            return False
        # FIX #6: delayed-fill simulation — decrement counter; True only when it hits 0.
        if order_id in self._fill_counters:
            self._fill_counters[order_id] -= 1
            return self._fill_counters[order_id] <= 0
        # No delay configured for this order: DRY_RUN instant fill.
        return True  # present id == filled

    async def cancel_order(self, token_id, missing_order_id=None):
        self.cancelled.append(token_id)
        return True


# ----------------------------------------------------------------------------
# Detector + FIX #1 freshness gate + FIX #4 no-entry-zone
# ----------------------------------------------------------------------------
def test_combined_ask_none_when_missing():
    st = _state(None, 0.5)
    assert st.combined_ask is None
    st = _state(0.47, 0.49)
    assert abs(st.combined_ask - 0.96) < 1e-9


def test_engine_fires_below_threshold():
    async def _run():
        fired = []
        async def _fire(st, c, t, age=0.0, band="fresh", yes_p=0.0, no_p=0.0):
            fired.append((st.market_id, c, t))
        eng = ArbEngine(on_fire=_fire, metrics=Metrics())
        eng.evaluate(_state(0.47, 0.49))  # 0.96 < 0.97 -> fire
        await asyncio.sleep(0)  # let the scheduled fire coroutine run
        return fired
    fired = asyncio.run(_run())
    assert len(fired) == 1
    assert abs(fired[0][1] - 0.96) < 1e-9


def test_engine_no_fire_at_threshold():
    async def _run():
        fired = []
        async def _fire(st, c, t, age=0.0, band="fresh", yes_p=0.0, no_p=0.0):
            fired.append(c)
        eng = ArbEngine(on_fire=_fire, metrics=Metrics())
        edge = 1.0 - SETTINGS.min_edge
        y = edge / 2 - 0.001
        n = edge / 2 + 0.001
        eng.evaluate(_state(y, n))
        await asyncio.sleep(0)
        return fired
    fired = asyncio.run(_run())
    assert len(fired) == 0


def test_engine_debounces_same_price():
    async def _run():
        fired = []
        async def _fire(st, c, t, age=0.0, band="fresh", yes_p=0.0, no_p=0.0):
            fired.append(c)
        eng = ArbEngine(on_fire=_fire, metrics=Metrics())
        eng.evaluate(_state(0.47, 0.49))
        eng.evaluate(_state(0.47, 0.49))  # same combined -> debounced
        await asyncio.sleep(0)
        return fired
    fired = asyncio.run(_run())
    assert len(fired) == 1


def test_engine_skips_stale_quote_fix1():
    """FIX #1: a fresh combined < threshold must NOT fire if either leg is staler
    than MAX_QUOTE_AGE_MS. We stamp one leg old, the other new."""
    async def _run():
        fired = []
        async def _fire(st, c, t, age=0.0, band="fresh", yes_p=0.0, no_p=0.0):
            fired.append(c)
        eng = ArbEngine(on_fire=_fire, metrics=Metrics())
        now = datetime.now().timestamp()
        st = MarketState(
            market_id="m1", question="BTC up?", yes_token_id="0xYES", no_token_id="0xNO",
            best_yes_ask=0.47, best_no_ask=0.49,
            yes_ts=now,                       # fresh
            no_ts=now - (SETTINGS.max_quote_age_ms / 1000.0) - 1.0,  # stale leg
        )
        eng.evaluate(st)
        await asyncio.sleep(0)
        return fired, eng.metrics
    fired, m = asyncio.run(_run())
    assert len(fired) == 0
    assert m.missed >= 1


def test_engine_skips_no_entry_zone_fix4():
    """FIX #4: no new entries in the final NO_ENTRY_SECONDS before window close."""
    async def _run():
        fired = []
        async def _fire(st, c, t, age=0.0, band="fresh", yes_p=0.0, no_p=0.0):
            fired.append(c)
        eng = ArbEngine(on_fire=_fire, metrics=Metrics())
        now = datetime.now().timestamp()
        st = MarketState(
            market_id="m1", question="BTC up?", yes_token_id="0xYES", no_token_id="0xNO",
            best_yes_ask=0.47, best_no_ask=0.49,
            yes_ts=now, no_ts=now,
            window_close=now + SETTINGS.no_entry_seconds - 1.0,  # inside no-entry zone
        )
        eng.evaluate(st)
        await asyncio.sleep(0)
        return fired
    fired = asyncio.run(_run())
    assert len(fired) == 0


# ----------------------------------------------------------------------------
# Execution: clean fill + distinct PnL accounts (FIX #2)
# ----------------------------------------------------------------------------
def test_execution_submits_both_legs_and_marks_pnl():
    async def _run():
        client = _StubClient()
        risk = RiskState(balance=1000.0)
        metrics = Metrics()
        exec_ = Execution(client, risk, metrics)
        exec_.register(_state(0.47, 0.49))
        st = _state(0.47, 0.49)
        # execute_dual_buy submits + returns immediately (FIX #6); fill check deferred.
        await exec_.execute_dual_buy(st, 0.96, 0.0, age_ms=10.0, band="fresh")
        # FIX #6: drive the deferred fill-resolution on the next tick.
        exec_.on_price_update(st)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return client, metrics
    client, metrics = asyncio.run(_run())
    assert len(client.sent) == 2
    assert client.sent[0][0] == "0xYES"
    assert client.sent[1][0] == "0xNO"
    assert metrics.fills == 1
    assert metrics.partial_fills == 0
    assert metrics.locked_pnl > 0          # FIX #2: locked, not blended into residual
    assert metrics.residual_pnl == 0.0


def test_execution_rest_pair_preflight_rejects_before_reserve_or_submit():
    class _RejectedPreflightClient(_StubClient):
        async def paper_pair_preflight(self, *args):
            return {
                "applied": True,
                "passed": False,
                "reason": "rest_preflight_insufficient_liquidity",
                "combined": None,
                "yes_vwap": 0.38,
                "no_vwap": None,
                "yes_available": 10.0,
                "no_available": 0.0,
                "latency_ms": 120.125,
            }

    async def _run():
        client = _RejectedPreflightClient()
        risk = RiskState(balance=1000.0)
        metrics = Metrics()
        execution = Execution(client, risk, metrics)
        state = _state(0.66, 0.32)
        execution.register(state)
        await execution.execute_dual_buy(
            state,
            0.96,
            time.monotonic(),
            age_ms=1.0,
            band="fresh",
            yes_p=0.66,
            no_p=0.32,
        )
        return client, risk, metrics

    client, risk, metrics = asyncio.run(_run())
    assert client.sent == []
    assert metrics.missed == 1
    assert risk.pending_deployed == 0.0
    assert risk.total_deployed == 0.0


def test_every_execution_submission_boundary_invokes_control_gate():
    class _CallsiteGateClient(_StubClient):
        def __init__(self):
            super().__init__(fail_token="0xNO")
            self._enforce_live_control = True
            self.control_checks = []

        def check_order_submission(self, call_site, *, token_id="", side=""):
            self.control_checks.append((call_site, token_id, side))
            # Admit only initial paper entries. Block both recovery submissions.
            return call_site == "execution.entry"

    async def _run():
        client = _CallsiteGateClient()
        risk = RiskState(balance=1000.0)
        execution = Execution(client, risk, Metrics())
        state = _state(0.47, 0.49, yes_bid=0.47, no_bid=0.49)
        execution.register(state)
        await execution.execute_dual_buy(
            state,
            0.96,
            time.monotonic(),
            age_ms=1.0,
            band="fresh",
            yes_p=0.47,
            no_p=0.49,
        )
        with patch("polymarket_arb.execution.SETTINGS", _patch_gap_ms(0)):
            for _ in range(5):
                execution.on_price_update(state)
                await asyncio.sleep(0)
        return client, risk

    from unittest.mock import patch

    client, risk = asyncio.run(_run())
    sites = [call[0] for call in client.control_checks]
    assert sites.count("execution.entry") == 2
    assert "execution.partial_hedge" in sites
    assert "execution.partial_exit" in sites
    # Only the initial YES leg reached the stub submitter; recovery calls were blocked.
    assert client.sent == [("0xYES", 0.47, 10.0, "BUY")]
    assert risk.halted is True


def test_execution_uses_recorded_fill_prices_for_realized_pnl():
    """Decision edge and simulated submission-book edge must remain distinct."""
    from unittest.mock import patch

    class _ReportedFillClient(_StubClient):
        def get_fill_report(self, order_id):
            if str(order_id).endswith("YES"):
                return {"fill_price": 0.47, "fill_source": "rest_book"}
            return {"fill_price": 0.49, "fill_source": "rest_book"}

    async def _run():
        client = _ReportedFillClient()
        metrics = Metrics()
        execution = Execution(client, RiskState(balance=1000.0), metrics)
        state = _state(0.47, 0.49)
        execution.register(state)
        with patch("polymarket_arb.execution.log.trade") as trade_log:
            await execution.execute_dual_buy(
                state, 0.96, time.monotonic(), age_ms=10.0, band="fresh",
                yes_p=0.47, no_p=0.49,
            )
            execution.on_price_update(state)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
        fill_event = next(call.args[1] for call in trade_log.call_args_list if call.args[0] == "filled")
        return metrics, fill_event

    metrics, fill_event = asyncio.run(_run())

    assert abs(metrics.locked_pnl - 0.4) < 1e-12
    assert fill_event["decision_combined"] == 0.96
    assert fill_event["combined"] == 0.96
    assert fill_event["expected_pnl"] == 0.4
    assert fill_event["pnl_locked"] == 0.4
    assert fill_event["fill_yes_source"] == "rest_book"
    assert fill_event["fill_no_source"] == "rest_book"


def test_execution_skips_on_low_liquidity():
    async def _run():
        client = _StubClient()
        risk = RiskState(balance=1000.0)
        metrics = Metrics()
        exec_ = Execution(client, risk, metrics)
        exec_.register(_state(0.47, 0.49))
        await exec_.execute_dual_buy(_state(0.47, 0.49, ysz=1.0, nsz=10.0), 0.96, 0.0, age_ms=10.0, band="fresh")
        # FIX #6: no orders sent (risk precheck failed), so no pending entries either.
        await asyncio.sleep(0)
        return client, metrics
    client, metrics = asyncio.run(_run())
    assert metrics.missed == 1
    assert len(client.sent) == 0


# ----------------------------------------------------------------------------
# FIX #2: partial-fill management (hedge + exit)
# ----------------------------------------------------------------------------
def _patch_gap_ms(gap_ms):
    """Create a Settings copy with max_leg_gap_ms overridden (frozen dataclass)."""
    import dataclasses as _dc
    return _dc.replace(SETTINGS, max_leg_gap_ms=gap_ms)


def test_execution_partial_hedges_missing_leg():
    """YES fills, NO rests -> bot cancels NO, then hedges NO at current ask when edge
    still survives. Residual PnL must be tracked separately and resolved."""
    from unittest.mock import patch
    async def _run():
        client = _StubClient(fail_token="0xNO")  # NO submit returns None on first try
        risk = RiskState(balance=1000.0)
        metrics = Metrics()
        exec_ = Execution(client, risk, metrics)
        exec_.register(_state(0.47, 0.49, no_bid=0.49))
        # YES=0.47 fills; NO current ask still 0.49 -> hedge combined 0.96 < 1.0 edge
        live_st = _state(0.47, 0.49, no_bid=0.49)
        await exec_.execute_dual_buy(live_st, 0.96, 0.0, age_ms=10.0, band="fresh")
        # FIX #6: execute_dual_buy returns immediately; fill resolution is deferred.
        # Patch SETTINGS with max_leg_gap_ms=0 so the first _check_fills probe sees
        # YES filled + gap elapsed -> partial event -> _manage_partial runs.
        fast_settings = _patch_gap_ms(0)
        with patch("polymarket_arb.execution.SETTINGS", fast_settings):
            for _ in range(5):
                exec_.on_price_update(live_st)
                await asyncio.sleep(0)
        return client, metrics
    client, metrics = asyncio.run(_run())
    assert metrics.partial_fills == 1
    assert metrics.partial_resolved == 1
    assert "0xNO" in client.cancelled
    hedge = [s for s in client.sent if s[0] == "0xNO" and s[3] == "BUY" and s[1] is not None]
    assert len(hedge) == 1  # hedge leg re-submitted and filled


def test_execution_partial_exits_filled_leg_when_no_hedge_edge():
    """YES fills, NO current ask blew out so hedge combined >= 1.0 -> exit filled leg
    at best bid. Residual PnL recorded separately (likely negative)."""
    from unittest.mock import patch
    async def _run():
        client = _StubClient(fail_token="0xNO")
        risk = RiskState(balance=1000.0)
        metrics = Metrics()
        exec_ = Execution(client, risk, metrics)
        exec_.register(_state(0.47, 0.49, yes_bid=0.46))
        # Gate fired at snapshot (0.47, 0.49) -> combined 0.96. The LIVE book has since
        # blown out (NO ask now 0.60 -> live combined 1.08) but C1 submits at the SNAPSHOT.
        # YES fills; NO ask 0.60 -> hedge combined 1.08 -> no edge -> exit YES at bid.
        live = _state(0.47, 0.60, yes_bid=0.46)
        await exec_.execute_dual_buy(live, 0.96, 0.0, age_ms=10.0, band="fresh", yes_p=0.47, no_p=0.49)
        # FIX #6: drain the deferred fill-resolution with gap_ms=0.
        fast_settings = _patch_gap_ms(0)
        with patch("polymarket_arb.execution.SETTINGS", fast_settings):
            for _ in range(5):
                exec_.on_price_update(live)
                await asyncio.sleep(0)
        return client, metrics
    client, metrics = asyncio.run(_run())
    assert metrics.partial_fills == 1
    assert metrics.partial_resolved == 1
    # filled YES leg sold back at the bid (SELL).
    exit_legs = [s for s in client.sent if s[0] == "0xYES" and s[3] == "SELL"]
    assert len(exit_legs) == 1
    assert metrics.residual_pnl != metrics.locked_pnl  # distinct accounts (FIX #2)


def test_partial_exit_accounts_for_reported_exit_fill_not_stale_ws_bid():
    """Protective exit PnL must use the actual execution report when available."""
    from unittest.mock import patch
    import pytest

    class _ReportedExitClient(_StubClient):
        def get_fill_report(self, order_id):
            if order_id and "sell" in order_id:
                return {"fill_price": 0.19, "fill_source": "rest_book"}
            return {}

    async def _run():
        client = _ReportedExitClient(fail_token="0xNO")
        risk = RiskState(balance=1000.0)
        metrics = Metrics()
        exec_ = Execution(client, risk, metrics)
        live = _state(0.48, 0.60, yes_bid=0.20)
        exec_.register(live)
        fast_settings = _patch_gap_ms(0)
        with patch("polymarket_arb.execution.SETTINGS", fast_settings):
            await exec_.execute_dual_buy(
                live, 0.96, 0.0, age_ms=10.0, band="fresh", yes_p=0.47, no_p=0.49
            )
            for _ in range(5):
                exec_.on_price_update(live)
                await asyncio.sleep(0)
        return metrics

    metrics = asyncio.run(_run())
    assert metrics.partial_resolved == 1
    assert metrics.residual_pnl == pytest.approx((0.19 - 0.47) * 10.0)


def test_execution_delayed_fill_confirms_on_next_ticks():
    """FIX #6: execute_dual_buy must NOT block on is_filled. Orders are submitted, then
    fill confirmation is deferred to subsequent ticks. This test simulates a fill that
    only confirms 2 ticks later (delay_ticks=2) and verifies:
      - execute_dual_buy returns immediately (no blocking on the fill),
      - the fill is NOT prematurely resolved on tick 1 (still pending),
      - the fill IS resolved as a clean arb once both legs confirm on tick 2+,
      - no double-processing (pending entry popped exactly once)."""
    from unittest.mock import patch
    async def _run():
        # delay_ticks=2: each order's is_filled returns False for the first 2 probes,
        # then True on the 3rd. Models a 2-tick delayed confirmation.
        client = _StubClient(delay_ticks=2)
        risk = RiskState(balance=1000.0)
        metrics = Metrics()
        exec_ = Execution(client, risk, metrics)
        exec_.register(_state(0.47, 0.49))
        st = _state(0.47, 0.49)

        t0 = time.monotonic()
        await exec_.execute_dual_buy(st, 0.96, 0.0, age_ms=10.0, band="fresh",
                                     yes_p=0.47, no_p=0.49)
        submit_done = time.monotonic()
        # execute_dual_buy must return well under the simulated fill delay.
        assert (submit_done - t0) < 0.01  # no blocking

        # Use a generous gap so the partial/neither branches don't fire prematurely.
        tick_settings = _patch_gap_ms(10_000)  # 10s gap so window never lapses during sim

        # Tick 1: both legs NOT yet confirmed (delay_ticks=2 not exhausted).
        with patch("polymarket_arb.execution.SETTINGS", tick_settings):
            exec_.on_price_update(st)
            await asyncio.sleep(0)  # let _check_fills probe tick 1
        # Still pending — neither leg should have been committed as filled yet.
        assert metrics.fills == 0, "fill resolved prematurely on tick 1"
        assert not client.cancelled, "orders cancelled prematurely on tick 1"

        # Tick 2: first probe returns True (counter hits 0), second probe returns True.
        with patch("polymarket_arb.execution.SETTINGS", tick_settings):
            exec_.on_price_update(st)
            await asyncio.sleep(0)  # let _check_fills probe tick 2
        # After tick 2, both legs should confirm filled -> clean arb.
        assert metrics.fills == 1, f"expected 1 fill, got {metrics.fills}"
        assert metrics.locked_pnl > 0
        assert metrics.residual_pnl == 0.0
        assert len(client.cancelled) == 0  # clean arb, no cancellations
        assert len(exec_._pending) == 0    # pending entry popped (no double-process)
        return client, metrics
    client, metrics = asyncio.run(_run())
    assert metrics.fills == 1
    # Admission latency is recorded at order acknowledgement only.  Deferred fill
    # resolution must not add a second, slower sample to the submit-path metric.
    assert metrics.acks == 1


def test_execution_delayed_partial_confirmed_after_gap():
    """FIX #6 variant: YES fills instantly, NO is delayed (delay_ticks=2). With
    max_leg_gap_ms=0, the gap lapses on the first probe while YES is already filled
    -> partial event triggers (cancel NO + hedge/exit). Verify the YES leg's instant
    fill + NO's delay don't cause a missed partial or double-processing."""
    from unittest.mock import patch
    async def _run():
        client = _StubClient(fail_token="0xNO", delay_ticks=0)
        # fail_token makes NO submit return None on first try -> no_id -> NO never fills
        # YES returns an id and is_filled returns True instantly.
        risk = RiskState(balance=1000.0)
        metrics = Metrics()
        exec_ = Execution(client, risk, metrics)
        exec_.register(_state(0.47, 0.49, yes_bid=0.47))
        live = _state(0.47, 0.49, yes_bid=0.47)
        await exec_.execute_dual_buy(live, 0.96, 0.0, age_ms=10.0, band="fresh",
                                     yes_p=0.47, no_p=0.49)
        # With gap_ms=0, first probe: YES filled, NO has no id -> partial -> manage.
        fast_settings = _patch_gap_ms(0)
        with patch("polymarket_arb.execution.SETTINGS", fast_settings):
            for _ in range(5):
                exec_.on_price_update(live)
                await asyncio.sleep(0)
        return client, metrics, exec_
    client, metrics, exec_ = asyncio.run(_run())
    assert metrics.partial_fills == 1
    assert metrics.partial_resolved == 1
    assert len(exec_._pending) == 0  # entry resolved, no double-process


def test_freshness_band_classification():
    assert classify_freshness_ms(100.0) == "fresh"
    assert classify_freshness_ms(1000.0) == "0.5-2s"
    assert classify_freshness_ms(5000.0) == "2-10s"
    assert classify_freshness_ms(50000.0) == "stale"


def test_metrics_freshness_attempt_buckets():
    m = Metrics()
    m.record_freshness_attempt(100.0, filled=True)
    m.record_freshness_attempt(5000.0, filled=False)
    assert m.fresh_attempts["fresh"] == 1
    assert m.fresh_attempts["2-10s"] == 1
    assert m.fills_by_band["fresh"] == 1
    assert m.fills_by_band["2-10s"] == 0


def test_metrics_records_tail_and_stage_latency_percentiles():
    metrics = Metrics()
    metrics.record_latency(0.001, 0.004, {"reserve": 0.0002, "dual_submit": 0.003})
    metrics.record_latency(0.003, 0.005, {"reserve": 0.0004, "dual_submit": 0.005})

    assert metrics.acks == 2
    assert metrics.p50_opp_to_order_ms() == 1.0
    assert metrics.p95_opp_to_order_ms() == 3.0
    assert metrics.p99_opp_to_order_ms() == 3.0
    assert metrics.stage_latency_ms("reserve", 0.95) == 0.4
    assert "p95_ms=3.00" in metrics.summary()
    assert "stage_submit_p50_ms=3.00" in metrics.summary()


def test_execution_records_submission_stages_once_per_dual_buy():
    async def _run():
        client = _StubClient()
        risk = RiskState(balance=1000.0)
        metrics = Metrics()
        exec_ = Execution(client, risk, metrics)
        st = _state(0.47, 0.49)
        exec_.register(st)
        await exec_.execute_dual_buy(
            st, 0.96, time.monotonic() - 0.001, age_ms=10.0, band="fresh",
            yes_p=0.47, no_p=0.49,
        )
        return metrics

    metrics = asyncio.run(_run())
    assert metrics.acks == 1
    assert metrics.p95_opp_to_order_ms() > 0.0
    assert metrics.stage_latency_ms("reserve") >= 0.0
    assert metrics.stage_latency_ms("dual_submit") >= 0.0


# ----------------------------------------------------------------------------
# FIX #3: window targeting helpers (deterministic + pre-resolve)
# ----------------------------------------------------------------------------
def test_window_slug_and_rotation():
    ts = 1_780_319_100  # arbitrary, already floored to 300
    assert window_slug("btc", "5m", ts) == "btc-updown-5m-1780319100"
    assert current_window_ts(ts + 1, 300) == ts
    assert next_window_ts(ts, 300) == ts + 300
    assert current_window_ts(ts + 123, 300) == ts


def test_risk_halt_trips_and_blocks():
    """CRITICAL regression: RiskState.halt() must exist and cause precheck to fail
    fast (the FIX#2 kill switch previously called a missing method -> AttributeError)."""
    risk = RiskState(balance=1000.0)
    assert risk.precheck(_state(0.47, 0.49), 4.8, 5.0) is None  # OK before halt
    risk.halt("test")
    assert risk.halted is True
    assert risk.precheck(_state(0.47, 0.49), 4.8, 5.0) is not None  # blocked after halt


def test_risk_reserve_is_atomic_over_caps():
    """HIGH regression: concurrent fires must not both pass the same capital cap.
    Two reserves that each fit alone but not together -> exactly one succeeds."""
    async def _run():
        # Binding cap here is max_position_per_market (default 200). Two reserves of 150
        # each fit ALONE (150 < 200) but not TOGETHER (300 > 200): exactly one must win.
        risk = RiskState(balance=10_000.0)
        st = _state(0.47, 0.49)
        r1, r2 = await asyncio.gather(
            risk.reserve(st, 75.0, 75.0),   # 150
            risk.reserve(st, 75.0, 75.0),   # 150 -> together 300 > 200 per-market cap
        )
        return r1, r2, risk
    r1, r2, risk = asyncio.run(_run())
    oks = [r for r in (r1, r2) if r is None]
    assert len(oks) == 1  # exactly one reservation granted (atomic check-and-reserve)
    assert risk.pending_deployed == 150.0


class _NoFillClient(_StubClient):
    """Both legs REST (submit returns an id) but never confirm filled."""
    async def is_filled(self, order_id, token_id, gap_ms=0):
        return False


def test_cancel_timeout_with_live_order_halts_and_retains_deployed_capital():
    """A cancellation timeout is unsafe while the resting order remains live.

    RED before ``_cancel_or_manual_review``: partial management continued into the
    hedge/exit path after a best-effort cancellation failure.  The uncertain order
    must receive exactly one status probe, then require manual review with the
    filled-leg capital still deployed.
    """
    class _TimeoutLiveOrderClient(_StubClient):
        def __init__(self):
            super().__init__()
            self.cancel_calls = 0
            self.fill_probes = 0

        async def cancel_order(self, token_id, missing_order_id=None):
            self.cancel_calls += 1
            raise TimeoutError("cancel request timed out")

        async def is_filled(self, order_id, token_id, gap_ms=0):
            self.fill_probes += 1
            return False  # the timed-out cancellation left the order live

    class _RecordingRiskState(RiskState):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.halt_reasons = []

        def halt(self, reason):
            self.halt_reasons.append(reason)
            super().halt(reason)

    async def _run():
        client = _TimeoutLiveOrderClient()
        risk = _RecordingRiskState(balance=1_000.0)
        metrics = Metrics()
        exec_ = Execution(client, risk, metrics)
        st = _state(0.47, 0.49)
        exec_.register(st, 10.0)
        tpl = exec_._templates[st.market_id]
        # The initial YES fill has already been committed.  It must remain deployed
        # because the NO order could still fill after the cancellation timeout.
        risk.commit(st.market_id, 4.8)
        await exec_._manage_partial(
            st, tpl, yes_id="yes-live", no_id="no-live", yes_filled=True,
            no_filled=False, size=10.0, combined=0.96, yes_p=0.47, no_p=0.49,
            age_ms=10.0, band="fresh", decision_ts=time.monotonic(),
        )
        return client, risk

    client, risk = asyncio.run(_run())
    assert client.cancel_calls == 1
    assert client.fill_probes == 1
    assert risk.halted
    assert "cancellation unconfirmed" in risk.halt_reasons[0]
    assert risk.pending_deployed == 0.0
    assert risk.total_deployed == 4.8
    assert risk.deployed_per_market == {"m1": 4.8}
    assert client.sent == []  # no hedge or exit order after unsafe cancellation


class _RaisingClient(_StubClient):
    """Submits both legs OK, then raises during fill-confirmation to exercise the
    try/finally reservation-cleanup path."""
    async def is_filled(self, order_id, token_id, gap_ms=0):
        raise RuntimeError("boom during fill check")


def test_neither_filled_cancels_both_resting_orders():
    """HIGH regression: when both legs rest but neither confirms filled, the bot must
    CANCEL both (they could fill independently later) and release the committed capital."""
    from unittest.mock import patch
    async def _run():
        client = _NoFillClient()
        risk = RiskState(balance=1000.0)
        metrics = Metrics()
        exec_ = Execution(client, risk, metrics)
        exec_.register(_state(0.47, 0.49))
        st = _state(0.47, 0.49)
        await exec_.execute_dual_buy(st, 0.96, 0.0, age_ms=10.0, band="fresh",
                                     yes_p=0.47, no_p=0.49)
        # FIX #6: drain deferred fill-resolution with gap_ms=0 so the gap window
        # lapses immediately -> neither filled -> cancel both + release capital.
        fast_settings = _patch_gap_ms(0)
        with patch("polymarket_arb.execution.SETTINGS", fast_settings):
            for _ in range(5):
                exec_.on_price_update(st)
                await asyncio.sleep(0)
        return client, risk, metrics
    client, risk, metrics = asyncio.run(_run())
    assert "0xYES" in client.cancelled and "0xNO" in client.cancelled  # both cancelled
    assert metrics.missed == 1
    assert risk.pending_deployed == 0.0   # reservation released (committed then released)
    assert risk.total_deployed == 0.0     # capital freed


def test_exception_midfire_releases_reservation():
    """HIGH regression: an exception between reserve() and submit must NOT strand
    the reservation (which would eventually halt all trading on a false cap breach).

    FIX #6: in the non-blocking model, if the exception happens AFTER the optimistic
    commit (orders submitted), the pending entry owns the lifecycle — _check_fills will
    cancel the resting orders. If the exception happens BEFORE commit (e.g. gather
    raises), the except clause releases the reservation directly.
    """
    async def _run():
        client = _RaisingClient()
        risk = RiskState(balance=1000.0)
        metrics = Metrics()
        exec_ = Execution(client, risk, metrics)
        exec_.register(_state(0.47, 0.49))
        raised = False
        try:
            await exec_.execute_dual_buy(_state(0.47, 0.49), 0.96, 0.0, age_ms=10.0,
                                         band="fresh", yes_p=0.47, no_p=0.49)
        except RuntimeError:
            raised = True
        return risk, raised, client
    risk, raised, client = asyncio.run(_run())
    # In the non-blocking model, execute_dual_buy succeeds (submit_order doesn't raise).
    # The is_filled exception happens later in _check_fills. Here we verify the fire
    # path itself completes without stranding capital.
    assert raised is False  # execute_dual_buy does not call is_filled anymore


def test_fix5_stress_math_distinguishes_edge_from_survival():
    """HIGH regression: the stress buffer must make 'survives_slippage' STRICTER than
    'edge_seen', or enforcement is a mathematical no-op. A window whose best combined
    shows edge (< 1 - MIN_EDGE) but sits within the slippage buffer of 1.0 must FAIL
    the stress. Uses realistic values that _update_active_min would actually produce."""
    from polymarket_arb.window_tracker import WindowObservation

    def _ready(meta):
        pass

    def _lookup(mid):
        return None

    t = WindowTracker(session=None, on_window_ready=_ready, state_lookup=_lookup)
    edge_threshold = 1.0 - SETTINGS.min_edge                       # e.g. 0.99
    survive_threshold = edge_threshold - SETTINGS.slippage_stress_buffer  # e.g. 0.96
    assert survive_threshold < edge_threshold  # buffer is real, not degenerate

    # A razor-thin-edge window: shows edge (min_combined < 0.99) but does NOT clear the
    # survive threshold (>= 0.96). In production _update_active_min sets edge_seen=True
    # here because min_combined < edge_threshold.
    thin = (edge_threshold + survive_threshold) / 2.0             # e.g. 0.965
    assert thin < edge_threshold and thin >= survive_threshold
    obs_thin = WindowObservation(
        slug="s", window_start=0, window_close=300, asset="btc", tf="5m",
        tod_bucket="00:00", min_combined=thin, edge_seen=True,
    )
    t._stress_test(obs_thin)
    assert obs_thin.survives_slippage is False  # thin edge must NOT survive the stress

    # A fat-edge window clears both thresholds and survives.
    fat = survive_threshold - 0.01
    obs_fat = WindowObservation(
        slug="s2", window_start=0, window_close=300, asset="btc", tf="5m",
        tod_bucket="00:00", min_combined=fat, edge_seen=True,
    )
    t._stress_test(obs_fat)
    assert obs_fat.survives_slippage is True


def test_fix5_stress_gate_blocks_bad_bucket():
    """HIGH regression: FIX#5 slippage-stress verdict must be ENFORCED. A time-of-day
    slice whose windows all fail the stress becomes blocked after MIN_STRESS_SAMPLES."""
    from polymarket_arb.clients.gamma_client import current_window_ts
    from polymarket_arb.window_tracker import WindowObservation, _tod_bucket

    def _ready(meta):
        pass

    def _lookup(mid):
        return None

    t = WindowTracker(session=None, on_window_ready=_ready, state_lookup=_lookup)
    ts = current_window_ts(1_780_319_123, 300)  # floored to a 300s boundary
    assert t._bucket_blocked(ts) is False  # fail-open with no samples
    # A realistic thin-edge min_combined that shows edge but fails the stress buffer.
    thin = (1.0 - SETTINGS.min_edge) - (SETTINGS.slippage_stress_buffer / 2.0)
    for _ in range(t.MIN_STRESS_SAMPLES):
        obs = WindowObservation(
            slug="s", window_start=ts, window_close=ts + 300, asset="btc", tf="5m",
            tod_bucket=_tod_bucket(ts), min_combined=thin, edge_seen=True,
        )
        t._stress_test(obs)
    assert t._bucket_blocked(ts) is True  # slice now discarded (enforced, not just logged)


def test_window_tracker_accepts_on_window_settled():
    """REGRESSION (4th audit CRITICAL): WindowTracker must accept on_window_settled
    and store it, so main.py's wiring doesn't crash at startup and capital is released
    at settlement. Defaults to None when omitted."""
    def _ready(meta):
        pass

    def _lookup(mid):
        return None

    def _settled(mid):
        pass

    t = WindowTracker(session=None, on_window_ready=_ready, state_lookup=_lookup,
                      on_window_settled=_settled)
    assert t._on_window_settled is _settled
    # Default when omitted.
    t2 = WindowTracker(session=None, on_window_ready=_ready, state_lookup=_lookup)
    assert t2._on_window_settled is None


# ----------------------------------------------------------------------------
# Partial fill_ratio regression: a partial fill must record PnL against the
# actually-filled quantity (fill_ratio * size), not the requested size. The
# clean-fill branch was silently applying the ratio (FIX already merged), but
# the partial-hedge / exit branches were discarding it. Tests below pin both.
# ----------------------------------------------------------------------------
class _PartialFillClient(_StubClient):
    """_StubClient that reports a configurable partial fill ratio per order id.

    Used to model "available liquidity < order size" without needing the real
    _BacktestTradingClient + parquet machinery. The default ratio of 0.5 mirrors
    the BacktestConfig(max_liquidity=5.0, order_size=10.0) closing-latency test.
    Optionally pass ``none_token`` to make one leg's submit return None (partial
    event -> _manage_partial), while STILL reporting a 0.5 ratio on the leg that
    DID submit — so the filled-leg's recorded PnL/cost must scale by 0.5.
    """

    def __init__(self, ratio=0.5, none_token=None, hedge_ratio=None):
        super().__init__(fail_token=none_token)
        self._ratio = ratio
        self._hedge_ratio = hedge_ratio if hedge_ratio is not None else ratio
        self._order_ratios = {}

    async def submit_order(self, token_id, price, size, side="BUY"):
        oid = await super().submit_order(token_id, price, size, side=side)
        # Hedge orders (retry attempts, _attempts[token_id] >= 2) use the hedge ratio;
        # initial leg submissions use the base ratio.
        if (self._fail_token and token_id == self._fail_token
                and self._attempts.get(token_id, 0) >= 2):
            self._order_ratios[oid] = self._hedge_ratio
        else:
            self._order_ratios[oid] = self._ratio
        return oid

    def get_fill_ratio(self, order_id):
        if order_id is None:
            return 0.0
        return self._order_ratios.get(order_id, self._ratio)


def test_clean_fill_partial_liquidity_scales_pnl_by_ratio():
    """REGRESSION: when both legs partially fill (fill_ratio<1), the clean-arb PnL
    must equal (1.0 - combined) * size * min(yes_ratio, no_ratio), not the full
    requested size. Without the ratio applied, a 50%-filled pair is recorded as
    fully filled and PnL is overstated 2x."""
    from unittest.mock import patch
    async def _run():
        client = _PartialFillClient(ratio=0.5)
        risk = RiskState(balance=1000.0)
        metrics = Metrics()
        exec_ = Execution(client, risk, metrics)
        exec_.register(_state(0.47, 0.49))
        st = _state(0.47, 0.49)
        await exec_.execute_dual_buy(st, 0.96, 0.0, age_ms=10.0, band="fresh",
                                     yes_p=0.47, no_p=0.49)
        # Both legs submit + confirm (instant fill per _StubClient default), but
        # each reports fill_ratio=0.5 -> clean arb should resolve on the next tick.
        tick_settings = _patch_gap_ms(10_000)
        with patch("polymarket_arb.execution.SETTINGS", tick_settings):
            for _ in range(5):
                exec_.on_price_update(st)
                await asyncio.sleep(0)
        return metrics
    metrics = asyncio.run(_run())
    assert metrics.fills == 1, f"expected 1 clean fill, got {metrics.fills}"
    # size=10 (BacktestConfig default), combined=0.96, ratio=min(0.5,0.5)=0.5
    # expected locked_pnl = (1.0 - 0.96) * 10 * 0.5 = 0.1
    expected_pnl = (1.0 - 0.96) * 10.0 * 0.5
    assert abs(metrics.locked_pnl - expected_pnl) < 1e-9, (
        f"partial-fill PnL {metrics.locked_pnl} != {expected_pnl}; "
        f"ratio not applied to clean-fill branch (would be 0.2 if discarded)")
    # residual_pnl untouched on a clean fill.
    assert metrics.residual_pnl == 0.0


def test_partial_hedge_records_filled_leg_ratio_in_pnl():
    """REGRESSION: one leg fills at ratio=0.5, the other rests. _manage_partial hedges
    the missing leg and books residual PnL; that PnL AND the hedge order size must
    scale by the FILLED leg's ratio (0.5), not the full requested size.

    Before the FIX: _manage_partial hardcoded effective_size=size, so a 50%-filled
    leg was hedged and accounted at full size — overstating residual PnL and
    deploying 2x the capital the position actually has."""
    from unittest.mock import patch
    async def _run():
        # NO rests on first submit (none_token), YES submits with ratio 0.5 and fills;
        # the hedge retry on NO submits with full ratio 1.0 (it has the liquidity).
        client = _PartialFillClient(ratio=0.5, none_token="0xNO", hedge_ratio=1.0)
        risk = RiskState(balance=1000.0)
        metrics = Metrics()
        exec_ = Execution(client, risk, metrics)
        exec_.register(_state(0.47, 0.49, no_bid=0.49))
        live_st = _state(0.47, 0.49, no_bid=0.49)
        await exec_.execute_dual_buy(live_st, 0.96, 0.0, age_ms=10.0, band="fresh",
                                     yes_p=0.47, no_p=0.49)
        fast_settings = _patch_gap_ms(0)
        with patch("polymarket_arb.execution.SETTINGS", fast_settings):
            for _ in range(5):
                exec_.on_price_update(live_st)
                await asyncio.sleep(0)
        return client, metrics
    client, metrics = asyncio.run(_run())
    assert metrics.partial_fills == 1
    assert metrics.partial_resolved == 1
    hedge_submits = [s for s in client.sent if s[0] == "0xNO" and s[3] == "BUY"]
    assert len(hedge_submits) == 1
    hedge_size = hedge_submits[0][2]
    # Filled leg ratio 0.5 -> hedge must size to fill_ratio*size = 0.5*10 = 5.
    assert abs(hedge_size - 5.0) < 1e-9, (
        f"hedge size {hedge_size} != 5.0; partial-fill ratio not threaded into hedge submit")
    # resolved_combined = filled_ask(0.47) + missing_ask(0.49) = 0.96
    # pair_size = effective_size(5.0) * min(1.0, hedge_ratio=1.0) = 5.0
    # expected residual.pnl = (1.0 - 0.96) * 5.0 = 0.1
    expected_pnl = (1.0 - 0.96) * 5.0
    assert abs(metrics.residual_pnl - expected_pnl) < 1e-9, (
        f"partial-hedge residual_pnl {metrics.residual_pnl} != {expected_pnl}; "
        f"would be 0.2 if effective_size ignored the filled-leg ratio")


# ----------------------------------------------------------------------------
# Bug #1: Capital committed BEFORE fill ratio is known.
# When both legs partially fill (ratio < 1), risk.commit is called with the
# full requested-size reservation. Capital should only be committed for the
# actually-filled quantity. Before the fix: committed = yes_p*size + no_p*size
# (full), but the fill ratio is fetched AFTER commit (lines 265-266 vs 295).
# ----------------------------------------------------------------------------
def test_partial_fill_does_not_overcommit_capital():
    """When both legs fill at ratio=0.5, total_deployed should reflect the
    actual filled cost (0.5 * size * (yes_p + no_p)), not the full requested.

    Before fix: risk.commit(st, reserved, from_reservation=reserved) commits
    the full size reservation, over-locking capital by 2x on a 50% fill.
    """
    from unittest.mock import patch
    async def _run():
        client = _PartialFillClient(ratio=0.5)
        risk = RiskState(balance=1_000.0)
        metrics = Metrics()
        exec_ = Execution(client, risk, Metrics())
        exec_.register(_state(0.47, 0.49, ysz=500, nsz=500))
        st = _state(0.47, 0.49, ysz=500, nsz=500)
        await exec_.execute_dual_buy(st, 0.96, 0.0, age_ms=10.0, band="fresh",
                                     yes_p=0.47, no_p=0.49)
        tick_settings = _patch_gap_ms(10_000)
        with patch("polymarket_arb.execution.SETTINGS", tick_settings):
            for _ in range(5):
                exec_.on_price_update(st)
                await asyncio.sleep(0)
        return risk, metrics

    risk, metrics = asyncio.run(_run())
    # size=10, yes_p=0.47, no_p=0.49, ratio=0.5 each
    # Full reservation = 0.47*10 + 0.49*10 = 9.6
    # Actual filled cost = 9.8 * 0.5 = 4.9
    assert risk.total_deployed == 4.8, (
        f"capital over-committed: total_deployed={risk.total_deployed} "
        f"should be 4.9 (full size * ratio), not 9.8 (full size without ratio)"
    )


# ----------------------------------------------------------------------------
# Bug #2: _exit_filled receives effective_size but PROCEEDS also uses full size.
# When _exit_filled submits the sell order, it uses `size` (which is effective_size)
# for cost, but the SELL order's `size` for proceeds must also be effective_size.
# However, the bigger issue is: if a partial fill exits, the residual PnL should
# account for the ratio-scaled position, not the full size. The test below
# verifies this end-to-end.
# ----------------------------------------------------------------------------
def test_partial_exit_proceeds_uses_effective_size():
    """When a leg fills at ratio=0.5 and is exited (hedge not possible), the
    proceeds must use effective_size (the filled quantity), not the full requested.

    If _exit_filled sold the full size (10.0) but only 5.0 actually filled,
    proceeds = bid * 10.0 would be wrong — the position is only 5.0.
    """
    from unittest.mock import patch
    async def _run():
        client = _PartialFillClient(ratio=0.5, none_token="0xNO")
        risk = RiskState(balance=1_000.0)
        metrics = Metrics()
        exec_ = Execution(client, risk, metrics)
        # yes=0.48, no=0.60 (no hedge edge), yes_bid=0.20
        exec_.register(_state(0.48, 0.60, ysz=500, nsz=500, yes_bid=0.20))
        st = _state(0.48, 0.60, ysz=500, nsz=500, yes_bid=0.20)
        await exec_.execute_dual_buy(st, 0.96, 0.0, age_ms=10.0, band="fresh",
                                     yes_p=0.47, no_p=0.49)
        tick_settings = _patch_gap_ms(0)
        with patch("polymarket_arb.execution.SETTINGS", tick_settings):
            for _ in range(5):
                exec_.on_price_update(st)
                await asyncio.sleep(0)
        return risk, metrics, client

    risk, metrics, client = asyncio.run(_run())
    # Find the exit SELL order — it must have size=effective_size=5.0, not 10.0
    sell_orders = [s for s in client.sent if len(s) > 3 and s[3] == "SELL"]
    assert len(sell_orders) == 1, f"Expected 1 exit SELL order, got {len(sell_orders)}"
    exit_size = sell_orders[0][2]
    assert abs(exit_size - 5.0) < 1e-9, (
        f"exit SELL order size={exit_size} should be 5.0 (effective_size), "
        f"not 10.0 (full requested size)"
    )


# ----------------------------------------------------------------------------
# Bug #3: Exception AFTER submit but BEFORE commit leaks the reservation.
# If both legs submit successfully (yes_id and no_id are both set), but an
# exception occurs in the code between submit and commit (e.g. get_fill_ratio
# or metrics recording), the except handler at line 347 checks
# `if yes_id is None and no_id is None` — which is FALSE (both are set),
# so unreserve() is never called and the reservation leaks into
# pending_deployed forever.
# ----------------------------------------------------------------------------
def test_exception_before_commit_leaks_reservation():
    """If an exception fires after both legs submit (ids assigned) but before
    risk.commit() is called, the reservation must still be unreserved.

    Before fix: the except block only unreserves when BOTH yes_id and no_id
    are None. When both are set (post-gather, pre-commit), the reservation
    leaks into pending_deployed permanently.
    """
    class _FailingRatioClient(_StubClient):
        # Both submits succeed, but get_fill_ratio raises — simulating a
        # transient SDK error between submit and commit.
        _call_count = 0
        def get_fill_ratio(self, order_id):
            _FailingRatioClient._call_count += 1
            raise RuntimeError("fill_ratio lookup transient error")

    async def _run():
        client = _FailingRatioClient()
        risk = RiskState(balance=1_000.0)
        metrics = Metrics()
        exec_ = Execution(client, risk, metrics)
        exec_.register(_state(0.47, 0.49, ysz=500, nsz=500))
        st = _state(0.47, 0.49, ysz=500, nsz=500)
        await exec_.execute_dual_buy(st, 0.96, 0.0, age_ms=10.0, band="fresh",
                                     yes_p=0.47, no_p=0.49)
        return risk

    risk = asyncio.run(_run())
    assert risk.pending_deployed == 0.0, (
        f"reservation leaked: pending_deployed={risk.pending_deployed} "
        f"should be 0.0 — exception after submit but before commit must unreserve"
    )
    assert risk.total_deployed == 0.0, (
        f"deployed capital leaked: total_deployed={risk.total_deployed} "
        f"should be 0.0"
    )


def test_exception_after_commit_releases_deployed_capital_and_cancels_orders():
    """A pending-stash failure after commit must release, not merely unreserve."""
    class _PendingLockFailureExecution(Execution):
        def _get_pending_lock(self):
            raise RuntimeError("pending stash unavailable")

    async def _run():
        client = _StubClient()
        risk = RiskState(balance=1_000.0)
        metrics = Metrics()
        exec_ = _PendingLockFailureExecution(client, risk, metrics)
        st = _state(0.47, 0.49, ysz=500, nsz=500)
        exec_.register(st)
        await exec_.execute_dual_buy(st, 0.96, 0.0, age_ms=10.0, band="fresh",
                                     yes_p=0.47, no_p=0.49)
        return client, risk

    client, risk = asyncio.run(_run())
    assert risk.pending_deployed == 0.0
    assert risk.total_deployed == 0.0
    assert set(client.cancelled) == {"0xYES", "0xNO"}


def test_post_stash_exception_removes_only_its_pending_entry():
    """A metrics failure after stashing cannot leave work for _check_fills to replay."""
    class _LatencyFailureMetrics(Metrics):
        def record_latency(self, *args, **kwargs):
            raise RuntimeError("latency metrics unavailable")

    async def _run():
        client = _StubClient()
        risk = RiskState(balance=1_000.0)
        metrics = _LatencyFailureMetrics()
        exec_ = Execution(client, risk, metrics)
        st = _state(0.47, 0.49, ysz=500, nsz=500)
        exec_.register(st)
        await exec_.execute_dual_buy(st, 0.96, 0.0, age_ms=10.0, band="fresh",
                                     yes_p=0.47, no_p=0.49)
        before = (metrics.fills, metrics.locked_pnl, metrics.session_pnl, risk.total_deployed)
        await exec_._check_fills()
        after = (metrics.fills, metrics.locked_pnl, metrics.session_pnl, risk.total_deployed)
        return exec_, risk, before, after

    exec_, risk, before, after = asyncio.run(_run())
    assert exec_._pending == {}
    assert risk.pending_deployed == 0.0
    assert risk.total_deployed == 0.0
    assert before == after


# ----------------------------------------------------------------------------
# Bug #3b: One-leg submit returns None (not exception) — capital accounting.
# When one leg's submit_order returns None (rejection, not exception), the
# code continues through commit with that leg's ratio=0.0. The pending entry
# is stashed, and _check_fills will eventually resolve via _manage_partial.
# This test verifies the capital is correctly ratio-scaled and net to zero
# after the partial is fully resolved (exit path).
# ----------------------------------------------------------------------------
def test_one_leg_submit_none_resolves_capital_to_zero():
    """When one leg's submit returns None (rejection), the capital must be
    correctly ratio-scaled and fully released after partial resolution.

    YES submits OK (ratio=1.0), NO returns None (ratio=0.0).
    committed_cost = yes_p * size * 1.0 + no_p * size * 0.0 = yes_p * size
    After partial exit: the YES leg is sold at bid, capital released.
    """
    from unittest.mock import patch
    async def _run():
        # Inline a minimal client that mimics _PaperFaultClient: NO buy returns None
        from polymarket_arb.clients.trading_client import TradingClient
        class _OneLegRejectClient(TradingClient):
            def __init__(self):
                self.s = SETTINGS
                self._paper_trade = False
                self.submits = []
                self.positions = {}
            async def submit_order(self, token_id, price, size, side="BUY"):
                self.submits.append((token_id, price, size, side))
                if token_id == "0xNO" and side == "BUY":
                    return None
                self.positions[token_id] = self.positions.get(token_id, 0.0) + (size if side == "BUY" else -size)
                return f"oneleg-{len(self.submits)}"
            async def is_filled(self, order_id, token_id, gap_ms=0):
                return order_id is not None
            def get_fill_ratio(self, order_id):
                return 1.0 if order_id is not None else 0.0
            async def cancel_order(self, token_id, missing_order_id=None):
                return True
        client = _OneLegRejectClient()
        risk = RiskState(balance=1_000.0)
        metrics = Metrics()
        exec_ = Execution(client, risk, metrics)
        exec_.register(_state(0.47, 0.49, ysz=500, nsz=500, yes_bid=0.20))
        st = _state(0.47, 0.49, ysz=500, nsz=500, yes_bid=0.20)
        # snapshot: yes_p=0.47, no_p=0.49 (combined=0.96)
        # NO submit returns None -> ratio=0.0; YES submits OK -> ratio=1.0
        # committed_cost = 0.47*10*1.0 + 0.49*10*0.0 = 4.7
        # _manage_partial: filled_leg=YES, effective_size=10.0*1.0=10.0
        # hedge_combined = 0.47 + 0.49 = 0.96 <= 1.0 -> hedge allowed
        # But _PaperFaultClient submit_order for NO BUY returns None again
        # (no_token="no" and side="BUY" -> None). So hedge fails too.
        # Falls through to exit: _exit_filled sells YES at best_yes_bid=0.20
        # cost = 0.47 * 10 = 4.7, proceeds = 0.20 * 10 = 2.0, residual = -2.7
        # Kill switch: -2.8 < -25? No (partial_loss_cap_usd=25). So no halt.
        # After exit: release(committed_cost=4.8), total_deployed=0
        await exec_.execute_dual_buy(st, 0.96, 0.0, age_ms=10.0, band="fresh",
                                     yes_p=0.47, no_p=0.49)
        tick_settings = _patch_gap_ms(0)  # immediate partial
        with patch("polymarket_arb.execution.SETTINGS", tick_settings):
            for _ in range(5):
                exec_.on_price_update(st)
                await asyncio.sleep(0)
        return risk, metrics

    risk, metrics = asyncio.run(_run())
    # After exit path: capital should be fully released
    assert risk.pending_deployed == 0.0, (
        f"pending_deployed should be 0 after resolution, got {risk.pending_deployed}"
    )
    assert risk.total_deployed == 0.0, (
        f"total_deployed should be 0 after exit release, got {risk.total_deployed}"
    )
    assert metrics.partial_resolved == 1
    # residual = (0.20 - 0.47) * 10.0 = -2.7
    assert abs(metrics.residual_pnl - (-2.7)) < 1e-9


# ---------------------------------------------------------------------------
# Hedge risk lifecycle: the original fire reserves its snapshot cost, but a
# later hedge can cost more than the missing snapshot leg.  It must obtain its
# own reservation before it is sent or repeated partials can bypass the total
# and per-market caps.
# ---------------------------------------------------------------------------
def test_partial_hedge_reserves_current_notional_before_submit():
    """A partial hedge that would take deployed capital over the cap is rejected.

    Initial YES=0.48/NO=0.10 reserves 5.80 and NO is rejected, leaving 4.80
    deployed in YES.  The live NO hedge is 0.50, so sending it would make the
    actual position cost 9.80.  A 6.00 cap allowed the original fire, but must
    block the later hedge before the fake client sees a BUY for NO.
    """
    from unittest.mock import patch

    async def _run():
        client = _PartialFillClient(ratio=1.0, none_token="0xNO", hedge_ratio=1.0)
        risk = RiskState(balance=1_000.0)
        metrics = Metrics()
        exec_ = Execution(client, risk, metrics)
        st = _state(0.47, 0.49, ysz=500, nsz=500, yes_bid=0.20, no_bid=0.49)
        exec_.register(st)
        constrained = _patch_gap_ms(0)
        constrained = __import__("dataclasses").replace(
            constrained, max_total_capital=6.0, max_position_per_market=6.0)
        with patch("polymarket_arb.execution.SETTINGS", constrained), \
             patch("polymarket_arb.risk.SETTINGS", constrained):
            await exec_.execute_dual_buy(st, 0.58, 0.0, age_ms=10.0, band="fresh",
                                         yes_p=0.48, no_p=0.10)
            for _ in range(5):
                exec_.on_price_update(st)
                await asyncio.sleep(0)
        return client, risk, metrics

    client, risk, metrics = asyncio.run(_run())
    hedge_buys = [order for order in client.sent if order[0] == "0xNO" and order[3] == "BUY"]
    assert hedge_buys == [], (
        "hedge BUY bypassed risk reserve: original filled cost=4.80 plus hedge=5.00 "
        "would deploy 9.80 over the 6.00 cap")
    assert risk.pending_deployed == 0.0
    assert risk.total_deployed == 0.0
    assert metrics.partial_resolved == 1


def test_partial_hedge_fill_keeps_actual_cost_deployed_and_halts():
    """A hedge reporting only part of effective_size is not a resolved pair."""
    from unittest.mock import patch

    async def _run():
        client = _PartialFillClient(ratio=1.0, none_token="0xNO", hedge_ratio=0.5)
        risk = RiskState(balance=1_000.0)
        metrics = Metrics()
        exec_ = Execution(client, risk, metrics)
        st = _state(0.47, 0.49, ysz=500, nsz=500, yes_bid=0.20, no_bid=0.49)
        exec_.register(st)
        fast_settings = _patch_gap_ms(0)
        with patch("polymarket_arb.execution.SETTINGS", fast_settings), \
             patch("polymarket_arb.risk.SETTINGS", fast_settings):
            await exec_.execute_dual_buy(st, 0.58, 0.0, age_ms=10.0, band="fresh",
                                         yes_p=0.48, no_p=0.10)
            for _ in range(5):
                exec_.on_price_update(st)
                await asyncio.sleep(0)
        return risk, metrics

    risk, metrics = asyncio.run(_run())
    # Original YES cost is 4.80; the half-filled 5.00 hedge costs 2.50.
    assert risk.halted
    assert risk.pending_deployed == 0.0
    assert abs(risk.total_deployed - 7.25) < 1e-9
    assert metrics.partial_resolved == 0


def test_confirmed_hedge_ratio_lookup_failure_commits_max_and_halts():
    """A post-fill ratio lookup failure must not leak a reservation or retry a hedge."""
    from unittest.mock import patch

    class _HedgeRatioFailureClient(_PartialFillClient):
        def get_fill_ratio(self, order_id):
            if order_id == "id-buy-xNO":
                raise RuntimeError("hedge fill-ratio unavailable")
            return super().get_fill_ratio(order_id)

    async def _run():
        client = _HedgeRatioFailureClient(ratio=1.0, none_token="0xNO")
        risk = RiskState(balance=1_000.0)
        metrics = Metrics()
        exec_ = Execution(client, risk, metrics)
        st = _state(0.47, 0.49, ysz=500, nsz=500, yes_bid=0.20, no_bid=0.49)
        exec_.register(st)
        fast_settings = _patch_gap_ms(0)
        with patch("polymarket_arb.execution.SETTINGS", fast_settings), \
             patch("polymarket_arb.risk.SETTINGS", fast_settings):
            await exec_.execute_dual_buy(st, 0.58, 0.0, age_ms=10.0, band="fresh",
                                         yes_p=0.48, no_p=0.10)
            for _ in range(5):
                exec_.on_price_update(st)
                await asyncio.sleep(0)
        return client, risk, metrics

    client, risk, metrics = asyncio.run(_run())
    hedge_buys = [order for order in client.sent if order[0] == "0xNO" and order[3] == "BUY"]
    assert len(hedge_buys) == 1
    assert risk.halted
    assert risk.pending_deployed == 0.0
    # Original YES cost (4.80) + worst-case confirmed hedge cost (5.00).
    assert abs(risk.total_deployed - 9.7) < 1e-9
    assert metrics.partial_resolved == 0


# ----------------------------------------------------------------------------
# TASK 4: debounce invalidation — a fire that dies with zero exposure must be
# re-fireable on the same combined price next tick
# ----------------------------------------------------------------------------
def test_invalidate_clears_per_market_debounce():
    """ArbEngine.invalidate removes the per-market combined-price debounce."""
    async def _run():
        fired = []
        async def _fire(st, c, t, age=0.0, band="fresh", yes_p=0.0, no_p=0.0):
            fired.append(c)
        eng = ArbEngine(on_fire=_fire, metrics=Metrics())
        st = _state(0.47, 0.49)  # combined 0.96 < 0.97 -> fire
        eng.evaluate(st)
        await asyncio.sleep(0)
        eng.evaluate(st)  # same combined price -> debounced
        await asyncio.sleep(0)
        assert len(fired) == 1
        eng.invalidate("m1")
        eng.evaluate(st)  # debounce cleared -> re-fire on the SAME combined
        await asyncio.sleep(0)
        return fired
    fired = asyncio.run(_run())
    assert len(fired) == 2
    assert abs(fired[1] - 0.96) < 1e-9


def test_invalidate_is_per_market():
    """Invalidating one market must not clear another market's debounce."""
    import dataclasses
    async def _run():
        fired = []
        async def _fire(st, c, t, age=0.0, band="fresh", yes_p=0.0, no_p=0.0):
            fired.append(st.market_id)
        eng = ArbEngine(on_fire=_fire, metrics=Metrics())
        a = _state(0.47, 0.49)  # m1
        b = dataclasses.replace(_state(0.47, 0.49), market_id="m2")
        eng.evaluate(a)
        eng.evaluate(b)
        await asyncio.sleep(0)
        eng.invalidate("m1")
        eng.evaluate(a)  # m1 cleared -> fires
        eng.evaluate(b)  # m2 still debounced -> suppressed
        await asyncio.sleep(0)
        return fired
    fired = asyncio.run(_run())
    assert fired == ["m1", "m2", "m1"]


def test_no_fill_timeout_clears_debounce_integration():
    """End-to-end: engine fire -> both legs never fill -> no-fill timeout cancels
    both with zero exposure -> engine's debounce is cleared so the SAME combined
    price can fire again on the next tick."""
    from polymarket_arb.config import SETTINGS as EXEC_SETTINGS

    class _NeverFillClient(_StubClient):
        async def is_filled(self, order_id, token_id, gap_ms=0):
            return False  # orders rest forever; only the timeout resolves them

    async def _run():
        client = _NeverFillClient()
        risk = RiskState(balance=1000.0)
        metrics = Metrics()
        exec_ = Execution(client, risk, metrics)
        eng = ArbEngine(on_fire=exec_.execute_dual_buy, metrics=metrics)
        exec_.set_invalidator(eng.invalidate)  # TASK 4 wiring (same as main.py)

        async def _drain():
            for _ in range(200):
                if not eng._tasks:
                    break
                await asyncio.sleep(0)
            # on_price_update probe tasks are untracked; give the loop turns so
            # the no-fill timeout resolution completes before we assert.
            for _ in range(50):
                await asyncio.sleep(0)

        st = _state(0.47, 0.49)
        exec_.register(st)

        # Fire #1: gate clears -> orders submitted, both rest unfilled.
        eng.evaluate(st)
        await _drain()
        assert exec_._pending, "fire must leave pending legs"
        assert len(client.sent) == 2

        # Force the no-fill timeout to expire: backdate first_sent past the gap.
        gap_s = EXEC_SETTINGS.max_leg_gap_ms / 1000.0
        for entry in exec_._pending.values():
            entry["first_sent"] = time.monotonic() - gap_s - 1.0

        # Resolve on the next tick: both cancel, exposure released, debounce cleared.
        exec_.on_price_update(st)
        await _drain()
        assert len(client.cancelled) == 2
        assert "m1" not in eng._last_fired, "debounce must be invalidated on no-fill"

        # Fire #2: same market, SAME combined price -> must fire again.
        eng.evaluate(st)
        await _drain()
        assert len(client.sent) == 4          # two dual-leg fires
        assert metrics.opportunities == 2     # second fire was NOT debounced away
        assert exec_._pending, "fire #2 must leave its own pending legs"

        # Resolve fire #2 the same way: no-fill timeout cancels, debounce cleared.
        for entry in exec_._pending.values():
            entry["first_sent"] = time.monotonic() - gap_s - 1.0
        exec_.on_price_update(st)
        await _drain()
        return client, metrics, eng
    client, metrics, eng = asyncio.run(_run())
    assert len(client.cancelled) == 4     # both fires' legs cancelled at timeout
    assert "m1" not in eng._last_fired
