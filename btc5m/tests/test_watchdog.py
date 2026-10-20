"""TASK B — Tests for the standalone watchdog monitor.

Validates the external liveness watcher:
  * Detects stale heartbeat (crash/hang) and fires ALERT_BOT_CRASH.
  * Detects missing heartbeat file (crash before first write).
  * Alerts when emergency-stop file appears.
  * Does NOT alert when heartbeat is fresh.
  * Does NOT alert crash when heartbeat recovers.
  * Exits immediately when ALERT_WEBHOOK_URL is empty (disabled).
  * _send_webhook returns False on failure, True on 2xx.
  * fire_webhook de-duplicates by kind.
"""
from __future__ import annotations

import os
import time

from unittest.mock import patch, MagicMock

from monitoring.watchdog import (
    Watchdog,
    fire_webhook,
    heartbeat_age_s,
    read_heartbeat_ts,
    _send_webhook,
    _build_payload,
    write_local_alert,
    read_local_alerts,
)


# ---------------------------------------------------------------------------
# _send_webhook (sync, mocked)
# ---------------------------------------------------------------------------
def test_send_webhook_returns_true_on_2xx():
    """A 2xx response returns True."""
    mock_resp = MagicMock(status_code=200, text="")
    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.post = MagicMock(return_value=mock_resp)

    with patch("httpx.Client", return_value=mock_client):
        result = _send_webhook("http://example.com/hook", {"content": "test"})
    assert result is True


def test_send_webhook_returns_false_on_5xx():
    """A 5xx response returns False."""
    mock_resp = MagicMock(status_code=500, text="server error")
    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.post = MagicMock(return_value=mock_resp)

    with patch("httpx.Client", return_value=mock_client):
        result = _send_webhook("http://example.com/hook", {"content": "test"})
    assert result is False


def test_send_webhook_returns_false_on_network_error():
    """A network exception returns False (never raises)."""
    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.post = MagicMock(side_effect=ConnectionError("boom"))

    with patch("httpx.Client", return_value=mock_client):
        result = _send_webhook("http://example.com/hook", {"content": "test"})
    assert result is False


# ---------------------------------------------------------------------------
# fire_webhook with de-duplication
# ---------------------------------------------------------------------------
def test_fire_webhook_returns_false_when_url_empty():
    """No URL => no fire, returns False."""
    result = fire_webhook("", "bot_crash", "msg")
    assert result is False


def test_fire_webhook_dedup_blocks_repeat():
    """Same kind within dedup_window_s is blocked."""
    dedup = {}
    with patch("monitoring.watchdog._send_webhook", return_value=True) as mock_send:
        r1 = fire_webhook("http://x", "kill_switch", "first", dedup=dedup, dedup_window_s=60.0)
        r2 = fire_webhook("http://x", "kill_switch", "dup", dedup=dedup, dedup_window_s=60.0)
    assert r1 is True
    assert r2 is False
    assert mock_send.call_count == 1


def test_fire_webhook_dedup_allows_different_kind():
    """Different kinds fire independently."""
    dedup = {}
    with patch("monitoring.watchdog._send_webhook", return_value=True) as mock_send:
        r1 = fire_webhook("http://x", "kill_switch", "first", dedup=dedup, dedup_window_s=60.0)
        r2 = fire_webhook("http://x", "bot_crash", "other", dedup=dedup, dedup_window_s=60.0)
    assert r1 is True
    assert r2 is True
    assert mock_send.call_count == 2


# ---------------------------------------------------------------------------
# _build_payload
# ---------------------------------------------------------------------------
def test_build_payload_shapes_message():
    payload = _build_payload("kill_switch", "cap breached", {"residual": -30.0})
    assert "[KILL_SWITCH]" in payload["content"]
    assert "cap breached" in payload["content"]
    assert "residual" in payload["content"]
    assert payload["username"] == "polymarket-arb-watchdog"


def test_build_payload_without_fields():
    payload = _build_payload("bot_crash", "process died")
    assert "[BOT_CRASH]" in payload["content"]
    assert "process died" in payload["content"]


# ---------------------------------------------------------------------------
# heartbeat_age_s / read_heartbeat_ts
# ---------------------------------------------------------------------------
def test_heartbeat_age_s_returns_inf_when_missing(tmp_path):
    missing = str(tmp_path / "nope")
    assert heartbeat_age_s(missing) == float("inf")


def test_heartbeat_age_s_returns_positive(tmp_path):
    hb = tmp_path / "hb"
    hb.write_text("1234567890.000")
    age = heartbeat_age_s(str(hb))
    assert age > 0


def test_read_heartbeat_ts_returns_none_when_missing(tmp_path):
    assert read_heartbeat_ts(str(tmp_path / "missing")) is None


def test_read_heartbeat_ts_parses_valid_file(tmp_path):
    hb = tmp_path / "hb"
    hb.write_text("1700000000.123")
    ts = read_heartbeat_ts(str(hb))
    assert ts == 1700000000.123


def test_read_heartbeat_ts_returns_none_on_invalid(tmp_path):
    hb = tmp_path / "hb"
    hb.write_text("not a number")
    assert read_heartbeat_ts(str(hb)) is None


# ---------------------------------------------------------------------------
# Watchdog — crash detection
# ---------------------------------------------------------------------------
def _make_watchdog(tmp_path, timeout_s=1.0, poll_s=0.1):
    return Watchdog(
        webhook_url="http://example.com/hook",
        heartbeat_file=str(tmp_path / "HEARTBEAT"),
        heartbeat_timeout_s=timeout_s,
        emergency_stop_file=str(tmp_path / "EMERGENCY_STOP"),
        poll_interval_s=poll_s,
    )


def test_watchdog_detects_stale_heartbeat(tmp_path):
    """A heartbeat older than the timeout triggers ALERT_BOT_CRASH."""
    wd = _make_watchdog(tmp_path, timeout_s=1.0)
    hb_file = str(tmp_path / "HEARTBEAT")
    old = time.time() - 5.0  # 5 seconds stale
    with open(hb_file, "w") as f:
        f.write(f"{old:.3f}")
    os.utime(hb_file, (old, old))

    with patch("monitoring.watchdog._send_webhook", return_value=True) as mock_send:
        wd.check()

    assert mock_send.call_count == 1
    call_args = mock_send.call_args
    payload = call_args[0][1]  # second positional arg is payload
    assert "BOT_CRASH" in payload["content"]
    assert wd._crash_alerted is True


def test_watchdog_detects_missing_heartbeat(tmp_path):
    """A completely missing heartbeat file triggers ALERT_BOT_CRASH."""
    wd = _make_watchdog(tmp_path, timeout_s=1.0)
    # Don't create the heartbeat file at all.

    with patch("monitoring.watchdog._send_webhook", return_value=True) as mock_send:
        wd.check()

    assert mock_send.call_count == 1
    payload = mock_send.call_args[0][1]
    assert "BOT_CRASH" in payload["content"]
    assert "missing" in payload["content"].lower()


def test_watchdog_no_alert_when_heartbeat_fresh(tmp_path):
    """A fresh heartbeat does NOT trigger any alert."""
    wd = _make_watchdog(tmp_path, timeout_s=10.0)
    hb_file = str(tmp_path / "HEARTBEAT")
    with open(hb_file, "w") as f:
        f.write(f"{time.time():.3f}")

    with patch("monitoring.watchdog._send_webhook", return_value=True) as mock_send:
        wd.check()

    assert mock_send.call_count == 0
    assert wd._crash_alerted is False


def test_watchdog_recovers_after_stale(tmp_path):
    """After a crash alert, a fresh heartbeat clears the alert and notifies recovery."""
    wd = _make_watchdog(tmp_path, timeout_s=1.0)
    hb_file = str(tmp_path / "HEARTBEAT")

    # First: stale heartbeat -> crash alert.
    old = time.time() - 5.0
    with open(hb_file, "w") as f:
        f.write(f"{old:.3f}")
    os.utime(hb_file, (old, old))
    with patch("monitoring.watchdog._send_webhook", return_value=True) as mock_send:
        wd.check()
    assert mock_send.call_count == 1
    assert wd._crash_alerted is True

    # Now: fresh heartbeat -> recovery notification + clears flag.
    with open(hb_file, "w") as f:
        f.write(f"{time.time():.3f}")
    with patch("monitoring.watchdog._send_webhook", return_value=True) as mock_send2:
        wd.check()
    assert mock_send2.call_count == 1
    payload = mock_send2.call_args[0][1]
    assert "BOT_RECOVERED" in payload["content"]
    assert wd._crash_alerted is False


def test_watchdog_does_not_repeat_crash_alert(tmp_path):
    """While still stale, the crash alert fires only once (dedup within check)."""
    wd = _make_watchdog(tmp_path, timeout_s=1.0)
    hb_file = str(tmp_path / "HEARTBEAT")
    old = time.time() - 5.0
    with open(hb_file, "w") as f:
        f.write(f"{old:.3f}")
    os.utime(hb_file, (old, old))

    with patch("monitoring.watchdog._send_webhook", return_value=True) as mock_send:
        wd.check()  # first check -> fires
        wd.check()  # second check -> should NOT fire again (already alerted)
    assert mock_send.call_count == 1


# ---------------------------------------------------------------------------
# Watchdog — emergency stop detection
# ---------------------------------------------------------------------------
def test_watchdog_detects_emergency_stop(tmp_path):
    """When the emergency-stop file appears, ALERT_EMERGENCY_STOP fires."""
    wd = _make_watchdog(tmp_path, timeout_s=10.0)
    # Fresh heartbeat so crash alert doesn't interfere.
    with open(str(tmp_path / "HEARTBEAT"), "w") as f:
        f.write(f"{time.time():.3f}")
    # Create the stop file.
    (tmp_path / "EMERGENCY_STOP").touch()

    with patch("monitoring.watchdog._send_webhook", return_value=True) as mock_send:
        wd.check()

    assert mock_send.call_count == 1
    payload = mock_send.call_args[0][1]
    assert "EMERGENCY_STOP" in payload["content"]
    assert wd._emergency_alerted is True


def test_watchdog_no_emergency_alert_when_file_absent(tmp_path):
    """No stop file => no emergency alert."""
    wd = _make_watchdog(tmp_path, timeout_s=10.0)
    with open(str(tmp_path / "HEARTBEAT"), "w") as f:
        f.write(f"{time.time():.3f}")

    with patch("monitoring.watchdog._send_webhook", return_value=True) as mock_send:
        wd.check()

    assert mock_send.call_count == 0


def test_watchdog_emergency_clear_notification(tmp_path):
    """When the stop file is removed after being present, a clearance alert fires."""
    wd = _make_watchdog(tmp_path, timeout_s=10.0)
    with open(str(tmp_path / "HEARTBEAT"), "w") as f:
        f.write(f"{time.time():.3f}")
    stop_file = tmp_path / "EMERGENCY_STOP"
    stop_file.touch()

    # First check: stop file present -> alert.
    with patch("monitoring.watchdog._send_webhook", return_value=True):
        wd.check()
    assert wd._emergency_alerted is True

    # Remove the file -> clearance alert.
    stop_file.unlink()
    with patch("monitoring.watchdog._send_webhook", return_value=True) as mock_send:
        wd.check()
    assert mock_send.call_count == 1
    payload = mock_send.call_args[0][1]
    assert "EMERGENCY_STOP_CLEARED" in payload["content"]
    assert wd._emergency_alerted is False


# ---------------------------------------------------------------------------
# Watchdog — run loop (max_iterations for testability)
# ---------------------------------------------------------------------------
def test_watchdog_run_with_max_iterations(tmp_path):
    """run(max_iterations=2) executes exactly 2 check cycles."""
    wd = _make_watchdog(tmp_path, timeout_s=1.0, poll_s=0.01)
    # Stale heartbeat so alerts fire.
    hb_file = str(tmp_path / "HEARTBEAT")
    old = time.time() - 5.0
    with open(hb_file, "w") as f:
        f.write(f"{old:.3f}")
    os.utime(hb_file, (old, old))

    with patch("monitoring.watchdog._send_webhook", return_value=True) as mock_send, \
         patch("time.sleep"):  # don't actually sleep in the test
        wd.run(max_iterations=2)

    # First iteration fires the crash alert; second is deduped by _crash_alerted.
    assert mock_send.call_count == 1


# ---------------------------------------------------------------------------
# TASK 1 — alert durability: retry/backoff + local file fallback + flush queue
# ---------------------------------------------------------------------------
def test_fire_webhook_records_locally_when_endpoint_dead(tmp_path):
    """A failed webhook delivery must retry, then land in the local log — never
    silently drop the alert."""
    fallback = str(tmp_path / "state" / "watchdog_alerts.jsonl")
    with patch("monitoring.watchdog._send_webhook", return_value=False) as mock_send:
        ok = fire_webhook(
            "http://dead-endpoint/hook", "bot_crash", "process died",
            {"stale_s": 9.0}, retry_attempts=1, fallback_path=fallback,
        )
    assert ok is False
    # Every attempt touched the (dead) endpoint before giving up.
    assert mock_send.call_count == 1
    records = read_local_alerts(fallback)
    assert len(records) == 1
    assert records[0]["kind"] == "bot_crash"
    assert records[0]["delivery"] == "failed"
    assert "process died" in records[0]["message"]


def test_watchdog_flushes_queued_alerts_when_endpoint_recovers(tmp_path):
    """Endpoint dead -> alert is queued + persisted locally. Endpoint recovers ->
    the queued alert flushes to the webhook and is marked sent locally."""
    fallback = str(tmp_path / "state" / "watchdog_alerts.jsonl")
    wd = Watchdog(
        webhook_url="http://example.com/hook",
        heartbeat_file=str(tmp_path / "HEARTBEAT"),
        heartbeat_timeout_s=1.0,
        emergency_stop_file=str(tmp_path / "EMERGENCY_STOP"),
        poll_interval_s=0.01,
        alerts_file=fallback,
        webhook_retry_attempts=1,  # skip the backoff sleep in tests
    )
    with open(str(tmp_path / "HEARTBEAT"), "w") as f:
        f.write(f"{time.time():.3f}")

    # Phase 1: endpoint dead. Fire a crash alert -> delivery fails -> queued.
    with patch("monitoring.watchdog._send_webhook", return_value=False) as mock_send:
        ok = wd._fire("bot_crash", "process died", {"stale_s": 9.0})
    assert ok is False
    assert len(wd._pending_alerts) == 1
    failed_records = read_local_alerts(fallback)
    assert any(r["kind"] == "bot_crash" and r["delivery"] == "failed" for r in failed_records)

    # Phase 2: endpoint recovers. The next check() flushes the queued alert.
    recovered = [True]
    def _recovering(url, payload, timeout_s=5.0):
        return recovered[0]
    with patch("monitoring.watchdog._send_webhook", side_effect=_recovering) as mock_send:
        wd.check()
    assert len(wd._pending_alerts) == 0
    assert mock_send.call_count >= 1
    all_records = read_local_alerts(fallback)
    assert any(r["kind"] == "bot_crash" and r["delivery"] == "sent" for r in all_records)


def test_write_local_alert_never_raises_on_bad_path(tmp_path):
    """Local fallback writes must never crash the monitor, even for bad paths."""
    write_local_alert("bot_crash", "msg", {"x": 1}, delivery="failed",
                      queued_ts=1.0, fallback_path=str(tmp_path / "no" / "dir" / "a.jsonl"))
    assert read_local_alerts(str(tmp_path / "no" / "dir" / "a.jsonl"))[0]["kind"] == "bot_crash"


# ---------------------------------------------------------------------------
# CLI — disabled exits early
# ---------------------------------------------------------------------------
def test_watchdog_cli_exits_when_webhook_empty(tmp_path):
    """The CLI main() returns 0 and prints a message when the webhook URL is empty."""
    from monitoring.watchdog import main as watchdog_main

    with patch.dict(os.environ, {"ALERT_WEBHOOK_URL": ""}):
        rc = watchdog_main(["--heartbeat", str(tmp_path / "hb")])
    assert rc == 0
