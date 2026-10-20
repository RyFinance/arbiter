"""TASK E2 — Tests for operational alerting hooks.

Validates that the three required alert conditions fire correctly:
  1. Kill-switch triggers (PARTIAL_LOSS_CAP_USD breach + risk.halt)
  2. Repeated order rejections (3+ consecutive submit_order returning None)
  3. WS disconnect lasting >30s (PriceFeed severe disconnect alert)

Each test uses a real WebhookAlerter with a mocked _send coroutine so we can
assert the alert kind/message WITHOUT making a network call. All alerts are
also written to alerts.jsonl via the real _write_alert_record path.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from polymarket_arb.alerting import (
    ALERT_KILL_SWITCH,
    ALERT_REPEATED_REJECTIONS,
    ALERT_WS_DISCONNECT_SEVERE,
    WebhookAlerter,
)
from polymarket_arb.config import SETTINGS
from polymarket_arb.clients.clob_client import ClobBookFeed
from polymarket_arb.execution import Execution
from polymarket_arb.metrics import Metrics
from polymarket_arb.models import MarketState
from polymarket_arb.price_feed import PriceFeed, WS_DISCONNECT_SEVERE_THRESHOLD_S
from polymarket_arb.risk import RiskState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _CapturingAlerter(WebhookAlerter):
    """Alerter that records every alert() call AND writes to alerts.jsonl."""

    def __init__(self, webhook_url: str = "http://test-webhook.example.com/hooks/arb"):
        super().__init__(webhook_url=webhook_url, dedup_window_s=0.0)
        self.calls: list[tuple] = []  # (kind, message, fields)

    async def _send(self, kind: str, message: str, fields: dict):
        # Capture instead of POSTing.
        self.calls.append((kind, message, fields))


def _read_alerts_jsonl(alerts_path: Path) -> list[dict]:
    """Read every record from the given alerts.jsonl path."""
    if not alerts_path.exists():
        return []
    out = []
    with alerts_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def _make_market_state(**kw) -> MarketState:
    """Build a MarketState with sensible defaults for execution tests."""
    defaults = dict(
        market_id="m-test", question="Test Q",
        yes_token_id="0xYES1234", no_token_id="0xNO1234",
        best_yes_ask=0.48, best_no_ask=0.50,
        yes_size=100.0, no_size=100.0,
        best_yes_bid=0.47, best_no_bid=0.49,
        last_update_ts=time.time(), yes_ts=time.time(), no_ts=time.time(),
    )
    defaults.update(kw)
    return MarketState(**defaults)


class _FakeClient:
    """Minimal stand-in for TradingClient that always rejects orders."""

    async def submit_order(self, *a, **kw):
        return None  # reject

    async def cancel_order(self, *a, **kw):
        return True

    async def is_filled(self, *a, **kw):
        return True


class _SuccessThenFailClient:
    """Returns None on first 2 submit_order calls, then returns an id."""

    def __init__(self):
        self.calls = 0

    async def submit_order(self, *a, **kw):
        self.calls += 1
        if self.calls <= 2:
            return None
        return f"order-{self.calls}"

    async def cancel_order(self, *a, **kw):
        return True

    async def is_filled(self, *a, **kw):
        return True


# ---------------------------------------------------------------------------
# Paper-only transport fault injection
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_force_paper_disconnect_closes_transport_and_blocks_nonpaper():
    """The session harness may exercise a real WS outage only in PAPER_TRADE."""
    feed = ClobBookFeed(["token"], MagicMock())
    ws = MagicMock()
    ws.close = AsyncMock()
    feed._ws = ws
    feed.healthy = True

    with patch("polymarket_arb.clients.clob_client.SETTINGS",
               SimpleNamespace(paper_trade=True)):
        started = time.monotonic()
        await feed.force_paper_disconnect(31.0)

    ws.close.assert_awaited_once()
    assert feed.healthy is False
    assert feed._forced_disconnect_until >= started + 30.0

    with patch("polymarket_arb.clients.clob_client.SETTINGS",
               SimpleNamespace(paper_trade=False)):
        with pytest.raises(RuntimeError, match="PAPER_TRADE-only"):
            await feed.force_paper_disconnect(1.0)


# ---------------------------------------------------------------------------
# 1. Kill-switch trigger (PARTIAL_LOSS_CAP_USD breach)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_kill_switch_on_partial_loss_cap_breach(tmp_path):
    """Execution._after_residual fires ALERT_KILL_SWITCH when residual loss
    breaches PARTIAL_LOSS_CAP_USD, and halts risk."""
    alerts_path = tmp_path / "alerts.jsonl"
    alerter = _CapturingAlerter()
    risk = RiskState(balance=100_000.0)
    metrics = Metrics()
    client = _FakeClient()
    exec_ = Execution(client=client, risk=risk, metrics=metrics, alerter=alerter)

    st = _make_market_state()
    cap = SETTINGS.partial_loss_cap_usd
    residual = -(cap + 10.0)  # exceeds cap

    # Patch the alerts log path so the test writes to tmp_path
    with patch("polymarket_arb.alerting.ALERTS_LOG_FILE", str(alerts_path)):
        exec_._after_residual(
            st=st, combined=0.96, size=10.0,
            residual=residual, age_ms=10.0, band="fresh", action="exit",
            released_cost=9.8,
        )
        # Let the scheduled _send task run
        await asyncio.sleep(0.05)

    kill_alerts = [a for a in alerter.calls if a[0] == ALERT_KILL_SWITCH]
    assert len(kill_alerts) == 1, \
        f"expected 1 kill_switch alert, got {len(kill_alerts)}"
    assert "PARTIAL_LOSS_CAP" in kill_alerts[0][1]
    assert risk.halted is True

    # Also verify it was written to alerts.jsonl
    records = _read_alerts_jsonl(alerts_path)
    kill_records = [r for r in records if r.get("kind") == ALERT_KILL_SWITCH]
    assert len(kill_records) == 1
    assert kill_records[0]["message"] == kill_alerts[0][1]


@pytest.mark.asyncio
async def test_kill_switch_via_risk_halt(tmp_path):
    """risk.halt() itself fires the kill-switch alert (source-of-truth path)."""
    alerts_path = tmp_path / "alerts.jsonl"
    alerter = _CapturingAlerter()
    risk = RiskState(balance=100_000.0, alerter=alerter)

    with patch("polymarket_arb.alerting.ALERTS_LOG_FILE", str(alerts_path)):
        risk.halt("manual test halt")
        await asyncio.sleep(0.05)

    kill_alerts = [a for a in alerter.calls if a[0] == ALERT_KILL_SWITCH]
    assert len(kill_alerts) == 1
    assert "manual test halt" in kill_alerts[0][1]
    assert risk.halted is True

    records = _read_alerts_jsonl(alerts_path)
    kill_records = [r for r in records if r.get("kind") == ALERT_KILL_SWITCH]
    assert len(kill_records) == 1


# ---------------------------------------------------------------------------
# 2. Repeated order rejections (3+ consecutive)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_repeated_rejections_alert_at_three_in_a_row(tmp_path):
    """Execution.execute_dual_buy tracks consecutive submit_order None returns.
    At 3+ in a row, fires ALERT_REPEATED_REJECTIONS."""
    alerts_path = tmp_path / "alerts.jsonl"
    alerter = _CapturingAlerter()
    risk = RiskState(balance=100_000.0)
    metrics = Metrics()
    client = _FakeClient()
    exec_ = Execution(client=client, risk=risk, metrics=metrics, alerter=alerter)

    st = _make_market_state()
    exec_.register(st, size=10.0)

    with patch("polymarket_arb.alerting.ALERTS_LOG_FILE", str(alerts_path)):
        for _ in range(3):
            await exec_.execute_dual_buy(
                st=st, combined=0.96, decision_ts=time.monotonic(),
                age_ms=10.0, band="fresh", yes_p=0.47, no_p=0.49,
            )
        await asyncio.sleep(0.05)

    rej_alerts = [a for a in alerter.calls if a[0] == ALERT_REPEATED_REJECTIONS]
    assert len(rej_alerts) == 1, \
        f"expected 1 repeated_rejections alert after 3 fires, got {len(rej_alerts)}"
    assert rej_alerts[0][2]["consecutive_rejections"] == 3

    records = _read_alerts_jsonl(alerts_path)
    rej_records = [r for r in records if r.get("kind") == ALERT_REPEATED_REJECTIONS]
    assert len(rej_records) == 1


@pytest.mark.asyncio
async def test_repeated_rejections_reset_on_success(tmp_path):
    """A successful submit resets the consecutive rejection counter."""
    alerts_path = tmp_path / "alerts.jsonl"
    alerter = _CapturingAlerter()
    risk = RiskState(balance=100_000.0)
    metrics = Metrics()
    client = _SuccessThenFailClient()
    exec_ = Execution(client=client, risk=risk, metrics=metrics, alerter=alerter)

    st = _make_market_state()
    exec_.register(st, size=10.0)

    with patch("polymarket_arb.alerting.ALERTS_LOG_FILE", str(alerts_path)):
        # 2 rejections
        for _ in range(2):
            await exec_.execute_dual_buy(
                st=st, combined=0.96, decision_ts=time.monotonic(),
                age_ms=10.0, band="fresh", yes_p=0.47, no_p=0.49,
            )
        # Success (both legs return ids)
        await exec_.execute_dual_buy(
            st=st, combined=0.96, decision_ts=time.monotonic(),
            age_ms=10.0, band="fresh", yes_p=0.47, no_p=0.49,
        )
        # 2 more rejections — counter should have reset, no alert
        for _ in range(2):
            await exec_.execute_dual_buy(
                st=st, combined=0.96, decision_ts=time.monotonic(),
                age_ms=10.0, band="fresh", yes_p=0.47, no_p=0.49,
            )
        await asyncio.sleep(0.05)

    rej_alerts = [a for a in alerter.calls if a[0] == ALERT_REPEATED_REJECTIONS]
    assert len(rej_alerts) == 0, "counter should have reset on success"


# ---------------------------------------------------------------------------
# 3. WS disconnect >30s
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ws_disconnect_severe_alert_after_30s(tmp_path):
    """PriceFeed fires ALERT_WS_DISCONNECT_SEVERE when WS is down >30s."""
    alerts_path = tmp_path / "alerts.jsonl"
    alerter = _CapturingAlerter()

    states: dict = {}
    pf = PriceFeed(states=states, on_update=MagicMock(), alerter=alerter)

    # Simulate WS going down: feed unhealthy, down-since set 35s ago.
    fake_feed = MagicMock()
    fake_feed.healthy = False
    pf._feed = fake_feed
    pf._ws_down_since = time.monotonic() - 35.0  # 35s ago

    with patch("polymarket_arb.alerting.ALERTS_LOG_FILE", str(alerts_path)):
        with patch.object(pf, "_all_token_ids", return_value=["0xTOKEN1"]):
            with patch("polymarket_arb.price_feed.fetch_book",
                       new_callable=AsyncMock, return_value=(None, None)):
                task = asyncio.create_task(pf._poll_loop())
                await asyncio.sleep(0.1)
                pf._stop = True
                await task
            await asyncio.sleep(0.05)  # let _send task run

    ws_alerts = [a for a in alerter.calls if a[0] == ALERT_WS_DISCONNECT_SEVERE]
    assert len(ws_alerts) >= 1, \
        f"expected ws_disconnect_severe alert, got {len(ws_alerts)}"
    assert ws_alerts[-1][2]["down_seconds"] >= 30.0
    assert ws_alerts[-1][2]["threshold_seconds"] == WS_DISCONNECT_SEVERE_THRESHOLD_S

    records = _read_alerts_jsonl(alerts_path)
    ws_records = [r for r in records if r.get("kind") == ALERT_WS_DISCONNECT_SEVERE]
    assert len(ws_records) >= 1


@pytest.mark.asyncio
async def test_ws_disconnect_no_alert_when_ws_healthy(tmp_path):
    """No alert when WS is healthy."""
    alerts_path = tmp_path / "alerts.jsonl"
    alerter = _CapturingAlerter()

    states: dict = {}
    pf = PriceFeed(states=states, on_update=MagicMock(), alerter=alerter)

    fake_feed = MagicMock()
    fake_feed.healthy = True
    pf._feed = fake_feed
    pf._ws_down_since = None

    with patch("polymarket_arb.alerting.ALERTS_LOG_FILE", str(alerts_path)):
        with patch.object(pf, "_all_token_ids", return_value=["0xTOKEN1"]):
            with patch("polymarket_arb.price_feed.fetch_book",
                       new_callable=AsyncMock, return_value=(0.5, 10.0)):
                task = asyncio.create_task(pf._poll_loop())
                await asyncio.sleep(0.1)
                pf._stop = True
                await task
            await asyncio.sleep(0.05)

    ws_alerts = [a for a in alerter.calls if a[0] == ALERT_WS_DISCONNECT_SEVERE]
    assert len(ws_alerts) == 0, "no alert should fire when WS is healthy"


@pytest.mark.asyncio
async def test_ws_disconnect_no_severe_alert_before_30s(tmp_path):
    """No severe alert when WS is down but <30s."""
    alerts_path = tmp_path / "alerts.jsonl"
    alerter = _CapturingAlerter()

    states: dict = {}
    pf = PriceFeed(states=states, on_update=MagicMock(), alerter=alerter)

    fake_feed = MagicMock()
    fake_feed.healthy = False
    pf._feed = fake_feed
    pf._ws_down_since = time.monotonic() - 5.0  # only 5s down

    with patch("polymarket_arb.alerting.ALERTS_LOG_FILE", str(alerts_path)):
        with patch.object(pf, "_all_token_ids", return_value=["0xTOKEN1"]):
            with patch("polymarket_arb.price_feed.fetch_book",
                       new_callable=AsyncMock, return_value=(None, None)):
                task = asyncio.create_task(pf._poll_loop())
                await asyncio.sleep(0.1)
                pf._stop = True
                await task
            await asyncio.sleep(0.05)

    ws_alerts = [a for a in alerter.calls if a[0] == ALERT_WS_DISCONNECT_SEVERE]
    assert len(ws_alerts) == 0, "no severe alert should fire before 30s"
