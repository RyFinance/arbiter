"""Score every depth snapshot against Brownian-bridge fair value; backtest.

Usage:
  python analyze_edge.py            # full run after snapshots.parquet exists
"""
from pathlib import Path
import math
import numpy as np
import pandas as pd

OUT = Path(__file__).parent
OUTD = OUT / "out"

def load_and_build():
    snaps = pd.read_parquet(OUTD / "snapshots.parquet")
    tmap = pd.read_csv(OUTD / "token_map.csv", dtype={"up": str, "down": str})
    long_up = tmap.dropna(subset=["up"])[["mts", "up"]].rename(
        columns={"up": "token"}).assign(side="up")
    long_dn = tmap.dropna(subset=["down"])[["mts", "down"]].rename(
        columns={"down": "token"}).assign(side="dn")
    tok2side = pd.concat([long_up, long_dn])
    s = snaps.merge(tok2side, on=["mts", "token"], how="inner")
    piv = s.pivot_table(index=["mts", "ts"], columns="side",
                        values=["bb", "ba", "bq", "qa", "imb5"], aggfunc="first")
    piv.columns = [f"{a}_{b}" for a, b in piv.columns]
    piv = piv.reset_index()
    return piv

def attach_btc_fair(piv: pd.DataFrame) -> pd.DataFrame:
    btc = pd.read_parquet(OUTD / "btc_1m_aug12_25.parquet").set_index("ts")
    rets = np.log(btc["close"] / btc["open"])
    rv60 = rets.pow(2).rolling(60).mean().pow(0.5) / math.sqrt(60.0)
    sec = piv.ts.values.astype("datetime64[s]")
    idx = pd.DatetimeIndex(sec).tz_localize("UTC")
    minute_idx = idx.floor("min")
    piv["spot"] = btc["close"].reindex(minute_idx - pd.Timedelta(seconds=60)).values
    piv["S_open"] = btc["open"].reindex(pd.to_datetime(piv.mts, unit="s", utc=True)).values
    piv["sigma_s"] = rv60.reindex(minute_idx - pd.Timedelta(seconds=60)).values
    piv["tau"] = piv.mts + 300 - piv.ts.astype("int64")
    piv["d"] = np.log(piv.spot / piv.S_open)
    z = piv.d / (piv.sigma_s * np.sqrt(np.maximum(piv.tau, 1)))
    piv["fair_up"] = 0.5 * (1.0 + np.vectorize(math.erf)(z.to_numpy() / math.sqrt(2)))
    # BTC winner for this window
    o = btc["open"].reindex(pd.to_datetime(piv.mts, unit="s", utc=True)).values
    c = btc["close"].reindex(pd.to_datetime(piv.mts, unit="s", utc=True)
                             + pd.Timedelta(minutes=4)).values
    piv["btc_winner"] = np.where(c >= o, "up", "dn")
    return piv

def backtest(piv: pd.DataFrame, theta: float, tau_lo=20, tau_hi=270,
             fee=0.0, one_per_window=True) -> dict:
    m = piv[(piv.tau > tau_lo) & (piv.tau < tau_hi)].copy()
    recs = []
    for side in ("up", "dn"):
        fair = m.fair_up if side == "up" else 1.0 - m.fair_up
        edge = fair - m[f"ba_{side}"]
        hit = edge > theta
        sub = pd.DataFrame({"mts": m.mts[hit], "ts": m.ts[hit], "side": side,
                            "px": m[f"ba_{side}"][hit], "fair": fair[hit],
                            "size": m[f"qa_{side}"][hit]})
        recs.append(sub)
    tdf = pd.concat(recs).sort_values("ts")
    if one_per_window:
        tdf = tdf.groupby(["mts", "side"]).first().reset_index()
    tdf["win"] = (tdf.side == tdf.mts.map(
        piv.drop_duplicates("mts").set_index("mts").btc_winner)).astype(float)
    tdf["pnl"] = tdf.win - tdf.px - fee * tdf.px
    ev = tdf.pnl.mean()
    sd = tdf.pnl.std()
    return {"theta": theta, "fee": fee, "n": len(tdf),
            "hit": round(tdf.win.mean(), 4),
            "avg_pnl": round(ev, 5), "sd": round(sd, 4),
            "sharpe_t": round(ev / max(sd, 1e-9), 3),
            "tot_shares": round(tdf.pnl.sum(), 2),
            "avg_size": round(tdf["size"].mean(), 1)}

def main():
    piv = load_and_build()
    print("matched snapshots:", len(piv))
    piv = attach_btc_fair(piv)
    piv.to_parquet(OUTD / "scored.parquet")
    print(f"{'theta':>6} {'fee':>5} {'n':>7} {'hit':>7} {'avg_pnl':>9} {'sharpe':>7} {'tot':>9}")
    for theta in (0.01, 0.02, 0.03, 0.05, 0.08):
        for fee in (0.0, 0.02):
            r = backtest(piv, theta, fee=fee)
            print(f"{r['theta']:>6} {r['fee']:>5} {r['n']:>7} {r['hit']:>7} "
                  f"{r['avg_pnl']:>9} {r['sharpe_t']:>7} {r['tot_shares']:>9}")

if __name__ == "__main__":
    main()
