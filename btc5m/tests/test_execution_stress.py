from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

from polymarket_arb.alerting import (
    ALERT_KILL_SWITCH,
    ALERT_REPEATED_REJECTIONS,
    ALERT_WS_DISCONNECT_SEVERE,
    Heartbeat,
    WebhookAlerter,
)
from polymarket_arb.config import SETTINGS
from polymarket_arb.dashboard_status import bot_liveness_status
from polymarket_arb.execution import Execution
from polymarket_arb.metrics import Metrics
from polymarket_arb.models import MarketState
from polymarket_arb.price_feed import PriceFeed, WS_DISCONNECT_SEVERE_THRESHOLD_S
from polymarket_arb.risk import RiskState


class _PaperFaultClient:
    """Paper-only adapter: one filled leg, or fully rejected submissions."""

    def __init__(self, reject: bool = False):
        self.reject = reject
        self.submits: list[tuple[str, str]] = []
        self.positions: dict[str, float] = {}

    async def submit_order(self, token_id, price, size, side="BUY"):
        self.submits.append((token_id, side))
        if self.reject or (token_id == "no" and side == "BUY"):
            return None
        self.positions[token_id] = self.positions.get(token_id, 0.0) + (
            size if side == "BUY" else -size
        )
        return f"paper-{len(self.submits)}"

    async def is_filled(self, order_id, token_id, gap_ms=0):
        return order_id is not None

    def get_fill_ratio(self, order_id):
        return 1.0 if order_id is not None else 0.0

    async def cancel_order(self, token_id, missing_order_id=None):
        return True


class _ExitRejectingPaperClient(_PaperFaultClient):
    async def submit_order(self, token_id, price, size, side="BUY"):
        if side == "SELL":
            self.submits.append((token_id, side))
            return None
        return await super().submit_order(token_id, price, size, side)


def _state() -> MarketState:
    now = time.time()
    return MarketState(
        market_id="paper-stress", question="paper stress", yes_token_id="yes", no_token_id="no",
        best_yes_ask=0.48, best_no_ask=0.70, best_yes_bid=0.0, best_no_bid=0.20,
        yes_size=500.0, no_size=500.0, last_update_ts=now, yes_ts=now, no_ts=now,
    )


def test_paper_stress_partial_unwinds_kills_and_persists_alerts(tmp_path):
    async def run():
        tuned = dataclasses.replace(
            SETTINGS, max_leg_gap_ms=0, partial_loss_cap_usd=1.0,
            max_position_per_market=1_000.0, max_total_capital=1_000.0,
        )
        alerts = tmp_path / "alerts.jsonl"
        heartbeat = tmp_path / "HEARTBEAT"
        alerter = WebhookAlerter(dedup_window_s=0.0, alerts_path=str(alerts))
        with patch("polymarket_arb.execution.SETTINGS", tuned), \
             patch("polymarket_arb.risk.SETTINGS", tuned), \
             patch("polymarket_arb.price_feed.SETTINGS", tuned):
            client = _PaperFaultClient()
            risk = RiskState(balance=1_000.0, alerter=alerter)
            execution = Execution(client, risk, Metrics(), alerter)
            state = _state()
            execution.register(state, size=10.0)
            await execution.execute_dual_buy(state, 0.96, time.monotonic(), yes_p=0.47, no_p=0.49)
            await execution._check_fills()

            assert client.positions == {"yes": 0.0}
            assert any(side == "SELL" for _, side in client.submits)
            assert risk.total_deployed == 0.0
            assert risk.halted is True
            submits_before_halt = len(client.submits)
            await execution.execute_dual_buy(state, 0.96, time.monotonic(), yes_p=0.47, no_p=0.49)
            assert len(client.submits) == submits_before_halt

            rejected = _PaperFaultClient(reject=True)
            rejection_execution = Execution(
                rejected, RiskState(balance=1_000.0), Metrics(), alerter,
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
            os.utime(heartbeat, (time.time() - 20.0, time.time() - 20.0))
            assert bot_liveness_status(str(heartbeat), 1.0)["state"] == "stale/disconnected"

        records = [json.loads(line) for line in alerts.read_text().splitlines()]
        kinds = {record["kind"] for record in records}
        assert {ALERT_KILL_SWITCH, ALERT_REPEATED_REJECTIONS, ALERT_WS_DISCONNECT_SEVERE} <= kinds
        return alerts, len(records)

    alerts, before_restart = asyncio.run(run())
    WebhookAlerter(alerts_path=str(alerts)).alert("paper_session_restarted", "controlled restart")
    records = [json.loads(line) for line in alerts.read_text().splitlines()]
    assert len(records) == before_restart + 1
    assert records[-1]["kind"] == "paper_session_restarted"


def test_dashboard_status_is_explicit_when_heartbeat_is_missing_or_stale(tmp_path):
    missing = bot_liveness_status(str(tmp_path / "missing"), 1.0)
    assert missing["state"] == "disconnected"
    heartbeat = tmp_path / "HEARTBEAT"
    heartbeat.write_text("heartbeat")
    os.utime(heartbeat, (time.time() - 5.0, time.time() - 5.0))
    assert bot_liveness_status(str(heartbeat), 1.0)["state"] == "stale/disconnected"


def test_stress_runner_proves_sigkill_alert_persistence(tmp_path):
    """The archive runner must use an actual SIGKILL, not a synthetic stale heartbeat."""
    root = Path(__file__).resolve().parent.parent
    env = {**os.environ, "PAPER_TRADE": "true"}
    result = subprocess.run(
        [
            sys.executable, str(root / "scripts" / "run_execution_stress_session.py"),
            "--archive-root", str(tmp_path),
        ],
        cwd=root,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    archive = Path(json.loads(result.stdout)["archive"])
    evidence = json.loads((archive / "evidence.json").read_text())
    assert evidence["crash"]["returncode"] == -9
    assert evidence["crash"]["pre_crash_alert_data_preserved"] is True
    assert evidence["partial_unwind"]["explicit_unwind_submitted"] is True
    assert evidence["alerts"]["fault_alerts_independent"] is True
    assert evidence["dashboard_after_process_death"]["state"] == "stale/disconnected"


def test_unconfirmed_partial_exit_halts_without_claiming_a_flat_position():
    async def run():
        tuned = dataclasses.replace(
            SETTINGS, max_leg_gap_ms=0, max_position_per_market=1_000.0,
            max_total_capital=1_000.0,
        )
        with patch("polymarket_arb.execution.SETTINGS", tuned), \
             patch("polymarket_arb.risk.SETTINGS", tuned):
            client = _ExitRejectingPaperClient()
            risk = RiskState(balance=1_000.0)
            execution = Execution(client, risk, Metrics())
            state = _state()
            execution.register(state, size=10.0)
            await execution.execute_dual_buy(state, 0.96, time.monotonic(), yes_p=0.47, no_p=0.49)
            await execution._check_fills()
            return client, risk

    client, risk = asyncio.run(run())
    assert risk.halted is True
    assert client.positions == {"yes": 10.0}
    # With per-leg ratio-scaled commit: only YES filled (ratio=1.0), NO
    # submitted as None (ratio=0.0). committed_cost = 0.48*10*1.0 + 0.50*10*0.0 = 4.7
    assert abs(risk.total_deployed - 4.7) < 1e-9
