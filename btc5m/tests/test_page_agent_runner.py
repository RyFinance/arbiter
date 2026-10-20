"""Tests for the fixed scrapers/page_agent_runner.py (TASK A).

Covers:
  * Fix #1 — import path: the module loads under risk.py's importlib shim AND
    when its functions are exercised directly.
  * Fix #2 — REST parsing: _record_to_event maps a realistic
    economic_calender JSON record to a MacroEvent with correct fields.
  * Fix #3 — API key wiring: placeholder key detected, real key accepted.
  * Fix #4 — impact classification: HIGH/MED/LOW from importance + keyword tables
    (port of the SPA's Vm() classifier), plus deviation-based escalation.
  * Timestamp parsing: datetime / date+time / date-only / fallback.

These tests are offline (no network) — the REST fetch is mocked.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Resolve the scraper module through the same importlib shim risk.py uses, so we
# exercise the exact code path the hot path loads.
from polymarket_arb.risk import _maybe_page_agent


def _get_module():
    mod, ok = _maybe_page_agent()
    assert ok and mod is not None, "page_agent_runner must import cleanly"
    return mod


# ---------------------------------------------------------------------------
# Fix #1: import path / module loads
# ---------------------------------------------------------------------------
def test_module_imports_via_shim():
    mod = _get_module()
    assert hasattr(mod, "PageAgentRunner")
    assert hasattr(mod, "MacroEvent")
    assert hasattr(mod, "MacroEventState")
    assert hasattr(mod, "classify_event_impact")
    assert hasattr(mod, "classify_impact")
    assert hasattr(mod, "LSE_BASE_URL")
    assert mod.LSE_BASE_URL == "https://api.londonstrategicedge.com"
    assert mod.LSE_ECONOMIC_CALENDAR_ENDPOINT == "economic_calender"


# ---------------------------------------------------------------------------
# Fix #3: API key wiring
# ---------------------------------------------------------------------------
def test_placeholder_key_constant_matches_config():
    """The placeholder sentinel in the scraper matches config.py's default."""
    from polymarket_arb.config import SETTINGS
    mod = _get_module()
    assert mod._LSE_PLACEHOLDER_KEY == "lse_live_00000000000000000000000000000000"
    assert mod._LSE_API_KEY == SETTINGS.lse_api_key


# ---------------------------------------------------------------------------
# Fix #4: impact classification (port of SPA Vm())
# ---------------------------------------------------------------------------
CLASSIFY_CASES = [
    # --- High-impact events (the known HIGH set from test_macro_event_accuracy) ---
    ("US Non-Farm Payrolls", "US", {}, "HIGH"),
    ("US ISM Manufacturing PMI", "US", {}, "HIGH"),
    ("US ISM Services PMI", "US", {}, "HIGH"),
    ("US Initial Jobless Claims", "US", {}, "HIGH"),
    ("US Retail Sales m/m", "US", {}, "HIGH"),
    ("US CPI m/m", "US", {}, "HIGH"),
    ("US PPI m/m", "US", {}, "HIGH"),
    ("US Q2 GDP", "US", {}, "HIGH"),
    ("US Core PCE", "US", {}, "HIGH"),
    ("US Michigan Consumer Sentiment", "US", {}, "HIGH"),
    ("US FOMC Rate Decision", "US", {}, "HIGH"),
    ("US Unemployment Rate", "US", {}, "HIGH"),  # Im keyword for Wm country
    ("ECB Interest Rate Decision", "EA", {}, "HIGH"),
    ("ECB Interest Rate Decision", "EU", {}, "HIGH"),
    ("BoE Interest Rate Decision", "GB", {}, "HIGH"),
    ("BoJ Interest Rate Decision", "JP", {}, "HIGH"),
    ("GB CPI m/m", "GB", {}, "HIGH"),  # cpi is HIGH for GB via Hm

    # --- Country scoping: CPI is HIGH via Um for developed-market countries
    # (cpi is in Um; DE/CA are Wm countries; not core -> HIGH per SPA Vm) ---
    ("DE CPI m/m", "DE", {}, "HIGH"),
    ("CA CPI m/m", "CA", {}, "HIGH"),

    # --- importance field overrides keywords ---
    ("Some Event", "US", {"importance": "high"}, "HIGH"),
    ("Some Event", "US", {"importance": "3"}, "HIGH"),
    ("Some Event", "US", {"importance": "red"}, "HIGH"),
    ("Some Event", "US", {"importance": "medium"}, "MED"),
    ("Some Event", "US", {"importance": "2"}, "MED"),
    ("Some Event", "US", {"importance": "yellow"}, "MED"),
    ("Some Event", "US", {"importance": "low"}, "LOW"),
    ("Some Event", "US", {"importance": "1"}, "LOW"),
    ("Some Event", "US", {"importance": "green"}, "LOW"),

    # --- auction is always low ---
    ("Treasury Bill Auction", "US", {}, "LOW"),
    ("German Bund Auction", "DE", {}, "LOW"),

    # --- MED via Fm ---
    ("US Existing Home Sales", "US", {}, "MED"),
    ("US Building Permits", "US", {}, "MED"),
    ("US Personal Income", "US", {}, "MED"),

    # --- unknown event with no signals -> LOW ---
    ("Corporate Earnings Call", "US", {}, "LOW"),
]


@pytest.mark.parametrize("title,country,extra,expected", CLASSIFY_CASES)
def test_classify_impact_keyword_and_importance(title, country, extra, expected):
    mod = _get_module()
    rec = {"event": title, "region_code": country}
    rec.update(extra)
    assert mod.classify_impact(rec) == expected, (
        f"classify_impact({title!r}, {country!r}) = "
        f"{mod.classify_impact(rec)!r}, expected {expected!r}"
    )


# ---------------------------------------------------------------------------
# REST fetch: mock httpx, verify params + key header + parsing
# ---------------------------------------------------------------------------
def test_deviation_escalates_med_to_high():
    """A MED event with a large actual-vs-consensus surprise escalates to HIGH."""
    mod = _get_module()
    rec = {"event": "US Existing Home Sales", "region_code": "US",
           "actual": 10.0, "consensus": 1.0}
    assert mod.classify_event_impact(rec) == "HIGH"


def test_small_deviation_keeps_med():
    mod = _get_module()
    rec = {"event": "US Existing Home Sales", "region_code": "US",
           "actual": 1.1, "consensus": 1.0}
    assert mod.classify_event_impact(rec) == "MED"


def test_keyword_high_not_downgraded_by_zero_deviation():
    """A keyword-HIGH event stays HIGH even with no actual/forecast data."""
    mod = _get_module()
    rec = {"event": "US FOMC Rate Decision", "region_code": "US"}
    assert mod.classify_event_impact(rec) == "HIGH"


# ---------------------------------------------------------------------------
# Fix #2: REST record -> MacroEvent parsing
# ---------------------------------------------------------------------------
def _runner():
    """Build a PageAgentRunner instance (methods live on the class, not the module)."""
    mod = _get_module()
    return mod.PageAgentRunner(refresh_interval_s=3600)


def test_record_to_event_basic():
    runner = _runner()
    rec = {
        "event": "US FOMC Rate Decision",
        "region_code": "US",
        "datetime": "2026-07-29T18:00:00+00:00",
        "period_hint": "Quarterly",
        "actual": "5.50%",
        "consensus": "5.25%",
        "forecast": "5.25%",
        "previous": "5.25%",
        "importance": "high",
        "is_stale": False,
    }
    ev = runner._record_to_event(rec)
    assert ev.title == "US FOMC Rate Decision"
    assert ev.impact == "HIGH"
    assert ev.timestamp == datetime(2026, 7, 29, 18, 0, tzinfo=timezone.utc).timestamp()
    assert "A: 5.50%" in ev.description
    assert "E: 5.25%" in ev.description
    assert "P: 5.25%" in ev.description
    assert ev.source == "lse_calendar_rest"


def test_record_to_event_date_time_fallback():
    runner = _runner()
    rec = {"event": "US CPI m/m", "region_code": "US", "date": "2026-07-30", "time": "12:30"}
    ev = runner._record_to_event(rec)
    assert ev.timestamp == datetime(2026, 7, 30, 12, 30, tzinfo=timezone.utc).timestamp()
    assert ev.impact == "HIGH"


def test_record_to_event_auction_low():
    runner = _runner()
    rec = {"event": "Treasury Bill Auction", "region_code": "US", "date": "2026-07-31", "time": "11:30"}
    ev = runner._record_to_event(rec)
    assert ev.impact == "LOW"


def test_record_to_event_missing_fields_defaults():
    runner = _runner()
    rec = {"event": "Mystery Event"}  # no date, no country, no impact signals
    ev = runner._record_to_event(rec)
    assert ev.title == "Mystery Event"
    assert ev.impact == "LOW"
    # timestamp falls back to "now"
    now = datetime.now(timezone.utc).timestamp()
    assert abs(ev.timestamp - now) < 5


# ---------------------------------------------------------------------------
# Timestamp parsing
# ---------------------------------------------------------------------------
def test_parse_timestamp_datetime_iso():
    mod = _get_module()
    ts = mod._parse_timestamp({"datetime": "2026-07-29T14:30:00+00:00"})
    assert ts == datetime(2026, 7, 29, 14, 30, tzinfo=timezone.utc).timestamp()


def test_parse_timestamp_date_plus_time():
    mod = _get_module()
    ts = mod._parse_timestamp({"date": "2026-07-29", "time": "14:30"})
    assert ts == datetime(2026, 7, 29, 14, 30, tzinfo=timezone.utc).timestamp()


def test_parse_timestamp_date_only():
    mod = _get_module()
    ts = mod._parse_timestamp({"date": "2026-07-29"})
    expected = datetime(2026, 7, 29, tzinfo=timezone.utc).timestamp()
    assert ts == expected


def test_parse_timestamp_empty_falls_back():
    mod = _get_module()
    before = time.time()
    ts = mod._parse_timestamp({})
    after = time.time()
    # Falls back to now; allow a small tolerance for float rounding.
    assert before - 1.0 <= ts <= after + 1.0


# ---------------------------------------------------------------------------
# REST fetch: mock httpx, verify params + key header + parsing
# ---------------------------------------------------------------------------
class _FakeResp:
    def __init__(self, payload, status_code=200, text=""):
        self._payload = payload
        self.status_code = status_code
        self.text = text or json.dumps(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx as _h
            raise _h.HTTPStatusError("err", response=self)


@pytest.mark.asyncio
async def test_fetch_calendar_uses_correct_endpoint_and_key():
    """The REST fetch hits /economic_calender with x-api-key from SETTINGS."""
    mod = _get_module()
    from polymarket_arb.config import SETTINGS
    saved_key = mod._LSE_API_KEY
    # Temporarily make the key look real so the fetch proceeds.
    mod._LSE_API_KEY = "lse_live_test_fake_unit"
    try:
        runner = mod.PageAgentRunner(refresh_interval_s=3600)
        fake_http = MagicMock()
        captured = {}

        async def fake_get(url, params=None):
            captured["url"] = url
            captured["params"] = params
            return _FakeResp([
                {"event": "US FOMC Rate Decision", "region_code": "US",
                 "datetime": "2026-07-29T18:00:00+00:00", "importance": "high"},
            ])

        fake_http.get = AsyncMock(side_effect=fake_get)
        fake_http.aclose = AsyncMock()
        runner._http = fake_http

        events = await runner._fetch_calendar_httpx()
        assert len(events) == 1
        assert events[0].impact == "HIGH"
        assert captured["url"].startswith("/economic_calender")
        assert captured["params"]["is_stale"] == "is.false"
        assert "date" in captured["params"]
    finally:
        # restore so other tests see the real placeholder key
        mod._LSE_API_KEY = saved_key


@pytest.mark.asyncio
async def test_fetch_calendar_placeholder_key_uses_anon_fallback():
    """With the placeholder key, the fetch falls back to the SPA anon-key
    and returns real events instead of short-circuiting to empty."""
    mod = _get_module()
    from polymarket_arb.config import SETTINGS
    saved = mod._LSE_API_KEY
    mod._LSE_API_KEY = SETTINGS.lse_api_key  # placeholder
    try:
        runner = mod.PageAgentRunner(refresh_interval_s=3600)
        fake_http = MagicMock()

        async def fake_get(url, params=None):
            return _FakeResp([
                {"event": "US ISM Manufacturing PMI", "region_code": "US",
                 "datetime": "2026-08-03T14:00:00+00:00", "importance": "high"},
            ])

        fake_http.get = AsyncMock(side_effect=fake_get)
        fake_http.aclose = AsyncMock()
        runner._http = fake_http

        events = await runner._fetch_calendar_httpx()
        # The fallback should have produced events, not an empty list.
        assert len(events) == 1
        assert events[0].impact == "HIGH"
    finally:
        mod._LSE_API_KEY = saved


@pytest.mark.asyncio
async def test_fetch_calendar_401_logs_and_returns_empty():
    """A 401 from the API is logged and yields no events (fail-open)."""
    mod = _get_module()
    saved = mod._LSE_API_KEY
    mod._LSE_API_KEY = "lse_live_test_fake_unit"
    try:
        runner = mod.PageAgentRunner(refresh_interval_s=3600)
        fake_http = MagicMock()

        async def fake_get(url, params=None):
            return _FakeResp({"error": "Unauthorized"}, status_code=401, text="Unauthorized")

        fake_http.get = AsyncMock(side_effect=fake_get)
        fake_http.aclose = AsyncMock()
        runner._http = fake_http
        events = await runner._fetch_calendar_httpx()
        assert events == []
    finally:
        mod._LSE_API_KEY = saved


@pytest.mark.asyncio
async def test_fetch_calendar_malformed_http_200_is_failure():
    """A successful status is not fresh data unless the JSON shape is valid."""
    mod = _get_module()
    saved = mod._LSE_API_KEY
    mod._LSE_API_KEY = "lse_live_test_fake_unit"
    try:
        runner = mod.PageAgentRunner(refresh_interval_s=3600)
        fake_http = MagicMock()
        fake_http.get = AsyncMock(return_value=_FakeResp({"unexpected": []}))
        fake_http.aclose = AsyncMock()
        runner._http = fake_http

        events = await runner._fetch_calendar_httpx()

        assert events == []
        assert runner._last_fetch_succeeded is False
    finally:
        mod._LSE_API_KEY = saved


@pytest.mark.asyncio
async def test_run_once_failed_fetch_preserves_persisted_timestamp(monkeypatch, tmp_path):
    mod = _get_module()
    state_path = tmp_path / "macro_events.json"
    prior = mod.MacroEventState(
        events=[mod.MacroEvent(title="Prior event", timestamp=2_000_000_000.0, impact="HIGH")],
        last_update=1234567890.25,
        high_risk_windows=[{"start": 10.0, "end": 20.0, "event": "Prior event"}],
    )
    state_path.write_text(json.dumps(prior.to_dict(), indent=2))
    before = state_path.read_bytes()

    monkeypatch.setattr(mod, "MACRO_EVENTS_PATH", str(state_path))
    monkeypatch.setattr(
        mod.PageAgentRunner,
        "_fetch_calendar_httpx",
        AsyncMock(return_value=[]),
    )

    assert await mod.run_once() is False
    assert state_path.read_bytes() == before
    assert json.loads(state_path.read_text())["last_update"] == 1234567890.25


def test_standalone_cli_returns_nonzero_when_refresh_fails(monkeypatch):
    mod = _get_module()

    async def failed_run_once():
        return False

    monkeypatch.setattr(mod, "run_once", failed_run_once)
    assert mod.standalone_main() == 1


@pytest.mark.asyncio
async def test_live_loop_failed_fetch_does_not_save_or_restamp(monkeypatch):
    mod = _get_module()
    runner = mod.PageAgentRunner(refresh_interval_s=0)
    runner._state = mod.MacroEventState(last_update=1234567890.25)
    runner._load_state = MagicMock()
    runner._save_state = MagicMock()

    async def fail_once():
        runner._stop = True
        return False

    runner._refresh_macro_events = fail_once
    sleep = AsyncMock()
    monkeypatch.setattr(mod.asyncio, "sleep", sleep)

    await runner._loop()

    runner._save_state.assert_not_called()
    assert runner._state.last_update == 1234567890.25
    sleep.assert_awaited_once_with(0)


# ---------------------------------------------------------------------------
# End-to-end: refresh -> merge -> windows
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_refresh_parses_and_classifies_and_merges():
    """A mocked REST response flows through to state events + high-risk windows."""
    mod = _get_module()
    from polymarket_arb.config import SETTINGS
    saved = mod._LSE_API_KEY
    mod._LSE_API_KEY = "lse_live_test_fake_unit"
    try:
        runner = mod.PageAgentRunner(refresh_interval_s=3600)
        now = time.time()
        far_future = now + 80000  # ~22h out, within the 7-day fetch window
        records = [
            {"event": "US FOMC Rate Decision", "region_code": "US",
             "datetime": datetime.fromtimestamp(far_future, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00"),
             "importance": "high", "period_hint": "Quarterly",
             "actual": "5.50%", "consensus": "5.25%"},
            {"event": "US Existing Home Sales", "region_code": "US",
             "datetime": datetime.fromtimestamp(now + 3600, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00"),
             "period_hint": "Monthly", "actual": 3.8, "consensus": 4.1},
            {"event": "Treasury Bill Auction", "region_code": "DE",
             "datetime": datetime.fromtimestamp(now + 1800, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00"),
             "period_hint": "Weekly"},
        ]

        fake_http = MagicMock()
        async def fake_get(url, params=None):
            return _FakeResp(records)
        fake_http.get = AsyncMock(side_effect=fake_get)
        fake_http.aclose = AsyncMock()
        runner._http = fake_http

        await runner._refresh_macro_events()
        runner._update_high_risk_windows()

        impacts = sorted(e.impact for e in runner._state.events)
        assert impacts == ["HIGH", "LOW", "MED"]
        # The HIGH event (FOMC) should produce exactly one high-risk window.
        assert len(runner._state.high_risk_windows) == 1
        win = runner._state.high_risk_windows[0]
        assert win["event"] == "US FOMC Rate Decision"
        # Window is +/- 3600s around the event
        assert win["start"] == pytest.approx(far_future - 3600)
        assert win["end"] == pytest.approx(far_future + 3600)
        # is_high_risk_window True inside, False outside.
        assert runner.is_high_risk_window(far_future) is True
        assert runner.is_high_risk_window(now) is False
    finally:
        mod._LSE_API_KEY = saved
    assert win["start"] == pytest.approx(far_future - 3600)
    assert win["end"] == pytest.approx(far_future + 3600)
    # is_high_risk_window True inside, False outside.
    assert runner.is_high_risk_window(far_future) is True
    assert runner.is_high_risk_window(now) is False


def test_high_risk_window_membership_pure_logic():
    """Direct unit test of the 2-hour window membership math."""
    mod = _get_module()
    now = time.time()
    runner = mod.PageAgentRunner.__new__(mod.PageAgentRunner)
    runner._state = mod.MacroEventState(
        events=[mod.MacroEvent(title="FOMC", timestamp=now + 3600, impact="HIGH")],
        last_update=now,
        high_risk_windows=[{"start": now - 100, "end": now + 500, "event": "FOMC"}],
    )
    assert runner.is_high_risk_window(now) is True
    assert runner.is_high_risk_window(now - 200) is False
    assert runner.is_high_risk_window(now + 600) is False


def test_macro_event_state_roundtrip():
    """MacroEventState.to_dict / from_dict round-trips cleanly."""
    mod = _get_module()
    ev = mod.MacroEvent(title="X", timestamp=1234567890.0, impact="HIGH",
                        description="d", url="u", source="s")
    state = mod.MacroEventState(events=[ev], last_update=1.0,
                                high_risk_windows=[{"start": 0, "end": 100, "event": "X"}])
    d = state.to_dict()
    rt = mod.MacroEventState.from_dict(d)
    assert rt.events[0].title == "X"
    assert rt.events[0].impact == "HIGH"
    assert rt.high_risk_windows == state.high_risk_windows
    assert rt.last_update == 1.0
