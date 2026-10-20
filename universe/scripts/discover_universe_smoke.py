#!/usr/bin/env python3
"""Real-data smoke check for Phase 1: run universe discovery + qualification
against the live Gamma API and report actual counts.

Not a pytest test (deliberately -- it depends on live market conditions that
change every run, so it isn't something that should gate CI green/red). Run it
by hand: `.venv/bin/python scripts/discover_universe_smoke.py`
"""
from __future__ import annotations

import asyncio
import sys
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import httpx

from polyuni.config import SETTINGS
from polyuni.market_universe import MarketUniverse, qualify_market


async def main() -> int:
    universe = MarketUniverse(SETTINGS)
    async with httpx.AsyncClient(base_url=SETTINGS.gamma_base_url) as session:
        result = await universe.sync(session)

    print(f"Gamma discovery returned {result.discovered} binary markets")
    print(f"Newly qualified this pass: {len(result.newly_qualified)}")
    print(f"Promoted to ACTIVE (capacity {SETTINGS.max_concurrent_markets}): {len(result.promoted_to_active)}")
    print(f"State counts: {dict(result.state_counts)}")

    # Reasons a market failed qualification, for a sense of what the filter is
    # actually screening out (liquidity vs volume vs spread vs neg_risk etc.).
    reasons = Counter()
    for meta in universe.markets.values():
        passes, reason = qualify_market(meta, SETTINGS)
        if not passes:
            reasons[reason.split("_")[0] if "_" in reason else reason] += 1
    print(f"Disqualification reasons (this pass, coarse bucket): {dict(reasons.most_common())}")

    print("\nTop 10 ACTIVE markets by 24h volume:")
    active = sorted(universe.active_markets(), key=lambda m: m.volume_24h_usd, reverse=True)
    for m in active[:10]:
        print(
            f"  {m.market_id:>8}  vol24h=${m.volume_24h_usd:>12,.0f}  "
            f"liq=${m.liquidity_usd:>10,.0f}  spread={m.spread_at_discovery}  {m.question[:60]}"
        )

    if result.discovered == 0:
        print("FAIL: discovery returned zero markets -- check network/Gamma API", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
