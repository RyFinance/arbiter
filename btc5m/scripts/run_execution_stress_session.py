"""Controlled PAPER_TRADE execution stress session.

This runner intentionally uses the production execution, risk, alert, heartbeat, and
PriceFeed failure paths with a paper-only order adapter. It never creates an SDK order,
uses no cross-venue or Kronos component, and does not send a chain transaction.
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import hashlib
import json
import os
import signal
import shutil
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from polymarket_arb.alerting import (  # noqa: E402
    ALERT_KILL_SWITCH,
    ALERT_REPEATED_REJECTIONS,
    ALERT_WS_DISCONNECT_SEVERE,
    Heartbeat,
    WebhookAlerter,
)
from polymarket_arb.config import SETTINGS  # noqa: E402
from polymarket_arb.dashboard_status import bot_liveness_status  # noqa: E402
from polymarket_arb.execution import Execution  # noqa: E402
from polymarket_arb.logger import BotLogger  # noqa: E402
from polymarket_arb.metrics import Metrics  # noqa: E402
from polymarket_arb.models import MarketState  # noqa: E402
from polymarket_arb.price_feed import PriceFeed, WS_DISCONNECT_SEVERE_THRESHOLD_S  # noqa: E402
from polymarket_arb.risk import RiskState  # noqa: E402


class PaperFaultClient:
    """Order adapter for a controlled, no-network PAPER_TRADE failure session."""

    def __init__(self, reject: bool = False):
        self.reject = reject
        self.submits: list[dict] = []
        self.positions: dict[str, float] = {}

    async def submit_order(self, token_id, price, size, side="BUY"):
        self.submits.append({"token_id": token_id, "price": price, "size": size, "side": side})
        if self.reject or (token_id == "paper-no" and side == "BUY"):
            return None
        self.positions[token_id] = self.positions.get(token_id, 0.0) + (
            size if side == "BUY" else -size
        )
        return f"paper-order-{len(self.submits)}"

    async def is_filled(self, order_id, token_id, gap_ms=0):
        return order_id is not None

    def get_fill_ratio(self, order_id):
        return 1.0 if order_id is not None else 0.0

    async def cancel_order(self, token_id, missing_order_id=None):
        return True


def stress_state() -> MarketState:
    now = time.time()
    return MarketState(
        market_id="paper-execution-stress", question="Controlled paper stress",
        yes_token_id="paper-yes", no_token_id="paper-no",
        best_yes_ask=0.48, best_no_ask=0.70, best_yes_bid=0.0, best_no_bid=0.20,
        yes_size=500.0, no_size=500.0, last_update_ts=now, yes_ts=now, no_ts=now,
    )


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def restart_probe(alerts_path: Path) -> int:
    WebhookAlerter(alerts_path=str(alerts_path)).alert(
        "paper_session_restarted", "controlled paper process restart completed",
    )
    return 0


def worker_probe(archive: Path, ready_path: Path) -> int:
    """Run the controlled paper session, then wait for the parent SIGKILL probe."""
    evidence = asyncio.run(run_session(archive))
    alerts_path = archive / "alerts.jsonl"
    evidence["alerts_before_crash"] = len(read_jsonl(alerts_path))
    (archive / "worker_evidence.json").write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )
    ready_path.write_text("ready\n", encoding="utf-8")
    while True:
        time.sleep(1.0)


async def run_session(archive: Path) -> dict:
    bot_log = archive / "bot.jsonl"
    alerts_log = archive / "alerts.jsonl"
    heartbeat = archive / "HEARTBEAT"
    logger = BotLogger(str(bot_log), level="INFO")
    alerter = WebhookAlerter(dedup_window_s=0.0, alerts_path=str(alerts_log))
    tuned = dataclasses.replace(
        SETTINGS, max_leg_gap_ms=0, partial_loss_cap_usd=1.0,
        max_position_per_market=1_000.0, max_total_capital=1_000.0,
    )
    import polymarket_arb.alerting as alerting_mod
    import polymarket_arb.execution as execution_mod
    import polymarket_arb.price_feed as price_feed_mod
    import polymarket_arb.risk as risk_mod
    page_agent_module, page_agent_ok = risk_mod._maybe_page_agent()
    if not page_agent_ok or page_agent_module is None:
        raise RuntimeError("execution stress requires the macro-state schema")
    # The production macro gate is fail-closed.  A zero/default timestamp is not a
    # neutral fixture: it is an invalid state and also causes the fallback alerter to
    # write outside this hermetic archive.  Use an explicitly fresh empty calendar.
    valid_empty_macro_state = page_agent_module.MacroEventState.from_dict({
        "events": [],
        "last_update": time.time(),
        "high_risk_windows": [],
    })

    with patch.object(alerting_mod, "log", logger), \
         patch.object(execution_mod, "log", logger), \
         patch.object(price_feed_mod, "log", logger), \
         patch.object(risk_mod, "log", logger), \
         patch.object(risk_mod, "_read_macro_state", lambda: valid_empty_macro_state), \
         patch.object(risk_mod, "_macro_cache", None), \
         patch.object(risk_mod, "_macro_cache_epoch", 0.0), \
         patch.object(risk_mod, "_macro_state_error", None), \
         patch.object(execution_mod, "SETTINGS", tuned), \
         patch.object(risk_mod, "SETTINGS", tuned), \
         patch.object(price_feed_mod, "SETTINGS", tuned):
        client = PaperFaultClient()
        risk = RiskState(balance=1_000.0, alerter=alerter)
        execution = Execution(client, risk, Metrics(), alerter)
        state = stress_state()
        execution.register(state, size=10.0)
        await execution.execute_dual_buy(state, 0.96, time.monotonic(), yes_p=0.47, no_p=0.49)
        await execution._check_fills()
        partial_proof = {
            "positions": client.positions,
            "total_deployed": risk.total_deployed,
            "halted": risk.halted,
            "unwind_orders": [submit for submit in client.submits if submit["side"] != "BUY"],
            "explicit_unwind_submitted": any(
                submit["side"] != "BUY" for submit in client.submits
            ),
            "no_naked_position": all(abs(quantity) < 1e-9 for quantity in client.positions.values()),
        }
        submits_before_halt = len(client.submits)
        await execution.execute_dual_buy(state, 0.96, time.monotonic(), yes_p=0.47, no_p=0.49)
        kill_switch_proof = {
            "submits_before_halt": submits_before_halt,
            "submits_after_halt_attempt": len(client.submits),
            "new_submits_blocked": len(client.submits) == submits_before_halt,
        }

        rejecting_client = PaperFaultClient(reject=True)
        rejection_execution = Execution(
            rejecting_client, RiskState(balance=1_000.0), Metrics(), alerter,
        )
        rejection_execution.register(state, size=10.0)
        for _ in range(3):
            await rejection_execution.execute_dual_buy(
                state, 0.96, time.monotonic(), yes_p=0.47, no_p=0.49,
            )
            await rejection_execution._check_fills()

        feed = PriceFeed({}, lambda _: None, alerter)
        feed._feed = type("DisconnectedFeed", (), {"healthy": False})()
        feed._ws_down_since = time.monotonic() - WS_DISCONNECT_SEVERE_THRESHOLD_S - 1.0
        feed._record_ws_unhealthy()

        Heartbeat(str(heartbeat), enabled=True).touch()

    logger.close()
    before_restart = len(read_jsonl(alerts_log))
    return {
        "paper_only": True,
        "live_transactions": 0,
        "crossvenue_calls": 0,
        "kronos_calls": 0,
        "partial_unwind": partial_proof,
        "kill_switch": kill_switch_proof,
        "rejection_submits": len(rejecting_client.submits),
        "alerts_before_restart": before_restart,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--restart-probe", action="store_true")
    parser.add_argument("--worker-probe", action="store_true")
    parser.add_argument("--alerts", type=Path)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--ready", type=Path)
    parser.add_argument("--archive-root", type=Path)
    args = parser.parse_args(argv)
    if args.restart_probe:
        if args.alerts is None:
            parser.error("--alerts is required with --restart-probe")
        return restart_probe(args.alerts)
    if args.worker_probe:
        if args.archive is None or args.ready is None:
            parser.error("--archive and --ready are required with --worker-probe")
        if os.getenv("PAPER_TRADE", "").lower() not in {"1", "true", "yes", "on"}:
            raise SystemExit("Refusing to run worker: set PAPER_TRADE=true.")
        return worker_probe(args.archive, args.ready)
    if os.getenv("PAPER_TRADE", "").lower() not in {"1", "true", "yes", "on"}:
        raise SystemExit("Refusing to run: set PAPER_TRADE=true.")

    archive_root = args.archive_root or ROOT / "state" / "paper_session_archives"
    archive = archive_root / f"execution_stress_{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
    archive.mkdir(parents=True, exist_ok=False)
    alerts_path = archive / "alerts.jsonl"
    heartbeat_path = archive / "HEARTBEAT"
    ready_path = archive / "WORKER_READY"
    worker = subprocess.Popen(
        [
            sys.executable, str(Path(__file__).resolve()), "--worker-probe",
            "--archive", str(archive), "--ready", str(ready_path),
        ],
        cwd=ROOT,
    )
    deadline = time.monotonic() + 10.0
    while not ready_path.exists() and worker.poll() is None and time.monotonic() < deadline:
        time.sleep(0.02)
    if not ready_path.exists():
        if worker.poll() is None:
            worker.kill()
            worker.wait(timeout=5)
        raise RuntimeError(f"paper worker did not become ready (returncode={worker.returncode})")

    evidence = json.loads((archive / "worker_evidence.json").read_text(encoding="utf-8"))
    pre_crash_bytes = alerts_path.read_bytes()
    pre_crash_sha256 = hashlib.sha256(pre_crash_bytes).hexdigest()
    pre_crash_count = len(read_jsonl(alerts_path))
    os.kill(worker.pid, signal.SIGKILL)
    worker.wait(timeout=5)
    time.sleep(0.1)
    post_crash_bytes = alerts_path.read_bytes()
    dashboard = bot_liveness_status(str(heartbeat_path), 0.05)
    evidence["crash"] = {
        "pid": worker.pid,
        "signal": "SIGKILL (-9)",
        "returncode": worker.returncode,
        "alerts_before_crash": pre_crash_count,
        "alerts_after_crash": len(read_jsonl(alerts_path)),
        "pre_crash_sha256": pre_crash_sha256,
        "post_crash_sha256": hashlib.sha256(post_crash_bytes).hexdigest(),
        "pre_crash_alert_data_preserved": post_crash_bytes == pre_crash_bytes,
    }
    evidence["dashboard_after_process_death"] = dashboard
    restart = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--restart-probe", "--alerts", str(alerts_path)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    records = read_jsonl(alerts_path)
    kinds = {record.get("kind") for record in records}
    expected = {ALERT_KILL_SWITCH, ALERT_REPEATED_REJECTIONS, ALERT_WS_DISCONNECT_SEVERE}
    alert_counts = {kind: sum(record.get("kind") == kind for record in records) for kind in expected}
    evidence["restart"] = {
        "returncode": restart.returncode,
        "alerts_after_restart": len(records),
        "persisted": len(records) == evidence["alerts_before_restart"] + 1,
        "restart_record_present": "paper_session_restarted" in kinds,
    }
    evidence["alerts"] = {
        "expected": sorted(expected),
        "seen": sorted(kinds),
        "counts": alert_counts,
        "independent": expected <= kinds,
        "fault_alerts_independent": (
            alert_counts[ALERT_REPEATED_REJECTIONS] >= 1
            and alert_counts[ALERT_WS_DISCONNECT_SEVERE] >= 1
        ),
    }
    evidence["pass"] = all([
        evidence["partial_unwind"]["no_naked_position"],
        evidence["partial_unwind"]["explicit_unwind_submitted"],
        evidence["partial_unwind"]["total_deployed"] == 0.0,
        evidence["kill_switch"]["new_submits_blocked"],
        evidence["alerts"]["independent"],
        evidence["alerts"]["fault_alerts_independent"],
        evidence["crash"]["returncode"] == -signal.SIGKILL,
        evidence["crash"]["pre_crash_alert_data_preserved"],
        evidence["restart"]["persisted"],
        evidence["restart"]["restart_record_present"],
        evidence["dashboard_after_process_death"]["state"] == "stale/disconnected",
    ])
    (archive / "evidence.json").write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
    shutil.copy2(ROOT / "docs" / "paper_trade_sdk_divergence_audit.md", archive / "paper_trade_sdk_divergence_audit.md")
    checksums = [f"{sha256(path)}  {path.name}" for path in sorted(archive.iterdir()) if path.is_file()]
    (archive / "SHA256SUMS").write_text("\n".join(checksums) + "\n")
    print(json.dumps({"archive": str(archive), "pass": evidence["pass"]}, indent=2))
    return 0 if evidence["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
