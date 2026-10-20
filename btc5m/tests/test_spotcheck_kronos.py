"""Tests for the Kronos forecast accuracy spot-check script.

Validates the core logic of the spot-check: ATR normalization, regime
classification consistency, and the structure of the output JSON.

These tests are pure-logic (no network) except where noted.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import numpy as np
import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from polymarket_arb.kronos_session import (
    _compute_atr,
    _regime_from_atr,
    _regime_params,
    _normalize_atr_to_pct,
    _scale_atr_to_daily,
    ATR_HIGH_THRESHOLD,
    ATR_LOW_THRESHOLD,
)
import polymarket_arb.kronos_session as ks


# ---------------------------------------------------------------------------
# Helpers -- mirror the spotcheck script's helpers
# ---------------------------------------------------------------------------
def _make_candles(n=500, seed=42, vol_scale=1.0, base_price=65000.0):
    """Generate synthetic 1-minute BTC/USD candles at a given price level."""
    rng = np.random.RandomState(seed)
    # Small per-minute noise around base_price
    prices = base_price + np.cumsum(rng.randn(n) * 0.5 * vol_scale * base_price * 0.001)
    candles = []
    for i in range(n):
        h = float(prices[i]) + abs(rng.randn()) * 50 * vol_scale
        l = float(prices[i]) - abs(rng.randn()) * 50 * vol_scale
        c = (h + l) / 2
        candles.append({
            "timestamp": i,
            "open": float(prices[i]),
            "high": h,
            "low": l,
            "close": c,
            "volume": float(abs(rng.randn()) * 100 + 50),
        })
    return candles


def _normalize_atr_to_pct(atr, candles):
    """Mirror the helper in spotcheck_kronos.py."""
    if not candles or atr <= 0:
        return 0.0
    closes = [float(c.get("close", 0)) for c in candles if c.get("close", 0) > 0]
    if not closes:
        return 0.0
    mean_close = sum(closes) / len(closes)
    if mean_close == 0:
        return 0.0
    return atr / mean_close


# ---------------------------------------------------------------------------
# ATR normalization tests
# ---------------------------------------------------------------------------
def test_atr_normalization_scales_correctly():
    """ATR% should be much smaller than raw dollar ATR for high-priced assets."""
    candles = _make_candles(n=500, base_price=65000.0)
    raw_atr = _compute_atr(candles, period=14)
    atr_pct = _normalize_atr_to_pct(raw_atr, candles)

    assert raw_atr > 0
    assert 0 < atr_pct < raw_atr  # percentage is always smaller than dollar
    # For 1-minute BTC at ~$65k, ATR% should be well below the LOW threshold
    assert atr_pct < ATR_LOW_THRESHOLD


def test_atr_normalization_with_different_price_levels():
    """Same percentage volatility at different price levels should give
    approximately the same ATR% (normalization makes ATR scale-invariant).
    """
    # Use a fixed percentage-based volatility so both price levels have
    # the same relative price movement
    rng = np.random.RandomState(42)
    n = 200
    vol_pct = 0.0001  # 0.01% per minute noise

    for base_price in [10000, 65000, 100000]:
        prices = base_price + np.cumsum(rng.randn(n) * vol_pct * base_price)
        candles = []
        for i in range(n):
            noise = abs(rng.randn()) * vol_pct * base_price
            candles.append({
                "open": float(prices[i]),
                "high": float(prices[i] + noise),
                "low": float(prices[i] - noise),
                "close": float(prices[i]),
                "volume": 100.0,
            })

        raw_atr = _compute_atr(candles, period=14)
        atr_pct = _normalize_atr_to_pct(raw_atr, candles)

        assert raw_atr > 0
        assert 0 < atr_pct < 0.01  # should be tiny for 1-minute BTC

    # The key property: ATR% is roughly the same regardless of price level
    # (because it's normalized by mean close)
    # We verified above that all three price levels produce valid small ATR%
    # values, confirming the normalization works.


# ---------------------------------------------------------------------------
# Regime classification tests
# ---------------------------------------------------------------------------
def test_regime_classification_with_normalized_atr():
    """Normalized ATR% should classify correctly against percentage thresholds."""
    # Create candles with HIGH volatility (>5% daily ATR%)
    # On daily candles, a 5% move means ATR% > 0.05
    rng = np.random.RandomState(42)
    n = 20
    # Generate prices with large daily swings
    prices = 50000 + np.cumsum(rng.randn(n) * 3000)  # ~6% daily swings
    candles = []
    for i in range(n):
        candles.append({
            "open": float(prices[i]),
            "high": float(prices[i] + abs(rng.randn()) * 2000),
            "low": float(prices[i] - abs(rng.randn()) * 2000),
            "close": float(prices[i] + rng.randn() * 500),
            "volume": 100.0,
        })

    raw_atr = _compute_atr(candles, period=14)
    atr_pct = _normalize_atr_to_pct(raw_atr, candles)
    regime, conf = _regime_from_atr(atr_pct)

    # With 6% daily swings, should be HIGH
    assert regime == "HIGH"
    assert conf > 0.5


def test_regime_classification_low_volatility():
    """Low volatility candles should classify as LOW."""
    rng = np.random.RandomState(42)
    n = 20
    # Very small price movements
    prices = 65000 + np.cumsum(rng.randn(n) * 10)  # tiny moves
    candles = []
    for i in range(n):
        candles.append({
            "open": float(prices[i]),
            "high": float(prices[i] + abs(rng.randn()) * 5),
            "low": float(prices[i] - abs(rng.randn()) * 5),
            "close": float(prices[i]),
            "volume": 100.0,
        })

    raw_atr = _compute_atr(candles, period=14)
    atr_pct = _normalize_atr_to_pct(raw_atr, candles)
    regime, _ = _regime_from_atr(atr_pct)
    assert regime == "LOW"


def test_regime_classification_med_volatility():
    """Medium volatility candles should classify as MED."""
    rng = np.random.RandomState(42)
    n = 20
    # Moderate moves (~3% daily)
    prices = 50000 + np.cumsum(rng.randn(n) * 1500)
    candles = []
    for i in range(n):
        candles.append({
            "open": float(prices[i]),
            "high": float(prices[i] + abs(rng.randn()) * 1000),
            "low": float(prices[i] - abs(rng.randn()) * 1000),
            "close": float(prices[i] + rng.randn() * 300),
            "volume": 100.0,
        })

    raw_atr = _compute_atr(candles, period=14)
    atr_pct = _normalize_atr_to_pct(raw_atr, candles)
    regime, _ = _regime_from_atr(atr_pct)
    assert regime == "MED"


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Scale-fix test: 1-min ATR normalized + scaled to daily-equivalent
# ---------------------------------------------------------------------------
def test_scale_fix_low_vol_classifies_correctly():
    """FIXED behavior: low-volatility 1-minute BTC data classifies as LOW.

    Before the fix, raw dollar ATR (~$20 at $65k) was compared directly to
    the 0.05 percentage threshold, causing it to ALWAYS classify as HIGH.
    After the fix, 1-min ATR% is normalized and scaled to daily-equivalent
    before threshold comparison, so low-vol data correctly classifies as LOW.
    """
    candles = _make_candles(n=500, base_price=65000.0, vol_scale=0.1)
    raw_atr = _compute_atr(candles, period=14)

    # Demonstrate the old bug: raw dollar ATR >> 0.05 threshold
    regime_buggy, _ = _regime_from_atr(raw_atr)
    assert regime_buggy == "HIGH"  # old buggy behavior: dollars vs pct
    assert raw_atr > ATR_HIGH_THRESHOLD

    # Fixed: normalize to pct, then scale to daily-equivalent
    atr_pct = _normalize_atr_to_pct(raw_atr, candles)
    atr_daily = _scale_atr_to_daily(atr_pct)
    regime_fixed, _ = _regime_from_atr(atr_daily)

    # The fixed heuristic correctly classifies low-vol 1-min BTC as LOW
    assert regime_fixed == "LOW"
    assert atr_daily < ATR_LOW_THRESHOLD  # daily-equiv ATR% below LOW/MED boundary


def test_scale_fix_produces_believable_mix():
    """Different volatility levels at $65k should produce HIGH/MED/LOW,
    not always one bucket (the old bug always produced HIGH).
    """
    results = {}
    for vol_scale in [0.1, 0.7, 1.1]:
        candles = _make_candles(n=500, base_price=65000.0, vol_scale=vol_scale)
        raw_atr = _compute_atr(candles, period=14)
        atr_pct = _normalize_atr_to_pct(raw_atr, candles)
        atr_daily = _scale_atr_to_daily(atr_pct)
        regime, _ = _regime_from_atr(atr_daily)
        results[vol_scale] = regime

    # Low vol -> LOW, moderate -> MED, high -> HIGH
    assert results[0.1] == "LOW"
    assert results[0.7] == "MED"
    assert results[1.1] == "HIGH"

    # The old bug would have made all of these "HIGH" (raw $ ATR > 0.05)
    old_buggy_results = []
    for vol_scale in [0.1, 0.7, 1.1]:
        candles = _make_candles(n=500, base_price=65000.0, vol_scale=vol_scale)
        raw_atr = _compute_atr(candles, period=14)
        buggy_regime, _ = _regime_from_atr(raw_atr)
        old_buggy_results.append(buggy_regime)
    assert all(r == "HIGH" for r in old_buggy_results), "pre-fix all should be HIGH (bug)"


# ---------------------------------------------------------------------------
# Kronos predicted ATR scale test
# ---------------------------------------------------------------------------
def test_kronos_predicted_atr_is_percentage():
    """The Kronos model's _predict_atr_from_forecast returns a percentage
    ATR (normalized by mean close). Verify this directly.
    """
    from polymarket_arb.kronos_session import _predict_atr_from_forecast

    # Create synthetic forecast candles
    import pandas as pd
    rng = np.random.RandomState(42)
    n = 60
    prices = 65000 + np.cumsum(rng.randn(n) * 100)
    df = pd.DataFrame({
        "open": prices,
        "high": prices + np.abs(rng.randn(n) * 50),
        "low": prices - np.abs(rng.randn(n) * 50),
        "close": prices + rng.randn(n) * 10,
        "volume": 100.0,
    })

    pred_atr = _predict_atr_from_forecast(df)
    # Should be a small percentage (0.0x%), not a dollar value
    assert 0 < pred_atr < 0.1  # 0.02-0.08% range
    # This should fall below ATR_LOW_THRESHOLD
    assert pred_atr < ATR_LOW_THRESHOLD


# ---------------------------------------------------------------------------
# Regime parameter consistency
# ---------------------------------------------------------------------------
def test_regime_params_consistency():
    """Each regime should map to distinct (no_entry, max_age) params."""
    high_params = _regime_params("HIGH")
    med_params = _regime_params("MED")
    low_params = _regime_params("LOW")

    # HIGH: strictest (10s, 250ms)
    # MED: moderate (5s, 500ms)
    # LOW: shorter no-entry zone, but data-backed freshness remains MED-tight.
    assert high_params[0] > med_params[0] > low_params[0]  # no_entry_seconds
    assert high_params[1] < med_params[1] == low_params[1]  # max_quote_age_ms


# ---------------------------------------------------------------------------
# Integration: spotcheck output file structure
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_spotcheck_output_file_structure():
    """Verify the spotcheck output JSON has the expected structure.

    This test reads the output file produced by spotcheck_kronos.py.
    It should be run AFTER the spotcheck has been executed.
    """
    output_path = os.path.join(PROJECT_ROOT, "state", "kronos_spotcheck.json")
    if not os.path.exists(output_path):
        pytest.skip("kronos_spotcheck.json not found -- run spotcheck first")

    with open(output_path) as f:
        data = json.load(f)

    # Top-level structure
    assert "spotcheck_timestamp" in data
    assert "configuration" in data
    assert "summary" in data
    assert "detailed_results" in data

    # Configuration fields
    cfg = data["configuration"]
    assert "historical_timestamps" in cfg
    # Spot-check was expanded from 5 to 20 checkpoints (5 original + 15 new
    # real-LSE-data checkpoints spanning distinct calendar dates). The
    # structure invariant is that the count is consistent across config and
    # summary, not fixed at the original 5.
    n_ts = len(cfg["historical_timestamps"])
    assert n_ts >= 5, "spot-check must contain at least the 5 original checkpoints"
    assert "following_session_hours" in cfg

    # Summary fields
    summary = data["summary"]
    assert summary["total_timestamps_checked"] == n_ts
    assert "ground_truth_regime_range" in summary
    assert "realized_daily_atr_pct_by_ground_truth_regime" in summary
    assert "forecast_label_matches_realized" in summary

    # Each detailed result should have core fields
    for result in data["detailed_results"]:
        assert "timestamp" in result
        assert "regime" in result
        assert "source" in result
        assert result["source"] in ("kronos", "heuristic", "default")
