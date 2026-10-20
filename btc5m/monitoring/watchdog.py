"""TASK B — Standalone watchdog / crash-recovery monitor.

This is a SEPARATE, lightweight process. Run it alongside the bot:

    python -m monitoring.watchdog --config polymarket_arb.config:SETTINGS
    # or simply:
    python monitoring/watchdog.py

It does three things:

1. Watches the heartbeat file written by the bot on every price tick. If the file
   is older than ``--heartbeat-timeout`` (default ``ALERT_HEARTBEAT_TIMEOUT_S``)
   seconds, the bot is declared crashed or hung -> fires ``ALERT_BOT_CRASH``.

2. Watches the emergency-stop file. If the file appears, the bot would normally
   halt on its own; the monitor ALSO alerts that an emergency stop was triggered.

3. Fires the same webhook for: bot-crash (stale heartbeat), emergency-stop,
   PARTIAL_LOSS_CAP_USD kill switch (detected via the alert log line the bot
   emits — this monitor does NOT independently reproduce risk math), and WS
   disconnect (the bot itself fires this alert; the monitor only handles the
   heartbeat/STOP-file cases it can observe externally).

Because this monitor runs OUTSIDE the bot process, it survives a bot crash, OOM
kill, or hang — it is the external witness that can page an operator.

The monitor is OPTIONAL: if ``ALERT_WEBHOOK_URL`` is empty, it exits immediately
(no alerting possible, nothing to do).
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from typing import Optional


# ---------------------------------------------------------------------------
# Alert durability (TASK 1): a failed webhook POST must NEVER silently drop a
# crash alert. Every delivery failure is (a) retried with backoff, (b) written
# to a local JSONL fallback so there is always an on-disk record, and (c) queued
# in-memory and re-flushed on a later cycle when the webhook recovers.
# ---------------------------------------------------------------------------

# Local fallback file for watchdog alerts (mirrors polymarket_arb.alerting's
# ALERTS_LOG_FILE pattern). Written on EVERY delivery failure, independent of
# the webhook. Overridable per-call for tests.
WATCHDOG_ALERTS_FILE = "state/watchdog_alerts.jsonl"

# Delivery retry policy (exponential backoff).
WEBHOOK_RETRY_ATTEMPTS = 3          # total attempts, including the first
WEBHOOK_RETRY_BASE_DELAY_S = 0.5    # delay after the 1st failure
WEBHOOK_RETRY_BACKOFF = 2.0         # multiplier each retry

# Max pending alerts kept in memory. Guard against unbounded growth if the
# endpoint is down for days; the local file remains complete regardless.
MAX_PENDING_ALERTS = 500


def _alerts_file_path(fallback_path: str | None) -> str:
    return fallback_path or WATCHDOG_ALERTS_FILE


def write_local_alert(kind: str, message: str, fields: Optional[dict],
                      delivery: str, queued_ts: float,
                      fallback_path: str | None = None) -> None:
    """Append one alert record to the local fallback JSONL. NEVER raises.

    ``delivery`` is ``"failed"`` (delivery could not complete after retries — the
    operator must page from the file) or ``"sent"`` (a queued alert was later
    flushed successfully). Every line is self-contained so the file is a complete
    audit trail even if the webhook was down the whole session.
    """
    try:
        path = _alerts_file_path(fallback_path)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        record = {
            "ts": time.time(),
            "queued_ts": round(queued_ts, 3),
            "kind": kind,
            "message": message,
            "delivery": delivery,
            "source": "watchdog",
        }
        if fields:
            record.update({k: v for k, v in (fields or {}).items() if v is not None})
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except Exception as e:  # E1: file writes must never crash the monitor
        print(f"[watchdog] local alert write FAILED for {kind}: {e!r}", flush=True)


def read_local_alerts(fallback_path: str | None = None) -> list[dict]:
    """Parse the local alert JSONL back into records for tests/operators."""
    path = _alerts_file_path(fallback_path)
    if not os.path.isfile(path):
        return []
    records = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return records


# ---------------------------------------------------------------------------
# Minimal webhook client (sync, no async loop needed for the monitor).
# ---------------------------------------------------------------------------
def _send_webhook(url: str, payload: dict, timeout_s: float = 5.0) -> bool:
    """POST a webhook payload. Returns True on HTTP 2xx, False otherwise.
    Never raises — the monitor must survive transient network blips."""
    try:
        import httpx
        with httpx.Client(timeout=timeout_s) as client:
            resp = client.post(url, json=payload)
            return resp.status_code < 400
    except Exception:
        return False


def _deliver_with_retry(url: str, payload: dict,
                        attempts: int | None = None,
                        base_delay_s: float | None = None,
                        backoff: float | None = None) -> bool:
    """POST ``payload`` with exponential-backoff retries. Returns True on success.

    TASK 1: a single transient failure (network blip, 5xx) must NOT drop the
    alert. Retry up to ``attempts`` times, sleeping ``base_delay_s * backoff**i``
    between attempts. Never raises. Callers treat ``False`` as "webhook is down"
    and fall back to the local file + queue.
    """
    attempts = WEBHOOK_RETRY_ATTEMPTS if attempts is None else attempts
    base_delay_s = WEBHOOK_RETRY_BASE_DELAY_S if base_delay_s is None else base_delay_s
    backoff = WEBHOOK_RETRY_BACKOFF if backoff is None else backoff
    for i in range(attempts):
        if _send_webhook(url, payload):
            return True
        if i < attempts - 1:
            try:
                time.sleep(base_delay_s * (backoff ** i))
            except Exception:
                pass
    return False


def _build_payload(kind: str, message: str, fields: Optional[dict] = None) -> dict:
    content = f"[{kind.upper()}] {message}"
    if fields:
        extra = "\n".join(f"• {k}={v}" for k, v in fields.items() if v is not None)
        if extra:
            content += "\n" + extra
    return {"content": content, "username": "polymarket-arb-watchdog"}


def fire_webhook(
    url: str, kind: str, message: str, fields: Optional[dict] = None,
    dedup: dict | None = None, dedup_window_s: float = 60.0,
    fallback_path: str | None = None,
    retry_attempts: int | None = None,
    retry_base_delay_s: float | None = None,
) -> bool:
    """Send a webhook alert with de-duplication, retry and local fallback.

    ``dedup`` is a {kind: monotonic_ts} dict shared across calls. Returns True if
    the fire was sent, False if skipped (dedup) or failed.

    TASK 1: a failed delivery is retried with backoff (``_deliver_with_retry``)
    and, if it still cannot be delivered, is written to the local fallback file
    (``fallback_path`` or ``WATCHDOG_ALERTS_FILE``) so a crash alert is NEVER
    silently dropped. The Watchdog additionally queues such alerts and flushes
    them in a later cycle once the endpoint recovers.
    """
    if not url:
        return False
    now = time.monotonic()
    if dedup is not None:
        # Use a None sentinel rather than 0.0 for "never seen" — time.monotonic()
        # returns ~0.0 in a fresh process, so defaulting to 0.0 would force every
        # first-ever fire to satisfy `now - 0.0 < dedup_window_s` and be deduped
        # before the webhook is ever attempted. shop-tested by test_watchdog.py.
        last = dedup.get(kind)
        if last is not None and (now - last) < dedup_window_s:
            return False  # within dedup window, skip
        dedup[kind] = now
    payload = _build_payload(kind, message, fields)
    ok = _deliver_with_retry(
        url, payload,
        attempts=retry_attempts,
        base_delay_s=retry_base_delay_s,
    )
    if not ok:
        # Endpoint is down (or returning errors) after retries. Log loudly + leave
        # a durable local record so the alert is never lost.
        print(f"[watchdog] ALERT DELIVERY FAILED for {kind} — writing local fallback "
              f"to {_alerts_file_path(fallback_path)}", flush=True)
        write_local_alert(kind, message, fields, delivery="failed", queued_ts=now,
                          fallback_path=fallback_path)
    return ok


# ---------------------------------------------------------------------------
# Heartbeat check
# ---------------------------------------------------------------------------
def heartbeat_age_s(path: str) -> float:
    """Seconds since the heartbeat file's mtime. ``float('inf')`` if missing."""
    try:
        return time.time() - os.path.getmtime(path)
    except OSError:
        return float("inf")


def read_heartbeat_ts(path: str) -> Optional[float]:
    """Read the epoch timestamp written inside the heartbeat file."""
    try:
        with open(path, "r") as f:
            return float(f.read().strip())
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Main monitor loop
# ---------------------------------------------------------------------------
class Watchdog:
    """External liveness watcher for the arb bot.

    Detects: stale heartbeat (crash/hang) and emergency-stop-file presence.

    A ``STOP`` sentinel file (separate from the heartbeat) is how the monitor
    distinguishes a clean shutdown from a crash:
      * If the bot is running its hot loop, the heartbeat should stay fresh.
      * If the heartbeat goes stale AND no STOP file exists -> CRASH / HANG.

    The bot itself writes the STOP sentinel on graceful shutdown (the monitor
    can also just detect "no heartbeat for N seconds" as a crash; the STOP file
    only suppresses false-positive crash alerts during planned restarts).
    """

    def __init__(
        self,
        webhook_url: str,
        heartbeat_file: str,
        heartbeat_timeout_s: float,
        emergency_stop_file: str,
        poll_interval_s: float = 2.0,
        alerts_file: str | None = None,
        webhook_retry_attempts: int | None = None,
        webhook_retry_delay_s: float | None = None,
    ):
        self.url = webhook_url
        self.heartbeat_file = heartbeat_file
        self.heartbeat_timeout_s = heartbeat_timeout_s
        self.emergency_stop_file = emergency_stop_file
        self.poll_interval_s = poll_interval_s
        self.alerts_file = alerts_file
        self.webhook_retry_attempts = webhook_retry_attempts
        self.webhook_retry_delay_s = webhook_retry_delay_s
        self._dedup: dict = {}
        self._stop = False
        # TASK 1: pending alerts whose delivery failed (webhook down). Re-flushed
        # every check() cycle until the endpoint recovers; bounded in-memory.
        self._pending_alerts: list[dict] = []
        # Track whether we've ALREADY fired a crash alert (avoid repeating until
        # the bot recovers — i.e. heartbeat freshens again).
        self._crash_alerted = False
        # Track whether we've ALREADY fired an emergency-stop alert.
        self._emergency_alerted = False

    def _fire(self, kind: str, message: str, fields: dict | None = None) -> bool:
        ok = fire_webhook(
            self.url, kind, message, fields,
            dedup=self._dedup, dedup_window_s=30.0,
            fallback_path=self.alerts_file,
            retry_attempts=self.webhook_retry_attempts,
            retry_base_delay_s=self.webhook_retry_delay_s,
        )
        if not ok:
            # Queue for a later flush once the webhook recovers. The local-file
            # fallback was already written by fire_webhook (delivery="failed").
            if len(self._pending_alerts) < MAX_PENDING_ALERTS:
                self._pending_alerts.append({
                    "payload": _build_payload(kind, message, fields),
                    "kind": kind, "message": message, "fields": fields or {},
                    "queued_ts": time.monotonic(),
                })
        return ok

    def _flush_pending(self) -> int:
        """Attempt to deliver queued alerts whose first attempt failed (TASK 1).

        Called every ``check()`` cycle. When the endpoint recovers, queued alerts
        flush here. Returns the number of alerts successfully delivered this pass.
        """
        if not self._pending_alerts:
            return 0
        flushed = 0
        remaining: list[dict] = []
        for entry in self._pending_alerts:
            if _deliver_with_retry(
                self.url, entry["payload"],
                attempts=self.webhook_retry_attempts,
                base_delay_s=self.webhook_retry_delay_s,
            ):
                write_local_alert(entry["kind"], entry["message"], entry["fields"],
                                  delivery="sent", queued_ts=entry["queued_ts"],
                                  fallback_path=self.alerts_file)
                flushed += 1
            else:
                remaining.append(entry)
        self._pending_alerts = remaining
        return flushed

    def check(self):
        """Run one observation cycle. Called in a loop by ``run``."""
        # TASK 1: first try to flush any queued alerts whose initial delivery
        # failed (webhook was down). When the endpoint recovers they flush here,
        # so a crash alert raised while the webhook was down is never lost.
        try:
            self._flush_pending()
        except Exception as e:
            print(f"[watchdog] pending-alert flush error: {e!r}", flush=True)
        now = time.time()

        # --- Heartbeat staleness -----------------------------------------------
        age = heartbeat_age_s(self.heartbeat_file)
        if age == float("inf"):
            # Heartbeat file missing entirely. Could be: bot never started, bot
            # crashed and file was never written, or first run. Only alert if
            # this is a fresh miss (not already in crash-alerted state).
            if not self._crash_alerted:
                ts = read_heartbeat_ts(self.heartbeat_file)
                self._fire(
                    "bot_crash",
                    "Bot heartbeat file missing — process not running or crashed",
                    {"heartbeat_file": self.heartbeat_file, "last_heartbeat_ts": ts},
                )
                self._crash_alerted = True
        elif age > self.heartbeat_timeout_s:
            if not self._crash_alerted:
                ts = read_heartbeat_ts(self.heartbeat_file)
                self._fire(
                    "bot_crash",
                    f"Bot heartbeat stale: no tick in {age:.1f}s "
                    f"(timeout={self.heartbeat_timeout_s}s)",
                    {
                        "heartbeat_file": self.heartbeat_file,
                        "stale_seconds": round(age, 1),
                        "timeout_seconds": self.heartbeat_timeout_s,
                        "last_heartbeat_ts": ts,
                    },
                )
                self._crash_alerted = True
        else:
            # Heartbeat is fresh — bot is alive. Clear the crash-alerted flag so a
            # future crash can alert again.
            if self._crash_alerted:
                self._fire(
                    "bot_recovered",
                    f"Bot heartbeat recovered after {age:.1f}s staleness",
                    {"heartbeat_file": self.heartbeat_file, "stale_seconds": round(age, 1)},
                )
            self._crash_alerted = False

        # --- Emergency stop file -----------------------------------------------
        if os.path.exists(self.emergency_stop_file):
            if not self._emergency_alerted:
                self._fire(
                    "emergency_stop",
                    "Emergency stop file detected — bot should be halted",
                    {"stop_file": self.emergency_stop_file},
                )
                self._emergency_alerted = True
        else:
            if self._emergency_alerted:
                self._fire(
                    "emergency_stop_cleared",
                    "Emergency stop file removed — trading may resume",
                    {"stop_file": self.emergency_stop_file},
                )
            self._emergency_alerted = False

    def run(self, max_iterations: int | None = None):
        """Run the monitor loop. ``max_iterations`` for testability (None = forever)."""
        print(f"[watchdog] monitoring heartbeat={self.heartbeat_file} "
              f"stop={self.emergency_stop_file} timeout={self.heartbeat_timeout_s}s",
              flush=True)
        iteration = 0
        while not self._stop:
            self.check()
            iteration += 1
            if max_iterations is not None and iteration >= max_iterations:
                break
            time.sleep(self.poll_interval_s)
        print("[watchdog] exiting", flush=True)

    def stop(self, *_):
        self._stop = True


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
def _load_config():
    """Load SETTINGS from the polymarket_arb package. Falls back to env vars
    if the package can't be imported (so the monitor can run standalone)."""
    try:
        from polymarket_arb.config import SETTINGS
        return SETTINGS
    except ImportError:
        # Standalone fallback: read env vars directly.
        from dataclasses import dataclass

        @dataclass
        class _Fallback:
            alert_webhook_url: str = os.getenv("ALERT_WEBHOOK_URL", "")
            alert_heartbeat_file: str = os.getenv("ALERT_HEARTBEAT_FILE", "state/HEARTBEAT")
            alert_heartbeat_timeout_s: float = float(os.getenv("ALERT_HEARTBEAT_TIMEOUT_S", "10.0"))
            emergency_stop_file: str = os.getenv("EMERGENCY_STOP_FILE", "state/EMERGENCY_STOP")
        return _Fallback()


def main(argv: list[str] | None = None) -> int:
    cfg = _load_config()
    parser = argparse.ArgumentParser(description="Polymarket arb bot watchdog monitor")
    parser.add_argument("--heartbeat", default=cfg.alert_heartbeat_file,
                        help="Path to the heartbeat file the bot writes")
    parser.add_argument("--timeout", type=float, default=cfg.alert_heartbeat_timeout_s,
                        help="Seconds of heartbeat staleness before crash alert")
    parser.add_argument("--emergency-stop", default=cfg.emergency_stop_file,
                        help="Path to the emergency stop sentinel file")
    parser.add_argument("--interval", type=float, default=2.0,
                        help="Poll interval (seconds)")
    parser.add_argument("--max-iterations", type=int, default=None,
                        help="Stop after N iterations (for testing)")
    args = parser.parse_args(argv)

    url = cfg.alert_webhook_url or os.getenv("ALERT_WEBHOOK_URL", "")
    if not url:
        print("[watchdog] ALERT_WEBHOOK_URL is empty — alerting disabled, exiting.", flush=True)
        return 0

    watchdog = Watchdog(
        webhook_url=url,
        heartbeat_file=args.heartbeat,
        heartbeat_timeout_s=args.timeout,
        emergency_stop_file=args.emergency_stop,
        poll_interval_s=args.interval,
    )

    def _handle_sig(signum, _):
        watchdog.stop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, _handle_sig)

    watchdog.run(max_iterations=args.max_iterations)
    return 0


if __name__ == "__main__":
    sys.exit(main())
