"""PnL-accounting invariant suite (TASK 1).

Property-style tests that pin the invariants any correct dual-leg PnL accounting
MUST satisfy. Paper-trade only; real numbers from synthetic fills driven through
the live Execution hot path (``execute_dual_buy`` + ``_check_fills``), mirrored into
``scripts/calc_pnl_convergence.compute_pnl_convergence`` so the two calculators get
identical inputs and we can diff.

Invariants pinned:
  (a) residual position == 0 after every completed two-leg fill (no naked exposure).
  (b) locked PnL == sum over filled (CLEAN) legs of (1.0 - yes_price - no_price)
      * effective_size, where effective_size = size * min(yes_ratio, no_ratio).
  (c) partial fills scale by min(yes_ratio, no_ratio) consistently on the entry,
      hedge, and exit paths.
  (d) expected-PnL and realized-PnL calculators given identical inputs produce
      identical outputs (engine ``Metrics.locked_pnl`` vs the calc script's
      ``real_locked_pnl``).

Any violation reported here is a LIVE BUG. Repros include file:line.
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import pytest

from polymarket_arb.clients.trading_client import TradingClient
from polymarket_arb.execution import Execution
from polymarket_arb.metrics import Metrics, classify_freshness_ms
from polymarket_arb.models import MarketState
from polymarket_arb.risk import RiskState

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from calc_pnl_convergence import compute_pnl_convergence  # noqa: E402


@pytest.fixture(autouse=True)
def _neutralize_macro_risk():
    """Install an EMPTY MacroEventState into risk.py's module cache so no HIGH
    window is ever active and the ``macro risk state unreadable`` block never
    fires. This mirrors the pattern in tests/test_macro_risk.py and makes every
    invariant test hermetic and order-independent."""
    import time as _time
    import polymarket_arb.risk as risk_mod

    MacroEventState = risk_mod._maybe_page_agent()[0].MacroEventState
    prev_cache = risk_mod._macro_cache
    prev_epoch = risk_mod._macro_cache_epoch
    risk_mod._macro_cache = MacroEventState(
        events=[], last_update=_time.time(), high_risk_windows=[],
    )
    risk_mod._macro_cache_epoch = _time.time() + 1_000_000  # far-future => hit
    yield
    risk_mod._macro_cache = prev_cache
    risk_mod._macro_cache_epoch = prev_epoch


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
def _market(market_id: str, yes_ask: float = 0.41, no_ask: float = 0.53) -> MarketState:
    now = datetime.now().timestamp()
    return MarketState(
        market_id=market_id,
        question="pnl-invariant",
        yes_token_id=f"{market_id}-YES",
        no_token_id=f"{market_id}-NO",
        best_yes_ask=yes_ask,
        best_no_ask=no_ask,
        yes_size=1_000.0,
        no_size=1_000.0,
        last_update_ts=now,
        yes_ts=now,
        no_ts=now,
    )


class _FillPlanClient(TradingClient):
    """Deterministic client: each fire's leg fills per a per-call plan.

    ``yes_fills``/``no_fills`` are queues of (is_filled, ratio) tuples. The
    probe result is STICKY: once the queue reports ``is_filled=True`` the leg
    keeps reporting filled on every subsequent ``is_filled`` probe (so multiple
    _check_fills ticks don't un-fill it). The probe pops the queue only until
    the first True; the ratio that was recorded at submit time (via
    ``get_fill_ratio``) is returned consistently for the same order id.
    """

    def __init__(
        self,
        *,
        yes_fills: list[tuple[bool, float]] | None = None,
        no_fills: list[tuple[bool, float]] | None = None,
        yes_exit_fills: list[bool] | None = None,
        no_exit_fills: list[bool] | None = None,
        submit_yes_ratio: float = 1.0,
        submit_no_ratio: float = 1.0,
        hedge_yes_ratio: float = 1.0,
        hedge_no_ratio: float = 1.0,
        reject_no_submit: bool = False,
    ):
        self.submits: list[tuple] = []
        self.cancels: list[tuple] = []
        self._yes_probe = list(yes_fills or [])
        self._no_probe = list(no_fills or [])
        self._yes_exit_probe = list(yes_exit_fills or [True])
        self._no_exit_probe = list(no_exit_fills or [True])
        self._submit_yes_ratio = submit_yes_ratio
        self._submit_no_ratio = submit_no_ratio
        self._hedge_yes_ratio = hedge_yes_ratio
        self._hedge_no_ratio = hedge_no_ratio
        self._reject_no_submit = reject_no_submit
        # Sticky fill status keyed by order id; set on first True probe.
        self._filled_status: dict[str, bool] = {}
        # ratio recorded at submit-time; engine reads get_fill_ratio once on submit
        # and again on the resolve branch, so storing per-order keeps them aligned.
        self._submit_ratios: dict[str, float] = {}

    async def submit_order(self, token_id, price, size, side="BUY"):
        if self._reject_no_submit and token_id.endswith("-NO"):
            return None
        oid = f"{side}-{token_id}-{len(self.submits)}"
        prior_buys = [s for s in self.submits
                      if s[0] == token_id and s[3] == "BUY"]
        is_hedge = bool(prior_buys) and side == "BUY"
        if is_hedge:
            ratio = (self._hedge_yes_ratio if token_id.endswith("-YES")
                     else self._hedge_no_ratio)
        else:
            ratio = (self._submit_yes_ratio if token_id.endswith("-YES")
                     else self._submit_no_ratio)
        self.submits.append((token_id, price, size, side, oid))
        self._submit_ratios[oid] = ratio
        return oid

    async def is_filled(self, order_id, token_id, gap_ms=0):
        if order_id is None:
            return False
        if self._filled_status.get(order_id):
            return True
        is_exit = order_id.startswith("SELL-")
        if is_exit:
            plan = self._yes_exit_probe if token_id.endswith("-YES") else self._no_exit_probe
        else:
            plan = self._yes_probe if token_id.endswith("-YES") else self._no_probe
        if not plan:
            return False
        item = plan.pop(0)
        filled = bool(item[0]) if isinstance(item, tuple) else bool(item)
        if filled and not is_exit:
            self._filled_status[order_id] = True
        return filled

    async def cancel_order(self, token_id, missing_order_id=None):
        self.cancels.append((token_id, missing_order_id))
        return True

    def get_fill_ratio(self, order_id):
        if order_id is None:
            return 0.0
        return self._submit_ratios.get(order_id, 1.0)



def _fire_and_resolve(
    execution: Execution,
    st: MarketState,
    *,
    combined: float,
    yes_p: float,
    no_p: float,
    age_ms: float = 0.0,
    force_gap: bool = False,
) -> list[dict]:
    """Fire one dual-buy at the given snapshot prices, then resolve it by
    driving ``_check_fills`` once. The fire size comes from the registered
    template (``Execution.register(st, size=...)``); do not pass it here.

    ``force_gap``: when True, surrender the gap window before the first
    ``_check_fills`` probe so partial/neither-filled timeout branches run.
    Returns the captured TRADE events (verbatim payload to feed the calc)."""
    import time as _time
    events: list[dict] = []
    from polymarket_arb import logger as _logger

    captured: list[dict] = []
    original_trade = _logger.log.trade

    def capture(msg, data=None, **kw):
        captured.append({"event": "TRADE", "msg": msg, "data": data or {}})
        return original_trade(msg, data, **kw)

    async def run():
        _logger.log.trade = capture
        try:
            await execution.execute_dual_buy(
                st, combined, 0.0, age_ms=age_ms,
                yes_p=yes_p, no_p=no_p,
            )
            if force_gap and execution._pending:
                for entry in execution._pending.values():
                    entry["first_sent"] = _time.monotonic() - 10.0
            await execution._check_fills()
            # Partial branches may need a second probe to complete the hedge/exit
            # confirmation; let the deferred path walk it once more.
            if force_gap and execution._pending:
                for entry in execution._pending.values():
                    entry["first_sent"] = _time.monotonic() - 10.0
                await execution._check_fills()
        finally:
            _logger.log.trade = original_trade

    asyncio.run(run())
    events.extend(captured)
    return events


def _fill_events_to_convergence_inputs(events: list[dict]) -> tuple[list, list]:
    """Translate engine TRADE events into the dict shapes compute_pnl_convergence
    consumes:``filled`` -> fills (combined + pnl_locked), ``missed`` -> missed."""
    fills, missed = [], []
    for ev in events:
        msg = ev.get("msg")
        data = ev.get("data", {})
        if msg == "filled":
            fills.append({
                "combined": float(data.get("combined", 0.0)),
                "pnl_locked": float(data.get("pnl_locked", 0.0)),
                "age_ms": float(data.get("age_ms", 0.0)),
                "freshness_band": data.get("freshness_band", "fresh"),
            })
        elif msg == "missed":
            missed.append({
                "combined": float(data.get("combined", 0.0)),
                "would_lock": float(data.get("would_lock", 0.0)),
                "reason": data.get("reason", "unknown"),
                "age_ms": float(data.get("age_ms", 0.0)),
                "freshness_band": data.get("freshness_band", "fresh"),
            })
        elif msg == "partial_resolved":
            # Hedge completion emits the same payload shape; convergence script
            # parses only filled/missed msg values, so surface partial_resolved
            # as a fill with its pnl_locked so the diff is apples-to-apples.
            fills.append({
                "combined": float(data.get("resolved_combined",
                                           data.get("combined", 0.0))),
                "pnl_locked": float(data.get("pnl_locked", 0.0)),
                "age_ms": float(data.get("age_ms", 0.0)),
                "freshness_band": data.get("freshness_band", "fresh"),
            })
    return fills, missed


# ---------------------------------------------------------------------------
# Invariant (a): residual position == 0 after every completed two-leg fill
# ---------------------------------------------------------------------------
def _run_clean_fill(
    yes_ratio: float = 1.0,
    no_ratio: float = 1.0,
    size: float = 10.0,
    yes_p: float = 0.41,
    no_p: float = 0.53,
):
    risk = RiskState(balance=1_000.0)
    metrics = Metrics()
    client = _FillPlanClient(
        submit_yes_ratio=yes_ratio,
        submit_no_ratio=no_ratio,
        yes_fills=[(True, yes_ratio)],
        no_fills=[(True, no_ratio)],
    )
    execution = Execution(client, risk, metrics)
    st = _market("clean-fill-market", yes_ask=no_p, no_ask=yes_p)
    execution.register(st, size=size)
    events = _fire_and_resolve(
        execution, st, combined=yes_p + no_p, yes_p=yes_p, no_p=no_p,
    )
    return execution, risk, metrics, client, events


def test_invariant_a_residual_position_zero_after_clean_two_leg_fill():
    """(a) After a completed two-leg fill the engine must hold no directional
    exposure: no pending entry, deployed capital equals the committed cost, and
    after settlement (release_window) deployed returns to zero — no naked
    exposure remains."""
    execution, risk, metrics, client, events = _run_clean_fill(
        yes_ratio=1.0, no_ratio=1.0, size=10.0,
    )
    msg = [e["msg"] for e in events]
    reasons = [e["data"].get("reason") for e in events]
    assert "filled" in msg, f"expected a filled event, got msgs={msg} reasons={reasons}"
    assert metrics.fills == 1
    # No pending entry: position is locked, not naked.
    assert execution._pending == {}
    # Deployed capital at fill time == committed cost of the two legs.
    committed = 0.41 * 10.0 + 0.53 * 10.0  # 9.40
    assert risk.total_deployed == pytest.approx(committed, abs=1e-9)
    assert risk.pending_deployed == pytest.approx(0.0)
    # Settlement (window rotation) releases the pair reservation.
    execution.release_window("clean-fill-market")
    assert risk.total_deployed == pytest.approx(0.0)
    assert risk.pending_deployed == pytest.approx(0.0)
    assert "clean-fill-market" not in risk.deployed_per_market


def test_invariant_a_residual_position_zero_after_partial_hedge_no_quote_exit():
    """(a) After a partial where the missing leg has no quote and we exit the
    filled leg, the engine must hold no directional exposure once the exit fills.
    The exit flattens the position; risk releases the deployed cost."""
    risk = RiskState(balance=1_000.0)
    metrics = Metrics()
    # YES fills at gate (ratio 0.5); NO never fills; the missing ask is None so
    # _manage_partial exits the YES leg at the best NO bid -> proceeds flatten.
    client = _FillPlanClient(
        submit_yes_ratio=0.5,
        submit_no_ratio=0.0,
        yes_fills=[(True, 0.5)],
        no_fills=[(False, 0.0)],
    )
    execution = Execution(client, risk, metrics)
    # YES @ 0.40, NO @ 0.50 -> entering with combined 0.90 (edge 0.10).
    # Live book after fire: yes bid 0.55 (sell the YES back), no ask None -> exit.
    st = _market("partial-exit-market", yes_ask=0.50, no_ask=0.50)
    # Override bids/asks post-fire by mutating after construction so _manage_partial
    # sees best_no_ask=None (forces exit path) and best_yes_bid for the exit proceeds.
    st.best_no_ask = None  # no live quote on the missing NO leg -> exit the YES
    st.best_yes_bid = 0.55
    st.best_no_bid = 0.55
    execution.register(st, size=10.0)
    events = _fire_and_resolve(
        execution, st, combined=0.90, yes_p=0.40, no_p=0.50, force_gap=True,
    )
    assert "partial_resolved" in [e["msg"] for e in events]
    # The exit path must have released the original reservation.
    assert risk.halted is False, "exit path must not halt when exit fills"
    assert execution._pending == {}
    # Effective size was 5.0 (YES @ 50%). Reservation released on resolution.
    assert "partial-exit-market" not in risk.deployed_per_market


# ---------------------------------------------------------------------------
# Invariant (b): locked PnL == sum over filled (clean) legs of
#   (1.0 - yes_price - no_price) * effective_size,  effective_size = size*min(...)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("yes_ratio", "no_ratio", "size", "yes_p", "no_p", "expected_locked"),
    [
        # Full fill: edge 0.10 * size 10 = 1.0
        (1.0, 1.0, 10.0, 0.41, 0.53, 0.6),    # 1 - 0.94 = 0.06 * 10 = 0.6
        (1.0, 1.0, 10.0, 0.40, 0.50, 1.0),    # 1 - 0.90 = 0.10 * 10 = 1.0
        # Partial min(yes_ratio, no_ratio) scaling: yes=0.5 -> min=0.5 -> 0.10*5 = 0.5
        (0.5, 1.0, 10.0, 0.40, 0.50, 0.5),
        (1.0, 0.5, 10.0, 0.40, 0.50, 0.5),
        (0.5, 0.5, 10.0, 0.40, 0.50, 0.5),
        # Asymmetric partials lock only the overlap min=0.25
        (0.25, 0.75, 10.0, 0.40, 0.50, 0.25),
        # Different sizes
        (1.0, 1.0, 100.0, 0.45, 0.50, 5.0),  # 0.05 * 100
    ],
)
def test_invariant_b_locked_pnl_formula(
    yes_ratio, no_ratio, size, yes_p, no_p, expected_locked,
):
    """(b) locked_pnl after a clean two-leg fill equals (1 - yes_p - no_p)
    * size * min(yes_ratio, no_ratio). Pins execution.py:608-610."""
    execution, risk, metrics, client, events = _run_clean_fill(
        yes_ratio=yes_ratio, no_ratio=no_ratio,
        size=size, yes_p=yes_p, no_p=no_p,
    )
    msgs = [e["msg"] for e in events]
    assert msgs == ["filled"], f"unexpected event sequence: {msgs}"
    assert metrics.locked_pnl == pytest.approx(expected_locked, abs=1e-9), (
        f"locked_pnl={metrics.locked_pnl} expected {expected_locked} "
        f"(yes_p={yes_p}, no_p={no_p}, size={size}, "
        f"yes_ratio={yes_ratio}, no_ratio={no_ratio})"
    )
    # session_pnl and realized_pnl must both mirror locked_pnl for a clean fill
    # (no residual).
    assert metrics.session_pnl == pytest.approx(metrics.locked_pnl, abs=1e-9)
    assert metrics.realized_pnl == pytest.approx(metrics.locked_pnl, abs=1e-9)
    assert metrics.residual_pnl == pytest.approx(0.0, abs=1e-9)


# ---------------------------------------------------------------------------
# Invariant (c): partial fills scale by min(yes_ratio, no_ratio) consistently
# across entry, hedge, exit paths.
# ---------------------------------------------------------------------------
def test_invariant_c_entry_path_effective_size_uses_min_ratio():
    """(c-1) Entry (clean-arb) effective_size MUST be size * min(yes_ratio, no_ratio).
    Locked PnL on a 40/70 split must be 1% of the min side, not the YES side."""
    # yes_ratio=0.4, no_ratio=0.7 -> min=0.4 -> edge 0.10 * 4 = 0.4
    execution, risk, metrics, client, events = _run_clean_fill(
        yes_ratio=0.4, no_ratio=0.7, size=10.0, yes_p=0.40, no_p=0.50,
    )
    assert metrics.locked_pnl == pytest.approx(0.40, abs=1e-9), (
        f"expected min-ratio scaling (0.4*10*0.10=0.40) got {metrics.locked_pnl}"
    )


def test_invariant_c_hedge_path_effective_size_uses_filled_ratio_after_full_hedge():
    """(c-2) Hedge path locked PnL scales by the FILLED leg ratio only when the
    hedge fills completely (hedge_ratio=1.0).
    Engine execution.py:728 sets effective_size = size * filled_ratio, and the
    hedge only completes the pair on the qty the filled leg actually holds, so
    locked PnL = (1 - resolved_combined) * effective_size where effective_size is
    size * filled_ratio (= min(filled_ratio, hedge_ratio) since hedge==1)."""
    risk = RiskState(balance=1_000.0)
    metrics = Metrics()
    # YES entry fills at 60% (ratio 0.6); NO entry never fills. Hedge path is
    # reached when MAX_LEG_GAP_MS elapses with YES filled, NO missing. We force
    # the gap-elapsed branch by submitting then driving _resolve_one directly.
    client = _FillPlanClient(
        submit_yes_ratio=0.6,
        submit_no_ratio=0.0,
        # NO never fills on probe -> _manage_partial fires after gap window.
        yes_fills=[(True, 0.6)],
        no_fills=[(False, 0.0)],
    )
    execution = Execution(client, risk, metrics)
    # Entry: YES @ 0.40, NO @ 0.50 -> combined 0.90 (edge 0.10), size 10.
    # After fire, the live book moves: best_yes_ask=0.40, best_no_ask=0.45 -> the
    # hedge BUY of NO @ 0.45 with YES @ 0.40 gives resolved_combined=0.85, edge 0.15.
    st = _market("hedge-market", yes_ask=0.40, no_ask=0.45)
    st.best_yes_bid = 0.40
    st.best_no_bid = 0.45
    execution.register(st, size=10.0)
    events = _fire_and_resolve(
        execution, st, combined=0.90, yes_p=0.40, no_p=0.50, force_gap=True,
    )

    # Either path leads to a partial_resolved event. The hedge must have submitted
    # NO at the live ask for effective_size = 10 * 0.6 = 6.0.
    hedge_submits = [s for s in client.submits if s[0].endswith("-NO")
                     and s[3] == "BUY"]
    assert hedge_submits, f"expected a NO hedge submit, got subs={client.submits}"
    # The first YES submit is the entry BUY; the NO hedge BUY (size 6.0) follows.
    # Identify the hedge by side=BUY on NO token after the entry NO submit (if any).
    no_submits = [s for s in client.submits if s[0].endswith("-NO")]
    hedge_size = no_submits[-1][2]  # last NO submit is the hedge
    assert hedge_size == pytest.approx(6.0, abs=1e-9), (
        f"hedge qty must be size * filled_ratio = 10*0.6 = 6.0, got {hedge_size}"
    )


def test_invariant_c_exit_path_effective_size_matches_filled_ratio_only():
    """(c-3) Exit path sells the FILLED leg at the effective size = size *
    filled_ratio. A 50%-filled YES leg exits at half the requested quantity."""
    risk = RiskState(balance=1_000.0)
    metrics = Metrics()
    client = _FillPlanClient(
        submit_yes_ratio=0.5,
        submit_no_ratio=0.0,
        yes_fills=[(True, 0.5)],
        no_fills=[(False, 0.0)],
    )
    execution = Execution(client, risk, metrics)
    st = _market("exit-scale-market", yes_ask=0.50, no_ask=0.50)
    st.best_no_ask = None  # missing quote -> exit
    st.best_yes_bid = 0.55
    st.best_no_bid = 0.55
    execution.register(st, size=10.0)
    events = _fire_and_resolve(
        execution, st, combined=0.90, yes_p=0.40, no_p=0.50, force_gap=True,
    )
    # The exit SELL of the YES leg must be at effective_size = 5.0
    sell_yes = [s for s in client.submits if s[0].endswith("-YES") and s[3] == "SELL"]
    assert sell_yes, f"expected an exit SELL on YES, got subs={client.submits}"
    assert sell_yes[-1][2] == pytest.approx(5.0, abs=1e-9), (
        f"exit qty must be size * filled_ratio = 10*0.5 = 5.0, got {sell_yes[-1][2]}"
    )


# ---------------------------------------------------------------------------
# Invariant (d): expected-PnL and realized-PnL calculators on identical inputs
# ---------------------------------------------------------------------------
def test_invariant_d_engine_locked_pnl_equals_convergence_real_locked():
    """(d) Given identical clean fills, engine Metrics.locked_pnl must equal
    compute_pnl_convergence(... )['real_locked_pnl'].

    Both calculators run on the same synthetic fill set. The engine accounts
    locked_pnl from the live fire path; the convergence script sums the per-fill
    pnl_locked field. For identical inputs they must agree to the cent."""
    cases = [
        # (yes_ratio, no_ratio, size, yes_p, no_p)
        (1.0, 1.0, 10.0, 0.40, 0.50),
        (0.5, 1.0, 10.0, 0.40, 0.50),
        (1.0, 0.5, 10.0, 0.40, 0.50),
        (0.5, 0.5, 10.0, 0.40, 0.50),
        (1.0, 1.0, 25.0, 0.45, 0.50),
        (1.0, 1.0, 10.0, 0.41, 0.53),
    ]
    for yes_ratio, no_ratio, size, yes_p, no_p in cases:
        execution, risk, metrics, client, events = _run_clean_fill(
            yes_ratio=yes_ratio, no_ratio=no_ratio, size=size,
            yes_p=yes_p, no_p=no_p,
        )
        expected_locked_engine = metrics.locked_pnl
        fills, missed = _fill_events_to_convergence_inputs(events)
        conv = compute_pnl_convergence(fills, missed, order_size=size)
        # compute_pnl_convergence sums pnl_locked (already per-fill), so its
        # real_locked_pnl must match the engine's running sum (after rounding).
        assert conv["real_locked_pnl"] == pytest.approx(
            expected_locked_engine, abs=1e-4,
        ), (
            f"MISMATCH for yes_p={yes_p}, no_p={no_p}, size={size}, "
            f"yes_ratio={yes_ratio}, no_ratio={no_ratio}: "
            f"engine.locked_pnl={expected_locked_engine} vs "
            f"convergence.real_locked_pnl={conv['real_locked_pnl']}"
        )


def test_invariant_d_stale_quote_abort_does_not_inflate_realized():
    """(d-corner) A stale-quote abort (neither leg fills, gap window lapses)
    must NOT inflate engine locked_pnl nor convergence real_locked_pnl. Both
    calculators must report zero realized on a no-fill miss."""
    risk = RiskState(balance=1_000.0)
    metrics = Metrics()
    client = _FillPlanClient(
        submit_yes_ratio=1.0,  # commit at full ratio (optimistic) but releases later
        submit_no_ratio=1.0,
        yes_fills=[(False, 0.0)],
        no_fills=[(False, 0.0)],
        reject_no_submit=False,
    )
    execution = Execution(client, risk, metrics)
    st = _market("stale-abort-market", yes_ask=0.41, no_ask=0.53)
    execution.register(st, size=10.0)
    events = _fire_and_resolve(
        execution, st, combined=0.94, yes_p=0.41, no_p=0.53, force_gap=True,
    )

    assert metrics.locked_pnl == pytest.approx(0.0, abs=1e-9), (
        f"clean-abort must not lock any PnL, got locked_pnl={metrics.locked_pnl}"
    )
    assert metrics.missed >= 1
    fills, missed = _fill_events_to_convergence_inputs(events)
    conv = compute_pnl_convergence(fills, missed, order_size=10.0)
    assert conv["real_locked_pnl"] == pytest.approx(0.0, abs=1e-4)


def test_invariant_d_convergence_real_locked_equals_engine_realized_when_partials_present():
    """(d-gap-class) The −28% expected-vs-realized gap found in the
    task_2_4 paper session must NOT be hidden by the accounting bucketing.

    Both calculators run on the same sequence (one clean fill + one partial
    hedged fill whose pnl flows through residual_pnl, NOT locked_pnl). The
    convergence script sums ``pnl_locked`` over ``filled`` AND
    ``partial_resolved`` events (we translate partial_resolved to a fill with
    its own pnl_locked), so its ``real_locked_pnl`` is the FULL realized PnL
    of the session. The engine's ``realized_pnl`` MUST equal that, otherwise
    the dashboard's convergence report would silently under-report the gap.

    Specifically pins:
      * engine.locked_pnl == sum of clean-arb fills' pnl_locked ONLY
      * engine.realized_pnl == locked_pnl + residual_pnl
      * convergence.real_locked_pnl == engine.realized_pnl
      * convergence.real_locked_pnl != engine.locked_pnl (when partial pnl>0)
    """
    risk = RiskState(balance=1_000.0)
    metrics = Metrics()
    events_all: list[dict] = []

    # 1) Clean fill: YES=NO @ 0.40/0.50 -> combined 0.90, size 10 -> locked 1.0
    client1 = _FillPlanClient(
        submit_yes_ratio=1.0, submit_no_ratio=1.0,
        yes_fills=[(True, 1.0)], no_fills=[(True, 1.0)],
    )
    ex1 = Execution(client1, risk, metrics)
    st1 = _market("clean-mkt", yes_ask=0.50, no_ask=0.48)
    ex1.register(st1, size=10.0)
    events_all.extend(_fire_and_resolve(ex1, st1, combined=0.90, yes_p=0.40, no_p=0.50))

    # 2) Partial hedged: YES entry fills at 60% (ratio 0.6); NO entry never fills
    #    -> _manage_partial: live NO ask = 0.45 -> hedge BUY NO @ 0.45, YES ask 0.40
    #       -> resolved_combined 0.85 -> pnl = (1-0.85) * 6.0 = 0.90
    #    This 0.90 lands in residual_pnl (NOT locked_pnl).
    client2 = _FillPlanClient(
        submit_yes_ratio=0.6, submit_no_ratio=0.0,
        # Probe plan: YES entry fills on the first probe (sticky True). NO entry
        # never fills (first NO probe False). After force_gap, _manage_partial
        # submits a fresh hedge BUY on NO; its second probe (still on the NO
        # queue) returns True (sticky = confirmed hedge).
        yes_fills=[(True, 0.6)], no_fills=[(False, 0.0), (True, 1.0)],
    )
    ex2 = Execution(client2, risk, metrics)
    st2 = _market("hedge-mkt", yes_ask=0.40, no_ask=0.45)
    st2.best_yes_bid = 0.40
    st2.best_no_bid = 0.45
    ex2.register(st2, size=10.0)
    events_all.extend(_fire_and_resolve(
        ex2, st2, combined=0.90, yes_p=0.40, no_p=0.50, force_gap=True,
    ))

    # Engine accounting: locked_pnl has only the clean fill (1.0); residual_pnl
    # has the partial hedge (0.90). realized_pnl = locked + residual.
    assert metrics.locked_pnl == pytest.approx(1.0, abs=1e-9), (
        f"locked_pnl must hold ONLY the clean fill (1.0); got "
        f"locked_pnl={metrics.locked_pnl} (partial pnl leaked into locked)"
    )
    assert metrics.residual_pnl == pytest.approx(0.90, abs=1e-9), (
        f"residual_pnl must hold the partial hedge pnl 0.90; got "
        f"residual_pnl={metrics.residual_pnl}"
    )
    assert metrics.realized_pnl == pytest.approx(
        metrics.locked_pnl + metrics.residual_pnl, abs=1e-9,
    )

    # Convergence: feed the SAME event set; partial_resolved events fold into
    # fills so real_locked_pnl accounts the FULL realized PnL of the session.
    fills, missed = _fill_events_to_convergence_inputs(events_all)
    conv = compute_pnl_convergence(fills, missed, order_size=10.0)
    assert conv["real_locked_pnl"] == pytest.approx(
        metrics.realized_pnl, abs=1e-4,
    ), (
        f"MISMATCH (gap-class): convergence.real_locked_pnl="
        f"{conv['real_locked_pnl']} != engine.realized_pnl="
        f"{metrics.realized_pnl}"
    )
    # And it must NOT equal the engine's clean-only locked_pnl (since pnl>0 in
    # the partial bucket) — this is exactly the −28% gap class flaring.
    assert conv["real_locked_pnl"] != pytest.approx(
        metrics.locked_pnl, abs=1e-4,
    ), "residual pnl leaked into real_locked_pnl but locked_pnl didn't move"


# ---------------------------------------------------------------------------
# Cross-path: invariant (c) across entry/hedge/exit simultaneously on
# overlapping events — pins that locked_pnl and residual_pnl never blend.
# ---------------------------------------------------------------------------
def test_invariant_realized_pnl_never_blends_residual_into_locked_for_clean_arb():
    """Even when a partial occurs in the same session, locked_pnl must remain
    strictly the clean-arb sum; residual_pnl holds partial losses. The two
    accounts must not silently mix — that was the unpinning risk in the
    task_2_4 paper session gap investigation."""
    risk = RiskState(balance=1_000.0)
    metrics = Metrics()
    # 1) clean fill first
    client1 = _FillPlanClient(
        submit_yes_ratio=1.0, submit_no_ratio=1.0,
        yes_fills=[(True, 1.0)], no_fills=[(True, 1.0)],
    )
    ex1 = Execution(client1, risk, metrics)
    st1 = _market("clean1", yes_ask=0.50, no_ask=0.48)
    ex1.register(st1, size=10.0)
    _fire_and_resolve(ex1, st1, combined=0.90, yes_p=0.40, no_p=0.50)
    locked_after_clean = metrics.locked_pnl

    # 2) then a partial exit-with-loss in the SAME session/metrics object
    client2 = _FillPlanClient(
        submit_yes_ratio=1.0, submit_no_ratio=0.0,
        yes_fills=[(True, 1.0)], no_fills=[(False, 0.0)],
    )
    ex2 = Execution(client2, risk, metrics)
    st2 = _market("partial2", yes_ask=0.50, no_ask=0.50)
    st2.best_no_ask = None  # missing quote -> exit
    st2.best_yes_bid = 0.30  # sell the YES back at a 0.10 loss (cost 0.40)
    st2.best_no_bid = 0.30
    ex2.register(st2, size=10.0)
    _fire_and_resolve(ex2, st2, combined=0.90, yes_p=0.40, no_p=0.50, force_gap=True)

    # locked_pnl must be UNCHANGED by the partial exit; residual_pnl absorbs it.
    assert metrics.locked_pnl == pytest.approx(locked_after_clean, abs=1e-9), (
        f"locked_pnl changed after a partial: {locked_after_clean} -> "
        f"{metrics.locked_pnl} — residual must NOT bleed into locked"
    )
    # The partial exit booked a residual loss (proceeds 0.30 - cost 0.40 = -0.10).
    assert metrics.residual_pnl < 0.0, (
        f"expected a residual loss from the partial exit; got "
        f"residual_pnl={metrics.residual_pnl}"
    )
    # session_pnl = locked + residual; realized_pnl must equal session_pnl.
    assert metrics.session_pnl == pytest.approx(
        metrics.locked_pnl + metrics.residual_pnl, abs=1e-9,
    )
    assert metrics.realized_pnl == pytest.approx(metrics.session_pnl, abs=1e-9)
