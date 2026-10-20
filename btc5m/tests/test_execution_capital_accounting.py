"""Independent capital-lifecycle regression tests for dual-leg execution."""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

from polymarket_arb.clients.trading_client import TradingClient
import polymarket_arb.execution as execution_module
from polymarket_arb.execution import Execution
from polymarket_arb.logger import BotLogger
from polymarket_arb.metrics import Metrics
from polymarket_arb.models import MarketState
from polymarket_arb.risk import RiskState

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
from report_paper_sessions import session_report


def _market(market_id: str) -> MarketState:
    now = datetime.now().timestamp()
    return MarketState(
        market_id=market_id,
        question="capital accounting",
        yes_token_id=f"{market_id}-YES",
        no_token_id=f"{market_id}-NO",
        best_yes_ask=0.41,
        best_no_ask=0.53,
        yes_size=100.0,
        no_size=100.0,
        last_update_ts=now,
        yes_ts=now,
        no_ts=now,
    )


class _RatioClient(TradingClient):
    """Deterministic submitter with an optional rejected NO leg."""

    def __init__(self, *, reject_no: bool = False):
        self.reject_no = reject_no

    async def submit_order(self, token_id, price, size, side="BUY"):
        if self.reject_no and token_id.endswith("-NO"):
            return None
        return f"order-{token_id}"

    def get_fill_ratio(self, order_id):
        return 0.37 if order_id is not None else 0.0


class _OneSuccessOneRaiseClient(TradingClient):
    """YES is accepted; NO fails and the accepted sibling cancels cleanly."""

    def __init__(self):
        self.yes_submitted = False

    async def submit_order(self, token_id, price, size, side="BUY"):
        if token_id.endswith("-YES"):
            self.yes_submitted = True
            return "accepted-yes"
        raise RuntimeError("NO submission transport failure")

    async def cancel_order(self, token_id, missing_order_id=None):
        return True


@pytest.mark.parametrize(
    ("reject_no", "expected_deployed"),
    [
        (False, 4.5214),  # (0.41 + 0.53) * 13 * 0.37
        (True, 1.9721),   # 0.41 * 13 * 0.37; rejected NO commits nothing
    ],
)
def test_ratio_scaled_commit_clears_only_its_own_reservation(
    reject_no: bool, expected_deployed: float,
):
    """Reserve -> ratio-scaled commit -> full unreserve leaves no pending leak."""

    async def run():
        risk = RiskState(balance=1_000.0)
        # An unrelated in-flight fire establishes a non-zero pending baseline.
        baseline = _market("baseline")
        assert await risk.reserve(baseline, 2.0, 3.0) is None
        assert risk.pending_deployed == 5.0

        st = _market("target")
        execution = Execution(_RatioClient(reject_no=reject_no), risk, Metrics())
        execution.register(st, size=13.0)
        await execution.execute_dual_buy(
            st, combined=0.94, decision_ts=0.0, yes_p=0.41, no_p=0.53,
        )
        committed_pending = risk.pending_deployed
        committed_total = risk.total_deployed
        committed_market = risk.deployed_per_market["target"]
        # Model the later resolution/settlement that releases the amount actually
        # committed, rather than the original full-size reservation.
        risk.release("target", committed_market)
        return risk, committed_pending, committed_total, committed_market

    risk, committed_pending, committed_total, committed_market = asyncio.run(run())

    # Target lifecycle: reserve 12.22 -> commit expected_deployed -> unreserve
    # 12.22 -> release expected_deployed. The independent 5.00 reservation remains.
    assert committed_pending == pytest.approx(5.0)
    assert committed_total == pytest.approx(expected_deployed)
    assert committed_market == pytest.approx(expected_deployed)
    assert risk.pending_deployed == pytest.approx(5.0)
    assert risk.pending_per_market == {"baseline": 5.0}
    assert risk.total_deployed == pytest.approx(0.0)
    assert "target" not in risk.deployed_per_market


def test_one_successful_submit_then_exception_restores_pending_baseline():
    """A gather failure after YES succeeds must unreserve the entire target cost."""

    async def run():
        risk = RiskState(balance=1_000.0)
        baseline = _market("baseline")
        assert await risk.reserve(baseline, 2.0, 3.0) is None

        st = _market("target")
        client = _OneSuccessOneRaiseClient()
        execution = Execution(client, risk, Metrics())
        execution.register(st, size=13.0)
        await execution.execute_dual_buy(
            st, combined=0.94, decision_ts=0.0, yes_p=0.41, no_p=0.53,
        )
        return risk, client

    risk, client = asyncio.run(run())

    assert client.yes_submitted, "YES must succeed before the NO submit raises"
    # Target reserve was 12.22. Its failed pre-commit fire must leave neither a
    # target reservation nor deployed capital; the unrelated 5.00 stays intact.
    assert risk.pending_deployed == pytest.approx(5.0)
    assert risk.pending_per_market == {"baseline": 5.0}
    assert risk.total_deployed == pytest.approx(0.0)
    assert "target" not in risk.deployed_per_market


def test_released_market_template_is_loud_counted_and_reported(tmp_path, capsys):
    """A fire after release must be an auditable paper rejection, not a no-op."""
    log_path = tmp_path / "bot.jsonl"
    original_log = execution_module.log
    execution_module.log = BotLogger(str(log_path))

    async def run():
        st = _market("released")
        metrics = Metrics()
        execution = Execution(_RatioClient(), RiskState(balance=1_000.0), metrics)
        execution.register(st)
        execution.release_window(st.market_id)
        await execution.execute_dual_buy(
            st, combined=0.94, decision_ts=0.0, age_ms=12.0,
            yes_p=0.41, no_p=0.53,
        )
        return metrics

    try:
        metrics = asyncio.run(run())
    finally:
        execution_module.log.close()
        execution_module.log = original_log
    assert metrics.missed == 1
    assert metrics.template_unavailable == 1
    assert "template_unavailable=1" in metrics.summary()
    assert "no order template for released" in capsys.readouterr().out

    report = session_report("synthetic", log_path)
    assert report["miss_reasons"] == {"template_unavailable": 1}
