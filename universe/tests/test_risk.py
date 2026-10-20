"""Tests for risk.py's capital accounting, kill-switch, and macro-provider
injection. Capital accounting is flagged in the plan as the most bug-sensitive
surface being carried over from polymarket_arb's execution.py -- tested here at
the risk.py level (reserve/commit/release atomicity), independently of the
execution split in Phase 2's other modules.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from polyuni.models import MarketState
from polyuni.risk import MacroCalendarProvider, NullMacroCalendarProvider, RiskState


def _state(**overrides) -> MarketState:
    import time as _time
    base = dict(
        market_id="m1", question="Q", yes_token_id="y", no_token_id="n",
        best_yes_ask=0.4, best_no_ask=0.5, yes_size=100.0, no_size=100.0,
        last_update_ts=_time.time(),  # fresh unless the caller overrides it
    )
    base.update(overrides)
    return MarketState(**base)


class _AlwaysHighRisk:
    """Test double: a MacroCalendarProvider that is always healthy but always
    reports high risk -- exercises the edge-widen / no-entry-widen paths."""

    def health(self, now):
        return None, None, {}

    def is_high_risk(self, now):
        return True


class _UnhealthyProvider:
    def health(self, now):
        return "stale", "macro feed is 999s old", {"age_seconds": 999.0}

    def is_high_risk(self, now):
        return False


def test_precheck_passes_under_defaults():
    risk = RiskState()
    st = _state()
    assert risk.precheck(st, cost_yes=4.0, cost_no=5.0) is None


def test_precheck_rejects_stale_book():
    import time
    risk = RiskState()
    # last_update_ts=0.0 has special "never set" semantics (falsy -> the staleness
    # check is intentionally skipped, deferring to arb_engine's own freshness gate)
    # -- a genuinely stale, non-zero timestamp is what actually exercises this gate.
    st = _state(last_update_ts=time.time() - 3600.0)
    assert "stale book" in risk.precheck(st, 4.0, 5.0)


def test_precheck_skips_staleness_check_when_last_update_ts_never_set():
    st = _state(last_update_ts=0.0)
    risk = RiskState()
    assert risk.precheck(st, 4.0, 5.0) is None


def test_precheck_rejects_low_liquidity_either_side():
    risk = RiskState()
    st = _state(yes_size=0.1)
    assert risk.precheck(st, 4.0, 5.0) == "low YES liquidity"
    st2 = _state(no_size=0.1)
    assert risk.precheck(st2, 4.0, 5.0) == "low NO liquidity"


def test_precheck_rejects_over_max_total_capital(monkeypatch):
    import polyuni.config as config_mod
    monkeypatch.setattr(config_mod, "SETTINGS",
                         config_mod.Settings(max_total_capital=10.0, max_position_per_market=10.0))
    monkeypatch.setattr("polyuni.risk.SETTINGS", config_mod.SETTINGS)
    risk = RiskState(total_deployed=8.0)
    st = _state()
    assert risk.precheck(st, cost_yes=1.0, cost_no=5.0) == "max capital exceeded"


def test_reserve_commit_release_round_trip_is_atomic():
    risk = RiskState()
    st = _state()

    reason = asyncio.run(risk.reserve(st, cost_yes=4.0, cost_no=5.0))
    assert reason is None
    assert risk.pending_deployed == pytest.approx(9.0)
    assert risk.pending_per_market["m1"] == pytest.approx(9.0)

    risk.commit("m1", 9.0, from_reservation=9.0)
    assert risk.total_deployed == pytest.approx(9.0)
    assert risk.pending_deployed == pytest.approx(0.0)
    assert "m1" not in risk.pending_per_market

    risk.release("m1", 9.0)
    assert risk.total_deployed == pytest.approx(0.0)
    assert "m1" not in risk.deployed_per_market


def test_unreserve_on_abort_frees_pending_without_touching_deployed():
    risk = RiskState()
    st = _state()
    asyncio.run(risk.reserve(st, cost_yes=4.0, cost_no=5.0))

    risk.unreserve("m1", 9.0)

    assert risk.pending_deployed == pytest.approx(0.0)
    assert risk.total_deployed == pytest.approx(0.0)


def test_concurrent_reserves_cannot_both_over_deploy(monkeypatch):
    """Two concurrent fires for prices that individually fit under
    max_total_capital but together exceed it: only one should be admitted --
    this is the exact 'atomic reserve' invariant polymarket_arb had a dedicated
    regression test for."""
    import polyuni.config as config_mod
    fresh_settings = config_mod.Settings(max_total_capital=15.0, max_position_per_market=15.0)
    monkeypatch.setattr("polyuni.risk.SETTINGS", fresh_settings)

    risk = RiskState()
    st_a = _state(market_id="a")
    st_b = _state(market_id="b")

    async def run():
        return await asyncio.gather(
            risk.reserve(st_a, cost_yes=5.0, cost_no=5.0),   # 10
            risk.reserve(st_b, cost_yes=5.0, cost_no=5.0),   # 10, 10+10=20 > 15
        )

    results = asyncio.run(run())
    admitted = [r for r in results if r is None]
    rejected = [r for r in results if r is not None]
    assert len(admitted) == 1
    assert len(rejected) == 1
    assert rejected[0] == "max capital exceeded"


def test_daily_kill_switch_rejects_and_alerts_once(monkeypatch):
    import polyuni.config as config_mod
    monkeypatch.setattr("polyuni.risk.SETTINGS",
                         config_mod.Settings(daily_kill_switch_usd=10.0))

    calls = []

    class _FakeAlerter:
        def alert(self, kind, message, **fields):
            calls.append((kind, message, fields))

    risk = RiskState(day_pnl=-9.0, alerter=_FakeAlerter())
    st = _state()

    r1 = risk.precheck(st, cost_yes=1.0, cost_no=1.0)  # day_pnl - 2 = -11 < -10
    assert r1 == "daily kill switch hit"
    r2 = risk.precheck(st, cost_yes=1.0, cost_no=1.0)
    assert r2 == "daily kill switch hit"
    assert len(calls) == 1  # deduplicated, not one alert per rejected precheck


def test_halt_is_idempotent_and_blocks_everything():
    risk = RiskState()
    st = _state()
    risk.halt("test")
    risk.halt("test again")  # idempotent, no double-alert assumption needed here
    assert risk.halted is True
    assert risk.precheck(st, 1.0, 1.0) == "halted/emergency-stop"


def test_emergency_stop_file_blocks_precheck(tmp_path, monkeypatch):
    import polyuni.config as config_mod
    stop_file = tmp_path / "EMERGENCY_STOP"
    monkeypatch.setattr("polyuni.risk.SETTINGS",
                         config_mod.Settings(emergency_stop_file=str(stop_file)))
    risk = RiskState()
    st = _state()
    assert risk.precheck(st, 1.0, 1.0) is None
    stop_file.touch()
    assert risk.precheck(st, 1.0, 1.0) == "halted/emergency-stop"


# ---- macro provider injection ----

def test_null_macro_provider_never_gates_anything():
    risk = RiskState(macro_provider=NullMacroCalendarProvider())
    st = _state()
    assert risk.precheck(st, 4.0, 5.0) is None


def test_high_risk_provider_widens_required_edge(monkeypatch):
    import polyuni.config as config_mod
    monkeypatch.setattr("polyuni.risk.SETTINGS",
                         config_mod.Settings(min_edge=0.01))
    risk = RiskState(macro_provider=_AlwaysHighRisk(), macro_edge_widen=0.05)
    # combined = 0.4+0.55 = 0.95; normal threshold 1-0.01=0.99 (would pass);
    # macro-widened threshold 1-(0.01+0.05)=0.94 (0.95 >= 0.94 -> rejected)
    st = _state(best_yes_ask=0.40, best_no_ask=0.55)
    reason = risk.precheck(st, cost_yes=4.0, cost_no=5.5)
    assert reason is not None
    assert "macro high-risk edge" in reason


def test_unhealthy_macro_provider_blocks_with_reason():
    risk = RiskState(macro_provider=_UnhealthyProvider())
    st = _state()
    reason = risk.precheck(st, 1.0, 1.0)
    assert reason == "macro state stale"


def test_macro_provider_satisfies_protocol_shape():
    provider: MacroCalendarProvider = NullMacroCalendarProvider()
    assert provider.health(0.0) == (None, None, {})
    assert provider.is_high_risk(0.0) is False
