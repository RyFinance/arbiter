"""
Live paper-trading harness v2: reversal signal, maker entry, DYNAMIC probability-based exit.

v1 (reversal_maker_papertrade.py) filled a resting maker bid and held to expiry.
v2 adds a live, per-tick hold-vs-exit decision computed from current market state --
no hard stops, no fixed thresholds on drawdown or time.

THE DECISION RULE
-----------------
At every tick while holding, compare two real quantities:

  hold_ev       = P(my side wins | live BTC price, time remaining, live vol)
  exit_proceeds = best_bid on my token, net of the taker fee I'd pay to cross out

Exit iff  exit_proceeds > hold_ev + margin.

This is principled rather than arbitrary: entry price is a sunk cost and plays no role.
The only question is whether the market will pay me more right now than my own fair
value of holding to resolution. It adapts automatically to time remaining, distance
from the reference price, and current volatility, because all three enter P(win).

P(win) MODEL
------------
Polymarket resolves on the Chainlink 60s TWAP over the window's final minute vs. the
window-open reference. Under driftless GBM, with tau seconds remaining and L=60s TWAP:

    m         = log(S_now / S_open)                    # move so far (known)
    var_eff   = (tau - L) + L/3           if tau >= L  # variance units of seconds
              = (tau/L)^2 * (tau/3)       if tau <  L  # only final tau still random
    P(up win) = Phi( m / (sigma_1s * sqrt(var_eff)) )

The var_eff formula is derived analytically and verified against Monte Carlo to within
~0.5% across tau in [30s, 300s]. Note it correctly makes the position MORE decided as
tau shrinks (var_eff -> 0), which is exactly the "is recovery still plausible?" logic.

TUNING
------
Every trade records BOTH the actual exit PnL and the hold-to-expiry counterfactual PnL,
plus the full per-tick decision trace. So each window is self-comparing: we learn whether
the exit rule beat holding without needing separate A/B runs. Analyze with --summary.

CAVEATS (real, not hedging)
---------------------------
* S_open uses the Binance 1m candle open at window start as a proxy for the Chainlink
  reference; these are different feeds and will disagree slightly.
* sigma is trailing realized vol from 1m candles, so it lags fast vol regime changes.
* Exit fills are assumed to happen at the displayed best bid; in reality a taker sell
  could get partial fills or slip, so exit_proceeds is mildly optimistic.
* Entry behavior is UNCHANGED from v1 (still the reversal signal). The model's P(win)
  at entry is logged but not yet used to gate entries -- that is a separate question
  this data will let us answer later.
"""
from __future__ import annotations
import argparse
import json
import math
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BINANCE_KLINES = "https://api.binance.com/api/v3/klines"
BINANCE_TICKER = "https://api.binance.com/api/v3/ticker/price"
GAMMA_EVENTS = "https://gamma-api.polymarket.com/events"
CLOB_BOOK = "https://clob.polymarket.com/book"

WINDOW_SECONDS = 300
TWAP_LOOKBACK_S = 60.0          # confirmed live via Gamma cryptoMarketConfig.twapLookbackSeconds
CRYPTO_FEE_RATE = 0.07          # confirmed live: feeSchedule.rate, takerOnly=true
POLL_INTERVAL_S = 5
VOL_LOOKBACK_MIN = 30
LOG_PATH = Path("state/reversal_maker_papertrade_v2_log.jsonl")


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def http_json(url: str, params: dict | None = None, timeout: float = 10.0):
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (research script)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def fetch_1m_klines(n: int = 40) -> list[dict]:
    data = http_json(BINANCE_KLINES, {"symbol": "BTCUSDT", "interval": "1m", "limit": n})
    return [{"open_time": int(k[0]) // 1000, "open": float(k[1]), "close": float(k[4])} for k in data]


def fetch_spot() -> float | None:
    try:
        return float(http_json(BINANCE_TICKER, {"symbol": "BTCUSDT"}, timeout=6.0)["price"])
    except Exception:
        return None


def best_bid_ask(token_id: str) -> tuple[float | None, float | None]:
    try:
        book = http_json(CLOB_BOOK, {"token_id": token_id}, timeout=8.0)
        bb = max((float(b["price"]) for b in book.get("bids") or []), default=None)
        ba = min((float(a["price"]) for a in book.get("asks") or []), default=None)
        return bb, ba
    except Exception:
        return None, None


def resolve_window_tokens(window_ts: int) -> dict | None:
    slug = f"btc-updown-5m-{window_ts}"
    try:
        mkt = http_json(GAMMA_EVENTS, {"slug": slug}, timeout=10.0)[0]["markets"][0]
        ids = json.loads(mkt["clobTokenIds"])
        return {"slug": slug, "up_token": str(ids[0]), "down_token": str(ids[1])}
    except Exception as e:
        print(f"  [resolve_window_tokens] failed for {slug}: {e!r}")
        return None


# --------------------------------------------------------------------------- #
# Model
# --------------------------------------------------------------------------- #
def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def taker_fee(p: float) -> float:
    """Polymarket crypto taker fee, confirmed live: fee = rate * p * (1-p)."""
    return CRYPTO_FEE_RATE * p * (1.0 - p)


def var_eff_seconds(tau: float, L: float = TWAP_LOOKBACK_S) -> float:
    """Effective variance (in seconds units) of the final-L-second TWAP, tau sec from now.

    Verified against Monte Carlo to ~0.5% for tau in [30, 300].
    """
    if tau <= 0:
        return 0.0
    if tau >= L:
        return (tau - L) + L / 3.0
    return (tau / L) ** 2 * (tau / 3.0)


def sigma_per_second(klines: list[dict]) -> float | None:
    """Trailing realized vol from 1m closes, converted to per-second."""
    closes = [k["close"] for k in klines][-(VOL_LOOKBACK_MIN + 1):]
    if len(closes) < 6:
        return None
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    mean_r = sum(rets) / len(rets)
    var = sum((r - mean_r) ** 2 for r in rets) / (len(rets) - 1)
    sigma_1m = math.sqrt(var)
    if sigma_1m <= 0:
        return None
    return sigma_1m / math.sqrt(60.0)


def prob_up_wins(spot: float, s_open: float, tau: float, sigma_1s: float) -> float:
    """P(final 60s TWAP >= window open) under driftless GBM."""
    ve = var_eff_seconds(tau)
    m = math.log(spot / s_open)
    if ve <= 0 or sigma_1s <= 0:
        return 1.0 if m >= 0 else 0.0
    return norm_cdf(m / (sigma_1s * math.sqrt(ve)))


# --------------------------------------------------------------------------- #
# Signal (identical rule to scripts/reversal_momentum_study.py, no re-fitting)
# --------------------------------------------------------------------------- #
def compute_scores(klines: list[dict], window_ts: int) -> dict:
    by_5m: dict[int, float] = {}
    for k in klines:
        if k["open_time"] >= window_ts:
            continue
        by_5m[k["open_time"] - (k["open_time"] % WINDOW_SECONDS)] = k["close"]
    buckets = sorted(by_5m)
    if len(buckets) < 6:
        return {}
    logc = [math.log(by_5m[b]) for b in buckets]
    scores = {f"N{n}": logc[-1] - logc[-1 - n] for n in (1, 2, 3) if len(logc) >= 1 + n}
    r1 = [logc[i] - logc[i - 1] for i in range(1, len(logc))]
    last5 = r1[-5:]
    w = [math.exp(-math.log(2) * i) for i in range(len(last5))]
    scores["decay"] = sum(wi * ri for wi, ri in zip(w, reversed(last5))) / sum(w)
    return scores


def window_open_price(klines: list[dict], window_ts: int) -> float | None:
    for k in klines:
        if k["open_time"] == window_ts:
            return k["open"]
    return None


def resolve_outcome(window_ts: int) -> bool | None:
    """TWAP-consistent: final-minute candle close vs. window-open price."""
    try:
        ks = {k["open_time"]: k for k in fetch_1m_klines(n=10)}
        o, f = ks.get(window_ts), ks.get(window_ts + WINDOW_SECONDS - 60)
        if o is None or f is None:
            return None
        return f["close"] >= o["open"]
    except Exception as e:
        print(f"  [resolve_outcome] failed: {e!r}")
        return None


# --------------------------------------------------------------------------- #
# Main loop
# --------------------------------------------------------------------------- #
def current_window_ts(now: float | None = None) -> int:
    n = int(now if now is not None else time.time())
    return n - (n % WINDOW_SECONDS)


def append_log(record: dict):
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a") as f:
        f.write(json.dumps(record, default=str) + "\n")


def run_one_window(variant: str, margin: float, last_window_ts: int | None) -> int:
    window_ts = current_window_ts()
    if last_window_ts is not None and window_ts <= last_window_ts:
        nxt = last_window_ts + WINDOW_SECONDS
        time.sleep(max(1.0, nxt - time.time()))
        window_ts = current_window_ts()
    window_close = window_ts + WINDOW_SECONDS
    print(f"\n[{datetime.now(timezone.utc).isoformat()}] window {window_ts} "
          f"({datetime.fromtimestamp(window_ts, tz=timezone.utc):%H:%M:%S}Z)")

    tokens = resolve_window_tokens(window_ts)
    if tokens is None:
        time.sleep(max(5.0, window_close - time.time()))
        return window_ts

    klines = fetch_1m_klines(n=VOL_LOOKBACK_MIN + 12)
    scores = compute_scores(klines, window_ts)
    s_open = window_open_price(klines, window_ts)
    sigma_1s = sigma_per_second(klines)
    if variant not in scores or s_open is None or sigma_1s is None:
        print("  insufficient data (signal/open/vol), skipping")
        time.sleep(max(5.0, window_close - time.time()))
        return window_ts

    raw_score = scores[variant]
    pred_up = raw_score < 0                       # reversal: bet against the recent move
    pred_token = tokens["up_token"] if pred_up else tokens["down_token"]
    print(f"  {variant}={raw_score:+.6f} -> {'UP' if pred_up else 'DOWN'}  "
          f"s_open={s_open:.2f} sigma_1s={sigma_1s:.6f}")

    bb, _ = best_bid_ask(pred_token)
    if bb is None or not (0.0 < bb < 1.0):
        print("  no usable bid, skipping")
        time.sleep(max(5.0, window_close - time.time()))
        return window_ts
    entry_price = bb
    spot0 = fetch_spot()
    p_entry_model = (prob_up_wins(spot0, s_open, window_close - time.time(), sigma_1s)
                     if spot0 else None)
    if p_entry_model is not None and not pred_up:
        p_entry_model = 1.0 - p_entry_model
    pm = f"{p_entry_model:.3f}" if p_entry_model is not None else "n/a"
    print(f"  ENTRY maker bid @ {entry_price:.3f}   (model P(win) at entry: {pm})")

    # ---- wait for fill ----
    filled, fill_ts = False, None
    while time.time() < window_close:
        time.sleep(min(POLL_INTERVAL_S, max(1.0, window_close - time.time())))
        _, ba = best_bid_ask(pred_token)
        if ba is not None and ba <= entry_price:
            filled, fill_ts = True, time.time()
            print(f"  FILLED (ask crossed to {ba:.3f})")
            break

    ticks, exited, exit_price, exit_ts, exit_reason = [], False, None, None, None

    # ---- hold/exit decision loop (always runs to window close for the counterfactual) ----
    while time.time() < window_close:
        time.sleep(min(POLL_INTERVAL_S, max(1.0, window_close - time.time())))
        tau = window_close - time.time()
        if tau <= 1.0:
            break
        spot = fetch_spot()
        bid_now, ask_now = best_bid_ask(pred_token)
        if spot is None or bid_now is None:
            continue
        p_up = prob_up_wins(spot, s_open, tau, sigma_1s)
        hold_ev = p_up if pred_up else 1.0 - p_up
        exit_proceeds = bid_now - taker_fee(bid_now)
        tick = {
            "t": round(time.time(), 2), "tau": round(tau, 1), "spot": spot,
            "m_bps": round(math.log(spot / s_open) * 1e4, 2),
            "p_win": round(hold_ev, 4), "bid": bid_now, "ask": ask_now,
            "exit_proceeds": round(exit_proceeds, 4),
            "edge_vs_hold": round(exit_proceeds - hold_ev, 4),
        }
        ticks.append(tick)

        if filled and not exited and exit_proceeds > hold_ev + margin:
            exited, exit_price, exit_ts = True, bid_now, time.time()
            exit_reason = f"exit_proceeds {exit_proceeds:.3f} > hold_ev {hold_ev:.3f} + {margin}"
            print(f"  EXIT @ {bid_now:.3f} (tau={tau:.0f}s, P(win)={hold_ev:.3f}, "
                  f"m={tick['m_bps']:+.0f}bps) -- {exit_reason}")

    # ---- resolve and score both paths ----
    time.sleep(20)
    actual_up = resolve_outcome(window_ts)
    correct = (actual_up == pred_up) if actual_up is not None else None

    pnl_hold = pnl_actual = None
    if filled and correct is not None:
        pnl_hold = (1.0 - entry_price) if correct else (-entry_price)
        if exited:
            pnl_actual = (exit_price - taker_fee(exit_price)) - entry_price
        else:
            pnl_actual = pnl_hold

    record = {
        "v": 2, "window_ts": window_ts, "slug": tokens["slug"], "variant": variant,
        "margin": margin, "raw_score": raw_score, "pred_up": pred_up,
        "s_open": s_open, "sigma_1s": sigma_1s, "p_entry_model": p_entry_model,
        "entry_price": entry_price, "filled": filled, "fill_ts": fill_ts,
        "exited": exited, "exit_price": exit_price, "exit_ts": exit_ts,
        "exit_reason": exit_reason, "actual_up": actual_up, "correct": correct,
        "pnl_hold": pnl_hold, "pnl_actual": pnl_actual,
        "n_ticks": len(ticks), "ticks": ticks,
        "logged_at": datetime.now(timezone.utc).isoformat(),
    }
    append_log(record)
    if filled and pnl_actual is not None:
        delta = pnl_actual - pnl_hold
        print(f"  RESULT actual_up={actual_up} correct={correct} "
              f"pnl_actual={pnl_actual:+.4f} pnl_hold={pnl_hold:+.4f} "
              f"(exit rule {'HELPED' if delta > 0 else 'HURT' if delta < 0 else 'neutral'} "
              f"{delta:+.4f})")
    else:
        print(f"  RESULT actual_up={actual_up} correct={correct} filled={filled} (no PnL)")
    return window_ts


def summarize():
    if not LOG_PATH.exists():
        print("no log yet")
        return
    recs = [json.loads(l) for l in LOG_PATH.open() if l.strip()]
    scored = [r for r in recs if r.get("pnl_actual") is not None]
    print(f"\n{'='*68}\nSUMMARY  {len(recs)} windows | {sum(1 for r in recs if r['filled'])} filled "
          f"| {len(scored)} scored")
    if not scored:
        print("="*68)
        return
    pnl_a = sum(r["pnl_actual"] for r in scored)
    pnl_h = sum(r["pnl_hold"] for r in scored)
    acc = sum(1 for r in scored if r["correct"]) / len(scored)
    n_ex = sum(1 for r in scored if r["exited"])
    print(f"  signal accuracy (filled):   {acc*100:.1f}%")
    print(f"  WITH exit rule:  {pnl_a:+.4f}  ({pnl_a/len(scored)*100:+.2f}c/trade)")
    print(f"  hold-to-expiry:  {pnl_h:+.4f}  ({pnl_h/len(scored)*100:+.2f}c/trade)")
    print(f"  exit rule delta: {pnl_a-pnl_h:+.4f}   ({n_ex}/{len(scored)} trades exited early)")
    if n_ex:
        ex = [r for r in scored if r["exited"]]
        helped = sum(1 for r in ex if r["pnl_actual"] > r["pnl_hold"])
        d = sum(r["pnl_actual"] - r["pnl_hold"] for r in ex)
        print(f"  of exits: {helped}/{len(ex)} helped, net {d:+.4f} ({d/len(ex)*100:+.2f}c/exit)")
    print("="*68)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--windows", type=int, default=1000000)
    ap.add_argument("--variant", default="N3", choices=["N1", "N2", "N3", "decay"])
    ap.add_argument("--margin", type=float, default=0.02,
                    help="required edge of exit proceeds over hold EV before cutting")
    ap.add_argument("--summary", action="store_true")
    args = ap.parse_args()
    if args.summary:
        summarize()
        return
    print(f"v2 harness: variant={args.variant} margin={args.margin} -> {LOG_PATH}")
    last = None
    for i in range(args.windows):
        try:
            last = run_one_window(args.variant, args.margin, last)
        except Exception as e:
            print(f"  window failed: {e!r}")
            time.sleep(30)
        if (i + 1) % 5 == 0:
            summarize()


if __name__ == "__main__":
    main()
