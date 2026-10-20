from __future__ import annotations

import asyncio

import httpx
import pytest

from polyuni.clients.clob_client import fetch_book, fetch_book_top
from polyuni.models import MarketState
from polyuni.price_feed import PriceFeed


@pytest.mark.asyncio
async def test_raw_leg_callback_runs_even_when_pair_coalescer_suppresses_one_leg():
    st = MarketState("m1", "test", "yes", "no")
    raw = []
    coherent = []
    feed = PriceFeed(
        {"m1": st},
        on_update=coherent.append,
        on_leg_update=lambda state, token: raw.append((state.market_id, token)),
    )
    feed.build_token_index()

    feed._on_ws_tick("yes", 0.48, 20.0, 0.47)
    await asyncio.sleep(0.03)

    assert raw == [("m1", "yes")]
    assert coherent == []
    assert st.best_yes_bid == 0.47


@pytest.mark.asyncio
async def test_rest_fallback_retains_bid_for_protective_exit():
    def handler(request):
        return httpx.Response(200, json={
            "asks": [{"price": "0.48", "size": "12"}],
            "bids": [{"price": "0.46", "size": "9"}],
        })

    async with httpx.AsyncClient(
        base_url="https://clob.polymarket.com",
        transport=httpx.MockTransport(handler),
    ) as client:
        assert await fetch_book_top(client, "token") == (0.48, 12.0, 0.46)
        assert await fetch_book(client, "token") == (0.48, 12.0)
