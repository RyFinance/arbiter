"""E8: Trend dependence + dn-side-in-HIGH + tau gating refinements."""
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

print("=== trend dependence (ewma15m, all regimes, theta=0.08) ===")
t = L.taker_backtest(piv, 0.08)
t["day"] = pd.to_datetime(t.mts, unit="s", utc=True).dt.date
btc_d = btc.close.resample("1D").last()
day_ret = (btc_d / btc_d.shift(1) - 1)
day_ret.index = day_ret.index.date
t["day_ret"] = t.day.map(day_ret.to_dict())
print(f"up-days: {(t.day_ret>0).mean():.0%} of trades")
for side in ("up", "dn"):
    for cond, label in ((t.day_ret > 0, "up-day"), (t.day_ret <= 0, "dn-day")):
        g = t[(t.side == side) & cond]
        if len(g):
            print(f"  {side} on {label:>6}: n={len(g):>4} net/sh={g.pnl.mean():+.4f} "
                  f"hit={g.win.mean():.3f}")

print("\n=== dn-side within HIGH-vol windows ===")
piv_hi = piv[piv.mts.isin(hi_mts)]
t = L.taker_backtest(piv_hi, 0.08)
for side, g in t.groupby("side"):
    print(f"  {side}: n={len(g)} net/sh={g.pnl.mean():+.4f} hit={g.win.mean():.3f}")

print("\n=== tau gating (up-side only, all regimes, theta=0.08) ===")
t = L.taker_backtest(piv, 0.08, tau_lo=20, tau_hi=270)
t["tau_at_entry"] = piv.set_index(["mts", "sec"]).loc[zip(t.mts, t.sec), "tau"].to_numpy()
up = t[t.side == "up"]
for lo, hi, label in ((20, 60, "late 20-60s"), (60, 150, "mid 60-150s"),
                      (150, 270, "early 150-270s")):
    g = up[(up.tau_at_entry > lo) & (up.tau_at_entry <= hi)]
    if len(g):
        print(f"  {label:>14}: n={len(g):>4} net/sh={g.pnl.mean():+.4f} hit={g.win.mean():.3f}")
