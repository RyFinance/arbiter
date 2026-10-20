"""Session volatility regime forecasting — COLD PATH ONLY.

Runs once at startup to forecast the BTC volatility regime for the upcoming
session. The regime adjusts window_tracker.py parameters (see _regime_params):

  HIGH -> NO_ENTRY_SECONDS = 7.0, MAX_QUOTE_AGE_MS = 400
  MED  -> NO_ENTRY_SECONDS = 5.0  (current default), MAX_QUOTE_AGE_MS = 500
  LOW  -> NO_ENTRY_SECONDS = 3.0, MAX_QUOTE_AGE_MS = 500

This is COLD PATH ONLY — never runs in the trading loop. Called once in
main.py after cache.load() and before starting the hot path.

Three regime sources are tried in priority order, each falling back to the
next on failure/timeout/unavailability (see forecast_session_regime):

  1. LightGBM (_try_lightgbm) — the 2026-08-20 bake-off winner. Classifies
     the current feature row directly into LOW/MED/HIGH using the same
     tercile cutoffs on forward_realized_vol_bps it was trained with (see
     scripts/fill_and_test_arb_v3.py and ML_REGIME_BAKEOFF_2026-08-20.md).
     67.51% mean accuracy across 3 independent time-disjoint real holdouts.
  2. Kronos-small (_try_kronos) — NeoQuasar/Kronos-small (24.7M params,
     https://github.com/shiyu-coder/Kronos), a pretrained time-series
     foundation model used zero-shot: forecasts 60min of candles, derives a
     predicted ATR from the forecast, classifies via the same ATR
     thresholds as the heuristic below. Bake-off runner-up (documented
     ~0.55 match rate) — kept as a fallback, not primary.
  3. ATR heuristic (_regime_from_atr) — always computed as the baseline
     even when an ML source succeeds; the ultimate fallback when neither
     model is available.

Each source has a KRONOS_TIMEOUT_SECONDS (30s) hard timeout on its full
load+inference path.

The module-level SESSION_VOL_REGIME variable is set to "HIGH" | "MED" | "LOW"
after forecasting completes. window_tracker.py consumes the regime indirectly
via SETTINGS.no_entry_seconds and SETTINGS.max_quote_age_ms (applied via
apply_regime_to_settings), so its consumption is unchanged.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple

from .clients.lse_client import LSEClient
from .config import SETTINGS
from .logger import log

# ---- Kronos configuration ----
# Use Kronos-small specifically (NOT base or large) — cold-path only, CPU inference.
KRONOS_MODEL_NAME = "NeoQuasar/Kronos-small"
KRONOS_TOKENIZER_NAME = "NeoQuasar/Kronos-Tokenizer-base"
# Local cache directory for downloaded model files.
# __file__ is .../polymarket_arb/kronos_session.py; dirname x2 = project root.
KRONOS_CACHE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "state", "kronos",
)
KRONOS_MODEL_LOCAL = os.path.join(KRONOS_CACHE_DIR, "kronos-small")
KRONOS_TOKENIZER_LOCAL = os.path.join(KRONOS_CACHE_DIR, "kronos-tokenizer-base")

# ---- LightGBM regime classifier ----
# 2026-08-20 bake-off winner: retrained on 845,516 real BTC/USD 1m rows
# (2025-01-01 -> 2026-08-11), 67.51% mean accuracy across 3 independent
# time-disjoint holdouts (all beating majority baseline) vs the from-scratch
# transformer classifier's 53.82% on the same real data and Kronos-small's
# documented ~0.55 match rate. See ML_REGIME_BAKEOFF_2026-08-20.md.
# Module attributes (not locals) so tests can monkeypatch them per-test.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(__file__))
LIGHTGBM_MODEL_PATH = os.path.join(_PROJECT_ROOT, "state", "lightgbm_volatility_model_v3.pkl")
MACRO_EVENTS_PATH = os.path.join(_PROJECT_ROOT, "state", "macro_events.json")
LIGHTGBM_SCRIPTS_DIR = os.path.join(_PROJECT_ROOT, "scripts")

# Hard timeout for model loading AND inference combined. If exceeded, fall back
# to the ATR heuristic. Set to 30 seconds per the spec.
KRONOS_TIMEOUT_SECONDS = 30.0

# Kronos inference config — CPU-only, small sample_count for speed.
KRONOS_MAX_CONTEXT = 512
KRONOS_PRED_LEN = 60  # predict 60 minutes ahead (1h) at 1m candles


# Module-level singleton: the loaded Kronos model + predictor.
# Loaded once at startup (cold path only). None until first load.
_kronos_predictor: Optional[Any] = None
_kronos_load_time: Optional[float] = None
_kronos_inference_time: Optional[float] = None

# Module-level singleton: the loaded LightGBM regime classifier.
_lightgbm_model: Optional[Any] = None

# Module-level output: set after forecast_session_regime() completes.
# window_tracker.py does NOT read this directly — it reads SETTINGS fields
# applied via apply_regime_to_settings(). This variable is provided for
# observability and external consumers (e.g. skill_router.py).
SESSION_VOL_REGIME: str = "MED"  # default before forecast runs


@dataclass
class VolRegime:
    """Volatility regime for the upcoming session."""
    regime: str  # "HIGH" | "MED" | "LOW"
    atr_14d: float  # daily-equivalent ATR% (1-min ATR scaled to daily via sqrt(1440))
    confidence: float  # 0.0 to 1.0
    no_entry_seconds: float
    max_quote_age_ms: int
    source: str  # "kronos" or "heuristic"

    def to_log(self) -> dict:
        return {
            "regime": self.regime,
            "atr_14d": round(self.atr_14d, 4),
            "confidence": round(self.confidence, 4),
            "no_entry_seconds": self.no_entry_seconds,
            "max_quote_age_ms": self.max_quote_age_ms,
            "source": self.source,
        }


# ---------------------------------------------------------------------------
# ATR Scale-Fix: Daily-Equivalent Normalization for 1-Minute Candles
# ---------------------------------------------------------------------------
# Kronos operates on 1-minute candles (per the original design spec), but the
# ATR thresholds below are calibrated for DAILY ATR% (5% daily range = HIGH,
# 3% daily range = LOW/MED boundary).  Without correction, the 1-minute ATR is always
# orders of magnitude smaller than the daily thresholds, causing the heuristic
# to classify every 1-minute window as LOW.
#
# Fix: normalize the 1-minute ATR to a daily-equivalent scale using the
# square-root-of-time rule:  sigma_daily = sigma_1min * sqrt(periods_per_day)
# With 1440 minutes per day, scaling factor = sqrt(1440) ~= 37.95.
# After scaling, the calibrated HIGH=0.05 / LOW=0.03 thresholds apply directly,
# preserving their original daily-regime semantics.
#
# This applies uniformly to:
#   * Heuristic path -- raw 1-min dollar ATR is first normalized to a
#     percentage (via _normalize_atr_to_pct), then scaled to daily-equivalent.
#   * Kronos path     -- Kronos already outputs a normalized percentage ATR
#     from the forecast; that percentage is scaled to daily-equivalent.
#
# Historical daily ATR% calibration (ground truth the thresholds were tuned on):
#   HIGH: > 5%  daily ATR%
#   MED:  3-5%  daily ATR%
#   LOW:  < 3%  daily ATR%
ATR_HIGH_THRESHOLD = 0.05
ATR_LOW_THRESHOLD = 0.03

# Minutes per day (1440).  Used for sqrt(time-scaling) normalization.
MINUTES_PER_DAY = 1440
# Daily-equivalent scaling factor = sqrt(MINUTES_PER_DAY) ~= 37.947
ATR_TIME_SCALING_FACTOR = (MINUTES_PER_DAY ** 0.5)


def _normalize_atr_to_pct(atr: float, candles: List[Dict[str, Any]]) -> float:
    """Convert a dollar-value ATR to a percentage of mean price.

    Args:
        atr: Raw dollar ATR (difference in price units).
        candles: Candles used to compute the ATR (for price reference).

    Returns:
        ATR as a fraction of mean close (e.g. 0.003 = 0.3%).
    """
    if not candles or atr <= 0:
        return 0.0
    closes = [float(c.get("close", 0)) for c in candles if c.get("close", 0) > 0]
    if not closes:
        return 0.0
    mean_close = sum(closes) / len(closes)
    if mean_close == 0:
        return 0.0
    return atr / mean_close


def _scale_atr_to_daily(atr_pct: float) -> float:
    """Scale a per-minute ATR (as a percentage) to a daily-equivalent ATR%.

    Uses the square-root-of-time rule: sigma_daily = sigma_1min * sqrt(N),
    where N = minutes per day (1440).  This lets the daily-calibrated
    thresholds (0.05 HIGH, 0.03 LOW/MED) be applied to 1-minute ATR values.
    """
    return atr_pct * ATR_TIME_SCALING_FACTOR


def _compute_atr(candles: List[Dict[str, Any]], period: int = 14) -> float:
    """Compute the Average True Range from OHLCV candles.

    ATR measures market volatility by decomposing the entire range of an asset
    price over the specified period.
    """
    if len(candles) < period + 1:
        return 0.0

    tr_values = []
    for i in range(1, len(candles)):
        high = float(candles[i].get("high", 0))
        low = float(candles[i].get("low", 0))
        prev_close = float(candles[i - 1].get("close", 0))

        if high == 0 or low == 0 or prev_close == 0:
            continue

        # True Range = max of:
        # 1. Current high - current low
        # 2. |current high - previous close|
        # 3. |current low - previous close|
        tr1 = high - low
        tr2 = abs(high - prev_close)
        tr3 = abs(low - prev_close)
        tr = max(tr1, tr2, tr3)
        tr_values.append(tr)

    if len(tr_values) < period:
        return 0.0

    # ATR = SMA of True Range over the period
    recent_tr = tr_values[-period:]
    return sum(recent_tr) / len(recent_tr)


def _regime_from_atr(atr: float) -> Tuple[str, float]:
    """Map ATR value to a volatility regime and confidence.

    Returns (regime, confidence) where confidence is 0.0-1.0.
    """
    if atr >= ATR_HIGH_THRESHOLD:
        # High volatility — confidence increases with ATR
        confidence = min(1.0, (atr - ATR_HIGH_THRESHOLD) / 0.10 + 0.5)
        return "HIGH", confidence
    elif atr >= ATR_LOW_THRESHOLD:
        # Medium volatility
        mid = (ATR_HIGH_THRESHOLD + ATR_LOW_THRESHOLD) / 2
        confidence = 0.5 + 0.5 * abs(atr - mid) / (mid - ATR_LOW_THRESHOLD)
        return "MED", min(1.0, confidence)
    else:
        # Low volatility
        confidence = min(1.0, (ATR_LOW_THRESHOLD - atr) / ATR_LOW_THRESHOLD + 0.5)
        return "LOW", confidence


def _regime_params(regime: str) -> Tuple[float, int]:
    """Get NO_ENTRY_SECONDS and MAX_QUOTE_AGE_MS for a regime.

    HIGH moderated 2026-08-20: STATUS.md L3 documents Kronos's HIGH call
    matching realized vol only 55% of the time, biased toward over-predicting
    HIGH. At the old 10.0s/250ms, every false HIGH call needlessly doubled the
    no-entry blackout and halved the quote-freshness budget versus MED — on
    top of the fact that HIGH is only marginally better than a coin flip.
    Moved closer to MED (still the most conservative regime, just not as
    punitively so given the weak signal backing it).
    """
    if regime == "HIGH":
        return 7.0, 400
    elif regime == "LOW":
        # Keep LOW's shorter no-entry zone, but do not loosen quote freshness:
        # paper samples showed >500ms quotes produced stale bursts, not fills.
        return 3.0, 500
    else:  # MED
        return 5.0, 500


def _candles_to_dataframe(candles: List[Dict[str, Any]]) -> Optional[Any]:
    """Convert raw candle dicts from LSE into a pandas DataFrame for Kronos.

    Kronos expects columns ['open', 'high', 'low', 'close'] and optionally
    'volume'. LSE returns dicts with keys like 'timestamp', 'open', 'high',
    'low', 'close', 'volume'.
    """
    import pandas as pd

    if not candles:
        return None

    rows = []
    for c in candles:
        rows.append({
            "open": float(c.get("open", 0)),
            "high": float(c.get("high", 0)),
            "low": float(c.get("low", 0)),
            "close": float(c.get("close", 0)),
            "volume": float(c.get("volume", 0)),
        })

    df = pd.DataFrame(rows)
    if len(df) < 2:
        return None
    return df


def _predict_atr_from_forecast(pred_df: Any) -> float:
    """Compute a predicted 1-minute ATR% from Kronos forecast candles.

    We take the forecast OHLCV (1-minute candles), compute the True Range over
    the prediction horizon, and return the average True Range normalized by
    mean close — i.e. a *1-minute percentage ATR* (e.g. 0.0003 = 0.03%).

    The caller (_try_kronos) scales this to a daily-equivalent ATR% via
    _scale_atr_to_daily before applying the daily-calibrated thresholds.
    """
    if pred_df is None or len(pred_df) < 2:
        return 0.0

    highs = pred_df["high"].values
    lows = pred_df["low"].values
    closes = pred_df["close"].values

    tr_values = []
    for i in range(1, len(highs)):
        h, l = float(highs[i]), float(lows[i])
        pc = float(closes[i - 1])
        if h == 0 or l == 0 or pc == 0:
            continue
        tr1 = h - l
        tr2 = abs(h - pc)
        tr3 = abs(l - pc)
        tr = max(tr1, tr2, tr3)
        tr_values.append(tr)

    if not tr_values:
        return 0.0

    # Return mean TR normalized by mean close -> 1-minute percentage ATR.
    # The caller scales this to a daily-equivalent via _scale_atr_to_daily
    # before applying the daily-calibrated thresholds.
    mean_close = float(closes.mean())
    if mean_close == 0:
        return 0.0
    mean_tr = sum(tr_values) / len(tr_values)
    return mean_tr / mean_close


def _load_kronos_model() -> Optional[Any]:
    """Load the Kronos model + tokenizer once. Returns a KronosPredictor or None.

    Uses Kronos-small from local HF cache (downloaded separately). Falls back
    to HuggingFace Hub download if local files are missing.
    """
    global _kronos_predictor, _kronos_load_time

    if _kronos_predictor is not None:
        return _kronos_predictor

    try:
        from polymarket_arb.kronos_model import Kronos, KronosTokenizer, KronosPredictor
        import torch
    except ImportError as e:
        log.info(f"Kronos model package not available — using ATR heuristic: {e}")
        return None

    t0 = time.time()
    try:
        # Prefer local cache; fall back to HF Hub download
        tokenizer_path = (
            KRONOS_TOKENIZER_LOCAL
            if os.path.isdir(KRONOS_TOKENIZER_LOCAL)
            else KRONOS_TOKENIZER_NAME
        )
        model_path = (
            KRONOS_MODEL_LOCAL
            if os.path.isdir(KRONOS_MODEL_LOCAL)
            else KRONOS_MODEL_NAME
        )

        tokenizer = KronosTokenizer.from_pretrained(tokenizer_path)
        model = Kronos.from_pretrained(model_path)
        model.eval()

        # CPU-only inference (no GPU needed for cold path)
        predictor = KronosPredictor(model, tokenizer, device="cpu", max_context=KRONOS_MAX_CONTEXT)

        _kronos_load_time = time.time() - t0
        _kronos_predictor = predictor
        log.info(f"Kronos-small model loaded in {_kronos_load_time:.2f}s (CPU)")
        return predictor

    except Exception as e:
        _kronos_load_time = time.time() - t0
        log.warning(f"Kronos model load failed in {_kronos_load_time:.2f}s: {e!r} — falling back to ATR heuristic")
        return None


def _try_kronos(candles: List[Dict[str, Any]]) -> Optional[VolRegime]:
    """Try to use the Kronos model for regime forecasting.

    Uses Kronos-small to forecast the next 60 minutes of BTC/USD 1m candles,
    then derives a predicted ATR from the forecast and classifies it into
    HIGH/MED/LOW using the same ATR thresholds as the heuristic.

    Returns None if Kronos is unavailable — caller falls back to heuristic.
    """
    global _kronos_inference_time

    predictor = _load_kronos_model()
    if predictor is None:
        return None

    df = _candles_to_dataframe(candles)
    if df is None:
        return None

    # Kronos max_context is 512 — truncate input if needed (take most recent).
    n_input = min(len(df), KRONOS_MAX_CONTEXT)
    x_df = df.tail(n_input).reset_index(drop=True)

    # Build timestamps for prediction
    from pandas import Series, date_range
    x_timestamp = Series(date_range("2026-01-01", periods=n_input, freq="1min"))
    y_timestamp = Series(date_range(
        "2026-01-01", periods=n_input + KRONOS_PRED_LEN, freq="1min"
    )[n_input:])

    t0 = time.time()
    try:
        pred_df = predictor.predict(
            x_df, x_timestamp, y_timestamp,
            pred_len=KRONOS_PRED_LEN,
            T=1.0, top_k=0, top_p=0.9,
            sample_count=1, verbose=False,
        )
        _kronos_inference_time = time.time() - t0
        log.info(f"Kronos inference completed in {_kronos_inference_time:.2f}s")

        # Derive predicted ATR from forecast.
        # _predict_atr_from_forecast returns a *1-minute* percentage ATR
        # (normalized by mean close).  Scale it to daily-equivalent so the
        # daily-calibrated thresholds (0.05 HIGH, 0.03 LOW/MED) apply correctly.
        pred_atr_pct = _predict_atr_from_forecast(pred_df)
        if pred_atr_pct <= 0:
            log.warning("Kronos forecast produced invalid ATR (0) — falling back to heuristic")
            return None

        pred_atr_scaled = _scale_atr_to_daily(pred_atr_pct)
        regime, confidence = _regime_from_atr(pred_atr_scaled)
        no_entry, max_age = _regime_params(regime)

        log.info(f"Kronos forecast: regime={regime}, pred_atr%={pred_atr_pct:.6f} "
                 f"(daily-equiv={pred_atr_scaled:.4f}), confidence={confidence:.2f}")

        return VolRegime(
            regime=regime,
            atr_14d=pred_atr_scaled,
            confidence=confidence,
            no_entry_seconds=no_entry,
            max_quote_age_ms=max_age,
            source="kronos",
        )

    except Exception as e:
        _kronos_inference_time = time.time() - t0
        log.warning(f"Kronos inference failed in {_kronos_inference_time:.2f}s: {e!r} — falling back to ATR heuristic")
        return None


def _load_lightgbm_model() -> Optional[Any]:
    """Load the trained LightGBM regime classifier once. Returns the fitted
    estimator (has .predict_proba) or None if the artifact/joblib is missing.
    """
    global _lightgbm_model

    if _lightgbm_model is not None:
        return _lightgbm_model
    if not os.path.isfile(LIGHTGBM_MODEL_PATH):
        return None
    try:
        import joblib
    except ImportError as e:
        log.info(f"joblib not available — LightGBM regime forecaster disabled: {e}")
        return None
    try:
        _lightgbm_model = joblib.load(LIGHTGBM_MODEL_PATH)
        log.info(f"LightGBM regime model loaded from {LIGHTGBM_MODEL_PATH}")
        return _lightgbm_model
    except Exception as e:
        log.warning(f"LightGBM model load failed: {e!r} — falling back")
        return None


FEAR_GREED_LIVE_URL = "https://api.alternative.me/fng/"
BINANCE_FUNDING_LIVE_URL = "https://fapi.binance.com/fapi/v1/fundingRate"
# Neutral fallbacks when a live fetch fails — "no signal" rather than a
# guess, so a transient network hiccup on a secondary feature doesn't reject
# an otherwise-good prediction (the NaN-guard below only exists to catch
# genuinely insufficient OHLCV warmup, not flaky external APIs).
_FEAR_GREED_NEUTRAL = 50.0
_FUNDING_RATE_NEUTRAL_BPS = 0.0
# Neutral defaults for the 2026-08-21 follow-up 3 features (book depth /
# OI-longshort / cross-asset). Ratios default to 1.0 (perfectly balanced
# long/short) and changes/returns to 0.0 (no move) since those are genuinely
# meaningful "no signal" values; the two magnitude features (total notional,
# raw OI level) have no such natural neutral point, so 0.0 is a pragmatic
# fallback for what should be a rare failure path, not a claim that 0 is
# typical.
_DEPTH_IMBALANCE_NEUTRAL = 0.0
_DEPTH_NOTIONAL_NEUTRAL = 0.0
_OPEN_INTEREST_NEUTRAL = 0.0
_OI_CHANGE_NEUTRAL_PCT = 0.0
_LONG_SHORT_RATIO_NEUTRAL = 1.0
_ETH_RETURN_NEUTRAL_PCT = 0.0
_ETH_VOL_NEUTRAL_PCT = 0.0


def _fetch_current_fear_greed() -> Optional[float]:
    """Latest Fear & Greed Index value (0-100). None on any failure."""
    try:
        import httpx

        resp = httpx.get(FEAR_GREED_LIVE_URL, params={"limit": 1}, timeout=5.0)
        resp.raise_for_status()
        data = resp.json().get("data", [])
        return float(data[0]["value"]) if data else None
    except Exception as e:
        log.debug(f"fear & greed live fetch failed: {e!r}")
        return None


def _fetch_current_funding_rate_bps() -> Optional[float]:
    """Latest BTCUSDT funding rate, in bps. None on any failure."""
    try:
        import httpx

        resp = httpx.get(
            BINANCE_FUNDING_LIVE_URL, params={"symbol": "BTCUSDT", "limit": 1}, timeout=5.0
        )
        resp.raise_for_status()
        data = resp.json()
        return float(data[-1]["fundingRate"]) * 10000.0 if data else None
    except Exception as e:
        log.debug(f"funding rate live fetch failed: {e!r}")
        return None


BINANCE_DEPTH_LIVE_URL = "https://fapi.binance.com/fapi/v1/depth"
BINANCE_OI_HIST_LIVE_URL = "https://fapi.binance.com/futures/data/openInterestHist"
BINANCE_TOPTRADER_RATIO_LIVE_URL = "https://fapi.binance.com/futures/data/topLongShortPositionRatio"
BINANCE_TAKER_RATIO_LIVE_URL = "https://fapi.binance.com/futures/data/takerlongshortRatio"


def _fetch_current_book_depth_features() -> Optional[Dict[str, float]]:
    """Order-book depth imbalance from a live snapshot (unlike training,
    live inference only needs the current book, not history — the live REST
    endpoint that follow-up 2 correctly rejected for *training* purposes is
    exactly right here).

    Mirrors load_binance_bookdepth's definitions as closely as a live L2
    snapshot allows: cumulative base-asset quantity from mid price out to
    +/-1%/5%, and total USD notional within +/-1%. None on any failure.
    """
    try:
        import httpx

        resp = httpx.get(BINANCE_DEPTH_LIVE_URL, params={"symbol": "BTCUSDT", "limit": 500}, timeout=5.0)
        resp.raise_for_status()
        data = resp.json()
        bids = [(float(p), float(q)) for p, q in data.get("bids", [])]
        asks = [(float(p), float(q)) for p, q in data.get("asks", [])]
        if not bids or not asks:
            return None
        mid = (bids[0][0] + asks[0][0]) / 2.0

        def _cum(levels, limit_price, below):
            qty = notional = 0.0
            for price, size in levels:
                if (below and price < limit_price) or (not below and price > limit_price):
                    break
                qty += size
                notional += price * size
            return qty, notional

        bid_1pct_qty, bid_1pct_notional = _cum(bids, mid * 0.99, below=True)
        ask_1pct_qty, ask_1pct_notional = _cum(asks, mid * 1.01, below=False)
        bid_5pct_qty, _ = _cum(bids, mid * 0.95, below=True)
        ask_5pct_qty, _ = _cum(asks, mid * 1.05, below=False)

        imbalance_1pct = (
            (bid_1pct_qty - ask_1pct_qty) / (bid_1pct_qty + ask_1pct_qty)
            if (bid_1pct_qty + ask_1pct_qty) else None
        )
        imbalance_5pct = (
            (bid_5pct_qty - ask_5pct_qty) / (bid_5pct_qty + ask_5pct_qty)
            if (bid_5pct_qty + ask_5pct_qty) else None
        )
        if imbalance_1pct is None or imbalance_5pct is None:
            return None
        return {
            "depth_imbalance_1pct": imbalance_1pct,
            "depth_imbalance_5pct": imbalance_5pct,
            "depth_total_notional_1pct": bid_1pct_notional + ask_1pct_notional,
        }
    except Exception as e:
        log.debug(f"book depth live fetch failed: {e!r}")
        return None


def _fetch_current_oi_longshort() -> Optional[Dict[str, float]]:
    """Open interest (+ its 1h change) and long/short positioning, live.

    openInterestHist at 5m period, ~13 rows, gives both the current value
    and a real ~1h-ago comparison point in one call (this endpoint's ~30-day
    history cap, which correctly ruled it out for *training*, is irrelevant
    here — live inference only ever needs the last hour).
    """
    try:
        import httpx

        resp = httpx.get(
            BINANCE_OI_HIST_LIVE_URL,
            params={"symbol": "BTCUSDT", "period": "5m", "limit": 13},
            timeout=5.0,
        )
        resp.raise_for_status()
        oi_rows = resp.json()
        if not oi_rows:
            return None
        oi_rows.sort(key=lambda r: r["timestamp"])
        current_oi = float(oi_rows[-1]["sumOpenInterest"])
        oi_1h_ago = float(oi_rows[0]["sumOpenInterest"])
        oi_change_1h_pct = (current_oi - oi_1h_ago) / oi_1h_ago * 100.0 if oi_1h_ago else None

        resp = httpx.get(
            BINANCE_TOPTRADER_RATIO_LIVE_URL,
            params={"symbol": "BTCUSDT", "period": "5m", "limit": 1},
            timeout=5.0,
        )
        resp.raise_for_status()
        toptrader_rows = resp.json()
        toptrader_ratio = float(toptrader_rows[0]["longShortRatio"]) if toptrader_rows else None

        resp = httpx.get(
            BINANCE_TAKER_RATIO_LIVE_URL,
            params={"symbol": "BTCUSDT", "period": "5m", "limit": 1},
            timeout=5.0,
        )
        resp.raise_for_status()
        taker_rows = resp.json()
        taker_ratio = float(taker_rows[0]["buySellRatio"]) if taker_rows else None

        if oi_change_1h_pct is None or toptrader_ratio is None or taker_ratio is None:
            return None
        return {
            "open_interest": current_oi,
            "oi_change_1h_pct": oi_change_1h_pct,
            "toptrader_long_short_ratio": toptrader_ratio,
            "taker_long_short_vol_ratio": taker_ratio,
        }
    except Exception as e:
        log.debug(f"OI/long-short live fetch failed: {e!r}")
        return None


def _fetch_current_cross_asset() -> Optional[Dict[str, float]]:
    """ETH-USD 1-day return + 5-day realized vol from the last ~10 real days,
    via yfinance (already a project dependency). None on any failure."""
    try:
        import numpy as _np
        import yfinance as yf

        hist = yf.download("ETH-USD", period="10d", progress=False, auto_adjust=True)
        if hist.empty or len(hist) < 6:
            return None
        closes = hist["Close"]
        if hasattr(closes, "columns"):  # MultiIndex columns edge case
            closes = closes.iloc[:, 0]
        log_returns = _np.log(closes).diff().dropna()
        if len(log_returns) < 5:
            return None
        return {
            "eth_return_1d_lag": float(log_returns.iloc[-1]) * 100.0,
            "eth_realized_vol_5d_lag": float(log_returns.tail(5).std()) * 100.0,
        }
    except Exception as e:
        log.debug(f"cross-asset live fetch failed: {e!r}")
        return None


def _try_lightgbm(candles: List[Dict[str, Any]]) -> Optional[VolRegime]:
    """Try the LightGBM regime classifier — the 2026-08-20 bake-off winner.

    Unlike Kronos (which forecasts candles and derives a regime from the
    predicted ATR), LightGBM classifies the current feature row directly
    into LOW/MED/HIGH using the same tercile cutoffs on
    forward_realized_vol_bps it was trained with — there's no ATR
    intermediate step. Reuses the exact feature pipeline from
    scripts/fill_and_test_arb_v3.py (the training script) so live inference
    matches training exactly; real timestamps are required (unlike
    _candles_to_dataframe's synthetic ones for Kronos) since several features
    are time-indexed rolling windows.

    Returns None if the model, its feature pipeline, or enough real history
    isn't available — caller falls back to Kronos-small, then the ATR
    heuristic.
    """
    model = _load_lightgbm_model()
    if model is None:
        return None

    try:
        import sys

        import pandas as pd

        if LIGHTGBM_SCRIPTS_DIR not in sys.path:
            sys.path.insert(0, LIGHTGBM_SCRIPTS_DIR)
        from fill_and_test_arb_v3 import (  # noqa: E402  (path inserted just above)
            CLASS_NAMES as LGBM_CLASS_NAMES,
            FEATURE_COLUMNS as LGBM_FEATURE_COLUMNS,
            calculate_technical_indicators,
            load_high_macro_events,
        )
    except ImportError as e:
        log.info(f"LightGBM feature pipeline unavailable — falling back: {e}")
        return None

    # LSEClient's raw response uses "ts", not "timestamp" (verified against a
    # live fetch — its own docstring claims "timestamp" but that's wrong;
    # normalize_lse_candles() in scripts/train_kronos_comprehensive.py
    # already has to handle this same ts-or-timestamp inconsistency). Check
    # both so this doesn't silently filter every row to zero the way it did
    # on first live smoke-test.
    rows = [
        {
            "timestamp": c["ts"] if c.get("ts") is not None else c["timestamp"],
            "open": float(c.get("open", 0)),
            "high": float(c.get("high", 0)),
            "low": float(c.get("low", 0)),
            "close": float(c.get("close", 0)),
            "volume": float(c.get("volume", 0)),
        }
        for c in candles
        if c.get("ts") is not None or c.get("timestamp") is not None
    ]
    # 60min is the slowest rolling window (rolling_vol_60m/atr_60m/bb) — need
    # at least that much real history for the latest row's features to be
    # fully populated (not silently NaN from insufficient warmup).
    if len(rows) < 60:
        return None

    df = pd.DataFrame(rows)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df = df.drop_duplicates(subset="timestamp").sort_values("timestamp").reset_index(drop=True)

    try:
        high_events, _meta = load_high_macro_events(Path(MACRO_EVENTS_PATH))
    except (OSError, ValueError) as e:
        log.debug(f"macro calendar unavailable for LightGBM features: {e!r}")
        high_events = []

    t0 = time.time()
    try:
        feats = calculate_technical_indicators(df, high_events)
        # Ask the loaded MODEL what it actually needs, rather than assuming
        # the current global FEATURE_COLUMNS — a model saved before
        # fear_greed_value/funding_rate_bps existed (or after some future
        # feature change) must not be handed columns it never trained on,
        # which LightGBM's sklearn wrapper rejects outright. feature_name_ is
        # set by scikit-learn's LGBMClassifier whenever it's fit on a
        # DataFrame with named columns (true for every model this pipeline
        # produces), so this makes _try_lightgbm self-adapting to whichever
        # model.pkl is actually on disk.
        feature_cols = list(getattr(model, "feature_name_", None) or
                             [c for c in LGBM_FEATURE_COLUMNS if c in feats.columns])

        # fear_greed_value / funding_rate_bps (added 2026-08-21, see
        # ML_REGIME_BAKEOFF_2026-08-20.md follow-up): live one-call fetches,
        # not the historical training files (those are stale by the time the
        # bot runs) — and only fetched if this particular model actually
        # uses them. A failed fetch gets a neutral default rather than NaN —
        # these are secondary signals; a network hiccup on either shouldn't
        # veto a prediction whose core OHLCV features are perfectly fine.
        if "fear_greed_value" in feature_cols:
            fear_greed = _fetch_current_fear_greed()
            feats["fear_greed_value"] = fear_greed if fear_greed is not None else _FEAR_GREED_NEUTRAL
        if "funding_rate_bps" in feature_cols:
            funding_rate = _fetch_current_funding_rate_bps()
            feats["funding_rate_bps"] = funding_rate if funding_rate is not None else _FUNDING_RATE_NEUTRAL_BPS

        depth_cols = {"depth_imbalance_1pct", "depth_imbalance_5pct", "depth_total_notional_1pct"}
        if depth_cols & set(feature_cols):
            book_depth = _fetch_current_book_depth_features() or {}
            feats["depth_imbalance_1pct"] = book_depth.get("depth_imbalance_1pct", _DEPTH_IMBALANCE_NEUTRAL)
            feats["depth_imbalance_5pct"] = book_depth.get("depth_imbalance_5pct", _DEPTH_IMBALANCE_NEUTRAL)
            feats["depth_total_notional_1pct"] = book_depth.get("depth_total_notional_1pct", _DEPTH_NOTIONAL_NEUTRAL)

        oi_cols = {"open_interest", "oi_change_1h_pct", "toptrader_long_short_ratio", "taker_long_short_vol_ratio"}
        if oi_cols & set(feature_cols):
            oi_longshort = _fetch_current_oi_longshort() or {}
            feats["open_interest"] = oi_longshort.get("open_interest", _OPEN_INTEREST_NEUTRAL)
            feats["oi_change_1h_pct"] = oi_longshort.get("oi_change_1h_pct", _OI_CHANGE_NEUTRAL_PCT)
            feats["toptrader_long_short_ratio"] = oi_longshort.get(
                "toptrader_long_short_ratio", _LONG_SHORT_RATIO_NEUTRAL
            )
            feats["taker_long_short_vol_ratio"] = oi_longshort.get(
                "taker_long_short_vol_ratio", _LONG_SHORT_RATIO_NEUTRAL
            )

        cross_asset_cols = {"eth_return_1d_lag", "eth_realized_vol_5d_lag"}
        if cross_asset_cols & set(feature_cols):
            cross_asset = _fetch_current_cross_asset() or {}
            feats["eth_return_1d_lag"] = cross_asset.get("eth_return_1d_lag", _ETH_RETURN_NEUTRAL_PCT)
            feats["eth_realized_vol_5d_lag"] = cross_asset.get("eth_realized_vol_5d_lag", _ETH_VOL_NEUTRAL_PCT)

        latest = feats.iloc[[-1]]
        x = latest[feature_cols]
        if x.isna().any(axis=1).iloc[0]:
            # Not enough contiguous real history yet for every rolling
            # window (e.g. right after a data gap) — predicting on a
            # partially-NaN row would be a guess dressed up as a forecast.
            log.info("LightGBM regime skipped — latest feature row has NaNs (insufficient warmup)")
            return None
        proba = model.predict_proba(x)[0]
    except Exception as e:
        log.warning(f"LightGBM regime inference failed in {time.time() - t0:.2f}s: {e!r} — falling back")
        return None

    idx = int(proba.argmax())
    regime = LGBM_CLASS_NAMES[idx]
    confidence = float(proba[idx])
    no_entry, max_age = _regime_params(regime)

    # atr_14d is diagnostic-only here — LightGBM classifies directly from its
    # own trained cutoffs, not from an ATR percentage — so reuse the
    # heuristic's ATR computation on the same window purely for logging
    # continuity with the other two sources.
    atr_pct = _scale_atr_to_daily(_normalize_atr_to_pct(_compute_atr(candles), candles))

    log.info(
        f"LightGBM forecast: regime={regime}, confidence={confidence:.2f}, "
        f"proba={dict(zip(LGBM_CLASS_NAMES, (round(float(p), 3) for p in proba)))}, "
        f"{time.time() - t0:.2f}s"
    )

    return VolRegime(
        regime=regime,
        atr_14d=atr_pct,
        confidence=confidence,
        no_entry_seconds=no_entry,
        max_quote_age_ms=max_age,
        source="lightgbm",
    )


async def forecast_session_regime(
    lse_client: LSEClient = None,
    days: int = 30,
    timeframe: str = "1m",
    use_kronos: bool = True,
) -> VolRegime:
    """Forecast the BTC volatility regime for the upcoming session.

    This is the COLD PATH entry point called once at startup.
    Fetches recent BTC/USD candles from LSE, then attempts to use the
    Kronos-small model for regime forecasting. If Kronos is unavailable,
    fails, or exceeds the 30s hard timeout, falls back to the ATR heuristic.

    Sets the module-level SESSION_VOL_REGIME variable.

    Args:
        lse_client: Optional LSEClient instance. If None, creates one.
        days: Number of days of historical data to use.
        timeframe: Candle timeframe (default: 1m).
        use_kronos: If True (default), attempt Kronos model forecast.
            If False, skip straight to the ATR heuristic.

    Returns:
        VolRegime with regime, parameters, and confidence.
    """
    global SESSION_VOL_REGIME
    start_time = time.time()
    log.info(f"Forecasting session vol regime (days={days}, timeframe={timeframe})")

    # Fetch candles
    if lse_client is None:
        lse_client = LSEClient()

    candles = await lse_client.fetch_btc_candles_for_kronos(days=days, timeframe=timeframe)

    if not candles:
        log.warning("No candles for regime forecast — defaulting to MED")
        regime, confidence = "MED", 0.5
        no_entry, max_age = _regime_params("MED")
        result = VolRegime(
            regime=regime,
            atr_14d=0.0,
            confidence=confidence,
            no_entry_seconds=no_entry,
            max_quote_age_ms=max_age,
            source="default",
        )
        SESSION_VOL_REGIME = regime
        return result

    # Compute heuristic ATR (always computed as fallback baseline).
    # 1-minute candles: raw dollar ATR must be normalized to a daily-equivalent
    # percentage before applying the daily-calibrated thresholds.
    atr_dollars = _compute_atr(candles, period=14)

    # Sparse/zero-range data guard: _compute_atr returns exactly 0.0 when there
    # are too few candles or no non-zero true ranges. Feeding that through
    # _regime_from_atr would fabricate a LOW regime (3.0s no-entry zone) for a
    # market whose volatility is UNKNOWN — the permissive direction. Fail to
    # the CONSERVATIVE MED default (5.0s no-entry, 500ms quote age) and log
    # loudly so operators see the forecast degraded instead of the bot silently
    # trading on a false LOW.
    if atr_dollars <= 0.0:
        log.warning(
            f"ATR heuristic produced zero ATR from {len(candles)} candles — "
            f"defaulting to conservative MED regime"
        )
        no_entry, max_age = _regime_params("MED")
        result = VolRegime(
            regime="MED",
            atr_14d=0.0,
            confidence=0.5,
            no_entry_seconds=no_entry,
            max_quote_age_ms=max_age,
            source="sparse",
        )
        SESSION_VOL_REGIME = "MED"
        return result

    atr_pct = _normalize_atr_to_pct(atr_dollars, candles)
    atr_scaled = _scale_atr_to_daily(atr_pct)
    regime, confidence = _regime_from_atr(atr_scaled)
    no_entry, max_age = _regime_params(regime)
    source = "heuristic"
    # Report the daily-equivalent ATR% as the canonical value (matches
    # the daily regime semantics the thresholds were designed for).
    atr = atr_scaled

    # Try the ML regime forecasters if requested, in priority order:
    # LightGBM first (2026-08-20 bake-off winner — 67.51% mean holdout
    # accuracy vs the from-scratch transformer's 53.82% and Kronos-small's
    # documented ~0.55 match rate, see ML_REGIME_BAKEOFF_2026-08-20.md), then
    # Kronos-small as fallback if LightGBM's model/feature pipeline isn't
    # available. Both run with the same hard timeout on the full
    # load+inference path, in a thread executor since both are blocking.
    if use_kronos:
        ml_regime = None
        for name, fn in (("LightGBM", _try_lightgbm), ("Kronos", _try_kronos)):
            try:
                loop = asyncio.get_running_loop()
                ml_regime = await asyncio.wait_for(
                    loop.run_in_executor(None, fn, candles),
                    timeout=KRONOS_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                log.warning(
                    f"{name} load+inference exceeded {KRONOS_TIMEOUT_SECONDS}s hard timeout "
                    f"— trying next source"
                )
                ml_regime = None
            except Exception as e:
                log.warning(f"{name} load+inference raised an exception: {e!r} — trying next source")
                ml_regime = None
            if ml_regime is not None:
                break
        else:
            log.warning("no ML regime source available — falling back to ATR heuristic")

        if ml_regime is not None:
            regime = ml_regime.regime
            confidence = ml_regime.confidence
            no_entry = ml_regime.no_entry_seconds
            max_age = ml_regime.max_quote_age_ms
            source = ml_regime.source
            atr = ml_regime.atr_14d

    result = VolRegime(
        regime=regime,
        atr_14d=atr,
        confidence=confidence,
        no_entry_seconds=no_entry,
        max_quote_age_ms=max_age,
        source=source,
    )

    # Set the module-level output variable
    SESSION_VOL_REGIME = regime

    elapsed = time.time() - start_time
    load_t = f"{_kronos_load_time:.2f}s" if _kronos_load_time else "N/A"
    inf_t = f"{_kronos_inference_time:.2f}s" if _kronos_inference_time else "N/A"
    log.info(
        f"Regime forecast: {result.regime} (daily-equiv ATR%={atr:.4f}, "
        f"confidence={confidence:.2f}, source={source}, {elapsed:.2f}s, "
        f"kronos_load={load_t}, kronos_inference={inf_t})"
    )

    return result


REGIME_STATE_PATH = "state/regime_active.json"


def apply_regime_to_settings(regime: VolRegime):
    """Apply the regime parameters to the global SETTINGS.

    This modifies the frozen Settings object's fields in-place.
    Called once in main.py after forecast_session_regime().
    """
    # Settings is frozen, but we can use object.__setattr__ to bypass
    object.__setattr__(SETTINGS, "no_entry_seconds", regime.no_entry_seconds)
    object.__setattr__(SETTINGS, "max_quote_age_ms", regime.max_quote_age_ms)
    log.info(f"Applied regime {regime.regime}: no_entry={regime.no_entry_seconds}s, "
             f"max_quote_age={regime.max_quote_age_ms}ms")
    # Kronos's regime call was previously invisible outside the raw log
    # (an operator watching the dashboard had no way to see it was silently
    # tightening entry timing). Persist it so botmind.py can surface it.
    try:
        path = Path(REGIME_STATE_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "regime": regime.regime,
                    "atr_14d": regime.atr_14d,
                    "confidence": regime.confidence,
                    "no_entry_seconds": regime.no_entry_seconds,
                    "max_quote_age_ms": regime.max_quote_age_ms,
                    "source": regime.source,
                    "applied_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                },
                indent=2,
            ) + "\n",
            encoding="utf-8",
        )
    except OSError as e:
        log.debug(f"regime state write failed: {e!r}")


async def main():
    """Run regime forecast standalone."""
    from .logger import BotLogger, log as _log
    global log
    log = BotLogger(jsonl_path="state/regime_forecast.jsonl", level="INFO")

    lse = LSEClient()
    regime = await forecast_session_regime(lse)
    await lse.aclose()

    print(f"\nSession Volatility Regime: {regime.regime}")
    print(f"  ATR (14d): {regime.atr_14d:.4f}")
    print(f"  Confidence: {regime.confidence:.2f}")
    print(f"  Source: {regime.source}")
    print(f"  NO_ENTRY_SECONDS: {regime.no_entry_seconds}")
    print(f"  MAX_QUOTE_AGE_MS: {regime.max_quote_age_ms}")

    apply_regime_to_settings(regime)
    print(f"\nApplied to SETTINGS:")
    print(f"  SETTINGS.no_entry_seconds = {SETTINGS.no_entry_seconds}")
    print(f"  SETTINGS.max_quote_age_ms = {SETTINGS.max_quote_age_ms}")


if __name__ == "__main__":
    asyncio.run(main())
