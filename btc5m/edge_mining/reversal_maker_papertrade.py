"""
Live paper-trading harness for the validated reversal signal, executed as a
maker (resting-bid) order to test the real, adverse-selection-net edge found in
edge_mining/ADVERSE_SELECTION_AND_EDGE_2026-08-27.md against live markets.

No real capital, no real orders submitted anywhere -- this only reads public
Binance + Polymarket data and simulates a resting bid using the exact fill rule
validated against real captured order-book data (fill if best-ask ever crosses
down to <= my entry price). Every window's full lifecycle (signal, entry, fills,
resolution, PnL) is appended to state/reversal_maker_papertrade_log.jsonl so a
real, cumulative, honest track record builds up over time instead of resetting
each run.

Usage: python3 edge_mining/reversal_maker_papertrade.py [--windows N] [--variant N1|N2|N3|decay]
"""
from __future__ import annotations
import argparse
import json
import math
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BINANCE_KLINES = "https://api.binance.com/api/v3/klines"
GAMMA_EVENTS = "https://gamma-api.polymarket.com/events"
CLOB_BOOK = "https://clob.polymarket.com/book"
WINDOW_SECONDS = 300
LOG_PATH = Path("state/reversal_maker_papertrade_log.jsonl")
POLL_INTERVAL_S = 8


def http_json(url: str, params: dict | None = None, timeout: float = 10.0):
    if params:
        import urllib.parse
        url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (research script)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def fetch_recent_1m_closes(n: int = 30) -> list[tuple[int, float]]:
    """Return [(open_time_s, close_price), ...] for the last n 1-min candles."""
    data = http_json(BINANCE_KLINES, {"symbol": "BTCUSDT", "interval": "1m", "limit": n})
    return [(int(k[0]) // 1000, float(k[4])) for k in data]


def current_window_ts(now: float | None = None) -> int:
    n = int(now if now is not None else time.time())
    return n - (n % WINDOW_SECONDS)


def compute_5m_bar_scores(closes_1m: list[tuple[int, float]], window_ts: int) -> dict:
    """Build 5-min bar closes from 1m candles strictly BEFORE window_ts (no lookahead),
    then compute score_N1/N2/N3/decay exactly as in scripts/reversal_momentum_study.py."""
    by_5m: dict[int, float] = {}
    for ts, close in closes_1m:
        if ts >= window_ts:
            continue
        bucket = ts - (ts % WINDOW_SECONDS)
        by_5m[bucket] = close  # last close seen in each bucket wins (candles are time-ordered)
    buckets = sorted(by_5m.keys())
    if len(buckets) < 6:
        return {}
    logc = [math.log(by_5m[b]) for b in buckets]
    scores = {}
    for n in (1, 2, 3):
        if len(logc) >= 1 + n:
            scores[f"N{n}"] = logc[-1] - logc[-1 - n]
    if len(logc) >= 6:
        r1 = [logc[i] - logc[i - 1] for i in range(1, len(logc))]
        last5 = r1[-5:]
        w = [math.exp(-math.log(2) * i) for i in range(len(last5))]
        w_sum = sum(w)
        decay = sum(wi * ri for wi, ri in zip(w, reversed(last5))) / w_sum
        scores["decay"] = decay
    return scores


def resolve_window_tokens(window_ts: int) -> dict | None:
    slug = f"btc-updown-5m-{window_ts}"
    try:
        data = http_json(GAMMA_EVENTS, {"slug": slug}, timeout=10.0)
        mkt = data[0]["markets"][0]
        ids = json.loads(mkt["clobTokenIds"])
        return {"slug": slug, "up_token": str(ids[0]), "down_token": str(ids[1]),
                "market_id": mkt.get("id")}
    except Exception as e:
        print(f"  [resolve_window_tokens] failed for {slug}: {e!r}")
        return None


def best_bid_ask(token_id: str) -> tuple[float | None, float | None]:
    try:
        book = http_json(CLOB_BOOK, {"token_id": token_id}, timeout=8.0)
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        bb = max((float(b["price"]) for b in bids), default=None)
        ba = min((float(a["price"]) for a in asks), default=None)
        return bb, ba
    except Exception:
        return None, None


def resolve_outcome(window_ts: int) -> bool | None:
    """TWAP-consistent resolution: last-60s OHLC4 vs window open, using fresh 1m candles."""
    try:
        closes = fetch_recent_1m_closes(n=8)
        by_ts = {ts: c for ts, c in closes}
        open_bucket = window_ts
        if open_bucket not in by_ts:
            return None
        open_price = by_ts[open_bucket]
        last_bucket = window_ts + WINDOW_SECONDS - 60
        if last_bucket not in by_ts:
            return None
        twap_price = by_ts[last_bucket]  # 1m-candle-close proxy, consistent with the rest of this session
        return twap_price >= open_price
    except Exception as e:
        print(f"  [resolve_outcome] failed: {e!r}")
        return None


def append_log(record: dict):
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a") as f:
        f.write(json.dumps(record, default=str) + "\n")


def run_one_window(variant: str, last_window_ts: int | None) -> int:
    """Returns the window_ts actually processed (caller uses this as last_window_ts)."""
    window_ts = current_window_ts()
    if last_window_ts is not None and window_ts <= last_window_ts:
        next_ts = last_window_ts + WINDOW_SECONDS
        sleep_s = max(1.0, next_ts - time.time())
        print(f"  already processed {last_window_ts}, waiting {sleep_s:.0f}s for next window {next_ts}")
        time.sleep(sleep_s)
        window_ts = current_window_ts()
    window_close = window_ts + WINDOW_SECONDS
    print(f"[{datetime.now(timezone.utc).isoformat()}] window_ts={window_ts} "
          f"({datetime.fromtimestamp(window_ts, tz=timezone.utc)})")

    tokens = resolve_window_tokens(window_ts)
    if tokens is None:
        print("  could not resolve tokens, skipping window")
        time.sleep(max(5.0, window_close - time.time()))
        return window_ts

    closes_1m = fetch_recent_1m_closes(n=40)
    scores = compute_5m_bar_scores(closes_1m, window_ts)
    if variant not in scores:
        print(f"  not enough history for variant {variant} yet, skipping")
        time.sleep(max(5.0, window_close - time.time()))
        return window_ts
    raw_score = scores[variant]
    pred_up = raw_score < 0  # reversal: bet against the recent-N-bar return
    pred_token = tokens["up_token"] if pred_up else tokens["down_token"]
    print(f"  variant={variant} raw_score={raw_score:+.6f} -> predict {'UP' if pred_up else 'DOWN'}")

    bb, ba = best_bid_ask(pred_token)
    if bb is None:
        print("  no bid on predicted token, skipping window")
        time.sleep(max(5.0, window_close - time.time()))
        return window_ts
    entry_price = bb
    entry_ts = time.time()
    print(f"  ENTRY (paper maker bid): token={pred_token[:12]}... price={entry_price:.3f}")

    filled = False
    fill_ts = None
    while time.time() < window_close:
        time.sleep(min(POLL_INTERVAL_S, max(1.0, window_close - time.time())))
        _, ba_now = best_bid_ask(pred_token)
        if ba_now is not None and ba_now <= entry_price:
            filled = True
            fill_ts = time.time()
            print(f"  FILLED at {datetime.now(timezone.utc).isoformat()} (ask crossed to {ba_now:.3f})")
            break
    if not filled:
        print("  window closed unfilled, order canceled")
    else:
        remaining = window_close - time.time()
        if remaining > 0:
            time.sleep(remaining)  # outcome depends on the full window's final TWAP, not the fill moment

    time.sleep(20)  # let the final-minute Binance candle close before pulling resolution data
    actual_up = resolve_outcome(window_ts)
    correct = (actual_up == pred_up) if actual_up is not None else None
    payoff = None
    if filled and correct is not None:
        payoff = (1.0 - entry_price) if correct else (-entry_price)

    record = {
        "window_ts": window_ts, "slug": tokens["slug"], "variant": variant,
        "raw_score": raw_score, "pred_up": pred_up, "entry_price": entry_price,
        "entry_ts": entry_ts, "filled": filled, "fill_ts": fill_ts,
        "actual_up": actual_up, "correct": correct, "payoff": payoff,
        "logged_at": datetime.now(timezone.utc).isoformat(),
    }
    append_log(record)
    print(f"  RESULT: actual_up={actual_up} correct={correct} filled={filled} payoff={payoff}")
    return window_ts


def summarize():
    if not LOG_PATH.exists():
        print("no log yet")
        return
    recs = [json.loads(l) for l in LOG_PATH.open() if l.strip()]
    n = len(recs)
    filled = [r for r in recs if r["filled"]]
    resolved_filled = [r for r in filled if r.get("payoff") is not None]
    print(f"\n=== SUMMARY: {n} windows logged, {len(filled)} filled, {len(resolved_filled)} resolved ===")
    if resolved_filled:
        acc = sum(1 for r in resolved_filled if r["correct"]) / len(resolved_filled)
        pnl = sum(r["payoff"] for r in resolved_filled)
        print(f"  accuracy | filled: {acc*100:.2f}%  total PnL: {pnl:+.4f}  avg PnL/trade: {pnl/len(resolved_filled)*100:+.3f}c")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--windows", type=int, default=1000000)
    ap.add_argument("--variant", default="N3", choices=["N1", "N2", "N3", "decay"])
    args = ap.parse_args()
    last_window_ts = None
    for i in range(args.windows):
        try:
            last_window_ts = run_one_window(args.variant, last_window_ts)
        except Exception as e:
            print(f"  window failed: {e!r}")
            time.sleep(30)
        if (i + 1) % 5 == 0:
            summarize()


if __name__ == "__main__":
    main()
