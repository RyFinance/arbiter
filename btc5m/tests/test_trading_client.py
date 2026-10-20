"""Tests for TradingClient.cancel_order (TASK C — CANCEL_ORDER PROPER FIX).

Verifies the fix: when canceling by token with no explicit order ID, the client
fetches open orders for that token via the SDK and cancels each by its real order ID,
instead of just logging a warning and no-oping.

The test uses a fake ClobClient substitute so no network is required.
"""
from __future__ import annotations

import asyncio
import time
from typing import Optional
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from polymarket_arb.clients.trading_client import (
    TradingClient,
    _paper_full_fill_price,
    _resolve_paper_fill_source,
)
from polymarket_arb.config import SETTINGS


class _FakeClobClient:
    """Mirrors the subset of ClobClient API used by cancel_order."""

    def __init__(self, open_orders=None, cancel_calls=None):
        # open_orders: list of dicts returned by get_open_orders for any token.
        self._open_orders = open_orders or []
        # cancel_calls: list to append cancelled order ids to for assertions.
        self.cancel_calls = cancel_calls if cancel_calls is not None else []

    def get_open_orders(self, params=None, only_first_page=False, next_cursor=None):
        # The real SDK paginates; here we just return the canned list.
        return list(self._open_orders)

    def cancel_orders(self, order_hashes):
        self.cancel_calls.extend(order_hashes)
        return {"success": True}


def _make_live_client(open_orders):
    """Build a TradingClient wired into LIVE mode with a fake SDK client."""
    # Fabricate a lightweight settings shim so `self.s.live` is True without
    # touching the real env-based SETTINGS (which is dry_run by default).
    fake_settings = MagicMock()
    fake_settings.live = True

    tc = TradingClient.__new__(TradingClient)
    tc.s = fake_settings
    tc._clob_client = _FakeClobClient(open_orders=open_orders)
    tc._address = "0x0000000000000000000000000000000000000000"
    tc._creds = MagicMock()  # ensure _ensure_creds is a no-op path
    return tc


def test_cancel_by_token_fetches_and_cancels_open_order():
    """A resting order on a token is discoverable via get_open_orders and canceled."""
    token_id = "0xTOKENABCDEF1234"
    real_order_id = "order-1234"
    open_orders = [
        {
            "order_id": real_order_id,
            "status": "OPEN",
            "token_id": token_id,
        }
    ]

    tc = _make_live_client(open_orders)
    fake_client = tc._clob_client  # type: ignore[attr-defined]

    result = asyncio.run(tc.cancel_order(token_id))

    assert result is True
    assert real_order_id in fake_client.cancel_calls, (
        f"expected order {real_order_id} to be canceled, calls={fake_client.cancel_calls}"
    )


def test_cancel_by_token_cancels_all_matching_open_orders():
    """Multiple open orders on the same token are all canceled."""
    token_id = "0xTOKENMULTI"
    open_orders = [
        {"order_id": "order-A", "status": "OPEN", "token_id": token_id},
        {"order_id": "order-B", "status": "OPEN", "token_id": token_id},
        {"order_id": "order-C", "status": "FILLED", "token_id": token_id},  # terminal, skip
    ]

    tc = _make_live_client(open_orders)
    fake_client = tc._clob_client  # type: ignore[attr-defined]

    result = asyncio.run(tc.cancel_order(token_id))
    assert result is True
    # Filled/canceled/expired/rejected statuses are skipped.
    assert set(fake_client.cancel_calls) == {"order-A", "order-B"}


def test_cancel_by_token_no_open_orders_returns_true():
    """If no open orders for the token, cancel is a no-op success (no exception)."""
    token_id = "0xEMPTY"
    tc = _make_live_client(open_orders=[])
    fake_client = tc._clob_client  # type: ignore[attr-defined]

    result = asyncio.run(tc.cancel_order(token_id))
    assert result is True
    assert fake_client.cancel_calls == []


def test_cancel_by_explicit_order_id_skips_fetch():
    """An explicit missing_order_id cancels directly without querying open orders."""
    token_id = "0xTOKENWITHID"
    real_order_id = "explicit-999"
    tc = _make_live_client(open_orders=[])
    fake_client = tc._clob_client  # type: ignore[attr-defined]

    result = asyncio.run(tc.cancel_order(token_id, missing_order_id=real_order_id))
    assert result is True
    assert fake_client.cancel_calls == [real_order_id]


def test_paper_submit_runs_sync_fill_simulation_off_event_loop():
    """PAPER_TRADE book simulation should not block unrelated event-loop work."""
    fake_settings = MagicMock()
    fake_settings.live = True

    tc = TradingClient.__new__(TradingClient)
    tc.s = fake_settings
    tc._paper_trade = True

    def slow_fill(token_id, price, size, side):
        time.sleep(0.05)
        return f"paper-{token_id}"

    tc._paper_simulate_fill = slow_fill

    async def run_two_submits():
        started = time.monotonic()
        results = await asyncio.gather(
            tc.submit_order("yes-token", 0.4, 10.0),
            tc.submit_order("no-token", 0.5, 10.0),
        )
        return results, time.monotonic() - started

    results, elapsed = asyncio.run(run_two_submits())

    assert results == ["paper-yes-token", "paper-no-token"]
    assert elapsed < 0.15


def test_paper_submit_with_cached_creds_does_not_derive_auth():
    fake_settings = MagicMock()
    fake_settings.live = True

    tc = TradingClient.__new__(TradingClient)
    tc.s = fake_settings
    tc._paper_trade = True
    tc._creds = MagicMock()
    tc._clob_client = MagicMock()
    tc._clob_client.create_or_derive_api_key = MagicMock()

    tc._paper_simulate_fill = MagicMock(return_value="paper-order")

    result = asyncio.run(tc.submit_order("token", 0.5, 10.0))

    assert result == "paper-order"
    tc._clob_client.create_or_derive_api_key.assert_not_called()


def test_paper_snapshot_fill_skips_rest_and_preserves_decision_price():
    fake_settings = MagicMock()
    fake_settings.live = True
    tc = TradingClient.__new__(TradingClient)
    tc.s = fake_settings
    tc._paper_trade = True
    tc._ensure_creds = MagicMock()
    tc._clob_client = MagicMock()

    result = asyncio.run(
        tc.submit_paper_order("yes-token", 0.40, 10.0, observed_fill_price=0.40)
    )

    assert result is not None
    tc._ensure_creds.assert_not_called()
    tc._clob_client.get_order_book.assert_not_called()


def test_paper_rest_fallback_rejects_non_marketable_limit():
    fake_settings = MagicMock()
    fake_settings.live = True
    tc = TradingClient.__new__(TradingClient)
    tc.s = fake_settings
    tc._paper_trade = True
    tc._ensure_creds = MagicMock()
    tc._clob_client = MagicMock()
    tc._clob_client.get_order_book.return_value = {"asks": [{"price": "0.60"}]}

    result = tc._paper_simulate_fill("yes-token", 0.40, 10.0, "BUY")

    assert result is None
    tc._clob_client.get_order_book.assert_called_once_with("yes-token")


def test_paper_rest_source_uses_submission_book_and_records_fill_detail():
    fake_settings = MagicMock()
    fake_settings.live = True
    tc = TradingClient.__new__(TradingClient)
    tc.s = fake_settings
    tc._paper_trade = True
    tc._paper_fill_source = "rest_book"
    tc._paper_fill_details = {}
    tc._order_modes = {}
    tc._ensure_creds = MagicMock()
    tc._clob_client = MagicMock()
    # Polymarket may return asks worst-first; the canonical parser must scan for min.
    tc._clob_client.get_order_book.return_value = {
        "asks": [{"price": "0.99", "size": "1"}, {"price": "0.39", "size": "10"}],
    }

    order_id = asyncio.run(
        tc.submit_paper_order("yes-token", 0.40, 10.0, observed_fill_price=0.40)
    )
    report = tc.get_fill_report(order_id)

    assert order_id is not None
    assert report["fill_price"] == pytest.approx(0.39)
    assert report["fill_source"] == "rest_book"
    assert report["limit_price"] == pytest.approx(0.40)
    tc._clob_client.get_order_book.assert_called_once_with("yes-token")


def test_paper_rest_fill_walks_depth_and_requires_full_requested_size():
    book = {
        "asks": [
            {"price": "0.39", "size": "4"},
            {"price": "0.40", "size": "6"},
            {"price": "0.41", "size": "100"},
        ]
    }
    vwap, eligible = _paper_full_fill_price(
        book, side="BUY", limit_price=0.40, size=10.0
    )
    assert vwap == pytest.approx(0.396)
    assert eligible == pytest.approx(10.0)
    missing, eligible = _paper_full_fill_price(
        book, side="BUY", limit_price=0.39, size=10.0
    )
    assert missing is None
    assert eligible == pytest.approx(4.0)

    sell_vwap, sell_eligible = _paper_full_fill_price(
        {
            "bids": [
                {"price": "0.40", "size": "4"},
                {"price": "0.39", "size": "6"},
                {"price": "0.38", "size": "100"},
            ]
        },
        side="SELL",
        limit_price=0.39,
        size=10.0,
    )
    assert sell_vwap == pytest.approx(0.394)
    assert sell_eligible == pytest.approx(10.0)


def test_paper_fill_source_defaults_to_rest_book(monkeypatch):
    """Regression: PAPER_FILL_SOURCE unset must resolve to rest_book, not
    decision_snapshot. Fail-on-revert for the marketable tautology — see
    test_default_fill_source_rejects_insufficient_depth_despite_observed_price
    below for the behavioral consequence this default controls."""
    monkeypatch.delenv("PAPER_FILL_SOURCE", raising=False)
    assert _resolve_paper_fill_source() == "rest_book"


def test_paper_fill_source_decision_snapshot_still_available_when_explicit(monkeypatch):
    """decision_snapshot remains a valid opt-in, just no longer the default."""
    monkeypatch.setenv("PAPER_FILL_SOURCE", "decision_snapshot")
    assert _resolve_paper_fill_source() == "decision_snapshot"


def test_unknown_paper_fill_source_falls_back_to_rest_book(monkeypatch):
    monkeypatch.setenv("PAPER_FILL_SOURCE", "garbage")
    assert _resolve_paper_fill_source() == "rest_book"


def test_default_fill_source_rejects_insufficient_depth_despite_observed_price():
    """Regression for the marketable tautology (execution.py:477-478 always
    passes observed_fill_price == price for both legs).

    Before the PAPER_FILL_SOURCE default was rest_book, submit_paper_order
    forwarded that observed_fill_price straight through whenever fill_source
    != "rest_book", so _paper_simulate_fill computed
    marketable = (fill_price <= price) with fill_price == price -- a
    tautology that can never be False. A fire against a book with less than
    the requested size available would still report a full fill.

    With the client constructed the way execution.py actually constructs it
    (no explicit _paper_fill_source override -- the real production default
    applies), the exact same call must now return None: the leg only has 4
    shares available within the limit but the order needs 10.
    """
    fake_settings = MagicMock()
    fake_settings.live = True
    tc = TradingClient.__new__(TradingClient)
    tc.s = fake_settings
    tc._paper_trade = True
    tc._paper_fill_source = _resolve_paper_fill_source()  # the real default, not hardcoded
    tc._paper_fill_details = {}
    tc._order_modes = {}
    tc._ensure_creds = MagicMock()
    tc._clob_client = MagicMock()
    tc._clob_client.get_order_book.return_value = {
        "asks": [{"price": "0.40", "size": "4"}],
    }

    order_id = asyncio.run(
        tc.submit_paper_order("yes-token", 0.40, 10.0, observed_fill_price=0.40)
    )

    assert order_id is None
    tc._clob_client.get_order_book.assert_called_once_with("yes-token")


def test_rest_pair_preflight_fetches_concurrently_and_rejects_moved_leg():
    fake_settings = MagicMock()
    fake_settings.live = True
    tc = TradingClient.__new__(TradingClient)
    tc.s = fake_settings
    tc._paper_trade = True
    tc._paper_fill_source = "rest_book"
    tc._ensure_creds = MagicMock()
    tc._clob_client = MagicMock()
    books = {
        "yes-token": {"asks": [{"price": "0.38", "size": "10"}]},
        # The WS decision limit was 0.32, but executable NO is now 0.75.
        "no-token": {"asks": [{"price": "0.75", "size": "10"}]},
    }
    tc._clob_client.get_order_book.side_effect = lambda token: books[token]

    report = asyncio.run(
        tc.paper_pair_preflight(
            "yes-token", "no-token", 0.66, 0.32, 10.0, 0.99
        )
    )

    assert report["applied"] is True
    assert report["passed"] is False
    assert report["reason"] == "rest_preflight_insufficient_liquidity"
    assert report["yes_vwap"] == pytest.approx(0.38)
    assert report["no_vwap"] is None
    assert tc._clob_client.get_order_book.call_count == 2


def test_protective_paper_exit_crosses_fresh_rest_bid_and_records_actual_price(tmp_path):
    from polymarket_arb.live_control import LiveTradingControl

    fake_settings = MagicMock()
    fake_settings.live = True
    tc = TradingClient.__new__(TradingClient)
    tc.s = fake_settings
    tc._paper_trade = True
    tc._paper_fill_source = "rest_book"
    tc._paper_fill_details = {}
    tc._order_modes = {}
    tc._live_control = LiveTradingControl(tmp_path / "control.json", tmp_path / "audit.jsonl")
    tc._ensure_creds = MagicMock()
    tc._clob_client = MagicMock()
    # A prior WS bid may have been 0.38; the current REST bid is now 0.37.
    tc._clob_client.get_order_book.return_value = {
        "bids": [
            {"price": "0.01", "size": "10"},
            {"price": "0.37", "size": "10"},
            {"price": "0.20", "size": "10"},
        ],
    }

    order_id = asyncio.run(tc.submit_protective_paper_exit("yes-token", 10.0))
    report = tc.get_fill_report(order_id)

    assert order_id is not None
    assert report["fill_price"] == pytest.approx(0.37)
    assert report["fill_source"] == "rest_book"
    assert report["limit_price"] == pytest.approx(0.0)
    tc._clob_client.get_order_book.assert_called_once_with("yes-token")
    tc._clob_client.create_and_post_order.assert_not_called()


def test_live_approval_setup_is_blocked_while_order_endpoint_is_unbuilt():
    """The product must not broadcast approval transactions for an unusable live path."""
    fake_settings = MagicMock()
    fake_settings.private_key = "ac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
    fake_settings.approval_max_gas_price_gwei = 80.0

    tc = TradingClient.__new__(TradingClient)
    tc.s = fake_settings
    tc._paper_trade = False
    tc._rpc_get_nonce = MagicMock(return_value=7)
    tc._rpc_get_gas_price = MagicMock(return_value=1_000_000_000)
    tc._rpc_send_raw_tx = MagicMock(return_value="0x" + "a" * 64)

    with patch("time.sleep", return_value=None):
        tc.setup_trading_approvals()

    tc._rpc_get_nonce.assert_not_called()
    tc._rpc_get_gas_price.assert_not_called()
    tc._rpc_send_raw_tx.assert_not_called()
