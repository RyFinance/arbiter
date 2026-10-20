"""
Independent test of short-horizon BTC reversal/momentum, at Polymarket's exact
5-minute / 60s-TWAP resolution definition, with walk-forward validation,
permutation significance, decay analysis, frozen holdout, and Polymarket-style
cost-adjusted EV.

Data: state/btc_1m_candles_real_2025_2026.parquet (real Binance BTCUSDT 1m,
already used elsewhere in this repo for the LightGBM regime model).
"""
import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score, log_loss

RNG = np.random.default_rng(42)
DATA_PATH = "state/btc_1m_candles_real_2025_2026.parquet"


# --------------------------------------------------------------------------- #
# Step 1: data integrity
# --------------------------------------------------------------------------- #
def step1_integrity(df: pd.DataFrame) -> pd.DataFrame:
    df = df.sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)
    diffs = df["timestamp"].diff().dt.total_seconds()
    gaps = diffs[diffs > 60]
    n_gap_events = len(gaps)
    total_missing_min = (gaps - 60).sum() / 60.0
    flat = ((df["high"] == df["low"]) & (df["low"] == df["open"]) & (df["open"] == df["close"]))
    n_flat = int(flat.sum())
    ret = np.log(df["close"] / df["open"]).replace([np.inf, -np.inf], np.nan)
    spike = ret.abs() > 0.05
    n_spike = int(spike.sum())

    print("=== STEP 1: DATA INTEGRITY ===")
    print(f"raw rows: {len(df)}")
    print(f"date range: {df['timestamp'].min()} -> {df['timestamp'].max()}")
    print(f"gap events (>60s between consecutive candles): {n_gap_events}, total missing ~{total_missing_min:.1f} min")
    if n_gap_events:
        top = gaps.sort_values(ascending=False).head(5)
        for idx, g in top.items():
            print(f"  gap of {g:.0f}s ending at {df.loc[idx,'timestamp']}")
    print(f"flat-lined candles (O=H=L=C): {n_flat} ({n_flat/len(df)*100:.3f}%)")
    print(f"single-bar |log return| > 5% (bad-tick candidates): {n_spike}")
    if n_spike:
        print(df.loc[spike, ["timestamp", "open", "close"]].head(10).to_string(index=False))
    print()
    return df


# --------------------------------------------------------------------------- #
# Step 2: build 5-min (and other) bars matching Polymarket's window definition
# --------------------------------------------------------------------------- #
def build_bars(df: pd.DataFrame, minutes: int, twap_seconds: int = 60) -> pd.DataFrame:
    """Bucket 1m candles into `minutes`-long windows aligned to UTC clock marks
    (Polymarket windows start on :00/:05/:10... in wall-clock time; since 5,15,30,60,240
    all divide evenly into 60/1440, UTC-epoch flooring == wall-clock alignment,
    no ET/UTC correction needed).
    bar_open      = open of the first 1m candle in the bucket
    bar_close     = close of the last 1m candle in the bucket (plain-close reference)
    twap_price    = OHLC4 average of the last `twap_seconds//60` 1m candle(s)
                    (proxy for Polymarket's real sub-minute Chainlink TWAP; flagged
                    as an approximation since we only have 1m granularity)
    n_candles     = number of 1m candles actually present (for completeness filtering)
    """
    ts = df["timestamp"]
    bucket = ts.dt.floor(f"{minutes}min")
    g = df.groupby(bucket)
    bar_open = g["open"].first()
    bar_close = g["close"].last()
    bar_high = g["high"].max()
    bar_low = g["low"].min()
    n_candles = g.size()

    twap_bars = max(1, twap_seconds // 60)
    def _twap(sub: pd.DataFrame) -> float:
        tail = sub.tail(twap_bars)
        return float((tail["open"] + tail["high"] + tail["low"] + tail["close"]).mean() / 4.0)
    twap_price = g.apply(_twap)

    out = pd.DataFrame({
        "open": bar_open, "high": bar_high, "low": bar_low, "close": bar_close,
        "twap_price": twap_price, "n_candles": n_candles,
    })
    out.index.name = "bucket"
    out = out.reset_index()

    expected = minutes
    complete = out["n_candles"] == expected
    n_incomplete = int((~complete).sum())
    print(f"[{minutes}min bars] built {len(out)} buckets, {n_incomplete} incomplete "
          f"(dropped, {n_incomplete/len(out)*100:.2f}%)")
    out = out[complete].reset_index(drop=True)

    out["label"] = (out["twap_price"] >= out["open"]).astype(int)
    out["label_close"] = (out["close"] >= out["open"]).astype(int)
    disagree = (out["label"] != out["label_close"]).mean()
    print(f"[{minutes}min bars] TWAP-vs-plain-close label disagreement: {disagree*100:.2f}% of windows")
    return out


# --------------------------------------------------------------------------- #
# Step 3: signals
# --------------------------------------------------------------------------- #
def add_signals(bars: pd.DataFrame, decay_k: int = 5, halflife_bars: float = 1.0) -> pd.DataFrame:
    bars = bars.copy()
    logc = np.log(bars["close"])
    r1 = logc.diff()  # single-bar log return, bar t vs t-1
    for n in (1, 2, 3):
        # cumulative return over the N complete bars ending at t-1 (no lookahead into bar t)
        bars[f"score_N{n}"] = (logc.shift(1) - logc.shift(1 + n))
    lam = np.log(2) / halflife_bars
    w = np.exp(-lam * np.arange(decay_k))
    w = w / w.sum()
    r1_shift = r1.shift(1)  # r1 of bar t-1 vs t-2 is the most recent "known" 1-bar return at decision time t
    decay_score = pd.Series(0.0, index=bars.index)
    for i in range(decay_k):
        decay_score += w[i] * r1_shift.shift(i)
    bars["score_decay"] = decay_score
    return bars


SIGNAL_COLS = ["score_N1", "score_N2", "score_N3", "score_decay"]


# --------------------------------------------------------------------------- #
# Step 4/7: walk-forward + holdout AUC/hit-rate/logloss for one score column
# --------------------------------------------------------------------------- #
def evaluate_variant(bars: pd.DataFrame, score_col: str, direction: str,
                      wf_mask: np.ndarray, calib_freq: str = "MS"):
    """direction: 'reversal' (predict against score) or 'momentum' (predict with score)."""
    sub = bars.loc[wf_mask, ["bucket", score_col, "label"]].dropna()
    raw_score = sub[score_col].values
    pred_score = -raw_score if direction == "reversal" else raw_score
    y = sub["label"].values

    auc = roc_auc_score(y, pred_score)
    hit = float(np.mean((pred_score > 0).astype(int) == y))

    # expanding-window walk-forward calibration (score -> P(label=1)) for log-loss only
    months = sub["bucket"].dt.to_period("M")
    uniq_months = sorted(months.unique())
    probs = np.full(len(sub), np.nan)
    for i, m in enumerate(uniq_months):
        if i == 0:
            continue  # no prior data to fit on
        train_mask = months < m
        test_mask = months == m
        if train_mask.sum() < 200:
            continue
        Xtr = pred_score[train_mask.values].reshape(-1, 1)
        ytr = y[train_mask.values]
        if len(np.unique(ytr)) < 2:
            continue
        clf = LogisticRegression()
        clf.fit(Xtr, ytr)
        Xte = pred_score[test_mask.values].reshape(-1, 1)
        probs[test_mask.values] = clf.predict_proba(Xte)[:, 1]

    valid = ~np.isnan(probs)
    ll = log_loss(y[valid], np.clip(probs[valid], 1e-6, 1 - 1e-6)) if valid.sum() > 50 else np.nan
    n_ll = int(valid.sum())
    return {
        "auc": auc, "hit_rate": hit, "log_loss": ll, "n": len(sub), "n_logloss": n_ll,
        "pred_score": pred_score, "y": y, "timestamps": sub["bucket"].values,
    }


def block_permutation_zscore(y: np.ndarray, pred_score: np.ndarray, block: int = 100,
                              n_perm: int = 1000, seed: int = 0) -> dict:
    rng = np.random.default_rng(seed)
    real_auc = roc_auc_score(y, pred_score)
    n = len(y)
    n_blocks = int(np.ceil(n / block))
    null_aucs = np.empty(n_perm)
    idx = np.arange(n)
    for p in range(n_perm):
        block_order = rng.permutation(n_blocks)
        shuffled = np.concatenate([idx[b * block:(b + 1) * block] for b in block_order])[:n]
        y_shuf = y[shuffled]
        try:
            null_aucs[p] = roc_auc_score(y_shuf, pred_score)
        except ValueError:
            null_aucs[p] = 0.5
    z = (real_auc - null_aucs.mean()) / null_aucs.std()
    return {"real_auc": real_auc, "null_mean": null_aucs.mean(), "null_std": null_aucs.std(),
            "z": z, "pctile": float((null_aucs >= real_auc).mean())}


def polymarket_fee(p: float) -> float:
    return 0.07 * p * (1 - p)


def cost_adjusted_ev(pred_score: np.ndarray, y: np.ndarray, spreads_cents: list[float]) -> dict:
    """
    Map |signal| into an implied probability tilt via the walk-forward hit rate
    (empirical calibration, not the GBM/TWAP model from the Polymarket prompt --
    that model assumes zero embedded edge; here we HAVE an edge to calibrate).
    Net EV per $1-payout contract = edge_in_prob * $1 - spread_cost - Polymarket fee,
    where edge_in_prob = P(correct) - 0.5 estimated from the walk-forward hit rate,
    entered at an assumed near-50/50 market price p ~= 0.50 (typical entry point for
    a signal with a small tilt), spread cost = half the quoted spread (taker), and
    the fee uses Polymarket's real crypto-fee formula 0.07*p*(1-p).
    """
    hit_rate = float(np.mean((pred_score > 0).astype(int) == y))
    edge_prob = hit_rate - 0.5
    p_entry = 0.5
    fee = polymarket_fee(p_entry)
    out = {}
    for c in spreads_cents:
        spread_cost = (c / 100.0) / 2.0  # taker crosses half the spread
        net_ev = edge_prob * 1.0 - spread_cost - fee
        out[c] = net_ev
    return {"hit_rate": hit_rate, "edge_prob": edge_prob, "fee": fee, "net_ev_by_spread": out}


def main():
    df = pd.read_parquet(DATA_PATH)
    df = step1_integrity(df)

    print("=== STEP 2: 5-MIN BARS (Polymarket window definition) ===")
    bars5 = build_bars(df, 5, twap_seconds=60)
    print(bars5[["bucket", "open", "close", "twap_price", "label", "label_close"]].head(3))
    print()

    print("=== STEP 3: SIGNALS ===")
    bars5 = add_signals(bars5)
    print(bars5[["bucket"] + SIGNAL_COLS].dropna().head(3))
    print()

    # holdout split: final 6 months frozen
    max_ts = bars5["bucket"].max()
    holdout_start = max_ts - pd.DateOffset(months=6)
    wf_mask = (bars5["bucket"] < holdout_start).values
    ho_mask = (bars5["bucket"] >= holdout_start).values
    print(f"walk-forward period: {bars5.loc[wf_mask,'bucket'].min()} -> {bars5.loc[wf_mask,'bucket'].max()} "
          f"({wf_mask.sum()} bars)")
    print(f"FROZEN holdout period: {bars5.loc[ho_mask,'bucket'].min()} -> {bars5.loc[ho_mask,'bucket'].max()} "
          f"({ho_mask.sum()} bars)\n")

    print("=== STEP 4: WALK-FORWARD RESULTS (5-min horizon, non-holdout only) ===")
    results = {}
    for col in SIGNAL_COLS:
        for direction in ("reversal", "momentum"):
            key = f"{col}_{direction}"
            res = evaluate_variant(bars5, col, direction, wf_mask)
            results[key] = res
            print(f"{key:24s} n={res['n']:6d}  AUC={res['auc']:.4f}  hit={res['hit_rate']*100:.2f}%  "
                  f"logloss={res['log_loss']:.4f} (n_ll={res['n_logloss']})")
    print()

    best_key = max(results, key=lambda k: results[k]["auc"])
    print(f"Best walk-forward variant by AUC: {best_key} (AUC={results[best_key]['auc']:.4f})\n")

    print("=== STEP 5: PERMUTATION SIGNIFICANCE (best variant + all variants) ===")
    perm_results = {}
    for key, res in results.items():
        pr = block_permutation_zscore(res["y"], res["pred_score"], block=100, n_perm=1000, seed=1)
        perm_results[key] = pr
        print(f"{key:24s} real_AUC={pr['real_auc']:.4f}  null_mean={pr['null_mean']:.4f} "
              f"null_std={pr['null_std']:.4f}  z={pr['z']:+.2f}  null_pctile={pr['pctile']*100:.1f}%")
    print()

    print("=== STEP 5b: PER-YEAR AUC (best variant) ===")
    best_col, best_dir = best_key.rsplit("_", 1)
    sub = bars5.loc[wf_mask, ["bucket", best_col, "label"]].dropna()
    sub["year"] = sub["bucket"].dt.year
    for yr, g in sub.groupby("year"):
        score = -g[best_col].values if best_dir == "reversal" else g[best_col].values
        if len(np.unique(g["label"])) < 2:
            print(f"  {yr}: n={len(g)} (single class, AUC undefined)")
            continue
        auc_y = roc_auc_score(g["label"].values, score)
        print(f"  {yr}: n={len(g):6d}  AUC={auc_y:.4f}")
    print()

    print("=== STEP 6: DECAY ANALYSIS ACROSS HORIZONS (best signal type: score_N1) ===")
    decay_rows = []
    for horizon in (5, 15, 30, 60, 240):
        b = build_bars(df, horizon, twap_seconds=60 if horizon == 5 else horizon * 60)
        b = add_signals(b)
        wf_h = (b["bucket"] < (b["bucket"].max() - pd.DateOffset(months=6))).values
        for direction in ("reversal", "momentum"):
            r = evaluate_variant(b, "score_N1", direction, wf_h)
            decay_rows.append({"horizon_min": horizon, "direction": direction, "auc": r["auc"],
                                "hit_rate": r["hit_rate"], "n": r["n"]})
            print(f"  horizon={horizon:4d}min  {direction:9s}  n={r['n']:6d}  AUC={r['auc']:.4f}  "
                  f"hit={r['hit_rate']*100:.2f}%")
    print()

    print("=== STEP 7: FROZEN HOLDOUT CONFIRMATION (best variant chosen from walk-forward ONLY) ===")
    ho_res = evaluate_variant(bars5, best_col, best_dir, ho_mask)
    print(f"Holdout {best_key}: n={ho_res['n']}  AUC={ho_res['auc']:.4f}  hit={ho_res['hit_rate']*100:.2f}%  "
          f"logloss={ho_res['log_loss']:.4f}")
    print(f"Walk-forward AUC was {results[best_key]['auc']:.4f} -> holdout AUC is {ho_res['auc']:.4f} "
          f"(delta={ho_res['auc']-results[best_key]['auc']:+.4f})\n")

    print("=== STEP 8: COST-ADJUSTED EV (Polymarket fee=0.07*p*(1-p), spread range 2/4/6/8c) ===")
    spreads = [2, 4, 6, 8]
    for key, res in results.items():
        ev = cost_adjusted_ev(res["pred_score"], res["y"], spreads)
        ev_str = "  ".join(f"{c}c:{ev['net_ev_by_spread'][c]*100:+.3f}c" for c in spreads)
        print(f"{key:24s} hit={ev['hit_rate']*100:.2f}%  edge_prob={ev['edge_prob']*100:+.3f}pp  "
              f"fee={ev['fee']*100:.3f}c  net_EV/$1: {ev_str}")
    ho_ev = cost_adjusted_ev(ho_res["pred_score"], ho_res["y"], spreads)
    ev_str = "  ".join(f"{c}c:{ho_ev['net_ev_by_spread'][c]*100:+.3f}c" for c in spreads)
    print(f"\nHOLDOUT {best_key:16s} hit={ho_ev['hit_rate']*100:.2f}%  edge_prob={ho_ev['edge_prob']*100:+.3f}pp  "
          f"fee={ho_ev['fee']*100:.3f}c  net_EV/$1: {ev_str}")


if __name__ == "__main__":
    main()
