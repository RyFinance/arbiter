"""Independent verification of Round 2 (E1-E9, EXPERIMENTS.md) champion
edges against the TWAP-corrected resolution ground truth built yesterday
(verify_resolution_and_edge.py). Round 2's own pipeline (edge_lib.py /
pair_and_backtest.py) still uses the naive 1m close>=open winner -- this
checks whether "edge A" / "edge D" survive the fix, and reports real hit
rates (not just $/day) since that's what's actually being asked about.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import numpy as np
import pandas as pd
import edge_lib as L
from verify_resolution_and_edge import build_twap_ground_truth

piv = L.load_paired()
variants, btc = L.sigma_variants()
base_idx = (pd.DatetimeIndex(pd.to_datetime(piv.sec, unit="s")).tz_localize("UTC")
            .floor("min") - pd.Timedelta(seconds=60))
sigma_ew15 = variants["ewma15m"].reindex(base_idx).to_numpy()
piv["fair_up"] = L.fair_with_sigma(piv, sigma_ew15, "ewma15m")

twap_gt = build_twap_ground_truth()
twap_winner = twap_gt.set_index("mts")["twap_dir"]

naive_winner = piv.drop_duplicates("mts").set_index("mts")["btc_winner"]
disagree = (twap_winner.reindex(naive_winner.index) != naive_winner).sum()
n_common = naive_winner.index.isin(twap_winner.index).sum()
print(f"Windows with both labels: {n_common}, disagreements: {disagree} "
      f"({disagree / n_common * 100:.2f}%)")


def taker_backtest_custom_winner(piv, theta, winner_map, tau_lo=20, tau_hi=270, clip=75):
    m = piv[(piv.tau > tau_lo) & (piv.tau < tau_hi)].copy()
    recs = []
    for side in ("up", "dn"):
        fair = m.fair_up if side == "up" else 1.0 - m.fair_up
        e = (fair - m[f"ba_{side}"]) > theta
        recs.append(pd.DataFrame({"mts": m.mts[e], "sec": m.sec[e], "side": side,
                                   "px": m[f"ba_{side}"][e], "fair": fair[e],
                                   "sh": m[f"qa_{side}"][e].clip(upper=clip)}))
    t = pd.concat(recs).sort_values("sec")
    t = t.groupby(["mts", "side"]).first().reset_index()
    t["win"] = t.mts.map(winner_map)
    t = t.dropna(subset=["win"])
    t["win"] = (t.side == t.win).astype(float)
    t["gross"] = t.win - t.px
    t["fee"] = L.taker_fee(t.px)
    t["pnl"] = t.gross - t.fee
    return t


def report(t, label):
    if len(t) == 0:
        print(f"  {label}: NO TRADES")
        return
    s = L.summary(t)
    print(f"  {label:<40} n/day={s['n/day']:>5.1f} hit={s['hit']:.4f} "
          f"net/sh={s['net/sh']:+.4f} net/day$={s['net/day_usd']:>7.1f} "
          f"posdays={s['pos_days']:.3f}")


print()
print("=== EDGE A: up-only, tau(150,270), theta=0.12, EWMA15m ===")
tA_naive = taker_backtest_custom_winner(piv, 0.12, naive_winner, tau_lo=150, tau_hi=270)
tA_naive = tA_naive[tA_naive.side == "up"]
report(tA_naive, "A (naive/wrong ground truth, as in EXPERIMENTS.md)")

tA_twap = taker_backtest_custom_winner(piv, 0.12, twap_winner, tau_lo=150, tau_hi=270)
tA_twap = tA_twap[tA_twap.side == "up"]
report(tA_twap, "A (TWAP-corrected ground truth)")

print()
print("=== EDGE D: up-always + dn-on-up-days, tau(150,270), theta=0.08, EWMA15m ===")
for winner_map, label in [(naive_winner, "naive/wrong"), (twap_winner, "TWAP-corrected")]:
    t = taker_backtest_custom_winner(piv, 0.08, winner_map, tau_lo=150, tau_hi=270)
    t["day"] = pd.to_datetime(t.mts, unit="s", utc=True).dt.date
    btc_d = btc.close.resample("1D").last()
    day_ret = (btc_d / btc_d.shift(1) - 1)
    day_ret.index = day_ret.index.date
    t["day_ret"] = t.day.map(day_ret.to_dict())
    t = t[(t.side == "up") | (t.day_ret > 0)]
    report(t, f"D ({label} ground truth)")

print()
print("=== For reference: baseline E0 (both sides, all tau, rv60 sigma), both ground truths ===")
piv0 = piv.copy()
sigma_rv60 = variants["rv60m"].reindex(base_idx).to_numpy()
piv0["fair_up"] = L.fair_with_sigma(piv0, sigma_rv60, "rv60m")
for winner_map, label in [(naive_winner, "naive/wrong"), (twap_winner, "TWAP-corrected")]:
    t = taker_backtest_custom_winner(piv0, 0.03, winner_map, tau_lo=20, tau_hi=270)
    report(t, f"E0 baseline theta=0.03 ({label})")
