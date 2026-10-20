"""Regime backtest — replay historical sessions and annotate fe/miss events.

Reads a session's bot.jsonl (or an archive directory) and, for every TRADE event
(filled, missed, partial_resolved), looks up the BTC volatility regime that was
active at that timestamp using historical candle data from parquet files.

Reports:
  - Per-regime summary: how many fe/miss events occurred in each regime
  - Regime breakdown of missed events by reason (stale_quote, no_fill, etc.)
  - Regime breakdown of filled events by PnL outcome

Usage:
    python scripts/regime_backtest.py
    python scripts/regime_backtest.py state/paper_session_archives/
    python scripts/regime_backtest.py --date 2026-08-05
    python scripts/regime_backtest.py --candles state/btc_1m_candles_lse_v3.parquet
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Reuse the proven ATR + regime classification from kronos_session.
from polymarket_arb.kronos_session import (
    _compute_atr,
    _normalize_atr_to_pct,
    _regime_from_atr,
    _scale_atr_to_daily,
)

try:
    import pandas as pd
    _HAS_PANDAS = True
except ImportError:
    _HAS_PANDAS = False


def _load_candles_from_parquet(path: Path) -> list[dict]:
    """Load BTC/USD 1-minute candles from a parquet file.

    Returns a list of {timestamp, open, high, low, close, volume} dicts sorted
    by timestamp ascending.
    """
    if not _HAS_PANDAS or not path.exists():
        return []
    df = pd.read_parquet(path)
    candles = []
    for _, row in df.iterrows():
        ts = row.get("timestamp") or row.get("ts") or row.get("time")
        if ts is None:
            continue
        if isinstance(ts, (int, float)):
            epoch = float(ts)
        else:
            try:
                dt = datetime.fromisoformat(str(ts))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                epoch = dt.timestamp()
            except (ValueError, TypeError):
                continue
        candles.append({
            "timestamp": epoch,
            "open": float(row.get("open", 0)),
            "high": float(row.get("high", 0)),
            "low": float(row.get("low", 0)),
            "close": float(row.get("close", 0)),
            "volume": float(row.get("volume", 0)),
        })
    candles.sort(key=lambda c: c["timestamp"])
    return candles


def _load_candles_from_jsonl(path: Path) -> list[dict]:
    """Load candles from a JSONL file (one candle per line)."""
    candles = []
    if not path.exists():
        return []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = rec.get("timestamp") or rec.get("ts")
            if ts is None:
                continue
            if isinstance(ts, (int, float)):
                epoch = float(ts)
            else:
                try:
                    dt = datetime.fromisoformat(str(ts))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    epoch = dt.timestamp()
                except (ValueError, TypeError):
                    continue
            candles.append({
                "timestamp": epoch,
                "open": float(rec.get("open", 0)),
                "high": float(rec.get("high", 0)),
                "low": float(rec.get("low", 0)),
                "close": float(rec.get("close", 0)),
                "volume": float(rec.get("volume", 0)),
            })
    candles.sort(key=lambda c: c["timestamp"])
    return candles


def _classify_regime_at(candles: list[dict], epoch: float, period: int = 14) -> str:
    """Classify the regime active at ``epoch`` using the ATR heuristic.

    Returns "LOW" | "MED" | "HIGH" | "UNKNOWN" (if insufficient data).
    """
    if not candles:
        return "UNKNOWN"

    recent = [c for c in candles if c["timestamp"] <= epoch]
    if len(recent) < period + 1:
        return "UNKNOWN"

    window = recent[-(period + 1):]
    atr = _compute_atr(window, period=period)
    atr_pct = _normalize_atr_to_pct(atr, window)
    atr_scaled = _scale_atr_to_daily(atr_pct)
    regime, _ = _regime_from_atr(atr_scaled)
    return regime


def _parse_trade_events(bot_jsonl: Path) -> list[dict]:
    """Parse all TRADE events from a bot.jsonl file.

    Returns a list of dicts with keys: t, msg, data, epoch.
    """
    events = []
    if not bot_jsonl.exists():
        return events
    with open(bot_jsonl) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("event") != "TRADE":
                continue
            t = rec.get("t", "")
            epoch = 0.0
            try:
                dt = datetime.fromisoformat(t)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                epoch = dt.timestamp()
            except (ValueError, TypeError):
                pass
            events.append({
                "t": t,
                "msg": rec.get("msg", ""),
                "data": rec.get("data", {}),
                "epoch": epoch,
            })
    return events


def _archive_date_to_epoch(date_str: str) -> float:
    """Convert YYYY-MM-DD to epoch seconds (midnight UTC)."""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return 0.0


def _archive_date_from_path(path: Path) -> str | None:
    """Extract YYYY-MM-DD from an archive directory name.

    Archive dirs: execution_stress_20260805T211659Z -> 2026-08-05
    """
    for part in path.parts:
        if part.startswith("execution_stress_") or part.startswith("paper_session_"):
            suffix = part.split("_", 2)[-1]
            date_str = suffix[:8]
            if len(date_str) == 8 and date_str.isdigit():
                y, m, d = date_str[:4], date_str[4:6], date_str[6:8]
                return f"{y}-{m}-{d}"
    return None


def _resolve_trade_epoch(event: dict, archive_date: str | None = None) -> float:
    """Resolve a TRADE event's timestamp to epoch seconds.

    bot.jsonl uses session-relative timestamps like "14:16:59.726" with no date.
    We use the archive directory name for the date context, then combine with
    the session-relative time.
    """
    if event["epoch"] > 0:
        return event["epoch"]
    if not archive_date:
        return 0.0
    date_epoch = _archive_date_to_epoch(archive_date)
    t = event["t"]
    try:
        dt = datetime.strptime(t, "%H:%M:%S.%f")
        return date_epoch + dt.hour * 3600 + dt.minute * 60 + dt.second + dt.microsecond / 1e6
    except ValueError:
        return date_epoch


def collect_trade_events(jsonl_path: Path, date_filter: str | None = None) -> list[dict]:
    """Parse bot.jsonl files, optionally from an archive directory.

    Returns events annotated with 'archive_date'.
    """
    events = []
    if jsonl_path.is_dir():
        for root, _dirs, files in os.walk(jsonl_path):
            if "bot.jsonl" in files:
                bot_path = Path(root) / "bot.jsonl"
                arch_date = _archive_date_from_path(bot_path)
                if date_filter and arch_date != date_filter:
                    continue
                parsed = _parse_trade_events(bot_path)
                for ev in parsed:
                    ev["archive_date"] = arch_date
                    events.append(ev)
    else:
        parsed = _parse_trade_events(jsonl_path)
        arch_date = _archive_date_from_path(jsonl_path.parent)
        for ev in parsed:
            ev["archive_date"] = arch_date
        events.extend(parsed)
    return events


def build_regime_report(events: list[dict], candles: list[dict] | None = None) -> dict:
    """Build a regime-annotated report from trade events and candle data."""
    if candles is None:
        candles = []

    regime_stats: dict[str, dict] = defaultdict(lambda: {
        "total": 0, "filled": 0, "missed": 0,
        "missed_by_reason": defaultdict(int),
        "pnl_locked_sum": 0.0,
        "pnl_residual_sum": 0.0,
    })

    annotated_events = []
    for ev in events:
        epoch = _resolve_trade_epoch(ev, ev.get("archive_date"))
        regime = _classify_regime_at(candles, epoch) if epoch > 0 and candles else "UNKNOWN"
        msg = ev["msg"]
        data = ev["data"]

        stats = regime_stats[regime]
        stats["total"] += 1

        if msg == "filled":
            stats["filled"] += 1
            stats["pnl_locked_sum"] += float(data.get("pnl_locked", 0))
        elif msg == "missed":
            stats["missed"] += 1
            stats["missed_by_reason"][data.get("reason", "unknown")] += 1
        elif msg == "partial_resolved":
            stats["filled"] += 1
            stats["pnl_residual_sum"] += float(data.get("residual_pnl", 0) or data.get("pnl_locked", 0))

        annotated_events.append({
            "t": ev["t"],
            "msg": msg,
            "regime": regime,
            "data": data,
        })

    for regime, stats in regime_stats.items():
        stats["missed_by_reason"] = dict(stats["missed_by_reason"])

    return {
        "total_events": len(events),
        "candle_count": len(candles),
        "regime_stats": dict(regime_stats),
        "annotated_events": annotated_events[:100],
    }


def main(argv: list[str] | None = None) -> int:
    argv = argv or sys.argv[1:]
    parser = argparse.ArgumentParser(
        description="Regime backtest: annotate fe/miss events with BTC volatility regime."
    )
    parser.add_argument("path", nargs="?", default="state/bot.jsonl",
                        help="Path to bot.jsonl or archive directory")
    parser.add_argument("--candles", default=None,
                        help="Path to BTC candles (parquet or jsonl)")
    parser.add_argument("--date", default=None,
                        help="Filter to archives dated YYYY-MM-DD")
    parser.add_argument("--output", "-o", default=None,
                        help="Write JSON report to this path")
    args = parser.parse_args(argv)

    jsonl_path = Path(args.path)
    if not jsonl_path.exists():
        print(f"ERROR: path not found: {jsonl_path}", file=sys.stderr)
        return 1

    candles = []
    if args.candles:
        candle_path = Path(args.candles)
        if candle_path.suffix == ".parquet":
            candles = _load_candles_from_parquet(candle_path)
        else:
            candles = _load_candles_from_jsonl(candle_path)
    else:
        for name in ["btc_1m_candles_lse_v3.parquet", "btc_1m_candles_lse.parquet",
                     "btc_1m_candles.parquet"]:
            p = PROJECT_ROOT / "state" / name
            if p.exists():
                candles = _load_candles_from_parquet(p)
                if candles:
                    print(f"Loaded {len(candles)} candles from {p.name}", file=sys.stderr)
                    break

    if not candles:
        print("WARNING: No candle data available — all events will be UNKNOWN regime.",
              file=sys.stderr)

    events = collect_trade_events(jsonl_path, args.date)
    if not events:
        print(f"No TRADE events found in {jsonl_path}", file=sys.stderr)
        return 1

    print(f"Found {len(events)} TRADE events", file=sys.stderr)
    if candles:
        print(f"  {len(candles)} BTC candles available for regime classification", file=sys.stderr)

    report = build_regime_report(events, candles)

    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, indent=2, default=str))
        print(f"Report written to {out_path}", file=sys.stderr)
    else:
        print(json.dumps(report, indent=2, default=str))

    print(f"\n{'='*60}", file=sys.stderr)
    print(f"Regime Backtest Report", file=sys.stderr)
    print(f"{'='*60}", file=sys.stderr)
    print(f"  Total TRADE events: {report['total_events']}", file=sys.stderr)
    print(f"  Candle data points: {report['candle_count']}", file=sys.stderr)
    print(f"\n  Per-regime summary:", file=sys.stderr)
    for regime in ["HIGH", "MED", "LOW", "UNKNOWN"]:
        stats = report["regime_stats"].get(regime)
        if not stats:
            continue
        print(f"    {regime:>8s}: total={stats['total']} filled={stats['filled']} "
              f"missed={stats['missed']} "
              f"locked_pnl={stats['pnl_locked_sum']:.4f} "
              f"residual_pnl={stats['pnl_residual_sum']:.4f}", file=sys.stderr)
        if stats["missed_by_reason"]:
            print(f"             missed reasons:", file=sys.stderr)
            for reason, count in sorted(stats["missed_by_reason"].items(),
                                         key=lambda x: -x[1]):
                print(f"               {reason}: {count}", file=sys.stderr)
    print(f"{'='*60}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
