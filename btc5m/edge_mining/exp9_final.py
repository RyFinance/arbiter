"""E9: Champion combos, final ranking."""
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
import numpy as np
import pandas as pd
import edge_lib as L

piv = L.load_paired()
variants, btc = L.sigma_variants()
base_idx = (pd.DatetimeIndex(pd.to_datetime(piv.sec, unit="s")).tz_localize("UTC")
            .floor("min") - pd.Timedelta(seconds=60))
sigma_ew15 = variants["ewma15m"].reindex(base_idx).to_numpy()
piv["fair_up"] = L.fair_with_sigma(piv, sigma_ew15, "ewma15m")
w_vol = piv.groupby("mts").sigma_s.median()
terc = pd.qcut(w_vol, 3, labels=["LOW", "MED", "HIGH"])
hi_mts = set(w_vol[terc == "HIGH"].index)

def run(piv_sub, theta, side, tau_lo, tau_hi, label):
    t = L.taker_backtest(piv_sub, theta, tau_lo=tau_lo, tau_hi=tau_hi)
    t = t[t.side == side]
    s = L.summary(t)
    print(f"  {label:<34} th={theta:<5} n/day={s['n/day']:>4.0f} hit={s['hit']:.3f} "
          f"net/sh={s['net/sh']:+.4f} net/day$={s['net/day_usd']:>6.0f} "
          f"posdays={s['pos_days']:.2f}")
    return t

print("=== A. up-only, early window (tau 150-270), all regimes ===")
for th in (0.05, 0.08, 0.12):
    run(piv, th, "up", 150, 270, "up | early | all-regime")

print("=== B. up-only, early window, HIGH-vol only ===")
for th in (0.05, 0.08, 0.12):
    run(piv[piv.mts.isin(hi_mts)], th, "up", 150, 270, "up | early | HIGH-vol")

print("=== C. both sides, early window, all regimes ===")
t = run(piv, 0.08, "both", 150, 270, "both | early | all-regime")

print("=== D. up + dn(up-days only), early, all regimes ===")
t = L.taker_backtest(piv, 0.08, tau_lo=150, tau_hi=270)
t["day"] = pd.to_datetime(t.mts, unit="s", utc=True).dt.date
btc_d = btc.close.resample("1D").last()
day_ret = (btc_d / btc_d.shift(1) - 1)
day_ret.index = day_ret.index.date
t["day_ret"] = t.day.map(day_ret.to_dict())
t = t[(t.side == "up") | (t.day_ret > 0)]
s = L.summary(t)
print(f"  {'up + dn-on-updays | early':<34} th=0.08  n/day={s['n/day']:>4.0f} "
      f"hit={s['hit']:.3f} net/sh={s['net/sh']:+.4f} net/day$={s['net/day_usd']:>6.0f} "
      f"posdays={s['pos_days']:.2f}")
