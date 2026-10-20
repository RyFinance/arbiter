"""PartialFillRecovery — the #1 real-bot loss driver: what happens when one leg
fills and the other doesn't within MAX_LEG_GAP_MS.

Never let a one-sided fill sit as an unmanaged directional bet:
  (1) cancel the resting (unfilled) order,
  (2) try to HEDGE it by crossing the spread -- only if the remaining edge still
      covers PARTIAL_HEDGE_MIN_EDGE,
  (3) otherwise EXIT the filled leg at the best bid (market) to cap the loss.

Residual PnL (hedge + exit outcome) is tracked SEPARATELY from clean-arb locked
PnL by PnlLedger, and a residual loss breaching PARTIAL_LOSS_CAP_USD trips the
kill switch (see PnlLedger.after_residual).
"""
from __future__ import annotations

from typing import Optional

from ..clients.trading_client import TradingClient
from ..config import SETTINGS
from ..logger import log
from ..models import MarketState
from ..risk import RiskState
from .order_router import OrderRouter, OrderTemplate
from .pnl_ledger import PnlLedger


class PartialFillRecovery:
    def __init__(
        self, client: TradingClient, risk: RiskState,
        order_router: OrderRouter, pnl_ledger: PnlLedger,
    ):
        self.client = client
        self.risk = risk
        self.order_router = order_router
        self.pnl_ledger = pnl_ledger

    async def manage_partial(
        self, st: MarketState, tpl: OrderTemplate, yes_id: Optional[str], no_id: Optional[str],
        yes_filled: bool, no_filled: bool,
        size: float, combined: float, yes_p: float, no_p: float, age_ms: float, band: str,
        reserved: float = 0.0, yes_ratio: float = 1.0, no_ratio: float = 1.0,
        fill_yes_source: str = "decision_fallback", fill_no_source: str = "decision_fallback",
        stage_timings: Optional[dict] = None, opp_to_order_ms: float = 0.0, ack_ms: float = 0.0,
    ) -> None:
        """Turn a partial fill into a bounded, hedged, or exited position.

        ``yes_ratio``/``no_ratio`` are the partial fill ratios returned by
        submit/get_fill_ratio for each leg. The FILLED leg's ratio expresses how
        much of it actually filled, so PnL and exit cost accounting must scale by
        it -- otherwise a 50%-filled leg is recorded as a full fill and residual
        PnL is overstated.
        """
        filled_token = tpl.yes_token_id if yes_filled else tpl.no_token_id
        missing_token = tpl.no_token_id if yes_filled else tpl.yes_token_id
        filled_id = yes_id if yes_filled else no_id
        filled_ratio = yes_ratio if yes_filled else no_ratio
        effective_size = size * filled_ratio
        filled_entry_price = yes_p if yes_filled else no_p
        filled_entry_source = fill_yes_source if yes_filled else fill_no_source
        mode = getattr(self.client, "mode", "live")

        # (1) Cancel the resting (unfilled) order first -- it can't be allowed to
        # fill later and leave us with an unexpected one-sided position.
        resting_id = no_id if yes_filled else yes_id
        if not await self.order_router.cancel_or_manual_review(
            missing_token, resting_id, "partial resting-leg cancellation",
        ):
            return

        # (2) Hedge attempt: buy the missing side at its CURRENT best ask (cross
        # spread), decided using the LIVE book, not the stale fire price.
        missing_ask = st.best_no_ask if yes_filled else st.best_yes_ask
        if missing_ask is None:
            residual = await self._exit_filled(
                st, tpl, filled_token, filled_id, effective_size, combined, yes_p, no_p, age_ms, band,
                filled_entry_source=filled_entry_source,
                stage_timings=stage_timings, opp_to_order_ms=opp_to_order_ms, ack_ms=ack_ms,
            )
            if residual is None:
                self.risk.halt("partial exit not confirmed")
                return
            self.pnl_ledger.after_residual(
                st, mode=mode, combined=combined, size=effective_size, residual=residual,
                age_ms=age_ms, band=band, action="exit_no_quote", released_cost=reserved,
            )
            return

        # The filled leg is already owned at its recorded execution price. Using a
        # later live ask here would fabricate a zero-slippage basis.
        hedge_combined = filled_entry_price + missing_ask
        if hedge_combined <= 1.0 - SETTINGS.partial_hedge_min_edge:
            hedge_reserved = float(missing_ask) * effective_size
            hedge_reason = await self.risk.reserve(st, hedge_reserved, 0.0)
            if hedge_reason is not None:
                log.warning(f"manage_partial hedge blocked by risk for {st.market_id}: {hedge_reason}")
                hedge_id = None
            else:
                hedge_id = await self.order_router.submit_hedge(missing_token, missing_ask, effective_size)
                if hedge_id is None:
                    self.risk.unreserve(st.market_id, hedge_reserved)

            # Confirm the hedge actually FILLED before declaring hedged. If it's
            # only resting, we must NOT also run the exit path (that would open a
            # hedge BUY and an exit SELL on the filled leg simultaneously = double
            # exposure). Cancel the resting hedge and fall through to exit.
            hedge_filled = False
            if hedge_id is not None:
                try:
                    hedge_filled = await self.client.is_filled(
                        hedge_id, missing_token, gap_ms=SETTINGS.max_leg_gap_ms)
                except Exception as e:
                    log.warning(f"manage_partial hedge fill check failed for {st.market_id}: {e!r}")

            if hedge_filled:
                hedge_fill_price, hedge_fill_source = self.order_router.fill_details(
                    hedge_id, float(missing_ask)
                )
                try:
                    hedge_ratio = self.client.get_fill_ratio(hedge_id) if hedge_id else 1.0
                except Exception as e:
                    # The exchange confirmed this hedge, so treating its fill as zero
                    # would understate exposure. Convert the whole reservation to
                    # deployed capital and halt for reconciliation.
                    log.error(f"manage_partial hedge fill-ratio lookup failed for {st.market_id}: {e!r}")
                    self.risk.commit(st.market_id, hedge_reserved, from_reservation=hedge_reserved)
                    self.risk.halt("confirmed hedge fill ratio unavailable")
                    return
                hedge_ratio = min(1.0, max(0.0, hedge_ratio))
                hedge_cost = hedge_reserved * hedge_ratio
                self.risk.commit(st.market_id, hedge_cost, from_reservation=hedge_reserved)
                if hedge_ratio < 1.0:
                    # A partially-filled hedge is still a mismatched position. Do not
                    # declare the pair resolved or release either leg's capital.
                    self.risk.halt("partial hedge not fully filled")
                    return

                pnl = self.pnl_ledger.record_hedge_resolution(
                    st, mode=mode, combined=combined,
                    filled_entry_price=filled_entry_price, filled_entry_source=filled_entry_source,
                    hedge_fill_price=hedge_fill_price, hedge_fill_source=hedge_fill_source,
                    pair_size=effective_size, filled_id=filled_id, hedge_id=hedge_id,
                    age_ms=age_ms, band=band, stage_timings=stage_timings,
                    opp_to_order_ms=opp_to_order_ms, ack_ms=ack_ms,
                )
                # Both the original filled leg and the now-confirmed hedge were
                # committed above -- release both exactly once.
                self.risk.release(st.market_id, reserved + hedge_cost)
                return

            # Hedge did not fill -> cancel it so we don't leave two live orders.
            if not await self.order_router.cancel_or_manual_review(
                missing_token, hedge_id, "partial hedge cancellation",
            ):
                return
            if hedge_id is not None:
                self.risk.unreserve(st.market_id, hedge_reserved)

        # (3) No hedgeable edge (or hedge didn't fill) -> EXIT the filled leg at
        # the best bid to cap the loss.
        residual = await self._exit_filled(
            st, tpl, filled_token, filled_id, effective_size, combined, yes_p, no_p, age_ms, band,
            filled_entry_source=filled_entry_source,
            stage_timings=stage_timings, opp_to_order_ms=opp_to_order_ms, ack_ms=ack_ms,
        )
        if residual is None:
            self.risk.halt("partial exit not confirmed")
            return
        # The missing leg never filled, so flattening releases the full pair reservation.
        self.pnl_ledger.after_residual(
            st, mode=mode, combined=combined, size=effective_size, residual=residual,
            age_ms=age_ms, band=band, action="exit", released_cost=reserved,
        )

    async def _exit_filled(
        self, st: MarketState, tpl: OrderTemplate, filled_token: str, filled_id: Optional[str],
        size: float, combined: float, yes_p: float, no_p: float, age_ms: float, band: str,
        stage_timings: Optional[dict] = None, opp_to_order_ms: float = 0.0, ack_ms: float = 0.0,
        filled_entry_source: str = "decision_fallback",
    ) -> Optional[float]:
        """Sell the filled leg back at the best bid (market) to flatten the position.

        Returns the realized residual PnL, or None when the exit is not confirmed
        -- callers halt rather than claiming a flattened position that may still
        be naked.

        Cost uses the gated SNAPSHOT price of the filled leg, not the live book,
        so residual PnL is not misreported by a later tick.
        """
        fill_price = yes_p if filled_token == tpl.yes_token_id else no_p
        bid = st.best_yes_bid if filled_token == tpl.yes_token_id else st.best_no_bid
        protective_paper_exit = getattr(self.client, "submit_protective_paper_exit", None)
        use_protective_paper_exit = bool(
            getattr(self.client, "_paper_trade", False) and callable(protective_paper_exit)
        )
        if bid is None and not use_protective_paper_exit:
            log.critical(f"EXIT FAILED no bid for {filled_token[-8:]} — exposure remains")
            return None

        exit_id = await self.order_router.submit_exit(filled_token, bid, size)
        if exit_id is None:
            log.critical(f"EXIT FAILED submit rejected for {filled_token[-8:]} — exposure remains")
            return None
        try:
            exit_filled = await self.client.is_filled(exit_id, filled_token, gap_ms=SETTINGS.max_leg_gap_ms)
        except Exception as e:
            log.error(f"_exit_filled confirmation failed for {filled_token[-8:]}: {e!r}")
            exit_filled = False
        if not exit_filled:
            await self.order_router.cancel_or_manual_review(
                filled_token, exit_id, "partial exit cancellation",
            )
            log.critical(f"EXIT FAILED unconfirmed for {filled_token[-8:]} — exposure remains")
            return None

        exit_price, exit_price_source = self.order_router.fill_details(exit_id, float(bid or 0.0))
        return self.pnl_ledger.record_exit(
            st, combined=combined, fill_price=fill_price, filled_entry_source=filled_entry_source,
            exit_price=exit_price, exit_price_source=exit_price_source, bid_at_decision=bid,
            size=size, age_ms=age_ms, band=band, stage_timings=stage_timings,
            opp_to_order_ms=opp_to_order_ms, ack_ms=ack_ms,
        )
