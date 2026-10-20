"""Market universe discovery + lifecycle tracking.

Replaces polymarket_arb's window_tracker.py (one rotating BTC 5-minute slot) with
N-concurrent tracking of an arbitrary number of markets, each independently moving
through DISCOVERED -> QUALIFIED -> ACTIVE -> CLOSING -> RELEASED as Gamma reports
their liquidity/volume/spread/closed state on each sync pass.

Phase 1 scope: discovery + qualification + capacity ranking only. ACTIVE here means
"selected for tracking" -- Phase 2 hooks real CLOB WS subscription and
Execution.register()/release_market() to the ACTIVE/CLOSING transitions.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import httpx

from .clients.gamma_client import discover_markets
from .config import Settings
from .logger import log
from .models import ACTIVE, CLOSING, DISCOVERED, QUALIFIED, RELEASED, MarketMeta


def qualify_market(meta: MarketMeta, settings: Settings, now: Optional[float] = None) -> Tuple[bool, str]:
    """Pure filter: does this market clear the tradeable-arbitrage bar right now?

    Returns (passes, reason) -- reason is always populated (a human-readable
    pass/fail explanation) so both logging and tests can assert on *why*, not
    just the boolean.
    """
    t = now if now is not None else time.time()

    if getattr(settings, "btc_only", False) and not meta.is_btc_up_down:
        return False, "btc_only_mode"
    if not meta.enable_order_book:
        return False, "no_order_book"
    if not meta.accepting_orders:
        return False, "not_accepting_orders"
    if settings.exclude_neg_risk and meta.neg_risk:
        return False, "neg_risk_excluded"
    if meta.liquidity_usd < settings.min_liquidity_usd:
        return False, f"liquidity_{meta.liquidity_usd:.0f}_below_{settings.min_liquidity_usd:.0f}"
    # A just-opened five-minute window cannot possibly have 24-hour volume yet.
    # Its deterministic discovery plus the same liquidity/spread/order-book gates
    # are the appropriate bar; retaining the generic volume gate made every early
    # BTC window ineligible by construction.
    is_priority_short_window = settings.strategy_mode == "sequential" and meta.is_btc_up_down
    if meta.volume_24h_usd < settings.min_volume_24h_usd and not is_priority_short_window:
        return False, f"volume24h_{meta.volume_24h_usd:.0f}_below_{settings.min_volume_24h_usd:.0f}"
    if meta.spread_at_discovery is not None and meta.spread_at_discovery > settings.max_spread_at_mid:
        return False, f"spread_{meta.spread_at_discovery:.4f}_above_{settings.max_spread_at_mid:.4f}"
    if meta.end_time:
        time_to_resolution = meta.end_time - t
        if time_to_resolution < settings.min_time_to_resolution_s:
            return False, "resolves_too_soon"
        if settings.max_time_to_resolution_s is not None and time_to_resolution > settings.max_time_to_resolution_s:
            return False, "resolves_too_far_out"
    return True, "qualified"


@dataclass
class SyncResult:
    """Summary of one discovery+requalify pass, for logging/tests/dashboard."""

    discovered: int = 0
    newly_qualified: List[str] = field(default_factory=list)
    newly_disqualified: List[str] = field(default_factory=list)  # QUALIFIED/ACTIVE -> CLOSING
    promoted_to_active: List[str] = field(default_factory=list)
    demoted_from_active: List[str] = field(default_factory=list)  # capacity pressure, not disqualification
    state_counts: Dict[str, int] = field(default_factory=dict)


class MarketUniverse:
    """Tracks every market's MarketMeta + lifecycle_state across sync passes."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.markets: Dict[str, MarketMeta] = {}

    def active_markets(self) -> List[MarketMeta]:
        return [m for m in self.markets.values() if m.lifecycle_state == ACTIVE]

    def qualified_markets(self) -> List[MarketMeta]:
        return [m for m in self.markets.values() if m.lifecycle_state == QUALIFIED]

    def closing_markets(self) -> List[MarketMeta]:
        return [m for m in self.markets.values() if m.lifecycle_state == CLOSING]

    def finalize_closing(self, market_ids: Optional[List[str]] = None) -> List[str]:
        """Transition CLOSING -> RELEASED for the given market_ids (or every
        currently-CLOSING market if omitted).

        Deliberately a separate step from sync(): callers (Phase 2's main loop)
        must call Execution.release_market() for each CLOSING market_id first, so
        partial-fill recovery / final ledger entry / final PnL happen before the
        market is dropped from active tracking. Released markets stay in
        self.markets (state RELEASED, not deleted) so a dashboard history view
        can still show what was tracked.
        """
        targets = market_ids if market_ids is not None else [m.market_id for m in self.closing_markets()]
        released: List[str] = []
        for market_id in targets:
            meta = self.markets.get(market_id)
            if meta is not None and meta.lifecycle_state == CLOSING:
                meta.lifecycle_state = RELEASED
                released.append(market_id)
        return released

    async def sync(self, session: httpx.AsyncClient) -> SyncResult:
        """One discovery + requalify pass. A single Gamma fetch serves both:
        newly-discovered markets get qualified for the first time, and
        already-tracked markets get their metadata refreshed and re-qualified --
        a market Gamma no longer returns (closed, orders disabled) is exactly as
        informative as one explicitly reporting closed=true, so both paths share
        this one fetch rather than issuing a second targeted re-check per market.
        """
        now = time.time()
        result = SyncResult()

        fresh = await discover_markets(
            session,
            whitelist=self.settings.market_whitelist,
            include_btc_short_windows=self.settings.include_btc_short_windows,
        )
        result.discovered = len(fresh)
        fresh_ids = {m.market_id for m in fresh}

        for meta in fresh:
            existing = self.markets.get(meta.market_id)
            prior_state = existing.lifecycle_state if existing else DISCOVERED
            meta.lifecycle_state = prior_state
            passes, reason = qualify_market(meta, self.settings, now=now)

            if passes:
                if prior_state in (DISCOVERED, CLOSING, RELEASED):
                    meta.lifecycle_state = QUALIFIED
                    meta.last_qualified_at = now
                    result.newly_qualified.append(meta.market_id)
                else:
                    meta.lifecycle_state = prior_state  # stays QUALIFIED or ACTIVE
                    meta.last_qualified_at = now
            else:
                if prior_state in (QUALIFIED, ACTIVE):
                    meta.lifecycle_state = CLOSING
                    result.newly_disqualified.append(meta.market_id)
                    log.info(f"market {meta.market_id} disqualified: {reason}")
                else:
                    meta.lifecycle_state = DISCOVERED  # never qualified, stays pending

            self.markets[meta.market_id] = meta

        # Markets we were tracking that Gamma no longer returns at all -- closed
        # or otherwise dropped out of the active/accepting-orders set.
        for market_id, meta in list(self.markets.items()):
            if market_id in fresh_ids:
                continue
            if meta.lifecycle_state in (QUALIFIED, ACTIVE):
                meta.lifecycle_state = CLOSING
                result.newly_disqualified.append(market_id)
                log.info(f"market {market_id} disqualified: dropped_from_gamma_active_set")

        # Capacity gate: rank QUALIFIED+ACTIVE by volume, promote/keep only the top N.
        candidates = [m for m in self.markets.values() if m.lifecycle_state in (QUALIFIED, ACTIVE)]
        candidates.sort(
            key=lambda m: (
                bool(self.settings.strategy_mode == "sequential" and m.is_btc_up_down),
                m.volume_24h_usd,
            ),
            reverse=True,
        )
        keep_ids = {m.market_id for m in candidates[: self.settings.max_concurrent_markets]}
        for m in candidates:
            if m.market_id in keep_ids and m.lifecycle_state == QUALIFIED:
                m.lifecycle_state = ACTIVE
                result.promoted_to_active.append(m.market_id)
            elif m.market_id not in keep_ids and m.lifecycle_state == ACTIVE:
                m.lifecycle_state = QUALIFIED  # still eligible, just over capacity
                result.demoted_from_active.append(m.market_id)

        for meta in self.markets.values():
            result.state_counts[meta.lifecycle_state] = result.state_counts.get(meta.lifecycle_state, 0) + 1

        return result

    def snapshot(self) -> dict:
        """JSON-serializable view of the current universe for the dashboard.

        The dashboard runs as a separate process from the bot (standard
        separation -- it reads state the bot writes, they don't share memory),
        so this needs to be periodically persisted to disk the same way
        PnlLedger.snapshot()/publish() work, not just callable in-process.
        """
        markets = []
        now = time.time()
        for meta in sorted(self.markets.values(), key=lambda m: m.volume_24h_usd, reverse=True):
            markets.append({
                "market_id": meta.market_id,
                "question": meta.question,
                "slug": meta.slug,
                "start_time": meta.start_time,
                "outcome_yes": meta.outcome_yes,
                "outcome_no": meta.outcome_no,
                "is_btc_up_down": meta.is_btc_up_down,
                "lifecycle_state": meta.lifecycle_state,
                "liquidity_usd": round(meta.liquidity_usd, 2),
                "volume_24h_usd": round(meta.volume_24h_usd, 2),
                "spread_at_discovery": meta.spread_at_discovery,
                "seconds_to_resolution": (round(meta.end_time - now, 1) if meta.end_time else None),
                "end_time": meta.end_time or None,
                "yes_token_id": meta.yes_token_id,
                "no_token_id": meta.no_token_id,
                "neg_risk": meta.neg_risk,
            })
        state_counts: Dict[str, int] = {}
        for meta in self.markets.values():
            state_counts[meta.lifecycle_state] = state_counts.get(meta.lifecycle_state, 0) + 1
        return {
            "generated_at": now,
            "total_tracked": len(self.markets),
            "state_counts": state_counts,
            "max_concurrent_markets": self.settings.max_concurrent_markets,
            "markets": markets,
        }

    def publish(self, path: str) -> None:
        """Atomically write snapshot() to `path` (temp file + os.replace)."""
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
