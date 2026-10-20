"""
Which resolution rule does Polymarket ACTUALLY use? Let the market tell us.

model_vs_market_final.py showed my GBM model beating the market even out-of-sample --
but with a suspicious tell: at tau<30s the market scored only AUC 0.83. With 25 seconds
left, traders can see spot and the outcome is nearly determined; the market should be
near-perfect there. Something is corrupting the MARKET's score but not the model's.

Two candidates, both of which would do exactly that (they distort the token->outcome
alignment and the label, while the model's spot-based P(up) is unaffected):
  1. the token map is wrong for some windows
  2. my ground-truth rule is wrong

This script tests #2 by exploiting a simple fact: near expiry, the market knows the
answer. Whichever candidate rule best matches the market's own near-expiry consensus
is almost certainly the real rule. The market is the referee here, not the model.

The market description says: resolve Up if the Chainlink 60s-TWAP "of the time range
specified in the title" >= "the price at the beginning of that range". Both halves are
ambiguous, so we test the plausible readings.
"""
import numpy as np
import pandas as pd

CANDIDATES = {
    "A_point_to_point":   "close(last sec) >= open(first sec)",
    "B_twap_vs_open":     "mean(last 60s) >= open(first sec)          [what I assumed]",
    "C_twap_vs_first60":  "mean(last 60s) >= mean(first 60s of window)",
    "D_twap_vs_prev60":   "mean(last 60s) >= mean(60s ending at window open)",
}


def build_labels(spot_path):
    s1 = pd.read_parquet(spot_path)
    s1["ts_s"] = s1["ts"].values.astype("datetime64[s]").astype("int64")
    s1 = s1.sort_values("ts_s").drop_duplicates("ts_s").reset_index(drop=True)
    s1["mts"] = (s1["ts_s"] // 300) * 300
    s1["sec"] = s1["ts_s"] - s1["mts"]

    n_sec = s1.groupby("mts").size()
    full = n_sec[n_sec >= 250].index

    g = s1[s1["mts"].isin(full)]
    first_open = g.groupby("mts")["open"].first()
    last_close = g.groupby("mts")["close"].last()
    twap_last60 = g[g["sec"] >= 240].groupby("mts")["close"].mean()
    twap_first60 = g[g["sec"] < 60].groupby("mts")["close"].mean()

    # 60s ending AT window open = the final 60s of the PREVIOUS window
    prev60 = twap_last60.copy()
    prev60.index = prev60.index + 300          # shift forward one window
    prev60 = prev60.reindex(twap_last60.index)

    out = pd.DataFrame({
        "A_point_to_point": (last_close >= first_open).astype(float),
        "B_twap_vs_open": (twap_last60 >= first_open).astype(float),
        "C_twap_vs_first60": (twap_last60 >= twap_first60).astype(float),
        "D_twap_vs_prev60": (twap_last60 >= prev60).astype(float),
    })
    return out


def market_consensus(snap_path, tmap_path, max_tau=30):
    sn = pd.read_parquet(snap_path)
    sn["ts_s"] = sn["ts"].astype("int64")
    sn["tau"] = sn["mts"] + 300 - sn["ts_s"]
    tm = pd.read_csv(tmap_path, dtype={"up": str, "down": str}).dropna(subset=["up", "down"])
    up_of = dict(zip(tm["mts"], tm["up"]))

    late = sn[(sn["tau"] >= -5) & (sn["tau"] <= max_tau)].copy()
    late["up_token"] = late["mts"].map(up_of)
    late = late[late["up_token"].notna() & (late["token"] == late["up_token"])]
    late = late.sort_values("ts").groupby("mts").tail(1)
    late["mid"] = (late["bb"] + late["ba"]) / 2
    # only windows where the market is actually confident
    conf = late[(late["mid"] > 0.85) | (late["mid"] < 0.15)]
    return pd.Series((conf["mid"] > 0.5).astype(int).values, index=conf["mts"].values)


def report(tag, spot_path, snap_path, tmap_path):
    print(f"\n{'='*72}\n{tag}\n{'='*72}")
    labels = build_labels(spot_path)
    for max_tau in (30, 60):
        mkt = market_consensus(snap_path, tmap_path, max_tau=max_tau)
        common = labels.index.intersection(mkt.index)
        if len(common) < 30:
            print(f"  tau<={max_tau}: only {len(common)} confident windows, skipping")
            continue
        print(f"\n  Market's confident verdict within tau<={max_tau}s, on {len(common)} windows:")
        print(f"  {'rule':>20}  {'agrees with market':>20}")
        for name in CANDIDATES:
            lab = labels.loc[common, name]
            ok = lab.notna()
            agree = (lab[ok].astype(int).values == mkt.loc[common][ok.values].values).mean()
            print(f"  {name:>20}  {agree*100:>19.1f}%")
    print(f"\n  rule definitions:")
    for k, v in CANDIDATES.items():
        print(f"    {k:>20}: {v}")

    print(f"\n  pairwise disagreement between rules (all windows):")
    ks = list(CANDIDATES)
    for i in range(len(ks)):
        for j in range(i + 1, len(ks)):
            a, b = labels[ks[i]], labels[ks[j]]
            ok = a.notna() & b.notna()
            print(f"    {ks[i]:>20} vs {ks[j]:<20} {(a[ok]!=b[ok]).mean()*100:5.2f}%")


if __name__ == "__main__":
    report("IN-SAMPLE Aug 12-25",
           "edge_mining/out/btc_1s_aug12_25.parquet",
           "edge_mining/out/snapshots.parquet",
           "edge_mining/out/token_map.csv")
    report("OUT-OF-SAMPLE Aug 25-27",
           "edge_mining/out/btc_1s_oos.parquet",
           "edge_mining/out/snapshots_oos.parquet",
           "edge_mining/out/token_map_oos.csv")
