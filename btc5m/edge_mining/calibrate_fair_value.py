"""
Is the GBM/TWAP P(win) model actually calibrated? Test on 19 months of real BTC data.

The v2 live harness uses P(up) = Phi(m / (sigma_1s * sqrt(var_eff))) to decide
hold-vs-exit. The very first live trade suggested the model is badly miscalibrated
(it called a position 89% dead at tau=61s; it won). This script checks that claim
properly, on 845k real 1-minute candles rather than one anecdote.

Method: for every 5-min window and every minute within it (tau = 240/180/120/60s),
compute the model's P(up) from information available at that instant, then compare
to what actually happened. A calibrated model's P(up)=0.2 bucket should win ~20%
of the time.

Also compares volatility estimators, since the suspicion is that close-to-close
1-minute vol understates the true path volatility that a TWAP settlement is exposed to:
  * close-to-close (what v2 currently uses)
  * Parkinson (high-low range, captures intra-minute path)
  * Garman-Klass (OHLC)
each over several lookbacks.
"""
import numpy as np
import pandas as pd
import sys, os
from math import sqrt, log

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
from reversal_momentum_study import build_bars

DATA = "state/btc_1m_candles_real_2025_2026.parquet"
TWAP_L = 60.0


def var_eff_seconds(tau, L=TWAP_L):
    tau = np.asarray(tau, dtype=float)
    return np.where(tau >= L, (tau - L) + L / 3.0, (tau / L) ** 2 * (tau / 3.0))


def norm_cdf(x):
    from scipy.special import ndtr
    return ndtr(x)


def main():
    df = pd.read_parquet(DATA).sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)
    print(f"loaded {len(df):,} 1m candles  {df['timestamp'].min()} -> {df['timestamp'].max()}")

    bars = build_bars(df, 5, twap_seconds=60)
    bars = bars[["bucket", "open", "label"]].rename(columns={"open": "s_open"})

    df["bucket"] = df["timestamp"].dt.floor("5min")
    df = df.merge(bars, on="bucket", how="inner")
    df["pos"] = df.groupby("bucket").cumcount()          # 0..4 within the window
    df["tau"] = 300 - (df["pos"] + 1) * 60                # seconds left after this candle closes
    df = df[df["tau"] > 0].copy()                         # tau = 240/180/120/60

    # ---- volatility estimators (all strictly trailing, no lookahead) ----
    logc = np.log(df["close"].values)
    r1 = np.concatenate([[np.nan], np.diff(logc)])
    df["r1"] = r1
    hl = np.log(df["high"].values / df["low"].values)
    co = np.log(df["close"].values / df["open"].values)

    est = {}
    for lb in (15, 30, 60):
        # close-to-close (what v2 uses today)
        est[f"cc{lb}"] = pd.Series(r1).rolling(lb).std().shift(1).values
        # Parkinson: sigma^2 = mean(ln(H/L)^2) / (4 ln 2)
        park = np.sqrt(pd.Series(hl ** 2).rolling(lb).mean().shift(1).values / (4 * np.log(2)))
        est[f"park{lb}"] = park
        # Garman-Klass: 0.5*ln(H/L)^2 - (2ln2-1)*ln(C/O)^2
        gk_term = 0.5 * hl ** 2 - (2 * np.log(2) - 1) * co ** 2
        est[f"gk{lb}"] = np.sqrt(np.clip(pd.Series(gk_term).rolling(lb).mean().shift(1).values, 0, None))

    df["m"] = np.log(df["close"].values / df["s_open"].values)
    ve = var_eff_seconds(df["tau"].values)
    y = df["label"].values.astype(int)

    print(f"\nevaluation points: {len(df):,} (across {df['bucket'].nunique():,} windows)\n")

    # ---- how big are moves actually, vs what each estimator predicts? ----
    print("=== realized 1-min vol by estimator (median, bps) ===")
    for k, v in est.items():
        print(f"  {k:>8}: {np.nanmedian(v)*1e4:6.2f} bps")
    print(f"  {'actual':>8}: {np.nanmedian(np.abs(r1))*1e4*1.2533:6.2f} bps  "
          f"(median|r| * sqrt(pi/2), a robust sigma proxy)")

    # ---- calibration per estimator ----
    from sklearn.metrics import roc_auc_score, brier_score_loss
    print("\n=== calibration of P(up) ===")
    print(f"{'estimator':>10} {'AUC':>7} {'Brier':>7} {'mean_p':>7} {'actual':>7} {'ECE':>7}")
    results = {}
    for k, sig_1m in est.items():
        sig_1s = sig_1m / sqrt(60.0)
        denom = sig_1s * np.sqrt(ve)
        ok = np.isfinite(denom) & (denom > 0)
        p = np.full(len(df), np.nan)
        p[ok] = norm_cdf(df["m"].values[ok] / denom[ok])
        m_ok = np.isfinite(p)
        pv, yv = p[m_ok], y[m_ok]
        auc = roc_auc_score(yv, pv)
        brier = brier_score_loss(yv, pv)
        # expected calibration error over 10 bins
        bins = np.clip((pv * 10).astype(int), 0, 9)
        ece = 0.0
        for b in range(10):
            sel = bins == b
            if sel.sum() > 50:
                ece += sel.mean() * abs(pv[sel].mean() - yv[sel].mean())
        results[k] = (auc, brier, ece, pv, yv)
        print(f"{k:>10} {auc:>7.4f} {brier:>7.4f} {pv.mean():>7.4f} {yv.mean():>7.4f} {ece:>7.4f}")

    best = min(results, key=lambda k: results[k][2])
    print(f"\nbest-calibrated (lowest ECE): {best}")

    # ---- calibration curve for the estimator v2 currently uses, vs the best ----
    for name in ("cc30", best):
        _, _, _, pv, yv = results[name]
        print(f"\n=== calibration curve: {name} ===")
        print(f"{'model P(up)':>14} {'n':>8} {'predicted':>10} {'ACTUAL':>8} {'gap':>8}")
        edges = [0, .05, .1, .2, .3, .4, .5, .6, .7, .8, .9, .95, 1.001]
        for lo, hi in zip(edges[:-1], edges[1:]):
            sel = (pv >= lo) & (pv < hi)
            if sel.sum() < 30:
                continue
            print(f"  [{lo:.2f},{hi:.2f}) {sel.sum():>8,} {pv[sel].mean():>10.3f} "
                  f"{yv[sel].mean():>8.3f} {yv[sel].mean()-pv[sel].mean():>+8.3f}")

    # ---- the decisive question: how often does a "dead" position come back? ----
    print("\n=== how often does the model call a position dead, and it recovers? ===")
    _, _, _, pv, yv = results["cc30"]
    tau_v = df["tau"].values[np.isfinite(p)] if False else df["tau"].values
    sig_1s = est["cc30"] / sqrt(60.0)
    denom = sig_1s * np.sqrt(ve)
    ok = np.isfinite(denom) & (denom > 0)
    p_all = np.full(len(df), np.nan)
    p_all[ok] = norm_cdf(df["m"].values[ok] / denom[ok])
    fin = np.isfinite(p_all)
    print(f"{'tau':>6} {'model says <10% (n)':>20} {'ACTUALLY won':>14}   {'model says >90% (n)':>20} {'ACTUALLY lost':>14}")
    for tau in (240, 180, 120, 60):
        sel = fin & (df["tau"].values == tau)
        dead = sel & (p_all < 0.10)
        sure = sel & (p_all > 0.90)
        d_rate = y[dead].mean() if dead.sum() else float("nan")
        s_rate = 1 - y[sure].mean() if sure.sum() else float("nan")
        print(f"{tau:>6} {dead.sum():>20,} {d_rate:>13.1%}   {sure.sum():>20,} {s_rate:>13.1%}")


if __name__ == "__main__":
    main()
