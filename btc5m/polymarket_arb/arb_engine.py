"""Arb engine — the detector. Deliberately trivial.

This is NOT where the edge is. The detector is one addition and a comparison. We keep
it as small as possible so the hot path from "price update" to "order submit" has the
fewest possible instructions. See README ("The detector is not the hard part").

HOT PATH: called once per price update for the affected market.
  * compute combined = YES_ask + NO_ask   (one float add)
  * FIX #1: reject if the older leg's quote is staler than MAX_QUOTE_AGE_MS at the
    moment of decision (median freshness is a vanity metric — we gate on the older leg
    because a single stale leg while the other looks fresh is what actually blows fills).
  * FIX #4: reject NEW entries inside the no-entry zone (final NO_ENTRY_SECONDS before
    window settlement, where spreads whip as the Chainlink reference becomes certain).
  * debounce: skip if we already fired on this exact (yes,no) price pair
"""
from __future__ import annotations

import asyncio
import time
from typing import Callable, Dict

from .config import SETTINGS
from .logger import log
from .metrics import Metrics, classify_freshness_ms
from .models import MarketState

# execution callback: engine -> execution (coroutine)
# signature: (market_state, combined, decision_ts, age_ms, band, yes_p, no_p)
Fire = Callable[[MarketState, float, float, float, str, float, float], None]


class ArbEngine:
    """Stateless edge detector that fans out arb fires to the execution layer.

    Called by ``PriceFeed`` on every book update (HOT PATH). Runs the four pre-fire
    gates in order — combined-ask edge, quote freshness (FIX #1), no-entry-zone
    debounce (FIX #4), and per-market combined-price debounce — and emits a single
    ``on_fire`` callback with the snapshot prices that cleared the gate. Does NOT
    submit orders itself; the snapshot it hands to ``on_fire`` is exact, so any
    latency between gate-clear and execution uses the same prices.

    Hot-path discipline: this method must return within microseconds per update, so
    no I/O and no allocations beyond the freshness band classification.
    """

    def __init__(self, on_fire: Fire, metrics: Metrics):
        self.on_fire = on_fire
        self.metrics = metrics
        # HOT PATH debounce: last fired combined price per market (avoids re-firing
        # on every WS book delta that doesn't change the arb condition meaningfully).
        self._last_fired: Dict[str, float] = {}
        # H1: retain fire tasks so they are not GC'd / cancelled mid-flight and so we
        # can surface exceptions instead of swallowing them.
        self._tasks: set = set()

    def evaluate(self, st: MarketState):
        """Called by price_feed on every update. HOT PATH — keep it minimal."""
        self.metrics.loops += 1
        combined = st.combined_ask
        if combined is None:
            return
        edge_price = 1.0 - SETTINGS.min_edge
        if combined >= edge_price:
            return  # no arb

        # FIX #1: quote-freshness gate. Measure age of the OLDER leg at THIS decision,
        # not at subscription time. Reject if either leg is staler than max_quote_age_ms.
        age_ms = st.max_age_ms()
        band = classify_freshness_ms(age_ms)
        if age_ms > SETTINGS.max_quote_age_ms:
            # Edge seen but quote too stale to trust -> count as missed + log the band
            # so we can measure how often staleness kills the trade (FIX #1 bucket).
            self.metrics.missed += 1
            self.metrics.record_freshness_attempt(age_ms, filled=False)
            would_lock = max(0.0, 1.0 - combined) * SETTINGS.order_size
            edge_surplus = max(0.0, edge_price - combined) * SETTINGS.order_size
            self.metrics.record_would_lock(would_lock)
            log.trade("missed", {
                "reason": "stale_quote", "market_id": st.market_id, "combined": round(combined, 4),
                "age_ms": round(age_ms, 1), "freshness_band": band,
                "size": SETTINGS.order_size,
                "would_lock": round(would_lock, 4),
                "edge_surplus": round(edge_surplus, 4),
            })
            return

        # FIX #4: no new entries inside the final NO_ENTRY_SECONDS before settlement.
        if st.in_no_entry_zone():
            self.metrics.missed += 1
            would_lock = max(0.0, 1.0 - combined) * SETTINGS.order_size
            edge_surplus = max(0.0, edge_price - combined) * SETTINGS.order_size
            self.metrics.record_would_lock(would_lock)
            log.trade("missed", {
                "reason": "no_entry_zone", "market_id": st.market_id, "combined": round(combined, 4),
                "age_ms": round(age_ms, 1), "freshness_band": band,
                "secs_to_close": round(st.window_close - time.time(), 1) if st.window_close else None,
                "size": SETTINGS.order_size,
                "would_lock": round(would_lock, 4),
                "edge_surplus": round(edge_surplus, 4),
            })
            return

        # HOT PATH: debounce duplicate firings on the same combined price.
        prev = self._last_fired.get(st.market_id)
        if prev is not None and prev == combined:
            return

        self.metrics.opportunities += 1
        self._last_fired[st.market_id] = combined
        edge = edge_price - combined
        log.info(
            f"{st.market_id} YES {st.best_yes_ask:.2f} NO {st.best_no_ask:.2f} "
            f"SUM {combined:.2f} EDGE {edge:.3f} age_ms={age_ms:.0f} band={band}"
        )
        # C1: snapshot the EXACT prices that passed the gate. Between here and
        # submission, WS ticks can mutate st.best_yes_ask/no_ask, so execution must
        # submit at THIS combined (and re-assert the edge) — not at whatever the book
        # says later. Pass the snapshot + age + band for freshness-at-fill logging.
        yes_p = st.best_yes_ask
        no_p = st.best_no_ask
        # LATENCY-SENSITIVE: hand off to execution immediately. We capture the
        # decision timestamp here; execution measures the rest to submission.
        # Schedule (don't await) so the detector never blocks on network round trips.
        task = asyncio.create_task(
            self.on_fire(st, combined, time.monotonic(), age_ms, band, yes_p, no_p)
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)  # H1: release when done
        task.add_done_callback(self._log_task_error)  # H1: surface exceptions

    def invalidate(self, market_id: str):
        """Clear the per-market combined-price debounce.

        Called by the execution layer after a fire produced ZERO exposure (both
        legs cancelled at the no-fill timeout, or a submit failure where every
        returned order was cancelled). Without this, a book that returns to the
        exact same combined price would never re-fire — the debounce would stay
        wedged on a dead trade and the arb opportunity would be silently lost.
        """
        self._last_fired.pop(market_id, None)

    @staticmethod
    def _log_task_error(task):
        # H1: a fire coroutine must never die silently (orphaned-task risk).
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.error(f"fire task failed: {exc!r}")
