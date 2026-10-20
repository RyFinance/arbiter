"""TASK E2 — Synthetic alert trigger harness.

Run this script to fire synthetic triggers for each of the three alert conditions
and verify they land in state/alerts.jsonl:

    python scripts/e2_alert_test_harness.py

It does NOT require the bot to be running and does NOT touch any exchange.
It writes real records to state/alerts.jsonl (via WebhookAlerter._write_alert_record)
so operators can grep/tail them. The webhook POST is mocked (no network needed).

Alert types tested:
  1. kill_switch             — simulated PARTIAL_LOSS_CAP_USD breach
  2. repeated_rejections     — simulated 3+ consecutive order rejections
  3. ws_disconnect_severe    — simulated WS down >30s
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

# Ensure project root is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from polymarket_arb.alerting import (  # noqa: E402
    ALERT_KILL_SWITCH,
    ALERT_REPEATED_REJECTIONS,
    ALERT_WS_DISCONNECT_SEVERE,
    WebhookAlerter,
)
from polymarket_arb.config import SETTINGS  # noqa: E402
from polymarket_arb.execution import Execution  # noqa: E402
from polymarket_arb.metrics import Metrics  # noqa: E402
from polymarket_arb.models import MarketState  # noqa: E402
from polymarket_arb.price_feed import (  # noqa: E402
    PriceFeed,
    WS_DISCONNECT_SEVERE_THRESHOLD_S,
)
from polymarket_arb.risk import RiskState  # noqa: E402


ALERTS_LOG = Path(SETTINGS.log_jsonl).parent / "alerts.jsonl"


def _read_alerts() -> list[dict]:
    if not ALERTS_LOG.exists():
        return []
    out = []
    with ALERTS_LOG.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return out


class _NoNetworkAlerter(WebhookAlerter):
    """Alerter that captures _send calls instead of making HTTP requests."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.sent: list[tuple] = []

    async def _send(self, kind: str, message: str, fields: dict):
        self.sent.append((kind, message, fields))


async def trigger_kill_switch(al: _NoNetworkAlerter) -> None:
    """Simulate a PARTIAL_LOSS_CAP_USD breach via execution._after_residual."""
    risk = RiskState(balance=100_000.0)
    metrics = Metrics()

    class _FakeClient:
        async def submit_order(self, *a, **kw): return "id"
        async def cancel_order(self, *a, **kw): return True
        async def is_filled(self, *a, **kw): return True

    exec_ = Execution(client=_FakeClient(), risk=risk, metrics=metrics, alerter=al)
    st = MarketState(
        market_id="synth-ks", question="Q", yes_token_id="0xYES", no_token_id="0xNO",
        best_yes_ask=0.48, best_no_ask=0.50, best_yes_bid=0.47, best_no_bid=0.49,
        last_update_ts=time.time(), yes_ts=time.time(), no_ts=time.time(),
    )
    cap = SETTINGS.partial_loss_cap_usd
    exec_._after_residual(
        st=st, combined=0.98, size=10.0,
        residual=-(cap + 10.0), age_ms=10.0, band="fresh", action="exit",
        released_cost=9.8,
    )


async def trigger_repeated_rejections(al: _NoNetworkAlerter) -> None:
    """Simulate 3+ consecutive order rejections via execute_dual_buy."""
    risk = RiskState(balance=100_000.0)
    metrics = Metrics()

    class _RejectClient:
        async def submit_order(self, *a, **kw): return None  # reject
        async def cancel_order(self, *a, **kw): return True

    exec_ = Execution(client=_RejectClient(), risk=risk, metrics=metrics, alerter=al)
    st = MarketState(
        market_id="synth-rej", question="Q", yes_token_id="0xYES", no_token_id="0xNO",
        best_yes_ask=0.48, best_no_ask=0.50, yes_size=100.0, no_size=100.0,
        best_yes_bid=0.47, best_no_bid=0.49,
        last_update_ts=time.time(), yes_ts=time.time(), no_ts=time.time(),
    )
    exec_.register(st, size=10.0)
    for _ in range(3):
        await exec_.execute_dual_buy(
            st=st, combined=0.98, decision_ts=time.monotonic(),
            age_ms=10.0, band="fresh", yes_p=0.48, no_p=0.50,
        )


async def trigger_ws_disconnect(al: _NoNetworkAlerter) -> None:
    """Simulate WS disconnect >30s via PriceFeed._poll_loop."""
    from unittest.mock import AsyncMock, MagicMock, patch

    states: dict = {}

    class _StopAfterOne:
        """Context to run one iteration of _poll_loop."""

        def __init__(self, pf: PriceFeed):
            self.pf = pf

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            self.pf._stop = True

    pf = PriceFeed(states=states, on_update=MagicMock(), alerter=al)
    fake_feed = MagicMock()
    fake_feed.healthy = False
    pf._feed = fake_feed
    pf._ws_down_since = time.monotonic() - WS_DISCONNECT_SEVERE_THRESHOLD_S - 5.0  # 35s

    async def _noop(*a, **kw):
        return None, None

    with patch.object(pf, "_all_token_ids", return_value=["0xSYNTH"]), \
         patch("polymarket_arb.price_feed.fetch_book", new=AsyncMock(side_effect=_noop)):
        task = asyncio.create_task(pf._poll_loop())
        await asyncio.sleep(0.1)
        pf._stop = True
        await task


async def main() -> int:
    al = _NoNetworkAlerter(
        webhook_url="http://test-webhook.example.com/hooks/arb",
        dedup_window_s=0.0,
    )

    before = len(_read_alerts())

    print("Firing synthetic alerts...")
    await trigger_kill_switch(al)
    print(f"  [1/3] kill_switch fired via _after_residual (PARTIAL_LOSS_CAP breach)")

    await trigger_repeated_rejections(al)
    print(f"  [2/3] repeated_rejections fired (3+ consecutive None subissions)")

    await trigger_ws_disconnect(al)
    print(f"  [3/3] ws_disconnect_severe fired (WS down >30s in poll loop)")

    # Verify alerts landed in alerts.jsonl
    after = _read_alerts()
    new_alerts = after[before:]

    kinds_seen = set()
    for rec in new_alerts:
        kinds_seen.add(rec.get("kind"))

    print(f"\nResults: {len(new_alerts)} new alerts written to {ALERTS_LOG}")
    for kind in [ALERT_KILL_SWITCH, ALERT_REPEATED_REJECTIONS, ALERT_WS_DISCONNECT_SEVERE]:
        status = "OK" if kind in kinds_seen else "MISSING"
        print(f"  {kind}: {status}")

    if not {ALERT_KILL_SWITCH, ALERT_REPEATED_REJECTIONS, ALERT_WS_DISCONNECT_SEVERE} <= kinds_seen:
        print("\nFAIL: not all alert types were recorded")
        return 1

    print("\nPASS: all three alert types fired and recorded in alerts.jsonl")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
