"""Depth-snapshot fill replay tests; no live client or transaction path."""
from __future__ import annotations

import asyncio

import pytest

from polymarket_arb.backtest_engine import (
    BacktestConfig,
    BacktestEngine,
    DepthSnapshotUnavailable,
    _BacktestTradingClient,
    cumulative_sell_fill,
    depth_snapshot_availability,
)
from polymarket_arb.models import MarketState


def _state() -> MarketState:
    return MarketState(
        market_id="market",
        question="test",
        yes_token_id="yes-token",
        no_token_id="no-token",
        best_yes_ask=0.40,
        best_no_ask=0.60,
        yes_size=3.0,
        no_size=3.0,
    )


def test_depth_snapshot_replay_consumes_full_cumulative_ladder():
    async def scenario():
        client = _BacktestTradingClient(BacktestConfig(fill_model="depth_snapshot", replay_speed=1000))
        client.set_market_state(_state(), {
            "yes_asks": [{"price": 0.40, "size": 3}, {"price": 0.41, "size": 7}],
            "no_asks": [],
        })
        order_id = await client.submit_order("yes-token", price=0.41, size=10)
        assert order_id is not None
        assert client.get_fill_ratio(order_id) == pytest.approx(1.0)
        assert client.get_fill_vwap(order_id) == pytest.approx(0.407)

    asyncio.run(scenario())


def test_depth_snapshot_replay_reports_thin_liquidity_and_respects_limit():
    async def scenario():
        client = _BacktestTradingClient(BacktestConfig(fill_model="depth_snapshot", replay_speed=1000))
        client.set_market_state(_state(), {
            "yes_asks": [[0.40, 2], [0.41, 3], [0.42, 100]],
            "no_asks": [],
        })
        order_id = await client.submit_order("yes-token", price=0.41, size=10)
        assert order_id is not None
        assert client.get_fill_ratio(order_id) == pytest.approx(0.5)
        assert client.get_fill_vwap(order_id) == pytest.approx(0.406)
        assert await client.submit_order("yes-token", price=0.39, size=10) is None

    asyncio.run(scenario())


def test_depth_snapshot_sell_consumes_retained_bids_not_asks():
    filled, vwap = cumulative_sell_fill(
        [(0.39, 2.0), (0.38, 3.0), (0.37, 100.0)],
        limit_price=0.38,
        requested_size=10.0,
    )
    assert filled == pytest.approx(5.0)
    assert vwap == pytest.approx(0.384)

    async def scenario():
        client = _BacktestTradingClient(
            BacktestConfig(fill_model="depth_snapshot", replay_speed=1000)
        )
        client.set_market_state(_state(), {
            "yes_asks": [[0.40, 100.0]],
            "no_asks": [[0.60, 100.0]],
            "yes_bids": [[0.39, 2.0], [0.38, 3.0]],
            "no_bids": [[0.59, 100.0]],
        })
        order_id = await client.submit_order(
            "yes-token", price=0.38, size=10.0, side="SELL"
        )
        assert order_id is not None
        assert client.get_fill_ratio(order_id) == pytest.approx(0.5)
        assert client.get_fill_vwap(order_id) == pytest.approx(0.384)

    asyncio.run(scenario())


def test_top_of_book_legacy_model_remains_available_for_comparison():
    async def scenario():
        client = _BacktestTradingClient(BacktestConfig(fill_model="top_of_book", replay_speed=1000))
        client.set_market_state(_state())
        order_id = await client.submit_order("yes-token", price=0.40, size=10)
        assert order_id is not None
        assert client.get_fill_ratio(order_id) == pytest.approx(0.3)
        assert client.get_fill_vwap(order_id) is None

    asyncio.run(scenario())


def test_depth_snapshot_mode_blocks_missing_ladders_without_fabricating_depth():
    ticks = [{
        "timestamp": 1.0,
        "yes_ask": 0.40,
        "no_ask": 0.58,
        "yes_bid": 0.39,
        "no_bid": 0.57,
        "yes_size": 100.0,
        "no_size": 100.0,
    }]
    assert depth_snapshot_availability(ticks) == {
        "total_ticks": 1,
        "complete_ticks": 0,
        "missing_yes_asks": 1,
        "missing_no_asks": 1,
        "invalid_yes_asks": 0,
        "invalid_no_asks": 0,
    }

    async def scenario():
        engine = BacktestEngine(BacktestConfig(fill_model="depth_snapshot", ticks=ticks))
        try:
            with pytest.raises(DepthSnapshotUnavailable, match="requires yes_asks and no_asks"):
                await engine.run()
        finally:
            await engine.aclose()

    asyncio.run(scenario())


def test_depth_replay_preserves_leg_timestamp_skew_for_freshness_gate():
    ticks = [{
        "timestamp": 10.6,
        "yes_timestamp": 10.0,
        "no_timestamp": 10.6,
        "yes_ask": 0.40,
        "no_ask": 0.56,
        "yes_bid": 0.39,
        "no_bid": 0.55,
        "yes_size": 100.0,
        "no_size": 100.0,
        "yes_asks": [[0.40, 100.0]],
        "no_asks": [[0.56, 100.0]],
    }]

    async def scenario():
        engine = BacktestEngine(BacktestConfig(fill_model="depth_snapshot", ticks=ticks))
        try:
            result = await engine.run()
        finally:
            await engine.aclose()
        assert result.metrics.opportunities == 0
        assert result.metrics.missed == 1
        assert result.metrics.fresh_attempts["0.5-2s"] == 1

    asyncio.run(scenario())


def test_depth_replay_uses_retained_window_close_for_no_entry_gate():
    ticks = [{
        "timestamp": 296.0,
        "yes_timestamp": 296.0,
        "no_timestamp": 296.0,
        "window_start": 0.0,
        "window_close": 300.0,
        "yes_ask": 0.40,
        "no_ask": 0.56,
        "yes_bid": 0.39,
        "no_bid": 0.55,
        "yes_size": 100.0,
        "no_size": 100.0,
        "yes_asks": [[0.40, 100.0]],
        "no_asks": [[0.56, 100.0]],
    }]

    async def scenario():
        engine = BacktestEngine(BacktestConfig(fill_model="depth_snapshot", ticks=ticks))
        try:
            result = await engine.run()
        finally:
            await engine.aclose()
        assert result.metrics.opportunities == 0
        assert result.metrics.missed == 1

    asyncio.run(scenario())
