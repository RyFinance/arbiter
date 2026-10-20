from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "capture_depth_ws.py"


def _module():
    spec = importlib.util.spec_from_file_location("capture_depth_ws_rollover_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_busy_websocket_checks_rollover_before_next_receive():
    module = _module()
    capture = module.DepthCapture(token_ids=["yes", "no"], writer=MagicMock())
    capture._current_window_ts = 1_000
    capture._is_window_rollover = MagicMock(side_effect=[False, False, True])
    capture._maybe_prepare_next_window = AsyncMock()
    capture._handle = MagicMock()
    ws = MagicMock()
    ws.recv = AsyncMock(return_value='{"asset_id":"yes","bids":[[0.4,1]]}')

    asyncio.run(capture._run_inner(ws))

    assert ws.recv.await_count == 1
    capture._handle.assert_called_once()
    assert capture._is_window_rollover.call_count == 3


def test_next_window_is_pre_resolved_inside_last_30_seconds():
    module = _module()
    capture = module.DepthCapture(token_ids=["yes", "no"], writer=MagicMock())
    capture._current_window_ts = 1_000
    resolved = ["next-yes", "next-no"]
    with patch.object(module, "resolve_window_tokens", new=AsyncMock(return_value=resolved)) as call:
        asyncio.run(capture._maybe_prepare_next_window(now=1_271.0))

    call.assert_awaited_once_with(1_300)
    assert capture._prepared_window_ts == 1_300
    assert capture._prepared_tokens == resolved


def test_pre_resolve_does_not_run_too_early_or_repeat_success():
    module = _module()
    capture = module.DepthCapture(token_ids=["yes", "no"], writer=MagicMock())
    capture._current_window_ts = 1_000
    resolver = AsyncMock(return_value=["next-yes", "next-no"])
    with patch.object(module, "resolve_window_tokens", new=resolver):
        asyncio.run(capture._maybe_prepare_next_window(now=1_269.0))
        assert resolver.await_count == 0
        asyncio.run(capture._maybe_prepare_next_window(now=1_271.0))
        asyncio.run(capture._maybe_prepare_next_window(now=1_280.0))
    assert resolver.await_count == 1


def test_rollover_receive_poll_is_bounded_to_one_second():
    module = _module()
    assert 0 < module.WINDOW_ROLLOVER_POLL_S <= 1.0
