"""Phase 0 smoke tests for alerting.py (copied verbatim from polymarket_arb)."""
from __future__ import annotations

import json

from polyuni.alerting import Heartbeat, WebhookAlerter, build_alerter, build_heartbeat


def test_disabled_alerter_still_writes_local_record(tmp_path):
    alerts_path = tmp_path / "alerts.jsonl"
    alerter = WebhookAlerter(webhook_url="", alerts_path=str(alerts_path))
    assert alerter.enabled is False

    alerter.alert("kill_switch", "capital cap breached", market_id="m1")

    lines = alerts_path.read_text().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["kind"] == "kill_switch"
    assert record["market_id"] == "m1"


def test_dedup_window_suppresses_repeated_fires_within_window(tmp_path):
    alerts_path = tmp_path / "alerts.jsonl"
    alerter = WebhookAlerter(webhook_url="https://example.invalid/hook",
                              dedup_window_s=60.0, alerts_path=str(alerts_path))
    assert alerter._should_fire("ws_disconnect") is True
    alerter._mark("ws_disconnect")
    assert alerter._should_fire("ws_disconnect") is False


def test_build_alerter_is_noop_when_no_webhook_configured():
    # SETTINGS is a frozen dataclass singleton read once at import time from an
    # environment with no ALERT_WEBHOOK_URL set (no .env is checked in) -- this
    # asserts the actual default rather than mutating the frozen instance.
    from polyuni.config import SETTINGS
    assert SETTINGS.alert_webhook_url == ""
    alerter = build_alerter()
    assert alerter.enabled is False


def test_heartbeat_disabled_is_never_stale():
    hb = Heartbeat(path="", enabled=False)
    assert hb.is_stale(0.001) is False


def test_heartbeat_touch_and_read_age(tmp_path):
    path = tmp_path / "HEARTBEAT"
    hb = Heartbeat(path=str(path), enabled=True)
    hb.touch()
    assert path.exists()
    assert Heartbeat.read_age(str(path)) < 5.0
    assert hb.is_stale(1000.0) is False


def test_missing_heartbeat_file_reads_as_infinitely_stale(tmp_path):
    missing = tmp_path / "does_not_exist"
    assert Heartbeat.read_age(str(missing)) == float("inf")
