"""Risk controls — checked in the execution path, before any order goes out.

HARD FAIL-SAFE stance: if a check fails (balance, stale, liquidity, daily cap,
emergency file), we ABORT both legs. One-sided fills are the worst outcome in an arb,
so we never fire a leg when the other is in doubt.

All checks are pure math over cached numbers — no network, no parsing. Keeps the
hot path cheap. Balance is read from a preloaded value refreshed in the background.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

from .config import SETTINGS
from .logger import log
from .models import MarketState
from .alerting import (WebhookAlerter, ALERT_KILL_SWITCH, ALERT_DAILY_KILL_SWITCH,
                       ALERT_MACRO_STATE_FAILURE)


# ---- TASK A: macro-risk integration ---------------------------------------
# The HIGH-impact window logic (the 2-hour window around each HIGH macro event, and
# the "is this timestamp inside any window" check) lives in
# scrapers/page_agent_runner.py. We IMPORT and REUSE it directly rather than
# reimplementing the window math here.
#
# `scrapers` is a top-level namespace package at the project root, but its module
# uses a relative `from ..config` import that only resolves when loaded under the
# `polymarket_arb.scrapers` package. We therefore load it via importlib with a
# synthetic parent package so `..config`/`.logger` resolve to the real modules —
# this is a one-time, import-safe shim (no source mutation of the scraper). The
# underlying data file is state/macro_events.json, also owned by the scraper.

def _load_page_agent_module():
    """Import scrapers/page_agent_runner.py under a synthetic parent package so its
    relative imports resolve. Returns the loaded module (cached in sys.modules). Raises
    ImportError if the file is absent, so callers can fail closed."""
    import importlib
    import importlib.util
    import sys
    import types

    key = "polymarket_arb.scrapers.page_agent_runner"
    if key in sys.modules:
        return sys.modules[key]

    here = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(here)  # polymarket_arb/ -> project root
    mod_path = os.path.join(project_root, "scrapers", "page_agent_runner.py")
    if not os.path.exists(mod_path):
        raise ImportError(f"page_agent_runner not found at {mod_path}")

    # Synthetic parent package so `from ..config` resolves to polymarket_arb.config.
    parent_key = "polymarket_arb.scrapers"
    if parent_key not in sys.modules:
        parent = types.ModuleType(parent_key)
        parent.__path__ = [os.path.join(project_root, "polymarket_arb", "scrapers")]
        parent.__package__ = "polymarket_arb"
        sys.modules[parent_key] = parent

    spec = importlib.util.spec_from_file_location(key, mod_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[key] = module
    spec.loader.exec_module(module)
    return module


# Lazy-loaded once at first use; kept module-level so every RiskState shares one reader.
# `None` means "not yet resolved" (import failed or not attempted).
_PAGE_AGENT = None
_PAGE_AGENT_IMPORT_OK = False
_PAGE_AGENT_IMPORT_ERROR: Optional[str] = None


def _maybe_page_agent():
    """Return ``(module, ok)`` without raising into the trading hot path.

    Import failure is retained as macro-state health information. ``RiskState``
    turns it into a hard precheck failure, a critical log, and a durable alert.
    """
    global _PAGE_AGENT, _PAGE_AGENT_IMPORT_OK, _PAGE_AGENT_IMPORT_ERROR
    if _PAGE_AGENT is not None:
        return _PAGE_AGENT, _PAGE_AGENT_IMPORT_OK
    try:
        _PAGE_AGENT = _load_page_agent_module()
        _PAGE_AGENT_IMPORT_OK = True
        _PAGE_AGENT_IMPORT_ERROR = None
    except Exception as e:  # ImportError / SyntaxError / etc. — fail closed in precheck
        _PAGE_AGENT = None
        _PAGE_AGENT_IMPORT_OK = False
        _PAGE_AGENT_IMPORT_ERROR = repr(e)
    return _PAGE_AGENT, _PAGE_AGENT_IMPORT_OK


def _read_macro_state() -> Optional[object]:
    """Load state/macro_events.json into a MacroEventState (reusing the scraper's
    dataclass).

    The caller classifies every failure. This helper deliberately raises on a
    missing, unreadable, or structurally invalid payload so those conditions can
    never be confused with a valid empty calendar.
    """
    module, ok = _maybe_page_agent()
    if not ok or module is None:
        raise RuntimeError(
            f"page_agent_runner unavailable: {_PAGE_AGENT_IMPORT_ERROR or 'unknown import error'}"
        )
    path = module.MACRO_EVENTS_PATH
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("macro state root must be a JSON object")
    if not isinstance(data.get("events"), list):
        raise ValueError("macro state events must be a list")
    if not isinstance(data.get("high_risk_windows"), list):
        raise ValueError("macro state high_risk_windows must be a list")
    return module.MacroEventState.from_dict(data)


# Cached macro state + the mtime/epoch of the last load. Refreshed at
# SETTINGS.cache_refresh_interval_s cadence (reusing the existing cache-refresh knob
# so macro data ages at the same rate as market_cache.json).
_macro_cache: Optional[object] = None
_macro_cache_epoch: float = 0.0
_macro_state_error: Optional[str] = None
_macro_state_error_code: Optional[str] = None


def _load_macro_events(force: bool = False) -> Optional[object]:
    """Return cached MacroEventState, refreshing from disk on the cache cadence.

    Load failures become explicit health codes for ``RiskState``. They never
    escape into the hot path and never represent a healthy empty state.
    """
    global _macro_cache, _macro_cache_epoch, _macro_state_error, _macro_state_error_code
    now = time.time()
    if not force and _macro_cache is not None and (now - _macro_cache_epoch) < SETTINGS.cache_refresh_interval_s:
        _macro_state_error = None
        _macro_state_error_code = None
        return _macro_cache

    module, ok = _maybe_page_agent()
    if not ok or module is None:
        _macro_cache = None
        _macro_cache_epoch = now
        _macro_state_error_code = "importer_unavailable"
        _macro_state_error = (
            "page_agent_runner import failed: "
            f"{_PAGE_AGENT_IMPORT_ERROR or 'module unavailable'}"
        )
        return None

    path = str(getattr(module, "MACRO_EVENTS_PATH", ""))
    if not path or not os.path.exists(path):
        _macro_cache = None
        _macro_cache_epoch = now
        _macro_state_error_code = "missing"
        _macro_state_error = f"macro state file missing: {path or '<unset>'}"
        return None

    try:
        state = _read_macro_state()
        if state is None:
            raise ValueError("macro state reader returned no state")
    except Exception as e:
        _macro_cache = None
        _macro_cache_epoch = now
        _macro_state_error_code = "unreadable_invalid"
        _macro_state_error = f"macro state unreadable or invalid at {path}: {e!r}"
        return None

    _macro_state_error = None
    _macro_state_error_code = None
    _macro_cache_epoch = now
    _macro_cache = state
    return state


def _macro_state_health(
    now: float,
) -> Tuple[Optional[object], Optional[str], Optional[str], Dict[str, object]]:
    """Return state plus a stable failure code/detail evaluated at ``now``.

    In particular, ``last_update`` age is calculated on every invocation even
    while the parsed state object remains inside the normal cache interval.
    """
    # A previously parsed cache must not hide deletion of the source file for an
    # entire refresh interval.  The existence check is cheap and makes "missing"
    # fail closed on the very next precheck while still avoiding JSON parsing on
    # the hot path.
    module, importer_ok = _maybe_page_agent()
    if importer_ok and module is not None:
        path = str(getattr(module, "MACRO_EVENTS_PATH", ""))
        if not path or not os.path.exists(path):
            return (
                None,
                "missing",
                f"macro state file missing: {path or '<unset>'}",
                {},
            )

    state = _load_macro_events()
    if _macro_state_error_code is not None:
        return None, _macro_state_error_code, _macro_state_error, {}
    if state is None:
        return None, "unreadable_invalid", "macro state loader returned no state", {}

    events = getattr(state, "events", None)
    windows = getattr(state, "high_risk_windows", None)
    if not isinstance(events, list) or not isinstance(windows, list):
        return (
            None,
            "unreadable_invalid",
            "macro state must contain list-valued events and high_risk_windows",
            {},
        )
    for index, window in enumerate(windows):
        if not isinstance(window, dict):
            return None, "unreadable_invalid", f"macro window {index} is not an object", {}
        try:
            start = float(window["start"])
            end = float(window["end"])
        except (KeyError, TypeError, ValueError) as e:
            return None, "unreadable_invalid", f"macro window {index} is invalid: {e!r}", {}
        if not math.isfinite(start) or not math.isfinite(end) or end < start:
            return (
                None,
                "unreadable_invalid",
                f"macro window {index} has invalid bounds start={start!r} end={end!r}",
                {},
            )

    try:
        last_update = float(getattr(state, "last_update"))
    except (AttributeError, TypeError, ValueError) as e:
        return None, "invalid_last_update", f"macro state last_update is invalid: {e!r}", {}
    if not math.isfinite(last_update) or last_update <= 0.0:
        return (
            None,
            "invalid_last_update",
            f"macro state last_update must be finite and > 0, got {last_update!r}",
            {"last_update": last_update},
        )

    age_s = now - last_update
    fields: Dict[str, object] = {
        "last_update": last_update,
        "age_seconds": age_s,
        "max_age_seconds": SETTINGS.macro_state_max_age_s,
    }
    if age_s > SETTINGS.macro_state_max_age_s:
        return (
            None,
            "stale",
            f"macro state age {age_s:.6f}s exceeds {SETTINGS.macro_state_max_age_s:.6f}s",
            fields,
        )
    return state, None, None, fields


def _high_macro_risk(now: Optional[float] = None, state: Optional[object] = None) -> bool:
    """True iff `now` falls inside any HIGH-impact 2-hour window declared in
    state/macro_events.json. Reuses PageAgentRunner.is_high_risk_window directly."""
    if now is None:
        now = time.time()
    module, ok = _maybe_page_agent()
    if not ok or module is None:
        return False
    if state is None:
        state = _load_macro_events()
    if state is None:
        return False
    # Reuse the scraper's exact window-membership test on its MacroEventState.
    runner = module.PageAgentRunner.__new__(module.PageAgentRunner)
    runner._state = state
    return bool(runner.is_high_risk_window(now))


@dataclass()
class RiskState:
    """Per-tick risk snapshot consumed by the kill-switch and pre-fire risk gate.

    HARD FAIL-SAFE stance (risk.py:3): if any check fails (balance, staleness,
    liquidity, daily cap, partial-loss cap), the corresponding fire is blocked rather
    than submitted and re-evaluated next tick. Fields are preloaded by a background
    refresh task and treated as advisory truth — the bot never reads balance off the
    hot path to avoid WS-level races. ``__post_init__`` triggers the daily-kill check.
    """

    # preloaded from background task; updated out-of-loop
    balance: float = 0.0
    day_pnl: float = 0.0
    deployed_per_market: Dict[str, float] = field(default_factory=dict)
    total_deployed: float = 0.0
    halted: bool = False
    # HIGH: capital reserved by an in-flight fire that has passed precheck but not yet
    # committed/released (network round trip still pending). Concurrent fire tasks must
    # see each other's reservations or two fires can both pass the cap check and
    # over-deploy. `reserve()` mutates these atomically under `_lock`.
    pending_deployed: float = 0.0
    pending_per_market: Dict[str, float] = field(default_factory=dict)
    _lock: Optional[asyncio.Lock] = field(default=None, repr=False, compare=False)
    # Optional alerter for kill-switch / daily-kill-switch alerts (TASK E2).
    alerter: Optional[WebhookAlerter] = field(default=None, repr=False, compare=False)
    # Track whether the daily-kill-switch alert has already fired this session
    # (avoid alert storms while the cap stays breached).
    _daily_kill_alerted: bool = field(default=False, repr=False, compare=False)
    # Macro-state alerting is transition based because WebhookAlerter intentionally
    # persists every alert() call before its webhook-level time deduplication.
    _active_macro_failure: Optional[str] = field(default=None, repr=False, compare=False)
    _macro_fallback_alerter: Optional[WebhookAlerter] = field(
        default=None, repr=False, compare=False
    )

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

    # ---- TASK A: macro-risk tightening ----
    # `macro_edge_widen` and `macro_no_entry_extra_s` are applied ONLY while an active
    # HIGH-impact macro window is in force. They are configurable per RiskState
    # instance while state freshness uses SETTINGS.macro_state_max_age_s.
    #
    # Why gate these in precheck: precheck is the last HARD gate before orders leave the
    # box (HARD FAIL-SAFE stance). Macro/news risk is non-financial, so it belongs
    # with balance/liquidity/kill-switch — not in the soft arb gate in arb_engine.
    macro_edge_widen: float = 0.005      # widen MIN_EDGE by this much under macro risk
    macro_no_entry_extra_s: float = 0.0  # widen NO_ENTRY_SECONDS by this many under risk

    @property
    def HIGH_MACRO_RISK(self) -> bool:
        """True iff the current wall-clock now is inside a HIGH-impact 2-hour window
        declared in state/macro_events.json. Macro state is cached and refreshed on the
        cache_refresh_interval_s cadence by `_load_macro_events`."""
        return _high_macro_risk()

    def _macro_state_gate(self, now: float) -> Tuple[Optional[object], Optional[str]]:
        """Evaluate macro-state health and manage failure/recovery transitions."""
        state, code, detail, fields = _macro_state_health(now)
        if code is None:
            if self._active_macro_failure is not None:
                log.info(
                    f"macro-risk state recovered from {self._active_macro_failure}; "
                    "entry gate re-enabled"
                )
            self._active_macro_failure = None
            return state, None

        reason = f"macro state {code.replace('_', ' ')}"
        if code != self._active_macro_failure:
            self._active_macro_failure = code
            alerter = self.alerter
            if alerter is None:
                if self._macro_fallback_alerter is None:
                    self._macro_fallback_alerter = WebhookAlerter(
                        webhook_url=SETTINGS.alert_webhook_url
                    )
                alerter = self._macro_fallback_alerter
            alerter.alert(
                ALERT_MACRO_STATE_FAILURE,
                f"MACRO STATE FAIL-SAFE: {detail}; entries blocked",
                failure=code,
                error=detail,
                **fields,
            )
        return None, reason

    def _macro_no_entry_hit(self, st: MarketState, now: float) -> bool:
        """Stricter no-entry check applied under HIGH_MACRO_RISK: widen the no-entry
        zone by `macro_no_entry_extra_s` for windowed markets."""
        if not st.window_close:
            return False
        effective = SETTINGS.no_entry_seconds + self.macro_no_entry_extra_s
        return (st.window_close - now) <= effective

    def _macro_edge_reject(
        self, st: MarketState, snapshot_combined: Optional[float] = None
    ) -> Optional[str]:
        """Under HIGH_MACRO_RISK, require a wider effective MIN_EDGE. Reject when the
        snapshot combined-ask no longer clears `1.0 - (min_edge + macro_edge_widen)`.
        Returns a reason string if rejected, else None."""
        yes, no = st.best_yes_ask, st.best_no_ask
        if yes is None or no is None:
            # No book to evaluate the widened edge against — let the standard
            # liquidity/stale checks surface the real reason rather than silently
            # approving a macro-window trade on missing prices.
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
    ) -> Optional[str]:
        """Return None if OK, else a reason string. Called BEFORE submitting."""
        now = time.time()
        # Evaluate macro age and load health on every call, even if a different gate
        # (halt, stale book, capital) will ultimately provide the rejection reason.
        macro_state, macro_failure = self._macro_state_gate(now)
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
        # HIGH: include in-flight reservations so concurrent fires can't both pass. A
        # reservation is capital that a sibling fire has already been approved for but
        # not yet committed (its submit() is on the wire).
        eff_total = self.total_deployed + self.pending_deployed
        eff_market = (self.deployed_per_market.get(st.market_id, 0.0)
                      + self.pending_per_market.get(st.market_id, 0.0))
        if cost > self.balance:
            return f"insufficient balance {self.balance:.2f} < {cost:.2f}"
        if eff_total + cost > SETTINGS.max_total_capital:
            return "max capital exceeded"
        if eff_market + cost > SETTINGS.max_position_per_market:
            return "max position/market exceeded"
        if self.day_pnl - cost < -SETTINGS.daily_kill_switch_usd:
            # TASK E2: alert on daily kill-switch breach. The bot does NOT halt on
            # this (it's a per-trade rejection, not a full stop), but operators
            # must know the daily loss cap was hit. Deduplicate to avoid alerting
            # on every subsequent rejected trade while the cap stays breached.
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

        # TASK A: macro-risk tightening. Active only during a HIGH-impact window;
        # widens the effective MIN_EDGE (stricter) and the no-entry zone, so trades
        # that would normally sail through get rejected on news/spread-blowup risk.
        if _high_macro_risk(now, macro_state):
            r = self._macro_edge_reject(st, snapshot_combined)
            if r is not None:
                return r
            if self._macro_no_entry_hit(st, now):
                return (f"macro high-risk no-entry zone "
                        f"(window_close {st.window_close - now:+.1f}s)")
        return None

    async def reserve(
        self,
        st: MarketState,
        cost_yes: float,
        cost_no: float,
        snapshot_combined: Optional[float] = None,
    ) -> Optional[str]:
        """HIGH: atomic precheck + reservation. Serializes the check-and-reserve across
        concurrent fire tasks so two fires can never both pass the same cap and
        over-deploy. Returns None + reserves `cost_yes+cost_no` on success, else a
        reason string with nothing reserved. The caller MUST later call `commit()`
        (which converts the reservation to deployed) or `unreserve()` (on abort).
        """
        async with self._get_lock():
            reason = self.precheck(st, cost_yes, cost_no, snapshot_combined)
            if reason is not None:
                return reason
            cost = cost_yes + cost_no
            self.pending_deployed += cost
            self.pending_per_market[st.market_id] = (
                self.pending_per_market.get(st.market_id, 0.0) + cost)
            return None

    def unreserve(self, market_id: str, cost: float):
        """Release a reservation made by `reserve()` without deploying (fire aborted
        before commit, e.g. no fill)."""
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
        """M1: free deployed capital (e.g. after exiting a filled leg in a partial
        event) so total_deployed / per-market caps stay accurate."""
        self.total_deployed = max(0.0, self.total_deployed - cost)
        cur = self.deployed_per_market.get(market_id, 0.0)
        self.deployed_per_market[market_id] = max(0.0, cur - cost)
        if self.deployed_per_market.get(market_id, 0.0) == 0.0:
            self.deployed_per_market.pop(market_id, None)

    def record_fill_pnl(self, pnl_locked: float):
        self.day_pnl += pnl_locked
        # Reset the daily-kill-switch alert flag when we recover. Day_pnl is
        # cumulative for the session; if it swings back above the cap threshold
        # the alert should be re-armable for a future breach.
        if self._daily_kill_alerted and self.day_pnl >= -SETTINGS.daily_kill_switch_usd:
            self._daily_kill_alerted = False

    def halt(self, reason: str):
        """CRITICAL (FIX #2 kill switch): trip the halt flag so every subsequent
        precheck/reserve fails fast and no new trades go out. Previously this method
        was CALLED by execution._after_residual on a loss-cap breach but did not
        exist, so the breach raised AttributeError and was swallowed — the bot kept
        trading past its loss cap. Idempotent + logged.

        Fires ALERT_KILL_SWITCH if an alerter is attached (so any code path that
        calls halt() — not just execution._after_residual — produces an alert).
        """
        if not self.halted:
            self.halted = True
            log.critical(f"RISK HALT: {reason} — no further trades will be sent")
            # TASK E2: fire kill-switch alert from the source of truth (halt itself)
            # so any caller that trips the halt produces an alert, not just
            # execution._after_residual which also alerts (deduplicated by kind).
            if self.alerter is not None:
                self.alerter.alert(
                    ALERT_KILL_SWITCH,
                    f"Risk halt triggered: {reason}",
                    reason=reason,
                )
