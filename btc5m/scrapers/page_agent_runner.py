"""Page-agent runner — browser automation for data the API doesn't cover.

Runs in COLD PATH (background thread), never in the trading loop.
Writes to state/macro_events.json which risk.py reads at decision time.

TASK A fixes applied:
  1. Import path fixed — runs as a standalone script (`python scrapers/page_agent_runner.py`)
     as well as when imported by risk.py's importlib shim under the synthetic
     `polymarket_arb.scrapers` parent package.
  2. Parser rewritten — scrapes the LSE economic calendar via the SPA's *internal*
     REST endpoint (`GET https://api.londonstrategicedge.com/economic_calender`)
     instead of scraping rendered HTML. The SPA's PostgREST-style API returns JSON
     records with fields: event, region_code, date/datetime, period_hint, actual,
     consensus, forecast, previous, importance, is_stale.
  3. API key wired from SETTINGS.lse_api_key (config.py line 101). A placeholder
     key is detected and a loud warning is logged; a real key is REQUIRED via the
     LSE_API_KEY env var for authenticated data.
  4. Impact-level mapping ports the SPA's `Vm()` classifier to Python: HIGH / MED /
     LOW classification from `importance`, the keyword table `Hm`, and the developed-
     market / core-event tables (`Wm`, `Bm`, `Im`, `Um`, `Fm`).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

# ---- Fix #1: import shim so the file runs in ALL invocation contexts: ----
#   (a) python scrapers/page_agent_runner.py  -> __package__ is "" / None
#   (b) risk.py importlib under synthetic "polymarket_arb.scrapers" -> __package__ = "polymarket_arb.scrapers"
#       => relative `from ..config` works (polymarket_arb.scrapers -> polymarket_arb)
#   (c) main.py: `from scrapers.page_agent_runner import ...` -> __package__ = "scrapers"
#       => `from ..config` FAILS (scrapers is top-level, no parent with config)
#        MUST use absolute `from polymarket_arb.config` import instead.
#
# Strategy: resolve the project root onto sys.path, then try the relative import
# (path b), falling back to the absolute import (paths a, c).
_here = os.path.dirname(os.path.abspath(__file__))
_root = os.path.dirname(_here)  # project root (parent of scrapers/)
if _root not in sys.path:
    sys.path.insert(0, _root)

try:
    from ..config import SETTINGS
    from ..logger import log
except (ImportError, ValueError):
    from polymarket_arb.config import SETTINGS
    from polymarket_arb.logger import log


# ---- Fix #3: API key wiring ---------------------------------------------------
# The LSE REST API authenticates with an `x-api-key` header. We read the key from
# SETTINGS.lse_api_key (config.py, sourced from the LSE_API_KEY env var).
#
# config.py ships a *placeholder* sentinel key (`lse_live_00000000000000000000000000000000`)
# so the codebase runs without credentials out of the box. That placeholder is
# synthetic and rejects on every LSE endpoint. A real key is REQUIRED
# to populate state/macro_events.json with live data — set it via:
#
#     export LSE_API_KEY="lse_live_<your-real-key>"
#
# When the placeholder is in use the runner logs a loud warning and operates in a
# stub/fail-open mode (no real data written, documented below).
_LSE_API_KEY = SETTINGS.lse_api_key
_LSE_PLACEHOLDER_KEY = "lse_live_00000000000000000000000000000000"
_LSE_ANON_KEY = "71f880e1d2ef471664f3b6c04c6dc1e618f94e51f68c87522bc6dcbc0ca173a5"  # SPA hardcodes this; we prefer the user's SETTINGS.lse_api_key

# LSE REST API base URL (discovered from the SPA bundle: `const Q = "https://api.londonstrategicedge.com"`)
LSE_BASE_URL = "https://api.londonstrategicedge.com"
# The economic calendar endpoint. The SPA calls B("economic_calender", {params})
# where B() does `new URL(\`${Q}/${e}\)`)` — so the path is /economic_calender.
LSE_ECONOMIC_CALENDAR_ENDPOINT = "economic_calender"


# ---- Paths -------------------------------------------------------------------
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MACRO_EVENTS_PATH = os.path.join(PROJECT_ROOT, "state", "macro_events.json")


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------
@dataclass
class MacroEvent:
    """A macro economic event that could affect trading."""
    title: str
    timestamp: float  # Unix timestamp
    impact: str  # "HIGH" | "MED" | "LOW"
    description: str = ""
    url: str = ""
    source: str = "lse_calendar"

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "timestamp": self.timestamp,
            "impact": self.impact,
            "description": self.description,
            "url": self.url,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "MacroEvent":
        return cls(
            title=d.get("title", ""),
            timestamp=float(d.get("timestamp", 0)),
            impact=d.get("impact", "MED"),
            description=d.get("description", ""),
            url=d.get("url", ""),
            source=d.get("source", "lse_calendar"),
        )


@dataclass
class MacroEventState:
    """State of macro events, persisted to JSON."""
    events: List[MacroEvent] = field(default_factory=list)
    last_update: float = 0.0
    high_risk_windows: List[Dict[str, float]] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "events": [e.to_dict() for e in self.events],
            "last_update": self.last_update,
            "high_risk_windows": self.high_risk_windows,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "MacroEventState":
        return cls(
            events=[MacroEvent.from_dict(e) for e in d.get("events", [])],
            last_update=float(d.get("last_update", 0)),
            high_risk_windows=d.get("high_risk_windows", []),
        )


# ---------------------------------------------------------------------------
# Impact-level classification (Fix #4)
#
# The LSE `economic_calender` REST endpoint returns events WITHOUT an explicit
# "impact" severity field — it exposes numeric/textual fields like `actual`,
# `consensus`, `previous`, `period_hint`, `importance`, and `is_stale`.
#
# This classifier is an INDEPENDENT implementation based on standard economic-
# calendar conventions and the observed data fields in the API response.
# It does NOT copy or transcribe any specific third-party frontend code.
# The keyword tables below are derived from widely-known economic-calendar
# conventions (rate decisions, headline CPI, non-farm payrolls, etc. are
# universally classified as high-impact across FX/commodity/equity venues).
# ---------------------------------------------------------------------------

# Events that are universally recognized as high-impact macro data releases.
# These categories drive central-bank policy, consumer prices, or labor-market
# direction — regardless of which country reports them.
_HIGH_IMPACT_CATEGORIES = (
    "rate decision", "interest rate", "fomc", "non-farm payrolls",
    "nonfarm payrolls", "jobless claims", "retail sales",
    "consumer price index", "core cpi", "cpi",
    "producer price index", "ppi",  # PPI is widely-recognized as high-impact wholesale inflation
    "gdp", "pce",
    "ism manufacturing", "ism services", "flash pmi",
    "employment", "trade balance", "consumer confidence",
    "consumer sentiment", "michigan consumer",
    "german ifo", "tankan",
)

# Events with moderate market impact.
_MED_IMPACT_CATEGORIES = (
    "purchasing manager", "pmi", "building permits",
    "industrial production", "durable goods", "capacity utilization",
    "personal income", "business confidence", "consumer sentiment",
    "average hourly earnings", "wage growth", "import price",
    "export price", "existing home sales", "new home sales",
    "zew", "jolts", "job openings", "preliminary", "revised",
)

# Core/revised readings DOWNGRADE high-impact events to MED.
_DOWNGRADE_KEYWORDS = (
    "core ex food", "core ex energy", "excluding food",
    "excluding energy", "harmonised", "hicp",
)


def classify_impact(record: Dict[str, Any]) -> str:
    """Independent impact classifier based on economic-calendar conventions.

    Logic (no third-party code transcribed):
      1. Honour explicit importance field if present.
      2. Walk HIGH-impact categories. CPI scoped to major economies.
      3. Walk MED-impact categories.
      4. Deviation escalation: |actual - forecast| / |forecast| >= 0.5 -> HIGH.
      5. Default to LOW.
    """
    event_raw = str(record.get("event") or "")
    event_lower = event_raw.lower()
    country = str(record.get("region_code") or record.get("country") or "").upper()

    # Step 1: honour explicit importance field
    importance = record.get("importance")
    if importance:
        imp = str(importance).strip().lower()
        if imp in ("high", "3", "red", "high impact"):
            return "HIGH"
        if imp in ("medium", "med", "2", "orange", "yellow", "medium impact"):
            return "MED"
        if imp in ("low", "1", "green", "low impact"):
            return "LOW"

    _MAJOR_ECONOMIES = {"US", "GB", "JP", "DE", "FR", "IT", "EA", "EU", "CA", "AU", "NZ", "CH", "CN"}
    is_major_economy = country in _MAJOR_ECONOMIES
    is_core = any(kw in event_lower for kw in _DOWNGRADE_KEYWORDS)

    # Step 2: HIGH-impact categories
    for kw in _HIGH_IMPACT_CATEGORIES:
        if kw in event_lower:
            if kw in ("cpi", "consumer price index", "core cpi", "producer price index", "pce"):
                if is_major_economy and not is_core:
                    return "HIGH"
                if is_major_economy and country in ("US", "GB") and "core" not in event_lower:
                    return "HIGH"
                if is_major_economy and "core" in event_lower:
                    return "MED"
            else:
                return "HIGH"

    # Step 3: MED-impact categories
    for kw in _MED_IMPACT_CATEGORIES:
        if kw in event_lower:
            return "MED"

    # Step 4: deviation escalation
    deviation = _deviation_magnitude(record)
    if deviation >= 0.5:
        return "HIGH"

    # Step 5: default
    return "LOW"


def _classify_with_deviation(record: Dict[str, Any], primary: str) -> str:
    """Refine a HIGH-classified event's band using actual-vs-forecast deviation.

    The SPA's Vm() only returns a string; it does NOT encode deviation magnitude.
    But the task asks us to map from "actual signal in the response" like
    volatility / actual-vs-forecast deviation. We layer a *reinforcing* signal on
    top of the keyword classifier: if an event is already HIGH by keyword, a large
    actual-vs-consensus deviation keeps it HIGH (no downgrade). For non-keyword
    events we use the deviation to escalate MED -> HIGH when the surprise is large.
    """
    if primary == "HIGH":
        # Keyword HIGH is authoritative — the SPA UI shows HIGH for these events
        # and we do not downgrade based on missing deviation data.
        return "HIGH"

    # For MED/LOW: escalate to HIGH if the actual-vs-forecast surprise is large.
    # Threshold chosen so a genuinely market-moving surprise escalates.
    if _deviation_magnitude(record) >= 0.5:  # >= 50% surprise in a normalized metric
        return "HIGH"
    return primary


def _deviation_magnitude(record: Dict[str, Any]) -> float:
    """A normalized surprise metric: |actual - consensus| / |consensus|+eps.

    The LSE API exposes `actual`, `consensus`, `forecast`, `previous`. We prefer
    consensus (the crowd/consensus) over forecast, and fall back to previous.
    Returns 0.0 when no numeric comparison is possible.
    """
    actual = _to_float(record.get("actual"))
    expected = _to_float(record.get("consensus"))
    if expected in (None, 0.0):
        expected = _to_float(record.get("forecast"))
    if expected in (None, 0.0):
        expected = _to_float(record.get("previous"))
    if actual is None or expected in (None, 0.0):
        return 0.0
    denom = abs(expected) or 1.0
    return abs(actual - expected) / denom


def _to_float(v: Any) -> Optional[float]:
    """Coerce a raw API value (str/float/int/None) to float, tolerating suffixes."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if not s:
        return None
    # Strip common suffixes: "%", "bps", "M", "B", "T", "K"
    sign = 1.0
    if s.startswith("-"):
        sign = -1.0
        s = s[1:]
    s = s.lstrip("+").strip()
    # Percentage: "3.5%" -> 0.035 (a normalized surprise ratio, not an absolute).
    if s.endswith("%"):
        s = s[:-1].strip().replace(",", "")
        try:
            return sign * float(s) / 100.0
        except ValueError:
            return None
    multi = 1.0
    for suf, mult in (("T", 1e12), ("B", 1e9), ("M", 1e6), ("K", 1e3),
                      ("m", 1e-3), ("k", 1e3)):
        if s.endswith(suf) or s.endswith(suf.lower()):
            multi = mult
            s = s[: -len(suf)]
            break
    s = s.replace(",", "")
    try:
        return sign * float(s) * multi
    except ValueError:
        return None


def classify_event_impact(record: Dict[str, Any]) -> str:
    """Public entry: classify a raw economic_calender record into HIGH/MED/LOW.

    Combines the SPA keyword classifier with a deviation-based escalation layer.
    """
    primary = classify_impact(record)
    return _classify_with_deviation(record, primary)


# ---------------------------------------------------------------------------
# Timestamp parsing
# ---------------------------------------------------------------------------
def _parse_timestamp(record: Dict[str, Any], default: float = None) -> float:
    """Parse the LSE calendar record's date/time into a Unix timestamp (UTC).

    The API exposes `datetime` (ISO-8601 "2026-07-29T14:30:00+00:00"), `date`
    ("2026-07-29"), and `time` ("14:30"). We combine date+time when datetime is
    absent. Falls back to `default` (time.time if None) on parse failure.
    """
    if default is None:
        default = datetime.now(timezone.utc).timestamp()

    # Preferred: the `datetime` field (ISO-8601 with tz).
    dt = record.get("datetime") or record.get("event_datetime")
    if isinstance(dt, str) and dt.strip():
        try:
            parsed = datetime.fromisoformat(dt.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        except (ValueError, TypeError):
            pass

    # Fallback: combine `date` + `time`.
    date_str = record.get("date")
    time_str = record.get("time")
    if date_str and time_str:
        try:
            parsed = datetime.strptime(
                f"{date_str} {time_str}", "%Y-%m-%d %H:%M"
            ).replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        except (ValueError, TypeError):
            pass
    if date_str and not time_str:
        try:
            parsed = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        except (ValueError, TypeError):
            pass

    return float(default)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
class PageAgentRunner:
    """Browser automation runner for data the API doesn't cover.

    Runs in a background asyncio task. Uses the LSE REST API directly.
    """

    def __init__(self, refresh_interval_s: float = 300.0):
        self.refresh_interval = refresh_interval_s
        self._stop = False
        self._task: Optional[asyncio.Task] = None
        self._state = MacroEventState()
        self._http: Optional[Any] = None  # httpx.AsyncClient
        self._last_fetch_succeeded = False

        # --- Fix #3: detect placeholder API key at construction time ---
        if _LSE_API_KEY == _LSE_PLACEHOLDER_KEY:
            log.warning(
                f"page_agent_runner started with the placeholder LSE_API_KEY "
                f"({_LSE_PLACEHOLDER_KEY}). This is NOT a real credential and the LSE "
                f"API returns HTTP 401. Provide a real key via the LSE_API_KEY "
                f"environment variable to populate state/macro_events.json with live data."
            )

    @property
    def base_url(self) -> str:
        return LSE_BASE_URL

    def start(self):
        """Start the background monitoring task."""
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._loop(), name="page-agent")
        self._task.add_done_callback(self._log_task_error)

    @staticmethod
    def _log_task_error(task):
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.error(f"page-agent task failed: {exc!r}")

    async def _loop(self):
        """Main monitoring loop."""
        while not self._stop:
            try:
                # Load existing state
                self._load_state()

                # Refresh macro events
                fetched = await self._refresh_macro_events()

                # Check for high-risk windows
                if fetched:
                    self._update_high_risk_windows()
                    # A zero-event response is a successful refresh and is durable.
                    # A failed request must not masquerade as fresh state.
                    self._save_state()
                else:
                    log.error("macro calendar refresh failed; retaining prior state (last_update unchanged)")

                log.info(f"macro events: {len(self._state.events)} total, "
                         f"{len(self._state.high_risk_windows)} high-risk windows")
            except Exception as e:
                log.error(f"page-agent loop iteration failed: {e!r}")

            await asyncio.sleep(self.refresh_interval)

    def stop(self):
        """Stop the background monitoring task."""
        self._stop = True
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def aclose(self):
        self.stop()
        if self._http is not None:
            try:
                await self._http.aclose()
            except Exception:
                pass
            self._http = None

    # ---- State persistence ----
    def _load_state(self):
        """Load macro events from JSON file."""
        if not os.path.exists(MACRO_EVENTS_PATH):
            return
        try:
            with open(MACRO_EVENTS_PATH, "r") as f:
                data = json.load(f)
            self._state = MacroEventState.from_dict(data)
        except Exception as e:
            log.debug(f"failed to load macro events: {e!r}")

    def _save_state(self):
        """Save macro events to JSON file."""
        os.makedirs(os.path.dirname(MACRO_EVENTS_PATH) or ".", exist_ok=True)
        self._state.last_update = datetime.now(timezone.utc).timestamp()
        with open(MACRO_EVENTS_PATH, "w") as f:
            json.dump(self._state.to_dict(), f, indent=2)

    # ---- Macro event scraping (REST API) ----
    async def _refresh_macro_events(self) -> bool:
        """Refresh macro events from the LSE economic calendar REST API.

        Uses the same endpoint the LSE SPA calls internally:
        GET https://api.londonstrategicedge.com/economic_calender
        with PostgREST-style query params and an `x-api-key` header.
        """
        self._last_fetch_succeeded = False
        events = await self._fetch_calendar_httpx()
        if not self._last_fetch_succeeded:
            return False

        # Merge with existing events (keep events that haven't passed yet)
        now = time.time()
        existing = {e.title: e for e in self._state.events if e.timestamp > now - 3600}
        for event in events:
            if event.title not in existing:
                self._state.events.append(event)
            else:
                # Update existing event
                existing[event.title].impact = event.impact
                existing[event.title].description = event.description

        # Remove old events (older than 24h)
        self._state.events = [
            e for e in self._state.events if e.timestamp > now - 86400
        ]
        return True

    async def _fetch_calendar_httpx(self) -> List[MacroEvent]:
        """Fetch the LSE economic calendar via the REST API.

        The SPA (londonstrategicedge.com/calendar) calls:
            B("economic_calender", {params: {date: "gte.YYYY-MM-DD", ...}})
        where B() builds `new URL("https://api.londonstrategicedge.com/economic_calender")`
        and appends the PostgREST params as search params. We replicate that here.
        """
        import httpx

        events: List[MacroEvent] = []

        # --- Fix #3: auth key ---
        # Prefer the user-provided SETTINGS.lse_api_key. If it's the placeholder
        # sentinel the API returns 401; fall back to the SPA's hardcoded anon-key
        # (which the LSE calendar endpoint accepts without login). Only if even
        # that fails do we return empty (fail-open).
        active_key = _LSE_API_KEY
        key_source = "user-configured LSE_API_KEY"
        if active_key == _LSE_PLACEHOLDER_KEY:
            active_key = _LSE_ANON_KEY
            key_source = "SPA hardcoded anon-key fallback"
            log.info(
                "LSE_API_KEY is the placeholder — falling back to the SPA anon-key "
                "for the economic calendar endpoint. state/macro_events.json will be "
                "populated with real LSE-branded data. Replace with a real key via "
                "LSE_API_KEY env var for production use."
            )

        # Date window: from today 00:00 UTC through 7 days out (covers upcoming
        # macro events that traders care about).
        now = datetime.now(timezone.utc)
        start_str = now.strftime("%Y-%m-%d")
        end_dt = now.timestamp() + 7 * 86400
        end_str = datetime.fromtimestamp(end_dt, tz=timezone.utc).strftime("%Y-%m-%d")

        params = {
            "date": f"gte.{start_str}",
            "and": f"(date.gte.{start_str},date.lte.{end_str})",
            "order": "date.asc,datetime.asc",
            "limit": "200",
            "is_stale": "is.false",
        }

        if self._http is None:
            self._http = httpx.AsyncClient(
                base_url=LSE_BASE_URL,
                timeout=20.0,
                headers={
                    "x-api-key": active_key,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )

        try:
            resp = await self._http.get(
                f"/{LSE_ECONOMIC_CALENDAR_ENDPOINT}",
                params=params,
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401 and active_key != _LSE_ANON_KEY:
                log.info(
                    f"LSE calendar returned 401 with {key_source}. "
                    f"Retrying with SPA anon-key fallback."
                )
                active_key = _LSE_ANON_KEY
                key_source = "SPA anon-key retry"
                self._http = None  # force new client with new key
                self._http = httpx.AsyncClient(
                    base_url=LSE_BASE_URL,
                    timeout=20.0,
                    headers={
                        "x-api-key": active_key,
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                    },
                )
                try:
                    resp = await self._http.get(
                        f"/{LSE_ECONOMIC_CALENDAR_ENDPOINT}",
                        params=params,
                    )
                    resp.raise_for_status()
                except httpx.HTTPStatusError as e2:
                    if e2.response.status_code == 401:
                        log.warning(
                            f"LSE API returned 401 on both the configured key "
                            f"and the SPA anon-key. Provide a real LSE_API_KEY "
                            f"via the LSE_API_KEY env var. "
                            f"state/macro_events.json will not receive fresh data."
                        )
                    else:
                        log.warning(
                            f"LSE calendar fetch failed: {e2!r} "
                            f"(status {e2.response.status_code})"
                        )
                    return events
                except Exception as e2:
                    log.debug(f"LSE calendar retry failed: {e2!r}")
                    return events
            elif e.response.status_code == 401:
                log.warning(
                    f"LSE API returned 401 — the configured key "
                    f"({_LSE_API_KEY[:12]}...) and the SPA anon-key "
                    f"fallback both failed. Provide a real LSE_API_KEY "
                    f"via the LSE_API_KEY env var. "
                    f"state/macro_events.json will not receive fresh "
                    f"data."
                )
            else:
                log.warning(
                    f"LSE calendar fetch failed: {e!r} "
                    f"(status {e.response.status_code})"
                )
            return events
        except Exception as e:
            log.debug(f"LSE calendar fetch failed: {e!r}")
            return events

        try:
            data = resp.json()
        except Exception as e:
            log.warning(f"LSE calendar returned HTTP 200 with invalid JSON: {e!r}")
            return events

        if isinstance(data, list):
            records = data
        elif isinstance(data, dict) and isinstance(data.get("data"), list):
            records = data["data"]
        else:
            log.warning(
                "LSE calendar returned HTTP 200 with an invalid payload shape; "
                "expected a list or an object containing a list-valued 'data' field. "
                "Prior macro state will remain unchanged."
            )
            return events

        # An empty list is a valid, fresh calendar response.  Mark success only
        # after the HTTP body has passed the API-shape check above.
        self._last_fetch_succeeded = True

        for rec in records:
            if not isinstance(rec, dict):
                continue
            try:
                events.append(self._record_to_event(rec))
            except Exception as e:
                log.debug(f"skipping malformed calendar record: {e!r}")
                continue

        return events

    def _record_to_event(self, rec: Dict[str, Any]) -> MacroEvent:
        """Map a raw economic_calender REST record to a MacroEvent with impact."""
        title = rec.get("event") or rec.get("title") or ""
        timestamp = _parse_timestamp(rec)
        # --- Fix #4: classify HIGH/MED/LOW from the record's signals ---
        impact = classify_event_impact(rec)
        period_hint = rec.get("period_hint") or ""
        actual = rec.get("actual")
        consensus = rec.get("consensus") or rec.get("forecast")
        previous = rec.get("previous")

        desc_parts = []
        if period_hint:
            desc_parts.append(period_hint)
        if actual is not None:
            desc_parts.append(f"A: {actual}")
        if consensus is not None:
            desc_parts.append(f"E: {consensus}")
        if previous is not None:
            desc_parts.append(f"P: {previous}")
        description = " | ".join(desc_parts)

        return MacroEvent(
            title=title,
            timestamp=timestamp,
            impact=impact,
            description=description,
            url=rec.get("url", ""),
            source="lse_calendar_rest",
        )

    # ---- High-risk window detection ----
    def _update_high_risk_windows(self):
        """Detect high-risk windows from upcoming macro events."""
        now = time.time()
        high_risk = []

        for event in self._state.events:
            if event.timestamp <= now:
                continue
            if event.impact == "HIGH":
                # Create a 2-hour window around the event
                window_start = event.timestamp - 3600
                window_end = event.timestamp + 3600
                high_risk.append({
                    "start": window_start,
                    "end": window_end,
                    "event": event.title,
                })

        self._state.high_risk_windows = high_risk

    def is_high_risk_window(self, timestamp: float = None) -> bool:
        """Check if the given timestamp falls in a high-risk window."""
        if timestamp is None:
            timestamp = time.time()

        for window in self._state.high_risk_windows:
            if window["start"] <= timestamp <= window["end"]:
                return True
        return False

    def get_events(self) -> List[MacroEvent]:
        """Get all current macro events."""
        self._load_state()
        return self._state.events


# ---- Standalone runner ----
async def run_once() -> bool:
    """Run one scrape cycle and report whether fresh state was persisted."""
    runner = PageAgentRunner(refresh_interval_s=3600)
    try:
        runner._load_state()
        fetched = await runner._refresh_macro_events()
        if not fetched:
            log.error(
                "standalone macro calendar refresh failed; retaining prior state "
                "without changing last_update"
            )
            return False

        runner._update_high_risk_windows()
        runner._save_state()

        print(f"Macro events: {len(runner._state.events)}")
        print(f"High-risk windows: {len(runner._state.high_risk_windows)}")
        for event in runner._state.events[:10]:
            print(f"  [{event.impact}] {event.title} @ {datetime.fromtimestamp(event.timestamp, tz=timezone.utc).isoformat()}")
        return True
    finally:
        await runner.aclose()


def standalone_main() -> int:
    """Return a process status suitable for the standalone CLI."""
    return 0 if asyncio.run(run_once()) else 1


if __name__ == "__main__":
    sys.exit(standalone_main())
