"""E4: Order-book imbalance as a short-horizon signal (new edge)."""
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
import numpy as np
import pandas as pd
import edge_lib as L

piv = L.load_paired()
p = piv[(piv.tau > 20) & (piv.tau < 270)].copy()
# normalized top-of-book imbalance, from up-side when present else dn mirrored
p["imb"] = np.where(p.bq_up.notna() & p.qa_up.notna(),
                    (p.bq_up - p.qa_up) / (p.bq_up + p.qa_up),
                    -(p.bq_dn - p.qa_dn) / (p.bq_dn + p.qa_dn))
p["mid"] = np.where(p.bb_up.notna() & p.ba_up.notna(),
                    (p.bb_up + p.ba_up) / 2, 1 - (p.bb_dn + p.ba_dn) / 2)

key = p.set_index(["mts", "sec"]).sort_index()
for h in (5, 10, 20):
    fwd = []
    for mts, g in key.groupby(level=0):
        g = g.sort_index()
        mids = g.mid
        fwd.append(pd.Series(mids.shift(-h) - mids, index=mids.index, name="fwd"))
    f = pd.concat(fwd)
    m = p[["mts", "sec", "imb"]].merge(f.rename("fwd"), on=["mts", "sec"],
                                       how="left")
    v = m.dropna(subset=["imb", "fwd"])
    c = np.corrcoef(v.imb, v.fwd)[0, 1]
    print(f"horizon {h:>2}s: corr(imb, dmid) = {c:+.4f}  (n={len(v)})")
    for th in (0.5, 0.7):
        strong = v[v.imb.abs() > th]
        if not len(strong):
            continue
        # if corr>0 trade momentum else reversal
        sig_side_sign = 1 if c > 0 else -1
        # simulate: enter side (up if imb>0*dir), exit h seconds later at bid
        pnls = []
        for mts, sec, imb in zip(strong.mts, strong.sec, strong.imb):
            want_up = (imb > 0) == (sig_side_sign == 1)
            side = "up" if want_up else "dn"
            row = key.loc[(mts, sec)]
            ask = row[f"ba_{side}"]
            # find exit row ~h seconds later
            g = key.xs(mts)
            g = g[g.index > sec]
            if len(g) < 1:
                continue
            exit_row = g.iloc[min(h, len(g) - 1)]
            bid = exit_row[f"bb_{side}"]
            if np.isnan(ask) or np.isnan(bid):
                continue
            pnls.append(bid - ask - L.taker_fee(ask) - L.taker_fee(bid))
        if pnls:
            arr = np.array(pnls)
            nd = pd.to_datetime(strong.mts, unit="s", utc=True).dt.date.nunique()
            print(f"  th={th}: n={len(arr)} pnl/sh={arr.mean():+.5f} "
                  f"hit={ (arr>0).mean():.3f} net/day$={(arr*60).sum()/nd:.0f}")
