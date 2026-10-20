"""E6: Does Polymarket order flow lead Binance? (long-format per-window)"""
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
import numpy as np
import pandas as pd
import edge_lib as L

piv = L.load_paired()
b1s = L.btc_1s()
piv["mid_up"] = np.where(piv.bb_up.notna() & piv.ba_up.notna(),
                         (piv.bb_up + piv.ba_up) / 2,
                         1 - (piv.bb_dn + piv.ba_dn) / 2)
S = b1s.close
r_full = np.log(S / S.shift(1))

leads = {k: [] for k in (1, 2, 3, 5, 8)}
sig_rs = {k: [] for k in (1, 2, 3, 5, 8)}
piv_sorted = piv.sort_values("sec")
for mts, g in piv_sorted.groupby("mts"):
    lo, hi = mts, mts + 299
    sl = S.loc[pd.to_datetime(lo, unit="s", utc=True): pd.to_datetime(hi, unit="s", utc=True)]
    if len(sl) < 60:
        continue
    r = np.log(sl / sl.shift(1)).to_numpy()
    P = g[["sec", "mid_up"]].sort_values("sec")
    pgrid = P.set_index("sec").reindex(range(lo, hi + 1)).ffill()
    dP = pgrid.mid_up.diff().to_numpy()
    for k in leads:
        x, y = dP[:-k], r[k:]
        m = np.isfinite(x) & np.isfinite(y)
        if m.sum() > 40:
            leads[k].append(np.corrcoef(x[m], y[m])[0, 1])
            sig_rs[k].append((x[m], y[m]))
print("mean corr(dP[t], r[t+k]):")
for k in leads:
    print(f"  k={k}s: {np.nanmean(leads[k]):+.4f}")

# tradeable? pool the best-lag data, sim BTC-side trade
best_k = max(leads, key=lambda k: abs(np.nanmean(leads[k])))
xs, ys = [], []
for x, y in sig_rs[best_k]:
    xs.append(x); ys.append(y)
X = np.concatenate(xs); Y = np.concatenate(ys)
print(f"\nbest lag k={best_k}s, n={len(X)}")
print("corr:", np.corrcoef(X, Y)[0, 1])
for th in (0.005, 0.01, 0.02, 0.03):
    take = np.abs(X) > th
    n = take.sum()
    if n < 50:
        continue
    gross_bps = Y[take].mean() * 1e4
    hit = (np.sign(X[take]) == np.sign(Y[take])).mean()
    print(f"  th={th}: n={n} hit={hit:.3f} gross={gross_bps:+.2f}bps "
          f"net(4bps rt)={gross_bps-4:+.2f}bps")
