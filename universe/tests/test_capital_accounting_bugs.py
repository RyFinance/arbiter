"""Regression tests for capital-accounting bugs, ported from polymarket_arb's
tests/test_capital_accounting_bugs.py.

TASK 1: Capital over-commitment on partial fills -- capital leak in the
        hedge-completion path (must call risk.release() after a successful hedge).
TASK 2: Reservation leak on one-leg submit failure -- emergency-cancel path.

Adaptation note: execution.py was split into order_router.py / fill_tracker.py /
partial_fill_recovery.py / pnl_ledger.py / execution.py, each with its own
`from ..config import SETTINGS` binding -- patching only
polyuni.execution.execution.SETTINGS would NOT affect the other four modules'
independent bindings of the same name. _patched_settings() below patches every
submodule that actually reads a gap/cap-sensitive field in these tests, so the
patch behaves like patching one shared SETTINGS did in the pre-split file.
"""
from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import time
from unittest.mock import patch

import pytest

from polyuni.config import SETTINGS
from polyuni.execution import Execution
from polyuni.metrics import Metrics
from polyuni.models import MarketState
from polyuni.risk import RiskState

_SETTINGS_IMPORT_SITES = [
    "polyuni.execution.execution.SETTINGS",
    "polyuni.execution.order_router.SETTINGS",
    "polyuni.execution.fill_tracker.SETTINGS",
    "polyuni.execution.partial_fill_recovery.SETTINGS",
    "polyuni.execution.pnl_ledger.SETTINGS",
    "polyuni.risk.SETTINGS",
]


@contextlib.contextmanager
def _patched_settings(tuned):
    with contextlib.ExitStack() as stack:
        for target in _SETTINGS_IMPORT_SITES:
            stack.enter_context(patch(target, tuned))
        yield


def _market() -> MarketState:
    now = time.time()
    return MarketState(
        market_id="cap-bug", question="cap bug test",
        yes_token_id="yes-token", no_token_id="no-token",
        best_yes_ask=0.41, best_no_ask=0.53,
        best_yes_bid=0.40, best_no_bid=0.52,
        yes_size=200.0, no_size=200.0,
        last_update_ts=now, yes_ts=now, no_ts=now,
    )


class _YesFillsNoRejects:
    """YES leg submits & fills (ratio=1.0), NO leg submitted but returns None
    (submit-failure). On hedge, the missing NO leg is successfully submitted and
    fills (ratio=1.0)."""

    def __init__(self):
        self.submit_count = 0
        self.positions: dict[str, float] = {}

    async def submit_order(self, token_id, price, size, side="BUY"):
        self.submit_count += 1
        if self.submit_count == 1:
            self.positions[token_id] = self.positions.get(token_id, 0.0) + size
            return "yes-order-1"
        if self.submit_count == 2:
            return None  # NO leg rejected at submit time
        self.positions[token_id] = self.positions.get(token_id, 0.0) + size
        return f"hedge-order-{self.submit_count}"

    async def is_filled(self, order_id, token_id, gap_ms=0):
        return order_id is not None

    def get_fill_ratio(self, order_id):
        return 1.0 if order_id is not None else 0.0

    async def cancel_order(self, token_id, missing_order_id=None):
        return True


def test_capital_released_after_successful_partial_hedge():
    """After a partial fill is completed by a successful hedge, the original
    committed capital must be released from total_deployed -- not stay locked
    forever, starving the bot of usable capital over time."""

    async def run():
        tuned = dataclasses.replace(
            SETTINGS,
            max_leg_gap_ms=0,  # gap window expires immediately
            max_position_per_market=1_000_000.0,
            max_total_capital=1_000_000.0,
            partial_hedge_min_edge=0.0,  # allow hedging at any edge
        )

        risk = RiskState()
        st = _market()
        client = _YesFillsNoRejects()

        with _patched_settings(tuned):
            execution = Execution(client, risk, Metrics())
            execution.register(st, size=20.0)
            await execution.execute_dual_buy(
                st, combined=0.94, decision_ts=0.0, yes_p=0.41, no_p=0.53,
            )
            # Fire committed: yes_p * 20 * 1.0 + 0.53 * 20 * 0.0 = 8.2
            assert risk.total_deployed == pytest.approx(8.2), \
                f"Expected 8.2 deployed after fire, got {risk.total_deployed}"

            # Resolve fills -- YES filled, NO missing -> partial -> hedge
            await execution.fill_tracker.check_fills()

        return risk

    risk = asyncio.run(run())

    assert risk.total_deployed == pytest.approx(0.0), \
        f"Capital leak after successful hedge: total_deployed={risk.total_deployed} " \
        f"(expected 0.0 after release)."


def test_neither_filled_releases_committed_capital():
    """When neither leg fills within the gap window, the committed capital must
    be fully released back to available."""

    async def run():
        tuned = dataclasses.replace(
            SETTINGS, max_leg_gap_ms=0,
            max_position_per_market=1_000_000.0, max_total_capital=1_000_000.0,
        )

        class _RatioClient:
            def __init__(self):
                self._ratios = {}

            async def submit_order(self, token_id, price, size, side="BUY"):
                oid = f"order-{token_id}"
                self._ratios[oid] = 0.37
                return oid

            async def is_filled(self, order_id, token_id, gap_ms=0):
                return False  # neither fills

            def get_fill_ratio(self, order_id):
                return self._ratios.get(order_id, 0.37)

            async def cancel_order(self, token_id, missing_order_id=None):
                return True

        risk = RiskState()
        st = _market()
        client = _RatioClient()

        with _patched_settings(tuned):
            execution = Execution(client, risk, Metrics())
            execution.register(st, size=10.0)
            await execution.execute_dual_buy(
                st, combined=0.94, decision_ts=0.0, yes_p=0.41, no_p=0.53,
            )
            # committed = 0.41*10*0.37 + 0.53*10*0.37 = 1.517 + 1.961 = 3.478
            assert risk.total_deployed == pytest.approx(3.478), \
                f"Expected 3.478 deployed after fire, got {risk.total_deployed}"

            await execution.fill_tracker.check_fills()

        assert risk.total_deployed == pytest.approx(0.0), \
            f"Capital not released after no-fill: total_deployed={risk.total_deployed}"
        assert risk.pending_deployed == pytest.approx(0.0), \
            f"Reservation not cleared: pending_deployed={risk.pending_deployed}"


def test_no_fill_timeout_does_not_release_when_cancel_leaves_orders_live():
    """A failed cancellation must leave capital locked for manual review. A
    negative is_filled response after the failed cancel is not proof the order
    is gone -- it may still be resting. Releasing its capital would let a later
    fill escape the risk limits."""

    async def run():
        tuned = dataclasses.replace(
            SETTINGS, max_leg_gap_ms=0,
            max_position_per_market=1_000_000.0, max_total_capital=1_000_000.0,
        )

        class _CancelFailsWhileLiveClient:
            def __init__(self):
                self._ratios = {}
                self.cancel_calls = 0
                self.fill_checks = 0

            async def submit_order(self, token_id, price, size, side="BUY"):
                oid = f"order-{token_id}"
                self._ratios[oid] = 0.37
                return oid

            async def is_filled(self, order_id, token_id, gap_ms=0):
                self.fill_checks += 1
                return False

            def get_fill_ratio(self, order_id):
                return self._ratios.get(order_id, 0.37)

            async def cancel_order(self, token_id, missing_order_id=None):
                self.cancel_calls += 1
                return False

        risk = RiskState()
        st = _market()
        client = _CancelFailsWhileLiveClient()
        with _patched_settings(tuned):
            execution = Execution(client, risk, Metrics())
            execution.register(st, size=10.0)
            await execution.execute_dual_buy(
                st, combined=0.94, decision_ts=0.0, yes_p=0.41, no_p=0.53,
            )
            committed = risk.total_deployed
            await execution.fill_tracker.check_fills()

        assert risk.total_deployed == pytest.approx(committed)
        assert risk.halted
        assert client.cancel_calls == 2
        # Two normal probes plus exactly one post-cancel check per failed cancel.
        assert client.fill_checks == 4

    asyncio.run(run())


# ---- TASK 2: Reservation leak on one-leg submit failure ----

class _OneSuccessOneNoneClient:
    """YES leg submits and succeeds; NO leg submits and raises an exception --
    the asyncio.gather failure path where one leg's submit_order raises before
    either result is captured."""

    def __init__(self):
        self.cancelled_orders: list[str] = []

    async def submit_order(self, token_id, price, size, side="BUY"):
        if token_id.endswith("-Y"):
            return "accepted-yes"
        raise RuntimeError("NO submission transport failure")

    async def is_filled(self, order_id, token_id, gap_ms=0):
        return True

    def get_fill_ratio(self, order_id):
        return 1.0 if order_id is not None else 0.0

    async def cancel_order(self, token_id, missing_order_id=None):
        if missing_order_id:
            self.cancelled_orders.append(missing_order_id)
        return True


def test_one_leg_submit_failure_unreserves_and_cancels_successful_leg():
    """When one leg's submit_order succeeds and the other raises, the
    emergency-cancel path must (1) unreserve the full reservation back to the
    pending baseline and (2) cancel the successful (now orphaned) leg."""

    async def run():
        tuned = dataclasses.replace(
            SETTINGS, max_position_per_market=1_000_000.0, max_total_capital=1_000_000.0,
        )

        risk = RiskState()
        baseline = MarketState(
            market_id="baseline", question="base",
            yes_token_id="base-Y", no_token_id="base-N",
            best_yes_ask=0.41, best_no_ask=0.53,
            yes_size=100.0, no_size=100.0,
            last_update_ts=time.time(), yes_ts=time.time(), no_ts=time.time(),
        )
        assert await risk.reserve(baseline, 2.0, 3.0) is None  # pending baseline = 5.0

        st = MarketState(
            market_id="target", question="target",
            yes_token_id="target-Y", no_token_id="target-N",
            best_yes_ask=0.41, best_no_ask=0.53,
            yes_size=100.0, no_size=100.0,
            last_update_ts=time.time(), yes_ts=time.time(), no_ts=time.time(),
        )
        client = _OneSuccessOneNoneClient()

        with _patched_settings(tuned):
            execution = Execution(client, risk, Metrics())
            execution.register(st, size=10.0)
            await execution.execute_dual_buy(
                st, combined=0.94, decision_ts=0.0, yes_p=0.41, no_p=0.53,
            )
        return risk, client

    risk, client = asyncio.run(run())

    assert risk.pending_deployed == pytest.approx(5.0), \
        f"Reservation leaked: pending_deployed={risk.pending_deployed} (expected 5.0)"
    assert risk.pending_per_market == {"baseline": 5.0}
    assert risk.total_deployed == pytest.approx(0.0)
    assert "target" not in risk.deployed_per_market
    assert len(client.cancelled_orders) == 1, \
        f"Expected 1 emergency cancel, got {len(client.cancelled_orders)}"
