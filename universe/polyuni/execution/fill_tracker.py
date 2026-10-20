"""FillTracker — deferred, non-blocking fill resolution.

Each pending fire captures the state needed to resolve on a LATER tick's
execution cycle, so the hot-path fire coroutine (execution.py's execute_dual_buy)
can return immediately after submission instead of blocking on is_filled. Probing
happens here, driven by on_price_update -> check_fills on every subsequent tick,
until each pending entry resolves to clean / partial / no-fill.

No fill is double-processed: once an entry is popped under the pending lock, it's
gone. No fill is missed: every pending leg is probed on every check_fills call.
"""
from __future__ import annotations

import asyncio
import time
from typing import Callable, Dict, Optional

from ..clients.trading_client import TradingClient
from ..config import SETTINGS
from ..logger import log
from ..risk import RiskState
from .order_router import OrderRouter
from .partial_fill_recovery import PartialFillRecovery
from .pnl_ledger import PnlLedger


class FillTracker:
    def __init__(
        self, client: TradingClient, risk: RiskState, order_router: OrderRouter,
        pnl_ledger: PnlLedger, partial_recovery: PartialFillRecovery,
    ):
        self.client = client
        self.risk = risk
        self.order_router = order_router
        self.pnl_ledger = pnl_ledger
        self.partial_recovery = partial_recovery
        self.pending: Dict[tuple, dict] = {}
        self._fire_seq: int = 0
        self._pending_lock: Optional[asyncio.Lock] = None
        # Wired post-construction (ArbEngine.invalidate) -- invoked when a fire
        # produced ZERO exposure, so the engine's per-market debounce is cleared
        # and the same price can be re-fired next tick.
        self._invalidate_fire: Optional[Callable[[str], None]] = None

    def set_invalidator(self, cb: Callable[[str], None]) -> None:
        self._invalidate_fire = cb

    def notify_no_exposure(self, market_id: str) -> None:
        if self._invalidate_fire is not None:
            self._invalidate_fire(market_id)

    def next_fire_seq(self) -> int:
        self._fire_seq += 1
        return self._fire_seq

    def get_pending_lock(self) -> asyncio.Lock:
        # Lazy init: asyncio.Lock() requires a running loop at construction time,
        # which breaks synchronous unit tests that construct Execution outside asyncio.
        if self._pending_lock is None:
            self._pending_lock = asyncio.Lock()
        return self._pending_lock

    async def add_pending(self, key: tuple, entry: dict) -> bool:
        async with self.get_pending_lock():
            if key not in self.pending:
                self.pending[key] = entry
                return True
            return False

    def pending_for_market(self, market_id: str) -> list[tuple]:
        return [k for k in self.pending if k[0] == market_id]

    def pop(self, key: tuple) -> Optional[dict]:
        return self.pending.pop(key, None)

    def on_price_update(self) -> None:
        """Tick-driven trigger (PriceFeed.on_update -> arb_engine -> execution).
        Schedules check_fills if there are pending fills; the probe runs
        concurrently with the arb decision for THIS tick so neither path blocks
        the other."""
        if not self.pending:
            return
        task = asyncio.create_task(self.check_fills())
        task.add_done_callback(self._log_task_error)

    async def check_fills(self) -> None:
        if not self.pending:
            return
        async with self.get_pending_lock():
            keys = list(self.pending.keys())
            if not keys:
                return
            for key in keys:
                entry = self.pending.get(key)
                if entry is None:
                    continue
                resolved = await self.resolve_one(entry, key)
                if resolved:
                    self.pending.pop(key, None)

    async def resolve_one(self, entry: dict, key: tuple) -> bool:
        """Probe one pending fire. Returns True if the entry has been fully
        resolved (popped by caller) and should be removed from pending, False if
        it should remain pending for the next tick."""
        st = entry["st"]
        tpl = entry["tpl"]
        yes_id = entry["yes_id"]
        no_id = entry["no_id"]
        size = entry["size"]
        snapshot_combined = entry["combined"]
        yes_p = entry["yes_p"]
        no_p = entry["no_p"]
        age_ms = entry["age_ms"]
        band = entry["band"]
        first_sent = entry["first_sent"]
        market_id = st.market_id
        mode = getattr(self.client, "mode", "live")

        if not entry["yes_filled"] and yes_id:
            try:
                entry["yes_filled"] = await self.client.is_filled(yes_id, tpl.yes_token_id, gap_ms=0)
            except Exception as e:
                log.warning(f"is_filled probe failed for yes {yes_id}: {e!r}")
        if not entry["no_filled"] and no_id:
            try:
                entry["no_filled"] = await self.client.is_filled(no_id, tpl.no_token_id, gap_ms=0)
            except Exception as e:
                log.warning(f"is_filled probe failed for no {no_id}: {e!r}")

        yes_filled = entry["yes_filled"]
        no_filled = entry["no_filled"]
        fill_yes_p, fill_yes_source = self.order_router.fill_details(yes_id, yes_p)
        fill_no_p, fill_no_source = self.order_router.fill_details(no_id, no_p)

        if yes_filled and no_filled:
            self.pnl_ledger.metrics.fills += 1
            self.pnl_ledger.metrics.record_fills_for_mode(mode)
            self.pnl_ledger.metrics.record_freshness_attempt(age_ms, filled=True)
            effective_size = size * min(entry.get("yes_ratio", 1.0), entry.get("no_ratio", 1.0))
            self.pnl_ledger.record_clean_fill(
                st, mode=mode, snapshot_combined=snapshot_combined,
                fill_yes_p=fill_yes_p, fill_no_p=fill_no_p,
                fill_yes_source=fill_yes_source, fill_no_source=fill_no_source,
                effective_size=effective_size, yes_id=yes_id, no_id=no_id,
                yes_p=yes_p, no_p=no_p, age_ms=age_ms, band=band,
                stage_timings=entry.get("stage_timings"),
                opp_to_order_ms=entry.get("opp_to_order_ms", 0.0),
                ack_ms=entry.get("ack_ms", 0.0),
            )
            self.pnl_ledger.resolve_ledger(entry, "resolved", entry.get("committed_cost", 0.0))
            return True

        if yes_filled or no_filled:
            # One leg filled, the other hasn't (yet). Don't manage immediately --
            # the missing leg may still fill within the gap window. If the gap has
            # elapsed (measured from first_sent), it's a confirmed partial.
            gap_ms = (time.monotonic() - first_sent) * 1000.0
            if gap_ms >= SETTINGS.max_leg_gap_ms:
                self.pnl_ledger.metrics.partial_fills += 1
                self.pnl_ledger.metrics.record_fills_for_mode(mode)
                self.pnl_ledger.metrics.record_freshness_attempt(age_ms, filled=True)
                log.critical(
                    f"PARTIAL FILL on {market_id}: "
                    f"yes={'FILLED' if yes_filled else 'MISSING'} "
                    f"no={'FILLED' if no_filled else 'MISSING'} — managing exposure "
                    f"(gap={gap_ms:.0f}ms >= {SETTINGS.max_leg_gap_ms}ms)"
                )
                committed_cost = entry.get("committed_cost", 0.0)
                await self.partial_recovery.manage_partial(
                    st, tpl, yes_id, no_id, yes_filled, no_filled,
                    size, snapshot_combined, fill_yes_p, fill_no_p, age_ms, band,
                    reserved=committed_cost,
                    yes_ratio=entry.get("yes_ratio", 1.0),
                    no_ratio=entry.get("no_ratio", 1.0),
                    fill_yes_source=fill_yes_source,
                    fill_no_source=fill_no_source,
                    stage_timings=entry.get("stage_timings"),
                    opp_to_order_ms=entry.get("opp_to_order_ms", 0.0),
                    ack_ms=entry.get("ack_ms", 0.0),
                )
                self.pnl_ledger.resolve_ledger(entry, "resolved", committed_cost)
                return True
            return False  # legs differ, window still open -- probe again next tick

        # Neither filled yet. Has the gap window elapsed? If so, neither is coming:
        # cancel both resting orders and release the optimistically-committed capital.
        gap_ms = (time.monotonic() - first_sent) * 1000.0
        if gap_ms >= SETTINGS.max_leg_gap_ms:
            yes_cancelled = (
                not yes_id or await self.order_router.cancel_or_manual_review(
                    tpl.yes_token_id, yes_id, "no-fill timeout"))
            no_cancelled = (
                not no_id or await self.order_router.cancel_or_manual_review(
                    tpl.no_token_id, no_id, "no-fill timeout"))
            if not (yes_cancelled and no_cancelled):
                # A failed cancel may leave an order live (or may have raced with a
                # fill). Retain capital and stop automatic retry; the operator must
                # reconcile the exchange state.
                self.pnl_ledger.resolve_ledger(entry, "manual_review", entry.get("committed_cost", 0.0))
                return True
            committed_cost = entry.get("committed_cost", 0.0)
            if committed_cost > 0:
                self.risk.release(market_id, committed_cost)
            # Both legs cancelled with zero fills -- no exposure survived this fire.
            # Clear the engine's per-market debounce so a book that returns to the
            # same combined price can be re-fired next tick.
            self.notify_no_exposure(market_id)
            self.pnl_ledger.record_missed(
                st, reason="no_fill_in_window", snapshot_combined=snapshot_combined, size=size,
                age_ms=age_ms, band=band, accumulate_would_lock=False,
                stage_timings=entry.get("stage_timings"),
                opp_to_order_ms=entry.get("opp_to_order_ms", 0.0), ack_ms=entry.get("ack_ms", 0.0),
            )
            self.pnl_ledger.resolve_ledger(entry, "resolved", 0.0)
            return True

        return False  # window still open, neither filled -- leave pending

    @staticmethod
    def _log_task_error(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.error(f"fill-resolution task failed: {exc!r}")
