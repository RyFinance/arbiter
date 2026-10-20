"""Shared utilities for edge experiments."""
from pathlib import Path
import math
import numpy as np
import pandas as pd

OUTD = Path(__file__).parent / "out"

def load_paired():
    """Per-(window, second) two-sided book frame."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "pab", str(Path(__file__).parent / "pair_and_backtest.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    piv = mod.load_and_pair()
    return mod.attach_fair(piv)

def fee_rate():
    return 0.07

def taker_fee(p):
    return fee_rate() * p * (1.0 - p)

def phi(x):
    return 0.5 * (1.0 + np.vectorize(math.erf)(np.asarray(x) / math.sqrt(2.0)))

def btc_1m():
    return pd.read_parquet(OUTD / "btc_1m_aug12_25.parquet").set_index("ts")

def btc_1s():
    return pd.read_parquet(OUTD / "btc_1s_aug12_25.parquet").set_index("ts")

def sigma_variants():
    """Return dict of name -> per-minute sigma series (per-second vol)."""
    btc = btc_1m()
    r = np.log(btc.close / btc.open)
    out = {}
    rv60 = r.pow(2).rolling(60).mean().pow(0.5) / math.sqrt(60.0)
    out["rv60m"] = rv60
    for hl in (15, 30, 120):
        lam = 0.5 ** (1.0 / hl)
        v = r.pow(2).ewm(alpha=1 - lam, adjust=False).mean()
        out[f"ewma{hl}m"] = v.pow(0.5) / math.sqrt(60.0)
    # GARCH(1,1)-lite on 1m returns
    r2 = (r ** 2).to_numpy()
    n = len(r2)
    g = np.empty(n); g[:1] = r2[:1] if r2[0] > 0 else (r2[1:60].mean())
    w_, a_, b_ = 0.00001, 0.08, 0.90
    lr = r2[1:60].mean() * (1 - a_ - b_)
    g[0] = lr / (1 - a_ - b_)
    for i in range(1, n):
        g[i] = lr + a_ * r2[i - 1] + b_ * g[i - 1]
    out["garch11"] = pd.Series(np.sqrt(g) / math.sqrt(60.0), index=btc.index)
    return out, btc

def sigma_1s():
    """Per-second realized vol from 1s closes (rolling 900s)."""
    b = btc_1s()
    r = np.log(b.close / b.open)
    return (r.pow(2).rolling(900).mean().pow(0.5)).dropna(), b

def fair_with_sigma(piv, sigma_s, name):
    z = piv.d / (sigma_s * np.sqrt(np.maximum(piv.tau.clip(lower=1), 1)))
    return phi(z)

def taker_backtest(piv, theta, tau_lo=20, tau_hi=270, fee=True, per_window=True,
                  clip=75):
    m = piv[(piv.tau > tau_lo) & (piv.tau < tau_hi)].copy()
    recs = []
    for side in ("up", "dn"):
        fair = m.fair_up if side == "up" else 1.0 - m.fair_up
        e = (fair - m[f"ba_{side}"]) > theta
        recs.append(pd.DataFrame({"mts": m.mts[e], "sec": m.sec[e], "side": side,
                                  "px": m[f"ba_{side}"][e], "fair": fair[e],
                                  "sh": m[f"qa_{side}"][e].clip(upper=clip)}))
    t = pd.concat(recs).sort_values("sec")
    if per_window:
        t = t.groupby(["mts", "side"]).first().reset_index()
    win = piv.drop_duplicates("mts").set_index("mts").btc_winner
    t["win"] = (t.side == t.mts.map(win)).astype(float)
    t["gross"] = t.win - t.px
    if fee:
        t["fee"] = taker_fee(t.px)
        t["pnl"] = t.gross - t.fee
    else:
        t["fee"] = 0.0
        t["pnl"] = t.gross
    return t

def summary(t, ndays=None):
    if ndays is None:
        ndays = pd.to_datetime(t.mts, unit="s", utc=True).dt.date.nunique()
    return {"n": len(t), "n/day": len(t) / ndays, "hit": t.win.mean(),
            "avg_px": t.px.mean(), "gross/sh": t.gross.mean(),
            "fee/sh": t.fee.mean(), "net/sh": t.pnl.mean(),
            "sharpe": t.pnl.mean() / max(t.pnl.std(), 1e-9),
            "net/day_usd": (t.pnl * t.sh).sum() / ndays,
            "pos_days": (t.assign(d=pd.to_datetime(t.mts, unit="s", utc=True).dt.date)
                          .groupby("d").pnl.apply(lambda g: (g * t.loc[g.index, "sh"]).sum()) > 0).mean()}
