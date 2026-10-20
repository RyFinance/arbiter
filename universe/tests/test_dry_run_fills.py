"""DRY_RUN fills come from the real public book, not the decision price."""
from types import SimpleNamespace

import pytest

from polyuni.clients import trading_client as tc

BOOK = {"asks": [{"price": "0.47", "size": "4"}, {"price": "0.49", "size": "20"}],
        "bids": [{"price": "0.45", "size": "50"}]}


class _Resp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


def _client():
    client = tc.TradingClient.__new__(tc.TradingClient)
    client.s = SimpleNamespace(clob_base_url="https://clob.example")
    client._paper_fill_details = {}
    return client


def test_buy_walks_depth_to_vwap(monkeypatch):
    monkeypatch.setattr(tc.httpx, "get", lambda *a, **k: _Resp(BOOK))
    client = _client()
    oid = client._dry_run_book_fill("tok", 0.50, 10, "BUY")
    detail = client._paper_fill_details[oid]
    # 4 @ 0.47 + 6 @ 0.49 -> 0.482, worse than top of book
    assert detail["fill_price"] == pytest.approx(0.482)
    assert detail["fill_source"] == "public_rest_book"


def test_insufficient_depth_within_limit_does_not_fill(monkeypatch):
    monkeypatch.setattr(tc.httpx, "get", lambda *a, **k: _Resp(BOOK))
    assert _client()._dry_run_book_fill("tok", 0.48, 10, "BUY") is None


def test_book_outage_does_not_fill(monkeypatch):
    def boom(*a, **k):
        raise tc.httpx.ConnectError("down")
    monkeypatch.setattr(tc.httpx, "get", boom)
    assert _client()._dry_run_book_fill("tok", 0.50, 10, "BUY") is None
