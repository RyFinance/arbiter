"""Tests for kronos_session.py — Kronos model loading, inference, and ATR fallback.

Covers:
  * ATR heuristic computation (deterministic, no network)
  * Regime classification from ATR values
  * Kronos model failure -> ATR heuristic fallback
  * Kronos timeout -> ATR heuristic fallback
  * Kronos unavailable (ImportError) -> ATR heuristic fallback
  * SESSION_VOL_REGIME is set correctly after forecast
  * use_kronos=False skip -> ATR heuristic directly
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT / "scripts"))
from fill_and_test_arb_v3 import FEATURE_COLUMNS as _LGBM_FEATURE_COLUMNS  # noqa: E402

from polymarket_arb.kronos_session import (
    VolRegime,
    _compute_atr,
    _regime_from_atr,
    _regime_params,
    _normalize_atr_to_pct,
    _scale_atr_to_daily,
    _try_lightgbm,
    forecast_session_regime,
    apply_regime_to_settings,
    SESSION_VOL_REGIME,
    KRONOS_TIMEOUT_SECONDS,
    ATR_HIGH_THRESHOLD,
    ATR_LOW_THRESHOLD,
    ATR_TIME_SCALING_FACTOR,
)
from polymarket_arb.config import SETTINGS
import polymarket_arb.kronos_session as ks


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_candles(n=500, seed=42, vol_scale=1.0):
    """Generate synthetic 1m BTC/USD candles for testing."""
    rng = np.random.RandomState(seed)
    prices = 100 + np.cumsum(rng.randn(n) * 0.5 * vol_scale)
    candles = []
    for i in range(n):
        h = prices[i] + abs(rng.randn()) * 1.0 * vol_scale
        l = prices[i] - abs(rng.randn()) * 1.0 * vol_scale
        c = (h + l + prices[i]) / 3
        candles.append({
            "timestamp": i,
            "open": float(prices[i]),
            "high": float(h),
            "low": float(l),
            "close": float(c),
            "volume": float(abs(rng.randn()) * 100 + 50),
        })
    return candles


def _mock_lse(candles):
    """Create a mock LSEClient that returns the given candles."""
    mock = AsyncMock()
    mock.fetch_btc_candles_for_kronos = AsyncMock(return_value=candles)
    mock.aclose = AsyncMock()
    return mock


def _make_realistic_candles(n=120, seed=42, vol_scale=1.0):
    """Like _make_candles, but with real, evenly 1-minute-spaced timestamps.

    _try_lightgbm needs genuine timestamps (unlike _try_kronos, which uses a
    synthetic evenly-spaced timeline internally) since several features are
    time-indexed rolling windows — _make_candles's plain-integer
    "timestamp": i is interpreted as nanoseconds-since-epoch by
    pd.to_datetime, which compresses all rows into under a microsecond and
    correctly (if confusingly) starves every rolling-window feature.
    """
    rng = np.random.RandomState(seed)
    prices = 100 + np.cumsum(rng.randn(n) * 0.5 * vol_scale)
    start = pd.Timestamp("2026-01-01T00:00:00Z")
    candles = []
    for i in range(n):
        h = prices[i] + abs(rng.randn()) * 1.0 * vol_scale
        l = prices[i] - abs(rng.randn()) * 1.0 * vol_scale
        c = (h + l + prices[i]) / 3
        candles.append({
            "timestamp": (start + pd.Timedelta(minutes=i)).isoformat(),
            "open": float(prices[i]),
            "high": float(h),
            "low": float(l),
            "close": float(c),
            "volume": float(abs(rng.randn()) * 100 + 50),
        })
    return candles


# ---------------------------------------------------------------------------
# ATR heuristic tests (pure logic, no Kronos dependency)
# ---------------------------------------------------------------------------
def test_compute_atr_basic():
    """ATR should be positive for a series with price movement."""
    candles = _make_candles(n=50)
    atr = _compute_atr(candles, period=14)
    assert atr > 0.0


def test_compute_atr_single_point_returns_zero():
    """ATR with too few candles should return 0."""
    candles = _make_candles(n=5)
    atr = _compute_atr(candles, period=14)
    assert atr == 0.0


def test_regime_from_atr_high():
    regime, conf = _regime_from_atr(0.08)  # 8% ATR -> HIGH
    assert regime == "HIGH"
    assert 0.0 <= conf <= 1.0


def test_regime_from_atr_med():
    regime, conf = _regime_from_atr(0.035)  # 3.5% ATR -> MED
    assert regime == "MED"
    assert 0.0 <= conf <= 1.0


def test_regime_from_atr_low():
    regime, conf = _regime_from_atr(0.01)  # 1% ATR -> LOW
    assert regime == "LOW"
    assert 0.0 <= conf <= 1.0


def test_regime_params():
    # HIGH moderated 2026-08-20 (STATUS.md L3: Kronos's HIGH call only
    # matches realized vol 55% of the time, biased toward over-predicting
    # HIGH) — was needlessly punitive relative to the weak signal behind it.
    assert _regime_params("HIGH") == (7.0, 400)
    assert _regime_params("MED") == (5.0, 500)
    assert _regime_params("LOW") == (3.0, 500)


def test_atr_thresholds():
    assert ATR_HIGH_THRESHOLD == 0.05
    assert ATR_LOW_THRESHOLD == 0.03


# ---------------------------------------------------------------------------
# _try_lightgbm — real feature pipeline, stubbed model (no dependency on the
# actual trained artifact under state/, so this runs the same in any clone)
# ---------------------------------------------------------------------------
def _stub_lightgbm_model(proba, feature_names=None):
    """A fake fitted estimator: predict_proba always returns this one row.

    feature_name_ must be a real list, not left as an auto-created MagicMock
    attribute — _try_lightgbm does `getattr(model, "feature_name_", None) or
    [...]`, and a bare MagicMock() is truthy, so `list(mock.feature_name_)`
    would run instead of the intended fallback and blow up unpredictably.
    Defaults to the real training feature list (includes fear_greed_value/
    funding_rate_bps) so tests exercise the same shape a real model has.
    """
    model = MagicMock()
    model.predict_proba = MagicMock(return_value=np.array([proba]))
    model.feature_name_ = list(feature_names) if feature_names is not None else list(_LGBM_FEATURE_COLUMNS)
    return model


def test_try_lightgbm_predicts_from_real_feature_pipeline():
    """With enough real, evenly-spaced history, _try_lightgbm computes real
    features (via the same calculate_technical_indicators used for training)
    and maps the model's argmax class to a regime + params."""
    candles = _make_realistic_candles(n=120)
    stub = _stub_lightgbm_model([0.1, 0.2, 0.7])  # HIGH wins
    # fear_greed_value/funding_rate_bps/book-depth/OI-longshort/cross-asset
    # are all live one-call fetches — mock every one so this test never
    # touches the network and stays deterministic.
    with patch.object(ks, "_load_lightgbm_model", return_value=stub), \
         patch.object(ks, "_fetch_current_fear_greed", return_value=42.0), \
         patch.object(ks, "_fetch_current_funding_rate_bps", return_value=1.5), \
         patch.object(ks, "_fetch_current_book_depth_features", return_value={
             "depth_imbalance_1pct": 0.1, "depth_imbalance_5pct": 0.05, "depth_total_notional_1pct": 1e8,
         }), \
         patch.object(ks, "_fetch_current_oi_longshort", return_value={
             "open_interest": 1e5, "oi_change_1h_pct": 0.5,
             "toptrader_long_short_ratio": 1.2, "taker_long_short_vol_ratio": 0.9,
         }), \
         patch.object(ks, "_fetch_current_cross_asset", return_value={
             "eth_return_1d_lag": 1.0, "eth_realized_vol_5d_lag": 2.0,
         }):
        result = _try_lightgbm(candles)

    assert result is not None
    assert result.source == "lightgbm"
    assert result.regime == "HIGH"
    assert result.confidence == pytest.approx(0.7)
    assert result.no_entry_seconds == 7.0  # _regime_params("HIGH")
    assert result.max_quote_age_ms == 400
    stub.predict_proba.assert_called_once()


def test_try_lightgbm_insufficient_history_returns_none():
    """Fewer than 60 real minutes of history -> None (not enough warmup for
    the slowest rolling-window feature), regardless of model availability."""
    candles = _make_realistic_candles(n=30)
    stub = _stub_lightgbm_model([0.1, 0.2, 0.7])
    with patch.object(ks, "_load_lightgbm_model", return_value=stub):
        result = _try_lightgbm(candles)
    assert result is None
    stub.predict_proba.assert_not_called()


def test_try_lightgbm_model_unavailable_returns_none():
    """No model artifact on disk (or joblib load failure) -> None, caller
    falls back to Kronos then the ATR heuristic."""
    candles = _make_realistic_candles(n=120)
    with patch.object(ks, "_load_lightgbm_model", return_value=None):
        result = _try_lightgbm(candles)
    assert result is None


def test_try_lightgbm_predict_exception_returns_none():
    """A model that raises during predict_proba must not propagate — the
    forecast has other fallbacks (Kronos, ATR heuristic) to try instead."""
    candles = _make_realistic_candles(n=120)
    stub = MagicMock()
    stub.feature_name_ = list(_LGBM_FEATURE_COLUMNS)
    stub.predict_proba = MagicMock(side_effect=RuntimeError("boom"))
    with patch.object(ks, "_load_lightgbm_model", return_value=stub), \
         patch.object(ks, "_fetch_current_fear_greed", return_value=42.0), \
         patch.object(ks, "_fetch_current_funding_rate_bps", return_value=1.5), \
         patch.object(ks, "_fetch_current_book_depth_features", return_value=None), \
         patch.object(ks, "_fetch_current_oi_longshort", return_value=None), \
         patch.object(ks, "_fetch_current_cross_asset", return_value=None):
        result = _try_lightgbm(candles)
    assert result is None


def test_try_lightgbm_external_signal_fetch_failure_uses_neutral_defaults():
    """A failed live fetch (network hiccup) must not veto an otherwise-good
    prediction — every external feature falls back to a neutral default
    rather than NaN, which would trip the warmup guard."""
    candles = _make_realistic_candles(n=120)
    stub = _stub_lightgbm_model([0.1, 0.2, 0.7])
    with patch.object(ks, "_load_lightgbm_model", return_value=stub), \
         patch.object(ks, "_fetch_current_fear_greed", return_value=None), \
         patch.object(ks, "_fetch_current_funding_rate_bps", return_value=None), \
         patch.object(ks, "_fetch_current_book_depth_features", return_value=None), \
         patch.object(ks, "_fetch_current_oi_longshort", return_value=None), \
         patch.object(ks, "_fetch_current_cross_asset", return_value=None):
        result = _try_lightgbm(candles)

    assert result is not None
    assert result.source == "lightgbm"
    call_args = stub.predict_proba.call_args[0][0]
    assert call_args["fear_greed_value"].iloc[0] == ks._FEAR_GREED_NEUTRAL
    assert call_args["funding_rate_bps"].iloc[0] == ks._FUNDING_RATE_NEUTRAL_BPS
    assert call_args["depth_imbalance_1pct"].iloc[0] == ks._DEPTH_IMBALANCE_NEUTRAL
    assert call_args["depth_imbalance_5pct"].iloc[0] == ks._DEPTH_IMBALANCE_NEUTRAL
    assert call_args["depth_total_notional_1pct"].iloc[0] == ks._DEPTH_NOTIONAL_NEUTRAL
    assert call_args["open_interest"].iloc[0] == ks._OPEN_INTEREST_NEUTRAL
    assert call_args["oi_change_1h_pct"].iloc[0] == ks._OI_CHANGE_NEUTRAL_PCT
    assert call_args["toptrader_long_short_ratio"].iloc[0] == ks._LONG_SHORT_RATIO_NEUTRAL
    assert call_args["taker_long_short_vol_ratio"].iloc[0] == ks._LONG_SHORT_RATIO_NEUTRAL
    assert call_args["eth_return_1d_lag"].iloc[0] == ks._ETH_RETURN_NEUTRAL_PCT
    assert call_args["eth_realized_vol_5d_lag"].iloc[0] == ks._ETH_VOL_NEUTRAL_PCT


def test_try_lightgbm_older_model_without_new_features_skips_live_fetches():
    """A model saved before the 2026-08-21 follow-ups (the actual currently-
    deployed lightgbm_volatility_model_v3.pkl shape) must not be handed any
    of those columns — feature_cols comes from the model's own
    feature_name_, not the current global FEATURE_COLUMNS, so this must not
    fetch any of the 5 live external signals or crash on a feature-count
    mismatch."""
    candles = _make_realistic_candles(n=120)
    new_feature_names = {
        "fear_greed_value", "funding_rate_bps",
        "depth_imbalance_1pct", "depth_imbalance_5pct", "depth_total_notional_1pct",
        "open_interest", "oi_change_1h_pct", "toptrader_long_short_ratio", "taker_long_short_vol_ratio",
        "eth_return_1d_lag", "eth_realized_vol_5d_lag",
    }
    old_feature_names = [c for c in _LGBM_FEATURE_COLUMNS if c not in new_feature_names]
    stub = _stub_lightgbm_model([0.2, 0.3, 0.5], feature_names=old_feature_names)
    with patch.object(ks, "_load_lightgbm_model", return_value=stub), \
         patch.object(ks, "_fetch_current_fear_greed", side_effect=AssertionError("should not be called")), \
         patch.object(ks, "_fetch_current_funding_rate_bps", side_effect=AssertionError("should not be called")), \
         patch.object(ks, "_fetch_current_book_depth_features", side_effect=AssertionError("should not be called")), \
         patch.object(ks, "_fetch_current_oi_longshort", side_effect=AssertionError("should not be called")), \
         patch.object(ks, "_fetch_current_cross_asset", side_effect=AssertionError("should not be called")):
        result = _try_lightgbm(candles)

    assert result is not None
    assert result.source == "lightgbm"
    call_args = stub.predict_proba.call_args[0][0]
    for col in new_feature_names:
        assert col not in call_args.columns


# ---------------------------------------------------------------------------
# Fallback tests (mock Kronos failing/timeout/unavailable)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_kronos_model_failure_falls_back_to_heuristic():
    """When _try_kronos raises an exception, forecast must fall back to ATR heuristic."""
    candles = _make_candles(n=500)
    mock_lse = _mock_lse(candles)

    original_timeout = ks.KRONOS_TIMEOUT_SECONDS
    ks.KRONOS_TIMEOUT_SECONDS = 5.0  # short but not instant

    def failing_kronos(_candles):
        raise RuntimeError("Kronos model crashed!")

    with patch.object(ks, "_try_kronos", failing_kronos):
        regime = await forecast_session_regime(lse_client=mock_lse)

    ks.KRONOS_TIMEOUT_SECONDS = original_timeout
    assert regime.source == "heuristic"
    assert regime.regime in ("HIGH", "MED", "LOW")
    # Heuristic ATR should match the daily-equivalent ATR% (raw dollar ATR
    # normalized to pct, then scaled to daily via sqrt(1440))
    raw_atr = _compute_atr(candles, period=14)
    atr_pct = _normalize_atr_to_pct(raw_atr, candles)
    expected_atr = _scale_atr_to_daily(atr_pct)
    assert abs(regime.atr_14d - expected_atr) < 1e-6


@pytest.mark.asyncio
async def test_kronos_timeout_falls_back_to_heuristic():
    """When Kronos exceeds the 30s hard timeout, forecast must fall back to ATR heuristic."""
    candles = _make_candles(n=500)
    mock_lse = _mock_lse(candles)

    original_timeout = ks.KRONOS_TIMEOUT_SECONDS
    ks.KRONOS_TIMEOUT_SECONDS = 0.5  # 500ms timeout

    import time
    def slow_kronos(_candles):
        time.sleep(5)  # way exceeds 500ms
        return None

    with patch.object(ks, "_try_kronos", slow_kronos):
        regime = await forecast_session_regime(lse_client=mock_lse)

    ks.KRONOS_TIMEOUT_SECONDS = original_timeout
    assert regime.source == "heuristic"
    assert regime.regime in ("HIGH", "MED", "LOW")
    # Expected regime is based on daily-equivalent ATR% (the fixed heuristic)
    raw_atr = _compute_atr(candles, period=14)
    atr_pct = _normalize_atr_to_pct(raw_atr, candles)
    expected_atr = _scale_atr_to_daily(atr_pct)
    expected_regime, _ = _regime_from_atr(expected_atr)
    assert regime.regime == expected_regime


@pytest.mark.asyncio
async def test_kronos_returns_none_falls_back_to_heuristic():
    """When _try_kronos returns None (model unavailable/produces invalid output),
    forecast must fall back to ATR heuristic."""
    candles = _make_candles(n=500)
    mock_lse = _mock_lse(candles)

    def none_kronos(_candles):
        return None  # model "available" but returns None

    with patch.object(ks, "_try_kronos", none_kronos):
        regime = await forecast_session_regime(lse_client=mock_lse)

    assert regime.source == "heuristic"
    raw_atr = _compute_atr(candles, period=14)
    atr_pct = _normalize_atr_to_pct(raw_atr, candles)
    expected_atr = _scale_atr_to_daily(atr_pct)
    assert abs(regime.atr_14d - expected_atr) < 1e-6


@pytest.mark.asyncio
async def test_kronos_unavailable_import_error_falls_back():
    """When the Kronos model package cannot be imported, _try_kronos returns None
    and the heuristic takes over."""
    candles = _make_candles(n=500)
    mock_lse = _mock_lse(candles)

    # _try_kronos internally calls _load_kronos_model which tries to import the
    # kronos_model package. Patching _try_kronos to return None simulates this.
    with patch.object(ks, "_try_kronos", return_value=None):
        regime = await forecast_session_regime(lse_client=mock_lse)

    assert regime.source == "heuristic"
    assert regime.regime in ("HIGH", "MED", "LOW")


@pytest.mark.asyncio
async def test_use_kronos_false_skips_kronos():
    """When use_kronos=False, the heuristic is used directly without calling _try_kronos."""
    candles = _make_candles(n=500)
    mock_lse = _mock_lse(candles)

    with patch.object(ks, "_try_kronos", side_effect=Exception("should not be called")):
        regime = await forecast_session_regime(lse_client=mock_lse, use_kronos=False)

    assert regime.source == "heuristic"
    raw_atr = _compute_atr(candles, period=14)
    atr_pct = _normalize_atr_to_pct(raw_atr, candles)
    expected_atr = _scale_atr_to_daily(atr_pct)
    assert abs(regime.atr_14d - expected_atr) < 1e-6


@pytest.mark.asyncio
async def test_no_candles_defaults_to_med():
    """When LSE returns no candles, default to MED regime."""
    mock_lse = _mock_lse([])

    regime = await forecast_session_regime(lse_client=mock_lse, use_kronos=False)

    assert regime.regime == "MED"
    assert regime.source == "default"
    assert regime.no_entry_seconds == 5.0
    assert regime.max_quote_age_ms == 500


@pytest.mark.asyncio
async def test_session_vol_regime_is_set():
    """SESSION_VOL_REGIME module variable must be set after forecast."""
    candles = _make_candles(n=500)
    mock_lse = _mock_lse(candles)

    original = ks.SESSION_VOL_REGIME
    try:
        regime = await forecast_session_regime(lse_client=mock_lse, use_kronos=False)
        assert ks.SESSION_VOL_REGIME == regime.regime
        assert ks.SESSION_VOL_REGIME in ("HIGH", "MED", "LOW")
    finally:
        ks.SESSION_VOL_REGIME = original


@pytest.mark.asyncio
async def test_kronos_success_uses_kronos_source():
    """LightGBM is tried first (2026-08-20 bake-off winner); when it's
    unavailable and _try_kronos returns a valid VolRegime, Kronos is used
    (source='kronos') instead of the heuristic."""
    candles = _make_candles(n=500)
    mock_lse = _mock_lse(candles)

    kronos_result = VolRegime(
        regime="HIGH",
        atr_14d=0.08,
        confidence=0.9,
        no_entry_seconds=10.0,
        max_quote_age_ms=250,
        source="kronos",
    )

    with patch.object(ks, "_try_lightgbm", return_value=None), \
         patch.object(ks, "_try_kronos", return_value=kronos_result):
        regime = await forecast_session_regime(lse_client=mock_lse)

    assert regime.source == "kronos"
    assert regime.regime == "HIGH"
    assert regime.no_entry_seconds == 10.0
    assert regime.max_quote_age_ms == 250


@pytest.mark.asyncio
async def test_lightgbm_success_uses_lightgbm_source_and_skips_kronos():
    """When _try_lightgbm succeeds, its result is used (source='lightgbm') and
    Kronos is never even attempted — LightGBM is the primary source."""
    candles = _make_candles(n=500)
    mock_lse = _mock_lse(candles)

    lightgbm_result = VolRegime(
        regime="LOW",
        atr_14d=0.02,
        confidence=0.7,
        no_entry_seconds=3.0,
        max_quote_age_ms=500,
        source="lightgbm",
    )

    with patch.object(ks, "_try_lightgbm", return_value=lightgbm_result), \
         patch.object(ks, "_try_kronos", side_effect=AssertionError("should not run")):
        regime = await forecast_session_regime(lse_client=mock_lse)

    assert regime.source == "lightgbm"
    assert regime.regime == "LOW"
    assert regime.no_entry_seconds == 3.0
    assert regime.max_quote_age_ms == 500


@pytest.mark.asyncio
async def test_both_ml_sources_unavailable_falls_back_to_heuristic():
    """When both _try_lightgbm and _try_kronos return None, forecast falls
    back to the ATR heuristic (source='heuristic')."""
    candles = _make_candles(n=500)
    mock_lse = _mock_lse(candles)

    with patch.object(ks, "_try_lightgbm", return_value=None), \
         patch.object(ks, "_try_kronos", return_value=None):
        regime = await forecast_session_regime(lse_client=mock_lse)

    assert regime.source == "heuristic"


# ---------------------------------------------------------------------------
# apply_regime_to_settings tests
# ---------------------------------------------------------------------------
def test_apply_regime_to_settings():
    """apply_regime_to_settings should update SETTINGS fields."""
    regime = VolRegime(
        regime="HIGH",
        atr_14d=0.08,
        confidence=0.9,
        no_entry_seconds=10.0,
        max_quote_age_ms=250,
        source="kronos",
    )
    original_no_entry = SETTINGS.no_entry_seconds
    original_max_age = SETTINGS.max_quote_age_ms
    try:
        apply_regime_to_settings(regime)
        assert SETTINGS.no_entry_seconds == 10.0
        assert SETTINGS.max_quote_age_ms == 250
    finally:
        object.__setattr__(SETTINGS, "no_entry_seconds", original_no_entry)
        object.__setattr__(SETTINGS, "max_quote_age_ms", original_max_age)


# ---------------------------------------------------------------------------
# Integration: full forecast flow with mocked Kronos returning a result
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_full_forecast_flow_high_vol():
    """High vol regime produces HIGH params."""
    # Generate high-volatility candles
    rng = np.random.RandomState(42)
    n = 500
    prices = 100 + np.cumsum(rng.randn(n) * 5.0)  # high volatility
    candles = []
    for i in range(n):
        h = prices[i] + abs(rng.randn()) * 5.0
        l = prices[i] - abs(rng.randn()) * 5.0
        c = (h + l + prices[i]) / 3
        candles.append({
            "timestamp": i,
            "open": float(prices[i]),
            "high": float(h),
            "low": float(l),
            "close": float(c),
            "volume": float(abs(rng.randn()) * 100),
        })
    mock_lse = _mock_lse(candles)

    regime = await forecast_session_regime(lse_client=mock_lse, use_kronos=False)
    assert regime.regime == "HIGH"
    assert regime.no_entry_seconds == 7.0
    assert regime.max_quote_age_ms == 400


@pytest.mark.asyncio
async def test_full_forecast_flow_low_vol():
    """Low volatility regime produces LOW params."""
    rng = np.random.RandomState(42)
    n = 500
    prices = 100 + np.cumsum(rng.randn(n) * 0.001)  # very low volatility
    candles = []
    for i in range(n):
        h = prices[i] + abs(rng.randn()) * 0.005
        l = prices[i] - abs(rng.randn()) * 0.005
        c = (h + l + prices[i]) / 3
        candles.append({
            "timestamp": i,
            "open": float(prices[i]),
            "high": float(h),
            "low": float(l),
            "close": float(c),
            "volume": float(abs(rng.randn()) * 10),
        })
    mock_lse = _mock_lse(candles)

    regime = await forecast_session_regime(lse_client=mock_lse, use_kronos=False)
    assert regime.regime == "LOW"
    assert regime.no_entry_seconds == 3.0
    assert regime.max_quote_age_ms == 500


# ---------------------------------------------------------------------------
# Scale-fix validation: 1-minute ATR is normalized + scaled to daily-equivalent
# before threshold comparison.  Verify the normalization helpers and that a
# moderate-volatility 1-min session produces a believable MED regime (not
# always LOW as the old bug caused).
# ---------------------------------------------------------------------------
def test_normalize_atr_to_pct():
    """_normalize_atr_to_pct converts dollar ATR to a fraction of mean close."""
    candles = [{"close": 100.0}, {"close": 100.0}, {"close": 100.0}]
    assert _normalize_atr_to_pct(5.0, candles) == pytest.approx(0.05)
    assert _normalize_atr_to_pct(0.0, candles) == 0.0
    assert _normalize_atr_to_pct(1.0, []) == 0.0


def test_scale_atr_to_daily():
    """_scale_atr_to_daily applies sqrt(1440) scaling."""
    # 0.001 (0.1%) 1-min ATR -> daily-equiv = 0.001 * sqrt(1440)
    expected = 0.001 * ATR_TIME_SCALING_FACTOR
    assert _scale_atr_to_daily(0.001) == pytest.approx(expected)
    assert _scale_atr_to_daily(0.0) == 0.0


def test_scale_atr_to_daily_known_factor():
    """Sanity: sqrt(1440) ~= 37.95."""
    assert ATR_TIME_SCALING_FACTOR == pytest.approx((1440 ** 0.5), rel=1e-6)
    assert 37.9 < ATR_TIME_SCALING_FACTOR < 38.0


@pytest.mark.asyncio
async def test_one_min_med_vol_produces_med_regime():
    """A moderate-volatility 1-minute BTC session should classify as MED.

    Before the scale fix, raw dollar ATR (~$20 at $65k) was compared to the
    0.05 percentage threshold, yielding ALWAYS HIGH regardless of actual vol.
    After the fix, 1-min ATR% is normalized and scaled to daily-equivalent,
    so a moderate session produces MED rather than always HIGH.
    """
    rng = np.random.RandomState(99)
    n = 1500
    base_price = 65000.0
    # ~0.05% per-minute noise -> moderate volatility (daily-equiv ATR% ~3.8%)
    vol_pct = 0.0005
    prices = base_price + np.cumsum(rng.randn(n) * vol_pct * base_price)
    candles = []
    for i in range(n):
        noise = abs(rng.randn()) * vol_pct * base_price
        candles.append({
            "timestamp": i,
            "open": float(prices[i]),
            "high": float(prices[i] + noise),
            "low": float(prices[i] - noise),
            "close": float(prices[i]),
            "volume": 100.0,
        })
    mock_lse = _mock_lse(candles)

    regime = await forecast_session_regime(lse_client=mock_lse, use_kronos=False)

    # With ~0.1% 1-min ATR%, daily-equiv ~= 0.1% * 37.95 ~= 3.8% -> MED
    assert regime.regime == "MED"
    assert regime.source == "heuristic"
    assert regime.no_entry_seconds == 5.0
    assert regime.max_quote_age_ms == 500


@pytest.mark.asyncio
async def test_one_min_btc_vol_mixed_regimes():
    """Different volatility levels at $65k price should produce different regimes."""
    base_price = 65000.0
    n = 1500

    for vol_pct, expected_regime in [
        (0.00003, "LOW"),    # ~0.003% per-min -> daily-equiv ~0.24% -> LOW
        (0.0005,  "MED"),    # ~0.05% per-min -> daily-equiv ~3.8% -> MED
        (0.001,   "HIGH"),   # ~0.1% per-min -> daily-equiv ~7.9% -> HIGH
    ]:
        rng = np.random.RandomState(42)
        prices = base_price + np.cumsum(rng.randn(n) * vol_pct * base_price)
        candles = []
        for i in range(n):
            noise = abs(rng.randn()) * vol_pct * base_price
            candles.append({
                "timestamp": i,
                "open": float(prices[i]),
                "high": float(prices[i] + noise),
                "low": float(prices[i] - noise),
                "close": float(prices[i]),
                "volume": 100.0,
            })
        mock_lse = _mock_lse(candles)
        regime = await forecast_session_regime(lse_client=mock_lse, use_kronos=False)
        assert regime.regime == expected_regime, (
            f"vol_pct={vol_pct} -> expected {expected_regime}, got {regime.regime} "
            f"(daily-equiv ATR%={regime.atr_14d:.4f})"
        )


# ---------------------------------------------------------------------------
# Sparse/zero-ATR data guard (TASK 3): zero ATR must NOT fabricate a LOW regime
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_sparse_candles_default_to_conservative_med():
    """Too few candles for an ATR (period 14) must yield conservative MED, not LOW.

    Before the guard, _regime_from_atr(0.0) produced LOW (3.0s no-entry zone)
    for a market whose volatility was UNKNOWN — the permissive direction.
    """
    candles = _make_candles(n=5)  # < period + 1 -> _compute_atr returns 0.0
    mock_lse = _mock_lse(candles)

    regime = await forecast_session_regime(lse_client=mock_lse, use_kronos=False)

    assert regime.regime == "MED"
    assert regime.source == "sparse"
    assert regime.no_entry_seconds == 5.0
    assert regime.max_quote_age_ms == 500
    assert regime.atr_14d == 0.0
    assert ks.SESSION_VOL_REGIME == "MED"


@pytest.mark.asyncio
async def test_flat_candles_default_to_conservative_med():
    """A long but zero-range candle series (ATR exactly 0.0) must also yield
    conservative MED, not a LOW regime with a loose 3s no-entry zone."""
    candles = []
    for i in range(200):
        candles.append({
            "timestamp": i,
            "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0,
            "volume": 100.0,
        })
    assert _compute_atr(candles, period=14) == 0.0
    mock_lse = _mock_lse(candles)

    regime = await forecast_session_regime(lse_client=mock_lse, use_kronos=False)

    assert regime.regime == "MED"
    assert regime.source == "sparse"
    assert regime.no_entry_seconds == 5.0
    assert regime.max_quote_age_ms == 500


@pytest.mark.asyncio
async def test_sparse_guard_logs_loudly():
    """The sparse-data default must be visible in the log, not silent."""
    candles = _make_candles(n=5)
    mock_lse = _mock_lse(candles)

    with patch.object(ks.log, "warning") as mock_warning:
        await forecast_session_regime(lse_client=mock_lse, use_kronos=False)

    assert any("zero ATR" in str(c.args[0]) for c in mock_warning.call_args_list)


@pytest.mark.asyncio
async def test_sparse_guard_skips_kronos_inference():
    """Sparse data must not be sent to Kronos inference — conservative MED is
    decided purely from the failed ATR heuristic."""
    candles = _make_candles(n=5)
    mock_lse = _mock_lse(candles)

    with patch.object(ks, "_try_kronos", side_effect=AssertionError("should not run")):
        regime = await forecast_session_regime(lse_client=mock_lse, use_kronos=True)

    assert regime.regime == "MED"
    assert regime.source == "sparse"
