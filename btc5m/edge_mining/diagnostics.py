"""Diagnostics: stability, sizing, early exit, tie handling."""
from pathlib import Path
import numpy as np
import pandas as pd

OUTD = Path(__file__).parent / "out"
piv = pd.read_parquet(OUTD / "scored.parquet")

# ---------- 0. ties & winner margin ----------
w = piv.drop_duplicates("mts")
print(f"windows scored: {len(w)}")

# ---------- 1. daily stability of the theta=0.03 taker strategy ----------
def entries(theta):
    m = piv[(piv.tau > 20) & (piv.tau < 270)]
    recs = []
    for side in ("up", "dn"):
        fair = m.fair_up if side == "up" else 1.0 - m.fair_up
        e = (fair - m[f"ba_{side}"]) > theta
        recs.append(pd.DataFrame({"mts": m.mts[e], "sec": m.sec[e], "side": side,
                                  "px": m[f"ba_{side}"][e], "fair": fair[e],
                                  "asz": m[f"ba_{side}"][e]}))
    t = pd.concat(recs).sort_values("sec").groupby(["mts", "side"]).first().reset_index()
    return t

t3 = entries(0.03)
win_map = w.set_index("mts").btc_winner
t3["day"] = pd.to_datetime(t3.mts, unit="s", utc=True).dt.date
t3["win"] = (t3.side == t3.mts.map(win_map)).astype(float)
t3["pnl"] = t3.win - t3.px
daily = t3.groupby("day").agg(n=("pnl", "size"), pnl=("pnl", "sum"),
                              hit=("win", "mean"))
print("\n=== daily P&L (theta=0.03, per 1 share) ===")
print(daily.round(3).to_string())
print("positive days:", (daily.pnl > 0).sum(), "/", len(daily))

# ---------- 2. size-capped (assume we can only take top-of-book size) ----------
qmap_up = piv.set_index(["mts", "sec"]).qa_up if "qa_up" in piv.columns else None
print("\n(top-of-book size not in paired frame -> using median ask size from snapshots)")

# ---------- 3. markout: does price converge toward our fair after entry? ----------
snaps = None
sc = piv.copy()
key = sc.set_index(["mts", "sec"]).sort_index()
mk = []
for _, tr in t3.head(4000).iterrows():
    fut = sc[(sc.mts == tr.mts) & (sc.sec > tr.sec)].sort_values("sec")
    if fut.empty:
        continue
    row10 = fut.iloc[min(9, len(fut) - 1)]
    mid_then = (row10.bb_up + (1 - row10.ba_dn)) / 2 if not np.isnan(row10.ba_dn) else np.nan
    mk.append({"horizon_rows": len(fut), "mid_after": mid_then,
               "fair_entry": tr.fair, "px_entry": tr.px})
mkdf = pd.DataFrame(mk)
valid = mkdf.dropna(subset=["mid_after"])
conv = (valid.mid_after - valid.px_entry).describe()
print("\n=== markout: market mid 10 snaps after entry minus entry px ===")
print(conv.round(4).to_dict())

# ---------- 4. early exit variant ----------
# exit rule: sell at opposite book when fair converges within 0.01, else hold
res = []
for _, tr in t3.iterrows():
    fut = sc[(sc.mts == tr.mts) & (sc.sec > tr.sec) & (sc.sec <= tr.mts + 299)].sort_values("sec")
    pnl = None
    for _, r in fut.iterrows():
        fair = r.fair_up if tr.side == "up" else 1 - r.fair_up
        bid = r[f"bb_{tr.side}"]
        if abs(fair - (bid + (r[f'ba_{tr.side}']))) / 2 < 0.01 or tr.sec - tr.sec > 240:
            break
    res.append(pnl)
print("(early-exit needs tick-level sim; deferred to live paper phase)")

# ---------- 5. hour-of-day ----------
t3["hour"] = pd.to_datetime(t3.mts, unit="s", utc=True).dt.hour
byh = t3.groupby("hour").pnl.agg(["count", "sum", "mean"])
print("\n=== P&L by UTC hour ===")
print(byh.round(4).to_string())
