#!/usr/bin/env python3
"""Bulk-fetch Binance Vision tick-level trades and aggregate to 1-minute
taker buy/sell volume + order-flow imbalance — 2026-08-22, Phase 0 of the
directional-bot pivot (see docs/BTC_DIRECTION_SIGNAL_2026-08-22.md).

Source: data.binance.vision (free, keyless, static S3-backed daily zips,
same archive family used for bookDepth/metrics in
scripts/fetch_binance_vision_signals.py). One CSV per day at
data/futures/um/daily/trades/BTCUSDT/BTCUSDT-trades-{date}.zip. Columns
(confirmed against a real fetch): id, price, qty, quote_qty, time (ms epoch),
is_buyer_maker. `is_buyer_maker=True` means the trade's SELLER was the
taker (aggressive sell); `is_buyer_maker=False` means the BUYER was the
taker (aggressive buy) — standard Binance convention.

This is the raw tick archive deliberately deferred in
scripts/fetch_binance_vision_signals.py for the volatility-regime model
(~8.8GB for the full window, judged redundant with book depth for a
magnitude target). For a DIRECTION target, aggressor-side volume is a much
more direct expression of buying/selling pressure than resting depth, so
it's worth the fetch here — but the ~15MB/day raw ticks are aggregated to
1-minute buy/sell volume immediately per day and discarded; only the small
aggregated result is persisted, never the raw ticks.

Usage:
    python scripts/fetch_binance_tick_trades.py
    python scripts/fetch_binance_tick_trades.py --start 2025-01-01 --end 2026-08-11
"""
from __future__ import annotations

import argparse
import io
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import httpx
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BINANCE_VISION_BASE = "https://data.binance.vision/data/futures/um/daily/trades"
SYMBOL = "BTCUSDT"


def _daterange(start: datetime, end: datetime):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def _aggregate_to_1min(df: pd.DataFrame) -> pd.DataFrame:
    """Collapse raw ticks to 1-minute taker buy/sell volume + trade count.

    Bucketed on `time` (ms epoch) floored to the minute. Immediately
    discards the tick-level frame in the caller — only this small result
    is ever kept.
    """
    ts = pd.to_datetime(df["time"], unit="ms", utc=True).dt.floor("min")
    is_taker_buy = ~df["is_buyer_maker"]
    buy_vol = df["quote_qty"].where(is_taker_buy, 0.0)
    sell_vol = df["quote_qty"].where(~is_taker_buy, 0.0)
    out = pd.DataFrame({
        "timestamp": ts,
        "taker_buy_quote_vol": buy_vol,
        "taker_sell_quote_vol": sell_vol,
        "trade_count": 1,
    })
    return out.groupby("timestamp", as_index=False).sum()


def _fetch_and_aggregate_one_day(client: httpx.Client, date: datetime) -> Optional[pd.DataFrame]:
    date_str = date.strftime("%Y-%m-%d")
    url = f"{BINANCE_VISION_BASE}/{SYMBOL}/{SYMBOL}-trades-{date_str}.zip"
    try:
        resp = client.get(url, timeout=60.0)
        if resp.status_code != 200:
            return None
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            name = zf.namelist()[0]
            with zf.open(name) as f:
                df = pd.read_csv(
                    f,
                    usecols=["quote_qty", "time", "is_buyer_maker"],
                    dtype={"quote_qty": "float64", "time": "int64", "is_buyer_maker": "bool"},
                )
        agg = _aggregate_to_1min(df)
        del df
        return agg
    except Exception as e:
        print(f"[WARN] trades {date_str} failed: {e!r}")
        return None


def fetch_bulk_aggregated(start: datetime, end: datetime, max_workers: int = 6) -> pd.DataFrame:
    """Fetch+aggregate every day in [start, end]. Raw ticks never accumulate
    across days — each day's frame is aggregated and dropped inside its own
    worker before the next result is collected.
    """
    dates = list(_daterange(start, end))
    frames = []
    missing = []
    with httpx.Client(headers={"User-Agent": "polymarket-arb-research/1.0"}) as client:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(_fetch_and_aggregate_one_day, client, d): d for d in dates}
            done = 0
            for fut in as_completed(futures):
                date = futures[fut]
                agg = fut.result()
                if agg is not None:
                    frames.append(agg)
                else:
                    missing.append(date.strftime("%Y-%m-%d"))
                done += 1
                if done % 20 == 0 or done == len(dates):
                    print(f"[INFO] trades: {done}/{len(dates)} days processed ({len(missing)} missing so far)")
    if missing:
        print(f"[WARN] trades: {len(missing)} missing day(s): {missing[:10]}{'...' if len(missing) > 10 else ''}")
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    out = out.groupby("timestamp", as_index=False).sum().sort_values("timestamp").reset_index(drop=True)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2025-01-01")
    parser.add_argument("--end", default="2026-08-11")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--out", type=Path, default=Path("state/binance_taker_flow_1m_btc.parquet"))
    args = parser.parse_args()

    start = datetime.strptime(args.start, "%Y-%m-%d")
    end = datetime.strptime(args.end, "%Y-%m-%d")

    print(f"[INFO] Fetching+aggregating tick trades {args.start} -> {args.end} ...")
    df = fetch_bulk_aggregated(start, end, max_workers=args.workers)
    print(f"[INFO] taker flow: {len(df)} 1-minute rows")
    if not df.empty:
        out = PROJECT_ROOT / args.out
        out.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(out, index=False)
        print(f"[SAVED] {out}")
        print(df.describe())

    return 0


if __name__ == "__main__":
    sys.exit(main())
