"""
Real-data adverse-selection test for maker-order execution of the reversal signal.

Uses ACTUAL captured Polymarket order-book ticks (edge_mining/out/snapshots.parquet,
Aug 12-25 2026, real bb/ba/bq/qa per token per 5-min window) -- not a spot-price proxy.

Idea: at window open, the reversal signal (already validated walk-forward+holdout on
19 months of spot data in scripts/reversal_momentum_study.py) picks a side. Simulate
resting a maker BID at the then-current best_bid on the predicted-winning token's book.
Scan subsequent real snapshots in that same window/token: if best_ask ever crosses down
to <= my resting bid, treat the order as FILLED at that entry price (idealized: assumes
guaranteed fill whenever the market trades through my level, ignoring queue position --
this is a generous upper bound on fill rate, flagged explicitly).

Then compare hit-rate CONDITIONAL ON FILL vs conditional on NOT filled vs unconditional.
If adverse selection is real and material, filled trades should win less often than
unfilled ones (informed flow disproportionately crosses your quote right before you lose).
"""
import numpy as np
import pandas as pd
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
from reversal_momentum_study import build_bars, add_signals, polymarket_fee

SNAP_PATH = "edge_mining/out/snapshots.parquet"
TOKEN_MAP_PATH = "edge_mining/out/token_map.csv"
SPOT_PATH = "edge_mining/out/btc_1m_aug12_25.parquet"


def main():
    print("Loading real captured order-book snapshots...")
    snaps = pd.read_parquet(SNAP_PATH)
    print(f"  {len(snaps):,} ticks, {snaps['mts'].nunique():,} windows, "
          f"{snaps['token'].nunique():,} distinct tokens")

    tmap = pd.read_csv(TOKEN_MAP_PATH, dtype={"up": str, "down": str})
    tmap = tmap.set_index("mts")[["up", "down"]]

    print("\nLoading real BTC spot data for the same period (signal construction + ground truth)...")
    spot = pd.read_parquet(SPOT_PATH)
    spot = spot.rename(columns={c: c.lower() for c in spot.columns})
    tcol = "timestamp" if "timestamp" in spot.columns else "ts"
    spot[tcol] = pd.to_datetime(spot[tcol], utc=True)
    spot = spot.rename(columns={tcol: "timestamp"}).sort_values("timestamp").drop_duplicates("timestamp")
    print(f"  spot range: {spot['timestamp'].min()} -> {spot['timestamp'].max()}  ({len(spot):,} rows)")

    bars5 = build_bars(spot, 5, twap_seconds=60)
    bars5 = add_signals(bars5)
    bars5["mts"] = bars5["bucket"].values.astype("datetime64[s]").astype("int64")
    bars5 = bars5.set_index("mts")

    variants = {
        "N1_reversal": ("score_N1", "reversal"),
        "N2_reversal": ("score_N2", "reversal"),
        "N3_reversal": ("score_N3", "reversal"),
        "decay_reversal": ("score_decay", "reversal"),
    }

    snaps = snaps.sort_values(["mts", "token", "ts"])
    snap_groups = {k: v for k, v in snaps.groupby(["mts", "token"])}

    for vname, (col, direction) in variants.items():
        rows = []
        common_mts = sorted(set(bars5.index) & set(tmap.index))
        for mts in common_mts:
            score = bars5.loc[mts, col]
            label = bars5.loc[mts, "label"]  # 1 = up won (TWAP-based, matches real resolution rule)
            if pd.isna(score):
                continue
            pred_up = (score < 0) if direction == "reversal" else (score > 0)  # reversal bets against recent return
            pred_token = tmap.loc[mts, "up"] if pred_up else tmap.loc[mts, "down"]
            actual_up = bool(label)
            correct = (pred_up == actual_up)

            key = (mts, pred_token)
            if key not in snap_groups:
                continue
            g = snap_groups[key].sort_values("ts")
            if len(g) < 2:
                continue
            entry = g.iloc[0]
            entry_price = entry["bb"]
            if pd.isna(entry_price) or entry_price <= 0 or entry_price >= 1:
                continue
            later = g.iloc[1:]
            crossed = later[later["ba"] <= entry_price]
            filled = len(crossed) > 0
            fill_ts = float(crossed.iloc[0]["ts"]) if filled else None

            payoff = None
            if filled:
                payoff = (1.0 - entry_price) if correct else (-entry_price)

            rows.append({
                "mts": mts, "pred_up": pred_up, "actual_up": actual_up, "correct": correct,
                "entry_price": entry_price, "filled": filled, "payoff": payoff,
            })

        df = pd.DataFrame(rows)
        if df.empty:
            print(f"\n{vname}: no matched windows")
            continue

        n = len(df)
        acc_all = df["correct"].mean()
        fill_rate = df["filled"].mean()
        filled_df = df[df["filled"]]
        unfilled_df = df[~df["filled"]]
        acc_filled = filled_df["correct"].mean() if len(filled_df) else float("nan")
        acc_unfilled = unfilled_df["correct"].mean() if len(unfilled_df) else float("nan")
        avg_entry_filled = filled_df["entry_price"].mean() if len(filled_df) else float("nan")
        avg_payoff = filled_df["payoff"].mean() if len(filled_df) else float("nan")
        total_pnl = filled_df["payoff"].sum() if len(filled_df) else 0.0

        print(f"\n=== {vname} ===")
        print(f"  matched windows: {n}  (real order-book data available for both sides)")
        print(f"  unconditional accuracy (this real-book-matched subsample): {acc_all*100:.2f}%")
        print(f"  fill rate (idealized: filled if book ever crosses my resting bid): {fill_rate*100:.2f}%  (n_filled={len(filled_df)})")
        print(f"  accuracy | FILLED:     {acc_filled*100:.2f}%   <- this is the adverse-selection-exposed number")
        print(f"  accuracy | NOT filled: {acc_unfilled*100:.2f}%")
        print(f"  avg entry price when filled: {avg_entry_filled:.4f}")
        print(f"  avg raw payoff per filled $1-notional trade (BEFORE fee, maker=0 fee so this IS net EV): "
              f"{avg_payoff*100:+.3f}c")
        print(f"  total PnL across all filled trades (${1:.0f} notional each): {total_pnl:+.3f}")

    print("\n\nNOTE: idealized fill rule (any book-crossing = fill) ignores queue position and is a "
          "generous upper bound on real fill probability. Real resting orders behind other maker "
          "liquidity at the same price would fill less often and possibly with worse adverse selection "
          "(the crossing flow may exhaust ahead-of-queue size before reaching you) -- or the reverse if "
          "size ahead of you is thin. This test isolates the SIGN and rough MAGNITUDE of adverse selection, "
          "not a production-grade fill simulator.")


if __name__ == "__main__":
    main()
