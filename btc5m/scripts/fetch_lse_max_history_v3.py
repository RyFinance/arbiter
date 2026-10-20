#!/usr/bin/env python3
"""Fetch maximum real BTC/USD 1m history from LSE vault via date-windowed pagination.

The vault /candles endpoint caps each request at 5000 rows and does not support
offset/page. However it honors the `start` param, so we can paginate by advancing
the start date past the last timestamp in each batch.

Reports the real total row count and date range. Saves with timestamps to
state/btc_1m_candles_lse_v3.parquet (does NOT overwrite the v2 parquet).
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from polymarket_arb.config import SETTINGS

UTC = timezone.utc
_LSE_PLACEHOLDER_KEY = "lse_live_00000000000000000000000000000000"
LSE_VAULT_URL = "https://api.londonstrategicedge.com/vault"


def _active_key() -> str:
    key = SETTINGS.lse_api_key
    if not key:
        return _LSE_PLACEHOLDER_KEY
    return key


def normalize(rows: list) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
    frame = pd.DataFrame(rows).copy()
    ts_col = "ts" if "ts" in frame.columns else "timestamp"
    result = frame[[ts_col, "open", "high", "low", "close", "volume"]].rename(columns={ts_col: "timestamp"})
    result["timestamp"] = pd.to_datetime(result["timestamp"], utc=True, errors="coerce")
    for col in ("open", "high", "low", "close", "volume"):
        result[col] = pd.to_numeric(result[col], errors="coerce")
    result = result.dropna().drop_duplicates(subset="timestamp").sort_values("timestamp")
    result = result[(result["close"] > 0) & (result["high"] > 0) & (result["low"] > 0)]
    return result.reset_index(drop=True)


async def fetch_batch(session: httpx.AsyncClient, start_date: str) -> pd.DataFrame:
    params = {"symbol": "BTC/USD", "timeframe": "1m", "start": start_date, "limit": "50000"}
    resp = await session.get("/candles", params=params)
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict):
        data = data.get("data", [])
    return normalize(data if isinstance(data, list) else [])


async def main():
    key = _active_key()
    print(f"[INFO] Using key: {key[:12]}...")

    all_dfs: list[pd.DataFrame] = []

    # Fetch forward from 2026-05-01 (the date range the v2 metrics used) up to today.
    # Each batch is ~5000 rows (~3.5 days of 1m candles). We advance start by
    # the last timestamp + 1 minute after each batch.
    start_date = "2026-05-01"
    end_ceiling = datetime.now(UTC)
    total_rows = 0
    batch_num = 0

    async with httpx.AsyncClient(
        base_url=LSE_VAULT_URL,
        timeout=60.0,
        headers={"x-api-key": key, "Content-Type": "application/json"},
    ) as session:
        while True:
            batch = await fetch_batch(session, start_date)
            if batch.empty:
                print(f"[batch {batch_num}] empty at start={start_date}, stopping")
                break
            total_rows += len(batch)
            last_ts = batch["timestamp"].iloc[-1]
            print(f"[batch {batch_num}] {len(batch)} rows, {batch['timestamp'].iloc[0]} to {last_ts}, total={total_rows}")
            all_dfs.append(batch)
            # Advance past the last timestamp
            next_start = (last_ts + timedelta(minutes=1)).strftime("%Y-%m-%d")
            next_start_dt = pd.to_datetime(next_start, utc=True)
            if next_start_dt >= end_ceiling:
                print(f"[done] reached end ceiling ({end_ceiling.date()})")
                break
            # Avoid infinite loop if the same start returns the same data
            if next_start == start_date:
                # Advance by the span of the batch
                span = last_ts - batch["timestamp"].iloc[0]
                next_start = (last_ts + timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
            start_date = next_start
            batch_num += 1
            if batch_num > 200:
                print("[safety] hit 200 batch limit")
                break

    if not all_dfs:
        print("[ERROR] No data fetched")
        return 1

    df = pd.concat(all_dfs, ignore_index=True)
    df = df.drop_duplicates(subset="timestamp").sort_values("timestamp").reset_index(drop=True)
    gaps = int(df["timestamp"].diff().gt(pd.Timedelta(minutes=1)).sum())

    out_path = PROJECT_ROOT / "state" / "btc_1m_candles_lse_v3.parquet"
    df.to_parquet(out_path, index=False)

    report = {
        "total_rows": len(df),
        "range_start": df["timestamp"].iloc[0].isoformat(),
        "range_end": df["timestamp"].iloc[-1].isoformat(),
        "gaps_over_1min": gaps,
        "batches": batch_num + 1,
        "saved_to": str(out_path),
    }
    report_path = PROJECT_ROOT / "state" / "lse_history_probe_v3.json"
    report_path.write_text(json.dumps(report, indent=2))

    print(f"\n[FINAL] {len(df)} rows, {df['timestamp'].iloc[0]} to {df['timestamp'].iloc[-1]}")
    print(f"[FINAL] gaps >1min: {gaps}")
    print(f"[SAVED] {out_path}")
    print(f"[SAVED] {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
