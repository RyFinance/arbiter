"""Execution — HOT PATH. The whole point of the bot.

Goals, in priority order:
  1. Shortest possible path from (price update) -> (both orders submitted).
  2. Correct paired YES/NO execution.
  3. Fail safe: if one leg can't go, don't leave the other hanging.

Key speed technique: PRECOMPUTED ORDER TEMPLATES.
  At startup we build a fixed template per market containing everything that does NOT
  change between opportunities (token ids, side=BUY, size default, client routing).
  When an arb fires we only overwrite price + size + timestamp — no object churn, no
  re-discovery, no re-parsing. Risk check + both submits happen in a single coroutine.

We submit BOTH legs with asyncio.gather so they fly concurrently on the wire (closest
thing to simultaneous submission).

PARTIAL-FILL / UNHEDGED-LEG MANAGEMENT (FIX #2) — the #1 real-bot loss driver:
  If one leg fills and the other does not confirm within MAX_LEG_GAP_MS, we NEVER let
  that sit as an unmanaged directional bet. We:
    (1) cancel the resting (unfilled) order,
    (2) try to HEDGE it by crossing the spread (buy the missing side at the current
        ask) — but ONLY if the remaining edge still covers PARTIAL_HEDGE_MIN_EDGE,
    (3) otherwise EXIT the filled leg at the best bid (market) to cap the loss.
  Residual PnL (hedge + exit outcome) is tracked SEPARATELY from clean-arb locked PnL,
  and if the residual loss breaches PARTIAL_LOSS_CAP_USD the kill switch trips.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Dict, Optional

from .alerting import WebhookAlerter, ALERT_REPEATED_REJECTIONS
from .clients.trading_client import TradingClient
from .config import SETTINGS
from .logger import log
from .metrics import Metrics
from .models import MarketState, TradeRecord
from .order_ledger import OrderLedger
from .risk import RiskState


def _format_stage_timings(stages_s: Optional[Dict[str, float]]) -> dict:
    """Convert internal stage timings (seconds) to a JSON-safe dict (milliseconds).

    Returns an empty dict when no stage timings are available (e.g. for TRADE events
    logged before the fire path reached the submission stage).
    """
    if not stages_s:
        return {}
    return {k: round(v * 1000.0, 3) for k, v in stages_s.items()}


@dataclass()
class OrderTemplate:
    """COLD-PATH-prebuilt, HOT-PATH-mutated. Only price/size/ts change per fire."""
    market_id: str
    yes_token_id: str
    no_token_id: str
    size: float


class Execution:
    """Order lifecycle manager — entry, deferred fill tracking, partial-fill recovery.

    Receives ``on_fire`` snapshots from ``ArbEngine`` and dispatches dual-leg buys
    against the gated snapshot (not the live book). Implements FIX #6 deferred,
    non-blocking fill-resolution (``on_price_update`` re-checks open orders on every
    tick instead of awaiting ``is_filled``)，and FIX #2 partial-fill management
    (hedge-or-exit the unmatched leg inside ``MAX_LEG_GAP_MS``，bounded by
    ``PARTIAL_LOSS_CAP_USD``). Owns the live order ledger + kill-switch trip logic.

    Hot-path surface: ``on_price_update`` runs on every WS tick; the rest is cold
    or runs in awaited fire-task coroutines.
    """

    def __init__(
        self,
        client: TradingClient,
        risk: RiskState,
        metrics: Metrics,
        alerter: Optional[WebhookAlerter] = None,
        ledger: Optional[OrderLedger] = None,
    ):
        self.client = client
        self.risk = risk
        self.metrics = metrics
        # TASK B: optional alerter for kill-switch / emergency-stop alerts.
        # No-op when None or disabled (ALERT_WEBHOOK_URL empty).
        self.alerter = alerter
        self.ledger = ledger
        self._templates: Dict[str, OrderTemplate] = {}
        self.records: Dict[str, TradeRecord] = {}
        # TASK E2: track consecutive order rejections (submit_order returning None)
        # across the hot path. Fires ALERT_REPEATED_REJECTIONS at 3+ in a row so
        # operators know the exchange is rejecting our orders (auth, liquidity,
        # or rate-limit issue) before the bot silently misses every arb.
        self._consecutive_rejections: int = 0
        # FIX #6: non-blocking fill tracking. Each entry captures the state needed to
        # resolve a fire on a LATER tick's execution cycle, so execute_dual_buy can
        # return immediately after submission instead of blocking on is_filled.
        # key = (market_id, yes_id, fire_seq) — the per-fire sequence suffix makes
        # the key unique even when yes_id is None (submit rejected), which otherwise
        # collides across fires and silently drops their tracking (fill-rate
        # investigation 2026-08-16: 76.5% of replayed pairs were discarded this way).
        self._pending: Dict[tuple, dict] = {}
        self._fire_seq: int = 0
        # TASK 4: optional callback into ArbEngine.invalidate, wired post-construction
        # (the engine is built after Execution in main.py / backtest_engine.py). Invoked
        # when a fire produced ZERO exposure, so the engine's per-market combined-price
        # debounce is cleared and the same price can be re-fired next tick.
        self._invalidate_fire: Optional[callable] = None

    def set_invalidator(self, cb: callable) -> None:
        """Wire a callback (ArbEngine.invalidate) invoked after a fire produced no
        exposure (all orders cancelled/vanished) to un-wedge the fire debounce."""
        self._invalidate_fire = cb

    def _notify_no_exposure(self, market_id: str) -> None:
        if self._invalidate_fire is not None:
            self._invalidate_fire(market_id)

    def _resolve_ledger(self, entry: dict, status: str, amount: float) -> None:
        record = entry.get("ledger_record")
        if self.ledger is not None and record is not None:
            self.ledger.append_resolved(
                record, status=status, final_committed_amount=amount)

    def _submission_allowed(self, call_site: str, token_id: str, side: str) -> bool:
        """Check the production client's live-control gate at every call site.

        Lightweight simulation clients used by the deterministic backtest do not
        expose this method and remain unchanged.  If a production checker exists
        but errors, the order is denied rather than bypassing the control layer.
        """
        # Production TradingClient sets this marker in its constructor.  A large
        # number of deterministic backtest/test doubles inherit the interface but
        # deliberately override submit_order; they never reach an exchange and do
        # not pretend to be the production boundary.
        if not getattr(self.client, "_enforce_live_control", False):
            return True
        checker = getattr(self.client, "check_order_submission", None)
        if not callable(checker):
            return True
        try:
            return bool(checker(call_site, token_id=token_id, side=side))
        except Exception as exc:
            log.critical(
                "LIVE_CONTROL_CHECK failed closed",
                {
                    "call_site": call_site,
                    "token_suffix": token_id[-8:],
                    "side": side,
                    "error": repr(exc),
                },
            )
            return False

    async def _submit_leg(
        self, token_id: str, price: float, size: float, observed_fill_price: float,
    ) -> tuple[Optional[str], float, float]:
        """Submit one leg and retain the actual coroutine start/end timestamps."""
        started = time.monotonic()
        if not self._submission_allowed("execution.entry", token_id, "BUY"):
            return None, started, time.monotonic()
        paper_submit = getattr(self.client, "submit_paper_order", None)
        if getattr(self.client, "_paper_trade", False) and callable(paper_submit):
            order_id = await paper_submit(
                token_id, price, size, side="BUY", observed_fill_price=observed_fill_price,
            )
        else:
            order_id = await self.client.submit_order(token_id, price, size, side="BUY")
        return order_id, started, time.monotonic()

    def _fill_details(self, order_id: Optional[str], fallback_price: float) -> tuple[float, str]:
        """Return recorded simulated fill price/source, or the decision fallback.

        Paper REST-book validation records the actual marketable top-of-book used by
        the simulator. Live clients currently expose no authoritative execution price
        through this interface, so their legacy decision-price fallback is explicit.
        """
        getter = getattr(self.client, "get_fill_report", None)
        if order_id is not None and callable(getter):
            try:
                report = getter(order_id) or {}
                if report.get("fill_price") is not None:
                    return float(report["fill_price"]), str(report.get("fill_source", "reported"))
            except Exception as exc:
                log.warning(f"fill detail lookup failed for {order_id}: {exc!r}")
        return float(fallback_price), "decision_fallback"

    async def _cancel_or_manual_review(
        self, token_id: str, order_id: Optional[str], context: str,
    ) -> bool:
        """Cancel an order, making one status probe only if cancellation is uncertain.

        ``is_filled(False)`` cannot distinguish a cancelled order from a resting or
        otherwise unavailable one, so it is deliberately treated as unsafe.  Callers
        must not release capital or submit an offsetting order unless this returns
        ``True``.
        """
        try:
            cancelled = await self.client.cancel_order(token_id, missing_order_id=order_id)
        except Exception as exc:
            log.error(f"{context}: cancel failed for {token_id[-8:]}: {exc!r}")
            cancelled = False
        if cancelled is True:
            return True

        try:
            filled = await self.client.is_filled(order_id, token_id, gap_ms=0)
        except Exception as exc:
            log.error(f"{context}: post-cancel status check failed for {token_id[-8:]}: {exc!r}")
            filled = None
        state = "filled" if filled is True else "live or unknown"
        log.critical(
            f"{context}: cancellation unconfirmed and order is {state}; "
            "halting for manual review"
        )
        self.risk.halt(f"{context}: cancellation unconfirmed ({state})")
        return False

    # ---- COLD PATH: build templates once per market ----
    def register(self, st: MarketState, size: float = None):
        self._templates[st.market_id] = OrderTemplate(
            market_id=st.market_id,
            yes_token_id=st.yes_token_id,
            no_token_id=st.no_token_id,
            size=size if size is not None else SETTINGS.order_size,
        )

    # NEW (FIX #3): register a dynamically-resolved windowed market mid-session.
    def add_market(self, st: MarketState, size: float = None) -> OrderTemplate:
        self.register(st, size)
        return self._templates[st.market_id]

    def release_window(self, market_id: str):
        """HIGH: release all deployed capital for a settled window's market.

        Clean-arb and resolved-partial fills commit cost into risk at fill time, but that
        capital must be freed once the window settles (positions resolve at $1/side) or
        total_deployed only grows and precheck eventually halts all trading. Called by
        window_tracker on rotation with the settling window's slug.
        """
        # Pending orders have not yet been reconciled.  Do not free either their
        # deployed cost or a reservation while their cancellation is uncertain.
        to_remove = [k for k in self._pending if k[0] == market_id]
        has_pending_orders = bool(to_remove)
        if not has_pending_orders:
            held = self.risk.deployed_per_market.get(market_id, 0.0)
            if held > 0:
                self.risk.release(market_id, held)
        # MEDIUM: also clear any in-flight RESERVATION for this market. If a fire was
        # reserved but not yet committed at rotation, the reservation would otherwise
        # leak into pending_deployed and spuriously block new trades until it resolved.
        pending = self.risk.pending_per_market.get(market_id, 0.0)
        if pending > 0 and not has_pending_orders:
            self.risk.unreserve(market_id, pending)
        # FIX #6: cancel any pending order legs for this market that haven't resolved
        # yet (optimistically committed but not yet confirmed filled). Without this,
        # a window that rotates while orders are still resting would leave dangling
        # orders + committed capital with no one tracking the fills.
        for k in to_remove:
            entry = self._pending.pop(k, None)
            if entry is None:
                continue
            tpl = entry["tpl"]
            yes_id = entry["yes_id"]
            no_id = entry["no_id"]
            if yes_id:
                # E1: create_task returns immediately; exceptions from the coroutine
                # would be silently swallowed. Wrap in a task with error logging.
                task = asyncio.create_task(self._cancel_or_manual_review(
                    tpl.yes_token_id, yes_id, "window rotation cancellation"))
                task.add_done_callback(self._log_task_error)
            if no_id:
                task = asyncio.create_task(self._cancel_or_manual_review(
                    tpl.no_token_id, no_id, "window rotation cancellation"))
                task.add_done_callback(self._log_task_error)
            # Rotation cannot prove cancellation/fill outcome synchronously. Keep the
            # audit trail conservative so an operator can review this exposure.
            self._resolve_ledger(entry, "manual_review", entry.get("committed_cost", 0.0))
        self._templates.pop(market_id, None)
        self.records.pop(market_id, None)

    # ---- HOT PATH core: called by arb_engine.on_fire ----
    async def execute_dual_buy(
        self, st: MarketState, combined: float, decision_ts: float,
        age_ms: float = 0.0, band: str = "fresh",
        yes_p: Optional[float] = None, no_p: Optional[float] = None,
    ):
        """FIRE (FIX #6 non-blocking). Validate -> reserve -> submit both legs concurrently
        -> schedule fill-check on next tick -> RETURN IMMEDIATELY.

        This is the latency-critical hot path: we must get money on the table ASAP and
        find out later if it stuck. Blocking inside this coroutine on is_filled() stalls
        the event loop between the WS tick handler and the next tick — unacceptable in a
        fast market where a stale check loses the arb.

        Instead we:
          1. precheck + reserve (atomic, as before)
          2. submit BOTH legs via asyncio.gather (as before)
          3. commit the reservation OPTIMISTICALLY to deployed capital (the orders are
             live and resting; treating them as deployed keeps risk caps honest), and
          4. stash a pending-fill record + schedule _check_fills() on the event loop.
          The actual fill confirmation + partial management happens on the NEXT tick's
          execution cycle (driven by on_price_update / _check_fills tick below), where a
          non-blocking single is_filled gap_ms=0 probe replaces the old blocking poll.

        FIX #2 partial management is preserved: if one leg fills and the other does not
        within max_leg_gap_ms of the FIRE time (measured against the fire timestamp, not
        against a synchronous poll), we cancel the resting leg, hedge or exit per the
        existing _manage_partial logic. No fill is missed (each pending leg is re-checked
        every tick until it resolves), no fill is double-processed (once resolved the
        pending record is popped atomically under _pending_lock).

        `yes_p`/`no_p` are the SNAPSHOT that passed the arb gate in arb_engine (C1):
        we submit at those prices and re-assert the edge against them, ignoring any
        later WS ticks that mutated st.best_yes_ask/no_ask after the gate fired.
        """
        tpl = self._templates.get(st.market_id)
        if tpl is None:
            # This is reachable during market rotation/release.  A silent return makes
            # a rejected fire indistinguishable from no opportunity at all.
            self.metrics.missed += 1
            self.metrics.template_unavailable += 1
            log.error(f"execute_dual_buy rejected: no order template for {st.market_id}")
            log.trade("missed", {
                "reason": "template_unavailable", "market_id": st.market_id,
                "combined": round(combined, 4), "age_ms": round(age_ms, 1),
                "freshness_band": band,
            })
            return

        # C1: use the gated snapshot, never the live (possibly-stale-by-ticks) book.
        if yes_p is None:
            yes_p = st.best_yes_ask
        if no_p is None:
            no_p = st.best_no_ask
        # LOW: latent footgun — if a snapshot price were ever None (only reachable via a
        # direct call, not the wired arb_engine path), bail instead of sending a $0 order.
        if yes_p is None or no_p is None:
            self.metrics.missed += 1
            log.trade("missed", {
                "reason": "missing_price", "market_id": st.market_id,
                "yes_p": yes_p, "no_p": no_p, "age_ms": round(age_ms, 1),
                "freshness_band": band,
            })
            return
        size = tpl.size
        snapshot_combined = (yes_p or 0.0) + (no_p or 0.0)
        edge_price = 1.0 - SETTINGS.min_edge
        # C1: re-assert the edge against the SNAPSHOT before risking any order. If the
        # snapshot no longer clears, skip (this can happen if evaluate's combined was
        # computed from a transient book state; better safe than a wrong-sided fill).
        if snapshot_combined >= edge_price:
            self.metrics.missed += 1
            self.metrics.record_freshness_attempt(age_ms, filled=False)
            would_lock = max(0.0, 1.0 - snapshot_combined) * size
            edge_surplus = max(0.0, edge_price - snapshot_combined) * size
            self.metrics.record_would_lock(would_lock)
            log.trade("missed", {
                "reason": "snapshot_no_edge", "market_id": st.market_id,
                "combined": round(snapshot_combined, 4), "age_ms": round(age_ms, 1),
                "freshness_band": band,
                "size": size, "would_lock": round(would_lock, 4),
                "edge_surplus": round(edge_surplus, 4),
            })
            return

        # REST-path paper orders are otherwise vulnerable to a known two-feed race:
        # the WS pair can clear the edge while one REST leg has already moved through
        # its limit. Revalidate both read-only books concurrently before reserving any
        # capital. The threshold is unchanged, and live submission remains unbuilt.
        pair_preflight = getattr(self.client, "paper_pair_preflight", None)
        if callable(pair_preflight):
            preflight = await pair_preflight(
                tpl.yes_token_id,
                tpl.no_token_id,
                yes_p,
                no_p,
                size,
                edge_price,
            )
            if preflight.get("applied"):
                log.info(
                    "REST_PAIR_PREFLIGHT "
                    f"market={st.market_id} passed={preflight.get('passed')} "
                    f"reason={preflight.get('reason')} "
                    f"decision_combined={snapshot_combined:.4f} "
                    f"rest_combined={preflight.get('combined')} "
                    f"yes_vwap={preflight.get('yes_vwap')} "
                    f"no_vwap={preflight.get('no_vwap')} "
                    f"yes_available={preflight.get('yes_available')} "
                    f"no_available={preflight.get('no_available')} "
                    f"size={preflight.get('size')} "
                    f"edge_price={preflight.get('edge_price')} "
                    f"latency_ms={preflight.get('latency_ms')}"
                )
                if not preflight.get("passed"):
                    self.metrics.missed += 1
                    self.metrics.record_freshness_attempt(age_ms, filled=False)
                    would_lock = max(0.0, 1.0 - snapshot_combined) * size
                    edge_surplus = max(0.0, edge_price - snapshot_combined) * size
                    self.metrics.record_would_lock(would_lock)
                    log.trade("missed", {
                        "reason": preflight.get("reason", "rest_preflight_failed"),
                        "market_id": st.market_id,
                        "combined": round(snapshot_combined, 4),
                        "rest_combined": preflight.get("combined"),
                        "rest_yes_vwap": preflight.get("yes_vwap"),
                        "rest_no_vwap": preflight.get("no_vwap"),
                        "rest_yes_available": preflight.get("yes_available"),
                        "rest_no_available": preflight.get("no_available"),
                        "rest_yes_limit": preflight.get("yes_limit", yes_p),
                        "rest_no_limit": preflight.get("no_limit", no_p),
                        "rest_edge_price": preflight.get("edge_price", edge_price),
                        "rest_preflight_latency_ms": preflight.get("latency_ms"),
                        "age_ms": round(age_ms, 1),
                        "freshness_band": band,
                        "size": size,
                        "would_lock": round(would_lock, 4),
                        "edge_surplus": round(edge_surplus, 4),
                    })
                    self._notify_no_exposure(st.market_id)
                    return

        cost_yes = yes_p * size
        cost_no = no_p * size

        reserved = cost_yes + cost_no
        # LATENCY-SENSITIVE SECTION START (decision_ts set in arb_engine).
        # 1) risk reserve = atomic precheck + reservation (HIGH). Serializes check-and-
        #    reserve across concurrent fire tasks so two fires can't both pass the same
        #    cap and over-deploy. On any fail, abort BOTH legs (nothing reserved).
        reserve_started = time.monotonic()
        reason = await self.risk.reserve(
            st, cost_yes, cost_no, snapshot_combined=snapshot_combined
        )
        reserve_done = time.monotonic()
        if reason is not None:
            self.metrics.missed += 1
            self.metrics.record_freshness_attempt(age_ms, filled=False)
            would_lock = max(0.0, 1.0 - snapshot_combined) * size
            edge_surplus = max(0.0, edge_price - snapshot_combined) * size
            self.metrics.record_would_lock(would_lock)
            log.trade("missed", {
                "reason": "risk:" + reason, "market_id": st.market_id,
                "combined": round(snapshot_combined, 4), "age_ms": round(age_ms, 1),
                "freshness_band": band,
                "size": size, "would_lock": round(would_lock, 4),
                "edge_surplus": round(edge_surplus, 4),
            })
            return

        fire_ts = reserve_done
        log.info(f"FIRE market={st.market_id} latency_ms={(fire_ts - decision_ts) * 1000:.2f} band={band}")

        yes_id = no_id = None
        committed_cost = 0.0
        commit_completed = False
        pending_key = None
        pending_entry = None
        pending_entry_inserted = False
        yes_started = no_started = yes_done = no_done = reserve_done
        first_sent = reserve_done
        try:
            # 2) submit both legs concurrently (closest to simultaneous submission) at the
            #    SNAPSHOT prices (C1). Use return_exceptions=True so that if one leg's
            #    submit raises, we still capture the successful leg's result (order id +
            #    timestamps). This lets the emergency-cancel handler below identify and
            #    cancel whichever leg did go through, instead of losing both ids when
            #    asyncio.gather first-fails-first-raises.
            submit_started = time.monotonic()
            yes_result, no_result = await asyncio.gather(
                self._submit_leg(tpl.yes_token_id, yes_p, size, yes_p),
                self._submit_leg(tpl.no_token_id, no_p, size, no_p),
                return_exceptions=True,
            )
            # Unpack: a result may be an Exception if that leg's submit_order raised.
            yes_exc = no_exc = None
            if isinstance(yes_result, Exception):
                yes_exc = yes_result
            else:
                yes_id, yes_started, yes_done = yes_result
            if isinstance(no_result, Exception):
                no_exc = no_result
            else:
                no_id, no_started, no_done = no_result
            first_sent = min(yes_started, no_started)
            submit_done = max(yes_done, no_done)

            # If either leg raised, cancel whichever sibling did reach the venue.
            # A returned order ID may already be a live, directional order, so its
            # reservation must remain counted until cancellation is *confirmed*.
            if yes_exc is not None or no_exc is not None:
                self.metrics.missed += 1
                exc = yes_exc or no_exc
                log.error(f"execute_dual_buy submit failed for {st.market_id}: {exc!r}; "
                          f"yes={yes_id} no={no_id}")
                cancellation_confirmed = True
                if yes_id:
                    cancellation_confirmed = await self._cancel_or_manual_review(
                        tpl.yes_token_id, yes_id, "submit sibling failure cancellation",
                    )
                if no_id:
                    cancellation_confirmed = (
                        await self._cancel_or_manual_review(
                            tpl.no_token_id, no_id, "submit sibling failure cancellation",
                        )
                        and cancellation_confirmed
                    )

                # With no returned order IDs, or after every returned order is
                # definitely cancelled, the reservation can safely be released.  If
                # a cancel is unconfirmed, _cancel_or_manual_review has halted risk;
                # leave the reservation in place so caps continue to include the
                # potentially live directional exposure.
                if cancellation_confirmed:
                    self.risk.unreserve(st.market_id, reserved)
                    # TASK 4: zero exposure survived this fire (every returned id was
                    # cancelled or none was placed) — clear the engine debounce so the
                    # same combined price can be re-evaluated next tick.
                    self._notify_no_exposure(st.market_id)
                return

            self.metrics.orders_sent += 2
            _mode = getattr(self.client, "mode", "live")
            self.metrics.record_orders_sent(_mode, 2)
            log.info(f"ACK yes_order={yes_id} no_order={no_id}")

            # Crash boundary: persist both returned IDs before mutating risk or the
            # volatile pending stash.  A restart can therefore query their real state.
            ledger_record = None
            if self.ledger is not None:
                ledger_record = {
                    "market_id": st.market_id, "yes_id": yes_id, "no_id": no_id,
                    "yes_token_id": tpl.yes_token_id, "no_token_id": tpl.no_token_id,
                    "reserved": reserved,
                    "mode": _mode,
                }
                self.ledger.append_pending(**ledger_record)

            # TASK E2: track consecutive rejections. If submit_order returns None
            # for either leg, that's an order rejection — increment the counter.
            # On any successful submit, reset the counter. At 3+ in a row, alert.
            if yes_id is not None and no_id is not None:
                self._consecutive_rejections = 0
            else:
                self._consecutive_rejections += 1
                if self._consecutive_rejections == 3 and self.alerter is not None:
                    self.alerter.alert(
                        ALERT_REPEATED_REJECTIONS,
                        f"3+ consecutive order rejections in a row "
                        f"(count={self._consecutive_rejections})",
                        market_id=st.market_id,
                        consecutive_rejections=self._consecutive_rejections,
                        yes_id=yes_id,
                        no_id=no_id,
                    )

            # 3) OPTIMISTIC commit: both orders are live on the wire / resting. Treat the
            #    reservation as deployed capital immediately so concurrent fires see an
            #    honest cap. If NEITHER leg fills by the time the gap window lapses, the
            #    deferred fill-check will unreserve/release the capital and cancel the
            #    resting orders (FIX #6 _check_fills "neither filled" branch). If only a
            #    submit failed (id is None), that leg is simply absent from the pending set.
            #
            # Record fill ratios BEFORE commit so capital is committed at the ACTUALLY
            # filled quantity, not the full requested size. Backtest clients return a
            # ratio < 1.0 when available liquidity is less than the order size; live
            # clients always return 1.0.
            yes_ratio = self.client.get_fill_ratio(yes_id) if yes_id else 0.0
            no_ratio = self.client.get_fill_ratio(no_id) if no_id else 0.0
            # Scale committed capital per-leg by each leg's fill ratio.
            # A partial fill on either leg means we only deployed that fraction
            # of the pair reservation for that leg. Using min() for the whole pair
            # would under-commit when one leg fills fully and the other doesn't fill
            # at all (e.g. a rejected submit), leaving deployed capital too low.
            committed_cost = yes_p * size * yes_ratio + no_p * size * no_ratio
            self.risk.commit(st.market_id, committed_cost, from_reservation=reserved)
            commit_completed = True

            # 4) opportunity-to-order stops at the first actual submission start;
            # dual-submit captures both paper/live leg completions separately.
            opp_to_order = first_sent - decision_ts
            post_submit_done = time.monotonic()
            ack_lat = submit_done - submit_started
            stages_s = {
                "reserve": reserve_done - reserve_started,
                "dispatch": first_sent - reserve_done,
                "dual_submit": submit_done - submit_started,
                "paper_leg_max": max(yes_done - yes_started, no_done - no_started),
                "post_submit": post_submit_done - submit_done,
                "execute_total": post_submit_done - reserve_started,
            }
            self.metrics.record_latency(opp_to_order, ack_lat, stages_s)
            log.info(
                "EXEC_TIMING "
                f"market={st.market_id} opp2first_ms={opp_to_order * 1000:.2f} "
                f"reserve_ms={stages_s['reserve'] * 1000:.2f} "
                f"dispatch_ms={stages_s['dispatch'] * 1000:.2f} "
                f"dual_submit_ms={stages_s['dual_submit'] * 1000:.2f} "
                f"paper_leg_max_ms={stages_s['paper_leg_max'] * 1000:.2f} "
                f"post_submit_ms={stages_s['post_submit'] * 1000:.2f}"
            )

            # Stash the pending fire so _check_fills (driven by the next price tick) can
            # probe fill status non-blockingly. key includes yes_id so re-firing the same
            # market while a prior leg is still pending doesn't collide.
            self._fire_seq += 1
            pending_key = (st.market_id, yes_id, self._fire_seq)
            pending_entry = {
                "st": st, "tpl": tpl,
                "combined": snapshot_combined, "yes_p": yes_p, "no_p": no_p, "size": size,
                "yes_id": yes_id, "no_id": no_id,
                "yes_filled": False, "no_filled": False,
                "yes_ratio": yes_ratio, "no_ratio": no_ratio,
                "committed_cost": committed_cost,
                "first_sent": first_sent, "decision_ts": decision_ts,
                "age_ms": age_ms, "band": band,
                "ledger_record": ledger_record,
                "stage_timings": stages_s,
                "opp_to_order_ms": opp_to_order * 1000.0,
                "ack_ms": ack_lat * 1000.0,
            }
            async with self._get_pending_lock():
                # Don't overwrite if a pending entry already exists (defensive; the key
                # includes yes_id so this is normally a fresh slot).
                if pending_key not in self._pending:
                    self._pending[pending_key] = pending_entry
                    pending_entry_inserted = True

            # 5) DEFERRED fill-resolution: do NOT await is_filled here (FIX #6). The
            #    pending entry is stashed above; fill confirmation happens on the NEXT
            #    tick's execution cycle via on_price_update -> _check_fills. This keeps
            #    the fire coroutine from blocking the event loop between ticks.
        except Exception as e:
            # An unexpected exception escaped during the fire sequence AFTER the
            # dual-submit succeeded (i.e., from get_fill_ratio, commit, metrics,
            # or latency recording). Submit failures are handled inline above
            # (return_exceptions=True path) before we reach commit. A later error
            # (pending stash, metrics, latency) can happen after commit, though:
            # release its deployed cost in that case; otherwise clear the live
            # reservation. Never do both, or accounting would double-release.
            if commit_completed:
                self.risk.release(st.market_id, committed_cost)
                self._resolve_ledger(pending_entry or {"ledger_record": ledger_record},
                                     "manual_review", reserved)
            else:
                self.risk.unreserve(st.market_id, reserved)
            # If a later setup/metrics step failed after this fire inserted its
            # pending record, remove only that exact object. Otherwise a later tick
            # can reprocess already-cancelled orders and double-count the outcome.
            if (pending_entry_inserted and pending_key is not None
                    and self._pending.get(pending_key) is pending_entry):
                self._pending.pop(pending_key, None)
            self.metrics.missed += 1
            log.error(f"execute_dual_buy aborted for {st.market_id}: {e!r}; "
                      f"yes={yes_id} no={no_id}")
            # Best-effort: cancel whichever leg succeeded before the exception.
            try:
                if yes_id:
                    await self.client.cancel_order(tpl.yes_token_id, missing_order_id=yes_id)
                if no_id:
                    await self.client.cancel_order(tpl.no_token_id, missing_order_id=no_id)
            except Exception as ce:
                log.error(f"emergency cancel failed for {st.market_id}: {ce!r}")
        # LATENCY-SENSITIVE SECTION END

    # ---- FIX #6: deferred, non-blocking fill resolution ----

    def _get_pending_lock(self):
        """Lazy asyncio.Lock for _pending mutations (lazy-init avoids breaking
        synchronous construction in tests)."""
        if getattr(self, "_pending_lock", None) is None:
            self._pending_lock = asyncio.Lock()
        return self._pending_lock

    async def _check_fills(self):
        """Non-blocking fill probe, called on each tick (on_price_update) and seeded
        once right after submission. For each pending fire:

          - probe each unconfirmed leg with a SINGLE is_filled(gap_ms=0) call (no loop
            inside is_filled — it returns immediately with the current status),
          - if BOTH legs are confirmed filled -> clean arb (commit already done; just
            record PnL), remove the pending entry,
          - if exactly ONE leg is confirmed filled within the max_leg_gap_ms window
            (measured against first_sent) -> partial event: remove the entry and hand
            off to _manage_partial (which cancels the resting leg + hedges/exits),
          - if the gap window lapses with neither confirmed -> cancel both resting
            orders, release the already-committed capital, record as missed,
          - if NEITHER is filled yet and the window is still open -> leave the entry
            pending for the next tick (no cancellation — the orders may still fill).

        No fill is double-processed: once an entry is popped under _pending_lock, it is
        gone. No fill is missed: every pending leg is probed on every _check_fills call.
        """
        if not self._pending:
            return
        async with self._get_pending_lock():
            # Snapshot the keys to process this tick. We re-check the live _pending set
            # after each resolution because _manage_partial may add transient state.
            keys = list(self._pending.keys())
            if not keys:
                return
            for key in keys:
                entry = self._pending.get(key)
                if entry is None:
                    continue
                resolved = await self._resolve_one(entry, key)
                if resolved:
                    self._pending.pop(key, None)

    async def _resolve_one(self, entry: dict, key: tuple) -> bool:
        """Probe one pending fire. Returns True if the entry has been fully resolved
        (popped by caller) and should be removed from _pending, False if it should
        remain pending for the next tick."""
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
        decision_ts = entry["decision_ts"]
        market_id = st.market_id

        # Non-blocking single probe per leg (gap_ms=0). Only probe legs that haven't
        # been confirmed filled yet and actually have an order id. Wrap in try/except
        # so a probe failure (network error, SDK hiccup) doesn't crash the _check_fills
        # task — we just treat it as "not yet confirmed" and retry on the next tick.
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
        fill_yes_p, fill_yes_source = self._fill_details(yes_id, yes_p)
        fill_no_p, fill_no_source = self._fill_details(no_id, no_p)

        if yes_filled and no_filled:
            # CLEAN ARB: both legs confirmed. Commit was already optimistic; now lock PnL.
            self.metrics.fills += 1
            self.metrics.record_fills_for_mode(getattr(self.client, "mode", "live"))
            self.metrics.record_freshness_attempt(age_ms, filled=True)
            effective_size = size * min(entry.get("yes_ratio", 1.0), entry.get("no_ratio", 1.0))
            fill_combined = fill_yes_p + fill_no_p
            expected_pnl = (1.0 - snapshot_combined) * effective_size
            pnl = (1.0 - fill_combined) * effective_size
            self.metrics.locked_pnl += pnl
            self.metrics.session_pnl += pnl
            self.metrics.realized_pnl += pnl
            self.risk.record_fill_pnl(pnl)
            log.trade("filled", {
                "market_id": market_id, "combined": round(fill_combined, 4),
                "decision_combined": round(snapshot_combined, 4),
                "decision_yes_ask": round(yes_p, 4),
                "decision_no_ask": round(no_p, 4),
                "fill_yes_price": round(fill_yes_p, 4),
                "fill_no_price": round(fill_no_p, 4),
                "fill_yes_source": fill_yes_source,
                "fill_no_source": fill_no_source,
                "fill_cost_delta": round(fill_combined - snapshot_combined, 4),
                "size": round(effective_size, 8),
                "expected_pnl": round(expected_pnl, 4),
                "age_ms": round(age_ms, 1), "freshness_band": band,
                "pnl_locked": round(pnl, 4),
                "stage_timings": _format_stage_timings(entry.get("stage_timings")),
                "opp_to_order_ms": round(entry.get("opp_to_order_ms", 0.0), 3),
                "ack_ms": round(entry.get("ack_ms", 0.0), 3),
            })
            log.info(f"FILLED pnl_locked={pnl:.4f} qty={effective_size:.0f}")
            self._record(st, fill_combined, effective_size, yes_id, no_id, True, True, pnl, "clean")
            self._resolve_ledger(entry, "resolved", entry.get("committed_cost", 0.0))
            return True

        if yes_filled or no_filled:
            # PARTIAL: one leg filled, the other hasn't (yet). Don't manage immediately —
            # the missing leg may still fill within the gap window. But if the gap has
            # elapsed (measured from first_sent), it's a confirmed partial: hand off to
            # _manage_partial and let it cancel/hedge/exit.
            gap_ms = (time.monotonic() - first_sent) * 1000.0
            if gap_ms >= SETTINGS.max_leg_gap_ms:
                self.metrics.partial_fills += 1
                self.metrics.record_fills_for_mode(getattr(self.client, "mode", "live"))
                self.metrics.record_freshness_attempt(age_ms, filled=True)
                log.critical(
                    f"PARTIAL FILL on {market_id}: "
                    f"yes={'FILLED' if yes_filled else 'MISSING'} "
                    f"no={'FILLED' if no_filled else 'MISSING'} — managing exposure "
                    f"(gap={gap_ms:.0f}ms >= {SETTINGS.max_leg_gap_ms}ms)"
                )
                # _manage_partial owns the position; pass the fire time so its latency
                # recording stays accurate. Commit already happened optimistically.
                # Pass the ACTUALLY committed cost (ratio-scaled), not the full reservation.
                committed_cost = entry.get("committed_cost", 0.0)
                stage_timings = entry.get("stage_timings")
                opp_to_order_ms = entry.get("opp_to_order_ms", 0.0)
                ack_ms = entry.get("ack_ms", 0.0)
                await self._manage_partial(
                    st, tpl, yes_id, no_id, yes_filled, no_filled,
                    size, snapshot_combined, fill_yes_p, fill_no_p, age_ms, band, decision_ts,
                    reserved=committed_cost,
                    yes_ratio=entry.get("yes_ratio", 1.0),
                    no_ratio=entry.get("no_ratio", 1.0),
                    fill_yes_source=fill_yes_source,
                    fill_no_source=fill_no_source,
                    stage_timings=stage_timings,
                    opp_to_order_ms=opp_to_order_ms,
                    ack_ms=ack_ms,
                )
                self._resolve_ledger(entry, "resolved", committed_cost)
                return True
            # Legs differ, but window still open — keep pending, probe again next tick.
            return False

        # Neither filled yet. Has the gap window elapsed? If so, neither is coming:
        # cancel both resting orders and release the optimistically-committed capital.
        gap_ms = (time.monotonic() - first_sent) * 1000.0
        if gap_ms >= SETTINGS.max_leg_gap_ms:
            yes_cancelled = (
                not yes_id or await self._cancel_or_manual_review(
                    tpl.yes_token_id, yes_id, "no-fill timeout"))
            no_cancelled = (
                not no_id or await self._cancel_or_manual_review(
                    tpl.no_token_id, no_id, "no-fill timeout"))
            if not (yes_cancelled and no_cancelled):
                # A failed cancel may leave an order live (or may have raced with a
                # fill).  Retain capital and remove this item from automatic retry;
                # the operator must reconcile the exchange state.
                self._resolve_ledger(entry, "manual_review", entry.get("committed_cost", 0.0))
                return True
            # Release the capital that was actually committed at submit time.
            # With ratio-scaled commit, this is committed_cost (not full reserved).
            # If neither leg filled, committed_ratio was 0, so committed_cost is 0.
            committed_cost = entry.get("committed_cost", 0.0)
            if committed_cost > 0:
                self.risk.release(market_id, committed_cost)
            # TASK 4: both legs cancelled with zero fills — no exposure survived
            # this fire. Clear the engine's per-market debounce so a book that
            # returns to the same combined price can be re-fired next tick
            # (otherwise the debounce stays wedged on a dead trade forever).
            self._notify_no_exposure(market_id)
            self.metrics.record_freshness_attempt(age_ms, filled=False)
            self.metrics.missed += 1
            log.trade("missed", {
                "reason": "no_fill_in_window", "market_id": market_id,
                "combined": round(snapshot_combined, 4), "age_ms": round(age_ms, 1),
                "freshness_band": band,
                "size": size,
                "would_lock": round(max(0.0, 1.0 - snapshot_combined) * size, 4),
                "stage_timings": _format_stage_timings(entry.get("stage_timings")),
                "opp_to_order_ms": round(entry.get("opp_to_order_ms", 0.0), 3),
                "ack_ms": round(entry.get("ack_ms", 0.0), 3),
            })
            self._resolve_ledger(entry, "resolved", 0.0)
            return True

        # Window still open, neither filled — leave pending for the next tick.
        return False

    async def _manage_partial(
        self, st, tpl, yes_id, no_id, yes_filled, no_filled,
        size, combined, yes_p, no_p, age_ms, band, decision_ts,
        reserved=0.0, yes_ratio=1.0, no_ratio=1.0,
        fill_yes_source="decision_fallback", fill_no_source="decision_fallback",
        stage_timings=None, opp_to_order_ms: float = 0.0, ack_ms: float = 0.0,
    ):
        """FIX #2: Turn a partial fill into a bounded, hedged, or exited position.

        Steps: cancel the resting unfilled leg -> attempt to hedge at the current ask
        if edge survives PARTIAL_HEDGE_MIN_EDGE -> else exit the filled leg at market.

        ``yes_ratio``/``no_ratio`` are the partial fill ratios returned by
        ``submit_order``/``get_fill_ratio`` for each leg. The FILLED leg's ratio
        expresses how much of it actually filled, so PnL and exit cost accounting
        must scale by it — otherwise a 50%-filled leg is recorded as a full fill and
        residual PnL is overstated. The missing leg never filled, so its ratio does
        not enter PnL (its order was cancelled); the hedge order carries its own
        fresh fill_ratio at submit time.
        """
        # Which side is filled, which is missing.
        filled_token = tpl.yes_token_id if yes_filled else tpl.no_token_id
        missing_token = tpl.no_token_id if yes_filled else tpl.yes_token_id
        filled_id = yes_id if yes_filled else no_id
        # The filled leg's ratio caps the realized quantity; the missing leg never
        # filled so its ratio is irrelevant for downstream PnL on the filled leg.
        filled_ratio = yes_ratio if yes_filled else no_ratio
        effective_size = size * filled_ratio
        filled_entry_price = yes_p if yes_filled else no_p
        filled_entry_source = fill_yes_source if yes_filled else fill_no_source
        expected_pnl = (1.0 - combined) * effective_size

        # The initial fire already committed the filled leg's actual cost. Do not
        # commit it again: doing so double-counts capital. The later hedge needs a
        # separate reservation and commit because its live price was not known then.
        # (1) Cancel the resting (unfilled) order so it can't fill later and leave us
        #     with an unexpected one-sided position once we've moved on. Pass the REAL
        #     resting id (H): if submit returned an id-but-didn't-fill, cancel that exact
        #     order; otherwise cancel by token (None).
        resting_id = no_id if yes_filled else yes_id
        if not await self._cancel_or_manual_review(
            missing_token, resting_id, "partial resting-leg cancellation",
        ):
            return

        # (2) Hedge attempt: buy the missing side at its CURRENT best ask (cross spread).
        #     Decide using the LIVE book, not the stale fire price.
        missing_ask = st.best_no_ask if yes_filled else st.best_yes_ask
        if missing_ask is None:
            # No live quote on the missing side -> can't hedge; exit the filled leg.
            residual = await self._exit_filled(
                st, tpl, filled_token, filled_id, effective_size, combined, yes_p, no_p, age_ms, band,
                filled_entry_source=filled_entry_source,
                stage_timings=stage_timings, opp_to_order_ms=opp_to_order_ms, ack_ms=ack_ms)
            if residual is None:
                self.risk.halt("partial exit not confirmed")
                return
            self._after_residual(st, combined, effective_size, residual, age_ms, band, "exit_no_quote",
                                 reserved)
            return

        # The filled leg is already owned at its recorded execution price.  Using
        # its later live ask here fabricates a zero-slippage basis and can accept or
        # reject a hedge for the wrong reason.
        hedge_combined = filled_entry_price + missing_ask
        # Hedge only if the WORSE (current) combined still leaves edge over the
        # (often tighter) partial hedge floor.
        if hedge_combined <= 1.0 - SETTINGS.partial_hedge_min_edge:
            # Hedge: cross the spread for the missing side at its current ask. Hedge
            # in the SAME quantity as the filled leg so the pair completes 1:1 —
            # hedging a full `size` after a partial fill would over-deploy capital.
            hedge_reserved = float(missing_ask) * effective_size
            hedge_reason = await self.risk.reserve(st, hedge_reserved, 0.0)
            if hedge_reason is not None:
                log.warning(
                    f"_manage_partial hedge blocked by risk for {st.market_id}: {hedge_reason}")
                hedge_id = None
            else:
                try:
                    if self._submission_allowed(
                        "execution.partial_hedge", missing_token, "BUY"
                    ):
                        hedge_id = await self.client.submit_order(
                            missing_token, float(missing_ask), effective_size, side="BUY"
                        )
                    else:
                        hedge_id = None
                except Exception as e:
                    log.error(f"_manage_partial hedge submit failed for {st.market_id}: {e!r}")
                    hedge_id = None
                if hedge_id is None:
                    self.risk.unreserve(st.market_id, hedge_reserved)
            # H3: confirm the hedge actually FILLED before declaring hedged. If it is
            #     only resting, we must NOT also run the exit path (that would open a
            #     hedge BUY and an exit SELL on the filled leg simultaneously =
            #     double exposure). Cancel the resting hedge and fall through to exit.
            hedge_filled = False
            if hedge_id is not None:
                try:
                    hedge_filled = await self.client.is_filled(
                        hedge_id, missing_token, gap_ms=SETTINGS.max_leg_gap_ms)
                except Exception as e:
                    log.warning(f"_manage_partial hedge fill check failed for {st.market_id}: {e!r}")
            if hedge_filled:
                hedge_fill_price, hedge_fill_source = self._fill_details(
                    hedge_id, float(missing_ask)
                )
                try:
                    hedge_ratio = self.client.get_fill_ratio(hedge_id) if hedge_id else 1.0
                except Exception as e:
                    # The exchange confirmed this hedge, so treating its fill as zero
                    # would understate exposure. Convert the whole reservation to
                    # deployed capital, halt for reconciliation, and return normally
                    # so the pending partial is removed instead of retrying a hedge.
                    log.error(
                        f"_manage_partial hedge fill-ratio lookup failed for {st.market_id}: {e!r}")
                    self.risk.commit(st.market_id, hedge_reserved,
                                     from_reservation=hedge_reserved)
                    self.risk.halt("confirmed hedge fill ratio unavailable")
                    return
                hedge_ratio = min(1.0, max(0.0, hedge_ratio))
                hedge_cost = hedge_reserved * hedge_ratio
                # Convert the worst-case hedge reservation to the cost actually
                # filled.  This ensures the hedge is visible to cap checks while it
                # is in flight and cannot disappear from accounting on resolution.
                self.risk.commit(st.market_id, hedge_cost, from_reservation=hedge_reserved)
                if hedge_ratio < 1.0:
                    # A partially-filled hedge is still a mismatched position. Do
                    # not declare the pair resolved or release either leg's capital.
                    # Keeping both filled costs deployed makes the cap conservative
                    # until an operator reconciles it.
                    self.risk.halt("partial hedge not fully filled")
                    return
                self.metrics.partial_resolved += 1
                # Locked PnL uses the worst of the two fills' combined cost, scaling
                # by the smaller of the two leg ratios (the pair can only realize PnL
                # on the qty both sides actually filled).
                resolved_combined = filled_entry_price + hedge_fill_price
                pair_size = effective_size
                pnl = max(0.0, (1.0 - resolved_combined)) * pair_size
                self.metrics.residual_pnl += pnl
                self.metrics.session_pnl += pnl
                self.metrics.realized_pnl += pnl
                # The original committed cost (ratio-scaled) is now fully realized:
                # both legs are filled and the pair is complete. Release the deployed
                # capital so it returns to available for new trades.  Both the
                # original filled leg and the now-confirmed hedge were committed, so
                # release both exactly once.
                self.risk.release(st.market_id, reserved + hedge_cost)
                self.risk.record_fill_pnl(pnl)
                log.trade("partial_resolved", {
                    "market_id": st.market_id, "action": "hedge",
                    "decision_combined": round(combined, 4),
                    "filled_entry_price": round(filled_entry_price, 4),
                    "filled_entry_source": filled_entry_source,
                    "hedge_fill_price": round(hedge_fill_price, 4),
                    "hedge_fill_source": hedge_fill_source,
                    "resolved_combined": round(resolved_combined, 4),
                    "size": round(pair_size, 8),
                    "expected_pnl": round(expected_pnl, 4),
                    "realized_pnl": round(pnl, 4),
                    "age_ms": round(age_ms, 1), "freshness_band": band,
                    "pnl_locked": round(pnl, 4),
                    "stage_timings": _format_stage_timings(stage_timings),
                    "opp_to_order_ms": round(opp_to_order_ms, 3),
                    "ack_ms": round(ack_ms, 3),
                })
                log.info(f"PARTIAL HEDGED pnl_locked={pnl:.4f} qty={pair_size:.0f}")
                self._record(st, resolved_combined, pair_size, filled_id, hedge_id, True, True, pnl, "hedged")
                return
            # Hedge did not fill -> cancel it so we don't leave two live orders.
            if not await self._cancel_or_manual_review(
                missing_token, hedge_id, "partial hedge cancellation",
            ):
                return
            if hedge_id is not None:
                self.risk.unreserve(st.market_id, hedge_reserved)

        # (3) No hedgeable edge (or hedge didn't fill) -> EXIT the filled leg at the
        #     best bid to cap the loss.
        residual = await self._exit_filled(
            st, tpl, filled_token, filled_id, effective_size, combined, yes_p, no_p, age_ms, band,
            filled_entry_source=filled_entry_source,
            stage_timings=stage_timings, opp_to_order_ms=opp_to_order_ms, ack_ms=ack_ms)
        if residual is None:
            self.risk.halt("partial exit not confirmed")
            return
        # The missing leg never filled, so flattening releases the full pair reservation.
        self._after_residual(st, combined, effective_size, residual, age_ms, band, "exit",
                             reserved)

    async def _exit_filled(self, st, tpl, filled_token, filled_id, size, combined,
                           yes_p, no_p, age_ms, band, stage_timings=None,
                           opp_to_order_ms: float = 0.0, ack_ms: float = 0.0,
                           filled_entry_source: str = "decision_fallback"):
        """Sell the filled leg back at the best bid (market) to flatten the position.

        Returns the realized residual PnL for the event (proceeds - cost of the filled
        leg). Negative = a realized loss; this is the number that must be reported
        SEPARATELY from clean-arb PnL (FIX #2). Cost uses the SNAPSHOT fill price
        (M4), not the live ask, so residual PnL is not misreported by a later tick.
        Returns None when the exit is not confirmed; callers halt rather than claiming
        a flattened position that may still be naked.
        """
        # M4: cost uses the gated snapshot price of the FILLED leg, not the live book.
        fill_price = yes_p if filled_token == tpl.yes_token_id else no_p
        cost = fill_price * size
        # Proceeds fallback: best WS BID on the filled side. PAPER_TRADE uses a
        # fresh REST best bid via submit_protective_paper_exit below and then reads
        # the recorded execution price instead of assuming this fallback filled.
        bid = st.best_yes_bid if filled_token == tpl.yes_token_id else st.best_no_bid
        protective_paper_exit = getattr(self.client, "submit_protective_paper_exit", None)
        use_protective_paper_exit = bool(
            getattr(self.client, "_paper_trade", False)
            and callable(protective_paper_exit)
        )
        if bid is None and not use_protective_paper_exit:
            # No bid quote -> can't exit cleanly; record worst-case (full cost lost).
            log.critical(f"EXIT FAILED no bid for {filled_token[-8:]} — exposure remains")
            return None
        # Do not report a realized exit until the sell is submitted and confirmed.
        try:
            if self._submission_allowed("execution.partial_exit", filled_token, "SELL"):
                if use_protective_paper_exit:
                    exit_id = await protective_paper_exit(filled_token, size)
                else:
                    exit_id = await self.client.submit_order(
                        filled_token, float(bid), size, side="SELL"
                    )
            else:
                exit_id = None
        except Exception as e:
            log.error(f"_exit_filled submit failed for {filled_token[-8:]}: {e!r}")
            exit_id = None
        if exit_id is None:
            log.critical(f"EXIT FAILED submit rejected for {filled_token[-8:]} — exposure remains")
            return None
        try:
            exit_filled = await self.client.is_filled(
                exit_id, filled_token, gap_ms=SETTINGS.max_leg_gap_ms)
        except Exception as e:
            log.error(f"_exit_filled confirmation failed for {filled_token[-8:]}: {e!r}")
            exit_filled = False
        if not exit_filled:
            await self._cancel_or_manual_review(
                filled_token, exit_id, "partial exit cancellation",
            )
            log.critical(f"EXIT FAILED unconfirmed for {filled_token[-8:]} — exposure remains")
            return None
        exit_price, exit_price_source = self._fill_details(exit_id, float(bid or 0.0))
        proceeds = exit_price * size
        residual = proceeds - cost
        log.trade("partial_resolved", {
            "market_id": st.market_id, "action": "exit",
            "decision_combined": round(combined, 4),
            "filled_entry_price": round(fill_price, 4),
            "filled_entry_source": filled_entry_source,
            "exit_price": round(exit_price, 4),
            "exit_price_source": exit_price_source,
            "ws_bid_at_exit_decision": round(float(bid), 4) if bid is not None else None,
            "cost": round(cost, 4),
            "proceeds": round(proceeds, 4), "residual_pnl": round(residual, 4),
            "size": round(size, 8),
            "expected_pnl": round((1.0 - combined) * size, 4),
            "realized_pnl": round(residual, 4),
            "pnl_locked": round(residual, 4),
            "age_ms": round(age_ms, 1), "freshness_band": band,
            "stage_timings": _format_stage_timings(stage_timings),
            "opp_to_order_ms": round(opp_to_order_ms, 3),
            "ack_ms": round(ack_ms, 3),
        })
        log.info(f"EXIT filled leg residual_pnl={residual:.4f}")
        return residual

    def _after_residual(self, st, combined, size, residual, age_ms, band, action,
                        released_cost=0.0):
        self.metrics.residual_pnl += residual
        self.metrics.session_pnl += residual
        self.metrics.realized_pnl += residual
        self.metrics.partial_resolved += 1
        self.risk.record_fill_pnl(residual)
        # Release the deployed cost for the fully-resolved position. For a partial exit
        # this is the original pair reservation, because the missing leg never filled.
        if released_cost > 0:
            self.risk.release(st.market_id, released_cost)
        self._record(st, combined, size, None, None, False, False, residual, "exit")
        # Kill switch (FIX #2): if residual loss breaches the cap, halt everything.
        if residual < -SETTINGS.partial_loss_cap_usd:
            log.critical(
                f"PARTIAL LOSS CAP BREACHED: residual={residual:.2f} > "
                f"-{SETTINGS.partial_loss_cap_usd:.2f} — halting"
            )
            # TASK B: fire kill-switch alert (fire-and-forget; no-op if alerter disabled).
            if self.alerter is not None:
                self.alerter.alert(
                    "kill_switch",
                    f"PARTIAL_LOSS_CAP_USD kill switch tripped: "
                    f"residual={residual:.2f} < -{SETTINGS.partial_loss_cap_usd:.2f}",
                    market_id=st.market_id,
                    residual_usd=round(residual, 2),
                    cap_usd=SETTINGS.partial_loss_cap_usd,
                    action=action,
                )
            self.risk.halt("partial loss cap")

    def _record(self, st, combined, size, yes_id, no_id, yf, nf, pnl, kind):
        rec = TradeRecord(
            market_id=st.market_id, ts=time.time(), yes_price=st.best_yes_ask,
            no_price=st.best_no_ask, combined=combined, size=size,
            yes_order_id=yes_id, no_order_id=no_id,
            yes_filled=yf, no_filled=nf, pnl_locked=pnl,
            mode=getattr(self.client, "mode", "live"),
        )
        self.records[st.market_id] = rec

    @staticmethod
    def _log_task_error(task):
        # E1: surface exceptions in background tasks instead of silently losing them.
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.error(f"fill-resolution task failed: {exc!r}")

    # FIX #6: called by PriceFeed on every tick (on_price_update). Kicks off the deferred
    # fill-resolution cycle for any in-flight fire on this market. This is the "NEXT tick's
    # execution cycle" — we probe pending fills here with non-blocking is_filled(gap_ms=0)
    # and resolve clean / partial / neither branches. Fire-and-forget: we don't await it
    # in the hot path, but we seed it as a task so it survives across ticks.
    def on_price_update(self, st: MarketState):
        """Tick-driven callback (PriceFeed.on_update -> arb_engine -> execution).

        Schedules _check_fills if there are pending fills; the probe runs concurrently
        with the arb decision for THIS tick so neither path blocks the other.
        """
        if not self._pending:
            return
        # Don't create redundant overlapping probes: if a probe is already in flight,
        # the next tick's on_price_update will simply schedule another — but the
        # _pending_lock inside _check_fills serializes safely, and the double-check on
        # `self._pending.get(key)` returning None handles races.
        task = asyncio.create_task(self._check_fills())
        task.add_done_callback(self._log_task_error)  # E1: surface exceptions from fill-resolution
