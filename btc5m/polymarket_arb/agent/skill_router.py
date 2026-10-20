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
"""
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Callable

from ..config import SETTINGS
from ..logger import log


SKILLS = {
    "query_lse_data": {
        "name": "Query LSE Data",
        "description": "Query London Strategic Edge market data",
        "keywords": ["lse", "london strategic", "candles", "ticks", "macro", "options"],
        "handler": "query_lse_data",
    },
    "run_kronos_forecast": {
        "name": "Kronos Volatility Forecast",
        "description": "Forecast BTC volatility regime",
        "keywords": ["vol", "volatility", "regime", "kronos", "forecast", "session"],
        "handler": "run_kronos_forecast",
    },
    "scan_polymarket": {
        "name": "Scan Polymarket Markets",
        "description": "Scan Polymarket for current markets and opportunities",
        "keywords": ["polymarket", "markets", "scan", "window", "arb", "updown"],
        "handler": "scan_polymarket",
    },
    "read_econ_calendar": {
        "name": "Read Economic Calendar",
        "description": "Check LSE economic calendar for macro events",
        "keywords": ["calendar", "events", "macro", "economic", "fed", "cpi", "gdp"],
        "handler": "read_econ_calendar",
    },
    "check_bot_health": {
        "name": "Check Bot Health",
        "description": "Check bot metrics, PnL, and health",
        "keywords": ["health", "metrics", "pnl", "profit", "loss", "stats", "performance"],
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


class SkillRouter:
    """Routes natural language queries to the appropriate skill."""

    def __init__(self):
        self.skills = SKILLS

    def _match_skill(self, query: str) -> Optional[str]:
        """Match a query to a skill using keyword matching."""
        query_lower = query.lower()
        best_match = None
        best_score = 0
        for skill_key, skill_def in self.skills.items():
            score = sum(1 for kw in skill_def["keywords"] if kw in query_lower)
            if score > best_score:
                best_score = score
                best_match = skill_key
        return best_match if best_score > 0 else None

    async def route(self, query: str) -> SkillResult:
        """Route a query to the appropriate skill and execute it."""
        start_time = datetime.now(timezone.utc)
        skill_key = self._match_skill(query)
        if skill_key is None:
            return SkillResult(skill="none", success=False, error=f"No skill matched: {query}")
        skill_def = self.skills[skill_key]
        handler = getattr(self, skill_def["handler"], None)
        if handler is None:
            return SkillResult(skill=skill_key, success=False, error=f"Handler not found: {skill_key}")
        try:
            data = await handler(query)
            elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
            return SkillResult(skill=skill_key, success=True, data=data, execution_time_s=elapsed)
        except Exception as e:
            log.error(f"Skill {skill_key} failed: {e!r}")
            return SkillResult(skill=skill_key, success=False, error=str(e))

    async def query_lse_data(self, query: str) -> Dict[str, Any]:
        """Query LSE market data."""
        from ..clients.lse_client import LSEClient
        lse = LSEClient()
        results = {}
        query_lower = query.lower()
        if "candle" in query_lower or "ohlcv" in query_lower:
            results["candles"] = await lse.fetch_btc_candles_for_kronos(days=30)
        if "macro" in query_lower or "us10y" in query_lower:
            results["macro"] = await lse.fetch_series("US10Y", start="2026-01-01", limit=100)
        if "options" in query_lower or "iv" in query_lower:
            results["options"] = await lse.fetch_options_chain(underlying="BTC", max_dte=30)
        if not results:
            results["candles"] = await lse.fetch_btc_candles_for_kronos(days=7)
        await lse.aclose()
        return results

    async def run_kronos_forecast(self, query: str) -> Dict[str, Any]:
        """Run Kronos volatility regime forecast."""
        from ..kronos_session import forecast_session_regime
        regime = await forecast_session_regime()
        return regime.to_log()

    async def scan_polymarket(self, query: str) -> Dict[str, Any]:
        """Scan Polymarket for current markets."""
        from ..scan import scan_once
        await scan_once(asset=SETTINGS.window_asset, timeframe=SETTINGS.window_timeframe,
                        window_seconds=SETTINGS.window_seconds)
        return {"asset": SETTINGS.window_asset, "timeframe": SETTINGS.window_timeframe,
                "min_edge": SETTINGS.min_edge, "window_mode": SETTINGS.window_mode}

    async def read_econ_calendar(self, query: str) -> Dict[str, Any]:
        """Read economic calendar for upcoming events."""
        macro_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                                  "state", "macro_events.json")
        events = []
        if os.path.exists(macro_path):
            with open(macro_path, "r") as f:
                data = json.load(f)
            events = data.get("events", [])
        now = datetime.now(timezone.utc).timestamp()
        upcoming = [e for e in events if e.get("timestamp", 0) > now]
        return {"upcoming_events": upcoming, "total_events": len(events)}

    async def check_bot_health(self, query: str) -> Dict[str, Any]:
        """Check bot health from state/bot.jsonl."""
        bot_jsonl = SETTINGS.log_jsonl
        if not os.path.exists(bot_jsonl):
            return {"error": "No bot.jsonl found", "health": "unknown"}
        lines = []
        try:
            with open(bot_jsonl, "r") as f:
                lines = f.readlines()[-100:]
        except Exception as e:
            return {"error": str(e), "health": "unknown"}
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
                elif entry.get("event") in ("ERROR", "CRITICAL"):
                    errors += 1
            except (json.JSONDecodeError, KeyError):
                continue
        health = "healthy"
        if errors > 10:
            health = "degraded"
        if errors > 50:
            health = "critical"
        return {"health": health, "recent_lines": len(lines), "fills": fills,
                "missed": missed, "errors": errors}


async def main():
    """Interactive skill router CLI."""
    router = SkillRouter()
    print("Polymarket Arb Bot - Skill Router")
    print("Available skills:", list(SKILLS.keys()))
    print("Type 'quit' to exit.")
    while True:
        try:
            query = input("You: ").strip()
            if query.lower() in ("quit", "exit", "q"):
                break
            if not query:
                continue
            result = await router.route(query)
            print(f"[{result.skill}] success={result.success}")
            if result.success:
                print(json.dumps(result.data, indent=2, default=str))
            else:
                print(f"Error: {result.error}")
        except (KeyboardInterrupt, EOFError):
            break


if __name__ == "__main__":
    asyncio.run(main())
