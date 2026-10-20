"""OrderRouter — order templates, submission gating, leg submission, cancellation.

Split out of polymarket_arb's execution.py god-object. Owns "how do we place and
cancel one leg," not fill tracking or PnL, which live in fill_tracker.py and
pnl_ledger.py respectively.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Dict, Optional

from ..clients.trading_client import TradingClient
from ..config import SETTINGS
from ..logger import log
from ..models import MarketState
from ..risk import RiskState


@dataclass()
class OrderTemplate:
    """COLD-PATH-prebuilt, HOT-PATH-mutated. Only price/size/ts change per fire."""
    market_id: str
    yes_token_id: str
    no_token_id: str
    size: float


class OrderRouter:
    """Order template lifecycle + submission/cancellation for one leg at a time.

    Stateless with respect to fills -- FillTracker owns everything about whether
    a submitted order actually filled.
    """

    def __init__(self, client: TradingClient, risk: RiskState):
        self.client = client
        self.risk = risk
        self.templates: Dict[str, OrderTemplate] = {}

    # ---- COLD PATH: build templates once per market ----
    def register(self, st: MarketState, size: Optional[float] = None) -> OrderTemplate:
        tpl = OrderTemplate(
            market_id=st.market_id,
            yes_token_id=st.yes_token_id,
            no_token_id=st.no_token_id,
            size=size if size is not None else SETTINGS.order_size,
        )
        self.templates[st.market_id] = tpl
        return tpl

    def add_market(self, st: MarketState, size: Optional[float] = None) -> OrderTemplate:
        """Register a market discovered/promoted mid-session (market_universe.py
        promoting a QUALIFIED market to ACTIVE)."""
        return self.register(st, size)

    def release_template(self, market_id: str) -> None:
        self.templates.pop(market_id, None)

    def _submission_allowed(self, call_site: str, token_id: str, side: str) -> bool:
        """Check the production client's live-control gate at every call site.

        Lightweight simulation/test doubles that don't expose this method are
        unaffected -- they never reach an exchange and don't pretend to be the
        production boundary.
        """
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

    async def submit_leg(
        self, token_id: str, price: float, size: float, observed_fill_price: float,
    ) -> tuple[Optional[str], float, float]:
        """Submit one BUY leg and retain the actual coroutine start/end timestamps."""
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

    async def submit_hedge(self, token_id: str, price: float, size: float) -> Optional[str]:
        """Submit a hedge BUY at the current (not decision-time) ask."""
        if not self._submission_allowed("execution.partial_hedge", token_id, "BUY"):
            return None
        try:
            return await self.client.submit_order(token_id, float(price), size, side="BUY")
        except Exception as e:
            log.error(f"hedge submit failed for {token_id[-8:]}: {e!r}")
            return None

    async def submit_exit(self, token_id: str, price: Optional[float], size: float) -> Optional[str]:
        """Submit a protective SELL to flatten a filled leg. Uses the paper
        protective-exit path (fresh REST bid) when available, else a plain SELL at
        `price`."""
        if not self._submission_allowed("execution.partial_exit", token_id, "SELL"):
            return None
        protective_paper_exit = getattr(self.client, "submit_protective_paper_exit", None)
        use_protective_paper_exit = bool(
            getattr(self.client, "_paper_trade", False) and callable(protective_paper_exit)
        )
        try:
            if use_protective_paper_exit:
                return await protective_paper_exit(token_id, size)
            if price is None:
                return None
            if getattr(self.client, "mode", "") == "live":
                # A protective exit must actually execute; FOK at the stale WS bid
                # is killed by one tick of movement and leaves the leg open.
                slippage = float(getattr(getattr(self.client, "s", None), "live_exit_slippage", 0.02))
                price = max(0.01, float(price) - slippage)
            return await self.client.submit_order(token_id, float(price), size, side="SELL")
        except Exception as e:
            log.error(f"exit submit failed for {token_id[-8:]}: {e!r}")
            return None

    async def cancel_or_manual_review(
        self, token_id: str, order_id: Optional[str], context: str,
    ) -> bool:
        """Cancel an order, making one status probe only if cancellation is uncertain.

        ``is_filled(False)`` cannot distinguish a cancelled order from a resting or
        otherwise unavailable one, so it is deliberately treated as unsafe. Callers
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

    def fill_details(self, order_id: Optional[str], fallback_price: float) -> tuple[float, str]:
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
