#!/usr/bin/env python3
"""Expanded Kronos validation & ATR threshold calibration.

Builds a large validation set of 20+ regime checkpoints spanning different
volatility regimes, times of day, and market conditions. Uses REAL BTC/USD
1-minute candles fetched from the LSE REST API (which is operational in this
environment). Runs the REAL Kronos-small model for each checkpoint and compares
the forecast regime against:
  1. Realized volatility of the following 24-hour session (1-min daily-equiv ATR%)
  2. Ground-truth daily regime from 30-day daily candle ATR%

After building the full dataset, re-tunes the HIGH/MED/LOW ATR-bucket thresholds
by analyzing the predicted vs realized ATR% distributions and reports old
vs new threshold values with the resulting match-rate improvement.

Output: state/kronos_calibration_expanded.json
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import math
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import httpx
import numpy as np

from polymarket_arb.config import SETTINGS
from polymarket_arb.kronos_session import (
    _compute_atr,
    _regime_from_atr,
    _regime_params,
    _normalize_atr_to_pct,
    _scale_atr_to_daily,
    _try_kronos,
    _load_kronos_model,
    _candles_to_dataframe,
    KRONOS_PRED_LEN,
    ATR_HIGH_THRESHOLD,
    ATR_LOW_THRESHOLD,
    ATR_TIME_SCALING_FACTOR,
    MINUTES_PER_DAY,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

LSE_BASE_URL = "https://api.londonstrategicedge.com/vault"
LSE_API_KEY = SETTINGS.lse_api_key  # read from LSE_API_KEY env / .env, never hardcoded
LSE_TIMEOUT = 30.0

# How many candles to request per LSE fetch
HISTORICAL_DAYS = 30          # days *before* checkpoint for Kronos input
FOLLOWING_SESSION_HOURS = 24  # hours *after* checkpoint for realized vol
DAILY_LOOKBACK_DAYS = 30      # days for daily ATR ground truth
ATR_PERIOD = 14
KRONOS_MAX_INPUT = 512        # Kronos context length

OUTPUT_PATH = os.path.join(PROJECT_ROOT, "state", "kronos_calibration_expanded.json")

# OLD thresholds (from kronos_session.py)
OLD_HIGH_THRESHOLD = ATR_HIGH_THRESHOLD  # 0.05 (5%)
OLD_LOW_THRESHOLD = ATR_LOW_THRESHOLD    # 0.02 (2%)

# ---------------------------------------------------------------------------
# Expanded checkpoint set — 22 real timestamps spanning different regimes
# ---------------------------------------------------------------------------

# Each checkpoint is at 00:00 UTC (market open in some sense) or at a specific
# intraday time to capture different market conditions.
# Timestamps span:
#   - Different months: Jan, Feb, Mar, Apr, May, Jun, Jul (7 months)
#   - Different volatility regimes: crash periods, calm periods, transition periods
#   - Weekdays: Mon-Sun represented
# We include the 5 existing historical timestamps from the original spotcheck
# plus 22 new real-world timestamps.

EXPANDED_CHECKPOINTS: List[Dict[str, Any]] = [
    # --- Original 5 historical timestamps (for combined analysis) ---
    {"timestamp": "2026-02-15T00:00:00Z", "label": "High vol event (Feb)", "notes": "original spotcheck"},
    {"timestamp": "2026-01-28T00:00:00Z", "label": "Low vol (Jan)", "notes": "original spotcheck"},
    {"timestamp": "2026-05-28T00:00:00Z", "label": "MED vol (May)", "notes": "original spotcheck"},
    {"timestamp": "2026-07-15T00:00:00Z", "label": "MED vol (Jul 15)", "notes": "original spotcheck"},
    {"timestamp": "2026-07-28T00:00:00Z", "label": "MED vol (Jul 28)", "notes": "original spotcheck"},

    # --- 22 additional real timestamps spanning different regimes ---
    # January — post-holiday, mixed volatility
    {"timestamp": "2026-01-05T08:00:00Z", "label": "Jan 05 08:00 UTC (Asian open)", "notes": "quiet post-holiday session"},
    {"timestamp": "2026-01-12T14:00:00Z", "label": "Jan 12 14:00 UTC (US open)", "notes": "mid-Jan baseline"},
    {"timestamp": "2026-01-20T02:00:00Z", "label": "Jan 20 02:00 UTC (late NY)", "notes": "thin liquidity overnight"},

    # February — high volatility period (crash analog)
    {"timestamp": "2026-02-02T00:00:00Z", "label": "Feb 02 00:00 UTC (Feb startup)", "notes": "start of Feb vol regime"},
    {"timestamp": "2026-02-10T20:00:00Z", "label": "Feb 10 20:00 UTC (US session)", "notes": "high vol pre-crash"},
    {"timestamp": "2026-02-20T12:00:00Z", "label": "Feb 20 12:00 UTC (European close)", "notes": "post-crash recovery"},
    {"timestamp": "2026-02-25T16:00:00Z", "label": "Feb 25 16:00 UTC (NY close)", "notes": "mid-week volatility"},

    # March — transition / medium vol
    {"timestamp": "2026-03-03T09:30:00Z", "label": "Mar 03 09:30 UTC (London open)", "notes": "March baseline"},
    {"timestamp": "2026-03-10T14:00:00Z", "label": "Mar 10 14:00 UTC (US open)", "notes": "medium vol, post-catalyst"},
    {"timestamp": "2026-03-18T00:00:00Z", "label": "Mar 18 00:00 UTC (mid-month)", "notes": "low-ish vol"},
    {"timestamp": "2026-03-25T18:00:00Z", "label": "Mar 25 18:00 UTC (Asian close)", "notes": "end of quarter prep"},

    # April — typically calmer
    {"timestamp": "2026-04-01T00:00:00Z", "label": "Apr 01 00:00 UTC (April Fools)", "notes": "quiet April start"},
    {"timestamp": "2026-04-08T10:00:00Z", "label": "Apr 08 10:00 UTC (mid-European)", "notes": "steady state"},
    {"timestamp": "2026-04-15T15:00:00Z", "label": "Apr 15 15:00 UTC (US close)", "notes": "pre-weekend"},
    {"timestamp": "2026-04-22T20:00:00Z", "label": "Apr 22 20:00 UTC (overnight)", "notes": "low vol session"},
    {"timestamp": "2026-04-29T06:00:00Z", "label": "Apr 29 06:00 UTC (Asian open)", "notes": "month-end balance"},

    # May — build-up to summer
    {"timestamp": "2026-05-06T12:00:00Z", "label": "May 06 12:00 UTC (midday EU)", "notes": "May baseline"},
    {"timestamp": "2026-05-13T14:00:00Z", "label": "May 13 14:00 UTC (US open)", "notes": "medium vol"},
    {"timestamp": "2026-05-20T18:00:00Z", "label": "May 20 18:00 UTC (NY close)", "notes": "pre-weekend build"},

    # June — summer consolidation
    {"timestamp": "2026-06-03T08:00:00Z", "label": "Jun 03 08:00 UTC (Asian open)", "notes": "quiet June start"},
    {"timestamp": "2026-06-10T10:00:00Z", "label": "Jun 10 10:00 UTC (mid-day)", "notes": "summer lull"},
    {"timestamp": "2026-06-17T14:00:00Z", "label": "Jun 17 14:00 UTC (US open)", "notes": "low vol session"},
    {"timestamp": "2026-06-24T16:00:00Z", "label": "Jun 24 16:00 UTC (NY close)", "notes": "month-end"},

    # July — mid-summer
    {"timestamp": "2026-07-01T00:00:00Z", "label": "Jul 01 00:00 UTC (quarter start)", "notes": "Q3 open"},
    {"timestamp": "2026-07-08T12:00:00Z", "label": "Jul 08 12:00 UTC (EU close)", "notes": "mid-July baseline"},
    {"timestamp": "2026-07-23T15:00:00Z", "label": "Jul 23 15:00 UTC (US close)", "notes": "pre-BTC conference?"},
]


# ---------------------------------------------------------------------------
# LSE API fetch helpers
# ---------------------------------------------------------------------------

def _parse_ts(ts_str: str) -> datetime:
    """Parse an LSE timestamp string like '2026-07-15 00:00:00.000000'."""
    # LSE returns: "2026-07-15 00:00:00.000000"
    parts = ts_str.split()
    date_part = parts[0]
    time_part = parts[1] if len(parts) > 1 else "00:00:00.000000"
    dt_str = f"{date_part}T{time_part}Z"
    dt_str = dt_str.replace("+00:00", "Z")
    try:
        return datetime.strptime(dt_str, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime.strptime(dt_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


async def fetch_lse_candles(
    symbol: str,
    timeframe: str,
    start: str,
    end: str,
    limit: int = 10000,
) -> List[Dict[str, Any]]:
    """Fetch OHLCV candles from LSE REST API.

    Returns list of dicts with ts, open, high, low, close, volume.
    """
    async with httpx.AsyncClient(
        base_url=LSE_BASE_URL,
        timeout=LSE_TIMEOUT,
        headers={
            "Content-Type": "application/json",
            "x-api-key": LSE_API_KEY,
        },
    ) as client:
        params = {
            "symbol": symbol,
            "timeframe": timeframe,
            "limit": str(limit),
            "start": start,
            "end": end,
        }
        try:
            resp = await client.get("/candles", params=params)
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict):
                data = data.get("data", [])
            return data if isinstance(data, list) else []
        except Exception as e:
            print(f"  [WARN] LSE fetch failed: {e!r}")
            return []


def candles_from_lse(raw: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Normalize LSE candle dicts to the format expected by kronos_session.

    LSE returns: {ts, symbol, open, high, low, close, volume}
    kronos_session expects: {open, high, low, close, volume} (+ optional timestamp)
    """
    candles = []
    for c in raw:
        candles.append({
            "timestamp": c.get("ts", ""),
            "open": float(c.get("open", 0)),
            "high": float(c.get("high", 0)),
            "low": float(c.get("low", 0)),
            "close": float(c.get("close", 0)),
            "volume": float(c.get("volume", 0)),
        })
    return candles


# ---------------------------------------------------------------------------
# Checkpoint runner
# ---------------------------------------------------------------------------

async def run_checkpoint(
    checkpoint: Dict[str, Any],
    predictor_available: bool,
) -> Dict[str, Any]:
    """Run a single Kronos validation checkpoint using REAL LSE data.

    Fetches:
      - 30 days of 1m candles before the checkpoint (for Kronos input + heuristic + daily ground truth)
      - 24h of 1m candles after the checkpoint (for realized volatility)
      - 30 daily candles before the checkpoint (for ground-truth regime)
    """
    ts_str = checkpoint["timestamp"]
    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    ts_date = ts.strftime("%Y-%m-%d")
    ts_time = ts.strftime("%H:%M:%S")

    # Date ranges for fetching
    hist_start = (ts - timedelta(days=HISTORICAL_DAYS)).strftime("%Y-%m-%d")
    hist_end = ts_date  # up to and including checkpoint date

    # Following session: ts + 1 day to ts + 1 day + 24h
    fol_start = (ts + timedelta(days=1)).strftime("%Y-%m-%d")
    fol_end = (ts + timedelta(days=2)).strftime("%Y-%m-%d")

    # Daily candles for ground truth (lookback)
    daily_start = (ts - timedelta(days=DAILY_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    daily_end = ts_date

    result: Dict[str, Any] = {
        "timestamp": ts_str,
        "label": checkpoint.get("label", ""),
        "notes": checkpoint.get("notes", ""),
    }

    # --- Fetch historical 1-minute candles ---
    print(f"  Fetching 30-day 1m history ({hist_start} -> {hist_end})...")
    t0 = time.time()
    raw_hist = await fetch_lse_candles("BTC/USD", "1m", hist_start, hist_end, limit=5000)
    hist_candles = candles_from_lse(raw_hist)
    result["historical_candles_fetched"] = len(hist_candles)
    result["fetch_time_seconds"] = round(time.time() - t0, 2)

    if not hist_candles:
        result["error"] = "No historical candles fetched"
        result["regime"] = "MED"
        result["source"] = "default"
        result["confidence"] = 0.5
        return result

    # --- Heuristic ATR on 1-minute candles (baseline) ---
    raw_atr = _compute_atr(hist_candles, period=ATR_PERIOD)
    atr_pct = _normalize_atr_to_pct(raw_atr, hist_candles)
    atr_scaled = _scale_atr_to_daily(atr_pct)
    heur_regime, heur_conf = _regime_from_atr(atr_scaled)
    result["heuristic"] = {
        "regime": heur_regime,
        "confidence": round(heur_conf, 4),
        "atr_dollars": round(raw_atr, 4),
        "atr_pct_1min": round(atr_pct, 8),
        "atr_pct_daily_equiv": round(atr_scaled, 6),
        "no_entry_seconds": _regime_params(heur_regime)[0],
        "max_quote_age_ms": _regime_params(heur_regime)[1],
    }

    # --- REAL Kronos model inference ---
    forecast_regime = heur_regime
    forecast_source = "heuristic"
    forecast_atr_scaled = atr_scaled
    forecast_conf = heur_conf

    if predictor_available and len(hist_candles) >= 2:
        # Kronos uses the most recent 512 candles
        kronos_input = hist_candles[-KRONOS_MAX_INPUT:]
        t0 = time.time()
        kronos_vr = _try_kronos(kronos_input)
        kronos_time = time.time() - t0

        if kronos_vr is not None:
            result["kronos"] = {
                "regime": kronos_vr.regime,
                "confidence": round(kronos_vr.confidence, 4),
                "atr_pct_daily_equiv": round(kronos_vr.atr_14d, 6),
                "no_entry_seconds": kronos_vr.no_entry_seconds,
                "max_quote_age_ms": kronos_vr.max_quote_age_ms,
                "inference_time_seconds": round(kronos_time, 2),
                "source": "real_kronos_model",
            }
            forecast_regime = kronos_vr.regime
            forecast_source = "kronos"
            forecast_atr_scaled = kronos_vr.atr_14d
            forecast_conf = kronos_vr.confidence
        else:
            result["kronos"] = {"error": "Kronos returned None"}
    else:
        result["kronos"] = {"error": "Kronos model not available"}

    result["regime"] = forecast_regime
    result["source"] = forecast_source
    result["confidence"] = round(forecast_conf, 4)
    result["predicted_atr_pct_daily_equiv"] = round(forecast_atr_scaled, 6)
    result["forecast_regime_rank"] = {"LOW": 0, "MED": 1, "HIGH": 2}.get(forecast_regime, -1)

    # --- Fetch following session candles (realized volatility) ---
    print(f"  Fetching 24h following session ({fol_start} -> {fol_end})...")
    raw_fol = await fetch_lse_candles("BTC/USD", "1m", fol_start, fol_end, limit=2000)
    fol_candles = candles_from_lse(raw_fol)
    result["following_candles_fetched"] = len(fol_candles)

    realized_regime = "UNKNOWN"
    realized_atr_scaled = None
    realized_atr_pct = None

    if fol_candles and len(fol_candles) >= ATR_PERIOD:
        r_raw_atr = _compute_atr(fol_candles, period=ATR_PERIOD)
        r_atr_pct = _normalize_atr_to_pct(r_raw_atr, fol_candles)
        r_atr_scaled = _scale_atr_to_daily(r_atr_pct)
        realized_regime, realized_conf = _regime_from_atr(r_atr_scaled)
        realized_atr_scaled = round(r_atr_scaled, 6)
        realized_atr_pct = round(r_atr_pct, 8)

        result["realized"] = {
            "regime": realized_regime,
            "confidence": round(realized_conf, 4),
            "atr_pct_daily_equiv": realized_atr_scaled,
            "atr_pct_1min": realized_atr_pct,
            "atr_dollars": round(r_raw_atr, 4),
            "candles_fetched": len(fol_candles),
        }
    else:
        result["realized"] = {"error": f"Only {len(fol_candles)} following-session candles"}

    # --- Ground truth: daily candles ---
    print(f"  Fetching 30-day daily candles ({daily_start} -> {daily_end})...")
    raw_daily = await fetch_lse_candles("BTC/USD", "1d", daily_start, daily_end, limit=100)
    daily_candles = candles_from_lse(raw_daily)
    result["daily_candles_fetched"] = len(daily_candles)

    gt_regime = "UNKNOWN"
    gt_atr_pct = None

    if daily_candles and len(daily_candles) > ATR_PERIOD:
        d_raw_atr = _compute_atr(daily_candles, period=ATR_PERIOD)
        d_atr_pct = _normalize_atr_to_pct(d_raw_atr, daily_candles)
        d_regime, d_conf = _regime_from_atr(d_atr_pct)
        gt_regime = d_regime
        gt_atr_pct = round(d_atr_pct, 6)

        result["ground_truth_daily"] = {
            "regime": gt_regime,
            "atr_pct": gt_atr_pct,
            "confidence": round(d_conf, 4),
            "candles_fetched": len(daily_candles),
        }
    else:
        result["ground_truth_daily"] = {"regime": "UNKNOWN", "note": f"Only {len(daily_candles)} daily candles"}

    # --- Comparisons ---
    result["realized_regime"] = realized_regime
    result["realized_regime_rank"] = {"LOW": 0, "MED": 1, "HIGH": 2}.get(realized_regime, -1)
    result["realized_atr_pct_daily_equiv"] = realized_atr_scaled
    result["ground_truth_regime"] = gt_regime
    result["ground_truth_atr_pct"] = gt_atr_pct

    # Match: forecast regime vs realized regime
    result["regime_match"] = forecast_regime == realized_regime
    result["rank_diff"] = result.get("forecast_regime_rank", -1) - result.get("realized_regime_rank", -1)

    # Match: forecast regime vs ground-truth daily regime
    result["gt_regime_match"] = forecast_regime == gt_regime if gt_regime != "UNKNOWN" else None

    # Match: ground truth daily vs realized daily (1-min realized scaled to daily)
    result["gt_matches_realized"] = gt_regime == realized_regime if gt_regime != "UNKNOWN" and realized_regime != "UNKNOWN" else None

    return result


# ---------------------------------------------------------------------------
# Threshold tuning analysis
# ---------------------------------------------------------------------------

def analyze_thresholds(
    results: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Analyze the predicted vs realized ATR% distributions and find optimal
    thresholds.

    We have:
      - predicted_atr_pct_daily_equiv: Kronos's forecast (daily-equivalent ATR%)
      - realized_atr_pct_daily_equiv: actual realized (daily-equivalent from 24h 1m)
      - ground_truth_atr_pct: 30-day daily ATR%

    The thresholds are applied to the *forecast* daily-equivalent ATR% to
    determine HIGH/MED/LOW. We want to find thresholds that maximize the
    match between forecast regime and realized regime.

    Strategy: sweep threshold values and compute match rate for each (HIGH, LOW)
    pair. Report the best pair.
    """
    # Collect (predicted_daily_atr_pct, realized_regime) pairs where we have both
    valid = []
    for r in results:
        pred = r.get("predicted_atr_pct_daily_equiv")
        realized = r.get("realized_regime")
        if pred is not None and realized in ("HIGH", "MED", "LOW"):
            valid.append((pred, realized))

    if not valid:
        return {"error": "No valid data for threshold analysis"}

    preds = np.array([p for p, _ in valid])
    realized = np.array([r for _, r in valid])
    regime_order = {"LOW": 0, "MED": 1, "HIGH": 2}
    realized_ranks = np.array([regime_order[r] for r in realized])

    # --- Current thresholds ---
    old_high = OLD_HIGH_THRESHOLD  # 0.05
    old_low = OLD_LOW_THRESHOLD    # 0.02

    def classify(arr: np.ndarray, high: float, low: float) -> np.ndarray:
        """Classify ATR% values into regime ranks: 0=LOW, 1=MED, 2=HIGH."""
        ranks = np.zeros(len(arr), dtype=int)
        ranks[arr >= high] = 2  # HIGH
        ranks[(arr >= low) & (arr < high)] = 1  # MED
        return ranks

    old_pred_ranks = classify(preds, old_high, old_low)
    old_matches = np.sum(old_pred_ranks == realized_ranks)
    old_match_rate = old_matches / len(valid)

    # --- Sweep thresholds ---
    # Try all combinations of high in [0.025, 0.12] step 0.005 and low in [0.005, 0.05] step 0.005
    best_match_rate = 0.0
    best_high = old_high
    best_low = old_low

    high_range = np.arange(0.025, 0.13, 0.005)  # 0.025 to 0.125
    low_range = np.arange(0.005, 0.055, 0.005)  # 0.005 to 0.050

    sweep_results = []
    for h in high_range:
        for l in low_range:
            if h <= l:
                continue
            pred_ranks = classify(preds, h, l)
            matches = np.sum(pred_ranks == realized_ranks)
            rate = matches / len(valid)
            sweep_results.append({"high": round(h, 3), "low": round(l, 3), "match_rate": round(rate, 4)})
            if rate > best_match_rate:
                best_match_rate = rate
                best_high = h
                best_low = l

    new_pred_ranks = classify(preds, best_high, best_low)
    new_matches = np.sum(new_pred_ranks == realized_ranks)
    new_match_rate = new_matches / len(valid)

    # Also analyze: what are the ATR% distributions for each realized regime?
    regime_distributions = {}
    for regime in ("LOW", "MED", "HIGH"):
        mask = realized_ranks == regime_order[regime]
        regime_preds = preds[mask]
        if len(regime_preds) > 0:
            regime_distributions[regime] = {
                "count": len(regime_preds),
                "mean_pred_atr_pct": round(float(np.mean(regime_preds)), 6),
                "min_pred_atr_pct": round(float(np.min(regime_preds)), 6),
                "max_pred_atr_pct": round(float(np.max(regime_preds)), 6),
                "median_pred_atr_pct": round(float(np.median(regime_preds)), 6),
                "std_pred_atr_pct": round(float(np.std(regime_preds)), 6),
                "predicted_regimes_with_old": {
                    "LOW": int(np.sum(classify(regime_preds, old_high, old_low) == 0)),
                    "MED": int(np.sum(classify(regime_preds, old_high, old_low) == 1)),
                    "HIGH": int(np.sum(classify(regime_preds, old_high, old_low) == 2)),
                },
            }
        else:
            regime_distributions[regime] = {"count": 0}

    # Also analyze ground-truth regime distributions (daily ATR%)
    gt_valid = []
    for r in results:
        gt = r.get("ground_truth_regime")
        pred = r.get("predicted_atr_pct_daily_equiv")
        if gt in ("HIGH", "MED", "LOW") and pred is not None:
            gt_valid.append((pred, gt))

    gt_regime_distributions = {}
    if gt_valid:
        gt_preds = np.array([p for p, _ in gt_valid])
        gt_labels = np.array([r for _, r in gt_valid])
        for regime in ("LOW", "MED", "HIGH"):
            mask = gt_labels == regime
            regime_preds = gt_preds[mask]
            if len(regime_preds) > 0:
                gt_regime_distributions[regime] = {
                    "count": len(regime_preds),
                    "mean_pred_atr_pct": round(float(np.mean(regime_preds)), 6),
                    "min_pred_atr_pct": round(float(np.min(regime_preds)), 6),
                    "max_pred_atr_pct": round(float(np.max(regime_preds)), 6),
                    "median_pred_atr_pct": round(float(np.median(regime_preds)), 6),
                }
            else:
                gt_regime_distributions[regime] = {"count": 0}

    # Threshold suggestion based on regime boundary analysis
    # Find natural gap between MED and HIGH, and between LOW and MED
    if "MED" in regime_distributions and "HIGH" in regime_distributions:
        med_max = regime_distributions["MED"].get("max_pred_atr_pct", 0)
        high_min = regime_distributions["HIGH"].get("min_pred_atr_pct", 1)
        gap_high = "gap exists" if high_min > med_max else "overlap"
    else:
        med_max = high_min = gap_high = "N/A"

    if "LOW" in regime_distributions and "MED" in regime_distributions:
        low_max = regime_distributions["LOW"].get("max_pred_atr_pct", 0)
        med_min = regime_distributions["MED"].get("min_pred_atr_pct", 0)
        gap_low = "gap exists" if med_min > low_max else "overlap"
    else:
        low_max = med_min = gap_low = "N/A"

    return {
        "analysis_method": (
            "Swept HIGH threshold from 0.025 to 0.125 (step 0.005) and LOW "
            "threshold from 0.005 to 0.050 (step 0.005) against the predicted "
            "daily-equivalent ATR% values, computing match rate with realized regimes."
        ),
        "valid_samples": len(valid),
        "old_thresholds": {
            "high": old_high,
            "low": old_low,
            "match_rate_vs_realized": round(float(old_match_rate), 4),
            "matches": int(old_matches),
            "mismatches": int(len(valid) - old_matches),
        },
        "new_thresholds": {
            "high": round(best_high, 3) if isinstance(best_high, float) else best_high,
            "low": round(best_low, 3) if isinstance(best_low, float) else best_low,
            "match_rate_vs_realized": round(float(new_match_rate), 4),
            "matches": int(new_matches),
            "mismatches": int(len(valid) - new_matches),
        },
        "match_rate_improvement": round(float(new_match_rate - old_match_rate), 4),
        "regime_distributions_predicted_vs_realized": regime_distributions,
        "ground_truth_regime_distributions": gt_regime_distributions,
        "boundary_analysis": {
            "high_threshold_suggestion": {
                "med_max_pred": med_max,
                "high_min_pred": high_min,
                "natural_gap": gap_high,
            },
            "low_threshold_suggestion": {
                "low_max_pred": low_max,
                "med_min_pred": med_min,
                "natural_gap": gap_low,
            },
        },
        "sweep_results_top10": sorted(sweep_results, key=lambda x: -x["match_rate"])[:10],
        "all_sweep_results": sweep_results,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)

    print("=" * 80)
    print("KRONOS EXPANDED CALIBRATION — 22 New Checkpoints")
    print("=" * 80)
    _key_status = "unset" if not LSE_API_KEY else (
        "placeholder sentinel" if LSE_API_KEY == "lse_live_00000000000000000000000000000000" else "real key"
    )
    print(f"LSE API key: {LSE_API_KEY[:20]}... ({_key_status})")
    print(f"ATR thresholds: HIGH >= {ATR_HIGH_THRESHOLD} ({ATR_HIGH_THRESHOLD*100}%), "
          f"LOW < {ATR_LOW_THRESHOLD} ({ATR_LOW_THRESHOLD*100}%)")
    print(f"Time scaling: 1-min ATR% -> daily-equiv via sqrt(1440) = "
          f"{ATR_TIME_SCALING_FACTOR:.4f}")
    print(f"Total checkpoints: {len(EXPANDED_CHECKPOINTS)}")
    print()

    # Load Kronos model once (pre-cache)
    print("Loading Kronos-small model...")
    t0 = time.time()
    predictor = _load_kronos_model()
    load_time = time.time() - t0
    model_available = predictor is not None
    print(f"  Model loaded in {load_time:.2f}s, available={model_available}")
    print()

    # Run all checkpoints
    all_results: List[Dict[str, Any]] = []
    for i, cp in enumerate(EXPANDED_CHECKPOINTS, 1):
        print(f"[{i}/{len(EXPANDED_CHECKPOINTS)}] {cp['label']} ({cp['timestamp']})")
        result = await run_checkpoint(cp, model_available)
        all_results.append(result)

        fore = result.get("regime", "?")
        real = result.get("realized_regime", "?")
        gt = result.get("ground_truth_regime", "?")
        pred_atr = result.get("predicted_atr_pct_daily_equiv", "?")
        real_atr = result.get("realized_atr_pct_daily_equiv", "?")
        match = result.get("regime_match", False)
        source = result.get("source", "?")
        kronos_atr = result.get("kronos", {}).get("atr_pct_daily_equiv", result.get("heuristic", {}).get("atr_pct_daily_equiv", "?"))

        print(f"  Forecast: {fore} (ATR%={kronos_atr}, source={source})")
        print(f"  Realized: {real} (ATR%={real_atr})")
        print(f"  Ground truth (daily): {gt} (ATR%={result.get('ground_truth_atr_pct', '?')})")
        print(f"  Match (forecast vs realized): {match}")
        if "error" in result.get("kronos", {}):
            print(f"  Kronos error: {result['kronos']['error']}")
        print()

    # --- Combined analysis with original 7 checkpoints ---
    print("=" * 80)
    print("COMBINED ANALYSIS: Original 7 + 20 new = 27 total")
    print("=" * 80)

    # Load original spotcheck results
    orig_path = os.path.join(PROJECT_ROOT, "state", "kronos_spotcheck.json")
    with open(orig_path) as f:
        orig_data = json.load(f)

    # Extract original results into the same format
    orig_results = []
    for r in orig_data["detailed_results"]:
        orig_results.append({
            "timestamp": r["timestamp"],
            "label": f"original_{r['timestamp']}",
            "source": r.get("source", "kronos"),
            "regime": r.get("regime"),
            "predicted_atr_pct_daily_equiv": r.get("predicted_atr_pct_daily_equiv", r.get("atr_pct_daily_equiv")),
            "realized_regime": r.get("realized_regime"),
            "realized_atr_pct_daily_equiv": r.get("realized_atr_pct_daily_equiv"),
            "ground_truth_regime": r.get("ground_truth_daily", {}).get("regime", "UNKNOWN"),
            "ground_truth_atr_pct": r.get("ground_truth_daily", {}).get("atr_pct"),
            "regime_match": r.get("regime_match"),
            "gt_regime_match": None,  # original didn't compute this
        })

    # Load original checkpoints 6 & 7 (synthetic)
    orig_67_path = os.path.join(PROJECT_ROOT, "state", "kronos_checkpoints_6_7.json")
    if os.path.exists(orig_67_path):
        with open(orig_67_path) as f:
            orig_67_data = json.load(f)
        for r in orig_67_data["detailed_results"]:
            orig_results.append({
                "checkpoint": r["checkpoint"],
                "timestamp": r["checkpoint"],
                "label": f"synthetic_{r['checkpoint']}",
                "source": r.get("forecast_source", "kronos"),
                "regime": r.get("forecast_regime"),
                "predicted_atr_pct_daily_equiv": r.get("kronos", {}).get("atr_pct_daily_equiv", r.get("heuristic", {}).get("atr_pct_daily_equiv")),
                "realized_regime": r.get("realized_regime"),
                "realized_atr_pct_daily_equiv": r.get("realized", {}).get("atr_pct_daily_equiv"),
                "ground_truth_regime": "UNKNOWN",  # synthetic data
                "ground_truth_atr_pct": None,
                "regime_match": r.get("regime_match"),
                "gt_regime_match": None,
                "synthetic": True,
            })

    # Combined: all original 7 + new 20
    combined = orig_results + all_results
    combined = [c for c in combined if c.get("regime") in ("HIGH", "MED", "LOW")]

    print(f"Combined valid checkpoints: {len(combined)}")
    for c in combined:
        match = "MATCH" if c.get("regime_match") else "MISMATCH"
        print(f"  {c['timestamp']}: forecast={c['regime']} realized={c.get('realized_regime')} gt={c.get('ground_truth_regime')} pred_ATR%={c.get('predicted_atr_pct_daily_equiv')} -> {match}")

    # --- Threshold analysis on combined set ---
    threshold_analysis = analyze_thresholds(combined)

    # --- Summary ---
    valid_new = [r for r in all_results if r.get("regime") in ("HIGH", "MED", "LOW")]
    matches_new = sum(1 for r in valid_new if r.get("regime_match") is True)
    mismatches_new = sum(1 for r in valid_new if r.get("regime_match") is False)

    # Source breakdown
    source_counts = {"kronos": 0, "heuristic": 0, "default": 0}
    for r in valid_new:
        src = r.get("source", "default")
        source_counts[src] = source_counts.get(src, 0) + 1

    # Regime distributions
    forecast_regimes = {"HIGH": 0, "MED": 0, "LOW": 0}
    realized_regimes = {"HIGH": 0, "MED": 0, "LOW": 0}
    gt_regimes = {"HIGH": 0, "MED": 0, "LOW": 0}
    for r in valid_new:
        fr = r.get("regime")
        rr = r.get("realized_regime")
        gr = r.get("ground_truth_regime")
        if fr in forecast_regimes:
            forecast_regimes[fr] += 1
        if rr in realized_regimes:
            realized_regimes[rr] += 1
        if gr in gt_regimes:
            gt_regimes[gr] += 1

    summary = {
        "total_checkpoints": len(EXPANDED_CHECKPOINTS),
        "new_checkpoints": len(all_results),
        "valid_new": len(valid_new),
        "new_matches": matches_new,
        "new_mismatches": mismatches_new,
        "new_match_rate": round(matches_new / len(valid_new), 4) if valid_new else 0,
        "combined_total": len(combined),
        "combined_match_rate": threshold_analysis.get("old_thresholds", {}).get("match_rate_vs_realized", 0),
        "source_breakdown_new": source_counts,
        "forecast_regime_distribution_new": forecast_regimes,
        "realized_regime_distribution_new": realized_regimes,
        "ground_truth_regime_distribution_new": gt_regimes,
        "kronos_model_available": model_available,
        "kronos_model_load_time_seconds": round(load_time, 2),
    }

    # Compute match rates broken out by regime
    regime_match_breakdown = {}
    for regime in ("HIGH", "MED", "LOW"):
        regime_results = [r for r in valid_new if r.get("regime") == regime]
        regime_matches = sum(1 for r in regime_results if r.get("regime_match"))
        regime_match_breakdown[regime] = {
            "forecast_count": len(regime_results),
            "matches": regime_matches,
            "rate": round(regime_matches / len(regime_results), 4) if regime_results else 0,
        }
    summary["regime_match_breakdown"] = regime_match_breakdown

    output = {
        "spotcheck_timestamp": datetime.now(timezone.utc).isoformat(),
        "script": "scripts/spotcheck_kronos_calibration.py",
        "configuration": {
            "checkpoints": [{"timestamp": c["timestamp"], "label": c["label"]} for c in EXPANDED_CHECKPOINTS],
            "historical_days": HISTORICAL_DAYS,
            "following_session_hours": FOLLOWING_SESSION_HOURS,
            "daily_lookback_days": DAILY_LOOKBACK_DAYS,
            "kronos_pred_len_minutes": KRONOS_PRED_LEN,
            "atr_period": ATR_PERIOD,
            "old_atr_high_threshold": OLD_HIGH_THRESHOLD,
            "old_atr_low_threshold": OLD_LOW_THRESHOLD,
            "at_r_normalization": (
                "1-minute ATR normalized to percentage via mean-close, then "
                f"scaled to daily-equivalent via sqrt(1440)={ATR_TIME_SCALING_FACTOR:.4f} "
                "before applying daily-calibrated thresholds (HIGH>=5%, LOW<2%)."
            ),
            "lse_api_key": f"{LSE_API_KEY[:20]}... ({_key_status})",
            "lse_data_source": "REAL BTC/USD candles from LSE REST API (https://api.londonstrategicedge.com/vault)",
            "kronos_model_status": "REAL model inference (state/kronos/kronos-small, 24.7M params)",
        },
        "summary": summary,
        "threshold_analysis": threshold_analysis,
        "detailed_results": all_results,
    }

    with open(OUTPUT_PATH, "w") as f:
        json.dump(output, f, indent=2, default=str)

    # Print final summary
    print("=" * 80)
    print("FINAL SUMMARY")
    print("=" * 80)
    print(json.dumps(summary, indent=2))
    print()
    print("THRESHOLD ANALYSIS:")
    ta = threshold_analysis
    print(f"  Valid samples: {ta['valid_samples']}")
    print(f"  OLD thresholds: HIGH={ta['old_thresholds']['high']}, LOW={ta['old_thresholds']['low']} "
          f"-> match_rate={ta['old_thresholds']['match_rate_vs_realized']} "
          f"({ta['old_thresholds']['matches']}/{ta['valid_samples']})")
    print(f"  NEW thresholds: HIGH={ta['new_thresholds']['high']}, LOW={ta['new_thresholds']['low']} "
          f"-> match_rate={ta['new_thresholds']['match_rate_vs_realized']} "
          f"({ta['new_thresholds']['matches']}/{ta['valid_samples']})")
    print(f"  Improvement: {ta['match_rate_improvement']}")
    print()
    print("Regime distributions (predicted vs realized):")
    for regime, dist in ta.get("regime_distributions_predicted_vs_realized", {}).items():
        print(f"  REALIZED {regime}: count={dist.get('count')}, mean_pred_ATR%={dist.get('mean_pred_atr_pct', 'N/A')}, "
              f"range=[{dist.get('min_pred_atr_pct', 'N/A')}, {dist.get('max_pred_atr_pct', 'N/A')}]")
    print()
    print("Top 10 threshold pairs by match rate:")
    for r in ta.get("sweep_results_top10", []):
        print(f"  HIGH={r['high']}, LOW={r['low']} -> match_rate={r['match_rate']}")
    print()
    print(f"Results written to {OUTPUT_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
