"""TASK A: Paper trading session validation harness.

Runs the bot in PAPER_TRADE=true mode for a minimum 2-hour session against live
Polymarket BTC 5-minute window markets. Captures and reports:
  - total ticks processed (loops from metrics)
  - arb opportunities detected (opps from metrics)
  - would_lock count (missed from metrics)
  - actual simulated fills (fills from metrics)
  - locked_pnl vs residual_pnl
  - HIGH_MACRO_RISK skip count

Patches:
1. Async callback bug in price_feed.py (on_update called sync, never awaited)
2. Logger propagation: main.py's _setup_logger() only replaces log in its own
   module namespace; other modules (clob_client, arb_engine, etc.) still use the
   default BotLogger with no JSONL. We patch the logger module's `log` so all
   imports see the configured instance.
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

# ---- CRITICAL: Set env vars BEFORE importing the bot package ----
PROJECT_ROOT = Path(__file__).resolve().parent.parent
os.environ["DRY_RUN"] = "false"
os.environ["PAPER_TRADE"] = "true"
os.environ["LIVE_SIGNING_VERIFIED"] = "true"
os.environ["PRIVATE_KEY"] = "ac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
os.environ["PROXY_WALLET"] = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"

LOG_PATH = PROJECT_ROOT / "state" / "bot.jsonl"
MONITOR_PATH = PROJECT_ROOT / "state" / "paper_session_monitor.json"


def parse_metrics_from_summary(summary_line: str) -> dict:
    """Parse the 'session metrics:' summary line from the bot log."""
    m = {}
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
    }
    for key, pat in patterns.items():
        match = re.search(pat, summary_line)
        if match:
            val = match.group(1)
            if key in ("opps", "sent", "fills", "partial_fills", "partial_resolved",
                        "missed", "macro_skipped", "acks", "loops"):
                m[key] = int(val)
            else:
                m[key] = float(val)
    return m


def parse_jsonl_events(path: Path) -> dict:
    """Parse the bot JSONL log and extract all events."""
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

    if not path.exists():
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

    # Parse the final summary line
    for rec in reversed(events):
        msg = rec.get("msg", "")
        if "session metrics:" in msg:
            result["summary"] = msg
            parsed = parse_metrics_from_summary(msg)
            for k, v in parsed.items():
                if k == "loops":
                    result["ticks"] = v
                elif k == "opps":
                    result["opportunities"] = v
                elif k == "missed":
                    result["missed"] = v
                    result["would_lock_count"] = v
                elif k == "fills":
                    result["fills"] = v
                elif k in ("partial_fills", "partial"):
                    result["partial_fills"] = v
                elif k in ("partial_resolved", "resolved"):
                    result["partial_resolved"] = v
                elif k == "locked_pnl":
                    result["locked_pnl"] = v
                elif k == "residual_pnl":
                    result["residual_pnl"] = v
                elif k == "session_pnl":
                    result["session_pnl"] = v
                elif k == "would_lock":
                    result["would_lock_pnl"] = v
                elif k == "macro_skipped":
                    result["macro_skipped"] = v
                elif k == "opp2order_avg_ms":
                    result["avg_opp_to_order_ms"] = v
                elif k == "p50_ms":
                    result["p50_opp_to_order_ms"] = v
                elif k == "ack_avg_ms":
                    result["avg_ack_ms"] = v
            break

    return result


def setup_global_logger():
    """Create a BotLogger and propagate it to all modules that imported `log`.

    The bot's main.py _setup_logger() only replaces the `log` variable in
    main.py's namespace. Other modules (clob_client, arb_engine, risk, etc.)
    imported `log` from logger.py at their module level, so they still use the
    default BotLogger (no JSONL file). This patches the logger module's `log`
    and then updates all other modules to use the same instance.
    """
    from polymarket_arb.logger import BotLogger
    import polymarket_arb.logger as logger_mod

    # Re-create the logger with JSONL output
    log_instance = BotLogger(jsonl_path=str(LOG_PATH), level="INFO")

    # Replace the module-level `log` in logger.py
    logger_mod.log = log_instance

    # Propagate to all modules that imported `log` from logger
    import importlib
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
    """Patch PriceFeed._on_ws_tick to properly schedule async callbacks.

    Bug: price_feed.py line 111 calls self.on_update(st) synchronously,
    but main.py passes an async def _on_tick — coroutine never awaited.
    Fix: keep _on_ws_tick sync, schedule on_update as asyncio.create_task.
    """
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
        if self.on_update is not None:
            coro = self.on_update(st)
            if asyncio.iscoroutine(coro):
                asyncio.create_task(coro)

    PriceFeed._on_ws_tick = patched_on_ws_tick


def patch_main_logger():
    """Patch main.py's _setup_logger to use our global logger instead of creating a new one."""
    from polymarket_arb.logger import BotLogger
    import polymarket_arb.main as main_mod
    from polymarket_arb.config import SETTINGS

    original_setup = main_mod._setup_logger

    def patched_setup():
        global log
        log_instance = BotLogger(jsonl_path=SETTINGS.log_jsonl, level=SETTINGS.log_level)
        main_mod.log = log_instance
        # Also propagate to logger module and other modules
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


async def run_paper_session(duration_hours: float = 2.0):
    """Run the bot in-process for the specified duration."""

    # Clear old log
    if LOG_PATH.exists():
        LOG_PATH.unlink()

    print(f"[{datetime.now(timezone.utc).isoformat()}] Starting paper trading session")
    print(f"  PAPER_TRADE={os.environ['PAPER_TRADE']}")
    print(f"  DRY_RUN={os.environ['DRY_RUN']}")
    print(f"  Duration target: {duration_hours} hours")
    print()

    # Apply patches BEFORE importing main
    setup_global_logger()
    patch_price_feed()
    patch_main_logger()
    print("Patches applied: global logger + async callback")

    from polymarket_arb.main import main

    start_time = time.time()
    end_time = start_time + duration_hours * 3600

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
                print(f"\n[!] Bot task CRASHED: {type(exc).__name__}: {exc}")
            else:
                print(f"\n[!] Bot task completed unexpectedly after {elapsed:.1f}s")
            break

        # Check log activity for hangs
        if LOG_PATH.exists():
            current_size = LOG_PATH.stat().st_size
            if current_size == last_log_size:
                idle_time = time.time() - last_log_check
                if idle_time > hang_threshold:
                    print(f"\n[!] WARNING: No log activity for {idle_time:.0f}s — checking tasks...")
                    all_tasks = asyncio.all_tasks()
                    print(f"    Active asyncio tasks: {len(all_tasks)}")
                    for t in all_tasks:
                        print(f"    - {t.get_name()}: done={t.done()}")
                    hung_tasks_detected = True
                    last_log_check = time.time()
            else:
                last_log_size = current_size
                last_log_check = time.time()

        # Report every check_interval seconds
        if elapsed - last_report >= check_interval:
            remaining = end_time - time.time()
            log_lines = 0
            if LOG_PATH.exists():
                with open(LOG_PATH, "r") as f:
                    log_lines = sum(1 for _ in f)

            current_metrics = parse_jsonl_events(LOG_PATH)
            print(f"[{elapsed/60:.0f}m / {duration_hours*60:.0f}m] "
                  f"alive={not task.done()} log_lines={log_lines} "
                  f"ticks={current_metrics.get('ticks', 0)} "
                  f"opps={current_metrics.get('opportunities', 0)} "
                  f"fills={current_metrics.get('fills', 0)} "
                  f"ws_subbed={current_metrics.get('ws_subscribed', False)} "
                  f"edge_events={current_metrics.get('edge_events', 0)} "
                  f"tasks={len(asyncio.all_tasks())}", flush=True)
            last_report = elapsed

        await asyncio.sleep(check_interval)

    # Stop the bot
    print(f"\n[{datetime.now(timezone.utc).isoformat()}] Stopping bot...")
    if not task.done():
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=20)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass

    # Give it a moment to write final summary
    await asyncio.sleep(3)

    elapsed = time.time() - start_time
    print(f"Session duration: {elapsed:.1f}s ({elapsed/60:.1f}m)")

    # Parse metrics
    metrics = parse_jsonl_events(LOG_PATH)
    metrics["session_duration_s"] = elapsed
    metrics["crashes"] = crashes
    metrics["hung_tasks"] = hung_tasks_detected

    # Save monitor results
    with open(MONITOR_PATH, "w") as f:
        json.dump(metrics, f, indent=2, default=str)

    return metrics


async def _async_monitor():
    """Run the paper trading session and report results."""
    metrics = await run_paper_session(duration_hours=2.0)

    print("\n" + "=" * 70)
    print("TASK A — EXTENDED PAPER TRADING SESSION VALIDATION RESULTS")
    print("=" * 70)
    print(f"Session duration:      {metrics['session_duration_s']:.1f}s ({metrics['session_duration_s']/60:.1f}m)")
    print(f"Crashed:               {metrics['crashes']}")
    print(f"Hung tasks:            {metrics['hung_tasks']}")
    print(f"Total ticks processed: {metrics['ticks']}")
    print(f"Arb opportunities:     {metrics['opportunities']}")
    print(f"Would-lock count:      {metrics['would_lock_count']}")
    print(f"Would-lock PnL forgone:{metrics['would_lock_pnl']:.4f}")
    print(f"Simulated fills:       {metrics['fills']}")
    print(f"Partial fills:         {metrics['partial_fills']}")
    print(f"Partial resolved:      {metrics.get('partial_resolved', 0)}")
    print(f"Missed (rejected):     {metrics['missed']}")
    print(f"Locked PnL:            {metrics['locked_pnl']:.4f}")
    print(f"Residual PnL:          {metrics['residual_pnl']:.4f}")
    print(f"Session PnL:           {metrics['session_pnl']:.4f}")
    print(f"HIGH_MACRO_RISK skips: {metrics['macro_skipped']}")
    print(f"Avg opp->order ms:     {metrics.get('avg_opp_to_order_ms', 'N/A')}")
    print(f"P50 opp->order ms:     {metrics.get('p50_opp_to_order_ms', 'N/A')}")
    print(f"Avg ack ms:            {metrics.get('avg_ack_ms', 'N/A')}")
    print(f"WS subscribed:         {metrics.get('ws_subscribed', False)}")
    print(f"Edge events:           {metrics.get('edge_events', 0)}")
    print(f"FIRE events:           {metrics.get('fire_events', 0)}")
    print(f"FILL events:           {metrics.get('fill_events', 0)}")
    print(f"Window windows seen:   {len(metrics['window_windows'])}")
    print(f"Total log lines:       {metrics['total_log_lines']}")
    if metrics["regime"]:
        print(f"Vol regime:            {metrics['regime']}")
    if metrics["summary"]:
        print(f"\nRaw summary: {metrics['summary']}")
    if metrics["trade_events"]:
        print(f"\nTrade events ({len(metrics['trade_events'])}):")
        for te in metrics["trade_events"][:10]:
            print(f"  - {te}")
    if metrics["errors"]:
        print(f"\nErrors ({len(metrics['errors'])}):")
        for e in metrics["errors"][:10]:
            print(f"  - {e}")
    print(f"\nMonitor data saved to: {MONITOR_PATH}")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(_async_monitor())
