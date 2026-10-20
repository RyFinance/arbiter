"""Independent regression checks for execution reservation/cancellation fixes."""

import asyncio
from datetime import datetime

from polymarket_arb.clients.trading_client import TradingClient
from polymarket_arb.execution import Execution
from polymarket_arb.metrics import Metrics
from polymarket_arb.models import MarketState
from polymarket_arb.risk import RiskState


def _market(yes_ask, no_ask):
    now = datetime.now().timestamp()
    return MarketState(
        market_id="verification-market",
        question="Verification market",
        yes_token_id="YES-token",
        no_token_id="NO-token",
        best_yes_ask=yes_ask,
        best_no_ask=no_ask,
        yes_size=1_000.0,
        no_size=1_000.0,
        last_update_ts=now,
        yes_ts=now,
        no_ts=now,
        window_close=0.0,
    )


class _FilledHedgeClient(TradingClient):
    def __init__(self):
        self.submits = []
        self.cancels = []

    async def submit_order(self, token_id, price, size, side="BUY"):
        self.submits.append((token_id, price, size, side))
        return f"{side}-{token_id}"

    async def is_filled(self, order_id, token_id, gap_ms=0):
        return True

    async def cancel_order(self, token_id, missing_order_id=None):
        self.cancels.append((token_id, missing_order_id))
        return True

    def get_fill_ratio(self, order_id):
        return 1.0


def test_partial_hedge_releases_only_its_committed_cost_preserving_baseline():
    """Reserve -> ratio-scaled commit -> hedge -> release must retain prior exposure."""
    async def run():
        client = _FilledHedgeClient()
        risk = RiskState(balance=1_000.0)
        metrics = Metrics()
        execution = Execution(client, risk, metrics)
        state = _market(0.37, 0.41)  # live hedge price differs from entry price
        execution.register(state)

        # Existing market exposure must survive this unrelated partial's release.
        risk.commit(state.market_id, 2.0)
        baseline = risk.total_deployed

        # Entry was YES @ 0.48, NO @ 0.50 for size 10; YES filled at 50%.
        reserved = 9.8
        assert await risk.reserve(state, 4.8, 5.0) is None
        assert risk.pending_deployed == reserved
        risk.commit(state.market_id, 2.4, from_reservation=reserved)
        assert risk.total_deployed == baseline + 2.4
        assert risk.pending_deployed == 0.0

        await execution._manage_partial(
            state, execution._templates[state.market_id], "entry-yes", None,
            True, False, 10.0, 0.96, 0.47, 0.49, 0.0, "fresh", 0.0,
            reserved=2.4, yes_ratio=0.5, no_ratio=0.0,
        )
        return client, risk, metrics, baseline

    client, risk, metrics, baseline = asyncio.run(run())
    assert client.cancels == [("NO-token", None)]
    assert client.submits == [("NO-token", 0.41, 5.0, "BUY")]
    assert metrics.partial_resolved == 1
    assert risk.pending_deployed == 0.0
    assert abs(risk.total_deployed - baseline) < 1e-9
    assert baseline == 2.0
    assert abs(risk.deployed_per_market["verification-market"] - 2.0) < 1e-9


class _YesSucceedsNoRaisesClient(TradingClient):
    def __init__(self):
        self.cancels = []

    async def submit_order(self, token_id, price, size, side="BUY"):
        if token_id == "NO-token":
            raise RuntimeError("NO transport failure")
        return "yes-live-order"

    async def cancel_order(self, token_id, missing_order_id=None):
        self.cancels.append((token_id, missing_order_id))
        return True


def test_no_submit_raise_cancels_successful_yes_and_unreserves_once():
    async def run():
        client = _YesSucceedsNoRaisesClient()
        risk = RiskState(balance=1_000.0)
        metrics = Metrics()
        execution = Execution(client, risk, metrics)
        state = _market(0.47, 0.49)
        execution.register(state)

        unreserve_calls = []
        original_unreserve = risk.unreserve

        def counted_unreserve(market_id, cost):
            unreserve_calls.append((market_id, cost))
            original_unreserve(market_id, cost)

        risk.unreserve = counted_unreserve
        await execution.execute_dual_buy(state, 0.96, 0.0, age_ms=0.0, band="fresh")
        return client, risk, metrics, unreserve_calls

    client, risk, metrics, unreserve_calls = asyncio.run(run())
    assert client.cancels == [("YES-token", "yes-live-order")]
    assert unreserve_calls == [("verification-market", 9.6)]
    assert risk.pending_deployed == 0.0
    assert risk.pending_per_market == {}
    assert risk.total_deployed == 0.0
    assert metrics.missed == 1


class _YesSucceedsNoRaisesCancelUnconfirmedClient(_YesSucceedsNoRaisesClient):
    async def cancel_order(self, token_id, missing_order_id=None):
        self.cancels.append((token_id, missing_order_id))
        return False

    async def is_filled(self, order_id, token_id, gap_ms=0):
        return False


def test_submit_sibling_failure_keeps_reservation_when_cancel_unconfirmed():
    async def run():
        client = _YesSucceedsNoRaisesCancelUnconfirmedClient()
        risk = RiskState(balance=1_000.0)
        metrics = Metrics()
        execution = Execution(client, risk, metrics)
        state = _market(0.47, 0.49)
        execution.register(state)

        await execution.execute_dual_buy(state, 0.96, 0.0, age_ms=0.0, band="fresh")
        return client, risk, metrics

    client, risk, metrics = asyncio.run(run())
    assert client.cancels == [("YES-token", "yes-live-order")]
    assert risk.halted is True
    assert abs(risk.pending_deployed - 9.6) < 1e-9
    assert abs(risk.pending_per_market["verification-market"] - 9.6) < 1e-9
    assert risk.total_deployed == 0.0
    assert metrics.missed == 1
