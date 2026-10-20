"""Phase 1 tests: universe filter qualification + lifecycle transitions.

qualify_market() tests use synthetic MarketMeta objects (no network). Lifecycle
tests drive MarketUniverse.sync() against a stubbed discover_markets so the
state machine is exercised without hitting the real Gamma API -- the real-data
smoke check lives in scripts/discover_universe_smoke.py, run separately.
"""
from __future__ import annotations

from dataclasses import replace
from typing import List
from unittest.mock import AsyncMock, patch

import pytest

from polyuni.config import Settings
from polyuni.market_universe import MarketUniverse, qualify_market
from polyuni.models import ACTIVE, CLOSING, DISCOVERED, QUALIFIED, RELEASED, MarketMeta


def _settings(**overrides) -> Settings:
    base = dict(
        min_liquidity_usd=500.0, min_volume_24h_usd=1000.0, max_spread_at_mid=0.05,
        min_time_to_resolution_s=300.0, max_time_to_resolution_s=None,
        exclude_neg_risk=True, max_concurrent_markets=50, market_whitelist=[],
    )
    base.update(overrides)
    return Settings(**base)


def _meta(**overrides) -> MarketMeta:
    base = dict(
        market_id="m1", question="Q", yes_token_id="y", no_token_id="n",
        enable_order_book=True, accepting_orders=True, neg_risk=False,
        liquidity_usd=1000.0, volume_24h_usd=5000.0, spread_at_discovery=0.01,
        end_time=0.0,
    )
    base.update(overrides)
    return MarketMeta(**base)


# ---- qualify_market: table-driven ----

QUALIFY_CASES = [
    ("baseline passes", {}, True, "qualified"),
    ("no order book", {"enable_order_book": False}, False, "no_order_book"),
    ("not accepting orders", {"accepting_orders": False}, False, "not_accepting_orders"),
    ("neg risk excluded", {"neg_risk": True}, False, "neg_risk_excluded"),
    ("liquidity too low", {"liquidity_usd": 100.0}, False, "liquidity_100_below_500"),
    ("volume too low", {"volume_24h_usd": 50.0}, False, "volume24h_50_below_1000"),
    ("spread too wide", {"spread_at_discovery": 0.20}, False, "spread_0.2000_above_0.0500"),
    ("no spread reported", {"spread_at_discovery": None}, True, "qualified"),
]


@pytest.mark.parametrize("name,overrides,expected_pass,expected_reason_prefix", QUALIFY_CASES, ids=[c[0] for c in QUALIFY_CASES])
def test_qualify_market_table(name, overrides, expected_pass, expected_reason_prefix):
    meta = _meta(**overrides)
    passes, reason = qualify_market(meta, _settings())
    assert passes is expected_pass
    assert reason == expected_reason_prefix


def test_qualify_market_neg_risk_allowed_when_not_excluded():
    meta = _meta(neg_risk=True)
    passes, reason = qualify_market(meta, _settings(exclude_neg_risk=False))
    assert passes is True


def test_qualify_market_resolves_too_soon():
    meta = _meta(end_time=1000.0)
    passes, reason = qualify_market(meta, _settings(min_time_to_resolution_s=300.0), now=900.0)
    assert passes is False
    assert reason == "resolves_too_soon"


def test_qualify_market_resolves_too_far_out_when_capped():
    meta = _meta(end_time=100_000.0)
    passes, reason = qualify_market(
        meta, _settings(max_time_to_resolution_s=1000.0), now=0.0
    )
    assert passes is False
    assert reason == "resolves_too_far_out"


def test_qualify_market_unknown_end_time_never_excluded_by_horizon():
    meta = _meta(end_time=0.0)
    passes, _ = qualify_market(meta, _settings(max_time_to_resolution_s=1.0))
    assert passes is True


def test_sequential_btc_short_window_does_not_require_impossible_24h_volume():
    meta = _meta(
        question="Bitcoin Up or Down",
        slug="btc-updown-5m-1800000000",
        outcome_yes="Up",
        outcome_no="Down",
        volume_24h_usd=0.0,
    )
    passes, reason = qualify_market(meta, _settings(strategy_mode="sequential"))
    assert (passes, reason) == (True, "qualified")


# ---- MarketUniverse.sync(): lifecycle transitions ----

def _universe_with_stubbed_discovery(settings: Settings, pages: List[List[MarketMeta]]):
    """Build a MarketUniverse whose discover_markets() call returns each list in
    `pages` on successive sync() calls."""
    universe = MarketUniverse(settings)
    mock = AsyncMock(side_effect=pages)
    patcher = patch("polyuni.market_universe.discover_markets", mock)
    return universe, patcher


@pytest.mark.asyncio
async def test_new_qualifying_market_becomes_qualified_then_active():
    settings = _settings(max_concurrent_markets=50)
    meta = _meta()
    universe, patcher = _universe_with_stubbed_discovery(settings, [[meta]])
    with patcher:
        result = await universe.sync(session=None)

    assert result.newly_qualified == ["m1"]
    assert universe.markets["m1"].lifecycle_state == ACTIVE  # under capacity -> promoted same pass
    assert result.promoted_to_active == ["m1"]


@pytest.mark.asyncio
async def test_disqualifying_market_moves_active_to_closing():
    settings = _settings()
    good = _meta(market_id="m1", volume_24h_usd=5000.0)
    bad = replace(good, volume_24h_usd=1.0)  # drops below min_volume_24h_usd next pass
    universe, patcher = _universe_with_stubbed_discovery(settings, [[good], [bad]])
    with patcher:
        await universe.sync(session=None)
        assert universe.markets["m1"].lifecycle_state == ACTIVE
        result = await universe.sync(session=None)

    assert result.newly_disqualified == ["m1"]
    assert universe.markets["m1"].lifecycle_state == CLOSING


@pytest.mark.asyncio
async def test_market_dropped_from_gamma_response_also_closes():
    settings = _settings()
    meta = _meta(market_id="m1")
    universe, patcher = _universe_with_stubbed_discovery(settings, [[meta], []])
    with patcher:
        await universe.sync(session=None)
        assert universe.markets["m1"].lifecycle_state == ACTIVE
        result = await universe.sync(session=None)

    assert "m1" in result.newly_disqualified
    assert universe.markets["m1"].lifecycle_state == CLOSING


@pytest.mark.asyncio
async def test_capacity_gate_only_promotes_top_n_by_volume():
    settings = _settings(max_concurrent_markets=2)
    markets = [
        _meta(market_id="m1", volume_24h_usd=9000.0),
        _meta(market_id="m2", volume_24h_usd=8000.0),
        _meta(market_id="m3", volume_24h_usd=7000.0),
    ]
    universe, patcher = _universe_with_stubbed_discovery(settings, [markets])
    with patcher:
        result = await universe.sync(session=None)

    assert sorted(result.promoted_to_active) == ["m1", "m2"]
    assert universe.markets["m1"].lifecycle_state == ACTIVE
    assert universe.markets["m2"].lifecycle_state == ACTIVE
    assert universe.markets["m3"].lifecycle_state == QUALIFIED  # eligible, just over capacity


@pytest.mark.asyncio
async def test_capacity_gate_prioritizes_sequential_btc_window_before_high_volume_market():
    settings = _settings(max_concurrent_markets=1, strategy_mode="sequential")
    generic = _meta(market_id="generic", volume_24h_usd=100_000.0)
    window = _meta(
        market_id="window",
        question="Bitcoin Up or Down",
        slug="btc-updown-5m-1800000000",
        outcome_yes="Up",
        outcome_no="Down",
        volume_24h_usd=0.0,
    )
    universe, patcher = _universe_with_stubbed_discovery(settings, [[generic, window]])
    with patcher:
        await universe.sync(session=None)

    assert universe.markets["window"].lifecycle_state == ACTIVE
    assert universe.markets["generic"].lifecycle_state == QUALIFIED


@pytest.mark.asyncio
async def test_capacity_pressure_demotes_lower_volume_active_market():
    settings = _settings(max_concurrent_markets=1)
    m1 = _meta(market_id="m1", volume_24h_usd=5000.0)
    universe, patcher = _universe_with_stubbed_discovery(settings, [[m1]])
    with patcher:
        await universe.sync(session=None)
    assert universe.markets["m1"].lifecycle_state == ACTIVE

    m1_same = replace(m1, volume_24h_usd=5000.0)
    m2_bigger = _meta(market_id="m2", volume_24h_usd=9000.0)
    mock = AsyncMock(return_value=[m1_same, m2_bigger])
    with patch("polyuni.market_universe.discover_markets", mock):
        result = await universe.sync(session=None)

    assert universe.markets["m2"].lifecycle_state == ACTIVE
    assert universe.markets["m1"].lifecycle_state == QUALIFIED
    assert result.demoted_from_active == ["m1"]


def test_finalize_closing_transitions_to_released_only_for_closing_markets():
    settings = _settings()
    universe = MarketUniverse(settings)
    universe.markets = {
        "m1": _meta(market_id="m1", lifecycle_state=CLOSING),
        "m2": _meta(market_id="m2", lifecycle_state=ACTIVE),
    }

    released = universe.finalize_closing()

    assert released == ["m1"]
    assert universe.markets["m1"].lifecycle_state == RELEASED
    assert universe.markets["m2"].lifecycle_state == ACTIVE  # untouched


def test_finalize_closing_respects_explicit_market_id_list():
    settings = _settings()
    universe = MarketUniverse(settings)
    universe.markets = {
        "m1": _meta(market_id="m1", lifecycle_state=CLOSING),
        "m2": _meta(market_id="m2", lifecycle_state=CLOSING),
    }

    released = universe.finalize_closing(market_ids=["m1"])

    assert released == ["m1"]
    assert universe.markets["m1"].lifecycle_state == RELEASED
    assert universe.markets["m2"].lifecycle_state == CLOSING  # not finalized yet


def test_btc_only_mode_rejects_non_btc_markets():
    import dataclasses
    from polyuni.config import SETTINGS

    settings = dataclasses.replace(SETTINGS, btc_only=True)
    meta = _meta()
    assert not meta.is_btc_up_down
    passes, reason = qualify_market(meta, settings)
    assert not passes and reason == "btc_only_mode"
