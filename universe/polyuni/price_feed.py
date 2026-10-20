"""Price feed — HOT PATH transport glue.

Owns the real-time book source (WebSocket preferred, REST poll fallback) and writes
fresh best-ask values into the in-memory MarketState map. This module does ZERO
business logic beyond freshness tracking — arbitrage decisions live in arb_engine.

Design for latency:
  * one shared httpx session (connection reuse, no DNS per tick)
  * WS on_tick -> update scalar fields -> fire callback (no parsing beyond `_best_ask`)
  * REST polling only when WS is disabled or has died; REST fallback still reuses the
    same session and same minimal parse.
"""
from __future__ import annotations

import asyncio
import time
from typing import Callable, Dict, Optional

import httpx

from .alerting import WebhookAlerter, ALERT_WS_DISCONNECT_SEVERE
from .clients.clob_client import ClobBookFeed, fetch_book_top
from .config import SETTINGS
from .logger import log
from .models import MarketState

# WS-disconnect alerting thresholds (TASK E2):
# - At STALE_BOOK_TIMEOUT_S (default 5s): log a warning (early notice).
# - At 30s: fire ALERT_WS_DISCONNECT_SEVERE (actionable alert; the bot is on
#   REST polling only and may miss fast arbs).
WS_DISCONNECT_SEVERE_THRESHOLD_S = 30.0

# arb_engine consumes (state) on every meaningful update; it reads st.market_id itself
OnUpdate = Callable[[MarketState], None]
OnLegUpdate = Callable[[MarketState, str], None]


class PriceFeed:
    """Real-time order-book source — WS-primary with REST fallback per leg/market.

    Maintains the authoritative per-market ``MarketState`` snapshot used by both
    ``ArbEngine`` and ``Execution``. Subscribes over the CLOB WebSocket on the hot
    path; ``fetch_book`` REST is used only to seed before first WS frame and as a
    self-healing fallback on disconnect. Per-leg freshness timestamps (FIX #1) are
    updated here on every inbound quote so the engine gates on the OLDER leg age.
    Supports subscribing additional markets mid-session as market_universe.py
    promotes them to ACTIVE -- N concurrent markets, not one rotating slot.
    """

    def __init__(
        self,
        states: Dict[str, MarketState],
        on_update: OnUpdate,
        alerter: Optional[WebhookAlerter] = None,
        on_leg_update: Optional[OnLegUpdate] = None,
    ):
        self.states = states
        self.on_update = on_update
        # Sequential entry deliberately reacts to one side at a time. Keep this
        # raw-leg callback separate from ``on_update``, whose paired coalescing is
        # still required by the legacy simultaneous strategy.
        self.on_leg_update = on_leg_update
        # Optional alerter. When None or disabled, WS-disconnect alerting is a no-op.
        self.alerter = alerter
        self._session: Optional[httpx.AsyncClient] = None
        self._feed: Optional[ClobBookFeed] = None
        self._ws_task: Optional[asyncio.Task] = None
        self._stop = False
        self._ws_ok = False
        self._token_owner: Dict[str, MarketState] = {}
        self._poll_latency = 0.0
        # CLOB emits paired YES and NO books either in one frame or in adjacent frames
        # a few milliseconds apart. Evaluating the first callback combines that fresh
        # leg with the prior opposite leg and creates a millisecond-lived false edge.
        # Hold one evaluation per market for the measured settle interval so both books
        # are applied before ArbEngine sees the pair.
        self._pending_updates: Dict[str, MarketState] = {}
        self._pending_update_tokens: Dict[str, set[str]] = {}
        self._update_flush_handles: Dict[str, asyncio.Handle] = {}
        self._coalescing_logged_markets: set[str] = set()
        self._incomplete_logged_markets: set[str] = set()
        self.coherent_pair_evaluations = 0
        self.incomplete_pair_suppressions = 0
        # Track when the WS went unhealthy so we can alert if it's been
        # down longer than STALE_BOOK_TIMEOUT_S.
        self._ws_down_since: Optional[float] = None
        self._ws_severe_alerted = False

    async def start(self):
        # HOT PATH setup: one persistent session reused for the whole session.
        self._session = httpx.AsyncClient(base_url=SETTINGS.clob_base_url, timeout=3.0)
        token_ids = self._all_token_ids()
        # Always start the REST poll loop as a fallback. It idles while the WS is
        # healthy and takes over automatically the moment the socket drops — so the
        # bot never sits watching nothing (the bug we hit: WS 404 + no fallback).
        poll = asyncio.create_task(self._poll_loop(), name="poll-feed")
        poll.add_done_callback(self._log_task_error)
        # `states` can be empty at startup (universe discovery runs concurrently and
        # promotes markets to ACTIVE asynchronously) -- eagerly starting the WS here
        # would no-op and the socket would NEVER start, permanently stranding the bot
        # on 250ms REST polling. Only spin the WS now if we already have tokens;
        # otherwise add_market() spins it lazily the first time a market is promoted.
        # Either way the WS is guaranteed to come up.
        if SETTINGS.use_ws and token_ids:
            self._start_ws(token_ids)

    def _start_ws(self, token_ids):
        """Spin up (or leave running) the WS feed for `token_ids`. Idempotent."""
        if not SETTINGS.use_ws or self._stop or not token_ids:
            return
        # A ClobBookFeed gives up after its bounded connection retries. Retire that
        # completed instance before a later market rotation starts a replacement;
        # otherwise `_feed is not None` permanently strands the process on REST.
        if self._ws_task is not None and self._ws_task.done():
            if self._feed is not None:
                self._feed.stop()
            self._feed = None
            self._ws_task = None
        if self._feed is not None:
            return
        self._feed = ClobBookFeed(list(token_ids), self._on_ws_tick)
        self._ws_task = asyncio.create_task(self._feed.run(), name="ws-feed")
        self._ws_task.add_done_callback(self._log_task_error)

    @staticmethod
    def _log_task_error(task):
        # MEDIUM: surface exceptions in background tasks instead of silently losing
        # the feed / rotation when one raises.
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.error(f"background task failed: {exc!r}")

    def _all_token_ids(self):
        ids = []
        for st in self.states.values():
            ids.append(st.yes_token_id)
            ids.append(st.no_token_id)
        return ids

    def build_token_index(self):
        # COLD-ish: built once after states are populated (startup).
        self._token_owner = {}
        for st in self.states.values():
            self._token_owner[st.yes_token_id] = st
            self._token_owner[st.no_token_id] = st

    # ---- WS entry: update scalar fields, route to arb engine ----
    def _on_ws_tick(self, token_id: str, best_ask: Optional[float], size: Optional[float],
                    best_bid: Optional[float] = None):
        # HOT PATH: O(1) reverse lookup built at startup.
        st = self._token_owner.get(token_id)
        if st is None:
            return
        if token_id == st.yes_token_id:
            st.best_yes_ask, st.yes_size, st.best_yes_bid = best_ask, size, best_bid
        else:
            st.best_no_ask, st.no_size, st.best_no_bid = best_ask, size, best_bid
        st.last_update_ts = time.time()
        # Update per-leg freshness timestamps (FIX #1): the quote for THIS leg just
        # arrived, so stamp it. The older of the two legs gates the arb decision.
        if token_id == st.yes_token_id:
            st.yes_ts = time.time()
        else:
            st.no_ts = time.time()
        if self.on_leg_update is not None:
            try:
                self.on_leg_update(st, token_id)
            except Exception as exc:
                # A strategy callback must never tear down the market-data feed.
                log.error(
                    f"raw leg callback failed market={st.market_id} "
                    f"token={token_id[-8:]}: {exc!r}"
                )
        self._pending_updates[st.market_id] = st
        self._pending_update_tokens.setdefault(st.market_id, set()).add(token_id)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Preserve deterministic behavior for synchronous diagnostics/tests.
            self._pending_updates.pop(st.market_id, None)
            self._pending_update_tokens.pop(st.market_id, None)
            self.on_update(st)
            return
        tokens = self._pending_update_tokens[st.market_id]
        existing = self._update_flush_handles.pop(st.market_id, None)
        if existing is not None:
            existing.cancel()
        if len(tokens) >= 2:
            # Both legs have changed since the previous evaluation. Flush on the
            # next loop turn so any remaining books in this raw frame are applied.
            self._update_flush_handles[st.market_id] = loop.call_soon(
                self._flush_market_update, st.market_id
            )
        else:
            # If the paired leg never arrives, drop the incomplete evaluation after
            # the measured envelope. The scalar state remains available, but no arb
            # decision may combine it with an older opposite-leg quote.
            self._update_flush_handles[st.market_id] = loop.call_later(
                float(getattr(SETTINGS, "cross_leg_settle_ms", 10.0)) / 1000.0,
                self._flush_market_update,
                st.market_id,
            )

    def _flush_market_update(self, market_id: str) -> None:
        """Evaluate only a complete two-leg update set for one market."""
        self._update_flush_handles.pop(market_id, None)
        state = self._pending_updates.pop(market_id, None)
        tokens = self._pending_update_tokens.pop(market_id, set())
        if self._stop or state is None:
            return
        token_count = len(tokens)
        if token_count < 2:
            self.incomplete_pair_suppressions += 1
            if market_id not in self._incomplete_logged_markets:
                log.info(
                    f"CROSS_LEG_UPDATE_INCOMPLETE market={market_id} legs={token_count} "
                    f"settle_ms={getattr(SETTINGS, 'cross_leg_settle_ms', 10.0)} "
                    "evaluation_suppressed=True"
                )
                self._incomplete_logged_markets.add(market_id)
            return
        if market_id not in self._coalescing_logged_markets:
            log.info(
                f"CROSS_LEG_UPDATE_COALESCED market={market_id} "
                f"legs={token_count} combined={state.combined_ask}"
            )
            self._coalescing_logged_markets.add(market_id)
        self.coherent_pair_evaluations += 1
        self.on_update(state)  # arb_engine reads state.market_id itself

    async def add_market(self, st: MarketState):
        """Subscribe to a market mid-session (market_universe.py promoting it to ACTIVE).

        Registers the token->state reverse index, adds its token ids to the WS
        subscription (if live) and to the REST poll set. The hot path then routes the
        book updates exactly as for startup markets — no special-casing. Returns the
        MarketState so callers can wire the execution template in one step.

        Must be awaited: it awaits the WS resubscribe coroutine; otherwise the token
        index is never built and newly-promoted markets get zero book data.
        """
        self.states[st.market_id] = st
        self._token_owner[st.yes_token_id] = st
        self._token_owner[st.no_token_id] = st
        token_ids = self._all_token_ids()
        # If the WS was never started (empty states at startup, see start()), or its
        # bounded retry task has completed, start a fresh feed with every desired
        # token. REST polling remains active until it becomes healthy.
        if self._feed is None or (
            self._ws_task is not None and self._ws_task.done()
        ):
            self._start_ws(token_ids)
        else:
            try:
                # Always update ClobBookFeed's desired tokens, including while its
                # socket is unhealthy. The next reconnect must target this window.
                await self._feed.resubscribe(token_ids)
            except Exception as e:  # REST fallback owns transport until reconnect
                log.error(
                    f"resubscribe failed for new window market; "
                    f"REST polling active: {e!r}"
                )
                self._feed.healthy = False
                self._record_ws_unhealthy()
        return st

    async def remove_market(self, market_id: str) -> None:
        """Unsubscribe a market that market_universe.py has moved to CLOSING or
        demoted from ACTIVE under capacity pressure.

        Without this, a closed/demoted market's tokens stay in `_token_owner`
        forever: book ticks for it keep arriving and keep calling `on_update`
        (arb_engine.evaluate -> possibly firing) for a market the caller believes
        it stopped tracking the moment it was dropped from `states` -- `states`
        membership was never actually what gated tick routing, `_token_owner`
        was. Also stops the CLOB WS subscription and the REST poll set from
        including its tokens, same reasoning as add_market() for the reverse
        direction.
        """
        st = self.states.pop(market_id, None)
        if st is None:
            return
        self._token_owner.pop(st.yes_token_id, None)
        self._token_owner.pop(st.no_token_id, None)
        self._pending_updates.pop(market_id, None)
        self._pending_update_tokens.pop(market_id, None)
        handle = self._update_flush_handles.pop(market_id, None)
        if handle is not None:
            handle.cancel()
        if self._feed is not None and not self._stop:
            token_ids = self._all_token_ids()
            try:
                await self._feed.resubscribe(token_ids)
            except Exception as e:
                log.error(f"resubscribe failed while removing market {market_id}; "
                          f"REST polling active: {e!r}")
                self._feed.healthy = False
                self._record_ws_unhealthy()

    # ---- REST fallback: poll each token, write best ask ----
    async def _poll_loop(self):
        while not self._stop:
            # Recompute the token set each iteration: markets added via add_market()
            # must be picked up here too, or they get zero book data when the bot
            # falls back to polling (the classic REST-variant feed gap).
            ids = self._all_token_ids()
            if not ids:
                await asyncio.sleep(SETTINGS.poll_interval_s)
                continue
            # Idle while the WebSocket is healthy; only poll when WS is down.
            if self._feed is not None and self._feed.healthy:
                await asyncio.sleep(SETTINGS.poll_interval_s)
                # WS recovered — clear the down-since marker so we don't re-alert.
                if self._ws_down_since is not None:
                    down_for = time.monotonic() - self._ws_down_since
                    log.info(f"WebSocket recovered after {down_for:.1f}s; resuming WS feed")
                    self._ws_down_since = None
                    self._ws_severe_alerted = False
                continue
            # A deliberately disabled WS is REST-only by configuration, not an outage.
            if not SETTINGS.use_ws:
                self._ws_down_since = None
                self._ws_severe_alerted = False
            else:
                self._record_ws_unhealthy()
            t0 = time.time()
            # fire all GETs concurrently (shared session, pooled connections)
            results = await asyncio.gather(
                *(fetch_book_top(self._session, tid) for tid in ids), return_exceptions=True
            )
            for tid, res in zip(ids, results):
                if isinstance(res, Exception):
                    log.warning(f"REST poll fetch failed for {tid[-8:]}: {res!r}")
                    continue
                if res[0] is None:
                    log.warning(f"REST poll returned no book for {tid[-8:]}")
                    continue
                best, size, bid = res
                self._on_ws_tick(tid, best, size, bid)
            self._poll_latency = time.time() - t0
            await asyncio.sleep(SETTINGS.poll_interval_s)

    def _record_ws_unhealthy(self):
        """Record a configured WebSocket outage and emit its alert once."""
        # WS is unhealthy — track how long it's been down.
        # Alert at two severity levels:
        # - STALE_BOOK_TIMEOUT_S (default 5s): log a warning (early notice,
        #   deduped to avoid spam on every poll iteration).
        # - 30s (WS_DISCONNECT_SEVERE_THRESHOLD_S): fire a severe webhook alert
        #   so operators know the bot is operating on REST-only and may be
        #   missing fast arbs.
        down_for = time.monotonic() - self._ws_down_since if self._ws_down_since else 0.0
        if self._ws_down_since is None:
            self._ws_down_since = time.monotonic()
        elif down_for > SETTINGS.stale_book_timeout_s:
            log.warning(
                f"WebSocket disconnected for {down_for:.1f}s "
                f"(>{SETTINGS.stale_book_timeout_s}s threshold), running on REST polling"
            )
            if (
                down_for > WS_DISCONNECT_SEVERE_THRESHOLD_S
                and not self._ws_severe_alerted
                and self.alerter is not None
            ):
                self.alerter.alert(
                    ALERT_WS_DISCONNECT_SEVERE,
                    f"WebSocket disconnected for {down_for:.1f}s "
                    f"(>{WS_DISCONNECT_SEVERE_THRESHOLD_S}s threshold), bot is REST-only",
                    down_seconds=round(down_for, 1),
                    threshold_seconds=WS_DISCONNECT_SEVERE_THRESHOLD_S,
                    rest_only=True,
                )
                self._ws_severe_alerted = True

    async def aclose(self):
        self._stop = True
        for handle in self._update_flush_handles.values():
            handle.cancel()
        self._update_flush_handles.clear()
        self._pending_updates.clear()
        self._pending_update_tokens.clear()
        if self._feed:
            self._feed.stop()
        if self._ws_task is not None:
            self._ws_task.cancel()
        if self._session:
            await self._session.aclose()
