"""Phase 0 smoke tests for models.py -- the lifecycle-state fields replacing
polymarket_arb's window_start/window_close (see market_universe.py, Phase 1)."""
from __future__ import annotations

import pytest

from polyuni.models import ACTIVE, DISCOVERED, MarketMeta, MarketState, TradeRecord


def _state(**overrides) -> MarketState:
    base = dict(
        market_id="m1", question="Will X happen?",
        yes_token_id="yes-1", no_token_id="no-1",
    )
    base.update(overrides)
    return MarketState(**base)


def test_combined_ask_none_until_both_legs_present():
    st = _state()
    assert st.combined_ask is None
    st.best_yes_ask = 0.40
    assert st.combined_ask is None
    st.best_no_ask = 0.55
    assert st.combined_ask == pytest.approx(0.95)


def test_max_age_ms_uses_older_of_two_legs():
    now = 1000.0
    st = _state(best_yes_ask=0.4, best_no_ask=0.5, yes_ts=now - 2.0, no_ts=now - 0.1)
    # older leg (yes, 2s stale) governs, not the average or the newer leg
    assert st.max_age_ms(now=now) == 2000.0


def test_max_age_ms_infinite_when_a_leg_missing():
    st = _state(best_yes_ask=0.4)
    assert st.max_age_ms() == float("inf")


def test_no_entry_zone_requires_known_end_time():
    st = _state()
    assert st.in_no_entry_zone(5.0) is False  # end_time unset -> never blocked
    st.end_time = 1010.0
    assert st.in_no_entry_zone(no_entry_seconds=20.0, now=1000.0) is True
    assert st.in_no_entry_zone(no_entry_seconds=5.0, now=1000.0) is False


def test_market_meta_round_trips_through_dict():
    meta = MarketMeta(
        market_id="m1", question="Q", yes_token_id="y", no_token_id="n",
        liquidity_usd=1234.5, volume_24h_usd=6789.0, enable_order_book=True,
        neg_risk=False, accepting_orders=True, has_description=True,
        lifecycle_state=ACTIVE,
    )
    restored = MarketMeta.from_dict(meta.to_dict())
    assert restored == meta


def test_market_meta_from_dict_defaults_lifecycle_to_discovered():
    restored = MarketMeta.from_dict({
        "market_id": "m1", "question": "Q", "yes_token_id": "y", "no_token_id": "n",
    })
    assert restored.lifecycle_state == DISCOVERED


def test_trade_record_partial_is_xor_of_leg_fills():
    rec = TradeRecord(
        market_id="m1", ts=0.0, yes_price=0.4, no_price=0.5, combined=0.9, size=10.0,
        yes_filled=True, no_filled=False,
    )
    assert rec.partial is True
    rec.no_filled = True
    assert rec.partial is False
