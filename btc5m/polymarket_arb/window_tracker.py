"""Rolling-window targeting + conditional-edge tracking (FIX #3 + FIX #5).

This module owns the deterministic 5-minute window lifecycle and the per-window
measurement that tells us WHERE the edge actually lives (FIX #5) — not whether the
broad strategy is profitable by default. We do NOT assume the broad strategy wins;
we log every window's:

  * combined-ask-at-open        (the instant we subscribe to the window)
  * min-combined-ask-during-window  (best arb opportunity seen, ignoring staleness)
  * time-of-day bucket          (which 5-min slot — edge is often time/regime-bound)
  * outcome                     ('pending' -> 'trading' -> 'settled' once closed)

plus the slippage-stress verdict: can the slice survive an added 0.01+ slip? If not,
the slice is discarded — we never force a slice live just because the book looked open.

The module is COLD-ish: it is driven by a background task that (a) pre-resolves the NEXT
window's token ids WINDOW_PRE_RESOLVE_S before close and (b) records the open/min stats.
The hot path only reads `current_window` to tag TradeRecords / decide no-entry zones.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Dict, Optional

from .clients.gamma_client import (
    current_window_ts,
    next_window_ts,
    resolve_window_market,
    window_slug,
)
from .config import SETTINGS
from .logger import log
from .models import MarketMeta
from .alerting import WebhookAlerter, ALERT_MARKET_RESOLUTION_FAILURE


@dataclass()
class WindowObservation:
    """Per-window measurement record (FIX #5). Persisted to JSONL for slice analysis."""
    slug: str
    window_start: int
    window_close: int
    asset: str
    tf: str
    tod_bucket: str          # e.g. "00:00", "12:05" — 5-min slot of the day (UTC)
    combined_at_open: Optional[float] = None
    min_combined: Optional[float] = None
    min_combined_at: Optional[float] = None
    edge_seen: bool = False          # did combined ever drop below 1 - MIN_EDGE?
    outcome: str = "pending"         # pending -> trading -> settled
    survives_slippage: Optional[bool] = None  # verdict after stress test

    def to_log(self) -> dict:
        return {
            "slug": self.slug,
            "window_start": self.window_start,
            "window_close": self.window_close,
            "tod_bucket": self.tod_bucket,
            "combined_at_open": self.combined_at_open,
            "min_combined": self.min_combined,
            "edge_seen": self.edge_seen,
            "outcome": self.outcome,
            "survives_slippage": self.survives_slippage,
        }


def _tod_bucket(window_start: int) -> str:
    # UTC HH:MM of the window start, floored to the 5-min slot.
    from datetime import datetime, timezone
    dt = datetime.fromtimestamp(window_start, tz=timezone.utc)
    return f"{dt.hour:02d}:{dt.minute:02d}"


class WindowTracker:
    """Drives window rotation + records FIX #5 stats. Couples to main via callbacks.

    `on_window_ready(meta)` is called with the MarketMeta for a newly-resolved window so
    main.py can register it with the execution engine + price feed. `state_lookup` lets
    the tracker read live combined-ask to update min-combined per window.
    """

    def __init__(
        self,
        session,
        on_window_ready,
        state_lookup,
        on_window_settled=None,
        alerter: Optional[WebhookAlerter] = None,
    ):
        self.s = SETTINGS
        self._session = session
        self._on_window_ready = on_window_ready
        self._state_lookup = state_lookup  # callable(market_id) -> Optional[MarketState]
        self._on_window_settled = on_window_settled  # callable(market_id) -> None (HIGH)
        self.alerter = alerter
        self.resolve_failures = 0
        self.last_resolution_error: Optional[str] = None
        self.current_window: Optional[int] = None
        self.observations: Dict[str, WindowObservation] = {}
        self._resolved_next: Dict[int, MarketMeta] = {}
        # FIX #3: the next window is RESOLVED at rotation (no feed gap) but its market is
        # SUBSCRIBED/activated only within WINDOW_PRE_RESOLVE_S of the current close —
        # that is the actual meaning of the knob (pre-subscribe before close).
        self._next_window_ts: Optional[int] = None
        self._next_activated: bool = False
        self._stop = False
        self._task: Optional[asyncio.Task] = None
        # FIX #5 ENFORCEMENT: accumulate slippage-stress verdicts per time-of-day slice
        # so future windows in a slice that never survives the stress are SKIPPED (not
        # just logged). Fail-open until MIN_STRESS_SAMPLES observations exist.
        self._bucket_seen: Dict[str, int] = {}
        self._bucket_fail: Dict[str, int] = {}

    async def start(self):
        self._task = asyncio.create_task(self._loop(), name="window-tracker")

    async def _resolve(self, window_ts: int) -> Optional[MarketMeta]:
        slug = window_slug(self.s.window_asset, self.s.window_timeframe, window_ts)
        try:
            meta = await resolve_window_market(self._session, slug)
        except Exception as e:
            meta = None
            self.last_resolution_error = repr(e)
            log.error(f"Gamma resolve failed for {slug}: {e!r}")
        if meta is None:
            self.resolve_failures += 1
            detail = self.last_resolution_error or "Gamma returned no market"
            log.error(f"Gamma resolve unavailable for {slug} (failures={self.resolve_failures}): {detail}")
            if self.alerter is not None:
                self.alerter.alert(ALERT_MARKET_RESOLUTION_FAILURE,
                    f"Gamma market resolution failed for {slug}", slug=slug,
                    consecutive_failures=self.resolve_failures, error=detail)
            return None
        self.resolve_failures = 0
        self.last_resolution_error = None
        meta.market_id = slug
        # Carry window bounds on the meta (proper fields, serialized to cache).
        meta.window_start = window_ts
        meta.window_close = window_ts + self.s.window_seconds
        self._resolved_next[window_ts] = meta
        self.observations[slug] = WindowObservation(
            slug=slug,
            window_start=meta.window_start,
            window_close=meta.window_close,
            asset=self.s.window_asset,
            tf=self.s.window_timeframe,
            tod_bucket=_tod_bucket(meta.window_start),
        )
        return meta

    async def _loop(self):
        """COLD background: resolve current + NEXT window, then rotate at boundaries."""
        now = time.time()
        self.current_window = current_window_ts(now, self.s.window_seconds)
        meta = await self._resolve(self.current_window)
        if meta is not None:
            await self._activate(meta)
        # Resolve the following window now (no feed gap), but defer its activation until
        # we are within WINDOW_PRE_RESOLVE_S of the current close.
        self._next_window_ts = next_window_ts(self.current_window, self.s.window_seconds)
        self._next_activated = False
        await self._resolve(self._next_window_ts)

        while not self._stop:
            try:
                now = time.time()
                w = current_window_ts(now, self.s.window_seconds)
                if w != self.current_window:
                    # Window rotated: settle the previous, activate the new, pre-resolve next.
                    self.current_window = w
                    prev_slug = window_slug(
                        self.s.window_asset, self.s.window_timeframe,
                        w - self.s.window_seconds,
                    )
                    obs = self.observations.get(prev_slug)
                    if obs is not None and obs.outcome == "trading":
                        obs.outcome = "settled"
                        self._stress_test(obs)
                        log.trade("window", obs.to_log())
                    # HIGH: release the settled window's deployed capital so total_deployed
                    # does not grow without bound and eventually halt all trading.
                    if self._on_window_settled is not None:
                        try:
                            self._on_window_settled(prev_slug)
                        except Exception as e:
                            log.warning(f"on_window_settled failed: {e!r}")
                    meta = self._resolved_next.get(w)
                    if meta is not None:
                        await self._activate(meta)
                    else:
                        # M: pre-resolve returned None for this window (not yet published /
                        # transient Gamma miss). Retry immediately so we don't run a window
                        # with NO market subscribed (an unbounded feed gap).
                        log.warning(f"window {prev_slug} not pre-resolved; retrying")
                        meta = await self._resolve(w)
                        if meta is not None:
                            await self._activate(meta)
                    # Resolve the following window now (no feed gap); defer its activation.
                    self._next_window_ts = next_window_ts(w, self.s.window_seconds)
                    self._next_activated = False
                    await self._resolve(self._next_window_ts)
                    # LOW: prune settled observations so memory stays bounded over time.
                    self._prune()
                else:
                    # Stayed in the same window. If the CURRENT window was never activated
                    # (startup resolve missed it), keep retrying until it resolves — the
                    # previous rotation-only retry would skip the first window entirely.
                    cur_slug = window_slug(self.s.window_asset, self.s.window_timeframe, w)
                    if cur_slug not in self.observations or self.observations[cur_slug].outcome == "pending":
                        if w in self._resolved_next and self._resolved_next[w] is not None:
                            await self._activate(self._resolved_next[w])
                        else:
                            meta = await self._resolve(w)
                            if meta is not None:
                                await self._activate(meta)
                    # FIX #3: activate the NEXT window's market once we are within
                    # WINDOW_PRE_RESOLVE_S of the NEXT window's open (== current close).
                    elif (not self._next_activated and self._next_window_ts is not None
                          and self._next_window_ts in self._resolved_next
                          and self._next_window_ts - now <= self.s.window_pre_resolve_s):
                        nxt_meta = self._resolved_next[self._next_window_ts]
                        if nxt_meta is not None:
                            log.info(f"pre-subscribing next window {nxt_meta.market_id} "
                                      f"(within {self.s.window_pre_resolve_s}s of close)")
                            await self._activate(nxt_meta)
                            self._next_activated = True
            except Exception as e:
                # MEDIUM: contain per-iteration errors so one bad window doesn't kill
                # rotation permanently (log + continue).
                log.error(f"window loop iteration failed: {e!r}")
            self._update_active_min()
            await asyncio.sleep(0.5)

    def _prune(self):
        """LOW: drop observations for windows well in the past to bound memory."""
        cutoff = time.time() - self.s.window_seconds * 4
        stale = [slug for slug, o in self.observations.items()
                 if o.outcome == "settled" and o.window_close < cutoff]
        for slug in stale:
            self.observations.pop(slug, None)
        # _resolved_next only holds the one upcoming window; clear stale entries.
        for ts in [ts for ts, m in self._resolved_next.items() if ts < self.current_window]:
            self._resolved_next.pop(ts, None)

    async def _activate(self, meta: MarketMeta):
        # FIX #5 ENFORCEMENT: skip activating a window whose time-of-day slice has
        # consistently failed the slippage-stress test. This is the "discard the slice"
        # behaviour the design claims — a blocked slice is measured but never traded.
        if self._bucket_blocked(int(meta.window_start)):
            obs = self.observations.get(meta.market_id)
            if obs is not None:
                obs.outcome = "skipped_stress"
            log.info(
                f"WINDOW SKIPPED {meta.market_id} — time-of-day slice "
                f"{_tod_bucket(int(meta.window_start))} never survives slippage stress"
            )
            return
        obs = self.observations.get(meta.market_id)
        if obs is not None:
            obs.outcome = "trading"
        log.info(f"WINDOW ACTIVE {meta.market_id} close={meta.window_close}")
        await self._on_window_ready(meta)

    def _update_active_min(self):
        if self.current_window is None:
            return
        slug = window_slug(self.s.window_asset, self.s.window_timeframe, self.current_window)
        obs = self.observations.get(slug)
        if obs is None or obs.outcome != "trading":
            return
        st = self._state_lookup(slug)
        if st is None:
            return
        c = st.combined_ask
        if c is None:
            return
        if obs.combined_at_open is None:
            obs.combined_at_open = c
        if obs.min_combined is None or c < obs.min_combined:
            obs.min_combined = c
            obs.min_combined_at = time.time()
        if c < 1.0 - self.s.min_edge:
            obs.edge_seen = True

    def _stress_test(self, obs: WindowObservation):
        """FIX #5: does the slice survive an added slippage of +MIN_EDGE at its best?

        We model the worst realistic fill as min_combined + slippage_stress. If that is
        still < 1.0, the slice survives; otherwise we DISCARD it (don't force it live).
        """
        if obs.min_combined is None:
            obs.survives_slippage = False
        else:
            # FIX #5: the stress must be a GENUINE buffer ON TOP OF the entry edge, or it
            # degenerates to the edge condition and never fails. A window that only ever
            # showed razor-thin edge (min_combined just under 1 - MIN_EDGE) must FAIL the
            # stress. Survive iff the best combined clears the entry edge PLUS the extra
            # slippage buffer: min_combined < 1 - MIN_EDGE - SLIPPAGE_STRESS_BUFFER.
            survive_threshold = 1.0 - self.s.min_edge - self.s.slippage_stress_buffer
            obs.survives_slippage = obs.min_combined < survive_threshold
        # FIX #5 ENFORCEMENT: fold the verdict into this time-of-day slice's running
        # tally. A slice whose windows never survive the stress is skipped going forward
        # (see _bucket_blocked) — this is the "discard slices that don't survive" claim,
        # made real rather than log-only. Windows with no edge_seen aren't counted as a
        # failure of the slice (nothing to stress), only those that showed an edge.
        if obs.edge_seen:
            self._bucket_seen[obs.tod_bucket] = self._bucket_seen.get(obs.tod_bucket, 0) + 1
            if not obs.survives_slippage:
                self._bucket_fail[obs.tod_bucket] = self._bucket_fail.get(obs.tod_bucket, 0) + 1

    # FIX #5: minimum observations before a slice may be blocked (avoid over-fitting to
    # one bad window) and the failure ratio above which the slice is discarded.
    MIN_STRESS_SAMPLES = 3
    STRESS_FAIL_RATIO = 1.0  # only block a slice that has NEVER survived the stress

    def _bucket_blocked(self, window_ts: int) -> bool:
        """FIX #5 ENFORCEMENT: True if this window's time-of-day slice has enough
        edge-showing samples and every one of them failed the slippage stress. Fail-open
        (returns False) until MIN_STRESS_SAMPLES accumulate, so we never suppress a slice
        we haven't actually measured."""
        bucket = _tod_bucket(window_ts)
        seen = self._bucket_seen.get(bucket, 0)
        if seen < self.MIN_STRESS_SAMPLES:
            return False
        fails = self._bucket_fail.get(bucket, 0)
        return (fails / seen) >= self.STRESS_FAIL_RATIO

    def stop(self):
        self._stop = True
        if self._task is not None:
            self._task.cancel()
