"""Pair the two token books per (window, second) correctly, then score + backtest."""
from pathlib import Path
import math
import numpy as np
import pandas as pd

OUTD = Path(__file__).parent / "out"

def load_and_pair() -> pd.DataFrame:
    snaps = pd.read_parquet(OUTD / "snapshots.parquet")
    tmap = pd.read_csv(OUTD / "token_map.csv", dtype={"up": str, "down": str})
    s = snaps.copy()
    s["sec"] = s.ts.astype("int64")
    # last snapshot per (window, token, second)
    s = s.sort_values("ts").groupby(["mts", "token", "sec"]).tail(1)

    long_rows = []
    for col, sidecol in (("up", "up"), ("down", "dn")):
        tm = tmap.dropna(subset=[col])[["mts", col]]
        ren = {"bb": f"bb_{sidecol}", "ba": f"ba_{sidecol}",
               "imb5": f"imb5_{sidecol}", "qa": f"qa_{sidecol}",
               "bq": f"bq_{sidecol}"}
        part = s.merge(tm, left_on=["mts", "token"], right_on=["mts", col],
                       how="inner").rename(columns=ren)
        long_rows.append(part[["mts", "sec"] + list(ren.values())])
    up_df, dn_df = long_rows
    piv = up_df.merge(dn_df, on=["mts", "sec"], how="outer")
    piv = piv.sort_values(["mts", "sec"])
    return piv

def attach_fair(piv: pd.DataFrame) -> pd.DataFrame:
    btc = pd.read_parquet(OUTD / "btc_1m_aug12_25.parquet").set_index("ts")
    rets = np.log(btc["close"] / btc["open"])
    rv60 = rets.pow(2).rolling(60).mean().pow(0.5) / math.sqrt(60.0)
    idx = pd.DatetimeIndex(pd.to_datetime(piv.sec, unit="s")).tz_localize("UTC")
    minute_idx = idx.floor("min")
    piv["spot"] = btc["close"].reindex(minute_idx - pd.Timedelta(seconds=60)).values
    widx = pd.to_datetime(piv.mts, unit="s", utc=True)
    piv["S_open"] = btc["open"].reindex(widx).values
    piv["sigma_s"] = rv60.reindex(minute_idx - pd.Timedelta(seconds=60)).values
    piv["tau"] = piv.mts + 300 - piv.sec
    piv = piv.dropna(subset=["spot", "S_open", "sigma_s"])
    piv["d"] = np.log(piv.spot / piv.S_open)
    z = piv.d / (piv.sigma_s * np.sqrt(np.maximum(piv.tau.clip(lower=1), 1)))
    piv["fair_up"] = 0.5 * (1.0 + np.vectorize(math.erf)(z.to_numpy() / math.sqrt(2)))
    widx_d = pd.DatetimeIndex(widx).drop_duplicates()
    o = btc["open"].reindex(widx_d).values
    c = btc["close"].reindex(widx_d + pd.Timedelta(minutes=4)).values
    wmap = pd.Series(np.where(c >= o, "up", "dn"), index=widx_d)
    piv["btc_winner"] = pd.to_datetime(piv.mts, unit="s", utc=True).map(wmap)
    # require both sides quoted to consider any trade
    return piv.dropna(subset=["ba_up", "ba_dn"])

def backtest(piv: pd.DataFrame, theta: float, tau_lo=20, tau_hi=270,
             fee=0.0, per_window=True):
    m = piv[(piv.tau > tau_lo) & (piv.tau < tau_hi)].copy()
    recs = []
    for side in ("up", "dn"):
        fair = m.fair_up if side == "up" else 1.0 - m.fair_up
        edge = fair - m[f"ba_{side}"]
        sub = pd.DataFrame({"mts": m.mts[edge > theta], "sec": m.sec[edge > theta],
                            "side": side, "px": m[f"ba_{side}"][edge > theta],
                            "fair": fair[edge > theta]})
        recs.append(sub)
    tdf = pd.concat(recs).sort_values("sec")
    if per_window:
        tdf = tdf.groupby(["mts", "side"]).first().reset_index()
    tdf["win"] = (tdf.side == tdf.mts.map(
        piv.drop_duplicates("mts").set_index("mts").btc_winner)).astype(float)
    tdf["pnl"] = tdf.win - tdf.px - fee * tdf.px
    ev, sd = tdf.pnl.mean(), tdf.pnl.std()
    return {"theta": theta, "fee": fee, "n": len(tdf), "hit": round(tdf.win.mean(), 4),
            "avg_px": round(tdf.px.mean(), 3), "avg_pnl": round(ev, 4),
            "sharpe": round(ev / max(sd, 1e-9), 3), "tot": round(tdf.pnl.sum(), 1)}

def main():
    piv = load_and_pair()
    print("paired rows:", len(piv), "| windows:", piv.mts.nunique())
    piv = attach_fair(piv)
    print("scored rows:", len(piv))
    piv.to_parquet(OUTD / "scored.parquet")
    print(f"{'theta':>6} {'fee':>5} {'n':>7} {'hit':>7} {'avg_px':>7} {'avg_pnl':>8} {'sharpe':>7} {'tot':>9}")
    for theta in (0.01, 0.02, 0.03, 0.05, 0.08):
        r = backtest(piv, theta)
        print(f"{r['theta']:>6} {r['fee']:>5} {r['n']:>7} {r['hit']:>7} {r['avg_px']:>7} "
              f"{r['avg_pnl']:>8} {r['sharpe']:>7} {r['tot']:>9}")

if __name__ == "__main__":
    main()
