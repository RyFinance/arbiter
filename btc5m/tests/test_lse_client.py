"""Tests for the LSE (London Strategic Edge) WebSocket client.

Covers the fixes in TASK A:
  1. DNS endpoint fix — client now targets wss://ws.londonstrategicedge.com
     (the old stream.lse host does not resolve).
  2. websockets>=12 parameter rename — ``additional_headers`` instead of
     the removed ``extra_headers``.
  3. Placeholder API key detection — a loud warning is logged when the
     sentinel key from config.py is in use.
  4. Tick parsing — the client correctly parses a ``{"type":"tick",...}``
     message and forwards it to the on_tick callback.

The test mocks the websockets connection so it runs entirely offline,
independent of whether real LSE credentials are configured.
"""
from __future__ import annotations

import asyncio
import io
import json
import sys
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from polymarket_arb.clients.lse_client import (
    LSEClient,
    LSE_WS_URL,
    _LSE_PLACEHOLDER_KEY,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class MockWS:
    """Minimal async context manager + iterator that mimics a websockets client.

    ``messages`` is a list of raw strings the server will "send" (iterated
    via ``async for raw in ws``).  ``sent`` captures everything the client
    writes with ``ws.send()`` so tests can assert on protocol messages.
    """

    def __init__(self, messages: List[str] | None = None):
        self._messages: List[str] = list(messages or [])
        self.sent: List[str] = []
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.closed = True
        return False

    async def send(self, data: str) -> None:
        self.sent.append(data)

    async def close(self) -> None:
        self.closed = True

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        if not self._messages:
            raise StopAsyncIteration
        return self._messages.pop(0)


class _DummyConnect:
    """Wraps a MockWS so it can be used as the return value of a patched
    ``websockets.connect`` call.

    ``websockets.connect(url, **kw)`` returns an object that supports
    ``async with`` — i.e. it is an *asynchronous context manager* (but NOT
    a coroutine).  The real ``connect`` class satisfies this; our fake
    just delegates to MockWS for the context-manager protocol.
    """

    def __init__(self, mock_ws: MockWS):
        self._mock_ws = mock_ws

    def __call__(self, url: str, **kwargs):
        self.url = url
        self.kwargs = kwargs
        return self._mock_ws  # MockWS already implements __aenter__/__aexit__


# ---------------------------------------------------------------------------
# DNS / endpoint fix (TASK A #1)
# ---------------------------------------------------------------------------

def test_ws_url_is_correct_host():
    """The WS URL must point to ws.lse (not the dead stream.lse host)."""
    assert LSE_WS_URL == "wss://ws.londonstrategicedge.com"
    assert "stream.londonstrategicedge.com" not in LSE_WS_URL


# ---------------------------------------------------------------------------
# Placeholder API key detection (TASK A #3)
# ---------------------------------------------------------------------------

def _capture_log_messages(caplog_or_stdin=None) -> tuple:
    """Returns (captured_lines, restore_cb).

    The project uses a custom BotLogger that prints to stdout (not the
    stdlib logging module), so pytest's caplog fixture won't capture it.
    We temporarily replace sys.stdout to intercept the output.
    """
    buf = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = buf

    def restore():
        sys.stdout = old_stdout

    return buf, restore


def test_placeholder_key_warning_logged():
    """Constructing with the sentinel key produces a loud warning on stdout."""
    buf, restore = _capture_log_messages()
    try:
        client = LSEClient(api_key=_LSE_PLACEHOLDER_KEY)
    finally:
        restore()

    output = buf.getvalue()
    assert "placeholder" in output.lower(), f"expected 'placeholder' in log output, got: {output!r}"
    assert _LSE_PLACEHOLDER_KEY in output
    # The client stores the key as-is — we never silently replace it.
    assert client.api_key == _LSE_PLACEHOLDER_KEY


def test_real_key_does_not_warn():
    """A non-placeholder key should not trigger the placeholder warning."""
    buf, restore = _capture_log_messages()
    try:
        client = LSEClient(api_key="lse_live_a_real_key_12345")
    finally:
        restore()

    output = buf.getvalue()
    assert "placeholder" not in output.lower()


# ---------------------------------------------------------------------------
# Tick parsing (TASK A #4 — pure unit test of _handle_ws_message)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_handle_ws_message_parses_tick():
    """A well-formed tick message is dispatched to on_tick with correct fields."""
    received: List[tuple] = []

    def on_tick(symbol: str, data: Dict[str, Any]) -> None:
        received.append((symbol, data))

    client = LSEClient(api_key="lse_live_real")
    client._ws_callback = on_tick

    tick_msg = json.dumps({
        "type": "tick",
        "symbol": "BTC/USD",
        "price": 67123.50,
        "bid": 67122.00,
        "ask": 67125.00,
        "ts": 1780319100,
    })
    await client._handle_ws_message(tick_msg)

    assert len(received) == 1
    symbol, data = received[0]
    assert symbol == "BTC/USD"
    assert data["price"] == 67123.50
    assert data["bid"] == 67122.00
    assert data["ask"] == 67125.00
    assert data["ts"] == 1780319100


@pytest.mark.asyncio
async def test_handle_ws_message_ignores_non_tick():
    """Non-tick messages (welcome, heartbeat, auth) do not trigger the callback."""
    received: List[tuple] = []
    client = LSEClient(api_key="lse_live_real")
    client._ws_callback = lambda s, d: received.append((s, d))

    for msg in [
        '{"type":"welcome","max_symbols":16}',
        '{"type":"subscribed","symbol":"BTC/USD","count":1}',
        '{"type":"heartbeat"}',
        '{"type":"auth","status":"ok"}',
        '{"type":"error","code":"INVALID_SYMBOL"}',
    ]:
        await client._handle_ws_message(msg)

    assert received == []


@pytest.mark.asyncio
async def test_handle_ws_message_invalid_json_ignored():
    """Malformed JSON does not raise and produces no callback."""
    received: List = []
    client = LSEClient(api_key="lse_live_real")
    client._ws_callback = lambda s, d: received.append((s, d))

    await client._handle_ws_message("not json {{{")
    assert received == []


@pytest.mark.asyncio
async def test_handle_ws_message_bytes_decoded():
    """Byte payloads are decoded before parsing."""
    received: List[tuple] = []
    client = LSEClient(api_key="lse_live_real")
    client._ws_callback = lambda s, d: received.append((s, d))

    tick_msg = json.dumps({"type": "tick", "symbol": "ETH/USD", "price": 3000})
    await client._handle_ws_message(tick_msg.encode("utf-8"))

    assert len(received) == 1
    assert received[0][0] == "ETH/USD"


# ---------------------------------------------------------------------------
# Full WS loop integration with mocked websockets
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ws_loop_connects_and_subscribes_and_dispatches_tick():
    """The WS loop connects to the correct URL with additional_headers,
    sends an auth message, subscribes per-symbol, and dispatches ticks.

    This also verifies that ``additional_headers`` (not the removed
    ``extra_headers``) is accepted by the installed websockets version —
    a TypeError would be raised if the wrong parameter name is used.
    """
    sample_tick = json.dumps({
        "type": "tick",
        "symbol": "BTC/USD",
        "price": 67000.0,
        "bid": 66999.0,
        "ask": 67001.0,
        "ts": 1780319200,
    })
    mock_ws = MockWS(messages=[sample_tick])

    captured: List[tuple] = []

    client = LSEClient(api_key="lse_live_real_test")
    # Use a closure that captures `client` after it's created.
    def on_tick(symbol: str, data: Dict[str, Any]) -> None:
        captured.append((symbol, data))
        # Stop the loop after the first tick so the test doesn't hang.
        client._stop = True
    client._ws_callback = on_tick
    client._ws_symbols = ["BTC/USD"]  # set directly; normally done by start_ws()

    # _DummyConnect records the URL and kwargs and returns MockWS.
    connect_wrapper = _DummyConnect(mock_ws)

    with patch("websockets.connect", connect_wrapper):
        ws_task = asyncio.create_task(client._ws_loop())
        try:
            await asyncio.wait_for(ws_task, timeout=2.0)
        except asyncio.TimeoutError:
            ws_task.cancel()

    # --- Verify the correct URL was used ---
    assert connect_wrapper.url == LSE_WS_URL

    # --- Fix #2: verify additional_headers (not extra_headers) was passed ---
    assert "additional_headers" in connect_wrapper.kwargs, (
        "websockets.connect must be called with 'additional_headers', "
        "not 'extra_headers' (renamed in websockets >= 12)"
    )
    assert "extra_headers" not in connect_wrapper.kwargs, (
        "'extra_headers' is not a valid parameter in websockets >= 12; "
        "use 'additional_headers' instead"
    )

    # --- Verify auth message was sent ---
    auth_msgs = [json.loads(m) for m in mock_ws.sent if '"auth"' in m]
    assert len(auth_msgs) == 1, f"expected 1 auth message, got sent={mock_ws.sent}"
    assert auth_msgs[0]["action"] == "auth"
    assert auth_msgs[0]["api_key"] == "lse_live_real_test"

    # --- Verify subscribe message was sent ---
    sub_msgs = [json.loads(m) for m in mock_ws.sent if '"subscribe"' in m]
    assert len(sub_msgs) == 1, f"expected 1 subscribe message, got sent={mock_ws.sent}"
    assert sub_msgs[0]["action"] == "subscribe"
    assert sub_msgs[0]["symbol"] == "BTC/USD"

    # --- Verify the tick was dispatched ---
    assert len(captured) == 1
    symbol, data = captured[0]
    assert symbol == "BTC/USD"
    assert data["price"] == 67000.0
    assert data["bid"] == 66999.0
    assert data["ask"] == 67001.0
    assert data["ts"] == 1780319200


@pytest.mark.asyncio
async def test_ws_loop_with_placeholder_key_logs_warning_and_no_auth():
    """When the placeholder key is used, the WS loop logs a warning and
    does NOT send an auth message (it skips auth rather than sending a
    bogus key)."""
    mock_ws = MockWS(messages=[])  # no messages — loop exits via StopAsyncIteration

    client = LSEClient(api_key=_LSE_PLACEHOLDER_KEY)
    client._ws_callback = lambda s, d: None
    client._ws_symbols = ["BTC/USD"]

    connect_calls = {"n": 0}

    def fake_connect(url, **kwargs):
        assert "additional_headers" in kwargs
        connect_calls["n"] += 1
        # Stop after the first connect attempt so the loop exits cleanly
        # (the mock WS has no messages, so we don't want reconnection loops).
        if connect_calls["n"] == 1:
            client._stop = True
        return mock_ws

    buf, restore_stdout = _capture_log_messages()
    try:
        with patch("websockets.connect", fake_connect):
            ws_task = asyncio.create_task(client._ws_loop())
            try:
                await asyncio.wait_for(ws_task, timeout=3.0)
            except asyncio.TimeoutError:
                ws_task.cancel()
                try:
                    await ws_task
                except asyncio.CancelledError:
                    pass
    finally:
        restore_stdout()

    output = buf.getvalue()

    # A placeholder warning should have been logged during construction.
    assert "placeholder" in output.lower(), (
        f"expected placeholder warning in log output, got: {output!r}"
    )

    # No auth message should be sent with the placeholder key.
    auth_msgs = [m for m in mock_ws.sent if '"auth"' in m]
    assert len(auth_msgs) == 0
