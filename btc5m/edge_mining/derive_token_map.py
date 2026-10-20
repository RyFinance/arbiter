"""Derive up/down token mapping empirically (v2, quality-gated).

Winner token = higher mid among near-expiry snapshots (tau in [-5, 90]);
require decisive convergence (win_mid > 0.85, tau <= 60) and BTC ground truth.
"""
from pathlib import Path
import numpy as np
import pandas as pd

OUT = Path(__file__).parent

def main():
    snaps = pd.read_parquet(OUT / "out" / "snapshots.parquet")
    snaps["sec"] = snaps.ts.astype("int64")
    snaps["tau"] = snaps.mts + 300 - snaps.sec
    btc = pd.read_parquet(OUT / "out" / "btc_1m_aug12_25.parquet").set_index("ts")

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

    # the loser token = the other distinct token captured in that window
    tok_pairs = snaps.groupby("mts").token.unique().to_frame("toks")
    ok = ok.merge(tok_pairs, on="mts", how="left")
    def other(toks, win_tok):
        toks = list(toks)
        if len(toks) < 2 or win_tok not in toks:
            return None
        toks.remove(win_tok)
        return toks[0]
    ok["loser"] = [other(t, w) for t, w in zip(ok.toks, ok.token)]

    ups = ok[ok.dir == "up"][["mts", "token", "loser"]].rename(
        columns={"token": "up", "loser": "down"})
    dns = ok[ok.dir == "dn"][["mts", "token", "loser"]].rename(
        columns={"token": "down", "loser": "up"})
    tmap = pd.concat([ups, dns]).sort_values("mts")[["mts", "up", "down"]]
    tmap.to_csv(OUT / "out" / "token_map.csv", index=False)
    n_both = (tmap.up.notna() & tmap.down.notna()).sum()
    print(f"mapped {len(tmap)} windows ({n_both} with both sides) "
          f"of {snaps.mts.nunique()} total")

if __name__ == "__main__":
    main()
