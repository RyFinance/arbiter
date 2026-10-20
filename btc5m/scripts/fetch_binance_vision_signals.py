#!/usr/bin/env python3
"""Bulk-fetch real historical order-book depth, open interest/long-short
ratio, and cross-asset signals for the LightGBM regime classifier —
2026-08-21 (ML_REGIME_BAKEOFF_2026-08-20.md follow-up 3).

Sources (all free, all verified this session to have real history over the
2025-01-01 -> 2026-08-11 training window):

  * Binance Vision (data.binance.vision) — a static, keyless, S3-backed bulk
    archive, NOT the same as Binance's live REST endpoints (which only
    expose current/recent snapshots and were correctly rejected as
    infeasible in follow-up 2). One daily zip per symbol per data type:
      - bookDepth: order-book depth at +/-1..5% from mid, ~every 35s.
        Columns: timestamp, percentage, depth, notional.
      - metrics: open interest + long/short ratios at 5-minute granularity,
        back to 2020-09-01. Columns: create_time, symbol,
        sum_open_interest, sum_open_interest_value,
        count_toptrader_long_short_ratio, sum_toptrader_long_short_ratio,
        count_long_short_ratio, sum_taker_long_short_vol_ratio.
  * yfinance (already a project dependency) — daily OHLCV for ETH-USD and a
    couple of other cross-asset references, no auth, no rate limit hit.

Deliberately NOT fetched here: tick-level trades (also on Binance Vision,
confirmed present, but ~8.8GB for the full window vs bookDepth/metrics'
~270MB combined, for what's likely smaller marginal value over the 1-minute
OHLCV already in the model — see the report for the reasoning).

Usage:
    python scripts/fetch_binance_vision_signals.py
    python scripts/fetch_binance_vision_signals.py --start 2025-01-01 --end 2026-08-11
    python scripts/fetch_binance_vision_signals.py --skip-cross-asset
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
BINANCE_VISION_BASE = "https://data.binance.vision/data/futures/um/daily"
SYMBOL = "BTCUSDT"


def _daterange(start: datetime, end: datetime):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def _fetch_one_day(client: httpx.Client, data_type: str, date: datetime) -> Optional[pd.DataFrame]:
    date_str = date.strftime("%Y-%m-%d")
    url = f"{BINANCE_VISION_BASE}/{data_type}/{SYMBOL}/{SYMBOL}-{data_type}-{date_str}.zip"
    try:
        resp = client.get(url, timeout=20.0)
        if resp.status_code != 200:
            return None
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            name = zf.namelist()[0]
            with zf.open(name) as f:
                df = pd.read_csv(f)
        return df
    except Exception as e:
        print(f"[WARN] {data_type} {date_str} failed: {e!r}")
        return None


def fetch_bulk(data_type: str, start: datetime, end: datetime, max_workers: int = 8) -> pd.DataFrame:
    """Fetch one daily-archive data type for every day in [start, end], concatenated.

    Binance Vision is a static CDN (not a rate-limited live API), so modest
    concurrency is fine — still bounded to be a considerate client.
    """
    dates = list(_daterange(start, end))
    frames = []
    missing = []
    with httpx.Client(headers={"User-Agent": "polymarket-arb-research/1.0"}) as client:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(_fetch_one_day, client, data_type, d): d for d in dates}
            done = 0
            for fut in as_completed(futures):
                date = futures[fut]
                df = fut.result()
                if df is not None:
                    frames.append(df)
                else:
                    missing.append(date.strftime("%Y-%m-%d"))
                done += 1
                if done % 100 == 0 or done == len(dates):
                    print(f"[INFO] {data_type}: {done}/{len(dates)} days fetched ({len(missing)} missing so far)")
    if missing:
        print(f"[WARN] {data_type}: {len(missing)} missing day(s): {missing[:10]}{'...' if len(missing) > 10 else ''}")
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def fetch_cross_asset(start: str, end: str) -> pd.DataFrame:
    """Daily close for a small cross-asset basket via yfinance."""
    import yfinance as yf

    tickers = {"ETH-USD": "eth", "GC=F": "gold", "DX-Y.NYB": "dxy"}
    frames = []
    for ticker, label in tickers.items():
        try:
            df = yf.download(ticker, start=start, end=end, progress=False, auto_adjust=True)
            if df.empty:
                print(f"[WARN] yfinance returned nothing for {ticker}")
                continue
            df = df.reset_index()
            if isinstance(df.columns, pd.MultiIndex):
                # Recent yfinance versions return MultiIndex columns
                # (field, ticker) even for a single symbol.
                df.columns = [c[0] for c in df.columns]
            out = df[["Date", "Close"]].rename(columns={"Date": "date", "Close": f"{label}_close"})
            frames.append(out.set_index("date"))
            print(f"[INFO] {ticker}: {len(out)} daily rows")
        except Exception as e:
            print(f"[WARN] yfinance fetch failed for {ticker}: {e!r}")
    if not frames:
        return pd.DataFrame()
    combined = frames[0]
    for f in frames[1:]:
        combined = combined.join(f, how="outer")
    return combined.reset_index()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2025-01-01")
    parser.add_argument("--end", default="2026-08-11")
    parser.add_argument("--skip-bookdepth", action="store_true")
    parser.add_argument("--skip-metrics", action="store_true")
    parser.add_argument("--skip-cross-asset", action="store_true")
    parser.add_argument("--bookdepth-out", type=Path, default=Path("state/binance_bookdepth_btc.parquet"))
    parser.add_argument("--metrics-out", type=Path, default=Path("state/binance_oi_longshort_btc.parquet"))
    parser.add_argument("--cross-asset-out", type=Path, default=Path("state/cross_asset_daily.parquet"))
    args = parser.parse_args()

    start = datetime.strptime(args.start, "%Y-%m-%d")
    end = datetime.strptime(args.end, "%Y-%m-%d")

    if not args.skip_bookdepth:
        print(f"[INFO] Fetching bookDepth {args.start} -> {args.end} ...")
        df = fetch_bulk("bookDepth", start, end)
        print(f"[INFO] bookDepth: {len(df)} total rows")
        if not df.empty:
            out = PROJECT_ROOT / args.bookdepth_out
            out.parent.mkdir(parents=True, exist_ok=True)
            df.to_parquet(out, index=False)
            print(f"[SAVED] {out}")

    if not args.skip_metrics:
        print(f"[INFO] Fetching metrics (OI/long-short) {args.start} -> {args.end} ...")
        df = fetch_bulk("metrics", start, end)
        print(f"[INFO] metrics: {len(df)} total rows")
        if not df.empty:
            out = PROJECT_ROOT / args.metrics_out
            out.parent.mkdir(parents=True, exist_ok=True)
            df.to_parquet(out, index=False)
            print(f"[SAVED] {out}")

    if not args.skip_cross_asset:
        print(f"[INFO] Fetching cross-asset daily bars via yfinance {args.start} -> {args.end} ...")
        df = fetch_cross_asset(args.start, args.end)
        print(f"[INFO] cross-asset: {len(df)} rows")
        if not df.empty:
            out = PROJECT_ROOT / args.cross_asset_out
            out.parent.mkdir(parents=True, exist_ok=True)
            df.to_parquet(out, index=False)
            print(f"[SAVED] {out}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
