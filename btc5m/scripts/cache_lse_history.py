#!/usr/bin/env python3
"""Pull and cache maximum retrievable real LSE history into state/cache/.

Caches two sources from the LSE Vault Edge API (https://api.londonstrategicedge.com/vault):

1. ``candles``  — BTC/USD 1m candles, paginated forward by ``start`` date until the
   API returns nothing or we hit the end ceiling. Max-spec ~9 years (2017-08-17 onward).
2. ``economic_calendar`` — full macro/economic event calendar, paginated monthly via
   ``start``/``end`` date filters (rows are capped at 5000 per request by the API).

Each cached artifact is written alongside a ``*_meta.json`` sidecar capturing:
  - fetch timestamp (UTC ISO-8601 + unix epoch)
  - source endpoint + symbol + timeframe
  - raw row count + deduplicated row count
  - date range (first/last timestamps)
  - gap report (count of intervals strictly greater than the native cadence)
  - SHA-256 of the payload bytes (the on-disk parquet/CSV hash)

The API key is read from ``SETTINGS.lse_api_key`` (config.py). It is NOT trusted
blind: we refuse to run if the resolve key is empty, and we always report the
key fingerprint used in the metadata so an operator can confirm which key pulled
what. The hardcoded default in config.py (``lse_live_...``) is treated as a real
key only after a live probe — see ``_verify_key_live``.

Idempotent: re-running overwrites only the artifacts whose fresh fetch produced
strictly more (or equal) rows than the on-disk copy; otherwise the existing
cache is preserved and the run is a no-op refresh of metadata. This protects
against partially failing pulls clobbering a known-good cache.

USAGE:
    python scripts/cache_lse_history.py           # fetch both sources
    python scripts/cache_lse_history.py --candles-only
    python scripts/cache_lse_history.py --calendar-only
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from polymarket_arb.config import SETTINGS  # noqa: E402

UTC = timezone.utc
LSE_VAULT_URL = "https://api.londonstrategicedge.com/vault"
CACHE_DIR = PROJECT_ROOT / "state" / "cache"

CANDLES_FILE = CACHE_DIR / "btc_usd_1m_candles.parquet"
CANDLES_META = CACHE_DIR / "btc_usd_1m_candles_meta.json"
CALENDAR_FILE = CACHE_DIR / "economic_calendar.csv"
CALENDAR_META = CACHE_DIR / "economic_calendar_meta.json"

# Conservative fetch ceilings. The API caps each /candles request at ~5000 rows;
# we request 5000 and paginate by advancing ``start`` past the last returned ts.
CANDLES_LIMIT = 5000
CANDLES_MAX_BATCHES = 200_000  # safety valve; ~9.5 months of 1m rows per batch
# Monthly windows keep /ref/economic_calendar requests well under the 5000-row cap
# (full-year windows overflow: 2024 returned exactly 5000 = cap-hit).
CALENDAR_LIMIT = 5000


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _key_fingerprint(key: str) -> str:
    """Return a non-reversible fingerprint of the API key for the metadata sidecar.

    SHA-256 of the key. The sidecar stores ONLY the hash + the first/last 4 chars,
    never the full key. This lets an operator confirm "this cache was pulled with
    the same key the operator set" without leaking the key into a cache file.
    """
    if not key:
        return "<empty>"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    masked = f"{key[:4]}…{key[-4:]}" if len(key) > 8 else "<short>"
    return f"{masked} sha256={digest[:16]}"


async def _verify_key_live(client: httpx.AsyncClient, key: str) -> bool:
    """Probe the API with a 1-row candle request. True only on HTTP 200 + JSON list."""
    if not key:
        print("[ERROR] LSE_API_KEY resolves to empty. Refusing to pull.")
        return False
    try:
        r = await client.get(
            "/candles",
            params={"symbol": "BTC/USD", "timeframe": "1m", "limit": "1"},
        )
        if r.status_code != 200:
            print(f"[ERROR] live probe HTTP {r.status_code}: {r.text[:200]}")
            return False
        body = r.json()
        if not isinstance(body, list):
            print(f"[ERROR] live probe returned non-list JSON: {type(body).__name__}")
            return False
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[ERROR] live probe raised {type(e).__name__}: {e}")
        return False


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _normalize_candles(rows: list[dict]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
    frame = pd.DataFrame(rows)
    ts_col = "ts" if "ts" in frame.columns else "timestamp"
    out = frame[[ts_col, "open", "high", "low", "close", "volume"]].rename(columns={ts_col: "timestamp"})
    out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True, errors="coerce")
    for col in ("open", "high", "low", "close", "volume"):
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out = out.dropna(subset=["timestamp"]).drop_duplicates(subset="timestamp").sort_values("timestamp")
    out = out[(out["close"] > 0) & (out["high"] > 0) & (out["low"] > 0)]
    return out.reset_index(drop=True)


def _gap_report(df: pd.DataFrame, cadence: timedelta, ts_col: str = "timestamp") -> dict:
    """Count gaps strictly greater than cadence, plus the worst-case max gap."""
    if len(df) < 2:
        return {"gaps_over_cadence": 0, "max_gap_s": 0.0, "expected_rows": len(df)}
    diffs = df[ts_col].diff().dropna()
    over = diffs[diffs > cadence]
    return {
        "gaps_over_cadence": int(len(over)),
        "max_gap_s": float(diffs.max().total_seconds()) if not diffs.empty else 0.0,
        "expected_rows_at_cadence": int((df[ts_col].max() - df[ts_col].min()) / cadence) + 1,
    }


async def fetch_candles(client: httpx.AsyncClient) -> tuple[pd.DataFrame, dict]:
    """Forward-paginate /candles from the earliest available date to now.

    Avoids re-traversing if an existing cache is fresher / as-complete: returns
    only the freshly-fetched rows. Caller decides whether to overwrite.
    """
    # Earliest BTC/USD tick per the catalog is 2017-08-17 04:00:28 UTC.
    start_date = "2017-08-17"
    end_ceiling = datetime.now(UTC)
    all_dfs: list[pd.DataFrame] = []
    total_raw = 0
    batches = 0
    last_seen_start: str | None = None

    while True:
        batch = await client.get(
            "/candles",
            params={"symbol": "BTC/USD", "timeframe": "1m", "start": start_date, "limit": str(CANDLES_LIMIT)},
        )
        batch.raise_for_status()
        rows = batch.json()
        if not isinstance(rows, list):
            raise RuntimeError(f"/candles returned non-list payload at start={start_date}")
        norm = _normalize_candles(rows)
        total_raw += len(rows)
        if norm.empty:
            print(f"  [candles batch {batches}] empty/invalid at start={start_date}, stopping")
            break
        print(
            f"  [candles batch {batches}] raw={len(rows)} clean={len(norm)} "
            f"{norm['timestamp'].iloc[0]} → {norm['timestamp'].iloc[-1]}"
        )
        all_dfs.append(norm)
        last_ts = norm["timestamp"].iloc[-1]
        next_start = (last_ts + timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
        next_start_dt = pd.to_datetime(next_start, utc=True)
        if next_start_dt >= end_ceiling:
            print(f"  [candles] reached now ceiling {end_ceiling.isoformat()}")
            break
        # Guard against an API that echoes the same window back forever.
        if next_start == last_seen_start:
            print(f"  [candles] start did not advance (stuck at {start_date}); aborting")
            break
        last_seen_start = start_date
        start_date = next_start
        batches += 1
        if batches > CANDLES_MAX_BATCHES:
            print(f"  [candles] safety cap hit at {CANDLES_MAX_BATCHES} batches")
            break

    if not all_dfs:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"]), {
            "batches": batches, "raw_rows": total_raw,
        }

    df = pd.concat(all_dfs, ignore_index=True)
    before = len(df)
    df = df.drop_duplicates(subset="timestamp").sort_values("timestamp").reset_index(drop=True)
    return df, {"batches": batches + 1, "raw_rows": total_raw, "deduped_dropped": before - len(df)}


async def fetch_calendar(client: httpx.AsyncClient) -> tuple[pd.DataFrame, dict]:
    """Paginate /ref/economic_calendar monthly from 2015-01-01 to 2026-12-01.

    Reference metadata reports rows=137,229 spanning 2015-01-01 → 2026-11-10.
    Full-year windows hit the 5000-row cap, so we walk one month at a time and
    dedup by event `id` (numeric, ascending by date) on concatenation.
    """
    start = datetime(2015, 1, 1, tzinfo=UTC)
    end = datetime(2026, 12, 1, tzinfo=UTC)
    all_rows: list[dict] = []
    raw_total = 0
    months = 0
    cursor = start
    while cursor <= end:
        window_end = (cursor.replace(day=1) + timedelta(days=32)).replace(day=1)
        r = await client.get(
            "/ref/economic_calendar",
            params={
                "start": cursor.strftime("%Y-%m-%d"),
                "end": window_end.strftime("%Y-%m-%d"),
                "limit": str(CALENDAR_LIMIT),
            },
        )
        r.raise_for_status()
        rows = r.json()
        if not isinstance(rows, list):
            raise RuntimeError(f"/ref/economic_calendar returned non-list at {cursor.date()}")
        raw_total += len(rows)
        all_rows.extend(rows)
        print(f"  [calendar month {cursor.strftime('%Y-%m')}] raw={len(rows)} cumulative={raw_total}")
        cursor = window_end
        months += 1

    df = pd.DataFrame(all_rows)
    dedup_dropped = 0
    if not df.empty:
        before = len(df)
        df = df.drop_duplicates(subset=["id"]).sort_values("id").reset_index(drop=True)
        dedup_dropped = before - len(df)
    return df, {"months": months, "raw_rows": raw_total, "deduped_dropped": dedup_dropped}


async def _maybe_overwrite(
    new_df: pd.DataFrame, target: Path, writer: str, key_fp: str, endpoint: str, extra_meta: dict
) -> dict:
    """Write ``new_df`` to ``target`` only if it has >= rows than the existing artifact.

    ``writer`` is "parquet" or "csv". Always rewrites the meta sidecar from the
    freshly persisted file (so SHA-256 matches what's on disk).
    Returns the metadata dict (also written to the sidecar).
    """
    sidecar = target.parent / (target.stem + "_meta.json")
    if new_df.empty:
        print(f"  [skip] no rows fetched for {target.name}; preserving existing cache")
        return json.loads(sidecar.read_text()) if sidecar.exists() else {}

    target.parent.mkdir(parents=True, exist_ok=True)
    prev_rows = -1
    if target.exists():
        try:
            if target.suffix == ".parquet":
                prev_rows = len(pd.read_parquet(target, columns=[new_df.columns[0]]))
            else:
                prev_rows = sum(1 for _ in open(target)) - 1
        except Exception:
            prev_rows = -1

    if prev_rows > len(new_df):
        print(
            f"  [preserve] existing {target.name} has {prev_rows} rows; "
            f"fresh pull has {len(new_df)} — preserving existing cache"
        )
        write_target = False
    else:
        write_target = True

    if write_target:
        if target.suffix == ".parquet":
            new_df.to_parquet(target, index=False)
        else:
            new_df.to_csv(target, index=False)
        print(f"  [wrote] {target} ({len(new_df)} rows)")
    else:
        # Read back what's on disk so metadata reflects ACTUAL bytes.
        if target.suffix == ".parquet":
            new_df = pd.read_parquet(target)
        else:
            new_df = pd.read_csv(target)
        print(f"  [kept]  {target} ({len(new_df)} rows on disk)")

    digest = _sha256_file(target)
    size_bytes = target.stat().st_size
    meta = {
        "artifact": target.name,
        "source_endpoint": endpoint,
        "key_fingerprint": key_fp,
        "fetched_at_utc": _now_iso(),
        "fetched_at_epoch": time.time(),
        "row_count": int(len(new_df)),
        "file_size_bytes": int(size_bytes),
        "sha256": digest,
        **extra_meta,
    }
    # Add date-range + gap fields that source-specific callers should populate.
    meta.setdefault("schema_columns", list(new_df.columns))
    sidecar = target.parent / (target.stem + "_meta.json")
    sidecar.write_text(json.dumps(meta, indent=2, default=str))
    print(f"  [meta]  {sidecar}")
    return meta


async def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--candles-only", action="store_true")
    ap.add_argument("--calendar-only", action="store_true")
    args = ap.parse_args(argv)
    do_candles = not args.calendar_only
    do_calendar = not args.candles_only

    key = SETTINGS.lse_api_key
    print(f"[INFO] resolved LSE_API_KEY: {_key_fingerprint(key)}")

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    results: dict[str, Any] = {"started_at_utc": _now_iso(), "key_fingerprint": _key_fingerprint(key)}

    async with httpx.AsyncClient(
        base_url=LSE_VAULT_URL,
        timeout=60.0,
        headers={"x-api-key": key, "Content-Type": "application/json"},
    ) as client:
        if not await _verify_key_live(client, key):
            print("[ABORT] API key did not verify live — no cache writes performed.")
            return 2

        if do_candles:
            print("\n[FETCH] /candles BTC/USD 1m — forward paginate from 2017-08-17")
            df, fetch_meta = await fetch_candles(client)
            endpoint = f"{LSE_VAULT_URL}/candles?symbol=BTC/USD&timeframe=1m"
            extra = {
                "symbol": "BTC/USD",
                "timeframe": "1m",
                "native_cadence_s": 60,
                **fetch_meta,
            }
            if not df.empty:
                extra.update({
                    "date_range_start": df["timestamp"].iloc[0].isoformat(),
                    "date_range_end": df["timestamp"].iloc[-1].isoformat(),
                    **_gap_report(df, timedelta(minutes=1)),
                })
            results["candles"] = await _maybe_overwrite(df, CANDLES_FILE, "parquet", results["key_fingerprint"], endpoint, extra)

        if do_calendar:
            print("\n[FETCH] /ref/economic_calendar — monthly paginate 2015-01 → 2026-12")
            df, fetch_meta = await fetch_calendar(client)
            endpoint = f"{LSE_VAULT_URL}/ref/economic_calendar"
            extra = {
                "dataset": "economic_calendar",
                "native_cadence_s": 0,  # event-driven, no fixed cadence
                **fetch_meta,
            }
            if "date" in df.columns and not df.empty:
                dts = pd.to_datetime(df["date"], errors="coerce").dropna()
                extra.update({
                    "date_range_start": dts.min().date().isoformat(),
                    "date_range_end": dts.max().date().isoformat(),
                })
            results["calendar"] = await _maybe_overwrite(df, CALENDAR_FILE, "csv", results["key_fingerprint"], endpoint, extra)

    results["finished_at_utc"] = _now_iso()
    run_log = CACHE_DIR / "cache_run_log.json"
    run_log.write_text(json.dumps(results, indent=2, default=str))
    print(f"\n[DONE] run log -> {run_log}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
