#!/usr/bin/env python3
"""Fetch external real-history signals for the LightGBM regime classifier — 2026-08-21.

Both sources are free/keyless and were verified this session to have real
historical depth over the 2025-01-01 -> 2026-08-11 training window (unlike
order-book depth or open-interest history, which only expose current/recent
snapshots via free APIs — see ML_REGIME_BAKEOFF_2026-08-20.md "Follow-up 2"):

  * Fear & Greed Index (alternative.me) — one call, `limit=0` returns the
    complete daily history since 2018-02-01.
  * Binance BTCUSDT funding rate — paginated by fundingTime, ~3 records/day
    (funding settles every 8h).

Usage:
    python scripts/fetch_external_signals.py
    python scripts/fetch_external_signals.py --start 2025-01-01 --end 2026-08-11
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[1]

FEAR_GREED_URL = "https://api.alternative.me/fng/"
BINANCE_FUNDING_URL = "https://fapi.binance.com/fapi/v1/fundingRate"


def fetch_fear_greed(client: httpx.Client) -> List[Dict[str, Any]]:
    """Full history in one call (limit=0 means "all")."""
    resp = client.get(FEAR_GREED_URL, params={"limit": 0, "format": "json"}, timeout=20.0)
    resp.raise_for_status()
    payload = resp.json()
    rows = payload.get("data", [])
    out = []
    for row in rows:
        out.append({
            "timestamp_utc": datetime.fromtimestamp(int(row["timestamp"]), tz=timezone.utc).isoformat(),
            "value": int(row["value"]),
            "classification": row.get("value_classification"),
        })
    out.sort(key=lambda r: r["timestamp_utc"])
    return out


def fetch_binance_funding_rate(client: httpx.Client, start_ms: int, end_ms: int) -> List[Dict[str, Any]]:
    """Paginate by fundingTime; Binance caps each response at 1000 rows."""
    out: List[Dict[str, Any]] = []
    cursor = start_ms
    seen_times = set()
    while cursor < end_ms:
        resp = client.get(
            BINANCE_FUNDING_URL,
            params={"symbol": "BTCUSDT", "startTime": cursor, "endTime": end_ms, "limit": 1000},
            timeout=20.0,
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        new_rows = 0
        for row in batch:
            ft = int(row["fundingTime"])
            if ft in seen_times:
                continue
            seen_times.add(ft)
            out.append({
                "timestamp_utc": datetime.fromtimestamp(ft / 1000.0, tz=timezone.utc).isoformat(),
                "funding_rate": float(row["fundingRate"]),
                "mark_price": float(row.get("markPrice", 0.0)) if row.get("markPrice") else None,
            })
            new_rows += 1
        if new_rows == 0:
            break
        cursor = max(int(row["fundingTime"]) for row in batch) + 1
        time.sleep(0.2)  # keyless public endpoint — be polite, not rate-limit-bound
    out.sort(key=lambda r: r["timestamp_utc"])
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2025-01-01")
    parser.add_argument("--end", default="2026-08-11")
    parser.add_argument("--fear-greed-out", type=Path, default=Path("state/fear_greed_index.json"))
    parser.add_argument("--funding-rate-out", type=Path, default=Path("state/binance_funding_rate_btc.json"))
    args = parser.parse_args()

    start_dt = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end_dt = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)

    with httpx.Client(headers={"User-Agent": "polymarket-arb-research/1.0"}) as client:
        print("[INFO] Fetching Fear & Greed Index full history...")
        fg_rows = fetch_fear_greed(client)
        print(f"[INFO] Fear & Greed: {len(fg_rows)} daily rows, "
              f"{fg_rows[0]['timestamp_utc']} -> {fg_rows[-1]['timestamp_utc']}")

        print(f"[INFO] Fetching Binance BTCUSDT funding rate {args.start} -> {args.end}...")
        fr_rows = fetch_binance_funding_rate(client, start_ms, end_ms)
        if fr_rows:
            print(f"[INFO] Funding rate: {len(fr_rows)} rows, "
                  f"{fr_rows[0]['timestamp_utc']} -> {fr_rows[-1]['timestamp_utc']}")
        else:
            print("[WARN] Funding rate: 0 rows returned")

    args.fear_greed_out.parent.mkdir(parents=True, exist_ok=True)
    args.fear_greed_out.write_text(json.dumps({"source": "alternative.me", "data": fg_rows}, indent=2) + "\n", encoding="utf-8")
    args.funding_rate_out.write_text(json.dumps({"source": "binance_fapi", "symbol": "BTCUSDT", "data": fr_rows}, indent=2) + "\n", encoding="utf-8")
    print(f"[SAVED] {args.fear_greed_out}")
    print(f"[SAVED] {args.funding_rate_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
