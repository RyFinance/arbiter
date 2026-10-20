"""Deterministic transport fail-safety regressions for rolling-window rotation."""
from __future__ import annotations

import asyncio
from contextlib import suppress
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from polymarket_arb.clients.clob_client import ClobBookFeed
from polymarket_arb.models import MarketState
from polymarket_arb.price_feed import PriceFeed


def _state(market_id: str, yes: str, no: str) -> MarketState:
    return MarketState(
        market_id=market_id,
        question=market_id,
        yes_token_id=yes,
        no_token_id=no,
    )


class _SendFailureSocket:
    def __init__(self):
        self.close_calls = []

    async def send(self, _payload):
        raise RuntimeError("send exploded")

    async def close(self, **kwargs):
        self.close_calls.append(kwargs)


@pytest.mark.asyncio
async def test_real_clob_resubscribe_failure_is_not_swallowed():
    """The real Clob layer must expose a failed live send to PriceFeed."""
    feed = ClobBookFeed(["old"], MagicMock())
    socket = _SendFailureSocket()
    feed._ws = socket
    feed.healthy = True

    with patch("polymarket_arb.clients.clob_client.log.error") as error_log:
        with pytest.raises(RuntimeError, match="send exploded"):
            await feed.resubscribe(["new-yes", "new-no"])

    assert feed.token_ids == ["new-yes", "new-no"]
    assert feed.healthy is False
    assert feed._ws is None
    assert socket.close_calls == [
        {"code": 1011, "reason": "resubscribe send failed"}
    ]
    error_log.assert_called_once()
    assert "REST fallback required" in error_log.call_args.args[0]


@pytest.mark.asyncio
async def test_unhealthy_rotation_retains_all_desired_tokens():
    old = _state("old", "old-yes", "old-no")
    new = _state("new", "new-yes", "new-no")
    price_feed = PriceFeed({old.market_id: old}, MagicMock())
    price_feed.build_token_index()
    clob_feed = ClobBookFeed(["old-yes", "old-no"], MagicMock())
    clob_feed.healthy = False
    price_feed._feed = clob_feed

    await price_feed.add_market(new)

    assert clob_feed.token_ids == [
        "old-yes",
        "old-no",
        "new-yes",
        "new-no",
    ]
    assert clob_feed.healthy is False
    assert price_feed._token_owner["new-yes"] is new
    assert price_feed._token_owner["new-no"] is new


class _StoppingSocket:
    def __init__(self):
        self.feed = None
        self.sent = []

    async def send(self, payload):
        self.sent.append(payload)

    def __aiter__(self):
        return self

    async def __anext__(self):
        self.feed.stop()
        raise StopAsyncIteration


class _SocketContext:
    def __init__(self, socket):
        self.socket = socket
        self.exited = False

    async def __aenter__(self):
        return self.socket

    async def __aexit__(self, _exc_type, _exc, _tb):
        self.exited = True


@pytest.mark.asyncio
async def test_run_clears_socket_when_connection_context_exits():
    feed = ClobBookFeed(["yes", "no"], MagicMock())
    socket = _StoppingSocket()
    socket.feed = feed
    context = _SocketContext(socket)

    with patch(
        "polymarket_arb.clients.clob_client.websockets.connect",
        return_value=context,
    ):
        await feed.run()

    assert context.exited is True
    assert len(socket.sent) == 2
    assert feed._ws is None
    assert feed.healthy is False


class _ReplacementFeed:
    instances = []

    def __init__(self, token_ids, _on_tick):
        self.token_ids = list(token_ids)
        self.healthy = False
        self.stopped = False
        self.started = asyncio.Event()
        type(self).instances.append(self)

    async def run(self):
        self.started.set()
        await asyncio.Event().wait()

    def stop(self):
        self.stopped = True


@pytest.mark.asyncio
async def test_add_market_restarts_completed_ws_task_and_feed():
    price_feed = PriceFeed({}, MagicMock())
    old_feed = MagicMock()

    async def completed_run():
        return None

    old_task = asyncio.create_task(completed_run())
    await old_task
    price_feed._feed = old_feed
    price_feed._ws_task = old_task
    _ReplacementFeed.instances.clear()

    settings = SimpleNamespace(use_ws=True)
    with patch("polymarket_arb.price_feed.SETTINGS", settings), patch(
        "polymarket_arb.price_feed.ClobBookFeed", _ReplacementFeed
    ):
        await price_feed.add_market(_state("new", "new-yes", "new-no"))
        await asyncio.wait_for(price_feed._feed.started.wait(), timeout=1.0)

    replacement = price_feed._feed
    assert replacement is _ReplacementFeed.instances[0]
    assert replacement.token_ids == ["new-yes", "new-no"]
    assert price_feed._ws_task is not old_task
    assert not price_feed._ws_task.done()
    old_feed.stop.assert_called_once_with()

    replacement.stop()
    price_feed._ws_task.cancel()
    with suppress(asyncio.CancelledError):
        await price_feed._ws_task


class _FailingRotationFeed:
    def __init__(self):
        self.healthy = True
        self.desired = []

    async def resubscribe(self, token_ids):
        self.desired = list(token_ids)
        raise RuntimeError("rotation send failed")


@pytest.mark.asyncio
async def test_resubscribe_failure_switches_to_rest_for_rotated_tokens():
    updates = []
    price_feed = None

    def on_update(state):
        updates.append((state.best_yes_ask, state.best_no_ask))
        if len(updates) == 2:
            price_feed._stop = True

    price_feed = PriceFeed({}, on_update)
    failing_feed = _FailingRotationFeed()
    price_feed._feed = failing_feed
    price_feed._session = object()
    state = _state("new", "new-yes", "new-no")

    async def fake_fetch(_session, token_id):
        return {
            "new-yes": (0.41, 12.0),
            "new-no": (0.57, 14.0),
        }[token_id]

    settings = SimpleNamespace(
        use_ws=True,
        poll_interval_s=0.0,
        stale_book_timeout_s=5.0,
    )
    with patch("polymarket_arb.price_feed.SETTINGS", settings), patch(
        "polymarket_arb.price_feed.fetch_book", side_effect=fake_fetch
    ):
        await price_feed.add_market(state)
        assert failing_feed.healthy is False
        assert failing_feed.desired == ["new-yes", "new-no"]
        assert price_feed._ws_down_since is not None
        await asyncio.wait_for(price_feed._poll_loop(), timeout=1.0)

    assert state.best_yes_ask == 0.41
    assert state.best_no_ask == 0.57
    assert state.yes_size == 12.0
    assert state.no_size == 14.0
    assert len(updates) == 2
