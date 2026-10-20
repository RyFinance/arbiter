from __future__ import annotations

import pytest

from polyuni.fees import effective_fee_rate, taker_fee


def test_crypto_taker_fee_matches_official_curve_at_48_cents():
    assert taker_fee(10, 0.48, 0.07) == pytest.approx(0.17472)


def test_fee_rounds_to_five_decimals_and_sub_quantum_amounts_to_zero():
    assert taker_fee(0.001, 0.01, 0.07) == 0.0
    assert taker_fee(1, 0.5, 0.000024) == pytest.approx(0.00001)


def test_explicit_fee_free_market_overrides_category_fallback():
    assert effective_fee_rate(
        fees_enabled=False,
        market_rate=0.07,
        is_crypto=True,
        crypto_fallback=0.07,
        default_fallback=0.05,
    ) == 0.0

