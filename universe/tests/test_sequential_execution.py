from __future__ import annotations

import time

import pytest

from polyuni.execution import Execution
from polyuni.fees import taker_fee
from polyuni.metrics import Metrics
from polyuni.models import MarketState
from polyuni.risk import RiskState


class _PaperClient:
    mode = "paper"

    def __init__(self):
        self.reports = {}
        self.count = 0

    async def submit_order(self, token_id, price, size, side="BUY"):
        self.count += 1
        order_id = f"paper-{self.count}"
        if side == "SELL":
            actual = 0.44
        elif token_id == "yes":
            actual = 0.47
        else:
            actual = 0.46
        self.reports[order_id] = {
            "fill_price": actual,
            "fill_source": "rest_book",
        }
        return order_id

    async def is_filled(self, order_id, token_id, gap_ms=0):
        return True

    def get_fill_ratio(self, order_id):
        return 1.0

    def get_fill_report(self, order_id):
        return self.reports.get(order_id, {})

    async def cancel_order(self, token_id, missing_order_id=None):
        return True


def _market():
    now = time.time()
    return MarketState(
        "m1",
        "Bitcoin Up or Down",
        "yes",
        "no",
        slug="btc-updown-5m-1800000000",
        outcome_yes="Up",
        outcome_no="Down",
        best_yes_ask=0.48,
        best_no_ask=0.48,
        yes_size=100,
        no_size=100,
        best_yes_bid=0.44,
        best_no_bid=0.44,
        last_update_ts=now,
        yes_ts=now,
        no_ts=now,
        start_time=now - 1,
        end_time=now + 299,
        fees_enabled=True,
        fee_rate=0.07,
    )


@pytest.mark.asyncio
async def test_sequential_execution_commits_actual_fill_costs_and_releases_pair_capital():
    metrics = Metrics()
    risk = RiskState()
    execution = Execution(_PaperClient(), risk, metrics)
    st = _market()
    execution.register(st, size=10)

    first = await execution.execute_sequential_buy(
        st, side="YES", limit_price=0.48, size=10, fee_rate=0.07,
        fee_exponent=1, sequence_id="seq", stage="first",
    )
    second = await execution.execute_sequential_buy(
        st, side="NO", limit_price=0.48, size=10, fee_rate=0.07,
        fee_exponent=1, sequence_id="seq", stage="second",
    )

    assert first["price"] == 0.47
    assert second["price"] == 0.46
    expected = (
        4.7 + taker_fee(10, 0.47, 0.07)
        + 4.6 + taker_fee(10, 0.46, 0.07)
    )
    assert risk.total_deployed == pytest.approx(expected)

    execution.pnl_ledger.record_sequential_pair(
        st,
        mode="paper",
        first_side="YES",
        first_price=first["price"],
        second_price=second["price"],
        first_fee=first["fee"],
        second_fee=second["fee"],
        size=10,
        first_order_id=first["order_id"],
        second_order_id=second["order_id"],
        leg_gap_s=1.0,
    )
    assert metrics.session_pnl > 0

    execution.release_market(st.market_id)
    assert risk.total_deployed == 0.0


@pytest.mark.asyncio
async def test_sequential_protective_exit_books_fees_and_releases_first_leg():
    metrics = Metrics()
    risk = RiskState()
    execution = Execution(_PaperClient(), risk, metrics)
    st = _market()
    execution.register(st, size=10)
    first = await execution.execute_sequential_buy(
        st, side="YES", limit_price=0.48, size=10, fee_rate=0.07,
        fee_exponent=1, sequence_id="seq", stage="first",
    )
    first.update({"sequence_id": "seq", "opened_at": time.time() - 2})

    result = await execution.execute_sequential_exit(
        st, open_leg=first, fee_rate=0.07, fee_exponent=1,
        reason="second_leg_timeout",
    )

    assert result["ok"] is True
    assert result["price"] == 0.44
    assert result["pnl"] < 0
    assert metrics.sequential_exits == 1
    assert risk.total_deployed == 0.0
