#!/usr/bin/env python3
"""Exact-label, real-CLOB quote backtest for the fresh 5m capture.

This is deliberately a quote backtest, not a fill claim: it uses the captured
best ask and assumes an immediate fill (optimistic).  Settlement labels come
from terminal Polymarket metadata, while the token map is Gamma's positional
Up/Down mapping.  The period is after 2026-08-25 09:00 UTC and was not used by
the older edge_mining filter-selection reports.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "edge_mining" / "out"
SNAPSHOT_PATH = OUT / "snapshots_oos.parquet"
TOKEN_MAP_PATH = OUT / "token_map_oos.csv"
BTC_PATH = OUT / "btc_1m_oos.parquet"
LABEL_PATH = ROOT / "state" / "polymarket_btc_updown_5m_aug12_27.parquet"
RESULT_PATH = ROOT / "research" / "clob_exact_oos_results_2026-08-27.json"
FEE_RATE = 0.07


def fee(p: pd.Series | np.ndarray) -> pd.Series | np.ndarray:
    return FEE_RATE * p * (1.0 - p)


def pair_books() -> pd.DataFrame:
    snaps = pd.read_parquet(SNAPSHOT_PATH)
    snaps["sec"] = snaps["ts"].astype("int64")
    snaps = snaps.sort_values("ts").groupby(["mts", "token", "sec"]).tail(1)
    tmap = pd.read_csv(TOKEN_MAP_PATH, dtype={"up": str, "down": str})
    parts = []
    for col, side in (("up", "up"), ("down", "dn")):
        tm = tmap.dropna(subset=[col])[["mts", col]]
        ren = {"bb": f"bb_{side}", "ba": f"ba_{side}", "bq": f"bq_{side}", "qa": f"qa_{side}"}
        part = snaps.merge(tm, left_on=["mts", "token"], right_on=["mts", col], how="inner").rename(columns=ren)
        parts.append(part[["mts", "sec"] + list(ren.values())])
    return parts[0].merge(parts[1], on=["mts", "sec"], how="outer").sort_values(["mts", "sec"])


def add_fair_value(piv: pd.DataFrame) -> pd.DataFrame:
    btc = pd.read_parquet(BTC_PATH).set_index("ts")
    ret = np.log(btc["close"] / btc["open"])
    lam = 0.5 ** (1.0 / 15.0)
    sigma = ret.pow(2).ewm(alpha=1.0 - lam, adjust=False).mean().pow(0.5) / math.sqrt(60.0)
    idx = pd.DatetimeIndex(pd.to_datetime(piv["sec"], unit="s")).tz_localize("UTC")
    base = idx.floor("min") - pd.Timedelta(seconds=60)
    widx = pd.to_datetime(piv["mts"], unit="s", utc=True)
    piv["spot"] = btc["close"].reindex(base).to_numpy()
    piv["s_open"] = btc["open"].reindex(widx).to_numpy()
    piv["sigma"] = sigma.reindex(base).to_numpy()
    piv["tau"] = piv["mts"] + 300 - piv["sec"]
    piv = piv.dropna(subset=["ba_up", "ba_dn", "spot", "s_open", "sigma"])
    z = np.log(piv["spot"] / piv["s_open"]) / (piv["sigma"] * np.sqrt(np.maximum(piv["tau"].clip(lower=1), 1)))
    piv["fair_up"] = 0.5 * (1.0 + np.vectorize(math.erf)(z.to_numpy() / math.sqrt(2.0)))
    return piv


def run() -> dict:
    piv = add_fair_value(pair_books())
    labels = pd.read_parquet(LABEL_PATH)
    labels = labels[labels["up_label"].notna()].copy()
    winner = dict(zip((labels["start"].astype("int64") // 10**9).astype(int), np.where(labels["up_label"].astype(int), "up", "dn")))
    label_matches = int(piv["mts"].map(winner).notna().groupby(piv["mts"]).first().sum())
    rows = []
    for theta in (0.01, 0.02, 0.03, 0.05, 0.08, 0.12):
        m = piv[(piv["tau"] > 20) & (piv["tau"] < 270)]
        sides = []
        for side in ("up", "dn"):
            fair = m["fair_up"] if side == "up" else 1.0 - m["fair_up"]
            take = (fair - m[f"ba_{side}"]) > theta
            sides.append(pd.DataFrame({
                "mts": m.loc[take, "mts"], "sec": m.loc[take, "sec"], "side": side,
                "px": m.loc[take, f"ba_{side}"], "shares": m.loc[take, f"qa_{side}"].clip(upper=75),
            }))
        trades = pd.concat(sides).sort_values("sec").groupby(["mts", "side"]).first().reset_index()
        trades["winner"] = trades["mts"].map(winner)
        trades = trades.dropna(subset=["winner"])
        trades["correct"] = (trades["side"] == trades["winner"]).astype(float)
        trades["pnl_per_share"] = trades["correct"] - trades["px"] - fee(trades["px"])
        daily = trades.assign(day=pd.to_datetime(trades["mts"], unit="s", utc=True).dt.date).groupby("day")["pnl_per_share"].sum()
        rows.append({
            "theta": theta, "trades": int(len(trades)), "windows": int(trades["mts"].nunique()),
            "hit_rate": float(trades["correct"].mean()), "average_ask": float(trades["px"].mean()),
            "net_ev_per_share": float(trades["pnl_per_share"].mean()),
            "net_pnl_at_75_share_cap": float((trades["pnl_per_share"] * trades["shares"]).sum()),
            "positive_days": int((daily > 0).sum()), "days": int(len(daily)),
        })
    result = {
        "period_start_utc": str(pd.to_datetime(piv["mts"].min(), unit="s", utc=True)),
        "period_end_utc": str(pd.to_datetime(piv["mts"].max(), unit="s", utc=True)),
        "snapshot_windows": int(piv["mts"].nunique()), "scored_rows": int(len(piv)),
        "terminal_label_matches": label_matches, "terminal_label_rows_available": int(len(winner)),
        "fee_formula": "0.07 * p * (1-p)",
        "execution_assumption": "captured best ask, immediate fill, 75-share cap; optimistic quote backtest, not confirmed fills",
        "thresholds": rows,
    }
    RESULT_PATH.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, indent=2))
    return result


if __name__ == "__main__":
    run()
