"""Regression tests for data-plane failures that must not remain silent."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from polymarket_arb.alerting import WebhookAlerter, ALERT_MARKET_RESOLUTION_FAILURE
from polymarket_arb.backtest_engine import (
    BacktestConfig,
    BacktestEngine,
    CandleFallbackUnavailable,
)
from polymarket_arb.models import MarketState
from polymarket_arb.price_feed import PriceFeed
from polymarket_arb.window_tracker import WindowTracker


def test_gamma_resolution_failure_is_alerted_and_counted():
    alerter = MagicMock(spec=WebhookAlerter)
    tracker = WindowTracker(MagicMock(), AsyncMock(), lambda _: None, alerter=alerter)
    with patch("polymarket_arb.window_tracker.resolve_window_market", new=AsyncMock(return_value=None)):
        assert asyncio.run(tracker._resolve(1_700_000_000)) is None
    assert tracker.resolve_failures == 1
    assert alerter.alert.call_args.args[0] == ALERT_MARKET_RESOLUTION_FAILURE


def test_failed_resubscribe_marks_feed_unhealthy():
    async def check():
        pf = PriceFeed({}, MagicMock())
        pf._feed = SimpleNamespace(healthy=True, resubscribe=AsyncMock(side_effect=RuntimeError("boom")))
        st = MarketState(market_id="m", question="q", yes_token_id="y", no_token_id="n")
        await pf.add_market(st)
        assert pf._feed.healthy is False
    asyncio.run(check())


def test_candle_fallback_is_explicitly_degraded():
    async def check():
        engine = BacktestEngine(BacktestConfig(use_lse=True))
        engine._lse = SimpleNamespace(
            start_tick_export=AsyncMock(return_value=None),
            fetch_candles=AsyncMock(return_value=[{"timestamp": 1, "close": .5, "volume": 2}]),
        )
        with pytest.raises(CandleFallbackUnavailable, match="not comparable"):
            await engine._load_ticks()
        assert engine._data_quality == "UNAVAILABLE_CANDLE_FALLBACK_NOT_COMPARABLE"
        assert engine._candle_fallback_ticks == 1
    asyncio.run(check())


def test_private_candle_fabricator_is_disabled():
    engine = BacktestEngine(BacktestConfig(use_lse=False))
    with pytest.raises(CandleFallbackUnavailable, match="refusing to fabricate"):
        engine._candles_to_ticks([{"timestamp": 1, "close": 0.5, "volume": 2}])
    assert engine.metrics.opportunities == 0
