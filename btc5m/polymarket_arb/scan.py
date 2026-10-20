"""One-shot BTC 5-minute window scanner — no trading, just live book inspection.

Usage:
    python -m polymarket_arb.scan
    python -m polymarket_arb.scan --watch
    python -m polymarket_arb.scan --asset eth --timeframe 15m
"""
from __future__ import annotations

import argparse
import asyncio
import time
from dataclasses import dataclass
from typing import Optional

import httpx

from .clients.clob_client import fetch_book
from .clients.gamma_client import (
    current_window_ts,
    next_window_ts,
    resolve_window_market,
    window_slug,
)
from .config import SETTINGS


@dataclass
class WindowQuote:
    """Single BTC ETH-or-SOL 5m/15m Polymarket “up/down?window” market book snapshot.

    Carries both legs (up + down), their current best asks + sizes, and the
    deterministic rolling-window start/close epochs. ``combined_ask`` and ``edge``
    derived properties mirror the gate math in ``ArbEngine`` so the scanner prints
    exactly what the decision logic is judging.
    """
    slug: str
    question: str
    up_ask: Optional[float]
    up_size: Optional[float]
    down_ask: Optional[float]
    down_size: Optional[float]
    window_start: int
    window_close: int
    accepting: bool = True

    @property
    def combined_ask(self) -> Optional[float]:
        if self.up_ask is None or self.down_ask is None:
            return None
        return self.up_ask + self.down_ask

    @property
    def edge(self) -> Optional[float]:
        c = self.combined_ask
        if c is None:
            return None
        return 1.0 - c

    @property
    def arbable(self) -> bool:
        c = self.combined_ask
        if c is None:
            return False
        return c < 1.0 - SETTINGS.min_edge


async def fetch_window_quote(
    gamma: httpx.AsyncClient,
    clob: httpx.AsyncClient,
    asset: str,
    timeframe: str,
    window_seconds: int,
    window_ts: int,
) -> Optional[WindowQuote]:
    """Resolve one rolling-window market and pull both legs' best asks for the scanner.

    Returns ``None`` if Gamma has no market for ``slug`` yet (window not opened);
    never raises on a missing side — bad/missing legs surface as ``None`` asks in
    the ``WindowQuote``, which ``combined_ask`` and ``edge`` propagate as None.
    """
    slug = window_slug(asset, timeframe, window_ts)
    meta = await resolve_window_market(gamma, slug)
    if meta is None:
        return None
    up, up_sz = await fetch_book(clob, meta.yes_token_id)
    down, down_sz = await fetch_book(clob, meta.no_token_id)
    return WindowQuote(
        slug=slug,
        question=meta.question,
        up_ask=up,
        up_size=up_sz,
        down_ask=down,
        down_size=down_sz,
        window_start=window_ts,
        window_close=window_ts + window_seconds,
    )


def _fmt_price(p: Optional[float]) -> str:
    return f"{p:.3f}" if p is not None else "—"


def _fmt_size(s: Optional[float]) -> str:
    return f"{s:.1f}" if s is not None else "—"


def print_quote(q: WindowQuote):
    """Pretty-print one window quote to stdout for the scanner CLI."""
    secs_left = max(0.0, q.window_close - time.time())
    combined = q.combined_ask
    edge = q.edge
    print(f"\n{q.question}")
    print(f"  slug:     {q.slug}")
    print(f"  closes:   {secs_left:.0f}s")
    print(f"  Up ask:   {_fmt_price(q.up_ask)}  ({_fmt_size(q.up_size)} shares)")
    print(f"  Down ask: {_fmt_price(q.down_ask)}  ({_fmt_size(q.down_size)} shares)")
    if combined is not None:
        print(f"  Combined: {combined:.4f}  edge: {edge:.4f}  arb: {'YES' if q.arbable else 'no'}")
    else:
        print("  Combined: — (missing ask on one or both legs — not executable)")


async def scan_once(asset: str, timeframe: str, window_seconds: int) -> None:
    """One-shot: fetch + print the current and next window quotes (no trading)."""
    now = time.time()
    cur = current_window_ts(now, window_seconds)
    nxt = next_window_ts(cur, window_seconds)

    async with httpx.AsyncClient(base_url=SETTINGS.gamma_base_url, timeout=15.0) as gamma:
        async with httpx.AsyncClient(base_url=SETTINGS.clob_base_url, timeout=5.0) as clob:
            current = await fetch_window_quote(gamma, clob, asset, timeframe, window_seconds, cur)
            upcoming = await fetch_window_quote(gamma, clob, asset, timeframe, window_seconds, nxt)

    print(f"Polymarket {asset.upper()} {timeframe} Up/Down — YES+NO bundle arb scan")
    print(f"Threshold: combined ask < {1.0 - SETTINGS.min_edge:.2f} (MIN_EDGE={SETTINGS.min_edge})")

    if current:
        print("\n--- Current window ---")
        print_quote(current)
    else:
        print(f"\nNo market published yet for current window ({window_slug(asset, timeframe, cur)})")

    if upcoming:
        print("\n--- Next window ---")
        print_quote(upcoming)
    else:
        print(f"\nNext window not published yet ({window_slug(asset, timeframe, nxt)})")


async def watch(asset: str, timeframe: str, window_seconds: int, interval: float) -> None:
    """Continuous poll: refresh + print the live window every ``interval`` seconds.

    Detects window rotation (new slug) and prints a banner; emits one line per
    sample with combined ask, edge, and an ``*** ARB ***`` flag when the edge
    would clear MIN_EDGE. Intended for operator eyeballs, never for trading.
    """
    last_slug = ""
    while True:
        now = time.time()
        cur = current_window_ts(now, window_seconds)
        slug = window_slug(asset, timeframe, cur)
        if slug != last_slug:
            print(f"\n{'=' * 60}\nWindow rotated -> {slug}\n{'=' * 60}")
            last_slug = slug
        async with httpx.AsyncClient(base_url=SETTINGS.gamma_base_url, timeout=15.0) as gamma:
            async with httpx.AsyncClient(base_url=SETTINGS.clob_base_url, timeout=5.0) as clob:
                q = await fetch_window_quote(gamma, clob, asset, timeframe, window_seconds, cur)
        if q:
            combined = q.combined_ask
            flag = " *** ARB ***" if q.arbable else ""
            if combined is not None:
                print(
                    f"[{time.strftime('%H:%M:%S')}] "
                    f"Up={_fmt_price(q.up_ask)} Down={_fmt_price(q.down_ask)} "
                    f"SUM={combined:.4f} edge={q.edge:.4f}{flag}"
                )
            else:
                print(f"[{time.strftime('%H:%M:%S')}] incomplete book (one side missing ask)")
        await asyncio.sleep(interval)


def main():
    """``polymarket-scan`` CLI: parse args and run ``scan_once`` or ``watch``."""
    p = argparse.ArgumentParser(description="Scan Polymarket BTC 5m Up/Down for bundle arb")
    p.add_argument("--asset", default=SETTINGS.window_asset, help="Asset slug (default: btc)")
    p.add_argument("--timeframe", default=SETTINGS.window_timeframe, help="Window tag (default: 5m)")
    p.add_argument("--window-seconds", type=int, default=SETTINGS.window_seconds, help="Window length in seconds")
    p.add_argument("--watch", action="store_true", help="Poll continuously")
    p.add_argument("--interval", type=float, default=1.0, help="Poll interval when --watch (seconds)")
    args = p.parse_args()

    if args.watch:
        asyncio.run(watch(args.asset, args.timeframe, args.window_seconds, args.interval))
    else:
        asyncio.run(scan_once(args.asset, args.timeframe, args.window_seconds))


if __name__ == "__main__":
    main()
