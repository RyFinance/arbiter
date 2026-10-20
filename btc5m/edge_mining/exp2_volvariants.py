"""E2: Vol-forecast variants of the fair-value model."""
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
import numpy as np
import pandas as pd
import edge_lib as L

piv = L.load_paired()
variants, btc = L.sigma_variants()
min_idx = pd.DatetimeIndex(pd.to_datetime(piv.sec, unit="s")).tz_localize("UTC").floor("min")
base_idx = min_idx - pd.Timedelta(seconds=60)

# add 1s-realized variant
sig1s, b1s = L.sigma_1s()
v1 = sig1s.reindex(pd.DatetimeIndex(pd.to_datetime(piv.sec, unit="s")).tz_localize("UTC") - pd.Timedelta(seconds=1))

results = {}
for name, series in variants.items():
    s = series.reindex(base_idx).to_numpy()
    fair = L.fair_with_sigma(piv, s, name)
    piv["fair_up"] = fair
    for theta in (0.05, 0.08, 0.12):
        t = L.taker_backtest(piv, theta)
        results[(name, theta)] = L.summary(t)
# 1s realized
fair = L.fair_with_sigma(piv, v1.to_numpy(), "rv15m_1s")
piv["fair_up"] = fair
for theta in (0.05, 0.08, 0.12):
    results[("rv15m_1s", theta)] = L.summary(L.taker_backtest(piv, theta))

print(f"\n{'variant':<10} {'theta':>5} {'n/day':>6} {'hit':>6} {'gross':>7} {'net/sh':>7} {'net/day$':>8} {'posdays':>7}")
for (name, theta), r in results.items():
    print(f"{name:<10} {theta:>5} {r['n/day']:>6.0f} {r['hit']:>6.3f} "
          f"{r['gross/sh']:>7.4f} {r['net/sh']:>7.4f} {r['net/day_usd']:>8.1f} "
          f"{r['pos_days']:>7.2f}")
