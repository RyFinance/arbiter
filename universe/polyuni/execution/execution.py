"""Execution — thin coordinator wiring OrderRouter + FillTracker +
PartialFillRecovery + PnlLedger together, and owning execute_dual_buy (the
hot-path entry point that genuinely spans all four: risk reserve, submit both
legs, stash for deferred fill tracking, initial ledger append).

This replaces polymarket_arb's single ~1200-line Execution class. The public
surface (register/add_market/release_market/execute_dual_buy/on_price_update/
set_invalidator) is unchanged so main.py doesn't need to know about the split.
"""
from __future__ import annotations

import asyncio
import time
from typing import Callable, Optional

from ..alerting import ALERT_REPEATED_REJECTIONS, WebhookAlerter
from ..clients.trading_client import TradingClient
from ..config import SETTINGS
from ..fees import taker_fee
from ..logger import log
from ..metrics import Metrics
from ..models import MarketState
from ..order_ledger import OrderLedger
from ..risk import RiskState
from .fill_tracker import FillTracker
from .order_router import OrderRouter, OrderTemplate
from .partial_fill_recovery import PartialFillRecovery
from .pnl_ledger import PnlLedger


class Execution:
    """Order lifecycle manager — entry, deferred fill tracking, partial-fill
    recovery, PnL. See the module docstring for the collaborator split."""

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
        self.alerter = alerter
        self.ledger = ledger

        self.order_router = OrderRouter(client, risk)
        self.pnl_ledger = PnlLedger(metrics, risk, ledger=ledger, alerter=alerter)
        self.partial_recovery = PartialFillRecovery(client, risk, self.order_router, self.pnl_ledger)
        self.fill_tracker = FillTracker(client, risk, self.order_router, self.pnl_ledger, self.partial_recovery)

        # Track consecutive order rejections (submit_order returning None) across
        # the hot path. Fires ALERT_REPEATED_REJECTIONS at 3+ in a row so operators
        # know the exchange is rejecting our orders before the bot silently misses
        # every arb.
        self._consecutive_rejections: int = 0

    @property
    def templates(self):
        return self.order_router.templates

    @property
    def records(self):
        return self.pnl_ledger.records

    def set_invalidator(self, cb: Callable[[str], None]) -> None:
        """Wire a callback (ArbEngine.invalidate) invoked after a fire produced no
        exposure, to un-wedge the fire debounce."""
        self.fill_tracker.set_invalidator(cb)

    # ---- COLD PATH: build templates once per market ----
    def register(self, st: MarketState, size: float = None) -> None:
        self.order_router.register(st, size)

    def add_market(self, st: MarketState, size: float = None) -> OrderTemplate:
        """Register a market promoted to ACTIVE by market_universe.py mid-session."""
        return self.order_router.add_market(st, size)

    def release_market(self, market_id: str) -> None:
        """Release all deployed capital and cancel any resting orders for a market
        that market_universe.py has moved to CLOSING.

        Call this BEFORE market_universe.MarketUniverse.finalize_closing(market_id)
        so partial-fill recovery / final ledger entry happen before the market is
        dropped from active tracking.

        Clean-arb and resolved-partial fills commit cost into risk at fill time,
        but that capital must be freed once the market closes, or total_deployed
        only grows and precheck eventually halts all trading.
        """
        # Pending orders have not yet been reconciled. Do not free either their
        # deployed cost or a reservation while their cancellation is uncertain.
        to_remove = self.fill_tracker.pending_for_market(market_id)
        has_pending_orders = bool(to_remove)
        if not has_pending_orders:
            held = self.risk.deployed_per_market.get(market_id, 0.0)
            if held > 0:
                self.risk.release(market_id, held)
        # Also clear any in-flight RESERVATION for this market. If a fire was
        # reserved but not yet committed at close time, the reservation would
        # otherwise leak into pending_deployed and spuriously block new trades.
        pending = self.risk.pending_per_market.get(market_id, 0.0)
        if pending > 0 and not has_pending_orders:
            self.risk.unreserve(market_id, pending)
        # Cancel any pending order legs for this market that haven't resolved yet
        # (optimistically committed but not yet confirmed filled). Without this, a
        # market that closes while orders are still resting would leave dangling
        # orders + committed capital with no one tracking the fills.
        for k in to_remove:
            entry = self.fill_tracker.pop(k)
            if entry is None:
                continue
            tpl = entry["tpl"]
            yes_id = entry["yes_id"]
            no_id = entry["no_id"]
            if yes_id:
                task = asyncio.create_task(self.order_router.cancel_or_manual_review(
                    tpl.yes_token_id, yes_id, "market close cancellation"))
                task.add_done_callback(self._log_task_error)
            if no_id:
                task = asyncio.create_task(self.order_router.cancel_or_manual_review(
                    tpl.no_token_id, no_id, "market close cancellation"))
                task.add_done_callback(self._log_task_error)
            # Close cannot prove cancellation/fill outcome synchronously. Keep the
            # audit trail conservative so an operator can review this exposure.
            self.pnl_ledger.resolve_ledger(entry, "manual_review", entry.get("committed_cost", 0.0))
        self.order_router.release_template(market_id)
        self.pnl_ledger.records.pop(market_id, None)

    async def execute_sequential_buy(
        self,
        st: MarketState,
        *,
        side: str,
        limit_price: float,
        size: float,
        fee_rate: float,
        fee_exponent: float,
        sequence_id: str,
        stage: str,
    ) -> dict:
        """Buy and confirm one leg of a deliberately time-separated paper pair.

        Capital is reserved and committed one leg at a time. The method returns
        the simulator's actual depth-walked fill price so the second-leg ceiling
        and PnL never rely on the earlier top-of-book observation.
        """
        mode = getattr(self.client, "mode", "live")
        if mode == "live":
            return {"ok": False, "reason": "paper_only"}
        tpl = self.order_router.templates.get(st.market_id)
        if tpl is None:
            return {"ok": False, "reason": "template_unavailable"}
        side = side.upper()
        token_id = tpl.yes_token_id if side == "YES" else tpl.no_token_id
        estimated_fee = taker_fee(size, limit_price, fee_rate, fee_exponent)
        estimated_cost = limit_price * size + estimated_fee
        reason = await self.risk.reserve(
            st,
            estimated_cost if side == "YES" else 0.0,
            estimated_cost if side == "NO" else 0.0,
            require_pair_edge=False,
        )
        if reason is not None:
            return {"ok": False, "reason": "risk:" + reason}

        try:
            order_id, started, done = await self.order_router.submit_leg(
                token_id, limit_price, size, limit_price
            )
        except Exception as exc:
            self.risk.unreserve(st.market_id, estimated_cost)
            return {"ok": False, "reason": "submit_exception", "error": repr(exc)}
        if order_id is None:
            self.risk.unreserve(st.market_id, estimated_cost)
            self._consecutive_rejections += 1
            return {"ok": False, "reason": "order_rejected"}

        self.metrics.orders_sent += 1
        self.metrics.record_orders_sent(mode, 1)
        try:
            filled = await self.client.is_filled(order_id, token_id, gap_ms=SETTINGS.max_leg_gap_ms)
        except Exception as exc:
            filled = False
            log.warning(f"sequential fill probe failed for {st.market_id}: {exc!r}")
        if not filled:
            cancelled = await self.order_router.cancel_or_manual_review(
                token_id, order_id, f"sequential {stage} no-fill cancellation"
            )
            if cancelled:
                self.risk.unreserve(st.market_id, estimated_cost)
            return {"ok": False, "reason": "no_fill" if cancelled else "manual_review"}

        ratio = min(1.0, max(0.0, float(self.client.get_fill_ratio(order_id))))
        if ratio < 1.0:
            # The production build is paper-only and its depth walk is FOK, so a
            # partial ratio indicates a broken/custom client. Keep the reservation
            # conservative and halt instead of pretending a matched quantity.
            actual_price, _ = self.order_router.fill_details(order_id, limit_price)
            committed = (actual_price * size + taker_fee(
                size, actual_price, fee_rate, fee_exponent
            )) * ratio
            self.risk.commit(st.market_id, committed, from_reservation=estimated_cost)
            self.risk.halt("sequential leg was partially filled")
            return {"ok": False, "reason": "partial_fill", "committed_cost": committed}

        actual_price, fill_source = self.order_router.fill_details(order_id, limit_price)
        actual_fee = taker_fee(size, actual_price, fee_rate, fee_exponent)
        actual_cost = actual_price * size + actual_fee
        self.risk.commit(st.market_id, actual_cost, from_reservation=estimated_cost)
        self._consecutive_rejections = 0
        if stage == "first":
            self.metrics.sequential_first_fills += 1
        if self.ledger is not None:
            self.ledger.append_sequence_event(
                sequence_id=sequence_id,
                market_id=st.market_id,
                status=f"{stage}_filled",
                side=side,
                token_id=token_id,
                order_id=order_id,
                price=actual_price,
                size=size,
                fee=actual_fee,
                note=fill_source,
            )
        return {
            "ok": True,
            "side": side,
            "token_id": token_id,
            "order_id": order_id,
            "price": actual_price,
            "fee": actual_fee,
            "cost": actual_cost,
            "size": size,
            "fill_source": fill_source,
            "mode": mode,
            "submit_ms": (done - started) * 1000.0,
        }

    async def execute_sequential_exit(
        self,
        st: MarketState,
        *,
        open_leg: dict,
        fee_rate: float,
        fee_exponent: float,
        reason: str,
    ) -> dict:
        """Flatten an unmatched first leg at a fresh paper best bid."""
        side = str(open_leg["side"]).upper()
        token_id = str(open_leg["token_id"])
        size = float(open_leg["size"])
        bid = st.best_yes_bid if side == "YES" else st.best_no_bid
        exit_id = await self.order_router.submit_exit(token_id, bid, size)
        if exit_id is None:
            self.risk.halt("sequential protective exit rejected")
            return {"ok": False, "reason": "exit_rejected"}
        try:
            filled = await self.client.is_filled(
                exit_id, token_id, gap_ms=SETTINGS.max_leg_gap_ms
            )
        except Exception as exc:
            log.error(f"sequential exit confirmation failed: {exc!r}")
            filled = False
        if not filled:
            await self.order_router.cancel_or_manual_review(
                token_id, exit_id, "sequential protective exit cancellation"
            )
            self.risk.halt("sequential protective exit unconfirmed")
            return {"ok": False, "reason": "exit_unconfirmed"}
        exit_price, fill_source = self.order_router.fill_details(exit_id, float(bid or 0.0))
        exit_fee = taker_fee(size, exit_price, fee_rate, fee_exponent)
        held_s = max(0.0, time.time() - float(open_leg["opened_at"]))
        residual = self.pnl_ledger.record_sequential_exit(
            st,
            mode=str(open_leg.get("mode") or getattr(self.client, "mode", "live")),
            side=side,
            entry_price=float(open_leg["price"]),
            entry_fee=float(open_leg["fee"]),
            exit_price=exit_price,
            exit_fee=exit_fee,
            size=size,
            held_s=held_s,
            reason=reason,
        )
        self.risk.release(st.market_id, float(open_leg["cost"]))
        if self.ledger is not None:
            self.ledger.append_sequence_event(
                sequence_id=str(open_leg["sequence_id"]),
                market_id=st.market_id,
                status="exited",
                side=side,
                token_id=token_id,
                order_id=exit_id,
                price=exit_price,
                size=size,
                fee=exit_fee,
                note=reason,
            )
        return {
            "ok": True,
            "order_id": exit_id,
            "price": exit_price,
            "fee": exit_fee,
            "pnl": residual,
            "fill_source": fill_source,
        }

    # ---- HOT PATH core: called by arb_engine.on_fire ----
    async def execute_dual_buy(
        self, st: MarketState, combined: float, decision_ts: float,
        age_ms: float = 0.0, band: str = "fresh",
        yes_p: Optional[float] = None, no_p: Optional[float] = None,
    ):
        """FIRE (non-blocking). Validate -> reserve -> submit both legs concurrently
        -> schedule fill-check on next tick -> RETURN IMMEDIATELY.

        Latency-critical hot path: get money on the table ASAP, find out later if
        it stuck. Blocking inside this coroutine on is_filled() stalls the event
        loop between the WS tick handler and the next tick.

        `yes_p`/`no_p` are the SNAPSHOT that passed the arb gate in arb_engine: we
        submit at those prices and re-assert the edge against them, ignoring any
        later WS ticks that mutated st.best_yes_ask/no_ask after the gate fired.
        """
        tpl = self.order_router.templates.get(st.market_id)
        if tpl is None:
            # Reachable during market discovery/release races. A silent return
            # makes a rejected fire indistinguishable from no opportunity at all.
            self.metrics.missed += 1
            self.metrics.template_unavailable += 1
            log.error(f"execute_dual_buy rejected: no order template for {st.market_id}")
            log.trade("missed", {
                "reason": "template_unavailable", "market_id": st.market_id,
                "combined": round(combined, 4), "age_ms": round(age_ms, 1),
                "freshness_band": band,
            })
            return

        if yes_p is None:
            yes_p = st.best_yes_ask
        if no_p is None:
            no_p = st.best_no_ask
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
        # Re-assert the edge against the SNAPSHOT before risking any order. If the
        # snapshot no longer clears, skip (this can happen if evaluate's combined
        # was computed from a transient book state; better safe than a wrong-sided
        # fill).
        if snapshot_combined >= edge_price:
            self.pnl_ledger.record_missed(
                st, reason="snapshot_no_edge", snapshot_combined=snapshot_combined,
                size=size, age_ms=age_ms, band=band,
            )
            return

        # REST-path paper orders are otherwise vulnerable to a known two-feed race:
        # the WS pair can clear the edge while one REST leg has already moved
        # through its limit. Revalidate both read-only books concurrently before
        # reserving any capital.
        pair_preflight = getattr(self.client, "paper_pair_preflight", None)
        if callable(pair_preflight):
            preflight = await pair_preflight(
                tpl.yes_token_id, tpl.no_token_id, yes_p, no_p, size, edge_price,
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
                    self.pnl_ledger.record_missed(
                        st, reason=preflight.get("reason", "rest_preflight_failed"),
                        snapshot_combined=snapshot_combined, size=size, age_ms=age_ms, band=band,
                    )
                    self.fill_tracker.notify_no_exposure(st.market_id)
                    return

        cost_yes = yes_p * size
        cost_no = no_p * size
        reserved = cost_yes + cost_no
        # LATENCY-SENSITIVE SECTION START (decision_ts set in arb_engine).
        reserve_started = time.monotonic()
        reason = await self.risk.reserve(st, cost_yes, cost_no, snapshot_combined=snapshot_combined)
        reserve_done = time.monotonic()
        if reason is not None:
            self.pnl_ledger.record_missed(
                st, reason="risk:" + reason, snapshot_combined=snapshot_combined,
                size=size, age_ms=age_ms, band=band,
            )
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
            # Submit both legs concurrently (closest to simultaneous submission) at
            # the SNAPSHOT prices. return_exceptions=True so that if one leg's
            # submit raises, we still capture the successful leg's result.
            submit_started = time.monotonic()
            yes_result, no_result = await asyncio.gather(
                self.order_router.submit_leg(tpl.yes_token_id, yes_p, size, yes_p),
                self.order_router.submit_leg(tpl.no_token_id, no_p, size, no_p),
                return_exceptions=True,
            )
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

            if yes_exc is not None or no_exc is not None:
                self.metrics.missed += 1
                exc = yes_exc or no_exc
                log.error(f"execute_dual_buy submit failed for {st.market_id}: {exc!r}; "
                          f"yes={yes_id} no={no_id}")
                cancellation_confirmed = True
                if yes_id:
                    cancellation_confirmed = await self.order_router.cancel_or_manual_review(
                        tpl.yes_token_id, yes_id, "submit sibling failure cancellation",
                    )
                if no_id:
                    cancellation_confirmed = (
                        await self.order_router.cancel_or_manual_review(
                            tpl.no_token_id, no_id, "submit sibling failure cancellation",
                        )
                        and cancellation_confirmed
                    )
                if cancellation_confirmed:
                    self.risk.unreserve(st.market_id, reserved)
                    self.fill_tracker.notify_no_exposure(st.market_id)
                return

            self.metrics.orders_sent += 2
            _mode = getattr(self.client, "mode", "live")
            self.metrics.record_orders_sent(_mode, 2)
            log.info(f"ACK yes_order={yes_id} no_order={no_id}")

            # Crash boundary: persist both returned IDs before mutating risk or the
            # volatile pending stash. A restart can therefore query their real state.
            ledger_record = None
            if self.ledger is not None:
                ledger_record = {
                    "market_id": st.market_id, "yes_id": yes_id, "no_id": no_id,
                    "yes_token_id": tpl.yes_token_id, "no_token_id": tpl.no_token_id,
                    "reserved": reserved, "mode": _mode,
                }
                self.ledger.append_pending(**ledger_record)

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
                        yes_id=yes_id, no_id=no_id,
                    )

            # OPTIMISTIC commit: both orders are live on the wire / resting. Treat
            # the reservation as deployed capital immediately so concurrent fires
            # see an honest cap. Record fill ratios BEFORE commit so capital is
            # committed at the ACTUALLY filled quantity, not the full requested size.
            yes_ratio = self.client.get_fill_ratio(yes_id) if yes_id else 0.0
            no_ratio = self.client.get_fill_ratio(no_id) if no_id else 0.0
            committed_cost = yes_p * size * yes_ratio + no_p * size * no_ratio
            self.risk.commit(st.market_id, committed_cost, from_reservation=reserved)
            commit_completed = True

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

            # Stash the pending fire so check_fills (driven by the next price tick)
            # can probe fill status non-blockingly.
            fire_seq = self.fill_tracker.next_fire_seq()
            pending_key = (st.market_id, yes_id, fire_seq)
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
            pending_entry_inserted = await self.fill_tracker.add_pending(pending_key, pending_entry)
            # DEFERRED fill-resolution: do NOT await is_filled here. Fill
            # confirmation happens on the NEXT tick's cycle via on_price_update.
        except Exception as e:
            # An unexpected exception escaped AFTER the dual-submit succeeded (from
            # get_fill_ratio, commit, metrics, or latency recording). Submit
            # failures are handled inline above (before commit). A later error can
            # happen after commit: release its deployed cost in that case;
            # otherwise clear the live reservation. Never do both.
            if commit_completed:
                self.risk.release(st.market_id, committed_cost)
                self.pnl_ledger.resolve_ledger(pending_entry or {"ledger_record": ledger_record},
                                                "manual_review", reserved)
            else:
                self.risk.unreserve(st.market_id, reserved)
            if (pending_entry_inserted and pending_key is not None
                    and self.fill_tracker.pending.get(pending_key) is pending_entry):
                self.fill_tracker.pop(pending_key)
            self.metrics.missed += 1
            log.error(f"execute_dual_buy aborted for {st.market_id}: {e!r}; "
                      f"yes={yes_id} no={no_id}")
            try:
                if yes_id:
                    await self.client.cancel_order(tpl.yes_token_id, missing_order_id=yes_id)
                if no_id:
                    await self.client.cancel_order(tpl.no_token_id, missing_order_id=no_id)
            except Exception as ce:
                log.error(f"emergency cancel failed for {st.market_id}: {ce!r}")
        # LATENCY-SENSITIVE SECTION END

    def on_price_update(self, st: MarketState) -> None:
        """Tick-driven callback (PriceFeed.on_update -> arb_engine -> execution)."""
        self.fill_tracker.on_price_update()

    @staticmethod
    def _log_task_error(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.error(f"fire task failed: {exc!r}")
