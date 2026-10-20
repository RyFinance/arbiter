#!/usr/bin/env python3
"""Download resolved BTC Up/Down outcome labels from Polymarket's Gamma API.

This is deliberately *not* a price or fill-data downloader. Gamma is used only
for settled-market metadata: the market identifier, resolution status, end
time, and terminal outcome token values. It never uses outcomePrices, bestBid,
or bestAsk as an executable price. The CLOB is still required for any claim of
tradability.

The script caches a compact, normalized Parquet file. It checks that each
market has exactly one terminal winner; ties/ambiguous results are retained as
rows with a null binary label so analyses can exclude them transparently.
"""

from __future__ import annotations

import argparse
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import requests


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "state" / "polymarket_btc_updown_resolved_outcomes.parquet"
GAMMA_EVENTS = "https://gamma-api.polymarket.com/events"
SERIES = {
    "5m": {
        "series_id": "10684",
        "horizon_minutes": 5,
        "slug_prefix": "btc-updown-5m-",
        "first_listed": "2025-11-21T00:00:00Z",
        "slice_days": 5,
    },
    "15m": {
        "series_id": "10192",
        "horizon_minutes": 15,
        "slug_prefix": "btc-updown-15m-",
        "first_listed": "2025-08-09T00:00:00Z",
        "slice_days": 20,
    },
    "1h": {
        "series_id": "10114",
        "horizon_minutes": 60,
        "slug_prefix": "bitcoin-up-or-down-",
        "first_listed": "2025-05-27T00:00:00Z",
        "slice_days": 60,
    },
    "4h": {
        "series_id": "10331",
        "horizon_minutes": 240,
        "slug_prefix": "btc-updown-4h-",
        "first_listed": "2025-09-24T00:00:00Z",
        "slice_days": 365,
    },
}


def as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        return json.loads(value)
    raise TypeError(f"unexpected list field: {type(value)}")


def parse_terminal_up(outcomes: Any, prices: Any) -> int | None:
    """Return Up=1, Down=0 only for a single decisive $1 winner."""
    outcome_names = [str(x).strip().lower() for x in as_list(outcomes)]
    terminal_prices = [float(x) for x in as_list(prices)]
    if len(outcome_names) != 2 or len(terminal_prices) != 2:
        return None
    winners = [i for i, p in enumerate(terminal_prices) if p >= 0.999999]
    if len(winners) != 1:
        return None
    winner = outcome_names[winners[0]]
    if winner == "up":
        return 1
    if winner == "down":
        return 0
    return None


def market_start(market: dict[str, Any], horizon_minutes: int, product: str) -> pd.Timestamp | None:
    slug = str(market.get("slug", ""))
    end = pd.to_datetime(market.get("endDate"), utc=True, errors="coerce")
    if pd.isna(end):
        return None
    if product in ("5m", "15m", "4h"):
        match = re.search(r"-(\d+)$", slug)
        if not match:
            return None
        start = pd.to_datetime(int(match.group(1)), unit="s", utc=True)
        # Reject a malformed slug rather than silently applying an assumed
        # schedule. A one-second API rounding tolerance is more than enough.
        if abs((end - (start + pd.Timedelta(minutes=horizon_minutes))).total_seconds()) > 1.0:
            return None
        return start
    # Older hourly slugs are ``...-may-28-10-pm-et-candle``; newer ones are
    # ``...-may-26-2026-7pm-et``.  Gamma's nested market ``endDate`` was
    # historically copied from the containing event (often noon for an entire
    # day's hourly markets), so deriving start from that field would duplicate
    # every hour.  Parse the title/slug instead and use the API date only to
    # fill the omitted year in the pre-2026 format.
    month_names = {
        "january": 1, "february": 2, "march": 3, "april": 4,
        "may": 5, "june": 6, "july": 7, "august": 8,
        "september": 9, "october": 10, "november": 11, "december": 12,
    }
    m = re.search(
        r"bitcoin-up-or-down-([a-z]+)-(\d{1,2})(?:-(\d{4}))?-(\d{1,2})(?:-)?(am|pm)-et",
        slug,
    )
    if not m:
        return None
    month_text, day_text, year_text, hour_text, ampm = m.groups()
    month = month_names.get(month_text)
    if month is None:
        return None
    hour = int(hour_text) % 12
    if ampm == "pm":
        hour += 12
    years = [int(year_text)] if year_text else [int(end.year) - 1, int(end.year), int(end.year) + 1]
    candidates: list[pd.Timestamp] = []
    for year in years:
        try:
            local = pd.Timestamp(year=year, month=month, day=int(day_text), hour=hour, tz="America/New_York")
        except Exception:
            continue
        candidates.append(local.tz_convert("UTC"))
    if not candidates:
        return None
    if year_text:
        return candidates[0]
    # For pre-2026 slugs, choose the calendar year whose parsed one-hour
    # window is nearest the API endDate.  This handles Dec/Jan rollover while
    # tolerating the old container-level (roughly noon) endDate.
    return min(candidates, key=lambda ts: abs((ts + pd.Timedelta(minutes=horizon_minutes) - end).total_seconds()))


def fetch_window(
    product: str,
    start: pd.Timestamp,
    end_exclusive: pd.Timestamp,
    pause_seconds: float,
    session: requests.Session,
) -> list[dict[str, Any]]:
    spec = SERIES[product]
    offset = 0
    records: list[dict[str, Any]] = []
    pages = 0
    while True:
        params = {
            "series_id": spec["series_id"],
            "closed": "true",
            "end_date_min": start.isoformat().replace("+00:00", "Z"),
            # Gamma treats endpoints as inclusive. The one-microsecond gap
            # makes adjacent windows half-open and avoids duplicate markets.
            "end_date_max": (end_exclusive - pd.Timedelta(microseconds=1)).isoformat().replace("+00:00", "Z"),
            "limit": 100,
            "offset": offset,
            "order": "endDate",
            "ascending": "true",
        }
        response = None
        for attempt in range(5):
            response = session.get(GAMMA_EVENTS, params=params, timeout=90)
            if response.status_code not in (429, 500, 502, 503, 504):
                break
            time.sleep(min(10.0, 0.5 * 2**attempt))
        assert response is not None
        response.raise_for_status()
        page = response.json()
        if not isinstance(page, list):
            raise ValueError(f"unexpected Gamma response at offset={offset}: {type(page)}")
        pages += 1
        for event in page:
            for market in event.get("markets", []):
                if str(market.get("slug", "")).startswith(spec["slug_prefix"]) is False:
                    continue
                market_end = pd.to_datetime(market.get("endDate"), utc=True, errors="coerce")
                market_start_time = market_start(market, spec["horizon_minutes"], product)
                if market_start_time is None or pd.isna(market_end):
                    continue
                # For hourly products the API endDate can be a stale container
                # value; the parsed title defines the actual one-hour window.
                if product == "1h":
                    market_end = market_start_time + pd.Timedelta(minutes=spec["horizon_minutes"])
                label = parse_terminal_up(market.get("outcomes"), market.get("outcomePrices"))
                records.append(
                    {
                        "product": product,
                        "series_id": spec["series_id"],
                        "horizon_minutes": spec["horizon_minutes"],
                        "event_slug": event.get("slug"),
                        "market_slug": market.get("slug"),
                        "condition_id": market.get("conditionId"),
                        "start": market_start_time,
                        "end": market_end,
                        "closed": bool(market.get("closed")),
                        "resolution_status": market.get("umaResolutionStatus"),
                        "up_label": label,
                        "outcomes_json": json.dumps(as_list(market.get("outcomes"))),
                        "terminal_outcome_prices_json": json.dumps(as_list(market.get("outcomePrices"))),
                        "resolution_source": market.get("resolutionSource"),
                        "description": market.get("description"),
                        "closed_time": market.get("closedTime"),
                    }
                )
        print(
            f"{product}: {start.date()}..{end_exclusive.date()} page={pages} offset={offset} rows={len(records)}",
            flush=True,
        )
        if len(page) < 100:
            break
        offset += 100
        if pause_seconds:
            time.sleep(pause_seconds)
    return records


def fetch_series(
    product: str,
    requested_start: pd.Timestamp,
    end: pd.Timestamp,
    pause_seconds: float,
    workers: int,
) -> list[dict[str, Any]]:
    spec = SERIES[product]
    start = max(requested_start, pd.Timestamp(spec["first_listed"]))
    windows: list[tuple[pd.Timestamp, pd.Timestamp]] = []
    cursor = start
    while cursor < end:
        next_cursor = min(cursor + pd.Timedelta(days=spec["slice_days"]), end)
        windows.append((cursor, next_cursor))
        cursor = next_cursor

    def run_window(window: tuple[pd.Timestamp, pd.Timestamp]) -> list[dict[str, Any]]:
        session = requests.Session()
        session.headers.update({"User-Agent": "polymarket-arb-research/1.0 (resolved-label-audit)"})
        return fetch_window(product, window[0], window[1], pause_seconds, session)

    records: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = [executor.submit(run_window, window) for window in windows]
        for future in as_completed(futures):
            records.extend(future.result())
    return records


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--products", nargs="+", choices=tuple(SERIES), default=tuple(SERIES))
    parser.add_argument("--start", default="2025-01-01T00:00:00Z")
    parser.add_argument("--end", default="2026-08-11T08:04:00Z")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--pause-seconds", type=float, default=0.02)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    if args.out.exists() and not args.force:
        raise SystemExit(f"{args.out} exists; refusing to overwrite without --force")

    start = pd.Timestamp(args.start)
    end = pd.Timestamp(args.end)
    if start.tzinfo is None or end.tzinfo is None:
        raise SystemExit("--start and --end must include a timezone")
    all_rows: list[dict[str, Any]] = []
    for product in args.products:
        all_rows.extend(fetch_series(product, start, end, args.pause_seconds, args.workers))
    if not all_rows:
        raise SystemExit("no rows retrieved")
    df = pd.DataFrame(all_rows).drop_duplicates(subset=["condition_id"]).sort_values(["product", "start"])
    df["closed_time"] = pd.to_datetime(df["closed_time"], utc=True, errors="coerce")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(args.out, index=False)
    summary = (
        df.groupby("product")
        .agg(
            rows=("condition_id", "size"),
            decisive_labels=("up_label", lambda x: int(x.notna().sum())),
            start=("start", "min"),
            end=("end", "max"),
        )
        .to_string()
    )
    print(f"wrote {len(df):,} rows to {args.out}")
    print(summary)


if __name__ == "__main__":
    main()
