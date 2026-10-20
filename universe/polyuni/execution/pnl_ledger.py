"""PnlLedger — the single owner of locked/residual/would-lock PnL accounting and
the TradeRecord/OrderLedger bookkeeping that goes with a resolved fire.

Split out of polymarket_arb's execution.py, where this PnL math was interleaved
three times (near-identically) across the clean-fill, hedge-resolution, and
exit-resolution code paths. Consolidating it here means FillTracker and
PartialFillRecovery both call the same accounting methods instead of each
re-deriving "how do I update locked_pnl/session_pnl/realized_pnl and tell risk
about it" slightly differently.
"""
from __future__ import annotations

import time
from typing import Dict, Optional

from ..alerting import WebhookAlerter
from ..config import SETTINGS
from ..logger import log
from ..metrics import Metrics
from ..models import MarketState, TradeRecord
from ..order_ledger import OrderLedger
from ..risk import RiskState


def _format_stage_timings(stages_s: Optional[Dict[str, float]]) -> dict:
    """Convert internal stage timings (seconds) to a JSON-safe dict (milliseconds).
    Empty dict when unavailable (e.g. TRADE events logged before submission)."""
    if not stages_s:
        return {}
    return {k: round(v * 1000.0, 3) for k, v in stages_s.items()}


class PnlLedger:
    def __init__(
        self,
        metrics: Metrics,
        risk: RiskState,
        ledger: Optional[OrderLedger] = None,
        alerter: Optional[WebhookAlerter] = None,
    ):
        self.metrics = metrics
        self.risk = risk
        self.ledger = ledger
        self.alerter = alerter
        self.records: Dict[str, TradeRecord] = {}
        # Per-market cumulative PnL. `records` above only ever holds the LATEST
        # TradeRecord per market_id (each new trade overwrites it), so it can't
        # answer "how much has this market made in total" -- this dict
        # accumulates instead of overwriting, which is what a per-market PnL
        # breakdown actually needs.
        self._per_market_locked: Dict[str, float] = {}
        self._per_market_residual: Dict[str, float] = {}

    def _bump_market(self, market_id: str, *, locked: float = 0.0, residual: float = 0.0) -> None:
        if locked:
            self._per_market_locked[market_id] = self._per_market_locked.get(market_id, 0.0) + locked
        if residual:
            self._per_market_residual[market_id] = self._per_market_residual.get(market_id, 0.0) + residual

    def resolve_ledger(self, entry: Optional[dict], status: str, amount: float) -> None:
        record = (entry or {}).get("ledger_record")
        if self.ledger is not None and record is not None:
            self.ledger.append_resolved(record, status=status, final_committed_amount=amount)

    def _record(self, st: MarketState, combined: float, size: float,
                yes_id: Optional[str], no_id: Optional[str],
                yf: bool, nf: bool, pnl: float, kind: str, mode: str) -> None:
        rec = TradeRecord(
            market_id=st.market_id, ts=time.time(), yes_price=st.best_yes_ask,
            no_price=st.best_no_ask, combined=combined, size=size,
            yes_order_id=yes_id, no_order_id=no_id,
            yes_filled=yf, no_filled=nf, pnl_locked=pnl, mode=mode,
        )
        self.records[st.market_id] = rec

    def record_clean_fill(
        self, st: MarketState, *, mode: str,
        snapshot_combined: float, fill_yes_p: float, fill_no_p: float,
        fill_yes_source: str, fill_no_source: str,
        effective_size: float, yes_id: Optional[str], no_id: Optional[str],
        yes_p: float, no_p: float, age_ms: float, band: str,
        stage_timings: Optional[dict], opp_to_order_ms: float, ack_ms: float,
    ) -> float:
        """Both legs confirmed filled -- clean arb. Returns the realized pnl."""
        fill_combined = fill_yes_p + fill_no_p
        expected_pnl = (1.0 - snapshot_combined) * effective_size
        pnl = (1.0 - fill_combined) * effective_size
        self.metrics.locked_pnl += pnl
        self.metrics.session_pnl += pnl
        self.metrics.realized_pnl += pnl
        self.risk.record_fill_pnl(pnl)
        self._bump_market(st.market_id, locked=pnl)
        log.trade("filled", {
            "market_id": st.market_id, "combined": round(fill_combined, 4),
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
            "stage_timings": _format_stage_timings(stage_timings),
            "opp_to_order_ms": round(opp_to_order_ms, 3),
            "ack_ms": round(ack_ms, 3),
        })
        log.info(f"FILLED pnl_locked={pnl:.4f} qty={effective_size:.0f}")
        self._record(st, fill_combined, effective_size, yes_id, no_id, True, True, pnl, "clean", mode)
        return pnl

    def record_hedge_resolution(
        self, st: MarketState, *, mode: str,
        combined: float, filled_entry_price: float, filled_entry_source: str,
        hedge_fill_price: float, hedge_fill_source: str, pair_size: float,
        filled_id: Optional[str], hedge_id: Optional[str],
        age_ms: float, band: str,
        stage_timings: Optional[dict], opp_to_order_ms: float, ack_ms: float,
    ) -> float:
        """A partial fill's missing leg was successfully hedged. Returns realized pnl."""
        resolved_combined = filled_entry_price + hedge_fill_price
        expected_pnl = (1.0 - combined) * pair_size
        pnl = max(0.0, (1.0 - resolved_combined)) * pair_size
        self.metrics.partial_resolved += 1
        self.metrics.residual_pnl += pnl
        self.metrics.session_pnl += pnl
        self.metrics.realized_pnl += pnl
        self.risk.record_fill_pnl(pnl)
        self._bump_market(st.market_id, residual=pnl)
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
        self._record(st, resolved_combined, pair_size, filled_id, hedge_id, True, True, pnl, "hedged", mode)
        return pnl

    def record_exit(
        self, st: MarketState, *,
        combined: float, fill_price: float, filled_entry_source: str,
        exit_price: float, exit_price_source: str, bid_at_decision: Optional[float],
        size: float, age_ms: float, band: str,
        stage_timings: Optional[dict], opp_to_order_ms: float, ack_ms: float,
    ) -> float:
        """A filled leg was flattened at market (no hedge, or hedge unavailable).
        Returns the realized residual PnL (proceeds - cost); negative = a loss."""
        cost = fill_price * size
        proceeds = exit_price * size
        residual = proceeds - cost
        log.trade("partial_resolved", {
            "market_id": st.market_id, "action": "exit",
            "decision_combined": round(combined, 4),
            "filled_entry_price": round(fill_price, 4),
            "filled_entry_source": filled_entry_source,
            "exit_price": round(exit_price, 4),
            "exit_price_source": exit_price_source,
            "ws_bid_at_exit_decision": round(float(bid_at_decision), 4) if bid_at_decision is not None else None,
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

    def after_residual(
        self, st: MarketState, *, mode: str,
        combined: float, size: float, residual: float, age_ms: float, band: str,
        action: str, released_cost: float = 0.0,
    ) -> None:
        """Book a residual (non-clean) resolution's PnL, release its capital, and
        trip the kill switch if the loss breaches PARTIAL_LOSS_CAP_USD."""
        self.metrics.residual_pnl += residual
        self.metrics.session_pnl += residual
        self.metrics.realized_pnl += residual
        self.metrics.partial_resolved += 1
        self.risk.record_fill_pnl(residual)
        self._bump_market(st.market_id, residual=residual)
        if released_cost > 0:
            self.risk.release(st.market_id, released_cost)
        self._record(st, combined, size, None, None, False, False, residual, "exit", mode)
        if residual < -SETTINGS.partial_loss_cap_usd:
            log.critical(
                f"PARTIAL LOSS CAP BREACHED: residual={residual:.2f} > "
                f"-{SETTINGS.partial_loss_cap_usd:.2f} — halting"
            )
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

    def record_missed(
        self, st: MarketState, *, reason: str, snapshot_combined: float, size: float,
        age_ms: float, band: str, stage_timings: Optional[dict] = None,
        opp_to_order_ms: float = 0.0, ack_ms: float = 0.0,
        accumulate_would_lock: bool = True,
    ) -> None:
        """accumulate_would_lock=False for the post-attempt no-fill-timeout path
        (FillTracker's no_fill_in_window): the original execution.py only fed
        metrics.would_lock_pnl from the three PRE-attempt gate-rejection sites
        (snapshot_no_edge, rest_preflight_failed, risk-reject) inside
        execute_dual_buy, not from a fire that was actually attempted and timed
        out waiting for a fill -- those are a different economic event (tried and
        didn't land, vs gated out before even trying), so the distinction is
        preserved here rather than silently merged when the three near-duplicate
        would_lock call sites were consolidated into this one shared method."""
        self.metrics.record_freshness_attempt(age_ms, filled=False)
        self.metrics.missed += 1
        would_lock = max(0.0, 1.0 - snapshot_combined) * size
        if accumulate_would_lock:
            self.metrics.record_would_lock(would_lock)
        log.trade("missed", {
            "reason": reason, "market_id": st.market_id,
            "combined": round(snapshot_combined, 4), "age_ms": round(age_ms, 1),
            "freshness_band": band, "size": size,
            "would_lock": round(would_lock, 4),
            "stage_timings": _format_stage_timings(stage_timings),
            "opp_to_order_ms": round(opp_to_order_ms, 3),
            "ack_ms": round(ack_ms, 3),
        })

    def record_sequential_pair(
        self,
        st: MarketState,
        *,
        mode: str,
        first_side: str,
        first_price: float,
        second_price: float,
        first_fee: float,
        second_fee: float,
        size: float,
        first_order_id: Optional[str],
        second_order_id: Optional[str],
        leg_gap_s: float,
    ) -> float:
        """Book a fee-adjusted pair acquired in two intentionally separate legs."""
        combined = first_price + second_price
        fees = first_fee + second_fee
        gross = (1.0 - combined) * size
        pnl = gross - fees
        self.metrics.fills += 1
        self.metrics.sequential_pairs += 1
        self.metrics.record_fills_for_mode(mode)
        self.metrics.locked_pnl += pnl
        self.metrics.session_pnl += pnl
        self.metrics.realized_pnl += pnl
        self.risk.record_fill_pnl(pnl)
        self._bump_market(st.market_id, locked=pnl)
        if first_side == "YES":
            yes_price, no_price = first_price, second_price
            yes_id, no_id = first_order_id, second_order_id
        else:
            yes_price, no_price = second_price, first_price
            yes_id, no_id = second_order_id, first_order_id
        self.records[st.market_id] = TradeRecord(
            market_id=st.market_id,
            ts=time.time(),
            yes_price=yes_price,
            no_price=no_price,
            combined=combined,
            size=size,
            yes_order_id=yes_id,
            no_order_id=no_id,
            yes_filled=True,
            no_filled=True,
            pnl_locked=pnl,
            mode=mode,
            fees_paid=fees,
            strategy="sequential",
            first_leg_side=first_side,
            leg_gap_s=leg_gap_s,
        )
        log.trade("sequential_pair_filled", {
            "market_id": st.market_id,
            "first_side": first_side,
            "first_price": round(first_price, 5),
            "second_price": round(second_price, 5),
            "combined": round(combined, 5),
            "fees_paid": round(fees, 5),
            "gross_pnl": round(gross, 5),
            "net_pnl": round(pnl, 5),
            "net_edge_per_share": round(pnl / size, 6) if size else 0.0,
            "size": size,
            "leg_gap_s": round(leg_gap_s, 3),
            "mode": mode,
        })
        log.info(
            f"SEQUENTIAL PAIR FILLED market={st.market_id} combined={combined:.4f} "
            f"fees={fees:.5f} net={pnl:.4f} gap={leg_gap_s:.2f}s"
        )
        return pnl

    def record_sequential_exit(
        self,
        st: MarketState,
        *,
        mode: str,
        side: str,
        entry_price: float,
        entry_fee: float,
        exit_price: float,
        exit_fee: float,
        size: float,
        held_s: float,
        reason: str,
    ) -> float:
        """Book a protective exit after the second-leg deadline expires."""
        residual = (exit_price - entry_price) * size - entry_fee - exit_fee
        self.metrics.residual_pnl += residual
        self.metrics.session_pnl += residual
        self.metrics.realized_pnl += residual
        self.metrics.partial_resolved += 1
        self.metrics.sequential_exits += 1
        self.risk.record_fill_pnl(residual)
        self._bump_market(st.market_id, residual=residual)
        if residual < -SETTINGS.partial_loss_cap_usd:
            log.critical(
                f"SEQUENTIAL EXIT LOSS CAP BREACHED: residual={residual:.2f} "
                f"cap=-{SETTINGS.partial_loss_cap_usd:.2f}"
            )
            if self.alerter is not None:
                self.alerter.alert(
                    "kill_switch",
                    "Sequential protective exit breached PARTIAL_LOSS_CAP_USD",
                    market_id=st.market_id,
                    residual_usd=round(residual, 2),
                    cap_usd=SETTINGS.partial_loss_cap_usd,
                    action="sequential_exit",
                )
            self.risk.halt("sequential exit loss cap")
        self.records[st.market_id] = TradeRecord(
            market_id=st.market_id,
            ts=time.time(),
            yes_price=entry_price if side == "YES" else 0.0,
            no_price=entry_price if side == "NO" else 0.0,
            combined=entry_price,
            size=size,
            yes_filled=False,
            no_filled=False,
            pnl_locked=residual,
            residual_pnl=residual,
            mode=mode,
            fees_paid=entry_fee + exit_fee,
            strategy="sequential_exit",
            first_leg_side=side,
            leg_gap_s=held_s,
        )
        log.trade("sequential_exit", {
            "market_id": st.market_id,
            "side": side,
            "entry_price": round(entry_price, 5),
            "exit_price": round(exit_price, 5),
            "entry_fee": round(entry_fee, 5),
            "exit_fee": round(exit_fee, 5),
            "residual_pnl": round(residual, 5),
            "held_s": round(held_s, 3),
            "reason": reason,
            "mode": mode,
        })
        return residual

    def snapshot(self) -> dict:
        """Canonical PnL snapshot: realized (locked/residual/session), forgone
        edge (would_lock), capital currently at risk, and a per-market
        breakdown. Called periodically by main.py (SETTINGS.pnl_publish_interval_s)
        so a live dashboard shows current numbers instead of only learning PnL
        at shutdown -- polymarket_arb's metrics.summary() was logged exactly
        once, at process exit, so any dashboard reading it showed stale/absent
        data for the entire life of a run.

        Note on "unrealized": once both legs of a pair are confirmed filled,
        the $1 payout is guaranteed regardless of the market's future price --
        that IS the arbitrage -- so a locked pair has no meaningful
        mark-to-market risk; locked_pnl is already economically realized even
        though the exchange hasn't settled yet. The only genuine "unrealized"
        exposure in this strategy is capital currently deployed but not yet
        resolved into a locked/residual PnL event (an in-flight partial, or a
        fire whose fill status isn't confirmed yet this tick) -- reported here
        as deployed capital, not a fabricated price re-estimate.
        """
        market_ids = (
            set(self._per_market_locked) | set(self._per_market_residual)
            | set(self.risk.deployed_per_market) | set(self.risk.pending_per_market)
        )
        per_market = {}
        for market_id in sorted(market_ids):
            locked = self._per_market_locked.get(market_id, 0.0)
            residual = self._per_market_residual.get(market_id, 0.0)
            per_market[market_id] = {
                "locked_pnl": round(locked, 4),
                "residual_pnl": round(residual, 4),
                "session_pnl": round(locked + residual, 4),
                "deployed_usd": round(self.risk.deployed_per_market.get(market_id, 0.0), 4),
                "pending_usd": round(self.risk.pending_per_market.get(market_id, 0.0), 4),
            }
        return {
            "generated_at": time.time(),
            "locked_pnl": round(self.metrics.locked_pnl, 4),
            "residual_pnl": round(self.metrics.residual_pnl, 4),
            "session_pnl": round(self.metrics.session_pnl, 4),
            "would_lock_pnl": round(self.metrics.would_lock_pnl, 4),
            "deployed_usd": round(self.risk.total_deployed, 4),
            "pending_usd": round(self.risk.pending_deployed, 4),
            "day_pnl": round(self.risk.day_pnl, 4),
            "loops": self.metrics.loops,
            "opportunities": self.metrics.opportunities,
            "missed": self.metrics.missed,
            "orders_sent": self.metrics.orders_sent,
            "fills": self.metrics.fills,
            "sequential_first_fills": self.metrics.sequential_first_fills,
            "sequential_pairs": self.metrics.sequential_pairs,
            "sequential_timeouts": self.metrics.sequential_timeouts,
            "sequential_exits": self.metrics.sequential_exits,
            "strategy_rejections": dict(sorted(self.metrics.strategy_rejections.items())),
            "per_market": per_market,
        }

    def publish(self, path: str) -> None:
        """Atomically write snapshot() to `path` (temp file + os.replace, same
        pattern as live_control.py's state write) so a concurrent reader (the
        dashboard) never observes a partially-written file."""
        import json
        import os
        from pathlib import Path

        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        payload = json.dumps(self.snapshot(), indent=2, sort_keys=True)
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        tmp.replace(target)
