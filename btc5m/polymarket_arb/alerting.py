"""TASK B — Crash recovery & alerting.

Two pieces live here:

1. ``WebhookAlerter`` — a thin, config-gated wrapper around a Discord/Telegram-style
   incoming webhook. When ``SETTINGS.alert_webhook_url`` is empty the alerter is a
   complete no-op (alerts are NEVER required, the bot runs fine without them).

   Alerts are de-duplicated: the same alert kind will not fire more than once per
   ``dedup_window_s``. This prevents alert storms during a fast-flapping condition
   (e.g. the WS churning reconnects). Each alert is also logged locally so there is
   always an on-disk record independent of the webhook.

   The public API is tiny: ``alert(kind, message, **_fields)``. ``kind`` is one of
   the four documented event categories:

   * ``ALERT_BOT_CRASH``    — heartbeat monitor detected the bot is dead.
   * ``ALERT_EMERGENCY_STOP`` — the emergency-stop file appeared.
   * ``ALERT_KILL_SWITCH``  — PARTIAL_LOSS_CAP_USD breached (risk.halt).
   * ``ALERT_WS_DISCONNECT`` — WS down longer than STALE_BOOK_TIMEOUT_S.

2. ``Heartbeat`` — a tiny helper that writes a timestamp to a file on every tick and
   can be read back to measure liveness. The bot writes it in the hot path; the
   standalone monitor script reads it externally.

Both pieces are safe to use from async code (``send`` is async) but ``Heartbeat``
is pure synchronous file I/O — it must be cheap enough to call on every price tick.
"""
from __future__ import annotations

import json
import os
import time
from typing import Optional

from .config import SETTINGS
from .logger import log


# ---------------------------------------------------------------------------
# Alert kinds (stable identifiers for de-dup + routing on the consumer side)
# ---------------------------------------------------------------------------
ALERT_BOT_CRASH = "bot_crash"
ALERT_EMERGENCY_STOP = "emergency_stop"
ALERT_KILL_SWITCH = "kill_switch"
ALERT_WS_DISCONNECT = "ws_disconnect"
ALERT_WS_DISCONNECT_SEVERE = "ws_disconnect_severe"
ALERT_REPEATED_REJECTIONS = "repeated_rejections"
ALERT_DAILY_KILL_SWITCH = "daily_kill_switch"
ALERT_MARKET_RESOLUTION_FAILURE = "market_resolution_failure"
ALERT_MACRO_STATE_FAILURE = "macro_state_failure"

# Path for the separate alerts log file (always written when an alert fires,
# regardless of whether a webhook URL is configured).
ALERTS_LOG_FILE = "state/alerts.jsonl"


class WebhookAlerter:
    """Config-gated webhookalerter. No-op when the URL is empty.

    De-duplicates by alert kind inside a sliding window so a flapping condition
    doesn't spam the channel. Each fire is also logged locally.
    """

    def __init__(
        self,
        webhook_url: str = "",
        dedup_window_s: float = 60.0,
        alerts_path: Optional[str] = None,
        mirror_alerts_path: Optional[str] = None,
        session_id: Optional[str] = None,
    ):
        self._url: str = webhook_url or ""
        self._dedup_window_s = dedup_window_s
        self._last_fired: dict = {}  # kind -> monotonic timestamp
        # Keep the implicit default unresolved so test/runtime overrides apply
        # when writing, while preserving an explicitly targeted archive path.
        self._alerts_path = alerts_path
        self._mirror_alerts_path = mirror_alerts_path
        self._session_id = session_id

    @property
    def enabled(self) -> bool:
        """Check if webhook alerting is enabled (URL configured)."""
        return bool(self._url)

    def _should_fire(self, kind: str) -> bool:
        """True if this kind may fire again (outside the dedup window)."""
        last = self._last_fired.get(kind, 0.0)
        return (time.monotonic() - last) >= self._dedup_window_s

    def _mark(self, kind: str):
        self._last_fired[kind] = time.monotonic()

    # -- public ---------------------------------------------------------------
    def alert(self, kind: str, message: str, **fields):
        """Log + (optionally) send a webhook alert. Async-safe: schedules the send.

        Always logs locally first (synchronous, cheap) so there is a durable record
        even if the webhook POST fails or is disabled. The webhook POST itself is
        fire-and-forget — we do NOT await it in the hot path.

        Also writes a structured record to ``alerts.jsonl`` so alerts have a
        single dedicated, queryable file independent of the main bot log.
        """
        log.critical(f"ALERT[{kind}]: {message}", data=fields or None)
        self._write_alert_record(kind, message, fields)
        if not self.enabled:
            return  # alerting disabled — never required
        if not self._should_fire(kind):
            return  # within dedup window, skip to avoid storms
        self._mark(kind)
        # Fire-and-forget: the async task will log/handle its own errors so a
        # webhook failure never propagates into the trading loop.
        try:
            import asyncio
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(self._send(kind, message, fields))
                return
        except RuntimeError:
            pass
        # No running loop (e.g. called from the monitor script): send inline.
        try:
            import asyncio
            asyncio.run(self._send(kind, message, fields))
        except Exception as e:
            log.error(f"alert webhook failed: {e!r}")

    def _write_alert_record(self, kind: str, message: str, fields: dict):
        """Append a structured alert record to alerts.jsonl (always on disk).

        This gives operators a single file to tail/grep for alerts, independent
        of the main bot.jsonl log. Safe to call from sync or async contexts.
        """
        primary = ALERTS_LOG_FILE if self._alerts_path is None else self._alerts_path
        paths = [primary]
        if self._mirror_alerts_path and os.path.abspath(self._mirror_alerts_path) != os.path.abspath(primary):
            paths.append(self._mirror_alerts_path)
        record = {
            "ts": time.time(),
            "kind": kind,
            "message": message,
        }
        if self._session_id:
            record["session_id"] = self._session_id
        record.update(fields or {})
        line = json.dumps(record, default=str) + "\n"
        for alerts_path in paths:
            try:
                os.makedirs(os.path.dirname(alerts_path) or ".", exist_ok=True)
                with open(alerts_path, "a", encoding="utf-8") as f:
                    f.write(line)
                    f.flush()
                    os.fsync(f.fileno())
            except Exception as e:  # E1: persistence must never crash the hot path
                log.warning(f"alerts.jsonl write failed for {alerts_path}: {e!r}")

    async def _send(self, kind: str, message: str, fields: dict):
        """POST the alert to the webhook URL. Never raises."""
        if not self._url:
            return
        payload = {
            "content": f"[{kind.upper()}] {message}",
            "username": "polymarket-arb-bot",
        }
        # Merge any extra diagnostic fields as a second embed-style block.
        if fields:
            lines = [f"[{k}={v}" if v is not None else f"{k}=none" for k, v in fields.items()]
            payload["content"] += "\n" + "\n".join(f"• {k}={v}" for k, v in fields.items() if v is not None)
        try:
            import httpx
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(self._url, json=payload)
                if resp.status_code >= 400:
                    log.error(f"alert webhook HTTP {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            log.error(f"alert webhook POST failed: {e!r}")

    # -- shutdown -------------------------------------------------------------
    def close(self):
        """No-op for now (no persistent connections). Kept for parity with lifecycle."""
        pass


class Heartbeat:
    """Writes a timestamp file on every tick; externally readable for liveness.

    ``touch()`` is intentionally synchronous and cheap — it's called in the hot path
    on every price update. The file contains a single float (epoch seconds).
    """

    def __init__(self, path: str = "", enabled: bool = False):
        self.path = path
        self.enabled = enabled
        if self.enabled and self.path:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)

    def touch(self) -> None:
        """Write the current wall-clock timestamp. No-op when disabled."""
        if not self.enabled or not self.path:
            return
        try:
            with open(self.path, "w") as f:
                f.write(f"{time.time():.3f}")
        except OSError as e:
            log.debug(f"heartbeat touch failed: {e!r}")

    @staticmethod
    def read_age(path: str) -> float:
        """Return seconds since the heartbeat file was last written.

        Returns ``float('inf')`` if the file does not exist (bot never started or
        died and the file was removed). Callers compare against a timeout.
        """
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return float("inf")
        return time.time() - mtime

    @staticmethod
    def read_timestamp(path: str) -> Optional[float]:
        """Read the epoch timestamp stored in the heartbeat file, or None."""
        try:
            with open(path, "r") as f:
                return float(f.read().strip())
        except (OSError, ValueError):
            return None

    def is_stale(self, timeout_s: float) -> bool:
        """True if the heartbeat is older than ``timeout_s`` (or missing)."""
        if not self.enabled:
            return False  # if we're not writing a heartbeat, don't claim we're stale
        return self.read_age(self.path) > timeout_s

    def stop(self):
        """Best-effort: leave the heartbeat file in place for the monitor to detect
        shutdown vs. crash. The monitor can distinguish by also watching the stop
        sentinel (see the standalone monitor's logic)."""
        pass


def build_alerter() -> WebhookAlerter:
    """Construct an alerter from SETTINGS, or a no-op when disabled."""
    return WebhookAlerter(webhook_url=SETTINGS.alert_webhook_url)


def build_heartbeat() -> Heartbeat:
    """Construct a heartbeat from SETTINGS, disabled when the webhook is off."""
    hb_enabled = bool(SETTINGS.alert_webhook_url)
    return Heartbeat(path=SETTINGS.alert_heartbeat_file, enabled=hb_enabled)
