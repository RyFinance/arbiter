"""E1: Response lag of Polymarket implied prob to BTC 1s moves (long-format)."""
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
r_std = r_full.std()
move_secs = r_full.index[r_full.abs() > 2.5 * r_std].astype("int64") // 10**9

lags = {k: [] for k in range(9)}
revs = {k: [] for k in range(1, 9)}
big_count = 0
snip = []
piv_sorted = piv.sort_values("sec")
for mts, g in piv_sorted.groupby("mts"):
    lo, hi = mts, mts + 299
    sl = S.loc[pd.to_datetime(lo, unit="s", utc=True): pd.to_datetime(hi, unit="s", utc=True)]
    if len(sl) < 30:
        continue
    r = np.log(sl / sl.shift(1)).to_numpy()
    P = g[["sec", "mid_up"]].sort_values("sec")
    pgrid = P.set_index("sec").reindex(range(lo, hi + 1)).ffill()
    dP = pgrid.mid_up.diff().to_numpy()
    for k in lags:
        x = r[:-k] if k else r
        y = dP[k:]
        m = np.isfinite(x) & np.isfinite(y)
        if m.sum() > 20:
            lags[k].append(np.corrcoef(x[m], y[m])[0, 1])
    for k in revs:
        x = dP[:-k]
        y = r[k:]
        m = np.isfinite(x) & np.isfinite(y)
        if m.sum() > 20:
            revs[k].append(np.corrcoef(x[m], y[m])[0, 1])
    big = np.abs(r) > 2.5 * r_std
    if big.sum():
        big_count += int(big.sum())

print("=== mean corr(r[t], dP[t+k]) across windows ===")
for k in range(9):
    v = np.nanmean(lags[k])
    print(f"  lag {k}s: {v:+.4f} (n_windows={len(lags[k])})")
print("\n=== reverse: mean corr(dP[t], r[t+k]) ===")
for k in range(1, 9):
    print(f"  lead {k}s: {np.nanmean(revs[k]):+.4f}")
print(f"\nbig 1s moves (2.5σ): {big_count}")

# snipable subset of theta=0.08 fires
t = L.taker_backtest(piv, 0.08)
tdt = t.sec.to_numpy()
recent = np.zeros(len(t), dtype=bool)
for i, ts in enumerate(tdt):
    d = ts - move_secs
    if ((d >= 0) & (d <= 3)).any():
        recent[i] = True
sub = t[recent]
print(f"\ntheta=0.08 fires within 3s of a big BTC move: {len(sub)} ({len(sub)/len(t)*100:.0f}%)")
if len(sub):
    print(f"  net P&L snipable: ${(sub.pnl*sub.sh).sum():.0f} | net/sh {sub.pnl.mean():.4f} "
          f"vs all {t.pnl.mean():.4f}")
