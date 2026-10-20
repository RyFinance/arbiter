"""Full out-of-sample validation: derive token map + both ground truths +
run the EXACT fixed filters (edge A, edge D, E0 baseline) discovered on
2026-08-12 -> 2026-08-25T08:07 data, against the genuinely fresh
2026-08-25T09:00 -> 2026-08-27 data. No re-fitting of any threshold here.
"""
from pathlib import Path
import math
import numpy as np
import pandas as pd

OUTD = Path(__file__).parent / "out"


def derive_token_map_oos():
    snaps = pd.read_parquet(OUTD / "snapshots_oos.parquet")
    snaps["sec"] = snaps.ts.astype("int64")
    snaps["tau"] = snaps.mts + 300 - snaps.sec
    btc = pd.read_parquet(OUTD / "btc_1m_oos.parquet").set_index("ts")

    cand = snaps[(snaps.tau >= -5) & (snaps.tau <= 90)].copy()
    cand["mid"] = (cand.bb + cand.ba) / 2
    last = cand.sort_values("ts").groupby(["mts", "token"]).tail(1)
    w = last.groupby("mts").mid.idxmax()
    winners = last.loc[w].set_index("mts")

    mtsi = pd.to_datetime(winners.index, unit="s", utc=True)
    o = btc.open.reindex(mtsi).values
    c = btc.close.reindex(mtsi + pd.Timedelta(minutes=4)).values
    res = pd.DataFrame({"token": winners.token, "win_mid": winners.mid,
                        "tau": winners.tau,
                        "dir": np.where(c >= o, "up", "dn")}, index=winners.index)
    ok = res[(res.win_mid > 0.85) & (res.tau <= 60)].reset_index()

    tok_pairs = snaps.groupby("mts").token.unique().to_frame("toks")
    ok = ok.merge(tok_pairs, on="mts", how="left")

    def other(toks, win_tok):
        toks = list(toks)
        if len(toks) < 2 or win_tok not in toks:
            return None
        toks.remove(win_tok)
        return toks[0]

    ok["loser"] = [other(t, w) for t, w in zip(ok.toks, ok.token)]
    ups = ok[ok.dir == "up"][["mts", "token", "loser"]].rename(columns={"token": "up", "loser": "down"})
    dns = ok[ok.dir == "dn"][["mts", "token", "loser"]].rename(columns={"token": "down", "loser": "up"})
    tmap = pd.concat([ups, dns]).sort_values("mts")[["mts", "up", "down"]]
    print(f"OOS token map: {len(tmap)} windows mapped of {snaps.mts.nunique()} total "
          f"(both sides: {(tmap.up.notna() & tmap.down.notna()).sum()})")
    return tmap, snaps, btc


def load_and_pair_oos(snaps, tmap):
    s = snaps.sort_values("ts").groupby(["mts", "token", "sec"]).tail(1)
    long_rows = []
    for col, sidecol in (("up", "up"), ("down", "dn")):
        tm = tmap.dropna(subset=[col])[["mts", col]]
        ren = {"bb": f"bb_{sidecol}", "ba": f"ba_{sidecol}",
               "imb5": f"imb5_{sidecol}", "qa": f"qa_{sidecol}", "bq": f"bq_{sidecol}"}
        part = s.merge(tm, left_on=["mts", "token"], right_on=["mts", col],
                       how="inner").rename(columns=ren)
        long_rows.append(part[["mts", "sec"] + list(ren.values())])
    up_df, dn_df = long_rows
    piv = up_df.merge(dn_df, on=["mts", "sec"], how="outer")
    return piv.sort_values(["mts", "sec"])


def attach_fair_oos(piv, btc):
    rets = np.log(btc["close"] / btc["open"])
    rv60 = rets.pow(2).rolling(60).mean().pow(0.5) / math.sqrt(60.0)
    lam15 = 0.5 ** (1.0 / 15)
    ewma15 = rets.pow(2).ewm(alpha=1 - lam15, adjust=False).mean().pow(0.5) / math.sqrt(60.0)

    idx = pd.DatetimeIndex(pd.to_datetime(piv.sec, unit="s")).tz_localize("UTC")
    minute_idx = idx.floor("min")
    base_idx = minute_idx - pd.Timedelta(seconds=60)
    piv["spot"] = btc["close"].reindex(base_idx).values
    widx = pd.to_datetime(piv.mts, unit="s", utc=True)
    piv["S_open"] = btc["open"].reindex(widx).values
    piv["sigma_ewma15"] = ewma15.reindex(base_idx).values
    piv["tau"] = piv.mts + 300 - piv.sec
    piv = piv.dropna(subset=["spot", "S_open", "sigma_ewma15"])
    piv["d"] = np.log(piv.spot / piv.S_open)
    z = piv.d / (piv.sigma_ewma15 * np.sqrt(np.maximum(piv.tau.clip(lower=1), 1)))
    piv["fair_up"] = 0.5 * (1.0 + np.vectorize(math.erf)(z.to_numpy() / math.sqrt(2)))

    widx_d = pd.DatetimeIndex(widx).drop_duplicates()
    o = btc["open"].reindex(widx_d).values
    c = btc["close"].reindex(widx_d + pd.Timedelta(minutes=4)).values
    wmap_naive = pd.Series(np.where(c >= o, "up", "dn"), index=widx_d)
    piv["btc_winner_naive"] = pd.to_datetime(piv.mts, unit="s", utc=True).map(wmap_naive)
    return piv.dropna(subset=["ba_up", "ba_dn"])


def build_twap_winner_oos(tmap, btc1s):
    rows = []
    for mts in sorted(tmap.mts.unique()):
        open_t = pd.Timestamp(mts, unit="s", tz="UTC")
        close_t = open_t + pd.Timedelta(seconds=300)
        twap_start = close_t - pd.Timedelta(seconds=30)
        try:
            open_px = btc1s.loc[open_t, "close"]
        except KeyError:
            continue
        window_1s = btc1s.loc[twap_start:close_t - pd.Timedelta(seconds=1), "close"]
        if len(window_1s) < 15:
            continue
        twap_close = float(window_1s.mean())
        rows.append({"mts": mts, "twap_dir": "up" if twap_close >= open_px else "dn"})
    return pd.DataFrame(rows).set_index("mts")["twap_dir"]


FEE_RATE = 0.07


def taker_fee(p):
    return FEE_RATE * p * (1.0 - p)


def taker_backtest(piv, theta, winner_map, tau_lo, tau_hi, clip=75):
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
    t["fee"] = taker_fee(t.px)
    t["pnl"] = t.gross - t.fee
    return t


def summary(t, ndays):
    if len(t) == 0:
        return {"n": 0}
    return {"n": len(t), "n/day": round(len(t) / ndays, 1), "hit": round(t.win.mean(), 4),
            "avg_px": round(t.px.mean(), 4), "net/sh": round(t.pnl.mean(), 4),
            "sharpe": round(t.pnl.mean() / max(t.pnl.std(), 1e-9), 4),
            "net/day$": round((t.pnl * t.sh).sum() / ndays, 1),
            "pos_days": round((t.assign(d=pd.to_datetime(t.mts, unit="s", utc=True).dt.date)
                               .groupby("d").apply(lambda g: (g.pnl * g.sh).sum()) > 0).mean(), 3)}


def main():
    tmap, snaps, btc1m = derive_token_map_oos()
    piv = load_and_pair_oos(snaps, tmap)
    piv = attach_fair_oos(piv, btc1m)
    print(f"OOS scored rows: {len(piv)}, windows: {piv.mts.nunique()}")

    btc1s = pd.read_parquet(OUTD / "btc_1s_oos.parquet").set_index("ts")
    twap_winner = build_twap_winner_oos(tmap, btc1s)
    naive_winner = piv.drop_duplicates("mts").set_index("mts")["btc_winner_naive"]

    common = naive_winner.index.intersection(twap_winner.index)
    disagree = (naive_winner.loc[common] != twap_winner.loc[common]).sum()
    print(f"OOS ground-truth disagreement: {disagree}/{len(common)} "
          f"({disagree / max(len(common),1) * 100:.2f}%)")

    ndays = (pd.to_datetime(piv.mts, unit="s", utc=True).dt.date.nunique())
    print(f"OOS days spanned: {ndays}")

    print()
    print("=== EDGE A (fixed: up-only, tau(150,270), theta=0.12) ===")
    for winner_map, label in [(naive_winner, "naive"), (twap_winner, "TWAP-corrected")]:
        t = taker_backtest(piv, 0.12, winner_map, 150, 270)
        t = t[t.side == "up"]
        print(f"  {label:<16}", summary(t, ndays))

    print()
    print("=== EDGE D (fixed: up-always + dn-on-up-days, tau(150,270), theta=0.08) ===")
    btc_d = btc1m.close.resample("1D").last()
    day_ret = (btc_d / btc_d.shift(1) - 1)
    day_ret.index = day_ret.index.date
    for winner_map, label in [(naive_winner, "naive"), (twap_winner, "TWAP-corrected")]:
        t = taker_backtest(piv, 0.08, winner_map, 150, 270)
        t["day"] = pd.to_datetime(t.mts, unit="s", utc=True).dt.date
        t["day_ret"] = t.day.map(day_ret.to_dict())
        t = t[(t.side == "up") | (t.day_ret > 0)]
        print(f"  {label:<16}", summary(t, ndays))

    print()
    print("=== E0 BASELINE (fixed: both sides, all tau(20,270), theta=0.03) ===")
    for winner_map, label in [(naive_winner, "naive"), (twap_winner, "TWAP-corrected")]:
        t = taker_backtest(piv, 0.03, winner_map, 20, 270)
        print(f"  {label:<16}", summary(t, ndays))


if __name__ == "__main__":
    main()
