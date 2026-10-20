import numpy as np
import pandas as pd

from scripts.btc_offline_features import build_features, report


def sample():
    n = 260
    return pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=n, freq="min", tz="UTC"),
        "open": np.arange(n, dtype=float) + 100,
        "high": np.arange(n, dtype=float) + 101,
        "low": np.arange(n, dtype=float) + 99,
        "close": np.arange(n, dtype=float) + 100.5,
        "volume": np.ones(n),
    })


def test_features_are_present_and_label_is_forward_only():
    f, y = build_features(sample(), horizon=5)
    expected = {"sma_20", "bb_upper", "rsi_14", "macd", "atr_14", "vol_squeeze", "vwap_distance", "zscore_20", "momentum_20", "half_life_60", "hour_utc", "day_of_week", "vol_regime"}
    assert expected <= set(f.columns)
    assert y.iloc[0] == (sample().close.iloc[5] / sample().close.iloc[0] - 1)
    assert f.index.max() < pd.Timestamp("2026-01-02", tz="UTC")


def test_report_contains_distribution_correlations_and_leakage_audit():
    text = report(sample(), horizon=5)
    assert "Causal realized-volatility regime distribution" in text
    assert "Feature/label correlations" in text
    assert "Leakage audit" in text
    assert "expanding tertile thresholds" in text
    assert "none" in text


def test_volatility_regime_is_unchanged_by_later_candles():
    candles = sample()
    before, _ = build_features(candles, horizon=5)
    changed = candles.copy()
    changed.loc[200:, ["open", "high", "low", "close"]] *= 10
    after, _ = build_features(changed, horizon=5)

    pd.testing.assert_series_equal(before["vol_regime"].iloc[:200], after["vol_regime"].iloc[:200])
