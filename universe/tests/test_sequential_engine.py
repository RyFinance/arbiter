from __future__ import annotations

import asyncio
import time
from dataclasses import replace

import pytest

from polyuni.config import SETTINGS
from polyuni.fees import taker_fee
from polyuni.metrics import Metrics
from polyuni.models import MarketState
from polyuni.regime_signal import RegimeSignal
from polyuni.sequential_engine import SequentialPairEngine


class _Pnl:
    def __init__(self, metrics):
        self.metrics = metrics
        self.pairs = []

    def record_sequential_pair(self, st, **details):
        self.pairs.append((st.market_id, details))
        self.metrics.sequential_pairs += 1


class _Risk:
    def __init__(self):
        self.halts = []

    def halt(self, reason):
        self.halts.append(reason)


class _Execution:
    def __init__(self, metrics):
        self.pnl_ledger = _Pnl(metrics)
        self.risk = _Risk()
        self.buys = []
        self.exits = []

    async def execute_sequential_buy(
        self, st, *, side, limit_price, size, fee_rate, fee_exponent,
        sequence_id, stage,
    ):
        self.buys.append({"side": side, "stage": stage, "price": limit_price})
        fee = taker_fee(size, limit_price, fee_rate, fee_exponent)
        return {
            "ok": True,
            "side": side,
            "token_id": st.yes_token_id if side == "YES" else st.no_token_id,
            "order_id": f"{stage}-{side}",
            "price": limit_price,
            "fee": fee,
            "cost": limit_price * size + fee,
            "size": size,
            "mode": "paper",
        }

    async def execute_sequential_exit(self, st, *, open_leg, **kwargs):
        self.exits.append((st.market_id, kwargs["reason"]))
        return {"ok": True, "price": 0.45, "fee": 0.1, "pnl": -0.4}


class _RegimeReader:
    def __init__(self, signal):
        self.signal = signal

    def read(self, now=None):
        return self.signal


def _settings(**overrides):
    base = dict(
        strategy_mode="sequential",
        order_size=10.0,
        max_quote_age_ms=5_000,
        no_entry_seconds=1.0,
        sequential_entry_window_s=120.0,
        sequential_volatility_lookback_s=20.0,
        sequential_min_local_move=0.015,
        sequential_min_samples=3,
        sequential_near_even_band=0.08,
        sequential_first_leg_max_price=0.48,
        sequential_second_leg_max_price=0.48,
        sequential_second_leg_timeout_s=1.0,
        sequential_min_net_edge=0.0025,
        sequential_cooldown_s=30.0,
    )
    base.update(overrides)
    return replace(SETTINGS, **base)


def _market(*, btc=True):
    now = time.time()
    return MarketState(
        market_id="m1",
        question=("Bitcoin Up or Down - test" if btc else "Will it rain?"),
        yes_token_id="up",
        no_token_id="down",
        slug=("btc-updown-5m-1800000000" if btc else "rain-test"),
        outcome_yes=("Up" if btc else "Yes"),
        outcome_no=("Down" if btc else "No"),
        start_time=now - 1,
        end_time=now + 299,
        fees_enabled=True,
        fee_rate=0.07,
        fee_exponent=1.0,
    )


def _tick(engine, st, side, price):
    now = time.time()
    if side == "YES":
        st.best_yes_ask, st.yes_size, st.yes_ts = price, 100.0, now
        token = st.yes_token_id
    else:
        st.best_no_ask, st.no_size, st.no_ts = price, 100.0, now
        token = st.no_token_id
    st.last_update_ts = now
    engine.on_leg_update(st, token)


async def _settle():
    await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_buys_legs_at_different_times_and_only_completes_fee_positive_pair():
    metrics = Metrics()
    execution = _Execution(metrics)
    engine = SequentialPairEngine(
        execution,
        metrics,
        settings=_settings(),
        regime_reader=_RegimeReader(RegimeSignal(status="missing")),
    )
    st = _market()

    _tick(engine, st, "YES", 0.50)
    _tick(engine, st, "YES", 0.49)
    assert execution.buys == []
    _tick(engine, st, "YES", 0.48)
    await _settle()

    assert [(b["stage"], b["side"]) for b in execution.buys] == [("first", "YES")]
    assert "m1" in engine.open_positions

    _tick(engine, st, "NO", 0.48)
    await _settle()

    assert [(b["stage"], b["side"]) for b in execution.buys] == [
        ("first", "YES"), ("second", "NO")
    ]
    assert "m1" not in engine.open_positions
    assert len(execution.pnl_ledger.pairs) == 1
    assert engine.snapshot()["completed_pairs"] == 1
    for price in (0.50, 0.49, 0.48):
        _tick(engine, st, "YES", price)
    await _settle()
    assert len(execution.buys) == 2  # market window is consumed; no second sequence
    assert engine.snapshot()["phase_counts"]["consumed"] == 1
    await engine.close()


@pytest.mark.asyncio
async def test_consumed_market_ticks_are_ignored_without_rejections():
    metrics = Metrics()
    execution = _Execution(metrics)
    engine = SequentialPairEngine(
        execution,
        metrics,
        settings=_settings(),
        regime_reader=_RegimeReader(RegimeSignal(status="missing")),
    )
    st = _market()

    for price in (0.50, 0.49, 0.48):
        _tick(engine, st, "YES", price)
    await _settle()
    _tick(engine, st, "NO", 0.48)
    await _settle()

    before = engine.snapshot()["rejections"]
    for _ in range(100):
        _tick(engine, st, "YES", 0.47)
        _tick(engine, st, "NO", 0.47)

    snapshot = engine.snapshot()
    assert snapshot["phase_counts"]["consumed"] == 1
    assert snapshot["rejections"] == before
    assert "consumed" not in snapshot["rejections"]
    assert len(execution.buys) == 2
    await engine.close()


@pytest.mark.asyncio
async def test_preopen_quotes_cannot_satisfy_postopen_volatility_gate():
    metrics = Metrics()
    execution = _Execution(metrics)
    engine = SequentialPairEngine(
        execution, metrics, settings=_settings(),
        regime_reader=_RegimeReader(RegimeSignal(status="missing")),
    )
    st = _market()
    st.start_time = time.time() + 60
    for price in (0.50, 0.49, 0.48):
        _tick(engine, st, "YES", price)
    await _settle()

    assert execution.buys == []
    assert engine.snapshot()["rejections"]["market_not_started"] == 3

    st.start_time = time.time() - 1
    for price in (0.50, 0.49, 0.48):
        _tick(engine, st, "YES", price)
    await _settle()
    assert len(execution.buys) == 1
    await engine.close()


@pytest.mark.asyncio
async def test_second_leg_rejects_price_above_fee_aware_ceiling():
    metrics = Metrics()
    execution = _Execution(metrics)
    engine = SequentialPairEngine(
        execution, metrics, settings=_settings(),
        regime_reader=_RegimeReader(RegimeSignal(status="missing")),
    )
    st = _market()
    for price in (0.50, 0.49, 0.48):
        _tick(engine, st, "YES", price)
    await _settle()

    _tick(engine, st, "NO", 0.49)
    await _settle()

    assert len(execution.buys) == 1
    assert engine.snapshot()["rejections"]["second_price_too_high"] == 1
    await engine.close()


@pytest.mark.asyncio
async def test_wall_clock_timeout_exits_without_waiting_for_another_tick():
    metrics = Metrics()
    execution = _Execution(metrics)
    engine = SequentialPairEngine(
        execution,
        metrics,
        settings=_settings(sequential_second_leg_timeout_s=0.02),
        regime_reader=_RegimeReader(RegimeSignal(status="missing")),
    )
    st = _market()
    for price in (0.50, 0.49, 0.48):
        _tick(engine, st, "YES", price)
    await asyncio.sleep(0.06)

    assert execution.exits == [("m1", "second_leg_timeout")]
    assert "m1" not in engine.open_positions
    assert engine.snapshot()["timeouts"] == 1
    for price in (0.50, 0.49, 0.48):
        _tick(engine, st, "YES", price)
    await _settle()
    assert len(execution.buys) == 1
    assert engine.snapshot()["phase_counts"]["consumed"] == 1
    await engine.close()


def test_lightgbm_only_adjusts_btc_first_leg_threshold_when_fresh():
    metrics = Metrics()
    execution = _Execution(metrics)
    high = RegimeSignal(
        regime="HIGH", confidence=0.8, source="lightgbm", status="fresh"
    )
    engine = SequentialPairEngine(
        execution, metrics, settings=_settings(), regime_reader=_RegimeReader(high)
    )
    required_btc, signal = engine._required_move(_market(btc=True), time.time())
    required_other, other_signal = engine._required_move(_market(btc=False), time.time())

    assert signal.usable is True
    assert required_btc == pytest.approx(0.015 * SETTINGS.lightgbm_high_vol_multiplier)
    assert required_other == pytest.approx(0.015)
    assert other_signal.status == "not_applicable"


def test_startup_manual_review_exposure_is_visible_in_strategy_snapshot():
    metrics = Metrics()
    engine = SequentialPairEngine(
        _Execution(metrics), metrics, settings=_settings(),
        regime_reader=_RegimeReader(RegimeSignal(status="missing")),
    )
    engine.restore_manual_reviews([{
        "sequence_id": "seq-1", "market_id": "m1", "side": "YES", "cost": 4.9,
    }])
    snapshot = engine.snapshot()
    assert snapshot["open_exposure_usd"] == 4.9
    assert snapshot["phase_counts"]["manual_review"] == 1
    assert snapshot["manual_review_sequences"][0]["sequence_id"] == "seq-1"
