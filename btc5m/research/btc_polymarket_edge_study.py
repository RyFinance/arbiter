#!/usr/bin/env python3
"""Leakage-aware BTC direction study for Polymarket Up/Down markets.

This script deliberately separates development from the final holdout:

    python research/btc_polymarket_edge_study.py dev
    python research/btc_polymarket_edge_study.py holdout

`dev` evaluates a preregistered signal library only on anchored walk-forward
folds ending before 2026-05-01, then writes one locked configuration per
horizon. `holdout` refuses to run without that lock and evaluates only those
four configurations on the final period.

The event labels are joined to a frozen cache of terminal Polymarket outcomes
retrieved from Gamma/CLOB metadata. Binance one-minute endpoint labels are
retained separately as a settlement-sensitivity audit, because they cannot
reconstruct Chainlink's 60-second TWAP.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd
from scipy.special import expit
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.pipeline import make_pipeline
from sklearn.base import BaseEstimator, TransformerMixin


ROOT = Path(__file__).resolve().parents[1]
DATA_PATH = ROOT / "state" / "btc_1m_candles_real_2025_2026.parquet"
OUTCOME_PATH = ROOT / "state" / "polymarket_btc_updown_resolved_outcomes.parquet"
# v3 was a proxy-only diagnostic and is intentionally preserved as a rejected
# audit artifact. v4 uses terminal Polymarket outcomes and hardened locking.
OUT_DIR = ROOT / "research" / "btc_polymarket_edge_out_v4"
DEV_RESULTS_PATH = OUT_DIR / "development_results.json"
LOCK_PATH = OUT_DIR / "locked_configs.json"
HOLDOUT_RESULTS_PATH = OUT_DIR / "holdout_results.json"

HOLDOUT_START = pd.Timestamp("2026-05-01T00:00:00Z")
HORIZONS = (5, 15, 60, 240)
FEE_RATE = 0.07
PRIMARY_ENTRY_PRICE = 0.50
ENTRY_PRICES = (0.35, 0.40, 0.45, 0.475, 0.50, 0.525, 0.55, 0.60, 0.65)
RANDOM_SEED = 20260826

# Five anchored development tests. The horizon-length purge is applied by
# requiring training outcomes to end no later than each validation start.
DEV_FOLDS = (
    ("2025-07-01", "2025-09-01"),
    ("2025-09-01", "2025-11-01"),
    ("2025-11-01", "2026-01-01"),
    ("2026-01-01", "2026-03-01"),
    ("2026-03-01", "2026-05-01"),
)

MIN_TOTAL_TRADES = {5: 1_000, 15: 500, 60: 250, 240: 100}
MIN_FOLD_TRADES = {5: 100, 15: 60, 60: 30, 240: 15}


@dataclass(frozen=True)
class Candidate:
    name: str
    family: str
    kind: str
    params: dict[str, Any]


class TrainingQuantileClipper(BaseEstimator, TransformerMixin):
    """Clip each feature to train-only tails before a numerical ML fit.

    This is fitted inside each chronological training fold through sklearn's
    pipeline, so it cannot use test or holdout feature distributions.
    """

    def __init__(self, lower: float = 0.001, upper: float = 0.999):
        self.lower = lower
        self.upper = upper

    def fit(self, x: np.ndarray, y: np.ndarray | None = None):
        x = np.asarray(x, dtype=float)
        self.lower_bounds_ = np.nanquantile(x, self.lower, axis=0)
        self.upper_bounds_ = np.nanquantile(x, self.upper, axis=0)
        return self

    def transform(self, x: np.ndarray) -> np.ndarray:
        return np.clip(np.asarray(x, dtype=float), self.lower_bounds_, self.upper_bounds_)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def fee_per_share(p: float) -> float:
    return FEE_RATE * p * (1.0 - p)


def breakeven_probability(p: float) -> float:
    return p + fee_per_share(p)


def maximum_ask(q: float) -> float:
    """Smaller root of q = p + r p (1-p), clipped to the unit interval."""
    q = float(np.clip(q, 0.0, 1.0))
    disc = (1.0 + FEE_RATE) ** 2 - 4.0 * FEE_RATE * q
    return float(((1.0 + FEE_RATE) - math.sqrt(max(disc, 0.0))) / (2.0 * FEE_RATE))


def _json_value(value: Any) -> Any:
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return None if not np.isfinite(value) else float(value)
    if isinstance(value, (pd.Timestamp,)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(v) for v in value]
    return value


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_json_value(payload), indent=2, sort_keys=True) + "\n")


def canonical_sha256(payload: dict[str, Any]) -> str:
    """Stable audit fingerprint for a fully specified experimental object."""
    encoded = json.dumps(_json_value(payload), sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_and_validate() -> tuple[pd.DataFrame, dict[str, Any]]:
    df = pd.read_parquet(DATA_PATH).sort_values("timestamp").reset_index(drop=True)
    required = {"timestamp", "open", "high", "low", "close", "volume"}
    if set(df.columns) != required:
        raise ValueError(f"unexpected columns: {list(df.columns)}")
    if str(df["timestamp"].dtype).find("UTC") < 0:
        raise ValueError("timestamp must be timezone-aware UTC")
    if not df["timestamp"].is_monotonic_increasing:
        raise ValueError("timestamps are not sorted")
    if df["timestamp"].duplicated().any():
        raise ValueError("duplicate timestamps")
    if df.isna().any().any():
        raise ValueError("nulls in source candles")
    invalid_ohlc = (
        (df["low"] > df[["open", "close", "high"]].min(axis=1))
        | (df["high"] < df[["open", "close", "low"]].max(axis=1))
        | (df[["open", "high", "low", "close"]] <= 0).any(axis=1)
        | (df["volume"] <= 0)
    )
    if invalid_ohlc.any():
        raise ValueError(f"{int(invalid_ohlc.sum())} invalid OHLCV rows")
    diffs = df["timestamp"].diff().dt.total_seconds().div(60.0)
    gaps = diffs[diffs > 1.0]
    integrity = {
        "path": str(DATA_PATH),
        "sha256": sha256(DATA_PATH),
        "rows": len(df),
        "first_timestamp": df["timestamp"].iloc[0],
        "last_timestamp": df["timestamp"].iloc[-1],
        "duplicate_timestamps": int(df["timestamp"].duplicated().sum()),
        "gap_events": int(len(gaps)),
        "missing_minutes": int((gaps - 1.0).sum()),
        "flat_candles": int(
            (df["open"].eq(df["high"]) & df["high"].eq(df["low"]) & df["low"].eq(df["close"])).sum()
        ),
        "invalid_ohlcv_rows": int(invalid_ohlc.sum()),
    }
    df = df.set_index("timestamp")
    return df, integrity


def load_resolved_outcomes() -> tuple[pd.DataFrame, dict[str, Any]]:
    """Load terminal Polymarket labels, excluding unresolved/ambiguous rows.

    The downloader stores one row per event and maps the winning outcome to
    ``up_label`` (1=Up, 0=Down).  We require a decisive binary label and a
    unique product/start key before allowing a row into model evaluation.
    """
    if not OUTCOME_PATH.exists():
        raise FileNotFoundError(f"missing resolved outcome cache: {OUTCOME_PATH}")
    out = pd.read_parquet(OUTCOME_PATH).copy()
    required = {"product", "start", "end", "up_label", "condition_id", "event_slug"}
    missing = required.difference(out.columns)
    if missing:
        raise ValueError(f"outcome cache missing columns: {sorted(missing)}")
    out["start"] = pd.to_datetime(out["start"], utc=True)
    out["end"] = pd.to_datetime(out["end"], utc=True)
    out["up_label"] = pd.to_numeric(out["up_label"], errors="coerce")
    if out["start"].isna().any() or out["end"].isna().any():
        raise ValueError("null outcome timestamps")
    rows_total = len(out)
    decisive = out["up_label"].isin([0.0, 1.0])
    if (~decisive & out["up_label"].notna()).any():
        raise ValueError("non-binary outcome labels")
    # Keep ambiguous rows in the audit counts, but never use them as labels.
    # Gamma occasionally exposes duplicate hourly listings (old/new slug) for
    # the same candle. Collapse only when their terminal labels and window
    # endpoints agree; conflicting duplicates are unsafe and abort the run.
    decisive_out = out.loc[decisive, ["product", "start", "end", "up_label", "condition_id", "event_slug"]].copy()
    grouped = decisive_out.groupby(["product", "start"], sort=False)
    if (grouped["up_label"].nunique() > 1).any() or (grouped["end"].nunique() > 1).any():
        raise ValueError("conflicting duplicate product/start outcome rows")
    duplicate_rows = len(decisive_out) - len(grouped.size())
    out = decisive_out.sort_values(["product", "start", "condition_id"]).drop_duplicates(
        ["product", "start"], keep="first"
    )
    out["up_label"] = out["up_label"].astype(np.int8)
    out = out.sort_values(["product", "start"]).reset_index(drop=True)
    audit = {
        "path": str(OUTCOME_PATH),
        "sha256": sha256(OUTCOME_PATH),
        "rows_total_in_cache": int(rows_total),
        "rows_decisive": int(len(out)),
        "duplicate_decisive_rows_collapsed": int(duplicate_rows),
        "rows_by_product": {str(k): int(v) for k, v in out.groupby("product").size().items()},
        "label_definition": "terminal Polymarket winner: Up=1, Down=0; ambiguous/nonterminal rows excluded",
    }
    return out, audit


def add_minute_features(raw: pd.DataFrame) -> pd.DataFrame:
    f = raw.copy()
    logc = np.log(f["close"])
    ret = logc.diff()
    f["ret_1"] = ret
    for n in (3, 5, 15, 30, 60, 240):
        f[f"mom_{n}"] = logc - logc.shift(n)
    f["mom_1"] = ret

    for n in (15, 60, 240):
        f[f"rv_{n}"] = np.sqrt(ret.pow(2).rolling(n, min_periods=n).sum())
    sigma_1m = f["rv_60"] / math.sqrt(60.0)
    f["body"] = np.log(f["close"] / f["open"])
    f["range"] = np.log(f["high"] / f["low"])
    denom = (f["high"] - f["low"]).replace(0.0, np.nan)
    f["clv"] = ((2.0 * f["close"] - f["high"] - f["low"]) / denom).fillna(0.0)
    upper = f["high"] - np.maximum(f["open"], f["close"])
    lower = np.minimum(f["open"], f["close"]) - f["low"]
    f["wick_asym"] = ((lower - upper) / (f["high"] - f["low"]).replace(0.0, np.nan)).fillna(0.0)
    f["body_z"] = f["body"] / sigma_1m.replace(0.0, np.nan)
    f["ret_z_1"] = f["ret_1"] / sigma_1m.replace(0.0, np.nan)
    for n in (5, 15, 60):
        expected_scale = sigma_1m * math.sqrt(float(n))
        f[f"mom_z_{n}"] = f[f"mom_{n}"] / expected_scale.replace(0.0, np.nan)

    sign_body = np.sign(f["body"])
    signed_volume = sign_body * f["volume"]
    clv_volume = f["clv"] * f["volume"]
    for n in (5, 15, 60):
        total_volume = f["volume"].rolling(n, min_periods=n).sum()
        f[f"svi_{n}"] = signed_volume.rolling(n, min_periods=n).sum() / total_volume
        f[f"clvi_{n}"] = clv_volume.rolling(n, min_periods=n).sum() / total_volume

    vol_mean = f["volume"].rolling(60, min_periods=60).mean().shift(1)
    f["volume_surprise"] = np.log(f["volume"] / vol_mean.replace(0.0, np.nan))
    f["rv_ratio"] = f["rv_15"] / f["rv_240"].replace(0.0, np.nan)
    f["vol_of_vol"] = f["ret_1"].abs().rolling(60, min_periods=60).std()
    f["price_z_60"] = (logc - logc.rolling(60, min_periods=60).mean()) / logc.rolling(
        60, min_periods=60
    ).std()
    f["price_z_240"] = (logc - logc.rolling(240, min_periods=240).mean()) / logc.rolling(
        240, min_periods=240
    ).std()
    f["positive_frac_15"] = (ret > 0).rolling(15, min_periods=15).mean() - 0.5
    f["positive_frac_60"] = (ret > 0).rolling(60, min_periods=60).mean() - 0.5
    f["ret_acf_15"] = ret.rolling(15, min_periods=15).corr(ret.shift(1))
    f["ret_acf_60"] = ret.rolling(60, min_periods=60).corr(ret.shift(1))
    f["amihud_15"] = (ret.abs() / f["volume"]).rolling(15, min_periods=15).mean()

    # Causal exponentially weighted scores. At an event start t, the sampled
    # feature row is t-1m, whose candle has just completed.
    f["ewm_5"] = ret.ewm(halflife=5, adjust=False, min_periods=20).mean()
    f["ewm_15"] = ret.ewm(halflife=15, adjust=False, min_periods=45).mean()

    # Endpoint proxies used only for labels/sensitivity, never as future features.
    f["endpoint_ohlc4"] = (f["open"] + f["high"] + f["low"] + f["close"]) / 4.0
    f["endpoint_hlc3"] = (f["high"] + f["low"] + f["close"]) / 3.0
    return f


def four_hour_starts(first: pd.Timestamp, last: pd.Timestamp) -> pd.DatetimeIndex:
    first_local_day = first.tz_convert("America/New_York").normalize().tz_localize(None)
    last_local_day = last.tz_convert("America/New_York").normalize().tz_localize(None)
    naive: list[pd.Timestamp] = []
    for day in pd.date_range(first_local_day, last_local_day, freq="D"):
        for hour in (0, 4, 8, 12, 16, 20):
            naive.append(day + pd.Timedelta(hours=hour))
    # 00/04/08/... avoid DST's ambiguous/nonexistent 02:00 hour.
    return pd.DatetimeIndex(naive).tz_localize("America/New_York").tz_convert("UTC")


def aligned_starts(raw: pd.DataFrame, horizon: int) -> pd.DatetimeIndex:
    first = raw.index.min()
    last_exclusive = raw.index.max() + pd.Timedelta(minutes=1)
    if horizon == 240:
        starts = four_hour_starts(first, last_exclusive)
    else:
        starts = pd.date_range(first.ceil(f"{horizon}min"), last_exclusive, freq=f"{horizon}min", tz="UTC")
    ends = starts + pd.Timedelta(minutes=horizon)
    return starts[(starts >= first) & (ends <= last_exclusive)]


def make_events(
    raw: pd.DataFrame,
    features: pd.DataFrame,
    horizon: int,
    outcomes: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    starts = aligned_starts(raw, horizon)
    minute = pd.Timedelta(minutes=1)
    ends = starts + pd.Timedelta(minutes=horizon)
    start_pos = raw.index.get_indexer(starts)
    end_pos = raw.index.get_indexer(ends - minute)
    feature_times = starts - minute
    feature_pos = features.index.get_indexer(feature_times)

    # A complete market window has both endpoints and exactly H contiguous
    # minute candles between them. This catches an interior gap without the
    # O(number of events x horizon) membership loop used in the first draft.
    complete = (start_pos >= 0) & (end_pos >= 0) & ((end_pos - start_pos) == horizon - 1)
    incomplete = int((~complete).sum())
    usable = complete & (feature_pos >= 0)
    starts = starts[usable]
    ends = ends[usable]
    start_pos = start_pos[usable]
    end_pos = end_pos[usable]
    feature_pos = feature_pos[usable]

    feat = features.iloc[feature_pos][MODEL_FEATURES].reset_index(drop=True)
    start_raw = raw.iloc[start_pos].reset_index(drop=True)
    end_raw = raw.iloc[end_pos].reset_index(drop=True)
    start_proxy = features.iloc[feature_pos].reset_index(drop=True)
    end_proxy = features.iloc[end_pos].reset_index(drop=True)
    events = feat.copy()
    events.insert(0, "start", starts)
    events.insert(1, "end", ends)
    events.insert(2, "feature_time", feature_times[usable])
    if horizon == 60:
        # The current hourly contract resolves exactly on finalized Binance
        # BTCUSDT 1H candle close >= open.
        start_price = start_raw["open"].to_numpy(float)
        end_price = end_raw["close"].to_numpy(float)
        y_proxy_primary = (end_price >= start_price).astype(np.int8)
        y_close = y_proxy_primary.copy()
        y_hlc3 = y_proxy_primary.copy()
        y_symmetric = y_proxy_primary.copy()
        proxy_resolution = "binance_1h_exact_proxy"
    else:
        # Current 5m/15m/4h contracts use a trailing 60-second Chainlink
        # BTC/USD TWAP. One-minute Binance OHLC cannot reconstruct it. The
        # primary proxy compares the actual Binance opening print at W to an
        # OHLC4 proxy for the final minute, avoiding an artificial dependence
        # between the feature candle W-1 and a start-side OHLC average. Keep
        # alternate endpoint definitions for an explicit sensitivity audit.
        start_price = start_raw["open"].to_numpy(float)
        end_price = end_proxy["endpoint_ohlc4"].to_numpy(float)
        y_proxy_primary = (end_price >= start_price).astype(np.int8)
        y_close = (
            end_raw["close"].to_numpy(float) >= start_raw["open"].to_numpy(float)
        ).astype(np.int8)
        y_hlc3 = (
            end_proxy["endpoint_hlc3"].to_numpy(float) >= start_raw["open"].to_numpy(float)
        ).astype(np.int8)
        y_symmetric = (
            end_proxy["endpoint_ohlc4"].to_numpy(float) >= start_proxy["endpoint_ohlc4"].to_numpy(float)
        ).astype(np.int8)
        proxy_resolution = "binance_open_to_final_1m_ohlc4_proxy_for_chainlink_60s_twap"
    events["y"] = y_proxy_primary
    events["y_proxy_primary"] = y_proxy_primary
    events["y_close_proxy"] = y_close
    events["y_hlc3_proxy"] = y_hlc3
    events["y_symmetric_proxy"] = y_symmetric
    events["proxy_return_bps"] = 10_000.0 * np.log(end_price / start_price)
    events["hour"] = starts.hour
    events["dow"] = starts.dayofweek
    events["minute"] = starts.minute
    events["hour_of_week"] = starts.dayofweek * 24 + starts.hour
    events = events.sort_values("start").reset_index(drop=True)
    events = events.replace([np.inf, -np.inf], np.nan).dropna(subset=MODEL_FEATURES).reset_index(drop=True)
    events_before_outcome_join = len(events)
    outcome_joined = 0
    outcome_label_disagreement = None
    if outcomes is not None:
        product = {5: "5m", 15: "15m", 60: "1h", 240: "4h"}[horizon]
        om = outcomes[outcomes["product"].eq(product)][["start", "end", "up_label", "condition_id", "event_slug"]].copy()
        # The cache has already been checked for duplicate decisive keys; the
        # explicit merge validation protects against accidental event-key drift.
        events = events.merge(om, on=["start", "end"], how="inner", validate="one_to_one")
        outcome_joined = len(events)
        outcome_label_disagreement = float(
            (events["up_label"].to_numpy(np.int8) != events["y_proxy_primary"].to_numpy(np.int8)).mean()
        ) if len(events) else float("nan")
        events["y"] = events["up_label"].astype(np.int8)
        events = events.drop(columns=["up_label"])
        resolution = "terminal_polymarket_outcome"
    else:
        resolution = proxy_resolution
    label_audit = {
        "horizon_minutes": horizon,
        "resolution_label": resolution,
        "complete_featured_events_before_outcome_join": events_before_outcome_join,
        "complete_featured_events": len(events),
        "incomplete_aligned_windows_dropped": incomplete,
        "outcome_rows_joined": outcome_joined,
        "outcome_label_disagreement_vs_binance_primary_proxy": outcome_label_disagreement,
        "primary_vs_close_proxy_disagreement": float((events["y_proxy_primary"] != events["y_close_proxy"]).mean()),
        "primary_vs_hlc3_proxy_disagreement": float((events["y_proxy_primary"] != events["y_hlc3_proxy"]).mean()),
        "primary_vs_symmetric_proxy_disagreement": float((events["y_proxy_primary"] != events["y_symmetric_proxy"]).mean()),
        "within_1bp_of_proxy_boundary": float((events["proxy_return_bps"].abs() <= 1.0).mean()),
        "within_2bp_of_proxy_boundary": float((events["proxy_return_bps"].abs() <= 2.0).mean()),
        "within_5bp_of_proxy_boundary": float((events["proxy_return_bps"].abs() <= 5.0).mean()),
    }
    return events, label_audit


MODEL_FEATURES = [
    "mom_1",
    "mom_3",
    "mom_5",
    "mom_15",
    "mom_30",
    "mom_60",
    "mom_240",
    "ret_z_1",
    "mom_z_5",
    "mom_z_15",
    "mom_z_60",
    "ewm_5",
    "ewm_15",
    "rv_15",
    "rv_60",
    "rv_240",
    "rv_ratio",
    "vol_of_vol",
    "body",
    "range",
    "clv",
    "wick_asym",
    "body_z",
    "svi_5",
    "svi_15",
    "svi_60",
    "clvi_5",
    "clvi_15",
    "clvi_60",
    "volume_surprise",
    "price_z_60",
    "price_z_240",
    "positive_frac_15",
    "positive_frac_60",
    "ret_acf_15",
    "ret_acf_60",
    "amihud_15",
]


def build_candidates() -> list[Candidate]:
    out: list[Candidate] = []

    autocorr_scores = ["mom_1", "mom_3", "mom_5", "mom_15", "mom_30", "mom_60", "mom_240", "ewm_5", "ewm_15"]
    for score in autocorr_scores:
        for direction, sign in (("momentum", 1), ("reversal", -1)):
            for q in (0.0, 0.80, 0.90, 0.95):
                out.append(
                    Candidate(
                        f"autocorr:{score}:{direction}:q{q:.2f}",
                        "return_autocorrelation",
                        "score",
                        {"score": score, "sign": sign, "abs_quantile": q, "regime": "all", "modifier": "all"},
                    )
                )

    for score in ("svi_5", "svi_15", "svi_60", "clvi_5", "clvi_15", "clvi_60"):
        for direction, sign in (("continuation", 1), ("reversal", -1)):
            for q in (0.0, 0.80, 0.90):
                out.append(
                    Candidate(
                        f"ohlcv_flow:{score}:{direction}:q{q:.2f}",
                        "ohlcv_order_flow_proxy",
                        "score",
                        {"score": score, "sign": sign, "abs_quantile": q, "regime": "all", "modifier": "all"},
                    )
                )

    for score in ("clv", "wick_asym", "body_z", "price_z_60", "price_z_240"):
        for direction, sign in (("continuation", 1), ("reversal", -1)):
            for q in (0.0, 0.80, 0.90):
                out.append(
                    Candidate(
                        f"shape_breakout:{score}:{direction}:q{q:.2f}",
                        "candle_shape_breakout",
                        "score",
                        {"score": score, "sign": sign, "abs_quantile": q, "regime": "all", "modifier": "all"},
                    )
                )

    for score in ("mom_1", "mom_5", "svi_15"):
        for direction, sign in (("continuation", 1), ("reversal", -1)):
            for regime in ("low", "high"):
                for q in (0.0, 0.80):
                    out.append(
                        Candidate(
                            f"vol_regime:{score}:{direction}:{regime}:q{q:.2f}",
                            "volatility_regime_interaction",
                            "score",
                            {"score": score, "sign": sign, "abs_quantile": q, "regime": regime, "modifier": "all"},
                        )
                    )

    for score in ("mom_1", "mom_5"):
        for direction, sign in (("continuation", 1), ("reversal", -1)):
            for q in (0.90, 0.95):
                out.append(
                    Candidate(
                        f"large_move:{score}:{direction}:q{q:.2f}:high_volume",
                        "large_single_move_reaction",
                        "score",
                        {"score": score, "sign": sign, "abs_quantile": q, "regime": "all", "modifier": "high_volume"},
                    )
                )

    for group_col in ("hour", "minute", "dow", "hour_of_week"):
        for edge in (0.0, 0.005, 0.010, 0.020):
            out.append(
                Candidate(
                    f"seasonal:{group_col}:edge{edge:.3f}",
                    "time_of_day_session",
                    "seasonal",
                    {"group": group_col, "min_edge": edge},
                )
            )

    out.append(Candidate("baseline:expanding_majority", "unconditional_baseline", "majority", {}))

    for leaf_nodes, l2 in ((7, 1.0), (15, 3.0)):
        for q in (0.0, 0.50, 0.80, 0.90):
            out.append(
                Candidate(
                    f"shallow_hgb:leaves{leaf_nodes}:l2{l2}:q{q:.2f}",
                    "shallow_boosted_tree",
                    "hgb",
                    {"max_leaf_nodes": leaf_nodes, "l2": l2, "confidence_quantile": q},
                )
            )
    return out


def score_decision(candidate: Candidate, train: pd.DataFrame, test: pd.DataFrame) -> np.ndarray:
    p = candidate.params
    values_train = train[p["score"]].to_numpy(float)
    values_test = test[p["score"]].to_numpy(float)
    threshold = -np.inf if p["abs_quantile"] <= 0 else float(np.nanquantile(np.abs(values_train), p["abs_quantile"]))
    trade = np.abs(values_test) >= threshold

    if p.get("regime") in ("low", "high"):
        lo, hi = np.nanquantile(train["rv_ratio"], [1.0 / 3.0, 2.0 / 3.0])
        if p["regime"] == "low":
            trade &= test["rv_ratio"].to_numpy(float) <= lo
        else:
            trade &= test["rv_ratio"].to_numpy(float) >= hi

    if p.get("modifier") == "high_volume":
        vcut = float(np.nanquantile(train["volume_surprise"], 0.75))
        trade &= test["volume_surprise"].to_numpy(float) >= vcut

    decision = p["sign"] * np.sign(values_test)
    decision[~trade] = 0
    return decision.astype(np.int8)


def seasonal_decision(candidate: Candidate, train: pd.DataFrame, test: pd.DataFrame) -> np.ndarray:
    group = candidate.params["group"]
    # Beta(20,20) shrinkage prevents tiny calendar bins from becoming certain.
    agg = train.groupby(group)["y"].agg(["sum", "count"])
    prob = (agg["sum"] + 20.0) / (agg["count"] + 40.0)
    mapped = test[group].map(prob).fillna(0.5).to_numpy(float)
    decision = np.sign(mapped - 0.5)
    decision[np.abs(mapped - 0.5) < candidate.params["min_edge"]] = 0
    return decision.astype(np.int8)


def majority_decision(train: pd.DataFrame, test: pd.DataFrame) -> np.ndarray:
    side = 1 if train["y"].mean() >= 0.5 else -1
    return np.full(len(test), side, dtype=np.int8)


def fit_model(kind: str, params: dict[str, Any], train: pd.DataFrame):
    x = train[MODEL_FEATURES].to_numpy(float)
    y = train["y"].to_numpy(int)
    if kind == "hgb":
        model = make_pipeline(
            TrainingQuantileClipper(),
            HistGradientBoostingClassifier(
                learning_rate=0.05,
                max_iter=100,
                max_leaf_nodes=params["max_leaf_nodes"],
                min_samples_leaf=100,
                l2_regularization=params["l2"],
                early_stopping=False,
                random_state=RANDOM_SEED,
            ),
        )
    else:
        raise ValueError(kind)
    model.fit(x, y)
    return model


def model_decisions(candidates: list[Candidate], train: pd.DataFrame, test: pd.DataFrame) -> dict[str, np.ndarray]:
    result: dict[str, np.ndarray] = {}
    grouped: dict[tuple[Any, ...], list[Candidate]] = {}
    for c in candidates:
        if c.kind != "hgb":
            continue
        key = (c.kind, c.params["max_leaf_nodes"], c.params["l2"])
        grouped.setdefault(key, []).append(c)

    x_train = train[MODEL_FEATURES].to_numpy(float)
    x_test = test[MODEL_FEATURES].to_numpy(float)
    for group in grouped.values():
        template = group[0]
        model = fit_model(template.kind, template.params, train)
        prob_train = model.predict_proba(x_train)[:, 1]
        prob_test = model.predict_proba(x_test)[:, 1]
        confidence_train = np.abs(prob_train - 0.5)
        for c in group:
            q = c.params["confidence_quantile"]
            cutoff = -np.inf if q <= 0 else float(np.quantile(confidence_train, q))
            d = np.sign(prob_test - 0.5)
            d[np.abs(prob_test - 0.5) < cutoff] = 0
            result[c.name] = d.astype(np.int8)
    return result


def decision_for_candidate(candidate: Candidate, train: pd.DataFrame, test: pd.DataFrame) -> np.ndarray:
    if candidate.kind == "score":
        return score_decision(candidate, train, test)
    if candidate.kind == "seasonal":
        return seasonal_decision(candidate, train, test)
    if candidate.kind == "majority":
        return majority_decision(train, test)
    if candidate.kind == "hgb":
        model = fit_model(candidate.kind, candidate.params, train)
        prob_train = model.predict_proba(train[MODEL_FEATURES].to_numpy(float))[:, 1]
        prob_test = model.predict_proba(test[MODEL_FEATURES].to_numpy(float))[:, 1]
        q = candidate.params["confidence_quantile"]
        cutoff = -np.inf if q <= 0 else float(np.quantile(np.abs(prob_train - 0.5), q))
        decision = np.sign(prob_test - 0.5)
        decision[np.abs(prob_test - 0.5) < cutoff] = 0
        return decision.astype(np.int8)
    if candidate.kind == "no_trade":
        return np.zeros(len(test), dtype=np.int8)
    raise ValueError(candidate.kind)


def development_folds_for_events(events: pd.DataFrame, horizon: int) -> tuple[tuple[pd.Timestamp, pd.Timestamp], ...]:
    """Create four anchored tests after a fixed train-only warm-up.

    Products launched at different dates (5m in late 2025, hourly in spring
    2025), so one global calendar schedule would create empty early folds for
    newer products.  The warm-up is 30 days, followed by four contiguous test
    intervals ending exactly at the untouched holdout boundary.
    """
    first = pd.Timestamp(events["start"].min()).floor("D")
    warmup = first + pd.Timedelta(days=30)
    if warmup >= HOLDOUT_START:
        raise ValueError(f"insufficient pre-holdout history for {horizon}m")
    total_seconds = (HOLDOUT_START - warmup).total_seconds()
    edges = [warmup + pd.Timedelta(seconds=total_seconds * i / 4.0) for i in range(5)]
    return tuple((edges[i], edges[i + 1]) for i in range(4))


def chronological_folds(events: pd.DataFrame, horizon: int) -> Iterable[tuple[int, pd.DataFrame, pd.DataFrame]]:
    folds = development_folds_for_events(events, horizon)
    for fold_id, (start, end) in enumerate(folds):
        train = events[(events["end"] <= start) & (events["start"] < HOLDOUT_START)].copy()
        test = events[(events["start"] >= start) & (events["end"] <= end)].copy()
        if len(train) == 0 or len(test) == 0:
            raise ValueError(f"empty fold {fold_id}")
        yield fold_id, train, test


def pnl_from_decision(decision: np.ndarray, y: np.ndarray, p: float = PRIMARY_ENTRY_PRICE) -> np.ndarray:
    trade = decision != 0
    y_sign = 2 * y.astype(np.int8) - 1
    correct = decision == y_sign
    pnl = np.zeros(len(y), dtype=float)
    pnl[trade] = correct[trade].astype(float) - p - fee_per_share(p)
    return pnl


def candidate_metrics(frame: pd.DataFrame, decision: np.ndarray, horizon: int) -> dict[str, Any]:
    y = frame["y"].to_numpy(int)
    trade = decision != 0
    y_sign = 2 * y.astype(np.int8) - 1
    correct = decision == y_sign
    pnl = pnl_from_decision(decision, y)
    n = int(trade.sum())
    accuracy = float(correct[trade].mean()) if n else float("nan")

    daily = pd.DataFrame({"date": frame["start"].dt.floor("D"), "pnl": pnl}).groupby("date")["pnl"].agg(
        ["sum", "count"]
    )
    daily_rate = daily["sum"] / daily["count"]
    mean_opp = float(pnl.mean())
    se_cluster = float(daily_rate.std(ddof=1) / math.sqrt(len(daily_rate))) if len(daily_rate) > 1 else float("nan")
    lcb_opp = mean_opp - 1.645 * se_cluster if np.isfinite(se_cluster) else float("nan")

    fold_rows = []
    for fold, loc in frame.groupby("fold").groups.items():
        idx = frame.index.get_indexer(loc)
        d = decision[idx]
        yf = frame.loc[loc, "y"].to_numpy(int)
        tf = d != 0
        cf = d == (2 * yf.astype(np.int8) - 1)
        acc = float(cf[tf].mean()) if tf.any() else float("nan")
        fold_rows.append(
            {
                "fold": int(fold),
                "trades": int(tf.sum()),
                "accuracy": acc,
                "ev_per_trade_at_50c": acc - breakeven_probability(0.5) if np.isfinite(acc) else None,
            }
        )
    eligible = n >= MIN_TOTAL_TRADES[horizon] and all(
        row["trades"] >= MIN_FOLD_TRADES[horizon] for row in fold_rows
    )
    return {
        "opportunities": len(frame),
        "trades": n,
        "coverage": n / len(frame),
        "accuracy": accuracy,
        "ev_per_trade_at_50c": accuracy - breakeven_probability(0.5) if n else None,
        "ev_per_opportunity_at_50c": mean_opp,
        "cluster_se_per_opportunity": se_cluster,
        "one_sided_95_lcb_per_opportunity": lcb_opp,
        "eligible_for_selection": eligible,
        "folds": fold_rows,
    }


def walk_forward_development(events: pd.DataFrame, horizon: int, candidates: list[Candidate]) -> dict[str, Any]:
    decisions: dict[str, list[np.ndarray]] = {c.name: [] for c in candidates}
    oos_frames: list[pd.DataFrame] = []
    fold_schedule = development_folds_for_events(events, horizon)
    for fold_id, train, test in chronological_folds(events, horizon):
        test = test.copy()
        test["fold"] = fold_id
        oos_frames.append(test)
        model_map = model_decisions(candidates, train, test)
        for c in candidates:
            if c.kind == "score":
                d = score_decision(c, train, test)
            elif c.kind == "seasonal":
                d = seasonal_decision(c, train, test)
            elif c.kind == "majority":
                d = majority_decision(train, test)
            else:
                d = model_map[c.name]
            decisions[c.name].append(d)

    oos = pd.concat(oos_frames, ignore_index=True)
    rows: list[dict[str, Any]] = []
    candidate_map = {c.name: c for c in candidates}
    for name, parts in decisions.items():
        d = np.concatenate(parts)
        metrics = candidate_metrics(oos, d, horizon)
        rows.append({"candidate": asdict(candidate_map[name]), "metrics": metrics})

    eligible = [r for r in rows if r["metrics"]["eligible_for_selection"]]
    if not eligible:
        raise RuntimeError(f"no eligible candidates for {horizon}m")
    eligible.sort(
        key=lambda r: (
            r["metrics"]["one_sided_95_lcb_per_opportunity"],
            r["metrics"]["ev_per_opportunity_at_50c"],
        ),
        reverse=True,
    )
    selected = eligible[0]
    no_trade_candidate = Candidate("baseline:no_trade", "risk_gate", "no_trade", {})
    no_trade_row = {
        "candidate": asdict(no_trade_candidate),
        "metrics": candidate_metrics(oos, np.zeros(len(oos), dtype=np.int8), horizon),
    }
    # A strategy that cannot demonstrate a positive lower-confidence bound on
    # development OOS economics is not allowed to advance merely because it
    # ranks first among losing alternatives.
    if selected["metrics"]["one_sided_95_lcb_per_opportunity"] <= 0.0:
        selected = no_trade_row
    rows.append(no_trade_row)

    family_best: dict[str, dict[str, Any]] = {}
    for row in rows:
        family = row["candidate"]["family"]
        current = family_best.get(family)
        score = row["metrics"]["one_sided_95_lcb_per_opportunity"]
        if current is None or score > current["metrics"]["one_sided_95_lcb_per_opportunity"]:
            family_best[family] = row

    return {
        "horizon_minutes": horizon,
        "development_oos_start": oos["start"].min(),
        "development_oos_end": oos["end"].max(),
        "development_oos_opportunities": len(oos),
        "development_folds": [(s, e) for s, e in fold_schedule],
        "candidate_count": len(candidates) + 1,
        "selection_objective": "max one-sided 95% day-cluster LCB of net PnL per market opportunity at 50c; advance no_trade unless the best active rule has positive LCB",
        "selected": selected,
        "family_best": family_best,
        "all_candidates": rows,
    }


def run_development() -> None:
    if LOCK_PATH.exists() or DEV_RESULTS_PATH.exists():
        raise SystemExit(
            "development artifacts already exist; refusing to overwrite a candidate-selection record. "
            "Use a new research run directory for a new experiment."
        )
    raw, integrity = load_and_validate()
    outcomes, outcome_integrity = load_resolved_outcomes()
    features = add_minute_features(raw)
    candidates = build_candidates()
    audits: dict[str, Any] = {}
    development: dict[str, Any] = {}
    locks: dict[str, Any] = {}

    print(f"dataset rows={len(raw):,} sha256={integrity['sha256']}")
    print(f"candidate library={len(candidates)} configurations per horizon")
    for horizon in HORIZONS:
        events, audit = make_events(raw, features, horizon, outcomes)
        audits[str(horizon)] = audit
        print(f"[{horizon:>3}m] events={len(events):,}; running development walk-forward...")
        result = walk_forward_development(events, horizon, candidates)
        development[str(horizon)] = result
        selected = result["selected"]
        locks[str(horizon)] = selected["candidate"]
        m = selected["metrics"]
        if selected["candidate"]["kind"] == "no_trade":
            print(
                f"[{horizon:>3}m] LOCK baseline:no_trade; best active development rule did not clear "
                "a positive day-cluster lower-confidence bound"
            )
        else:
            print(
                f"[{horizon:>3}m] LOCK {selected['candidate']['name']} trades={m['trades']:,} "
                f"acc={m['accuracy']:.4%} EV/trade@50={m['ev_per_trade_at_50c']:+.5f} "
                f"LCB/opp={m['one_sided_95_lcb_per_opportunity']:+.6f}"
            )

    common = {
        "created_at_utc": pd.Timestamp.now(tz="UTC"),
        "data_integrity": integrity,
        "outcome_integrity": outcome_integrity,
        "holdout_start": HOLDOUT_START,
        "development_fold_scheme": "four anchored tests after 30-day product-specific warm-up; all tests end by 2026-05-01",
        "current_fee_formula": "fee_per_share = 0.07 * p * (1-p)",
        "primary_selection_entry_price": PRIMARY_ENTRY_PRICE,
        "candidate_library_count_per_horizon": len(candidates),
        "label_scope": {
            "5m": "terminal Polymarket winner (Chainlink BTC/USD settlement; Binance proxies retained for sensitivity)",
            "15m": "terminal Polymarket winner (Chainlink BTC/USD settlement; Binance proxies retained for sensitivity)",
            "60m": "terminal Polymarket winner (Binance finalized 1H rule; local candle label cross-check retained)",
            "240m": "terminal Polymarket winner (Chainlink BTC/USD settlement; Binance proxies retained for sensitivity)",
        },
    }
    write_json(DEV_RESULTS_PATH, {**common, "event_label_audits": audits, "development": development})
    lock_material = {
        "script_sha256": sha256(Path(__file__)),
        "dataset_sha256": integrity["sha256"],
        "outcome_cache_sha256": outcome_integrity["sha256"],
        "holdout_start": HOLDOUT_START,
        "development_fold_scheme": "four anchored tests after 30-day product-specific warm-up; all tests end by 2026-05-01",
        "fee_formula": "fee_per_share = 0.07 * p * (1-p)",
        "primary_selection_entry_price": PRIMARY_ENTRY_PRICE,
        "candidate_library": [asdict(c) for c in candidates],
        "locked_configs": locks,
    }
    lock_payload = {
        **common,
        "lock_sha_material": lock_material,
    }
    # The lock hash covers code, data, split, economics, full search library,
    # and the final selected configuration. A post-selection edit changes it.
    lock_payload["lock_id"] = canonical_sha256(lock_material)
    write_json(LOCK_PATH, lock_payload)
    print(f"wrote development results: {DEV_RESULTS_PATH}")
    print(f"wrote frozen lock: {LOCK_PATH} ({lock_payload['lock_id']})")


def block_bootstrap_accuracy(
    frame: pd.DataFrame,
    decision: np.ndarray,
    y_col: str = "y",
    n_boot: int = 10_000,
    seed: int = RANDOM_SEED,
) -> dict[str, float]:
    rng = np.random.default_rng(seed)
    work = pd.DataFrame(
        {
            "date": frame["start"].dt.floor("D"),
            "trade": (decision != 0).astype(int),
            "correct": (decision == (2 * frame[y_col].to_numpy(np.int8) - 1)).astype(int) * (decision != 0),
        }
    )
    daily = work.groupby("date")[["trade", "correct"]].sum().to_numpy(float)
    valid = daily[:, 0].sum() > 0
    if not valid:
        return {"point": float("nan"), "lower_one_sided_95": float("nan"), "upper_95": float("nan")}
    draws = np.empty(n_boot, dtype=float)
    for b in range(n_boot):
        chosen = daily[rng.integers(0, len(daily), size=len(daily))]
        trades = chosen[:, 0].sum()
        draws[b] = chosen[:, 1].sum() / trades if trades else np.nan
    point = daily[:, 1].sum() / daily[:, 0].sum()
    return {
        "point": float(point),
        "lower_one_sided_95": float(np.nanquantile(draws, 0.05)),
        "lower_two_sided_95": float(np.nanquantile(draws, 0.025)),
        "upper_two_sided_95": float(np.nanquantile(draws, 0.975)),
        "clusters_days": int(len(daily)),
    }


def block_permutation_test(
    frame: pd.DataFrame,
    decision: np.ndarray,
    n_perm: int = 10_000,
    block_days: int = 7,
    seed: int = RANDOM_SEED,
) -> dict[str, Any]:
    """Shuffle contiguous outcome blocks; keep the frozen decisions fixed."""
    rng = np.random.default_rng(seed)
    y = frame["y"].to_numpy(np.int8)
    horizon = int(round((frame["end"].iloc[0] - frame["start"].iloc[0]).total_seconds() / 60.0))
    block_len = max(1, block_days * 24 * 60 // horizon)
    blocks = [y[i : i + block_len] for i in range(0, len(y), block_len)]
    observed = float(pnl_from_decision(decision, y).mean())
    null = np.empty(n_perm, dtype=float)
    for b in range(n_perm):
        order = rng.permutation(len(blocks))
        yp = np.concatenate([blocks[i] for i in order])[: len(y)]
        null[b] = pnl_from_decision(decision, yp).mean()
    p_value = (1.0 + float(np.sum(null >= observed))) / (n_perm + 1.0)
    std = float(null.std(ddof=1))
    return {
        "method": "fixed-strategy permutation of contiguous 7-day outcome blocks",
        "block_days": block_days,
        "block_count": len(blocks),
        "permutations": n_perm,
        "observed_ev_per_opportunity_at_50c": observed,
        "null_mean": float(null.mean()),
        "null_std": std,
        "z_score": (observed - float(null.mean())) / std if std > 0 else None,
        "raw_one_sided_p": p_value,
    }


def holm_adjust(p_values: dict[str, float]) -> dict[str, float]:
    ordered = sorted(p_values.items(), key=lambda kv: kv[1])
    m = len(ordered)
    adjusted: dict[str, float] = {}
    running = 0.0
    for rank, (key, p) in enumerate(ordered):
        value = min(1.0, (m - rank) * p)
        running = max(running, value)
        adjusted[key] = running
    return adjusted


def economic_frontier(q: float, q_lower: float) -> list[dict[str, Any]]:
    rows = []
    for p in ENTRY_PRICES:
        fee = fee_per_share(p)
        ev = q - p - fee
        ev_lower = q_lower - p - fee
        cash = p + fee
        rows.append(
            {
                "ask": p,
                "fee_per_share": fee,
                "break_even_probability": p + fee,
                "ev_per_share": ev,
                "ev_per_dollar_total_cash_paid": ev / cash,
                "one_sided_95_lower_ev_per_share": ev_lower,
                "one_sided_95_lower_ev_per_dollar": ev_lower / cash,
            }
        )
    return rows


def monthly_results(frame: pd.DataFrame, decision: np.ndarray) -> list[dict[str, Any]]:
    work = frame[["start", "y"]].copy()
    work["decision"] = decision
    work["month"] = work["start"].dt.to_period("M").astype(str)
    out = []
    for month, g in work.groupby("month"):
        trade = g["decision"].to_numpy(np.int8) != 0
        correct = g["decision"].to_numpy(np.int8) == (2 * g["y"].to_numpy(np.int8) - 1)
        q = float(correct[trade].mean()) if trade.any() else float("nan")
        out.append(
            {
                "month": month,
                "opportunities": len(g),
                "trades": int(trade.sum()),
                "accuracy": q,
                "ev_per_trade_at_50c": q - breakeven_probability(0.5) if np.isfinite(q) else None,
            }
        )
    return out


def evaluate_holdout_horizon(
    raw: pd.DataFrame,
    features: pd.DataFrame,
    outcomes: pd.DataFrame,
    horizon: int,
    candidate: Candidate,
) -> tuple[dict[str, Any], dict[str, Any]]:
    events, audit = make_events(raw, features, horizon, outcomes)
    dev = events[events["end"] <= HOLDOUT_START].copy()
    holdout = events[events["start"] >= HOLDOUT_START].copy()
    if len(dev) == 0 or len(holdout) == 0:
        raise ValueError(f"empty dev/holdout for {horizon}")
    decision = decision_for_candidate(candidate, dev, holdout)
    trade = decision != 0
    correct = decision == (2 * holdout["y"].to_numpy(np.int8) - 1)
    q = float(correct[trade].mean()) if trade.any() else float("nan")
    ci = block_bootstrap_accuracy(holdout, decision, "y", seed=RANDOM_SEED + horizon)
    perm = block_permutation_test(holdout, decision, seed=RANDOM_SEED + horizon)

    proxy_sensitivity: dict[str, Any] = {}
    for col in ("y", "y_close_proxy", "y_hlc3_proxy", "y_symmetric_proxy"):
        yc = holdout[col].to_numpy(np.int8)
        cc = decision == (2 * yc - 1)
        qc = float(cc[trade].mean()) if trade.any() else float("nan")
        proxy_sensitivity[col] = {
            "accuracy": qc,
            "ev_per_trade_at_50c": qc - breakeven_probability(0.5),
        }
    if horizon != 60:
        y_primary = holdout["y"].to_numpy(np.int8)
        y_close = holdout["y_close_proxy"].to_numpy(np.int8)
        y_hlc3 = holdout["y_hlc3_proxy"].to_numpy(np.int8)
        y_symmetric = holdout["y_symmetric_proxy"].to_numpy(np.int8)
        dsign = decision
        correct_all = (
            (dsign == (2 * y_primary - 1))
            & (dsign == (2 * y_close - 1))
            & (dsign == (2 * y_hlc3 - 1))
            & (dsign == (2 * y_symmetric - 1))
            & trade
        )
        proxy_sensitivity["conservative_all_proxy_disagreements_counted_wrong"] = {
            "accuracy": float(correct_all.sum() / trade.sum()),
            "ev_per_trade_at_50c": float(correct_all.sum() / trade.sum()) - breakeven_probability(0.5),
        }

    result = {
        "horizon_minutes": horizon,
        "locked_candidate": asdict(candidate),
        "holdout_start": holdout["start"].min(),
        "holdout_end": holdout["end"].max(),
        "opportunities": len(holdout),
        "trades": int(trade.sum()),
        "coverage": float(trade.mean()),
        "accuracy": q,
        "day_cluster_accuracy_interval": ci,
        "maximum_fee_only_ask_from_point_q": maximum_ask(q),
        "maximum_fee_only_ask_from_one_sided_95_lower_q": maximum_ask(ci["lower_one_sided_95"]),
        "hypothetical_entry_price_frontier": economic_frontier(q, ci["lower_one_sided_95"]),
        "monthly": monthly_results(holdout, decision),
        "permutation_test": perm,
        "label_proxy_sensitivity": proxy_sensitivity,
        "settlement_validity": "terminal Polymarket outcome label; Binance proxy disagreement is reported separately",
    }
    return result, audit


def run_holdout() -> None:
    if not LOCK_PATH.exists():
        raise SystemExit(f"missing lock {LOCK_PATH}; run the dev stage first")
    if HOLDOUT_RESULTS_PATH.exists():
        raise SystemExit(
            "holdout results already exist; refusing to reopen the final period. "
            "A revised strategy requires a new future holdout."
        )
    lock = json.loads(LOCK_PATH.read_text())
    raw, integrity = load_and_validate()
    outcomes, outcome_integrity = load_resolved_outcomes()
    material = lock["lock_sha_material"]
    if canonical_sha256(material) != lock["lock_id"]:
        raise SystemExit("lock fingerprint mismatch; refusing to open holdout")
    if integrity["sha256"] != material["dataset_sha256"]:
        raise SystemExit("dataset hash changed after development lock")
    if outcome_integrity["sha256"] != material["outcome_cache_sha256"]:
        raise SystemExit("resolved outcome cache changed after development lock")
    if material["holdout_start"] != HOLDOUT_START.isoformat():
        raise SystemExit("holdout boundary differs from development lock")
    if material["script_sha256"] != sha256(Path(__file__)):
        raise SystemExit("research script changed after development lock; refusing to open holdout")

    features = add_minute_features(raw)
    results: dict[str, Any] = {}
    audits: dict[str, Any] = {}
    p_values: dict[str, float] = {}
    print(f"opening frozen holdout with lock {lock['lock_id']}")
    for horizon in HORIZONS:
        cdict = material["locked_configs"][str(horizon)]
        candidate = Candidate(**cdict)
        if candidate.kind == "no_trade":
            # The final labels for this horizon are intentionally not opened:
            # the development risk gate has already frozen a no-trade policy.
            result = {
                "horizon_minutes": horizon,
                "locked_candidate": asdict(candidate),
                "status": "no_trade_locked_from_development",
                "holdout_labels_evaluated": False,
                "reason": "No active rule had a positive development day-cluster lower-confidence bound.",
            }
            audit = {"holdout_labels_evaluated": False}
        else:
            result, audit = evaluate_holdout_horizon(raw, features, outcomes, horizon, candidate)
        results[str(horizon)] = result
        audits[str(horizon)] = audit
        if candidate.kind == "no_trade":
            print(f"[{horizon:>3}m] no-trade policy carried through; final labels not evaluated")
        else:
            p_values[str(horizon)] = result["permutation_test"]["raw_one_sided_p"]
            print(
                f"[{horizon:>3}m] {candidate.name} trades={result['trades']:,} "
                f"acc={result['accuracy']:.4%} p_raw={p_values[str(horizon)]:.5f} "
                f"max_ask(q)={result['maximum_fee_only_ask_from_point_q']:.4f}"
            )

    adjusted = holm_adjust(p_values)
    for horizon, p_adj in adjusted.items():
        results[horizon]["permutation_test"]["holm_adjusted_p_across_four_horizons"] = p_adj

    payload = {
        "created_at_utc": pd.Timestamp.now(tz="UTC"),
        "lock_id": lock["lock_id"],
        "dataset_sha256": integrity["sha256"],
        "outcome_cache_sha256": outcome_integrity["sha256"],
        "fee_formula": "fee_per_share = 0.07 * p * (1-p)",
        "economic_scope": (
            "Hypothetical fills at stated executable asks. No historical CLOB order books were supplied, so this is not a fill backtest."
        ),
        "event_label_audits": audits,
        "holdout": results,
    }
    write_json(HOLDOUT_RESULTS_PATH, payload)
    print(f"wrote one-shot holdout results: {HOLDOUT_RESULTS_PATH}")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("stage", choices=("dev", "holdout"))
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if args.stage == "dev":
        run_development()
    else:
        run_holdout()
