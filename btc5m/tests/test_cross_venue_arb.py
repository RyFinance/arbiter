from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest

from polymarket_arb.clients.kalshi_client import KalshiBTCMarket, KalshiClient
from polymarket_arb.cross_venue_arb import (
    CrossVenueArbObserver,
    compute_price_gaps,
    best_positive_gap,
    kalshi_to_poly_window_starts,
    observer_error_record,
    summarize_log,
)


def kalshi_market() -> KalshiBTCMarket:
    return KalshiBTCMarket.from_market_payload(
        {
            "event_ticker": "KXBTC15M-26AUG030315",
            "ticker": "KXBTC15M-26AUG030315-15",
            "title": "BTC price up in next 15 mins?",
            "status": "active",
            "open_time": "2026-08-03T07:00:00Z",
            "close_time": "2026-08-03T07:15:00Z",
            "expected_expiration_time": "2026-08-03T07:20:00Z",
            "rules_primary": "CF Benchmarks BRTI",
            "rules_secondary": "CF Benchmarks RTI",
            "yes_ask_dollars": "0.4300",
            "yes_bid_dollars": "0.4200",
            "no_ask_dollars": "0.5800",
            "no_bid_dollars": "0.5700",
            "yes_ask_size_fp": "100.00",
            "no_ask_size_fp": "100.00",
        }
    )


def test_kalshi_to_poly_window_starts_maps_one_15m_to_three_5m():
    starts = kalshi_to_poly_window_starts(kalshi_market())
    assert len(starts) == 3
    assert [datetime.fromtimestamp(ts, timezone.utc).minute for ts in starts] == [0, 5, 10]


def test_compute_price_gaps_and_best_gap():
    gaps = compute_price_gaps(
        kalshi_yes_ask=0.43,
        kalshi_no_ask=0.58,
        poly_up_proxy=0.51,
        poly_down_proxy=0.49,
    )

    assert gaps["kalshi_yes_cheaper"] == pytest.approx(0.08)
    assert gaps["polymarket_down_proxy_cheaper"] == pytest.approx(0.09)
    assert best_positive_gap(gaps) == ("NO/DOWN", "polymarket_proxy", pytest.approx(0.09))


@pytest.mark.asyncio
async def test_observer_scores_mocked_pair(tmp_path):
    async def kalshi_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"markets": [kalshi_market().raw]})

    async def market_handler(request: httpx.Request) -> httpx.Response:
        if "gamma-api.polymarket.com" in str(request.url):
            slug = request.url.path.rsplit("/", 1)[-1]
            token_suffix = slug[-3:]
            return httpx.Response(
                200,
                json={
                    "id": slug,
                    "question": slug,
                    "clobTokenIds": f'["up-{token_suffix}", "down-{token_suffix}"]',
                    "resolutionSource": "https://data.chain.link/streams/btc-usd",
                    "acceptingOrders": True,
                    "active": True,
                    "closed": False,
                },
            )
        token_id = request.url.params["token_id"]
        price = "0.5100" if str(token_id).startswith("up") else "0.4900"
        return httpx.Response(
            200,
            json={
                "asks": [{"price": price, "size": "10"}],
                "bids": [{"price": str(float(price) - 0.01), "size": "10"}],
            },
        )

    kalshi_session = httpx.AsyncClient(
        base_url="https://example-kalshi.test",
        transport=httpx.MockTransport(kalshi_handler),
    )
    poly_session = httpx.AsyncClient(transport=httpx.MockTransport(market_handler))
    client = KalshiClient(base_url="https://example-kalshi.test", session=kalshi_session)
    observer = CrossVenueArbObserver(client, poly_session, log_path=tmp_path / "cross.jsonl")

    snapshots = await observer.observe_once()
    observer.append_logs(snapshots)

    await kalshi_session.aclose()
    await poly_session.aclose()

    assert len(snapshots) == 1
    snapshot = snapshots[0]
    assert snapshot.status == "ok"
    assert snapshot.poly_up_proxy == pytest.approx(0.51)
    assert snapshot.poly_down_proxy == pytest.approx(0.49)
    assert snapshot.best_gap_side == "NO/DOWN"
    assert snapshot.best_gap_venue == "polymarket_proxy"
    assert snapshot.oracle_mismatch_risk is True
    assert (tmp_path / "cross.jsonl").exists()


def test_observer_error_record_is_summarized(tmp_path):
    path = tmp_path / "cross.jsonl"
    record = observer_error_record(httpx.ConnectTimeout("kalshi timeout"))
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")

    summary = summarize_log(path)

    assert record["status"] == "error"
    assert record["reason"] == "observer_poll_failed"
    assert record["oracle_mismatch_risk"] is True
    assert summary["records"] == 1
    assert summary["status_counts"] == {"error": 1}
    assert summary["oracle_mismatch_risk_records"] == 1
