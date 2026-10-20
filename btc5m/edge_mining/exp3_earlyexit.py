"""E3: Early-exit variant — recycle capital instead of holding to expiry."""
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
import numpy as np
import pandas as pd
import edge_lib as L

piv = L.load_paired()
t = L.taker_backtest(piv, 0.08)

# index: (mts, sec) -> per-side quotes
key = piv.set_index(["mts", "sec"]).sort_index()

rows = []
for tp in (0.015, 0.02, 0.03):
    for max_hold in (60, 120):
        pnls, exits, held = [], [], []
        for _, tr in t.iterrows():
            fut = key.xs(tr.mts)
            fut = fut[fut.index > tr.sec].sort_index()
            bid_col = f"bb_{tr.side}"
            exited = False
            pnl = None
            for sec, row in fut.iterrows():
                if np.isnan(row[bid_col]):
                    continue
                held_t = sec - tr.sec
                if row[bid_col] - tr.px >= tp and held_t <= max_hold:
                    pnl = row[bid_col] - tr.px - L.taker_fee(tr.px) - L.taker_fee(row[bid_col])
                    exited = True
                    break
                if held_t > max_hold:
                    pnl = row[bid_col] - tr.px - L.taker_fee(tr.px) - L.taker_fee(row[bid_col])
                    exited = True
                    break
            if not exited:
                pnl = tr.win - tr.px - L.taker_fee(tr.px)
                held_t = 300
            pnls.append(pnl); exits.append(exited); held.append(held_t)
        pnl = np.array(pnls)
        ndays = pd.to_datetime(t.mts, unit="s", utc=True).dt.date.nunique()
        print(f"tp={tp} maxhold={max_hold}s: early_exit={np.mean(exits)*100:.0f}% "
              f"med_hold={np.median(held):.0f}s net/day=${(pnl*t.sh).sum()/ndays:.0f} "
              f"sharpe={pnl.mean()/max(pnl.std(),1e-9):.3f} gross_eq={pnl.mean():.4f}")
