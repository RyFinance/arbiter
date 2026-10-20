"""
Model vs. market, done correctly, in-sample and out-of-sample.

Supersedes model_vs_market.py / model_vs_market_oos.py. Both had problems:
  * model_vs_market.py ran only on the in-sample period.
  * model_vs_market_oos.py derived its OOS token map circularly -- it identified the
    winning token by which price converged to ~1.0, then labeled it by the TWAP
    outcome, which by construction makes the market's own late price a near-perfect
    predictor of the label (market AUC hit 0.9949 at tau<30s, an artifact).

This version uses ONLY authoritative Polymarket token->outcome maps (Gamma API) for
both periods, and sanity-checks each map before using it: if the map is right, the
"up" token's price near expiry should be high exactly when the TWAP label is up.

Ground truth is the true 60-second TWAP computed from 1-second BTC data -- the real
resolution rule (confirmed live: cryptoMarketConfig.twapLookbackSeconds = 60).
"""
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
    return sum(((b == i).mean() * abs(p[b == i].mean() - y[b == i].mean()))
               for i in range(nbins) if (b == i).sum() > 50)


def build_truth_and_vol(spot_path):
    s1 = pd.read_parquet(spot_path)
    s1["ts_s"] = s1["ts"].values.astype("datetime64[s]").astype("int64")
    s1 = s1.sort_values("ts_s").drop_duplicates("ts_s")
    spot_by_s = pd.Series(s1["close"].values, index=s1["ts_s"].values)

    s1["mts"] = (s1["ts_s"] // 300) * 300
    s1["sec_in_win"] = s1["ts_s"] - s1["mts"]
    truth = pd.DataFrame({
        "s_open": s1.groupby("mts")["open"].first(),
        "twap": s1[s1["sec_in_win"] >= 240].groupby("mts")["close"].mean(),
        "n_sec": s1.groupby("mts").size(),
    }).dropna()
    truth = truth[truth["n_sec"] >= 250]
    truth["label"] = (truth["twap"] >= truth["s_open"]).astype(int)

    s1["min_ts"] = (s1["ts_s"] // 60) * 60
    m1 = s1.groupby("min_ts").agg(high=("high", "max"), low=("low", "min"))
    hl2 = np.log(m1["high"] / m1["low"]) ** 2
    park = np.sqrt(hl2.rolling(VOL_LOOKBACK_MIN).mean().shift(1) / (4 * np.log(2)))
    return spot_by_s, truth, (park / sqrt(60.0)).dropna()


def sanity_check_map(sn, tmap, truth):
    """If the token map is right, the UP token's late mid should be high iff label==1."""
    s = sn.copy()
    s["tau"] = s["mts"] + 300 - s["ts_s"]
    late = s[(s["tau"] >= -5) & (s["tau"] <= 60)].sort_values("ts").groupby(["mts", "token"]).tail(1)
    up_of = dict(zip(tmap["mts"], tmap["up"]))
    late = late[late["mts"].map(up_of).notna()]
    late = late[late["token"] == late["mts"].map(up_of)]
    late["mid"] = (late["bb"] + late["ba"]) / 2
    late["label"] = late["mts"].map(truth["label"])
    late = late.dropna(subset=["label"])
    if len(late) < 20:
        print(f"    [map check] too few late ticks ({len(late)})")
        return
    agree = ((late["mid"] > 0.5).astype(int) == late["label"]).mean()
    print(f"    [map check] UP-token late mid agrees with TWAP label on "
          f"{agree*100:.1f}% of {len(late)} windows "
          f"({'OK' if agree > 0.85 else 'SUSPECT -- map may be wrong'})")


def run(snap_path, spot_path, tmap_path, tag):
    print(f"\n{'#'*76}\n# {tag}\n{'#'*76}")
    spot_by_s, truth, sigma_by_min = build_truth_and_vol(spot_path)
    tmap = pd.read_csv(tmap_path, dtype={"up": str, "down": str}).dropna(subset=["up", "down"])
    print(f"  ground truth {len(truth):,} windows (up rate {truth['label'].mean():.3f}); "
          f"authoritative token map {len(tmap):,} windows")

    sn = pd.read_parquet(snap_path)
    sn["ts_s"] = sn["ts"].astype("int64")
    sanity_check_map(sn, tmap, truth)

    up_of = dict(zip(tmap["mts"], tmap["up"]))
    down_of = dict(zip(tmap["mts"], tmap["down"]))
    sn["bucket"] = (sn["ts_s"] // BUCKET_S) * BUCKET_S
    sn = sn.sort_values("ts").groupby(["mts", "token", "bucket"], as_index=False).last()
    sn["up_token"] = sn["mts"].map(up_of)
    sn["down_token"] = sn["mts"].map(down_of)
    sn = sn[sn["up_token"].notna()].copy()
    keep = (sn["token"].values == sn["up_token"].values) | (sn["token"].values == sn["down_token"].values)
    sn = sn[keep].copy()
    sn["is_up_token"] = sn["token"].values == sn["up_token"].values

    mid = (sn["bb"].values + sn["ba"].values) / 2.0
    sn["market_p_up"] = np.where(sn["is_up_token"].values, mid, 1.0 - mid)
    sn["tau"] = sn["mts"].values + 300 - sn["ts_s"].values
    sn = sn[(sn["tau"] > 5) & (sn["tau"] <= 300)].copy()
    sn["spot"] = sn["ts_s"].map(spot_by_s)
    sn["s_open"] = sn["mts"].map(truth["s_open"])
    sn["label"] = sn["mts"].map(truth["label"])
    sn["sigma_1s"] = ((sn["ts_s"] // 60) * 60).map(sigma_by_min)
    sn = sn.dropna(subset=["spot", "s_open", "label", "sigma_1s", "market_p_up"])
    sn = sn[(sn["market_p_up"] > 0) & (sn["market_p_up"] < 1)]

    m = np.log(sn["spot"].values / sn["s_open"].values)
    denom = sn["sigma_1s"].values * np.sqrt(var_eff_seconds(sn["tau"].values))
    k = denom > 0
    sn, m, denom = sn[k].copy(), m[k], denom[k]
    sn["model_p_up"] = ndtr(m / denom)

    y = sn["label"].values.astype(int)
    pm = sn["model_p_up"].values
    pk = np.clip(sn["market_p_up"].values, 1e-6, 1 - 1e-6)
    print(f"  {len(sn):,} points across {sn['mts'].nunique():,} windows | "
          f"{pd.to_datetime(sn['ts_s'].min(), unit='s'):%m-%d %H:%M} -> "
          f"{pd.to_datetime(sn['ts_s'].max(), unit='s'):%m-%d %H:%M}")

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

    print(f"\n  === v2 exit rule (sell when market bid, net of taker fee, beats model P(win)) ===")
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
    run("edge_mining/out/snapshots.parquet", "edge_mining/out/btc_1s_aug12_25.parquet",
        "edge_mining/out/token_map.csv",
        "IN-SAMPLE  Aug 12-25  (authoritative Gamma token map)")
    run("edge_mining/out/snapshots_oos.parquet", "edge_mining/out/btc_1s_oos.parquet",
        "edge_mining/out/token_map_oos.csv",
        "OUT-OF-SAMPLE  Aug 25 09:05 -> Aug 27  (authoritative Gamma token map, never tuned on)")
