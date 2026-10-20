"""E5: Vol-regime conditioning + by-side + z-score + persistence refinements."""
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
import numpy as np
import pandas as pd
import edge_lib as L

piv = L.load_paired()
w_vol = piv.groupby("mts").sigma_s.median()
terc = pd.qcut(w_vol, 3, labels=["LOW", "MED", "HIGH"])
regime = terc.to_dict()
for name in ("LOW", "MED", "HIGH"):
    mts_mask = [m for m, r in regime.items() if r == name]
    sub = piv[piv.mts.isin(mts_mask)]
    t = L.taker_backtest(sub, 0.08)
    s = L.summary(t)
    print(f"regime {name:>4}: n/day={s['n/day']:>5.0f} hit={s['hit']:.3f} "
          f"net/sh={s['net/sh']:+.4f} net/day$={s['net/day_usd']:>7.0f} "
          f"posdays={s['pos_days']:.2f}")

# by-side asymmetry
t = L.taker_backtest(piv, 0.08)
print("\nby side:")
for side, g in t.groupby("side"):
    print(f"  {side}: n={len(g)} net/sh={g.pnl.mean():+.4f} hit={g.win.mean():.3f}")

# persistence refinement: fire must hold for >=3 consecutive seconds
p = piv[(piv.tau > 20) & (piv.tau < 270)].copy()
for side in ("up", "dn"):
    fair = p.fair_up if side == "up" else 1.0 - p.fair_up
    p[f"edge_{side}"] = fair - p[f"ba_{side}"]
p = p.sort_values("sec")
print("\npersistence filter (edge>0.08 for >=N consecutive secs):")
for nsec in (1, 2, 3, 5):
    recs = []
    for side in ("up", "dn"):
        e = p[f"edge_{side}"] > 0.08
        p[f"e_{side}"] = e
    for mts, g in p.groupby("mts"):
        for side in ("up", "dn"):
            col = f"e_{side}"
            runs = g[col].astype(int)
            cum = runs.groupby((runs != runs.shift()).cumsum()).transform("size")
            fires = g[runs.astype(bool) & (cum >= nsec)]
            if fires.empty:
                continue
            f0 = fires.iloc[0]
            recs.append({"mts": mts, "sec": f0.sec, "side": side,
                         "px": f0[f"ba_{side}"], "sh": min(f0[f"qa_{side}"], 75)})
    t = pd.DataFrame(recs)
    if t.empty:
        continue
    win = piv.drop_duplicates("mts").set_index("mts").btc_winner
    t["win"] = (t.side == t.mts.map(win)).astype(float)
    t["pnl"] = t.win - t.px - L.taker_fee(t.px)
    nd = pd.to_datetime(t.mts, unit="s", utc=True).dt.date.nunique()
    print(f"  nsec>={nsec}: n/day={len(t)/nd:.0f} hit={t.win.mean():.3f} "
          f"net/sh={t.pnl.mean():+.4f} net/day$={(t.pnl*t.sh).sum()/nd:>7.0f}")
