"""TASK 2 — paper vs live mode visibility in order records + summaries.

Regression coverage for the silent-failure finding: dry-run/paper order ids were
recorded indistinguishably from live ids, and ``get_fill_ratio`` returned 1.0 for
any id, so a paper session and a live session produced identical-looking records.

Fix contract verified here:
  * every order record (TradeRecord + OrderLedger) carries an explicit ``mode``
    field ("dry_run" | "paper" | "live");
  * Metrics.session summary breaks OUT paper vs live counts;
  * any order-id lookup labels a dry-run id as dry-run in its output.
"""
from __future__ import annotations

import asyncio

from polymarket_arb.clients.trading_client import TradingClient
from polymarket_arb.config import SETTINGS
from polymarket_arb.execution import Execution
from polymarket_arb.metrics import Metrics
from polymarket_arb.models import MarketState
from polymarket_arb.risk import RiskState


class _ModeClient(TradingClient):
    """Deterministic TradingClient stub whose ``mode`` is fixed, emitting ids
    shaped like the real product (dryrun-* vs 0x<hex>-ish)."""

    def __init__(self, mode: str):
        self._mode = mode
        self.s = SETTINGS
        self._order_modes = {}
        self.sent = []
        self.cancelled = []

    @property
    def mode(self) -> str:
        return self._mode

    async def submit_order(self, token_id, price, size, side="BUY"):
        if self._mode == "dry_run":
            oid = f"dryrun-{side.lower()}-{token_id[-8:]}"
        else:
            oid = f"0x{mode_tag(self._mode)}{token_id[-8:]}{side.upper()}"
        self.sent.append((token_id, price, size, side))
        return self._register_order(oid, self._mode)

    async def is_filled(self, order_id, token_id, gap_ms=0):
        return order_id is not None

    async def cancel_order(self, token_id, missing_order_id=None):
        self.cancelled.append(token_id)
        return True


def mode_tag(mode: str) -> str:
    return {"dry_run": "d", "paper": "p", "live": "l"}[mode]


def _state(market_id: str, yes, no):
    now = __import__("time").time()
    return MarketState(
        market_id=market_id, question="mode test",
        yes_token_id="0xYY" + market_id, no_token_id="0xNN" + market_id,
        best_yes_ask=yes, best_no_ask=no,
        last_update_ts=now, yes_ts=now, no_ts=now,
    )


async def _one_session(mode: str, metrics: Metrics):
    """Run one clean dual-buy + fill resolution for a mode against ``metrics``."""
    client = _ModeClient(mode)
    risk = RiskState(balance=1000.0)
    exec_ = Execution(client, risk, metrics)
    st = _state(f"m-{mode}", 0.47, 0.49)
    exec_.register(st)
    await exec_.execute_dual_buy(st, 0.96, 0.0, age_ms=10.0, band="fresh")
    exec_.on_price_update(st)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    return exec_, client, st


def test_mixed_mode_session_summary_breaks_out_paper_and_live():
    """A session mixing dry_run + paper + live orders must summarize each mode
    separately, and every in-memory TradeRecord must carry its mode."""
    async def _run():
        metrics = Metrics()
        seen = []
        for mode in ("dry_run", "paper", "live"):
            exec_, client, st = await _one_session(mode, metrics)
            seen.append(exec_.records[st.market_id])
        return metrics, seen
    metrics, seen = asyncio.run(_run())

    # Order counts broken out by mode (each clean fire = 2 orders, 1 fill).
    assert metrics.orders_sent == 6
    assert metrics.orders_sent_by_mode == {"dry_run": 2, "paper": 2, "live": 2}
    assert metrics.fills_by_mode == {"dry_run": 1, "paper": 1, "live": 1}

    # The session summary string must expose each mode separately.
    summary = metrics.summary()
    assert "paper=2s/1f" in summary
    assert "live=2s/1f" in summary
    assert "dry_run=2s/1f" in summary

    # Every record carries an explicit mode.
    by_market = {r.market_id: r for r in seen}
    assert by_market["m-dry_run"].mode == "dry_run"
    assert by_market["m-paper"].mode == "paper"
    assert by_market["m-live"].mode == "live"


def test_record_mode_is_paper_when_client_is_paper():
    """The record attached for a paper-mode client is labeled paper, not live."""
    async def _run():
        metrics = Metrics()
        exec_, client, st = await _one_session("paper", metrics)
        return exec_.records[st.market_id]
    rec = asyncio.run(_run())
    assert rec.mode == "paper"
    assert rec.yes_order_id is not None


def test_dry_run_order_id_lookup_labeled_dry_run():
    """Any ID lookup on a dry-run id must be labeled dry-run in the output."""
    client = _ModeClient("dry_run")
    oid = asyncio.run(client.submit_order("0x1234567890abcdef", 0.40, 10.0))
    assert oid.startswith("dryrun-")
    assert client.mode_of(oid) == "dry_run"
    report = client.get_fill_report(oid)
    assert report["mode"] == "dry_run"
    assert report["fill_ratio"] == 1.0
    assert report["counts_as_paper"] is True
    # Legacy float contract on get_fill_ratio is preserved for execution math.
    assert client.get_fill_ratio(oid) == 1.0
    # Unknown, non-registered id has no paper trace -> live.
    assert client.mode_of("0x" + "ab" * 32) == "live"


def test_paper_order_id_lookup_labeled_paper():
    """Paper-trade ids are shaped like real CLOB ids, so they MUST be labeled
    paper via the issued-id registry — not guessed as live."""
    client = _ModeClient("paper")
    oid = asyncio.run(client.submit_order("0xabcdef1234567890", 0.40, 10.0))
    assert client.mode_of(oid) == "paper"
    report = client.get_fill_report(oid)
    assert report["mode"] == "paper"
    assert client.mode_of(oid) != "live"


def test_live_order_id_lookup_labeled_live():
    client = _ModeClient("live")
    oid = asyncio.run(client.submit_order("0xfeedfacefeedface", 0.40, 10.0))
    assert client.mode_of(oid) == "live"
    assert client.mode_of(None) == "live"
    assert client.get_fill_ratio(None) == 0.0
