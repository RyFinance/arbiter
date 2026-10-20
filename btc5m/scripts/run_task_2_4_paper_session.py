#!/usr/bin/env python3
"""Run the Tasks 2-4 paper-only validation session and archive its evidence.

This runner deliberately uses the normal application process, CLOB WebSocket,
REST fallback, and paper fill simulation. It never submits an exchange order,
checks geoblocking, or starts any cross-venue component.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = PROJECT_ROOT / "state"


def utc_session_id() -> str:
    return "task_2_4_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def parse_summary(message: str) -> dict[str, float | int]:
    import re

    patterns = {
        "loops": r"loops=(\d+)",
        "opportunities": r"opps=(\d+)",
        "orders_sent": r"sent=(\d+)",
        "fills": r"fills=(\d+)",
        "partial_fills": r"partial=(\d+)",
        "partial_resolved": r"resolved=(\d+)",
        "missed": r"missed=(\d+)",
        "opp_to_order_avg_ms": r"opp2order_avg_ms=([\d.]+)",
        "p50_ms": r"p50_ms=([\d.]+)",
        "p95_ms": r"p95_ms=([\d.]+)",
        "p99_ms": r"p99_ms=([\d.]+)",
        "ack_avg_ms": r"ack_avg_ms=([\d.]+)",
        "stage_reserve_p50_ms": r"stage_reserve_p50_ms=([\d.]+)",
        "stage_submit_p50_ms": r"stage_submit_p50_ms=([\d.]+)",
        "stage_postsubmit_p50_ms": r"stage_postsubmit_p50_ms=([\d.]+)",
        "locked_pnl": r"locked_pnl=(-?[\d.]+)",
        "residual_pnl": r"residual_pnl=(-?[\d.]+)",
        "session_pnl": r"session_pnl=(-?[\d.]+)",
        "would_lock": r"would_lock=(-?[\d.]+)",
    }
    result: dict[str, float | int] = {}
    int_fields = {
        "loops", "opportunities", "orders_sent", "fills", "partial_fills",
        "partial_resolved", "missed",
    }
    for key, pattern in patterns.items():
        match = re.search(pattern, message)
        if match:
            result[key] = int(match.group(1)) if key in int_fields else float(match.group(1))
    return result


def inspect_log(path: Path) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines() if path.exists() else []:
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    summary = {}
    summary_message = None
    miss_reasons: dict[str, int] = {}
    quote_ages: list[float] = []
    execution_timing_samples: list[dict[str, float]] = []
    for event in events:
        message = str(event.get("msg", ""))
        if "session metrics:" in message:
            summary_message = message
            summary = parse_summary(message)
        if message.startswith("EXEC_TIMING "):
            timing = {
                key: float(value) for key, value in re.findall(
                    r"(opp2first_ms|reserve_ms|dual_submit_ms|paper_leg_max_ms|post_submit_ms)=([\d.]+)",
                    message,
                )
            }
            if timing:
                execution_timing_samples.append(timing)
        if event.get("event") != "TRADE":
            continue
        data = event.get("data") or {}
        if event.get("msg") == "missed":
            reason = str(data.get("reason", "unspecified"))
            miss_reasons[reason] = miss_reasons.get(reason, 0) + 1
        if event.get("msg") == "filled" and data.get("age_ms") is not None:
            try:
                quote_ages.append(float(data["age_ms"]))
            except (TypeError, ValueError):
                pass

    return {
        "event_count": len(events),
        "summary": summary,
        "summary_message": summary_message,
        "miss_reasons": miss_reasons,
        "filled_trade_age_ms": quote_ages,
        "filled_trade_ages_within_500ms": bool(quote_ages) and max(quote_ages) <= 500.0,
        "fill_trade_event_count": len(quote_ages),
        "execution_timing_samples_ms": execution_timing_samples,
    }


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_publish(source: Path, destination: Path) -> None:
    """Publish a completed evidence file without exposing a partial dashboard file."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def select_post_halt_probe_state(feed: Any, execution: Any) -> Any | None:
    """Freeze one tradable real-book state before halt tears windows down."""
    state = next((
        candidate for candidate in feed.states.values()
        if candidate.market_id in execution._templates
        and candidate.best_yes_ask is not None
        and candidate.best_no_ask is not None
    ), None)
    # RiskState.halt makes the main task release active windows asynchronously.
    # Keep a scalar snapshot so the later reserve probe cannot race that teardown.
    return copy.copy(state) if state is not None else None


def seal_archive(archive: Path) -> None:
    entries = []
    for path in sorted(p for p in archive.rglob("*") if p.is_file() and p.name != "SHA256SUMS"):
        entries.append(f"{sha256(path)}  {path.relative_to(archive)}")
    manifest = archive / "SHA256SUMS"
    manifest.write_text("\n".join(entries) + "\n", encoding="ascii")
    for path in archive.rglob("*"):
        if path.is_file():
            path.chmod(0o444)
        elif path.is_dir():
            path.chmod(0o555)
    archive.chmod(0o555)


async def wait_until(predicate, timeout_s: float, description: str) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.25)
    raise TimeoutError(f"timed out waiting for {description}")


async def _start_local_alert_sink() -> tuple[asyncio.AbstractServer, list[dict[str, Any]], str]:
    """Start an in-process webhook receiver for paper-only alert verification."""
    deliveries: list[dict[str, Any]] = []

    async def receive(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            headers = await reader.readuntil(b"\r\n\r\n")
            header_text = headers.decode("iso-8859-1")
            length = 0
            for line in header_text.split("\r\n"):
                if line.lower().startswith("content-length:"):
                    length = int(line.split(":", 1)[1].strip())
                    break
            payload = json.loads((await reader.readexactly(length)).decode("utf-8")) if length else {}
            deliveries.append(payload)
            writer.write(b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(receive, host="127.0.0.1", port=0)
    port = server.sockets[0].getsockname()[1]
    return server, deliveries, f"http://127.0.0.1:{port}/paper-alert-sink"


async def run_session(
    duration_minutes: float, disconnect_seconds: float, verify_kill_switch: bool = False,
    paper_fill_source: str = "rest_book",
) -> Path:
    if duration_minutes < 15.0:
        raise ValueError("duration_minutes must be at least 15")
    if disconnect_seconds and disconnect_seconds <= 31.0:
        raise ValueError("disconnect_seconds must be 0 or greater than 31")
    if paper_fill_source not in {"decision_snapshot", "rest_book"}:
        raise ValueError("paper_fill_source must be decision_snapshot or rest_book")

    session_id = utc_session_id()
    archive = STATE_DIR / "paper_session_archives" / session_id
    archive.mkdir(parents=True, exist_ok=False)
    live_log = STATE_DIR / "bot.jsonl"
    session_log = archive / "bot.jsonl"
    alerts_log = STATE_DIR / "alerts.jsonl"
    session_alerts_log = archive / "alerts.jsonl"
    freshness_report = STATE_DIR / "freshness_pnl_report.json"
    prior_log = archive / "prior_bot.jsonl"
    if live_log.exists():
        shutil.copy2(live_log, prior_log)
    if alerts_log.exists():
        shutil.copy2(alerts_log, archive / "prior_alerts.jsonl")
    if freshness_report.exists():
        shutil.copy2(freshness_report, archive / "prior_freshness_pnl_report.json")

    # Write bot and alert evidence directly into the unsealed session archive. The
    # session alerter mirrors each fsync'd record to the append-only operational log,
    # tagged with session_id. A crash cannot erase or strand already-fired alerts.
    session_log.write_text("", encoding="utf-8")
    session_alerts_log.write_text("", encoding="utf-8")
    alert_start_offset = alerts_log.stat().st_size if alerts_log.exists() else 0
    freshness_report.unlink(missing_ok=True)

    # Set before importing project modules; PAPER_TRADE is the safety boundary.
    os.environ["PAPER_TRADE"] = "true"
    os.environ["DRY_RUN"] = "false"
    os.environ["LIVE_SIGNING_VERIFIED"] = "true"
    os.environ["LOG_JSONL"] = str(session_log)
    os.environ["PAPER_FILL_SOURCE"] = paper_fill_source
    # A local receiver makes this an enabled, delivered alert test without
    # contacting any external notification service.
    alert_server, webhook_deliveries, alert_url = await _start_local_alert_sink()
    os.environ["ALERT_WEBHOOK_URL"] = alert_url

    from polymarket_arb import alerting
    from polymarket_arb.price_feed import PriceFeed
    from polymarket_arb.execution import Execution
    from polymarket_arb.risk import RiskState
    import polymarket_arb.kronos_session as kronos_module
    import polymarket_arb.main as main_module

    captured: dict[str, Any] = {}
    feed_created = asyncio.Event()
    original_init = PriceFeed.__init__

    def capture_feed(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        captured["feed"] = self
        feed_created.set()

    PriceFeed.__init__ = capture_feed
    original_execution_init = Execution.__init__

    def capture_execution(self, *args, **kwargs):
        original_execution_init(self, *args, **kwargs)
        captured["execution"] = self

    Execution.__init__ = capture_execution
    original_risk_init = RiskState.__init__

    def capture_risk(self, *args, **kwargs):
        original_risk_init(self, *args, **kwargs)
        captured["risk"] = self

    RiskState.__init__ = capture_risk
    original_forecast = kronos_module.forecast_session_regime

    async def paper_only_regime():
        # This validation is explicitly no-Kronos: retain stable default thresholds
        # without loading a model or making the forecast's candle request.
        return kronos_module.VolRegime(
            regime="MED", atr_14d=0.0, confidence=0.0,
            no_entry_seconds=3.0, max_quote_age_ms=500, source="harness_default",
        )

    kronos_module.forecast_session_regime = paper_only_regime
    original_build_alerter = main_module.build_alerter

    def build_session_alerter():
        return alerting.WebhookAlerter(
            webhook_url=alert_url,
            alerts_path=str(session_alerts_log),
            mirror_alerts_path=str(alerts_log),
            session_id=session_id,
        )

    main_module.build_alerter = build_session_alerter
    started_at = datetime.now(timezone.utc).isoformat()
    started_monotonic = time.monotonic()
    bot_task = asyncio.create_task(main_module.main(), name="task-2-4-paper-bot")
    fault: dict[str, Any] = {"requested_seconds": disconnect_seconds, "enabled": bool(disconnect_seconds)}
    kill_switch = {"requested": verify_kill_switch, "halted": False,
                   "alert_recorded": False, "webhook_delivered": False,
                   "post_halt_rejection_reason": None,
                   "orders_sent_before_post_halt_probe": None,
                   "orders_sent_after_post_halt_probe": None,
                   "post_halt_probe_blocked": False}
    shutdown = {"bot_task_completed": False, "bot_task_timeout": False}
    try:
        await asyncio.wait_for(feed_created.wait(), timeout=180)
        feed = captured["feed"]
        await wait_until(
            lambda: getattr(feed, "_feed", None) is not None and feed._feed.healthy,
            timeout_s=180,
            description="initial CLOB WebSocket subscription",
        )
        await wait_until(
            lambda: any(
                state.combined_ask is not None
                for state in getattr(feed, "states", {}).values()
            ),
            timeout_s=180,
            description="first paired live order book",
        )
        connected_started_at = datetime.now(timezone.utc).isoformat()
        connected_started_monotonic = time.monotonic()
        if disconnect_seconds:
            await asyncio.sleep(2.0)
            fault["started_at_monotonic"] = time.monotonic()
            await feed._feed.force_paper_disconnect(disconnect_seconds)
            await wait_until(
                lambda: not feed._feed.healthy,
                timeout_s=10,
                description="CLOB WebSocket closure",
            )
            fault["observed_down_at_monotonic"] = time.monotonic()
            await asyncio.sleep(disconnect_seconds + 0.5)
            await wait_until(
                lambda: feed._feed.healthy,
                timeout_s=120,
                description="CLOB WebSocket recovery",
            )
            fault["recovered_at_monotonic"] = time.monotonic()
            fault["observed_outage_seconds"] = round(
                fault["recovered_at_monotonic"] - fault["observed_down_at_monotonic"], 3
            )

        remaining = duration_minutes * 60.0 - (time.monotonic() - connected_started_monotonic)
        if remaining > 0:
            try:
                await asyncio.wait_for(asyncio.shield(bot_task), timeout=remaining)
            except asyncio.TimeoutError:
                pass
            else:
                # main() completing before the timeout is NOT necessarily a
                # crash: it also returns cleanly (no exception) on a
                # deliberate SIGINT/SIGTERM — e.g. the dashboard's Stop
                # button terminates this whole process, and main()'s own
                # signal handler exits its hot loop gracefully. main()
                # reports why via its return value ("signal" for that clean
                # path); only treat anything else as the unexpected-exit bug
                # this check exists to catch.
                exit_reason = bot_task.result()
                shutdown["bot_task_completed"] = True
                shutdown["bot_task_exit_reason"] = exit_reason
                if exit_reason != "signal":
                    raise RuntimeError(
                        "paper bot exited before connected observation completed "
                        f"(reason={exit_reason!r})"
                    )
        if verify_kill_switch:
            risk = captured.get("risk")
            if risk is None:
                raise RuntimeError("paper harness could not capture RiskState")
            execution = captured.get("execution")
            if execution is None:
                raise RuntimeError("paper harness could not capture Execution")
            state = select_post_halt_probe_state(feed, execution)
            if state is None:
                raise RuntimeError("no current registered market for pre-halt probe capture")
            # Inject only after the full market-observation interval, so the
            # verification does not halt the PnL sample at session start.
            risk.halt("paper harness kill-switch verification")
            await wait_until(
                lambda: any(
                    json.loads(line).get("kind") == "kill_switch"
                    for line in session_alerts_log.read_text(encoding="utf-8").splitlines()
                    if line
                ),
                timeout_s=10,
                description="durable kill-switch alert",
            )
            await wait_until(lambda: bool(webhook_deliveries), timeout_s=10,
                             description="local webhook delivery")
            kill_switch.update(halted=bool(risk.halted), alert_recorded=True,
                               webhook_delivered=True)
            before_sent = execution.metrics.orders_sent
            # Exercise the production reservation gate with the current real book.
            # Calling execute_dual_buy here would require a qualifying arb edge and
            # could stop at snapshot_no_edge before reaching the kill switch. Direct
            # reserve uses real prices/costs and proves the source-of-truth halt gate.
            post_halt_reason = await risk.reserve(
                state,
                state.best_yes_ask * 10.0,
                state.best_no_ask * 10.0,
                snapshot_combined=state.combined_ask,
            )
            after_sent = execution.metrics.orders_sent
            blocked = before_sent == after_sent and post_halt_reason == "halted/emergency-stop"
            kill_switch.update(
                post_halt_rejection_reason=post_halt_reason,
                orders_sent_before_post_halt_probe=before_sent,
                orders_sent_after_post_halt_probe=after_sent,
                post_halt_probe_blocked=blocked,
            )
            if not blocked:
                raise RuntimeError(
                    f"post-halt execution probe failed: reason={post_halt_reason!r} "
                    f"orders={before_sent}->{after_sent}"
                )
    finally:
        PriceFeed.__init__ = original_init
        Execution.__init__ = original_execution_init
        RiskState.__init__ = original_risk_init
        kronos_module.forecast_session_regime = original_forecast
        main_module.build_alerter = original_build_alerter
        if not bot_task.done():
            bot_task.cancel()
        try:
            await asyncio.wait_for(bot_task, timeout=45)
        except asyncio.CancelledError:
            pass
        except asyncio.TimeoutError:
            # The archive is primary evidence. Do not leave an entire paper
            # session unreported if a background cleanup task ignores cancel.
            shutdown["bot_task_timeout"] = True
        shutdown["bot_task_completed"] = bot_task.done()
        alert_server.close()
        await alert_server.wait_closed()

    finished_at = datetime.now(timezone.utc).isoformat()
    # Publish the completed session atomically. Alert records already live in the
    # archive and in the append-only operational mirror.
    atomic_publish(session_log, live_log)
    session_alert_rows = [
        json.loads(line)
        for line in session_alerts_log.read_text(encoding="utf-8").splitlines()
        if line
    ]
    freshness_result = subprocess.run(
        [sys.executable, "scripts/freshness_pnl_report.py", "--print"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    (archive / "freshness_pnl_report_console.txt").write_text(
        freshness_result.stdout + freshness_result.stderr, encoding="utf-8"
    )
    dashboard_metrics: dict[str, Any] = {}
    if freshness_report.exists():
        shutil.copy2(freshness_report, archive / "freshness_pnl_report.json")
        dashboard_metrics = json.loads(freshness_report.read_text(encoding="utf-8"))
    from calc_pnl_convergence import compute_pnl_convergence, parse_bot_jsonl
    pnl_inputs = parse_bot_jsonl(session_log)
    pnl_convergence = compute_pnl_convergence(
        pnl_inputs["fills"], pnl_inputs["missed"], order_size=10.0,
        partials=pnl_inputs["partials"],
    )
    (archive / "pnl_convergence.json").write_text(
        json.dumps(pnl_convergence, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    inspection = inspect_log(session_log)
    severe_alerts = [
        row for row in session_alert_rows
        if row.get("kind") == "ws_disconnect_severe"
    ]
    recovery_messages = [
        event.get("msg") for event in (
            json.loads(line) for line in session_log.read_text(encoding="utf-8").splitlines() if line
        ) if "WebSocket recovered after" in str(event.get("msg", ""))
    ]
    report = {
        "session_id": session_id,
        "paper_only": True,
        "kronos_started": False,
        "geoblock_check_performed": False,
        "cross_venue_started": False,
        "real_orders_submitted": False,
        "paper_fill_source": paper_fill_source,
        "alerting": {
            "enabled": True,
            "webhook": "local-paper-alert-sink",
            "delivery_count": len(webhook_deliveries),
            "operational_log": str(alerts_log),
            "operational_log_start_offset": alert_start_offset,
            "operational_log_end_offset": alerts_log.stat().st_size if alerts_log.exists() else 0,
            "session_alert_records": len(session_alert_rows),
            "kill_switch": kill_switch,
        },
        "shutdown": shutdown,
        "started_at": started_at,
        "connected_started_at": connected_started_at,
        "finished_at": finished_at,
        "duration_seconds": round(time.monotonic() - started_monotonic, 3),
        "connected_observation_seconds": round(
            time.monotonic() - connected_started_monotonic, 3
        ),
        "transport_fault": fault,
        "ws_disconnect_severe_count": len(severe_alerts),
        "ws_disconnect_severe_records": severe_alerts,
        "ws_recovery_messages": recovery_messages,
        "freshness_report_exit_code": freshness_result.returncode,
        "dashboard_metrics_after_session": dashboard_metrics,
        "event_level_pnl_convergence": pnl_convergence,
        "metrics": inspection,
    }
    (archive / "session_report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    seal_archive(archive)
    return archive


def finalize_existing_archive(
    archive: Path,
    duration_seconds: float | None,
    failure_reason: str | None = None,
    paper_fill_source: str = "rest_book",
) -> None:
    """Recover an archive if a post-session report step failed after log capture."""
    session_log = archive / "bot.jsonl"
    alerts_log = archive / "alerts.jsonl"
    if not session_log.exists() or not alerts_log.exists():
        raise FileNotFoundError("archive must contain bot.jsonl and alerts.jsonl")
    inspection = inspect_log(session_log)
    dashboard_metrics: dict[str, Any] = {}
    freshness_report = archive / "freshness_pnl_report.json"
    if freshness_report.exists():
        dashboard_metrics = json.loads(freshness_report.read_text(encoding="utf-8"))
    severe_alerts = [
        row for row in (json.loads(line) for line in alerts_log.read_text(encoding="utf-8").splitlines() if line)
        if row.get("kind") == "ws_disconnect_severe"
    ]
    alert_rows = [
        json.loads(line) for line in alerts_log.read_text(encoding="utf-8").splitlines() if line
    ]
    from calc_pnl_convergence import compute_pnl_convergence, parse_bot_jsonl
    pnl_inputs = parse_bot_jsonl(session_log)
    pnl_convergence = compute_pnl_convergence(
        pnl_inputs["fills"], pnl_inputs["missed"], order_size=10.0,
        partials=pnl_inputs["partials"],
    )
    (archive / "pnl_convergence.json").write_text(
        json.dumps(pnl_convergence, indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )
    match = re.fullmatch(r"task_2_4_(\d{8}T\d{6}Z)", archive.name)
    started_at = (
        datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ")
        .replace(tzinfo=timezone.utc)
        .isoformat()
        if match
        else None
    )
    report = {
        "session_id": archive.name,
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "connected_observation_seconds": 0.0,
        "paper_only": True,
        "paper_fill_source": paper_fill_source,
        "real_orders_submitted": False,
        "cross_venue_started": False,
        "geoblock_check_performed": False,
        "kronos_started": False,
        "duration_seconds": duration_seconds,
        "recovered_archive_report": True,
        "run_status": "failed" if failure_reason else "recovered",
        "failure_reason": failure_reason,
        "shutdown": {
            "bot_task_completed": False if failure_reason else True,
            "bot_task_timeout": bool(failure_reason and "timeout" in failure_reason.lower()),
        },
        "alerting": {
            "session_alert_records": len(alert_rows),
            "kill_switch_records": sum(row.get("kind") == "kill_switch" for row in alert_rows),
        },
        "transport_fault": {"enabled": False, "requested_seconds": 0.0},
        "ws_disconnect_severe_count": len(severe_alerts),
        "ws_disconnect_severe_records": severe_alerts,
        "dashboard_metrics_after_session": dashboard_metrics,
        "event_level_pnl_convergence": pnl_convergence,
        "metrics": inspection,
    }
    (archive / "session_report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    seal_archive(archive)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run an independent Tasks 2-4 paper session.")
    parser.add_argument("--duration-minutes", type=float, default=30.5)
    parser.add_argument("--disconnect-seconds", type=float, default=0.0)
    parser.add_argument("--verify-kill-switch", action="store_true")
    parser.add_argument(
        "--paper-fill-source", choices=("decision_snapshot", "rest_book"),
        default="rest_book",
    )
    parser.add_argument("--finalize-archive", type=Path)
    parser.add_argument("--duration-seconds", type=float)
    parser.add_argument("--failure-reason", type=str)
    args = parser.parse_args()
    if args.finalize_archive:
        finalize_existing_archive(
            args.finalize_archive,
            args.duration_seconds,
            args.failure_reason,
            args.paper_fill_source,
        )
        print(args.finalize_archive)
        return 0
    archive = asyncio.run(run_session(
        args.duration_minutes, args.disconnect_seconds, args.verify_kill_switch,
        args.paper_fill_source,
    ))
    print(archive)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
