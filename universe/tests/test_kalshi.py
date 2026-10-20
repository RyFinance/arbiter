"""Kalshi venue: book maths, fees, market mapping and fill-or-kill orders."""
import dataclasses
from types import SimpleNamespace

import pytest

from polyuni import config, fees, kalshi
from polyuni.clients import trading_client as tc

RAW_BOOK = {"orderbook_fp": {
    "yes_dollars": [["0.4000", "50"], ["0.4400", "20"]],   # YES bids, ascending
    "no_dollars": [["0.5000", "5"], ["0.5300", "8"]],      # NO bids, ascending
}}


def test_parse_book_and_top_of_book():
    book = kalshi.parse_book(RAW_BOOK)
    top = kalshi.top_of_book(book)
    assert top["yes_ask"] == pytest.approx(0.47)      # 1 - best NO bid 0.53
    assert top["yes_ask_size"] == 8
    assert top["yes_bid"] == pytest.approx(0.44)
    assert top["no_ask"] == pytest.approx(0.56)       # 1 - best YES bid 0.44
    assert top["no_bid"] == pytest.approx(0.53)


def test_legacy_cents_book():
    book = kalshi.parse_book({"orderbook": {"yes": [[44, 20]], "no": [[53, 8]]}})
    assert kalshi.top_of_book(book)["yes_ask"] == pytest.approx(0.47)


def test_side_book_feeds_the_shared_fok_walk():
    asks = kalshi.side_book(kalshi.parse_book(RAW_BOOK), "yes")
    # 8 @ 0.47 + 2 @ 0.50 for 10 contracts
    vwap, _ = tc._paper_full_fill_price(asks, side="BUY", limit_price=0.50, size=10)
    assert vwap == pytest.approx((8 * 0.47 + 2 * 0.50) / 10)
    assert tc._paper_full_fill_price(asks, side="BUY", limit_price=0.48, size=10)[0] is None


def test_kalshi_fee_rounds_up_to_the_cent():
    assert kalshi.kalshi_fee(10, 0.5) == 0.18            # 0.175 -> 0.18
    assert kalshi.kalshi_fee(1, 0.01) == 0.01             # 0.000693 -> 0.01
    assert kalshi.kalshi_fee(0, 0.5) == 0.0


def test_engine_fee_uses_kalshi_rounding_when_venue_is_kalshi(monkeypatch):
    monkeypatch.setattr(config, "SETTINGS", dataclasses.replace(config.SETTINGS, venue="kalshi"))
    assert fees.taker_fee(10, 0.5, 0.07) == kalshi.kalshi_fee(10, 0.5)


def test_market_meta_maps_btc_15m_as_up_down():
    meta = kalshi.market_meta({
        "ticker": "KXBTC15M-26OCT201830-30", "title": "BTC price up in next 15 mins?",
        "yes_sub_title": "Target Price: $82,984.38", "market_type": "binary", "status": "active",
        "open_time": "2026-10-20T22:15:00Z", "close_time": "2026-10-20T22:30:00Z",
        "yes_bid_dollars": "0.7200", "yes_ask_dollars": "0.7300",
        "yes_bid_size_fp": "100", "yes_ask_size_fp": "100", "volume_24h_fp": "0",
    })
    assert meta.yes_token_id == "KXBTC15M-26OCT201830-30:yes"
    assert meta.is_btc_up_down and meta.accepting_orders
    assert meta.end_time - meta.start_time == 900
    assert meta.fee_rate == 0.07 and meta.spread_at_discovery == pytest.approx(0.01)


def test_order_body_is_fill_or_kill():
    body = kalshi.KalshiOrderClient.order_body("KXBTC15M-X:no", 0.27, 10, "BUY")
    assert body["ticker"] == "KXBTC15M-X" and body["side"] == "no" and body["action"] == "buy"
    assert body["no_price"] == 27 and body["count"] == 10 and body["time_in_force"] == "fill_or_kill"
    assert kalshi.KalshiOrderClient.order_body("T:yes", 0.995, 1, "SELL")["yes_price_dollars"] == "0.9950"


class _Resp:
    def __init__(self, status, data):
        self.status_code, self._data, self.text = status, data, str(data)

    def json(self):
        return self._data


def _order_client(resp):
    client = kalshi.KalshiOrderClient.__new__(kalshi.KalshiOrderClient)
    client.base, client.prefix = "https://kalshi.example/trade-api/v2", "/trade-api/v2"
    client._headers = lambda method, path: {}
    client.http = SimpleNamespace(post=lambda url, json, headers: resp)
    return client


def test_post_fok_returns_fill_price_on_full_execution():
    resp = _Resp(201, {"order": {"order_id": "o1", "status": "executed", "fill_count_fp": "10",
                                 "taker_fill_cost_dollars": "4.6000"}})
    assert _order_client(resp).post_fok("T:yes", 0.47, 10, "BUY") == ("o1", pytest.approx(0.46))


def test_post_fok_killed_or_rejected_is_no_fill():
    killed = _Resp(201, {"order": {"order_id": "o2", "status": "canceled", "fill_count_fp": "0"}})
    assert _order_client(killed).post_fok("T:yes", 0.47, 10, "BUY") is None
    assert _order_client(_Resp(400, {"error": "bad"})).post_fok("T:yes", 0.47, 10, "BUY") is None
    assert _order_client(killed).post_fok("T:yes", 0.47, 2.5, "BUY") is None  # whole contracts only


def test_dry_run_fill_walks_the_kalshi_book(monkeypatch):
    monkeypatch.setattr(kalshi, "fetch_book_sync", lambda ticker: kalshi.parse_book(RAW_BOOK))
    client = tc.TradingClient.__new__(tc.TradingClient)
    client.s = SimpleNamespace(clob_base_url="https://unused")
    client._paper_fill_details = {}
    oid = client._dry_run_book_fill("KXBTC15M-X:yes", 0.50, 10, "BUY")
    assert client._paper_fill_details[oid]["fill_price"] == pytest.approx(0.476)


def test_kalshi_live_order_respects_cap(monkeypatch):
    monkeypatch.setenv("ARBITER_LIVE_TRADING", "enabled")
    monkeypatch.setenv("LIVE_EXECUTION", "true")
    calls = []
    client = tc.TradingClient.__new__(tc.TradingClient)
    client.s = SimpleNamespace(live_execution=True, live_max_order_usd=5.0)
    client._venue = "kalshi"
    client._kalshi_orders = SimpleNamespace(post_fok=lambda *a: calls.append(a) or ("o9", 0.45))
    client._paper_fill_details = {}
    assert client._post_live_fok("T:yes", 0.5, 20, "BUY") is None and not calls   # $10 > $5
    assert client._post_live_fok("T:yes", 0.45, 10, "BUY") == "o9"
    assert client.get_fill_report("o9")["fill_price"] == pytest.approx(0.45)


def test_botctl_rejects_unknown_venue():
    from dashboard import botctl
    assert botctl.start_bot(venue="binance") == {"error": "venue must be one of polymarket, kalshi"}


def test_next_window_is_tracked_shortly_before_it_opens():
    base = {"ticker": "KXBTC15M-NEXT", "title": "BTC price up in next 15 mins?", "market_type": "binary",
            "status": "initialized", "open_time": "2026-10-20T22:45:00Z", "close_time": "2026-10-20T23:00:00Z"}
    opens = kalshi._epoch(base["open_time"])
    assert kalshi.market_meta(base, now=opens - 120).accepting_orders          # 2 min before open
    assert not kalshi.market_meta(base, now=opens - 3600).accepting_orders     # an hour out
    assert not kalshi.market_meta(dict(base, status="closed"), now=opens + 10).accepting_orders
