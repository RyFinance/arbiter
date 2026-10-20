from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest

from polyuni.clients import gamma_client


@pytest.mark.asyncio
async def test_offset_cap_422_cleanly_ends_pagination(monkeypatch):
    """Gamma's offset ceiling is a normal pagination terminator, not a failed scan."""
    monkeypatch.setattr(gamma_client, "_MAX_PAGES", 2)
    request = httpx.Request("GET", "https://gamma-api.polymarket.com/markets")
    full_page = httpx.Response(200, json=[{} for _ in range(100)], request=request)
    offset_cap = httpx.Response(
        422, json={"error": "offset is too large"}, request=request
    )
    session = AsyncMock()
    session.get = AsyncMock(side_effect=[full_page, offset_cap])

    markets = await gamma_client.discover_markets(session)

    assert markets == []
    assert session.get.await_count == 2


@pytest.mark.asyncio
async def test_first_page_422_remains_a_failed_scan(monkeypatch):
    monkeypatch.setattr(gamma_client, "_MAX_PAGES", 1)
    request = httpx.Request("GET", "https://gamma-api.polymarket.com/markets")
    failure = httpx.Response(422, json={"error": "bad filter"}, request=request)
    session = AsyncMock()
    session.get = AsyncMock(return_value=failure)

    markets = await gamma_client.discover_markets(session)

    assert markets == []
    assert session.get.await_count == 1


def test_up_down_market_uses_slug_window_start_and_fee_metadata():
    meta = gamma_client._parse_market({
        "id": "btc-window",
        "slug": "btc-updown-5m-1800000000",
        "question": "Bitcoin Up or Down",
        "outcomes": '["Up", "Down"]',
        "clobTokenIds": '["up-token", "down-token"]',
        # This creation timestamp must not override the actual slug boundary.
        "startDate": "2026-01-01T00:00:00Z",
        "endDate": "2027-01-15T08:05:00Z",
        "feesEnabled": True,
        "feeSchedule": {"rate": 0.07, "exponent": 1},
    })

    assert meta is not None
    assert (meta.outcome_yes, meta.outcome_no) == ("Up", "Down")
    assert (meta.yes_token_id, meta.no_token_id) == ("up-token", "down-token")
    assert meta.start_time == 1_800_000_000.0
    assert meta.fee_rate == pytest.approx(0.07)


@pytest.mark.asyncio
async def test_short_window_discovery_fetches_current_and_next_5m_and_15m():
    async def get(path, **kwargs):
        slug = path.rsplit("/", 1)[-1]
        boundary = int(slug.rsplit("-", 1)[-1])
        request = httpx.Request("GET", f"https://gamma-api.polymarket.com{path}")
        return httpx.Response(200, request=request, json={
            "id": slug,
            "slug": slug,
            "question": "Bitcoin Up or Down",
            "outcomes": '["Up", "Down"]',
            "clobTokenIds": f'["{slug}-up", "{slug}-down"]',
            "active": True,
            "closed": False,
            "acceptingOrders": True,
            "enableOrderBook": True,
            "eventStartTime": "2027-01-15T08:00:00Z",
            "endDate": "2027-01-15T08:15:00Z",
            "liquidityNum": 10000,
            "volume24hr": 0,
            "spread": 0.01,
            "feesEnabled": True,
            "feeSchedule": {"rate": 0.07, "exponent": 1},
            "boundary": boundary,
        })

    session = AsyncMock()
    session.get = AsyncMock(side_effect=get)
    markets = await gamma_client.discover_btc_short_windows(
        session, now=1_800_000_123,
    )

    assert {m.slug for m in markets} == {
        "btc-updown-5m-1800000000",
        "btc-updown-5m-1800000300",
        "btc-updown-15m-1800000000",
        "btc-updown-15m-1800000900",
    }
    assert session.get.await_count == 4
