"""Independent verification pass (2026-08-25), not part of the original
edge_mining pipeline.

Checks two things the original FINDINGS.md did not:
1. Real Polymarket 5-min BTC markets settle against a Chainlink 30-second
   TWAP as of 2026-08-07 (confirmed via web search), not a single-point
   Binance close>=open comparison as the original pipeline assumed. This
   recomputes ground truth with a 30s-TWAP-vs-open-print approximation
   using the already-fetched 1-second Binance klines and re-scores the
   backtest to see if the edge survives a more realistic resolution rule.
2. Reproduces the "net of fee" headline using real ask sizes (position
   sizing) rather than the unreproducible ad-hoc figure in FINDINGS.md.
"""
from pathlib import Path
import numpy as np
import pandas as pd

OUTD = Path(__file__).parent / "out"


def build_twap_ground_truth():
    btc1s = pd.read_parquet(OUTD / "btc_1s_aug12_25.parquet").set_index("ts")
    tmap = pd.read_csv(OUTD / "token_map.csv", dtype={"up": str, "down": str})
    mts_list = sorted(tmap.mts.unique())

    rows = []
    for mts in mts_list:
        open_t = pd.Timestamp(mts, unit="s", tz="UTC")
        close_t = open_t + pd.Timedelta(seconds=300)
        twap_start = close_t - pd.Timedelta(seconds=30)
        try:
            open_px = btc1s.loc[open_t, "close"]
        except KeyError:
            continue
        window_1s = btc1s.loc[twap_start:close_t - pd.Timedelta(seconds=1), "close"]
        if len(window_1s) < 15:  # need at least half the TWAP window present
            continue
        twap_close = float(window_1s.mean())
        # Original (naive) ground truth: 1-min candle close>=open
        rows.append({
            "mts": mts,
            "open_px": float(open_px),
            "twap_close_30s": twap_close,
            "twap_dir": "up" if twap_close >= open_px else "dn",
        })
    return pd.DataFrame(rows)


def compare_ground_truths(twap_gt: pd.DataFrame, piv: pd.DataFrame):
    naive = piv.drop_duplicates("mts").set_index("mts")["btc_winner"]
    twap_gt = twap_gt.set_index("mts")
    merged = twap_gt.join(naive, how="inner")
    disagree = merged[merged["twap_dir"] != merged["btc_winner"]]
    print(f"[GROUND TRUTH COMPARISON] {len(merged)} windows with both labels")
    print(f"  naive (1m close>=open) vs 30s-TWAP-vs-open: "
          f"{len(disagree)} disagreements ({len(disagree) / len(merged) * 100:.2f}%)")
    return merged, disagree


def backtest_with_ground_truth(piv: pd.DataFrame, winner_map: pd.Series, theta: float,
                                tau_lo=20, tau_hi=270, fee_rate=0.07, use_size_cap=True,
                                share_cap=75):
    m = piv[(piv.tau > tau_lo) & (piv.tau < tau_hi)].copy()
    recs = []
    for side in ("up", "dn"):
        fair = m.fair_up if side == "up" else 1.0 - m.fair_up
        edge = fair - m[f"ba_{side}"]
        hit = edge > theta
        cols = {"mts": m.mts[hit], "sec": m.sec[hit] if "sec" in m.columns else m.ts[hit],
                "side": side, "px": m[f"ba_{side}"][hit], "fair": fair[hit]}
        if f"qa_{side}" in m.columns:
            cols["size"] = m[f"qa_{side}"][hit]
        recs.append(pd.DataFrame(cols))
    tdf = pd.concat(recs).sort_values("sec")
    tdf = tdf.groupby(["mts", "side"]).first().reset_index()
    tdf["win"] = tdf.mts.map(winner_map)
    tdf = tdf.dropna(subset=["win"])
    tdf["win"] = (tdf.side == tdf.win).astype(float)
    p = tdf.px
    fee = fee_rate * p * (1 - p)
    tdf["pnl_gross"] = tdf.win - p
    tdf["pnl_net"] = tdf.win - p - fee
    if use_size_cap and "size" in tdf.columns:
        tdf["shares"] = tdf["size"].clip(upper=share_cap)
    else:
        tdf["shares"] = 1.0
    tdf["dollar_pnl_net"] = tdf["pnl_net"] * tdf["shares"]
    n = len(tdf)
    days = (pd.to_datetime(tdf.mts, unit="s", utc=True).dt.date.nunique()) or 1
    return {
        "theta": theta, "n_trades": n, "hit_rate_pct": round(tdf.win.mean() * 100, 2),
        "avg_px": round(p.mean(), 4),
        "gross_avg_pnl_per_share": round(tdf.pnl_gross.mean(), 5),
        "net_avg_pnl_per_share": round(tdf.pnl_net.mean(), 5),
        "net_sharpe_per_trade": round(tdf.pnl_net.mean() / max(tdf.pnl_net.std(), 1e-9), 4),
        "avg_shares_per_trade": round(tdf["shares"].mean(), 1) if "size" in tdf.columns else None,
        "total_net_dollar_pnl": round(tdf["dollar_pnl_net"].sum(), 2),
        "net_dollar_pnl_per_day": round(tdf["dollar_pnl_net"].sum() / days, 2),
        "days": int(days),
    }


def main():
    piv = pd.read_parquet(OUTD / "scored.parquet")
    print(f"Loaded scored.parquet: {len(piv)} rows, {piv.mts.nunique()} windows")
    has_size = "qa_up" in piv.columns or "qa_dn" in piv.columns
    print(f"Has real ask-size columns for position sizing: {has_size}")

    twap_gt = build_twap_ground_truth()
    print(f"Built TWAP ground truth for {len(twap_gt)} windows")
    merged, disagree = compare_ground_truths(twap_gt, piv)

    naive_winner = piv.drop_duplicates("mts").set_index("mts")["btc_winner"]
    twap_winner = twap_gt.set_index("mts")["twap_dir"]

    print()
    print("=" * 100)
    print("BACKTEST A: original (naive 1m close>=open) ground truth, net of REAL Polymarket fee formula")
    print("=" * 100)
    for theta in (0.02, 0.03, 0.05, 0.08, 0.12):
        r = backtest_with_ground_truth(piv, naive_winner, theta, use_size_cap=has_size)
        print(r)

    print()
    print("=" * 100)
    print("BACKTEST B: corrected 30s-TWAP-vs-open ground truth (matches real Chainlink settlement mechanism), net of fee")
    print("=" * 100)
    for theta in (0.02, 0.03, 0.05, 0.08, 0.12):
        r = backtest_with_ground_truth(piv, twap_winner, theta, use_size_cap=has_size)
        print(r)


if __name__ == "__main__":
    main()
