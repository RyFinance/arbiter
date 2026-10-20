"""Risk controls — checked in the execution path, before any order goes out.

HARD FAIL-SAFE stance: if a check fails (staleness, liquidity, daily cap,
emergency file), we ABORT both legs. One-sided fills are the worst outcome in an
arb, so we never fire a leg when the other is in doubt.

All checks are pure math over cached numbers — no network, no parsing. Keeps the
hot path cheap.

Differences from polymarket_arb's risk.py, both deliberate:

1. No `balance` field/precheck. polymarket_arb's RiskState.balance was set once
   at construction (`RiskState(balance=SETTINGS.max_total_capital)`, main.py's own
   comment called it a "placeholder preload") and never updated again -- the
   `cost > self.balance` check could therefore never reject anything
   max_total_capital wouldn't already reject. Rather than port a check that's
   dead by construction, it's removed; max_total_capital/max_position_per_market
   are the real capital caps.
2. Macro-calendar risk is an injectable `MacroCalendarProvider`, not a hard
   dynamic import of a specific scraper module. polymarket_arb's risk.py loaded
   scrapers/page_agent_runner.py via a hand-built synthetic sys.modules entry so
   its relative imports would resolve -- fragile, fail-closed-guarded but still a
   maintainability trap. polyuni has no macro-calendar scraper at all yet; the
   default `NullMacroCalendarProvider` reports "no macro risk, always healthy",
   so macro gating is fully opt-in by injecting a real provider later rather than
   mandatory dead-weight coupling today.
"""
from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Protocol, Tuple

from .alerting import ALERT_DAILY_KILL_SWITCH, ALERT_KILL_SWITCH, ALERT_MACRO_STATE_FAILURE, WebhookAlerter
from .config import SETTINGS
from .logger import log
from .models import MarketState


class MacroCalendarProvider(Protocol):
    """Injectable macro/news-risk source. See NullMacroCalendarProvider for the
    default (no macro gating at all)."""

    def health(self, now: float) -> Tuple[Optional[str], Optional[str], Dict[str, object]]:
        """Return (failure_code, detail, fields). failure_code is None when healthy."""
        ...

    def is_high_risk(self, now: float) -> bool:
        """True iff `now` falls inside a high-impact macro event window."""
        ...


class NullMacroCalendarProvider:
    """Default provider: always healthy, never high-risk. No macro gating occurs
    unless a real provider is injected into RiskState."""

    def health(self, now: float) -> Tuple[Optional[str], Optional[str], Dict[str, object]]:
        return None, None, {}

    def is_high_risk(self, now: float) -> bool:
        return False


@dataclass()
class RiskState:
    """Per-tick risk snapshot consumed by the kill-switch and pre-fire risk gate.

    HARD FAIL-SAFE stance: if any check fails (staleness, liquidity, daily cap,
    partial-loss cap), the corresponding fire is blocked rather than submitted and
    re-evaluated next tick.
    """

    day_pnl: float = 0.0
    deployed_per_market: Dict[str, float] = field(default_factory=dict)
    total_deployed: float = 0.0
    halted: bool = False
    # Capital reserved by an in-flight fire that has passed precheck but not yet
    # committed/released (network round trip still pending). Concurrent fire tasks
    # must see each other's reservations or two fires can both pass the same cap
    # and over-deploy. `reserve()` mutates these atomically under `_lock`.
    pending_deployed: float = 0.0
    pending_per_market: Dict[str, float] = field(default_factory=dict)
    _lock: Optional[asyncio.Lock] = field(default=None, repr=False, compare=False)
    alerter: Optional[WebhookAlerter] = field(default=None, repr=False, compare=False)
    # Avoid alert storms while the daily cap stays breached.
    _daily_kill_alerted: bool = field(default=False, repr=False, compare=False)
    macro_provider: MacroCalendarProvider = field(default_factory=NullMacroCalendarProvider)
    # Macro-state alerting is transition based because WebhookAlerter intentionally
    # persists every alert() call before its webhook-level time deduplication.
    _active_macro_failure: Optional[str] = field(default=None, repr=False, compare=False)
    # Applied ONLY while macro_provider.is_high_risk() is true.
    macro_edge_widen: float = 0.005      # widen MIN_EDGE by this much under macro risk
    macro_no_entry_extra_s: float = 0.0  # widen no_entry_seconds by this many under risk

    def _get_lock(self) -> asyncio.Lock:
        # Lazy init: asyncio.Lock() requires a running loop at construction time,
        # which breaks synchronous unit tests that construct RiskState outside asyncio.
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def emergency_triggered(self) -> bool:
        try:
            return os.path.exists(SETTINGS.emergency_stop_file)
        except OSError:
            return False

    def _macro_state_gate(self, now: float) -> Optional[str]:
        """Evaluate macro-provider health and manage failure/recovery transitions.
        Returns a rejection reason string, or None if healthy."""
        code, detail, fields = self.macro_provider.health(now)
        if code is None:
            if self._active_macro_failure is not None:
                log.info(
                    f"macro-risk state recovered from {self._active_macro_failure}; "
                    "entry gate re-enabled"
                )
            self._active_macro_failure = None
            return None

        reason = f"macro state {code.replace('_', ' ')}"
        if code != self._active_macro_failure:
            self._active_macro_failure = code
            alerter = self.alerter or WebhookAlerter(webhook_url=SETTINGS.alert_webhook_url)
            alerter.alert(
                ALERT_MACRO_STATE_FAILURE,
                f"MACRO STATE FAIL-SAFE: {detail}; entries blocked",
                failure=code,
                error=detail,
                **fields,
            )
        return reason

    def _macro_no_entry_hit(self, st: MarketState, now: float) -> bool:
        """Stricter no-entry check applied under high macro risk: widen the
        no-entry zone by `macro_no_entry_extra_s`."""
        if not st.end_time:
            return False
        effective = SETTINGS.no_entry_seconds + self.macro_no_entry_extra_s
        return (st.end_time - now) <= effective

    def _macro_edge_reject(
        self, st: MarketState, snapshot_combined: Optional[float] = None
    ) -> Optional[str]:
        """Under high macro risk, require a wider effective MIN_EDGE. Reject when
        the snapshot combined-ask no longer clears
        `1.0 - (min_edge + macro_edge_widen)`."""
        yes, no = st.best_yes_ask, st.best_no_ask
        if yes is None or no is None:
            # No book to evaluate the widened edge against -- let the standard
            # liquidity/stale checks surface the real reason.
            return None
        combined = yes + no if snapshot_combined is None else snapshot_combined
        edge_threshold = 1.0 - (SETTINGS.min_edge + self.macro_edge_widen)
        if combined >= edge_threshold:
            return (f"macro high-risk edge {combined:.4f} >= {edge_threshold:.4f} "
                    f"(min_edge + {self.macro_edge_widen})")
        return None

    def precheck(
        self,
        st: MarketState,
        cost_yes: float,
        cost_no: float,
        snapshot_combined: Optional[float] = None,
        *,
        require_pair_edge: bool = True,
    ) -> Optional[str]:
        """Return None if OK, else a reason string. Called BEFORE submitting."""
        now = time.time()
        macro_failure = self._macro_state_gate(now)
        if self.halted or self.emergency_triggered():
            return "halted/emergency-stop"
        if macro_failure is not None:
            return macro_failure
        if st.last_update_ts and (now - st.last_update_ts) > SETTINGS.stale_book_timeout_s:
            return f"stale book {now - st.last_update_ts:.1f}s"
        if st.yes_size is not None and st.yes_size < SETTINGS.min_liquidity_per_side:
            return "low YES liquidity"
        if st.no_size is not None and st.no_size < SETTINGS.min_liquidity_per_side:
            return "low NO liquidity"
        cost = cost_yes + cost_no
        # Include in-flight reservations so concurrent fires can't both pass. A
        # reservation is capital that a sibling fire has already been approved for
        # but not yet committed (its submit() is on the wire).
        eff_total = self.total_deployed + self.pending_deployed
        eff_market = (self.deployed_per_market.get(st.market_id, 0.0)
                      + self.pending_per_market.get(st.market_id, 0.0))
        if eff_total + cost > SETTINGS.max_total_capital:
            return "max capital exceeded"
        if eff_market + cost > SETTINGS.max_position_per_market:
            return "max position/market exceeded"
        if self.day_pnl - cost < -SETTINGS.daily_kill_switch_usd:
            # Alert on daily kill-switch breach. The bot does NOT halt on this
            # (it's a per-trade rejection, not a full stop), but operators must
            # know the daily loss cap was hit. Deduplicated to avoid alert storms
            # while the cap stays breached.
            if self.alerter is not None and not self._daily_kill_alerted:
                self.alerter.alert(
                    ALERT_DAILY_KILL_SWITCH,
                    f"DAILY_KILL_SWITCH_USD cap hit: day_pnl={self.day_pnl:.2f} "
                    f"cost={cost:.2f} cap=-{SETTINGS.daily_kill_switch_usd:.2f}",
                    day_pnl=round(self.day_pnl, 2),
                    cost=round(cost, 2),
                    cap_usd=SETTINGS.daily_kill_switch_usd,
                )
                self._daily_kill_alerted = True
            return "daily kill switch hit"

        # Macro-risk tightening: active only during a high-risk window; widens the
        # effective MIN_EDGE (stricter) and the no-entry zone, so trades that would
        # normally sail through get rejected on news/spread-blowup risk.
        if self.macro_provider.is_high_risk(now):
            # The simultaneous strategy must already own both legs before this
            # edge test is meaningful. A sequential first leg has no pair yet;
            # applying YES+NO here would reject the very volatility-driven entry
            # it is designed to make. Macro availability and no-entry gates still
            # apply to every new leg.
            if require_pair_edge:
                r = self._macro_edge_reject(st, snapshot_combined)
                if r is not None:
                    return r
            if self._macro_no_entry_hit(st, now):
                return f"macro high-risk no-entry zone (end_time {st.end_time - now:+.1f}s)"
        return None

    async def reserve(
        self,
        st: MarketState,
        cost_yes: float,
        cost_no: float,
        snapshot_combined: Optional[float] = None,
        *,
        require_pair_edge: bool = True,
    ) -> Optional[str]:
        """Atomic precheck + reservation. Serializes the check-and-reserve across
        concurrent fire tasks so two fires can never both pass the same cap and
        over-deploy. Returns None + reserves `cost_yes+cost_no` on success, else a
        reason string with nothing reserved. The caller MUST later call `commit()`
        (converts the reservation to deployed) or `unreserve()` (on abort).
        """
        async with self._get_lock():
            reason = self.precheck(
                st,
                cost_yes,
                cost_no,
                snapshot_combined,
                require_pair_edge=require_pair_edge,
            )
            if reason is not None:
                return reason
            cost = cost_yes + cost_no
            self.pending_deployed += cost
            self.pending_per_market[st.market_id] = (
                self.pending_per_market.get(st.market_id, 0.0) + cost)
            return None

    def unreserve(self, market_id: str, cost: float):
        """Release a reservation made by `reserve()` without deploying (fire
        aborted before commit, e.g. no fill)."""
        self.pending_deployed = max(0.0, self.pending_deployed - cost)
        cur = self.pending_per_market.get(market_id, 0.0)
        self.pending_per_market[market_id] = max(0.0, cur - cost)
        if self.pending_per_market.get(market_id, 0.0) == 0.0:
            self.pending_per_market.pop(market_id, None)

    def commit(self, market_id: str, cost: float, from_reservation: float = 0.0):
        """Move `cost` into deployed capital. If this fill was covered by a prior
        `reserve()`, pass `from_reservation` to draw down that reservation so the
        pending pool doesn't double-count."""
        self.total_deployed += cost
        self.deployed_per_market[market_id] = self.deployed_per_market.get(market_id, 0.0) + cost
        if from_reservation > 0:
            self.unreserve(market_id, from_reservation)

    def restore_deployed(self, market_id: str, cost: float):
        """Restore conservative deployed exposure during startup reconciliation."""
        if cost <= 0:
            return
        self.total_deployed += cost
        self.deployed_per_market[market_id] = self.deployed_per_market.get(market_id, 0.0) + cost

    def release(self, market_id: str, cost: float):
        """Free deployed capital (e.g. after exiting a filled leg in a partial
        event) so total_deployed / per-market caps stay accurate."""
        self.total_deployed = max(0.0, self.total_deployed - cost)
        cur = self.deployed_per_market.get(market_id, 0.0)
        self.deployed_per_market[market_id] = max(0.0, cur - cost)
        if self.deployed_per_market.get(market_id, 0.0) == 0.0:
            self.deployed_per_market.pop(market_id, None)

    def record_fill_pnl(self, pnl_locked: float):
        self.day_pnl += pnl_locked
        # Reset the daily-kill-switch alert flag when we recover. day_pnl is
        # cumulative for the session; if it swings back above the cap threshold
        # the alert should be re-armable for a future breach.
        if self._daily_kill_alerted and self.day_pnl >= -SETTINGS.daily_kill_switch_usd:
            self._daily_kill_alerted = False

    def halt(self, reason: str):
        """Trip the halt flag so every subsequent precheck/reserve fails fast and
        no new trades go out. Idempotent + logged. Fires ALERT_KILL_SWITCH if an
        alerter is attached."""
        if not self.halted:
            self.halted = True
            log.critical(f"RISK HALT: {reason} — no further trades will be sent")
            if self.alerter is not None:
                self.alerter.alert(
                    ALERT_KILL_SWITCH,
                    f"Risk halt triggered: {reason}",
                    reason=reason,
                )
