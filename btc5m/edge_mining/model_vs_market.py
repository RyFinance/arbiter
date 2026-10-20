"""
THE DECISIVE TEST for the v2 exit rule: is my fair-value model a better predictor
than the market's own price?

The v2 exit rule is "sell when the market's bid pays more than my model's P(win)".
That is only sound if my model is at least as good an estimator as the market price.
If the market is better calibrated, the rule systematically sells winners to
better-informed counterparties -- which is exactly what the first live trade looked like.

Data (all real, nothing simulated):
  * edge_mining/out/snapshots.parquet  -- 8.66M captured Polymarket book ticks
  * edge_mining/out/btc_1s_aug12_25.parquet -- 1-second BTC spot over the same period
  * ground truth = true 60s TWAP (mean of final 60 seconds) vs window-open price,
    computed from the 1-second data -- the real resolution rule, not a 1m proxy.

For each (window, time) we compare, against the same realized outcome:
  market_p_up = mid price of the UP token
  model_p_up  = Phi(m / (sigma_1s * sqrt(var_eff)))   using Parkinson vol (best-calibrated)
"""
import numpy as np
import pandas as pd
from math import sqrt, log
from scipy.special import ndtr
from sklearn.metrics import roc_auc_score, brier_score_loss

SNAP = "edge_mining/out/snapshots.parquet"
SPOT1S = "edge_mining/out/btc_1s_aug12_25.parquet"
TMAP = "edge_mining/out/token_map.csv"
TWAP_L = 60.0
BUCKET_S = 10          # subsample book ticks to one per 10s per token
VOL_LOOKBACK_MIN = 60  # park60 won the calibration test


def var_eff_seconds(tau, L=TWAP_L):
    tau = np.asarray(tau, dtype=float)
    return np.where(tau >= L, (tau - L) + L / 3.0, (tau / L) ** 2 * (tau / 3.0))


def ece(p, y, nbins=10):
    b = np.clip((p * nbins).astype(int), 0, nbins - 1)
    tot = 0.0
    for i in range(nbins):
        s = b == i
        if s.sum() > 50:
            tot += s.mean() * abs(p[s].mean() - y[s].mean())
    return tot


def main():
    print("loading 1s spot...")
    s1 = pd.read_parquet(SPOT1S)
    s1["ts_s"] = (s1["ts"].values.astype("datetime64[s]").astype("int64"))
    s1 = s1.sort_values("ts_s").drop_duplicates("ts_s")
    spot_by_s = pd.Series(s1["close"].values, index=s1["ts_s"].values)
    print(f"  {len(s1):,} seconds, {s1['ts_s'].min()} -> {s1['ts_s'].max()}")

    # ---- TRUE ground truth: real 60s TWAP vs window open, from 1-second data ----
    s1["mts"] = (s1["ts_s"] // 300) * 300
    s1["sec_in_win"] = s1["ts_s"] - s1["mts"]
    opens = s1.groupby("mts")["open"].first()
    twap = s1[s1["sec_in_win"] >= 240].groupby("mts")["close"].mean()
    truth = pd.DataFrame({"s_open": opens, "twap": twap}).dropna()
    truth["label"] = (truth["twap"] >= truth["s_open"]).astype(int)
    print(f"  ground truth windows: {len(truth):,}  (up rate {truth['label'].mean():.3f})")

    # ---- Parkinson vol from 1-minute bars, trailing, no lookahead ----
    s1["min_ts"] = (s1["ts_s"] // 60) * 60
    m1 = s1.groupby("min_ts").agg(high=("high", "max"), low=("low", "min"))
    hl2 = np.log(m1["high"] / m1["low"]) ** 2
    park = np.sqrt(hl2.rolling(VOL_LOOKBACK_MIN).mean().shift(1) / (4 * np.log(2)))
    sigma_1s_by_min = (park / sqrt(60.0)).dropna()
    print(f"  vol series: {len(sigma_1s_by_min):,} minutes  "
          f"(median {sigma_1s_by_min.median()*sqrt(60)*1e4:.2f} bps/min)")

    print("loading token map + book snapshots...")
    tm = pd.read_csv(TMAP, dtype={"up": str, "down": str}).dropna(subset=["up", "down"])
    up_of = dict(zip(tm["mts"], tm["up"]))
    down_of = dict(zip(tm["mts"], tm["down"]))

    sn = pd.read_parquet(SNAP)
    sn["ts_s"] = sn["ts"].astype("int64")
    sn["bucket"] = (sn["ts_s"] // BUCKET_S) * BUCKET_S
    sn = sn.sort_values("ts").groupby(["mts", "token", "bucket"], as_index=False).last()
    print(f"  {len(sn):,} book ticks after {BUCKET_S}s subsampling")

    sn["up_token"] = sn["mts"].map(up_of)
    sn["down_token"] = sn["mts"].map(down_of)
    sn = sn[sn["up_token"].notna()].copy()
    is_up = sn["token"].values == sn["up_token"].values
    is_dn = sn["token"].values == sn["down_token"].values
    sn = sn[is_up | is_dn].copy()
    sn["is_up_token"] = sn["token"].values == sn["up_token"].values

    # market-implied P(up), from the mid of whichever token this tick is
    mid = (sn["bb"].values + sn["ba"].values) / 2.0
    sn["market_p_up"] = np.where(sn["is_up_token"].values, mid, 1.0 - mid)

    # model inputs
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
    sn = sn[denom > 0].copy()
    m = m[denom > 0]
    denom = denom[denom > 0]
    sn["model_p_up"] = ndtr(m / denom)

    y = sn["label"].values.astype(int)
    pm = sn["model_p_up"].values
    pk = np.clip(sn["market_p_up"].values, 1e-6, 1 - 1e-6)
    print(f"\ncomparison points: {len(sn):,} across {sn['mts'].nunique():,} real windows")
    print(f"period: {pd.to_datetime(sn['ts_s'].min(), unit='s')} -> {pd.to_datetime(sn['ts_s'].max(), unit='s')}")

    print(f"\n{'='*74}\n{'predictor':>12} {'AUC':>8} {'Brier':>8} {'ECE':>8}   (lower Brier/ECE better)")
    print(f"{'MY MODEL':>12} {roc_auc_score(y,pm):>8.4f} {brier_score_loss(y,pm):>8.4f} {ece(pm,y):>8.4f}")
    print(f"{'MARKET':>12} {roc_auc_score(y,pk):>8.4f} {brier_score_loss(y,pk):>8.4f} {ece(pk,y):>8.4f}")
    print("="*74)

    print(f"\n=== by time remaining ===")
    print(f"{'tau band':>12} {'n':>8} {'model AUC':>10} {'mkt AUC':>9} {'model Brier':>12} {'mkt Brier':>10} {'winner':>8}")
    for lo, hi in [(240,300),(180,240),(120,180),(60,120),(30,60),(5,30)]:
        s = (sn["tau"].values > lo) & (sn["tau"].values <= hi)
        if s.sum() < 200 or len(np.unique(y[s])) < 2:
            continue
        a_m, a_k = roc_auc_score(y[s], pm[s]), roc_auc_score(y[s], pk[s])
        b_m, b_k = brier_score_loss(y[s], pm[s]), brier_score_loss(y[s], pk[s])
        win = "MODEL" if b_m < b_k else "market"
        print(f"  ({lo:>3},{hi:>3}] {s.sum():>8,} {a_m:>10.4f} {a_k:>9.4f} {b_m:>12.4f} {b_k:>10.4f} {win:>8}")

    # ---- the exact v2 situation: model says sell, market disagrees. who is right? ----
    print(f"\n=== when model and market disagree, who wins? ===")
    disagree = np.abs(pm - pk) > 0.10
    print(f"disagreement >10pp: {disagree.sum():,} points ({disagree.mean()*100:.1f}%)")
    if disagree.sum() > 100:
        mh = pm[disagree] > pk[disagree]     # model more bullish on UP than market
        yd = y[disagree]
        print(f"  model MORE bullish than market: n={mh.sum():,}  actual up rate={yd[mh].mean():.3f} "
              f"(model {pm[disagree][mh].mean():.3f} vs market {pk[disagree][mh].mean():.3f})")
        print(f"  model LESS bullish than market: n={(~mh).sum():,}  actual up rate={yd[~mh].mean():.3f} "
              f"(model {pm[disagree][~mh].mean():.3f} vs market {pk[disagree][~mh].mean():.3f})")

    # ---- direct simulation of the v2 rule ----
    print(f"\n=== direct test of the v2 exit rule ===")
    print("For every point, if I held the token this tick prices, would selling at the")
    print("market beat holding to resolution? (net of the 0.07*p*(1-p) taker fee)")
    for margin in (0.0, 0.02, 0.05, 0.10):
        # value of holding the UP token = P(up wins). model thinks pm, truth is y.
        bid_up = np.where(sn["is_up_token"].values, sn["bb"].values, 1 - sn["ba"].values)
        bid_up = np.clip(bid_up, 1e-6, 1-1e-6)
        proceeds = bid_up - 0.07 * bid_up * (1 - bid_up)
        trigger = proceeds > pm + margin
        if trigger.sum() < 50:
            print(f"  margin={margin:.2f}: {trigger.sum()} triggers (too few)")
            continue
        realized_if_exit = proceeds[trigger]
        realized_if_hold = y[trigger].astype(float)
        d = (realized_if_exit - realized_if_hold).mean()
        print(f"  margin={margin:.2f}: {trigger.sum():>7,} triggers | "
              f"exit avg {realized_if_exit.mean():.4f} vs hold avg {realized_if_hold.mean():.4f} | "
              f"exit rule {'GAINS' if d>0 else 'LOSES'} {d*100:+.2f}c per trigger")


if __name__ == "__main__":
    main()
