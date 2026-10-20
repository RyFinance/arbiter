"""SkillOpt skill router — agent orchestration layer.

NOT in the core arb hot path. This is a supervisory agent layer that routes
natural language queries to the appropriate skill.

Skills:
  A) query_lse_data        -> calls lse_client.py endpoints
  B) run_kronos_forecast   -> calls kronos_session.py
  C) scan_polymarket       -> calls gamma_client.py
  D) read_econ_calendar    -> calls page-agent on LSE calendar page
  E) check_bot_health      -> reads state/bot.jsonl metrics

Usage:
    from polymarket_arb.agent.skill_router import SkillRouter
    router = SkillRouter()
    result = await router.route("Is BTC vol regime high right now?")
    # Routes to run_kronos_forecast

This module is only built after core gaps are closed (signing verified,
backtest engine working, Kronos integrated, page-agent running).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Callable, Tuple

from ..config import SETTINGS
from ..logger import log


# Skill definitions
SKILLS = {
    "query_lse_data": {
        "name": "Query LSE Data",
        "description": "Query London Strategic Edge market data (candles, ticks, macro series, options)",
        "keywords": ["lse", "london strategic", "candles", "ticks", "macro", "options", "btc/USD", "btcusd"],
        "handler": "query_lse_data",
    },
    "run_kronos_forecast": {
        "name": "Kronos Volatility Forecast",
        "description": "Forecast BTC volatility regime for the upcoming session",
        "keywords": ["vol", "volatility", "regime", "kronos", "forecast", "high vol", "low vol", "session"],
        "handler": "run_kronos_forecast",
    },
    "scan_polymarket": {
        "name": "Scan Polymarket Markets",
        "description": "Scan Polymarket for current markets, window opportunities, and arb conditions",
        "keywords": ["polymarket", "markets", "scan", "window", "arb", "updown", "btc 5m", "opportunity"],
        "handler": "scan_polymarket",
    },
    "read_econ_calendar": {
        "name": "Read Economic Calendar",
        "description": "Check LSE economic calendar for upcoming macro events",
        "keywords": ["calendar", "events", "macro", "economic", "fed", "cpi", "gdp", "jobs", "nfp"],
        "handler": "read_econ_calendar",
    },
    "check_bot_health": {
        "name": "Check Bot Health",
        "description": "Check bot metrics, PnL, and health from state/bot.jsonl",
        "keywords": ["health", "metrics", "pnl", "profit", "loss", "stats", "performance", "bot status"],
        "handler": "check_bot_health",
    },
}


@dataclass
class SkillResult:
    """Result from a skill execution."""
    skill: str
    success: bool
    data: Any = None
    error: Optional[str] = None
    execution_time_s: float = 0.0

    def to_dict(self) -> dict:
        return {
            "skill": self.skill,
            "success": self.success,
            "data": self.data,
            "error": self.error,
            "execution_time_s": round(self.execution_time_s, 3),
        }


class SkillRouter:
    """Routes natural language queries to the appropriate skill.

    Uses keyword matching to determine which skill to invoke.
    Can be extended with more sophisticated NLP routing.
    """

    def __init__(self):
        self.skills = SKILLS
        self._handlers: Dict[str, Callable] = {}

    def _match_skill(self, query: str) -> Optional[str]:
        """Match a query to a skill using keyword matching.

        Returns the skill key, or None if no match.
        """
        query_lower = query.lower()

        # Try exact keyword matches first
        best_match = None
        best_score = 0

        for skill_key, skill_def in self.skills.items():
            score = 0
            for keyword in skill_def["keywords"]:
                if keyword in query_lower:
                    score += 1
            if score > best_score:
                best_score = score
                best_match = skill_key

        return best_match if best_score > 0 else None

    async def route(self, query: str) -> SkillResult:
        """Route a query to the appropriate skill and execute it.

        Args:
            query: Natural language query from the user.

        Returns:
            SkillResult with the skill's output.
        """
        start_time = datetime.now(timezone.utc)

        skill_key = self._match_skill(query)
        if skill_key is None:
            return SkillResult(
                skill="none",
                success=False,
                error=f"No skill matched query: {query}",
                execution_time_s=0.0,
            )

        skill_def = self.skills[skill_key]
        log.info(f"Routing query to skill: {skill_def['name']}")

        handler = getattr(self, skill_def["handler"], None)
        if handler is None:
            return SkillResult(
                skill=skill_key,
                success=False,
                error=f"Handler not found for skill: {skill_key}",
                execution_time_s=0.0,
            )

        try:
            data = await handler(query)
            elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
            return SkillResult(
                skill=skill_key,
                success=True,
                data=data,
                execution_time_s=elapsed,
            )
        except Exception as e:
            elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
            log.error(f"Skill {skill_key} failed: {e!r}")
            return SkillResult(
                skill=skill_key,
                success=False,
                error=str(e),
                execution_time_s=elapsed,
            )

    # ---- Skill Handlers ----

    async def query_lse_data(self, query: str) -> Dict[str, Any]:
        """Query LSE market data based on the query."""
        from ..clients.lse_client import LSEClient

        lse = LSEClient()
        results = {}

        query_lower = query.lower()

        if "candle" in query_lower or "ohlcv" in query_lower:
            results["candles"] = await lse.fetch_btc_candles_for_kronos(days=30)

        if "macro" in query_lower or "us10y" in query_lower or "yield" in query_lower:
            results["macro"] = await lse.fetch_series("US10Y", start="2026-01-01", limit=100)

        if "options" in query_lower or "iv" in query_lower:
            results["options"] = await lse.fetch_options_chain(underlying="BTC", max_dte=30)

        if not results:
            # Default: return recent candles
            results["candles"] = await lse.fetch_btc_candles_for_kronos(days=7)

        await lse.aclose()
        return results

    async def run_kronos_forecast(self, query: str) -> Dict[str, Any]:
        """Run Kronos volatility regime forecast."""
        from ..kronos_session import forecast_session_regime

        regime = await forecast_session_regime()
        return regime.to_log()

    async def scan_polymarket(self, query: str) -> Dict[str, Any]:
        """Scan Polymarket for current markets and opportunities."""
        from ..scan import scan_once

        # Run the scan
        await scan_once(
            asset=SETTINGS.window_asset,
            timeframe=SETTINGS.window_timeframe,
            window_seconds=SETTINGS.window_seconds,
        )

        return {
            "asset": SETTINGS.window_asset,
            "timeframe": SETTINGS.window_timeframe,
            "min_edge": SETTINGS.min_edge,
            "window_mode": SETTINGS.window_mode,
        }

    async def read_econ_calendar(self, query: str) -> Dict[str, Any]:
        """Read economic calendar for upcoming events."""
        # Load macro events from page-agent
        macro_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "state", "macro_events.json"
        )

        events = []
        if os.path.exists(macro_path):
            with open(macro_path, "r") as f:
                data = json.load(f)
            events = data.get("events", [])

        # Filter to upcoming events
        now = datetime.now(timezone.utc).timestamp()
        upcoming = [e for e in events if e.get("timestamp", 0) > now]

        return {
            "upcoming_events": upcoming,
            "total_events": len(events),
            "high_risk_windows": data.get("high_risk_windows", []),
        }

    async def check_bot_health(self, query: str) -> Dict[str, Any]:
        """Check bot health from state/bot.jsonl."""
        bot_jsonl = SETTINGS.log_jsonl

        if not os.path.exists(bot_jsonl):
            return {"error": "No bot.jsonl found", "health": "unknown"}

        # Read last 100 lines
        lines = []
        try:
            with open(bot_jsonl, "r") as f:
                lines = f.readlines()[-100:]
        except Exception as e:
            return {"error": str(e), "health": "unknown"}

        # Parse and summarize
        trades = []
        errors = 0
        fills = 0
        missed = 0

        for line in lines:
            try:
                entry = json.loads(line.strip())
                if entry.get("event") == "TRADE":
                    outcome = entry.get("msg", "")
                    if outcome == "filled":
                        fills += 1
                    elif outcome == "missed":
                        missed += 1
                elif entry.get("event") == "ERROR" or entry.get("event") == "CRITICAL":
                    errors += 1
            except (json.JSONDecodeError, KeyError):
                continue

        health = "healthy"
        if errors > 10:
            health = "degraded"
        if errors > 50:
            health = "critical"

        return {
            "health": health,
            "recent_trades": len(lines),
            "fills": fills,
            "missed": missed,
            "errors": errors,
            "last_update": lines[-1].strip() if lines else None,
        }


async def main():
    """Interactive skill router CLI."""
    router = SkillRouter()

    print("Polymarket Arb Bot - Skill Router")
    print("=" * 50)
    print("Available skills:")
    for key, skill in SKILLS.items():
        print(f"  {key}: {skill['name']}")
    print()
    print("Type 'quit' or 'exit' to stop.")
    print()

    while True:
        try:
            query = input("You: ").strip()
            if query.lower() in ("quit", "exit", "q"):
                break
            if not query:
                continue

            result = await router.route(query)
            print(f"\n[{result.skill}] (success={result.success}, {result.execution_time_s:.2f}s)")
            if result.success:
                print(json.dumps(result.data, indent=2, default=str))
            else:
                print(f"Error: {result.error}")
            print()
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"Error: {e!r}")


if __name__ == "__main__":
    asyncio.run(main())
