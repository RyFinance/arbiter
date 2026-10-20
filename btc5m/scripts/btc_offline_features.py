"""Offline BTC OHLCV features and leakage-audited research report.

No network, model, execution, or sentiment inputs are used.  Features at t
use only bars through t; the default label is the close-to-close return after
the requested forward horizon.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd


# Every member is available at the feature cutoff t.  ``vol_regime`` is a
# numeric low/mid/high encoding whose thresholds are calculated from past
# realized volatility only; it is deliberately safe to pass to sklearn.
CAUSAL_FEATURE_COLUMNS = (
    "close",
    "sma_5",
    "sma_20",
    "sma_50",
    "sma_200",
    "bb_mid",
    "bb_upper",
    "bb_lower",
    "bb_width",
    "rsi_14",
    "macd",
    "macd_signal",
    "macd_hist",
    "atr_14",
    "realized_vol_30",
    "vol_squeeze",
    "vwap_distance",
    "zscore_20",
    "momentum_20",
    "zscore_60",
    "momentum_60",
    "half_life_60",
    "hour_utc",
    "day_of_week",
    "vol_regime",
)


def _wilder(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def _causal_volatility_regime(realized_volatility: pd.Series) -> pd.Series:
    """Encode current volatility against expanding *prior* tertile thresholds.

    The full-sample rank previously used here let the regime at t depend on
    observations after t.  Each threshold below sees values strictly before t,
    so adding or changing later candles cannot alter an earlier regime.
    """
    history = realized_volatility.shift(1)
    lower = history.expanding(min_periods=30).quantile(1 / 3)
    upper = history.expanding(min_periods=30).quantile(2 / 3)
    regime = pd.Series(np.nan, index=realized_volatility.index, dtype="float64")
    eligible = realized_volatility.notna() & lower.notna() & upper.notna()
    regime.loc[eligible & (realized_volatility <= lower)] = 0.0
    regime.loc[eligible & (realized_volatility > lower) & (realized_volatility <= upper)] = 1.0
    regime.loc[eligible & (realized_volatility > upper)] = 2.0
    return regime


def build_features(candles: pd.DataFrame, horizon: int = 15) -> tuple[pd.DataFrame, pd.Series]:
    required = {"open", "high", "low", "close", "volume"}
    missing = required - set(candles.columns)
    if missing:
        raise ValueError(f"missing OHLCV columns: {sorted(missing)}")
    d = candles.copy()
    if "timestamp" in d:
        d["timestamp"] = pd.to_datetime(d["timestamp"], utc=True)
        d = d.sort_values("timestamp").drop_duplicates("timestamp").set_index("timestamp")
    else:
        d.index = pd.RangeIndex(len(d), name="bar")
    c, h, l, v = [pd.to_numeric(d[x], errors="coerce") for x in ("close", "high", "low", "volume")]
    f = pd.DataFrame(index=d.index)
    f["close"] = c
    for n in (5, 20, 50, 200):
        f[f"sma_{n}"] = c.rolling(n, min_periods=n).mean()
    mid = c.rolling(20, min_periods=20).mean()
    sd = c.rolling(20, min_periods=20).std(ddof=0)
    f["bb_mid"], f["bb_upper"], f["bb_lower"] = mid, mid + 2 * sd, mid - 2 * sd
    f["bb_width"] = (4 * sd / mid.replace(0, np.nan)).replace([np.inf, -np.inf], np.nan)
    delta = c.diff()
    gain, loss = delta.clip(lower=0), -delta.clip(upper=0)
    avg_gain, avg_loss = _wilder(gain, 14), _wilder(loss, 14)
    rs = avg_gain / avg_loss.replace(0, np.nan)
    f["rsi_14"] = 100 - 100 / (1 + rs)
    ema12, ema26 = c.ewm(span=12, adjust=False, min_periods=12).mean(), c.ewm(span=26, adjust=False, min_periods=26).mean()
    f["macd"] = ema12 - ema26
    f["macd_signal"] = f["macd"].ewm(span=9, adjust=False, min_periods=9).mean()
    f["macd_hist"] = f["macd"] - f["macd_signal"]
    prev = c.shift(1)
    tr = pd.concat([h - l, (h - prev).abs(), (l - prev).abs()], axis=1).max(axis=1)
    f["atr_14"] = _wilder(tr, 14)
    logret = np.log(c).diff()
    f["realized_vol_30"] = logret.rolling(30, min_periods=30).std(ddof=0) * np.sqrt(30)
    f["vol_squeeze"] = f["bb_width"] / f["realized_vol_30"].replace(0, np.nan)
    typical = (h + l + c) / 3
    f["vwap_distance"] = typical.mul(v).rolling(30, min_periods=30).sum() / v.rolling(30, min_periods=30).sum() / c - 1
    for n in (20, 60):
        mean, std = c.rolling(n, min_periods=n).mean(), c.rolling(n, min_periods=n).std(ddof=0)
        f[f"zscore_{n}"] = (c - mean) / std.replace(0, np.nan)
        f[f"momentum_{n}"] = c.pct_change(n)
    # AR(1) half-life of the rolling mean-reversion residual; capped for readability.
    resid = c - f["sma_20"]
    f["half_life_60"] = resid.rolling(60, min_periods=60).apply(
        lambda x: float(np.clip(-np.log(2) / np.polyfit(x[:-1], np.diff(x), 1)[0], 0, 10000))
        if np.polyfit(x[:-1], np.diff(x), 1)[0] < 0 else np.nan, raw=True
    )
    if isinstance(f.index, pd.DatetimeIndex):
        f["hour_utc"] = f.index.hour
        f["day_of_week"] = f.index.dayofweek
    else:
        f["hour_utc"], f["day_of_week"] = np.nan, np.nan
    f["vol_regime"] = _causal_volatility_regime(f["realized_vol_30"])
    label = c.shift(-horizon) / c - 1
    return f, label.rename(f"forward_return_{horizon}")


def report(candles: pd.DataFrame, horizon: int = 15) -> str:
    f, y = build_features(candles, horizon)
    usable = pd.concat([f, y], axis=1).dropna(subset=[y.name])
    numeric = f.select_dtypes(include=np.number).columns
    corr = usable[list(numeric) + [y.name]].corr()[y.name].drop(y.name).sort_values(key=abs, ascending=False)
    lines = ["# BTC offline feature report", "", f"Rows: {len(candles):,}; label horizon: {horizon} bars; usable labels: {len(usable):,}", "", "## Causal realized-volatility regime distribution"]
    dist = usable["vol_regime"].value_counts(dropna=False).sort_index()
    regime_names = {0.0: "low", 1.0: "mid", 2.0: "high"}
    lines += [f"- {regime_names.get(k, 'nan')}: {int(v):,} ({v / len(usable):.2%})" for k, v in dist.items()]
    lines += ["", "## Feature/label correlations (Pearson, complete pairs)"]
    lines += [f"- `{k}`: {v:.6f} (n={usable[[k, y.name]].dropna().shape[0]:,})" for k, v in corr.items()]
    future_words = ("forward", "future", "label", "target")
    flags = [c for c in f.columns if any(w in c.lower() for w in future_words)]
    lines += ["", "## Leakage audit", f"- Feature columns containing future/label tokens: {flags or 'none'}", "- All rolling/EMA/VWAP features are causal (current bar and prior bars only).", "- `vol_regime` uses expanding tertile thresholds from realized-volatility observations strictly before the current bar; it never uses a full-sample rank or quantile.", "- The forward-return label uses `close.shift(-horizon)` and is excluded from feature input."]
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("input", type=Path)
    ap.add_argument("--output", type=Path, default=Path("docs/btc_offline_feature_report.md"))
    ap.add_argument("--horizon", type=int, default=15)
    args = ap.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report(pd.read_parquet(args.input), args.horizon))
    print(args.output)


if __name__ == "__main__":
    main()
