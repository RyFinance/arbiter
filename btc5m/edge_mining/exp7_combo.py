"""E7: Combined champion — EWMA15m fair, HIGH-vol regime, UP-side only.

Also: trend filter test (up-side trades on up-days vs down-days).
"""
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
piv_hi = piv[piv.mts.isin(hi_mts)]

print("=== combo: ewma15m + HIGH vol + up-side only ===")
for theta in (0.05, 0.08, 0.12, 0.15):
    t = L.taker_backtest(piv_hi, theta)
    t = t[t.side == "up"]
    if t.empty:
        continue
    s = L.summary(t)
    print(f"  theta={theta}: n/day={s['n/day']:>5.0f} hit={s['hit']:.3f} "
          f"net/sh={s['net/sh']:+.4f} net/day$={s['net/day_usd']:>7.0f} "
          f"posdays={s['pos_days']:.2f}")

print("\n=== baseline (rv60, all regimes, up-only) for comparison ===")
piv["fair_up"] = L.fair_with_sigma(piv, piv.sigma_s.to_numpy(), "rv60")
t = L.taker_backtest(piv, 0.08); t = t[t.side == "up"]
s = L.summary(t)
print(f"  theta=0.08: n/day={s['n/day']:.0f} hit={s['hit']:.3f} "
      f"net/sh={s['net/sh']:+.4f} net/day$={s['net/day_usd']:.0f} posdays={s['pos_days']:.2f}")

print("\n=== trend filter: up-side P&L on up-days vs down-days ===")
piv["fair_up"] = L.fair_with_sigma(piv, sigma_ew15, "ewma15m")
t = L.taker_backtest(piv, 0.08)
t["day"] = pd.to_datetime(t.mts, unit="s", utc=True).dt.date
btc_d = btc.close.resample("1D").last()
day_ret = (btc_d / btc_d.shift(1) - 1)
t["day_ret"] = t.day.map(day_ret.to_dict())
for side in ("up", "dn"):
    for cond, label in ((t.day_ret > 0, "up-day"), (t.day_ret <= 0, "dn-day")):
        g = t[(t.side == side) & cond]
        if len(g):
            print(f"  {side} on {label:>6}: n={len(g):>4} net/sh={g.pnl.mean():+.4f} "
                  f"hit={g.win.mean():.3f}")
