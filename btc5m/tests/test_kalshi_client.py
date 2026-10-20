from __future__ import annotations

import httpx
import pytest

from polymarket_arb.clients.kalshi_client import KalshiClient, KalshiOrderBook


MARKET = {
    "event_ticker": "KXBTC15M-26AUG030315",
    "ticker": "KXBTC15M-26AUG030315-15",
    "title": "BTC price up in next 15 mins?",
    "status": "active",
    "open_time": "2026-08-03T07:00:00Z",
    "close_time": "2026-08-03T07:15:00Z",
    "expected_expiration_time": "2026-08-03T07:20:00Z",
    "rules_primary": "If CF Benchmarks BRTI end average is at least start average, resolves Yes.",
    "rules_secondary": "Uses CF Benchmarks RTI 60-price average.",
    "yes_ask_dollars": "0.4300",
    "yes_bid_dollars": "0.4200",
    "no_ask_dollars": "0.5800",
    "no_bid_dollars": "0.5700",
    "yes_ask_size_fp": "100.00",
    "yes_bid_size_fp": "90.00",
    "no_ask_size_fp": "80.00",
    "no_bid_size_fp": "70.00",
}


@pytest.mark.asyncio
async def test_list_btc_15m_markets_parses_public_payload():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/markets"
        assert request.url.params["series_ticker"] == "KXBTC15M"
        assert request.url.params["status"] == "open"
        return httpx.Response(200, json={"markets": [MARKET]})

    session = httpx.AsyncClient(
        base_url="https://example.test",
        transport=httpx.MockTransport(handler),
    )
    client = KalshiClient(base_url="https://example.test", session=session)

    markets = await client.list_btc_15m_markets()
    await session.aclose()

    assert len(markets) == 1
    market = markets[0]
    assert market.ticker == "KXBTC15M-26AUG030315-15"
    assert market.yes_ask == 0.43
    assert market.no_ask == 0.58
    assert market.open_time.isoformat() == "2026-08-03T07:00:00+00:00"
    assert market.close_time.isoformat() == "2026-08-03T07:15:00+00:00"
    assert market.is_tradable is True


@pytest.mark.asyncio
async def test_get_event_markets_carries_settlement_sources():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "event": {"settlement_sources": [{"name": "CF Benchmarks"}]},
                "markets": [MARKET],
            },
        )

    session = httpx.AsyncClient(
        base_url="https://example.test",
        transport=httpx.MockTransport(handler),
    )
    client = KalshiClient(base_url="https://example.test", session=session)

    markets = await client.get_event_markets("KXBTC15M-26AUG030315")
    await session.aclose()

    assert markets[0].settlement_sources == [{"name": "CF Benchmarks"}]
    assert "CF Benchmarks" in markets[0].rules_primary


def test_orderbook_parses_price_levels():
    book = KalshiOrderBook.from_payload(
        "KXBTC15M-TEST",
        {
            "orderbook_fp": {
                "yes_dollars": [["0.4100", "2651.00"]],
                "no_dollars": [["0.5700", "600.00"]],
            }
        },
    )

    assert book.yes == [(0.41, 2651.0)]
    assert book.no == [(0.57, 600.0)]
