from __future__ import annotations

import asyncio

from polymarket_arb.models import MarketState
from polymarket_arb.price_feed import PriceFeed


def test_same_loop_leg_updates_are_evaluated_as_one_coherent_pair():
    state = MarketState(
        market_id="m",
        question="q",
        yes_token_id="yes",
        no_token_id="no",
    )
    observed: list[tuple[float | None, float | None]] = []
    feed = PriceFeed({"m": state}, lambda st: observed.append(
        (st.best_yes_ask, st.best_no_ask)
    ))
    feed.build_token_index()

    async def exercise() -> None:
        feed._on_ws_tick("no", 0.43, 10.0)
        await asyncio.sleep(0)
        observed.clear()
        # This reproduces a two-book WebSocket frame. The old implementation
        # evaluated 0.55 + stale 0.43 before applying the paired 0.46 update.
        feed._on_ws_tick("yes", 0.55, 10.0)
        feed._on_ws_tick("no", 0.46, 10.0)
        assert observed == []
        await asyncio.sleep(0.02)

    asyncio.run(exercise())

    assert observed == [(0.55, 0.46)]
    assert feed._coalescing_logged_markets == {"m"}
    assert feed.coherent_pair_evaluations == 1
    assert feed.incomplete_pair_suppressions == 0


def test_synchronous_tick_still_flushes_without_a_running_loop():
    state = MarketState(
        market_id="m",
        question="q",
        yes_token_id="yes",
        no_token_id="no",
    )
    observed = []
    feed = PriceFeed({"m": state}, observed.append)
    feed.build_token_index()

    feed._on_ws_tick("yes", 0.40, 10.0)

    assert observed == [state]


def test_adjacent_frames_inside_ten_ms_settle_window_are_coalesced():
    state = MarketState(
        market_id="m",
        question="q",
        yes_token_id="yes",
        no_token_id="no",
    )
    observed: list[tuple[float | None, float | None]] = []
    feed = PriceFeed(
        {"m": state},
        lambda st: observed.append((st.best_yes_ask, st.best_no_ask)),
    )
    feed.build_token_index()

    async def exercise() -> None:
        feed._on_ws_tick("no", 0.43, 10.0)
        await asyncio.sleep(0.02)
        observed.clear()
        feed._on_ws_tick("yes", 0.55, 10.0)
        # The independent real-depth capture observed the paired leg 0.330–5.276ms
        # later. Reproduce separate frames inside that measured envelope.
        await asyncio.sleep(0.006)
        feed._on_ws_tick("no", 0.46, 10.0)
        assert observed == []
        await asyncio.sleep(0.01)

    asyncio.run(exercise())

    assert observed == [(0.55, 0.46)]


def test_close_cancels_pending_coalesced_evaluation():
    state = MarketState(
        market_id="m",
        question="q",
        yes_token_id="yes",
        no_token_id="no",
    )
    observed = []
    feed = PriceFeed({"m": state}, observed.append)
    feed.build_token_index()

    async def exercise() -> None:
        feed._on_ws_tick("yes", 0.40, 10.0)
        await feed.aclose()
        await asyncio.sleep(0.02)

    asyncio.run(exercise())

    assert observed == []


def test_one_sided_update_timeout_still_evaluates_with_last_known_leg():
    """A lone leg tick past the settle window must still reach on_update.

    The old behavior dropped it entirely, which meant most real single-leg
    order-book moves (the common case — both legs almost never tick within
    the same cross_leg_settle_ms window) never reached arb_engine at all, so
    a genuinely crossed book was invisible. The staleness gate in arb_engine
    (max_quote_age_ms) is what actually protects against combining a fresh
    leg with a meaningfully stale opposite leg — this coalescing hold is only
    meant to prefer a truly simultaneous pair when one is available.
    """
    state = MarketState(
        market_id="m",
        question="q",
        yes_token_id="yes",
        no_token_id="no",
    )
    observed = []
    feed = PriceFeed({"m": state}, observed.append)
    feed.build_token_index()

    async def exercise() -> None:
        feed._on_ws_tick("yes", 0.40, 10.0)
        await asyncio.sleep(0.02)

    asyncio.run(exercise())

    assert observed == [state]
    assert feed._incomplete_logged_markets == {"m"}
    assert feed.incomplete_pair_suppressions == 1
    assert feed.coherent_pair_evaluations == 0
