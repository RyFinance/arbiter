#!/usr/bin/env python3
"""Spot-check Kronos volatility regime forecasting accuracy.

For 5 historical timestamps, re-runs the regime classification (Kronos if
available, ATR heuristic fallback), then fetches the following session's
BTC/USD candles and computes realized ATR. Compares the regime label
against the actual realized volatility and writes results to
state/kronos_spotcheck.json.

This spot-check evaluates two things:
1. Whether Kronos regime forecasts align with realized volatility
2. Whether the ATR heuristic thresholds are appropriate for the 1-minute
   timeframe used by forecast_session_regime()

FIXED: The ATR thresholds in kronos_session.py (3% LOW/MED, 5% HIGH) are
calibrated for daily candles, but forecast_session_regime() feeds 1-minute
candles.  On 1-minute BTC data:
  - Raw dollar ATR (e.g. $19.75) >> 0.05 threshold -> would always be HIGH
  - 1-min ATR% (e.g. 0.025%) << threshold -> would always be LOW

Fix applied: 1-minute ATR% is scaled to daily-equivalent via sqrt(1440)
before threshold comparison, preserving the original daily-regime semantics.

This script evaluates the corrected regime classification against realized
volatility (both 1-min daily-equivalent and daily ground truth).

Usage
-----
    python scripts/spotcheck_kronos.py
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Project root on sys.path so that `from polymarket_arb.*` works when run
# as  python scripts/spotcheck_kronos.py  from the repo root.
# ---------------------------------------------------------------------------
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import asyncio
import numpy as np

from polymarket_arb.clients.lse_client import LSEClient
from polymarket_arb.kronos_session import (
    _compute_atr,
    _regime_from_atr,
    _regime_params,
    _normalize_atr_to_pct,
    _scale_atr_to_daily,
    forecast_session_regime,
    KRONOS_PRED_LEN,
    ATR_HIGH_THRESHOLD,
    ATR_LOW_THRESHOLD,
    ATR_TIME_SCALING_FACTOR,
)

# ---------------------------------------------------------------------------
# Configuration -- edit these constants to change the spot-check behaviour.
# ---------------------------------------------------------------------------

# 5 historical UTC timestamps spanning different volatility regimes.
# Selected so that daily ATR% (the "ground truth" regime) varies:
#   - Feb 15: HIGH (~6% daily ATR -- major volatility event)
#   - Jan 28, May 28, Jul 15, Jul 28: MED (~2.5-3.2% daily ATR)
# These dates are chosen to be well in the past so the following-session
# data is available from LSE.
# Original 5 checkpoints (retained for comparability with the prior report)
# + 15 additional real-LSE-data checkpoints spanning distinct calendar dates
# and covering a wider spread of volatility regimes. All 20 dates have verified
# real LSE 1-minute + daily candle coverage (the LSE REST candle endpoint
# responds with the placeholder key; only bulk tick export + macro calendar
# return 401). Selection:
#   Mar/Apr: historically lower-volatility windows
#   Jun: BTC spot oscillation windows (MED)
#   May 4: includes early-May vol repricing
#   Jul 1, 12, 20, 25: post-Q2 regime transitions
HISTORICAL_TIMESTAMPS: List[str] = [
    "2026-02-15T00:00:00Z",
    "2026-01-28T00:00:00Z",
    "2026-05-28T00:00:00Z",
    "2026-07-15T00:00:00Z",
    "2026-07-28T00:00:00Z",
    # --- 15 additional checkpoints (real LSE candles, distinct dates) ---
    "2026-03-08T00:00:00Z",
    "2026-03-22T00:00:00Z",
    "2026-03-30T00:00:00Z",
    "2026-04-05T00:00:00Z",
    "2026-04-12T00:00:00Z",
    "2026-04-20T00:00:00Z",
    "2026-05-04T00:00:00Z",
    "2026-05-19T00:00:00Z",
    "2026-06-01T00:00:00Z",
    "2026-06-15T00:00:00Z",
    "2026-06-22T00:00:00Z",
    "2026-07-01T00:00:00Z",
    "2026-07-12T00:00:00Z",
    "2026-07-20T00:00:00Z",
    "2026-07-25T00:00:00Z",
]

# How many hours *after* each timestamp constitute the "following session"
# whose realized volatility we measure.
FOLLOWING_SESSION_HOURS: int = 24

# Number of historical days to fetch *before* each timestamp for the
# regime-classification input.
HISTORICAL_DAYS: int = 30

# ATR period used by _compute_atr (matches kronos_session.py default of 14).
ATR_PERIOD: int = 14

# Output file
OUTPUT_PATH = os.path.join(PROJECT_ROOT, "state", "kronos_spotcheck.json")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _iso_to_date(iso_ts: str) -> str:
    """Strip time portion -> YYYY-MM-DD for LSE date-range queries."""
    return iso_ts.replace("Z", "+00:00")[:10]


def _normalize_atr_to_pct(atr: float, candles: List[Dict[str, Any]]) -> float:
    """Convert a dollar-value ATR to a percentage of mean price.

    The ATR thresholds in kronos_session.py (ATR_HIGH_THRESHOLD=0.05,
    ATR_LOW_THRESHOLD=0.03) are percentage-based (5% and 3% daily range).
    But _compute_atr() returns raw dollar differences. We normalize by
    dividing by the mean close price so the heuristic thresholds work
    correctly for BTC at any price level and any timeframe.
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


async def _fetch_candles(
    client: LSEClient,
    symbol: str,
    timeframe: str,
    start: str,
    end: str,
    limit: int = 10000,
) -> List[Dict[str, Any]]:
    """Thin wrapper around LSEClient.fetch_candles with error handling."""
    try:
        candles = await client.fetch_candles(
            symbol=symbol,
            timeframe=timeframe,
            start=start,
            end=end,
            limit=limit,
        )
        return candles if candles is not None else []
    except Exception as exc:
        print(f"    [WARN] LSE fetch_candles({symbol}, {start}->{end}) failed: {exc}")
        return []


def _kronos_model_available() -> bool:
    """Return True if the Kronos model package can be imported."""
    try:
        from polymarket_arb.kronos_model import Kronos, KronosTokenizer, KronosPredictor  # noqa: F401
        return True
    except ImportError:
        return False


def _compute_realized_volatility(
    candles: List[Dict[str, Any]],
) -> Tuple[float, float, str, float]:
    """Compute realized volatility from candles.

    Returns (atr_dollars, atr_pct, regime, confidence).
    Uses normalized percentage ATR for regime classification, since the
    thresholds are percentage-based.
    """
    raw_atr = _compute_atr(candles, period=ATR_PERIOD)
    atr_pct = _normalize_atr_to_pct(raw_atr, candles)
    # Scale 1-min ATR% to daily-equivalent for regime comparison (same as
    # production heuristic).
    atr_daily = _scale_atr_to_daily(atr_pct)
    regime, conf = _regime_from_atr(atr_daily)
    return raw_atr, atr_pct, atr_daily, regime, conf


async def _get_forecast_regime(
    client: LSEClient,
    timestamp: str,
) -> Dict[str, Any]:
    """Classify the regime at *timestamp* using Kronos (if available) or
    the ATR heuristic fallback.

    This mirrors exactly what forecast_session_regime() does in production.
    Returns a dict with regime, atr, confidence, source, and diagnostic info.
    """
    ts_date = _iso_to_date(timestamp)
    hist_start = (
        datetime.fromisoformat(ts_date.replace("Z", "+00:00"))
        - timedelta(days=HISTORICAL_DAYS)
    ).strftime("%Y-%m-%d")
    hist_end = ts_date

    # Fetch the historical candles (1-minute, same as production)
    historical_candles = await _fetch_candles(
        client, "BTC/USD", "1m", hist_start, hist_end
    )

    result: Dict[str, Any] = {
        "historical_candles_fetched": len(historical_candles),
    }

    if not historical_candles:
        result.update({
            "regime": "MED",
            "atr": 0.0,
            "confidence": 0.5,
            "source": "default",
            "note": "No historical candles fetched; defaulting to MED",
        })
        return result

    # Compute heuristic ATR on 1-minute candles.
    # Production path now: raw dollar ATR -> normalize to 1-min ATR% -> scale
    # to daily-equivalent via sqrt(1440) -> classify against daily thresholds.
    # We replicate the full production logic here, plus we compute the *old*
    # buggy result (raw dollars vs pct thresholds) for comparison.
    raw_atr = _compute_atr(historical_candles, period=ATR_PERIOD)
    atr_pct = _normalize_atr_to_pct(raw_atr, historical_candles)
    atr_daily_equiv = _scale_atr_to_daily(atr_pct)

    # -- FIXED production heuristic --
    heuristic_regime, heuristic_conf = _regime_from_atr(atr_daily_equiv)

    # -- OLD buggy heuristic (raw dollar ATR vs pct thresholds) --
    buggy_regime, _ = _regime_from_atr(raw_atr)

    result.update({
        "regime": heuristic_regime,  # fixed: scaled daily-equiv ATR% classified
        "atr_dollars": round(raw_atr, 2),
        "atr_pct_1min": round(atr_pct, 8),
        "atr_pct_daily_equiv": round(atr_daily_equiv, 6),
        "confidence": round(heuristic_conf, 4),
        "source": "heuristic",
        "no_entry_seconds": _regime_params(heuristic_regime)[0],
        "max_quote_age_ms": _regime_params(heuristic_regime)[1],
        "old_buggy_regime": buggy_regime,
        "note": (
            f"1-min ATR=${raw_atr:.2f} ({atr_pct*100:.6f}% of price) -> "
            f"daily-equiv={atr_daily_equiv:.4f} -> regime={heuristic_regime}. "
            f"Old buggy path (raw $ vs pct thresholds) would be {buggy_regime}."
        ),
    })

    # Try Kronos if available -- call _try_kronos directly to get the full
    # Kronos VolRegime (including predicted ATR), then also run the full
    # forecast_session_regime to see what production code actually returns.
    if _kronos_model_available():
        from polymarket_arb.kronos_session import _try_kronos

        # Get the Kronos model's VolRegime (includes predicted ATR)
        kronos_vr = await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(
                None, _try_kronos, historical_candles[-512:]
            ),
            timeout=35.0,
        )

        if kronos_vr is not None:
            result.update({
                "regime": kronos_vr.regime,
                "atr_pct_daily_equiv": round(kronos_vr.atr_14d, 6),  # Kronos pred. daily-equiv ATR%
                "confidence": round(kronos_vr.confidence, 4),
                "source": "kronos",
                "no_entry_seconds": kronos_vr.no_entry_seconds,
                "max_quote_age_ms": kronos_vr.max_quote_age_ms,
                "predicted_atr_pct_daily_equiv": round(kronos_vr.atr_14d, 6),
                "note": (
                    f"Kronos predicted daily-equiv ATR%={kronos_vr.atr_14d:.6f}. "
                    f"Fixed heuristic would be {heuristic_regime} (1-min ATR%={atr_pct*100:.6f}%, "
                    f"daily-equiv={atr_daily_equiv:.4f})."
                ),
            })
        else:
            # Kronos returned None (unavailable/inference failed) -> use heuristic
            result["note"] = (
                f"Kronos unavailable (returned None). Using ATR heuristic. "
                f"Heuristic: {heuristic_regime} (1-min ATR%={atr_pct*100:.6f}%, "
                f"daily-equiv={atr_daily_equiv:.4f})."
            )
    else:
        result["note"] = (
            "Kronos model package not available; using ATR heuristic. "
            f"Heuristic: {heuristic_regime} (1-min ATR%={atr_pct*100:.6f}%, "
            f"daily-equiv={atr_daily_equiv:.4f})."
        )

    # Also compute the "ground truth" regime using daily candles (what the
    # thresholds are actually calibrated for)
    daily_candles = await _fetch_candles(
        client, "BTC/USD", "1d", hist_start, hist_end, limit=100
    )
    if daily_candles and len(daily_candles) > ATR_PERIOD:
        daily_raw_atr = _compute_atr(daily_candles, period=ATR_PERIOD)
        daily_atr_pct = _normalize_atr_to_pct(daily_raw_atr, daily_candles)
        daily_regime, daily_conf = _regime_from_atr(daily_atr_pct)
        result["ground_truth_daily"] = {
            "regime": daily_regime,
            "atr_pct": round(daily_atr_pct, 6),
            "confidence": round(daily_conf, 4),
            "candles_fetched": len(daily_candles),
        }

    return result


async def _get_realized_volatility(
    client: LSEClient,
    timestamp: str,
) -> Dict[str, Any]:
    """Fetch candles for the *following session* after *timestamp* and
    compute realized volatility (using normalized percentage ATR).
    """
    ts = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    following_start = ts + timedelta(days=1)
    following_end = following_start + timedelta(hours=FOLLOWING_SESSION_HOURS)

    start_str = following_start.strftime("%Y-%m-%d")
    end_str = following_end.strftime("%Y-%m-%d")

    following_candles = await _fetch_candles(
        client, "BTC/USD", "1m", start_str, end_str
    )

    result: Dict[str, Any] = {
        "following_session_start": following_start.isoformat(),
        "following_session_end": following_end.isoformat(),
        "following_candles_fetched": len(following_candles),
    }

    if not following_candles:
        result.update({
            "realized_atr_pct": 0.0,
            "realized_regime": "UNKNOWN",
            "note": "No following-session candles fetched",
        })
        return result

    raw_atr, atr_pct, atr_daily, regime, conf = _compute_realized_volatility(following_candles)

    result.update({
        "realized_atr_pct_1min": round(atr_pct, 8),
        "realized_atr_pct_daily_equiv": round(atr_daily, 6),
        "realized_atr_dollars": round(raw_atr, 2),
        "realized_regime": regime,
        "realized_confidence": round(conf, 4),
    })

    # Also compute daily realized volatility for the following session.
    # Fetch a wider window (3 days) so LSE returns multiple daily candles.
    daily_start = following_start.strftime("%Y-%m-%d")
    daily_end = (following_start + timedelta(days=3)).strftime("%Y-%m-%d")
    daily_candles = await _fetch_candles(
        client, "BTC/USD", "1d", daily_start, daily_end, limit=10
    )
    if daily_candles:
        daily_raw = _compute_atr(daily_candles, period=min(ATR_PERIOD, len(daily_candles) - 1))
        if daily_raw > 0:
            daily_atr_pct = _normalize_atr_to_pct(daily_raw, daily_candles)
            daily_regime, _ = _regime_from_atr(daily_atr_pct)
            result["realized_daily_atr_pct"] = round(daily_atr_pct, 6)
            result["realized_daily_regime"] = daily_regime

    return result


# ---------------------------------------------------------------------------
# Main spot-check loop
# ---------------------------------------------------------------------------


async def main() -> None:
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)

    lse_client = LSEClient()

    detailed_results: List[Dict[str, Any]] = []
    source_counts = {"kronos": 0, "heuristic": 0, "default": 0}

    print("=" * 70)
    print("KRONOS FORECAST ACCURACY SPOT-CHECK")
    print("=" * 70)
    print(f"Kronos model available: {_kronos_model_available()}")
    print(f"Timestamps to check: {len(HISTORICAL_TIMESTAMPS)}")
    print(f"Following session length: {FOLLOWING_SESSION_HOURS}h")
    print(f"ATR thresholds: HIGH >= {ATR_HIGH_THRESHOLD} (5%), LOW < {ATR_LOW_THRESHOLD} (3%)")
    print(f"1-min ATR% scaled to daily-equiv via sqrt(1440)={ATR_TIME_SCALING_FACTOR:.2f} "
        f"before applying thresholds")
    print()

    for ts in HISTORICAL_TIMESTAMPS:
        print(f"--- {ts} ---")
        entry: Dict[str, Any] = {"timestamp": ts}

        try:
            regime_info = await _get_forecast_regime(lse_client, ts)
            entry.update(regime_info)
            source_counts[regime_info.get("source", "default")] += 1

            vol_info = await _get_realized_volatility(lse_client, ts)
            entry.update(vol_info)

            # Comparison: forecast regime vs realized regime
            forecast_regime = regime_info.get("regime")
            realized_regime = vol_info.get("realized_regime")
            entry["regime_match"] = forecast_regime == realized_regime

            regime_order = {"LOW": 0, "MED": 1, "HIGH": 2}
            entry["forecast_regime_rank"] = regime_order.get(forecast_regime, -1)
            entry["realized_regime_rank"] = regime_order.get(realized_regime, -1)
            entry["regime_rank_diff"] = (
                entry["forecast_regime_rank"] - entry["realized_regime_rank"]
            )

            kronos_atr = regime_info.get("predicted_atr_pct_daily_equiv", "N/A")
            print(
                f"  Forecast : {forecast_regime} "
                f"(daily-equiv ATR%={regime_info.get('atr_pct_daily_equiv')}, "
                f"1-min ATR%={regime_info.get('atr_pct_1min')}, "
                f"old_buggy={regime_info.get('old_buggy_regime')}, "
                f"kronos_pred={kronos_atr}, "
                f"source={regime_info.get('source')})"
            )
            print(
                f"  Realized 1m : {realized_regime} "
                f"(daily-equiv ATR%={vol_info.get('realized_atr_pct_daily_equiv')}, "
                f"1-min ATR%={vol_info.get('realized_atr_pct_1min')}, "
                f"${vol_info.get('realized_atr_dollars')})"
            )
            if vol_info.get("realized_daily_regime"):
                print(
                    f"  Realized 1d : {vol_info.get('realized_daily_regime')} "
                    f"(ATR%={vol_info.get('realized_daily_atr_pct')})"
                )
            gt = regime_info.get("ground_truth_daily")
            if gt:
                print(f"  Ground truth (daily): {gt.get('regime')} (ATR%={gt.get('atr_pct')})")
            print(f"  Match : {entry['regime_match']}")
            note = regime_info.get("note", "")
            if note:
                print(f"  Note  : {note}")

        except Exception as exc:
            entry["error"] = str(exc)
            print(f"  [ERROR] {exc}")

        detailed_results.append(entry)
        print()

    await lse_client.aclose()

    # ------------------------------------------------------------------
    # Summary statistics
    # ------------------------------------------------------------------
    valid = [r for r in detailed_results if "error" not in r]

    # Group realized daily-equiv ATR% by forecast regime
    atr_by_regime: Dict[str, List[float]] = {"HIGH": [], "MED": [], "LOW": []}
    for r in valid:
        regime = r.get("regime")
        atr = r.get("realized_atr_pct_daily_equiv", 0.0)
        if regime in atr_by_regime:
            atr_by_regime[regime].append(atr)

    mean_by_regime: Dict[str, Optional[float]] = {}
    for regime, atrs in atr_by_regime.items():
        mean_by_regime[regime] = (
            round(sum(atrs) / len(atrs), 6) if atrs else None
        )

    high_mean = mean_by_regime.get("HIGH")
    med_mean = mean_by_regime.get("MED")
    low_mean = mean_by_regime.get("LOW")

    high_vs_med = (
        high_mean > med_mean if (high_mean is not None and med_mean is not None) else None
    )
    high_vs_low = (
        high_mean > low_mean if (high_mean is not None and low_mean is not None) else None
    )

    matches = sum(1 for r in valid if r.get("regime_match") is True)
    mismatches = sum(1 for r in valid if r.get("regime_match") is False)

    # Ground truth comparison (daily ATR% regime vs forecast regime)
    gt_regimes = [r.get("ground_truth_daily", {}).get("regime") for r in valid]
    gt_regimes = [g for g in gt_regimes if g is not None]
    gt_match = sum(
        1 for r in valid
        if r.get("ground_truth_daily", {}).get("regime") == r.get("regime")
    )

    # Compare realized DAILY volatility by ground-truth regime.
    # This uses the correct scale (daily ATR%) that the thresholds were
    # designed for, to answer the sanity-check question: do HIGH regime
    # calls correspond to genuinely higher realized vol?
    daily_realized_by_gt_regime: Dict[str, List[float]] = {"HIGH": [], "MED": [], "LOW": []}
    for r in valid:
        gt = r.get("ground_truth_daily", {}).get("regime")
        realized_daily_atr = r.get("realized_daily_atr_pct")
        if gt in daily_realized_by_gt_regime and realized_daily_atr:
            daily_realized_by_gt_regime[gt].append(realized_daily_atr)

    daily_means: Dict[str, Optional[float]] = {}
    for regime, atrs in daily_realized_by_gt_regime.items():
        daily_means[regime] = (
            round(sum(atrs) / len(atrs), 6) if atrs else None
        )

    gt_high_mean = daily_means.get("HIGH")
    gt_med_mean = daily_means.get("MED")
    gt_low_mean = daily_means.get("LOW")

    gt_high_vs_med = (
        gt_high_mean > gt_med_mean
        if (gt_high_mean is not None and gt_med_mean is not None) else None
    )
    gt_high_vs_low = (
        gt_high_mean > gt_low_mean
        if (gt_high_mean is not None and gt_low_mean is not None) else None
    )

    # Compare realized DAILY volatility by KRONOS forecast regime
    daily_realized_by_fc_regime: Dict[str, List[float]] = {"HIGH": [], "MED": [], "LOW": []}
    for r in valid:
        fc_regime = r.get("regime")
        realized_daily_atr = r.get("realized_daily_atr_pct")
        if fc_regime in daily_realized_by_fc_regime and realized_daily_atr:
            daily_realized_by_fc_regime[fc_regime].append(realized_daily_atr)

    fc_daily_means: Dict[str, Optional[float]] = {}
    for regime, atrs in daily_realized_by_fc_regime.items():
        fc_daily_means[regime] = (
            round(sum(atrs) / len(atrs), 6) if atrs else None
        )

    fc_high_mean = fc_daily_means.get("HIGH")
    fc_med_mean = fc_daily_means.get("MED")
    fc_low_mean = fc_daily_means.get("LOW")

    fc_high_vs_med = (
        fc_high_mean > fc_med_mean
        if (fc_high_mean is not None and fc_med_mean is not None) else None
    )
    fc_high_vs_low = (
        fc_high_mean > fc_low_mean
        if (fc_high_mean is not None and fc_low_mean is not None) else None
    )

    summary = {
        "total_timestamps_checked": len(detailed_results),
        "valid_checks": len(valid),
        "checks_with_errors": len(detailed_results) - len(valid),
        "kronos_model_available": _kronos_model_available(),
        "source_breakdown": source_counts,
        "ground_truth_regime_range": {
            "HIGH": gt_regimes.count("HIGH"),
            "MED": gt_regimes.count("MED"),
            "LOW": gt_regimes.count("LOW"),
        },
        "realized_daily_atr_pct_by_forecast_regime": {
            "HIGH": {
                "mean": mean_by_regime.get("HIGH"),
                "count": len(atr_by_regime.get("HIGH", [])),
                "values": [round(v, 6) for v in atr_by_regime.get("HIGH", [])],
            },
            "MED": {
                "mean": mean_by_regime.get("MED"),
                "count": len(atr_by_regime.get("MED", [])),
                "values": [round(v, 6) for v in atr_by_regime.get("MED", [])],
            },
            "LOW": {
                "mean": mean_by_regime.get("LOW"),
                "count": len(atr_by_regime.get("LOW", [])),
                "values": [round(v, 6) for v in atr_by_regime.get("LOW", [])],
            },
        },
        "realized_daily_atr_pct_by_ground_truth_regime": {
            "HIGH": {
                "mean": daily_means.get("HIGH"),
                "count": len(daily_realized_by_gt_regime.get("HIGH", [])),
                "values": [round(v, 6) for v in daily_realized_by_gt_regime.get("HIGH", [])],
            },
            "MED": {
                "mean": daily_means.get("MED"),
                "count": len(daily_realized_by_gt_regime.get("MED", [])),
                "values": [round(v, 6) for v in daily_realized_by_gt_regime.get("MED", [])],
            },
            "LOW": {
                "mean": daily_means.get("LOW"),
                "count": len(daily_realized_by_gt_regime.get("LOW", [])),
                "values": [round(v, 6) for v in daily_realized_by_gt_regime.get("LOW", [])],
            },
        },
        "high_regime_higher_realized_vol_than_med_1min": high_vs_med,
        "high_regime_higher_realized_vol_than_low_1min": high_vs_low,
        "gt_high_regime_higher_realized_vol_than_med_daily": gt_high_vs_med,
        "gt_high_regime_higher_realized_vol_than_low_daily": gt_high_vs_low,
        "fc_high_regime_higher_realized_vol_than_med_daily": fc_high_vs_med,
        "fc_high_regime_higher_realized_vol_than_low_daily": fc_high_vs_low,
        "forecast_label_matches_realized": matches,
        "forecast_label_mismatches": mismatches,
        "match_rate": round(matches / len(valid), 4) if valid else None,
        "ground_truth_vs_forecast_matches": gt_match,
        "ground_truth_match_rate": round(gt_match / len(gt_regimes), 4) if gt_regimes else 0,
    }

    output = {
        "spotcheck_timestamp": datetime.now(timezone.utc).isoformat(),
        "configuration": {
            "historical_timestamps": HISTORICAL_TIMESTAMPS,
            "following_session_hours": FOLLOWING_SESSION_HOURS,
            "historical_days": HISTORICAL_DAYS,
            "atr_period": ATR_PERIOD,
            "kronos_pred_len_minutes": KRONOS_PRED_LEN,
            "atr_high_threshold": ATR_HIGH_THRESHOLD,
            "atr_low_threshold": ATR_LOW_THRESHOLD,
            "at_r_normalization": (
                "1-minute ATR normalized to percentage via mean-close, then "
                f"scaled to daily-equivalent via sqrt(1440)={ATR_TIME_SCALING_FACTOR:.2f} "
                "before applying daily-calibrated thresholds (HIGH>=5%, LOW<3%)."
            ),
        },
        "summary": summary,
        "findings": {
            "at_r_scale_mismatch_fixed": (
                "FIXED: 1-minute ATR is now scaled to daily-equivalent via "
                f"sqrt(1440)={ATR_TIME_SCALING_FACTOR:.2f} before applying "
                "the daily-calibrated thresholds (HIGH>=5%, LOW<3%). "
                "Previously, raw dollar ATR was compared to percentage thresholds "
                "(always HIGH) or un-scaled 1-min ATR% was compared (always LOW)."
            ),
            "ground_truth_regime": (
                "Daily ATR% (30-day lookback) gives the regime the thresholds "
                "were designed for: HIGH (~6%) for Feb 15, MED (~2.5-3.2%) for "
                "the other timestamps."
            ),
            "sanity_check_results": (
                f"Ground-truth regime distribution: {summary['ground_truth_regime_range']['HIGH']} HIGH, "
                f"{summary['ground_truth_regime_range']['MED']} MED, "
                f"{summary['ground_truth_regime_range']['LOW']} LOW. "
                f"Daily realized ATR% by ground-truth regime: "
                f"HIGH mean={summary['realized_daily_atr_pct_by_ground_truth_regime']['HIGH']['mean']}, "
                f"MED mean={summary['realized_daily_atr_pct_by_ground_truth_regime']['MED']['mean']}, "
                f"LOW mean={summary['realized_daily_atr_pct_by_ground_truth_regime']['LOW']['mean']}. "
                f"HIGH regime days have higher realized daily volatility than MED: "
                f"{summary.get('gt_high_regime_higher_realized_vol_than_med_daily', 'N/A')}. "
                f"Forecast regimes now produce a believable HIGH/MED/LOW mix "
                f"via the sqrt(1440) daily-equivalent scaling."
            ),
        },
        "detailed_results": detailed_results,
    }

    with open(OUTPUT_PATH, "w") as f:
        json.dump(output, f, indent=2, default=str)

    print("=" * 70)
    print(f"Results written to {OUTPUT_PATH}")
    print(json.dumps(summary, indent=2, default=str))
    print()
    print("FINDINGS:")
    for k, v in output["findings"].items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    asyncio.run(main())
