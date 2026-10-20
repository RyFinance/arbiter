"""A1: Fresh PAPER_TRADE session with cached credentials.

Runs the bot in PAPER_TRADE=true, DRY_RUN=false mode for a minimum 20-minute
session using the credentials already cached in .env (POLYMARKET_API_KEY/SECRET/PASSPHRASE).
Logs to state/session_A1_cached.log (console) and state/bot.jsonl (JSONL).

Key verification goal: confirm that API credential derivation happens ONCE at
cold-path startup (not per-fire), and measure the resulting latency.

Usage:
    .venv/bin/python scripts/run_a1_session.py [duration_minutes]
    .venv/bin/python scripts/run_a1_session.py > state/session_A1_cached.log 2>&1
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Load .env explicitly to ensure cached credentials are available.
from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env", override=True)
os.environ["DRY_RUN"] = "false"
os.environ["PAPER_TRADE"] = "true"
os.environ["LIVE_SIGNING_VERIFIED"] = "true"

LOG_PATH = PROJECT_ROOT / "state" / "bot.jsonl"
CONSOLE_LOG_PATH = PROJECT_ROOT / "state" / "session_A1_cached.log"
MONITOR_PATH = PROJECT_ROOT / "state" / "paper_session_monitor_aug3.json"

# Duration: minimum 20 minutes per task spec. Default 25 min.
DURATION_MINUTES = 25


def setup_global_logger():
    """Create a BotLogger and propagate it to all modules that imported `log`."""
    from polymarket_arb.logger import BotLogger
    import polymarket_arb.logger as logger_mod

    log_instance = BotLogger(jsonl_path=str(LOG_PATH), level="INFO")
    logger_mod.log = log_instance

    modules_to_patch = [
        "polymarket_arb.main",
        "polymarket_arb.arb_engine",
        "polymarket_arb.execution",
        "polymarket_arb.price_feed",
        "polymarket_arb.risk",
        "polymarket_arb.kronos_session",
        "polymarket_arb.window_tracker",
        "polymarket_arb.cache",
        "polymarket_arb.clients.clob_client",
        "polymarket_arb.clients.trading_client",
        "polymarket_arb.clients.gamma_client",
        "polymarket_arb.clients.lse_client",
        "polymarket_arb.scan",
        "polymarket_arb.metrics",
    ]
    for mod_name in modules_to_patch:
        try:
            mod = sys.modules.get(mod_name)
            if mod is not None and hasattr(mod, "log"):
                mod.log = log_instance
        except Exception:
            pass

    return log_instance


def patch_price_feed():
    """Patch PriceFeed._on_ws_tick to properly schedule async callbacks."""
    from polymarket_arb.price_feed import PriceFeed
    import time as _time

    def patched_on_ws_tick(self, token_id, best_ask, size, best_bid=None):
        st = self._token_owner.get(token_id)
        if st is None:
            return
        if token_id == st.yes_token_id:
            st.best_yes_ask, st.yes_size, st.best_yes_bid = best_ask, size, best_bid
        else:
            st.best_no_ask, st.no_size, st.best_no_bid = best_ask, size, best_bid
        st.last_update_ts = _time.time()
        if token_id == st.yes_token_id:
            st.yes_ts = _time.time()
        else:
            st.no_ts = _time.time()
        self.on_update(st)

    PriceFeed._on_ws_tick = patched_on_ws_tick


def patch_main_logger():
    """Patch main.py's _setup_logger to use our global logger."""
    from polymarket_arb.logger import BotLogger
    import polymarket_arb.main as main_mod
    from polymarket_arb.config import SETTINGS

    def patched_setup():
        global log
        log_instance = BotLogger(jsonl_path=SETTINGS.log_jsonl, level=SETTINGS.log_level)
        main_mod.log = log_instance
        import polymarket_arb.logger as logger_mod
        logger_mod.log = log_instance
        for mod_name in ["polymarket_arb.arb_engine", "polymarket_arb.execution",
                         "polymarket_arb.price_feed", "polymarket_arb.risk",
                         "polymarket_arb.kronos_session", "polymarket_arb.window_tracker",
                         "polymarket_arb.cache",
                         "polymarket_arb.clients.clob_client",
                         "polymarket_arb.clients.trading_client",
                         "polymarket_arb.clients.gamma_client",
                         "polymarket_arb.clients.lse_client",
                         "polymarket_arb.scan", "polymarket_arb.metrics"]:
            mod = sys.modules.get(mod_name)
            if mod is not None and hasattr(mod, "log"):
                mod.log = log_instance

    main_mod._setup_logger = patched_setup


def _parse_summary_line(result: dict, msg: str) -> None:
    """Parse a ``session metrics: ...`` line into *result* (mutates in-place)."""
    patterns = {
        "loops": r"loops=(\d+)",
        "opportunities": r"opps=(\d+)",
        "orders_sent": r"sent=(\d+)",
        "fills": r"fills=(\d+)",
        "partial_fills": r"partial=(\d+)",
        "partial_resolved": r"resolved=(\d+)",
        "missed": r"missed=(\d+)",
        "opp2order_avg_ms": r"opp2order_avg_ms=([\d.]+)",
        "p50_ms": r"p50_ms=([\d.]+)",
        "ack_avg_ms": r"ack_avg_ms=([\d.]+)",
        "locked_pnl": r"locked_pnl=([\d.]+)",
        "residual_pnl": r"residual_pnl=([\d.]+)",
        "session_pnl": r"session_pnl=([\d.]+)",
        "would_lock": r"would_lock=([\d.]+)",
        "macro_skipped": r"macro_skipped=(\d+)",
        "macro_skipped_pnl": r"macro_skipped_pnl=([\d.]+)",
        "acks": r"acks=(\d+)",
        "fresh_fills": r"fresh_fills=(\d+)",
        "fresh_attempts": r"fresh_attempts=(\d+)",
        "stale_periods": r"stale_periods=(\d+)",
        "window_rotations": r"window_rotations=(\d+)",
    }
    int_keys = {
        "opps", "sent", "fills", "partial_fills", "partial_resolved",
        "missed", "macro_skipped", "acks", "loops", "fresh_fills",
        "fresh_attempts", "stale_periods", "window_rotations",
        "orders_sent",
    }
    for key, pat in patterns.items():
        match = re.search(pat, msg)
        if match:
            val = match.group(1)
            if key in int_keys:
                result[key] = int(val)
            else:
                result[key] = float(val)
    if "opp2order_avg_ms" in result:
        result["avg_opp_to_order_ms"] = result["opp2order_avg_ms"]
    if "p50_ms" in result:
        result["p50_opp_to_order_ms"] = result["p50_ms"]
    band_match = re.search(r"bandfills\[([^\]]+)\]", msg)
    if band_match:
        result["bandfills"] = {}
        for part in band_match.group(1).split():
            if "=" in part:
                band_name, val = part.split("=")
                if "/" in val:
                    filled, attempt = val.split("/")
                    result["bandfills"][band_name] = {
                        "filled": int(filled),
                        "attempts": int(attempt),
                    }


def parse_jsonl_events(path: Path, console_log_path: Path = None) -> dict:
    """Parse the bot JSONL log and extract all events.

    If the JSONL is empty/missing and ``console_log_path`` is given, falls back
    to parsing the final ``session metrics:`` summary line from the console log
    so latency/fill metrics are never reported as zero due to a log-rotation or
    flush race at session end.
    """
    result = {
        "ticks": 0,
        "opportunities": 0,
        "would_lock_count": 0,
        "fills": 0,
        "partial_fills": 0,
        "partial_resolved": 0,
        "missed": 0,
        "macro_skipped": 0,
        "locked_pnl": 0.0,
        "residual_pnl": 0.0,
        "would_lock_pnl": 0.0,
        "session_pnl": 0.0,
        "realized_pnl": 0.0,
        "avg_opp_to_order_ms": 0.0,
        "p50_opp_to_order_ms": 0.0,
        "avg_ack_ms": 0.0,
        "window_windows": [],
        "regime": None,
        "errors": [],
        "summary": None,
        "trade_events": [],
        "total_log_lines": 0,
        "ws_subscribed": False,
        "edge_events": 0,
        "fire_events": 0,
        "fill_events": 0,
    }

    # ── retry loop: the bot's log.close() may still be flushing ──
    _max_wait = 10.0
    _start = time.time()
    while (not path.exists() or path.stat().st_size == 0) and (time.time() - _start) < _max_wait:
        time.sleep(0.5)

    # ── fallback: parse console log for the summary line ──
    _console_summary = None
    if console_log_path is not None and console_log_path.exists():
        try:
            _txt = console_log_path.read_text(errors="replace")
            for _line in reversed(_txt.splitlines()):
                if "session metrics:" in _line:
                    _console_summary = _line
                    break
        except Exception:
            pass

    if not path.exists():
        # Still missing — try console fallback before giving up
        if _console_summary:
            _parse_summary_line(result, _console_summary)
            result["summary"] = _console_summary
        return result

    events = []
    with open(path, "r") as f:
        for line in f:
            result["total_log_lines"] += 1
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                events.append(rec)
            except json.JSONDecodeError:
                continue

    for rec in events:
        event = rec.get("event", "")
        data = rec.get("data", {})
        msg = rec.get("msg", "")

        if "WINDOW ACTIVE" in msg:
            result["window_windows"].append(msg)

        if "Regime forecast:" in msg or "Applied regime" in msg:
            result["regime"] = msg

        if "HIGH_MACRO_RISK" in msg:
            result["macro_skipped"] += 1

        if "WS subscribed" in msg:
            result["ws_subscribed"] = True

        if "EDGE" in msg:
            result["edge_events"] += 1

        if "FIRE" in msg:
            result["fire_events"] += 1

        if "FILLED" in msg:
            result["fill_events"] += 1

        if event == "ERROR" or event == "CRITICAL":
            result["errors"].append(msg)

        if event == "TRADE":
            outcome = rec.get("msg", "")
            if outcome == "filled":
                result["fills"] += 1
            elif outcome == "missed":
                result["missed"] += 1
                result["would_lock_count"] += 1
                wl = data.get("would_lock")
                if wl is not None:
                    result["would_lock_pnl"] += float(wl)

    # Parse the final summary line from JSONL events
    _found_summary = False
    for rec in reversed(events):
        msg = rec.get("msg", "")
        if "session metrics:" in msg:
            result["summary"] = msg
            _parse_summary_line(result, msg)
            _found_summary = True
            break

    # ── fallback: if the JSONL had no summary line (flush race), use console ──
    if not _found_summary and _console_summary is not None:
        result["summary"] = _console_summary
        _parse_summary_line(result, _console_summary)

    return result


async def run_a1_session(duration_minutes: float = 25.0):
    """Run the bot in PAPER_TRADE mode for the specified duration."""

    # Clear old log
    if LOG_PATH.exists():
        LOG_PATH.unlink()

    start_time = time.time()
    end_time = start_time + duration_minutes * 60

    print(f"[{datetime.now(timezone.utc).isoformat()}] Starting A1 paper trading session", flush=True)
    print(f"  PAPER_TRADE={os.environ['PAPER_TRADE']}", flush=True)
    print(f"  DRY_RUN={os.environ['DRY_RUN']}", flush=True)
    print(f"  LIVE_SIGNING_VERIFIED={os.environ.get('LIVE_SIGNING_VERIFIED', 'not set')}", flush=True)
    print(f"  PRIVATE_KEY set: {bool(os.environ.get('PRIVATE_KEY'))}", flush=True)
    print(f"  POLYMARKET_API_KEY cached: {bool(os.environ.get('POLYMARKET_API_KEY'))}", flush=True)
    print(f"  Duration target: {duration_minutes} minutes", flush=True)
    print(flush=True)

    # Apply patches BEFORE importing main
    log_instance = setup_global_logger()
    patch_price_feed()
    patch_main_logger()
    print("Patches applied: global logger + async callback", flush=True)

    from polymarket_arb.main import main

    task = asyncio.create_task(main(), name="bot-main")

    hang_threshold = 300  # 5 minutes without log activity
    last_log_size = 0
    last_log_check = time.time()
    crashes = False
    hung_tasks_detected = False
    last_report = 0

    check_interval = 120  # Report every 2 minutes
    while time.time() < end_time:
        elapsed = time.time() - start_time

        if task.done():
            exc = task.exception()
            if exc:
                crashes = True
                print(f"\n[!] Bot task CRASHED: {type(exc).__name__}: {exc}", flush=True)
            else:
                print(f"\n[!] Bot task completed unexpectedly after {elapsed:.1f}s", flush=True)
            break

        # Check log activity for hangs
        if LOG_PATH.exists():
            current_size = LOG_PATH.stat().st_size
            if current_size == last_log_size:
                idle_time = time.time() - last_log_check
                if idle_time > hang_threshold:
                    print(f"\n[!] WARNING: No log activity for {idle_time:.0f}s — checking tasks...", flush=True)
                    all_tasks = asyncio.all_tasks()
                    print(f"    Active asyncio tasks: {len(all_tasks)}", flush=True)
                    for t in all_tasks:
                        print(f"    - {t.get_name()}: done={t.done()}", flush=True)
                    hung_tasks_detected = True
                    last_log_check = time.time()
            else:
                last_log_size = current_size
                last_log_check = time.time()

        # Report every check_interval seconds
        if elapsed - last_report >= check_interval:
            current_metrics = parse_jsonl_events(LOG_PATH)
            print(f"[{elapsed/60:.0f}m / {duration_minutes:.0f}m] "
                  f"alive={not task.done()} log_lines={current_metrics.get('total_log_lines', 0)} "
                  f"ticks={current_metrics.get('ticks', 0)} "
                  f"opps={current_metrics.get('opportunities', 0)} "
                  f"fills={current_metrics.get('fills', 0)} "
                  f"missed={current_metrics.get('missed', 0)} "
                  f"ws_subbed={current_metrics.get('ws_subscribed', False)} "
                  f"avg_ms={current_metrics.get('avg_opp_to_order_ms', 'N/A')} "
                  f"p50_ms={current_metrics.get('p50_opp_to_order_ms', 'N/A')}",
                  flush=True)
            last_report = elapsed

        await asyncio.sleep(0.5)

    # Stop the bot
    print(f"\n[{datetime.now(timezone.utc).isoformat()}] Stopping bot...", flush=True)
    if not task.done():
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=20)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass

    # Give it a moment to write final summary
    await asyncio.sleep(3)

    elapsed = time.time() - start_time
    print(f"Session duration: {elapsed:.1f}s ({elapsed/60:.1f}m)", flush=True)

    # Parse final metrics — pass console log path as fallback source of truth
    metrics = parse_jsonl_events(LOG_PATH, console_log_path=CONSOLE_LOG_PATH)
    metrics["session_duration_s"] = elapsed
    metrics["crashes"] = crashes
    metrics["hung_tasks"] = hung_tasks_detected

    # Save monitor results
    with open(MONITOR_PATH, "w") as f:
        json.dump(metrics, f, indent=2, default=str)

    # Print final results
    print("\n" + "=" * 70, flush=True)
    print(f"A1 SESSION RESULTS", flush=True)
    print("=" * 70, flush=True)
    print(f"Session duration:      {metrics['session_duration_s']:.1f}s ({metrics['session_duration_s']/60:.1f}m)", flush=True)
    print(f"Crashed:               {metrics['crashes']}", flush=True)
    print(f"Hung tasks:            {metrics['hung_tasks']}", flush=True)
    print(f"Total ticks processed: {metrics.get('ticks', 0)}", flush=True)
    print(f"Arb opportunities:     {metrics.get('opportunities', 0)}", flush=True)
    print(f"Would-lock count:      {metrics.get('would_lock_count', 0)}", flush=True)
    print(f"Would-lock PnL forgone:{metrics.get('would_lock_pnl', 0):.4f}", flush=True)
    print(f"Simulated fills:       {metrics.get('fills', 0)}", flush=True)
    print(f"Partial fills:         {metrics.get('partial_fills', 0)}", flush=True)
    print(f"Partial resolved:      {metrics.get('partial_resolved', 0)}", flush=True)
    print(f"Missed (rejected):     {metrics.get('missed', 0)}", flush=True)
    print(f"Locked PnL:            {metrics.get('locked_pnl', 0):.4f}", flush=True)
    print(f"Residual PnL:          {metrics.get('residual_pnl', 0):.4f}", flush=True)
    print(f"Session PnL:           {metrics.get('session_pnl', 0):.4f}", flush=True)
    print(f"HIGH_MACRO_RISK skips: {metrics.get('macro_skipped', 0)}", flush=True)
    print(f"Avg opp->order ms:     {metrics.get('avg_opp_to_order_ms', 'N/A')}", flush=True)
    print(f"P50 opp->order ms:     {metrics.get('p50_opp_to_order_ms', 'N/A')}", flush=True)
    print(f"Avg ack ms:            {metrics.get('avg_ack_ms', 'N/A')}", flush=True)
    print(f"WS subscribed:         {metrics.get('ws_subscribed', False)}", flush=True)
    print(f"Edge events:           {metrics.get('edge_events', 0)}", flush=True)
    print(f"FIRE events:           {metrics.get('fire_events', 0)}", flush=True)
    print(f"FILL events:           {metrics.get('fill_events', 0)}", flush=True)
    print(f"Window windows seen:   {len(metrics.get('window_windows', []))}", flush=True)
    print(f"Total log lines:       {metrics.get('total_log_lines', 0)}", flush=True)
    if metrics.get("regime"):
        print(f"Vol regime:            {metrics['regime']}", flush=True)
    if metrics.get("summary"):
        print(f"\nRaw summary: {metrics['summary']}", flush=True)
    if metrics.get("bandfills"):
        print(f"\nFreshness bands: {metrics['bandfills']}", flush=True)
    if metrics["errors"]:
        print(f"\nErrors ({len(metrics['errors'])}):", flush=True)
        for e in metrics["errors"][:10]:
            print(f"  - {e}", flush=True)
    print(f"\nMonitor data saved to: {MONITOR_PATH}", flush=True)
    print("=" * 70, flush=True)

    return metrics


if __name__ == "__main__":
    duration = float(sys.argv[1]) if len(sys.argv) > 1 else DURATION_MINUTES
    asyncio.run(run_a1_session(duration))
