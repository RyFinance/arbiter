"""
OUT-OF-SAMPLE test of the model-vs-market result.

model_vs_market.py found the GBM/TWAP fair-value model beats the market's own price
at every horizon -- but on the Aug 12-25 window, which is exactly the in-sample period
where edge_mining's very similar fair-value model looked strong and then died OOS
(see OOS_VALIDATION_2026-08-26.md). So that result cannot be trusted until it is
re-run on genuinely fresh data.

This runs the IDENTICAL test, no re-fitting of anything, on the held-out capture:
Aug 25 09:05 -> Aug 27 00:05 (snapshots_oos.parquet), which was never used to build
or tune the model.

Token map: derived here rather than reused, because the existing OOS derivation in
oos_full_validate.py labels up/down tokens using the NAIVE close>=open rule, which
disagrees with the real TWAP resolution on ~7% of windows and would mislabel those.
This version derives direction from the true 60s TWAP computed off 1-second data.
"""
import argparse
import numpy as np
import pandas as pd
from math import sqrt
from scipy.special import ndtr
from sklearn.metrics import roc_auc_score, brier_score_loss

TWAP_L = 60.0
BUCKET_S = 10
VOL_LOOKBACK_MIN = 60


def var_eff_seconds(tau, L=TWAP_L):
    tau = np.asarray(tau, dtype=float)
    return np.where(tau >= L, (tau - L) + L / 3.0, (tau / L) ** 2 * (tau / 3.0))


def ece(p, y, nbins=10):
    b = np.clip((p * nbins).astype(int), 0, nbins - 1)
    return sum(( (b == i).mean() * abs(p[b == i].mean() - y[b == i].mean()) )
               for i in range(nbins) if (b == i).sum() > 50)


def build_truth_and_vol(spot_path):
    s1 = pd.read_parquet(spot_path)
    s1["ts_s"] = s1["ts"].values.astype("datetime64[s]").astype("int64")
    s1 = s1.sort_values("ts_s").drop_duplicates("ts_s")
    spot_by_s = pd.Series(s1["close"].values, index=s1["ts_s"].values)

    s1["mts"] = (s1["ts_s"] // 300) * 300
    s1["sec_in_win"] = s1["ts_s"] - s1["mts"]
    n_sec = s1.groupby("mts").size()
    opens = s1.groupby("mts")["open"].first()
    twap = s1[s1["sec_in_win"] >= 240].groupby("mts")["close"].mean()
    truth = pd.DataFrame({"s_open": opens, "twap": twap, "n_sec": n_sec}).dropna()
    truth = truth[truth["n_sec"] >= 250]           # require near-complete windows
    truth["label"] = (truth["twap"] >= truth["s_open"]).astype(int)

    s1["min_ts"] = (s1["ts_s"] // 60) * 60
    m1 = s1.groupby("min_ts").agg(high=("high", "max"), low=("low", "min"))
    hl2 = np.log(m1["high"] / m1["low"]) ** 2
    park = np.sqrt(hl2.rolling(VOL_LOOKBACK_MIN).mean().shift(1) / (4 * np.log(2)))
    sigma_1s_by_min = (park / sqrt(60.0)).dropna()
    return spot_by_s, truth, sigma_1s_by_min


def derive_token_map(snaps, truth):
    """Identify each window's up/down token pair.

    The token whose mid converges toward 1.0 near expiry is the winner; the TRUE
    (TWAP-based) direction then tells us whether that winner is the up or down token.
    """
    s = snaps.copy()
    s["sec"] = s["ts"].astype("int64")
    s["tau"] = s["mts"] + 300 - s["sec"]
    cand = s[(s["tau"] >= -5) & (s["tau"] <= 90)].copy()
    cand["mid"] = (cand["bb"] + cand["ba"]) / 2
    last = cand.sort_values("ts").groupby(["mts", "token"]).tail(1)
    win_idx = last.groupby("mts")["mid"].idxmax()
    winners = last.loc[win_idx].set_index("mts")
    winners = winners[winners["mid"] > 0.85]

    lab = truth["label"].reindex(winners.index)
    winners = winners[lab.notna()]
    lab = lab.dropna().astype(int)

    pairs = s.groupby("mts")["token"].unique()
    rows = []
    for mts, wtok in winners["token"].items():
        toks = list(pairs.get(mts, []))
        if len(toks) != 2 or wtok not in toks:
            continue
        loser = [t for t in toks if t != wtok][0]
        if lab.loc[mts] == 1:      # TWAP says up won -> winner token is the UP token
            rows.append({"mts": mts, "up": wtok, "down": loser})
        else:
            rows.append({"mts": mts, "up": loser, "down": wtok})
    tmap = pd.DataFrame(rows)
    print(f"  token map: {len(tmap)} windows mapped (of {snaps['mts'].nunique()} captured)")
    return tmap


def run(snap_path, spot_path, tag):
    print(f"\n{'#'*74}\n# {tag}\n{'#'*74}")
    spot_by_s, truth, sigma_1s_by_min = build_truth_and_vol(spot_path)
    print(f"  ground truth: {len(truth):,} windows, up rate {truth['label'].mean():.3f}")

    sn = pd.read_parquet(snap_path)
    sn["ts_s"] = sn["ts"].astype("int64")
    tmap = derive_token_map(sn, truth)
    if tmap.empty:
        print("  no mappable windows")
        return
    up_of = dict(zip(tmap["mts"], tmap["up"]))
    down_of = dict(zip(tmap["mts"], tmap["down"]))

    sn["bucket"] = (sn["ts_s"] // BUCKET_S) * BUCKET_S
    sn = sn.sort_values("ts").groupby(["mts", "token", "bucket"], as_index=False).last()
    sn["up_token"] = sn["mts"].map(up_of)
    sn["down_token"] = sn["mts"].map(down_of)
    sn = sn[sn["up_token"].notna()].copy()
    is_up = sn["token"].values == sn["up_token"].values
    is_dn = sn["token"].values == sn["down_token"].values
    sn = sn[is_up | is_dn].copy()
    sn["is_up_token"] = sn["token"].values == sn["up_token"].values

    mid = (sn["bb"].values + sn["ba"].values) / 2.0
    sn["market_p_up"] = np.where(sn["is_up_token"].values, mid, 1.0 - mid)
    sn["tau"] = sn["mts"].values + 300 - sn["ts_s"].values
    sn = sn[(sn["tau"] > 5) & (sn["tau"] <= 300)].copy()
    sn["spot"] = sn["ts_s"].map(spot_by_s)
    sn["s_open"] = sn["mts"].map(truth["s_open"])
    sn["label"] = sn["mts"].map(truth["label"])
    sn["sigma_1s"] = ((sn["ts_s"] // 60) * 60).map(sigma_1s_by_min)
    sn = sn.dropna(subset=["spot", "s_open", "label", "sigma_1s", "market_p_up"])
    sn = sn[(sn["market_p_up"] > 0) & (sn["market_p_up"] < 1)]

    m = np.log(sn["spot"].values / sn["s_open"].values)
    denom = sn["sigma_1s"].values * np.sqrt(var_eff_seconds(sn["tau"].values))
    keep = denom > 0
    sn, m, denom = sn[keep].copy(), m[keep], denom[keep]
    sn["model_p_up"] = ndtr(m / denom)

    y = sn["label"].values.astype(int)
    pm = sn["model_p_up"].values
    pk = np.clip(sn["market_p_up"].values, 1e-6, 1 - 1e-6)
    print(f"  comparison points: {len(sn):,} across {sn['mts'].nunique():,} windows")
    print(f"  period: {pd.to_datetime(sn['ts_s'].min(), unit='s')} -> "
          f"{pd.to_datetime(sn['ts_s'].max(), unit='s')}")

    print(f"\n  {'predictor':>10} {'AUC':>8} {'Brier':>8} {'ECE':>8}")
    print(f"  {'MY MODEL':>10} {roc_auc_score(y,pm):>8.4f} {brier_score_loss(y,pm):>8.4f} {ece(pm,y):>8.4f}")
    print(f"  {'MARKET':>10} {roc_auc_score(y,pk):>8.4f} {brier_score_loss(y,pk):>8.4f} {ece(pk,y):>8.4f}")

    print(f"\n  {'tau band':>11} {'n':>7} {'mdl AUC':>8} {'mkt AUC':>8} {'mdl Brier':>10} {'mkt Brier':>10} {'winner':>7}")
    for lo, hi in [(240,300),(180,240),(120,180),(60,120),(30,60),(5,30)]:
        s = (sn["tau"].values > lo) & (sn["tau"].values <= hi)
        if s.sum() < 100 or len(np.unique(y[s])) < 2:
            continue
        b_m, b_k = brier_score_loss(y[s], pm[s]), brier_score_loss(y[s], pk[s])
        print(f"   ({lo:>3},{hi:>3}] {s.sum():>7,} {roc_auc_score(y[s],pm[s]):>8.4f} "
              f"{roc_auc_score(y[s],pk[s]):>8.4f} {b_m:>10.4f} {b_k:>10.4f} "
              f"{'MODEL' if b_m<b_k else 'market':>7}")

    print(f"\n  === v2 exit rule ===")
    bid_up = np.where(sn["is_up_token"].values, sn["bb"].values, 1 - sn["ba"].values)
    bid_up = np.clip(bid_up, 1e-6, 1 - 1e-6)
    proceeds = bid_up - 0.07 * bid_up * (1 - bid_up)
    for margin in (0.0, 0.02, 0.05, 0.10):
        trig = proceeds > pm + margin
        if trig.sum() < 50:
            print(f"    margin={margin:.2f}: {trig.sum()} triggers (too few)")
            continue
        d = (proceeds[trig] - y[trig]).mean()
        print(f"    margin={margin:.2f}: {trig.sum():>6,} triggers | exit {proceeds[trig].mean():.4f} "
              f"vs hold {y[trig].mean():.4f} | {'GAINS' if d>0 else 'LOSES'} {d*100:+.2f}c")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--both", action="store_true")
    a = ap.parse_args()
    if a.both:
        run("edge_mining/out/snapshots.parquet", "edge_mining/out/btc_1s_aug12_25.parquet",
            "IN-SAMPLE (Aug 12-25) -- same-method re-run for direct comparability")
    run("edge_mining/out/snapshots_oos.parquet", "edge_mining/out/btc_1s_oos.parquet",
        "OUT-OF-SAMPLE (Aug 25 09:05 -> Aug 27) -- never used to build or tune anything")
