"""TASK B — Tests for the alerting module.

Validates:
  * WebhookAlerter is a no-op when the URL is empty (disabled, never required).
  * WebhookAlerter.logs locally even when disabled (durable record).
  * WebhookAlerter de-duplicates by kind within the dedup window.
  * Heartbeat writes/reads timestamps correctly.
  * Heartbeat.is_stale / read_age behave correctly.
  * Execution fires ALERT_KILL_SWITCH when PARTIAL_LOSS_CAP_USD is breached.
  * Build helpers return no-op instances when alerting is disabled.
"""
from __future__ import annotations

import os
import time

import pytest
from unittest.mock import patch, MagicMock, AsyncMock

from polymarket_arb.alerting import (
    WebhookAlerter,
    Heartbeat,
    build_alerter,
    build_heartbeat,
    ALERT_BOT_CRASH,
    ALERT_EMERGENCY_STOP,
    ALERT_KILL_SWITCH,
    ALERT_WS_DISCONNECT,
)


def _make_alerter_with_url(url="http://example.com/webhook"):
    return WebhookAlerter(webhook_url=url, dedup_window_s=0.5)


# ---------------------------------------------------------------------------
# WebhookAlerter — disabled (empty URL)
# ---------------------------------------------------------------------------
def test_alerter_disabled_when_url_empty():
    """When the URL is empty, the alerter is a no-op (enabled=False)."""
    alerter = WebhookAlerter(webhook_url="")
    assert alerter.enabled is False
    # Calling alert should not raise and should not attempt any network activity.
    alerter.alert("bot_crash", "test message")
    assert alerter.enabled is False


def test_alerter_disabled_via_config():
    """build_alerter() with empty ALERT_WEBHOOK_URL returns a disabled alerter."""
    with patch("polymarket_arb.alerting.SETTINGS") as mock_settings:
        mock_settings.alert_webhook_url = ""
        alerter = build_alerter()
        assert alerter.enabled is False


# ---------------------------------------------------------------------------
# WebhookAlerter — enabled (mocked httpx)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_alerter_sends_when_enabled():
    """When enabled, alert() schedules a webhook POST via the running loop."""
    alerter = _make_alerter_with_url()
    assert alerter.enabled is True

    # Patch _send so we can observe the call without real network.
    sent = []

    async def fake_send(kind, msg, fields):
        sent.append((kind, msg, fields))

    with patch.object(alerter, "_send", new=fake_send):
        # alert() calls log.critical first (always), then schedules _send.
        alerter.alert("bot_crash", "test crash", detail="something")

        # Let the scheduled task run.
        import asyncio
        await asyncio.sleep(0.1)

    assert len(sent) == 1
    assert sent[0][0] == "bot_crash"
    assert sent[0][1] == "test crash"
    assert sent[0][2]["detail"] == "something"


def test_alerter_logs_locally_even_when_disabled():
    """alert() always logs via log.critical regardless of webhook status."""
    alerter = WebhookAlerter(webhook_url="")
    with patch("polymarket_arb.alerting.log") as mock_log:
        alerter.alert("kill_switch", "cap breached", residual=-30.0)
        mock_log.critical.assert_called_once()
        assert "kill_switch" in mock_log.critical.call_args[0][0]


def test_alerter_uses_current_default_alert_log_path(tmp_path):
    """Default-targeted alert persistence honors a patched log path."""
    alerts_path = tmp_path / "alerts.jsonl"
    alerter = WebhookAlerter(webhook_url="")

    with patch("polymarket_arb.alerting.ALERTS_LOG_FILE", str(alerts_path)):
        alerter.alert("kill_switch", "cap breached")

    assert alerts_path.exists()
    assert '"kind": "kill_switch"' in alerts_path.read_text(encoding="utf-8")


def test_alerter_preserves_explicit_alert_archive_path(tmp_path):
    """An explicit archive target overrides the current default log path."""
    alerts_path = tmp_path / "alerts.jsonl"
    archive_path = tmp_path / "archive" / "alerts.jsonl"

    with patch("polymarket_arb.alerting.ALERTS_LOG_FILE", str(alerts_path)):
        alerter = WebhookAlerter(webhook_url="", alerts_path=str(archive_path))
        alerter.alert("kill_switch", "cap breached")

    assert archive_path.exists()
    assert not alerts_path.exists()


def test_alerter_fsyncs_session_record_and_operational_mirror(tmp_path):
    session_path = tmp_path / "session" / "alerts.jsonl"
    operational_path = tmp_path / "state" / "alerts.jsonl"
    alerter = WebhookAlerter(
        webhook_url="",
        alerts_path=str(session_path),
        mirror_alerts_path=str(operational_path),
        session_id="paper-123",
    )

    alerter.alert("kill_switch", "halted", reason="test")

    session_row = __import__("json").loads(session_path.read_text())
    operational_row = __import__("json").loads(operational_path.read_text())
    assert session_row == operational_row
    assert session_row["session_id"] == "paper-123"
    assert session_row["kind"] == "kill_switch"


# ---------------------------------------------------------------------------
# WebhookAlerter — de-duplication
# ---------------------------------------------------------------------------
def test_alerter_dedup_blocks_repeat_within_window():
    """The same kind within dedup_window_s should not fire again."""
    alerter = _make_alerter_with_url()
    assert alerter._should_fire("kill_switch") is True
    alerter._mark("kill_switch")
    assert alerter._should_fire("kill_switch") is False  # within window
    # Different kind fires independently.
    assert alerter._should_fire("ws_disconnect") is True


def test_alerter_dedup_allows_after_window():
    """After dedup_window_s elapses, the same kind can fire again."""
    alerter = WebhookAlerter(webhook_url="http://x", dedup_window_s=0.1)
    assert alerter._should_fire("x") is True
    alerter._mark("x")
    assert alerter._should_fire("x") is False
    time.sleep(0.15)
    assert alerter._should_fire("x") is True


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------
def test_heartbeat_disabled_when_not_configured():
    """Heartbeat with enabled=False never writes a file."""
    hb = Heartbeat(path="/tmp/should_not_exist_xyz", enabled=False)
    hb.touch()
    assert not os.path.exists("/tmp/should_not_exist_xyz")
    assert hb.is_stale(5.0) is False  # disabled => is_stale always False


def test_heartbeat_writes_and_reads_timestamp(tmp_path):
    """touch() writes a timestamp; read_timestamp() reads it back."""
    hb_file = str(tmp_path / "hb")
    hb = Heartbeat(path=hb_file, enabled=True)
    hb.touch()
    ts = Heartbeat.read_timestamp(hb_file)
    assert ts is not None
    assert abs(ts - time.time()) < 2.0  # written ~now


def test_heartbeat_age_grows_over_time(tmp_path):
    """read_age() returns a positive, increasing value as time passes."""
    hb_file = str(tmp_path / "hb")
    hb = Heartbeat(path=hb_file, enabled=True)
    hb.touch()
    age1 = Heartbeat.read_age(hb_file)
    time.sleep(0.2)
    age2 = Heartbeat.read_age(hb_file)
    assert age2 > age1 > 0
    assert age1 < 1.0
    assert age2 < 1.0


def test_heartbeat_read_age_inf_when_missing(tmp_path):
    """read_age() returns inf for a missing file."""
    missing = str(tmp_path / "does_not_exist")
    assert Heartbeat.read_age(missing) == float("inf")
    assert Heartbeat.read_timestamp(missing) is None


def test_heartbeat_is_stale_when_timeout_exceeded(tmp_path):
    """is_stale() returns True when the heartbeat is older than the timeout."""
    hb_file = str(tmp_path / "hb")
    hb = Heartbeat(path=hb_file, enabled=True)
    old = time.time() - 999.0
    with open(hb_file, "w") as f:
        f.write(f"{old:.3f}")
    os.utime(hb_file, (old, old))
    assert hb.is_stale(5.0) is True


def test_heartbeat_not_stale_when_fresh(tmp_path):
    """is_stale() returns False when touched recently."""
    hb_file = str(tmp_path / "hb")
    hb = Heartbeat(path=hb_file, enabled=True)
    hb.touch()
    assert hb.is_stale(5.0) is False


# ---------------------------------------------------------------------------
# Build helpers
# ---------------------------------------------------------------------------
def test_build_heartbeat_disabled_when_webhook_empty():
    """build_heartbeat() returns a disabled heartbeat when alerting is off."""
    with patch("polymarket_arb.alerting.SETTINGS") as mock_s:
        mock_s.alert_webhook_url = ""
        mock_s.alert_heartbeat_file = "state/HEARTBEAT"
        mock_s.alert_heartbeat_timeout_s = 10.0
        hb = build_heartbeat()
        assert hb.enabled is False


def test_build_heartbeat_enabled_when_webhook_set():
    """build_heartbeat() returns an enabled heartbeat when alerting is on."""
    with patch("polymarket_arb.alerting.SETTINGS") as mock_s:
        mock_s.alert_webhook_url = "http://webhook"
        mock_s.alert_heartbeat_file = "state/HEARTBEAT"
        mock_s.alert_heartbeat_timeout_s = 10.0
        hb = build_heartbeat()
        assert hb.enabled is True


# ---------------------------------------------------------------------------
# Execution — kill switch alert integration
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_kill_switch_alert_fires_on_partial_loss_breach():
    """When residual loss breaches PARTIAL_LOSS_CAP_USD, Execution fires the
    ALERT_KILL_SWITCH webhook."""
    from polymarket_arb.execution import Execution
    from polymarket_arb.risk import RiskState
    from polymarket_arb.metrics import Metrics
    from polymarket_arb.models import MarketState
    from polymarket_arb.config import SETTINGS

    class _NoOpClient:
        async def submit_order(self, *a, **kw): return "id"
        async def cancel_order(self, *a, **kw): return True
        async def is_filled(self, *a, **kw): return True

    risk = RiskState(balance=10000.0)
    metrics = Metrics()
    alerter = _make_alerter_with_url()

    sent_alerts = []
    alerter.alert = lambda kind, msg, **kw: sent_alerts.append((kind, msg, kw))

    exec_ = Execution(client=_NoOpClient(), risk=risk, metrics=metrics, alerter=alerter)
    exec_.register(MarketState(
        market_id="m1", question="Q", yes_token_id="0xYES", no_token_id="0xNO",
        best_yes_ask=0.48, best_no_ask=0.50, yes_size=100, no_size=100,
        last_update_ts=time.time(), yes_ts=time.time(), no_ts=time.time(),
        best_yes_bid=0.47, best_no_bid=0.49,
    ))

    cap = SETTINGS.partial_loss_cap_usd
    exec_._after_residual(
        st=MarketState(
            market_id="m1", question="Q", yes_token_id="0xYES", no_token_id="0xNO",
            best_yes_ask=0.48, best_no_ask=0.50,
        ),
        combined=0.98, size=10.0,
        residual=-(cap + 10.0),  # exceeds cap
        age_ms=10.0, band="fresh", action="exit",
    )

    kill_alerts = [a for a in sent_alerts if a[0] == ALERT_KILL_SWITCH]
    assert len(kill_alerts) == 1
    assert "PARTIAL_LOSS_CAP_USD" in kill_alerts[0][1]
    assert risk.halted is True


@pytest.mark.asyncio
async def test_no_kill_switch_alert_when_alerter_none():
    """Without an alerter, the kill switch still halts risk (no crash)."""
    from polymarket_arb.execution import Execution
    from polymarket_arb.risk import RiskState
    from polymarket_arb.metrics import Metrics
    from polymarket_arb.models import MarketState

    class _NoOpClient:
        async def submit_order(self, *a, **kw): return "id"
        async def cancel_order(self, *a, **kw): return True
        async def is_filled(self, *a, **kw): return True

    risk = RiskState(balance=10000.0)
    metrics = Metrics()
    exec_ = Execution(client=_NoOpClient(), risk=risk, metrics=metrics, alerter=None)
    exec_._after_residual(
        st=MarketState(
            market_id="m1", question="Q", yes_token_id="0xYES", no_token_id="0xNO",
            best_yes_ask=0.48, best_no_ask=0.50,
        ),
        combined=0.98, size=10.0,
        residual=-999.0,  # well past cap
        age_ms=10.0, band="fresh", action="exit",
    )
    assert risk.halted is True


# ---------------------------------------------------------------------------
# Alert kind constants are stable
# ---------------------------------------------------------------------------
def test_alert_kind_constants():
    """The four alert kind constants match the spec."""
    assert ALERT_BOT_CRASH == "bot_crash"
    assert ALERT_EMERGENCY_STOP == "emergency_stop"
    assert ALERT_KILL_SWITCH == "kill_switch"
    assert ALERT_WS_DISCONNECT == "ws_disconnect"
