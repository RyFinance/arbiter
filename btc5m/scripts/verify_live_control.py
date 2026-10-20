#!/usr/bin/env python3
"""Runtime proof that both control states cannot reach a live exchange order POST."""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
from typing import Any

from polymarket_arb.clients.trading_client import TradingClient
from polymarket_arb.live_control import LiveTradingControl


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "state" / "live_control_verification_2026-08-15.json"


class ExchangePostSentinel:
    """Fails loudly if any code tries to access an exchange SDK order method."""

    def __init__(self) -> None:
        self.order_post_attempts = 0

    def create_and_post_order(self, *args: Any, **kwargs: Any) -> None:
        self.order_post_attempts += 1
        raise AssertionError("exchange order POST must be unreachable")


async def _probe(control: LiveTradingControl, enabled: bool) -> dict[str, Any]:
    control.set_operator_enabled(
        enabled,
        actor="runtime_verifier",
        reason=f"verify operator_enabled={enabled} remains non-posting",
    )
    sentinel = ExchangePostSentinel()
    client = TradingClient.__new__(TradingClient)
    client.s = SimpleNamespace(live=True)
    client._paper_trade = False
    client._enforce_live_control = True
    client._live_control = control
    client._clob_client = sentinel
    client._order_modes = {}
    result = await client.submit_order("verification-token", 0.40, 1.0, side="BUY")
    audit = control.read_audit()
    checks = [row for row in audit if row.get("kind") == "live_control_submission_check"]
    latest = checks[-1] if checks else {}
    return {
        "operator_enabled": enabled,
        "submit_result": result,
        "exchange_order_post_attempts": sentinel.order_post_attempts,
        "latest_check": latest,
        "passed": (
            result is None
            and sentinel.order_post_attempts == 0
            and latest.get("allowed") is False
        ),
    }


async def verify() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="live-control-verifier-") as temporary:
        root = Path(temporary)
        control = LiveTradingControl(root / "state.json", root / "audit.jsonl")
        off = await _probe(control, False)
        armed = await _probe(control, True)
        state = control.read_state()
        return {
            "scope": "live-control runtime verification with exchange POST sentinel",
            "run_at": datetime.now(timezone.utc).isoformat(),
            "real_network_used": False,
            "real_orders_submitted": False,
            "operator_off_probe": off,
            "operator_armed_probe": armed,
            "effective_live_submission_enabled": state.effective_live_submission_enabled,
            "hard_block_reason": state.hard_block_reason,
            "passed": bool(
                off["passed"]
                and armed["passed"]
                and not state.effective_live_submission_enabled
            ),
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    report = asyncio.run(verify())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(args.output)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
