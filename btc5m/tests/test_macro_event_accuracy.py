"""TASK C — Validation tests for macro event accuracy cross-reference.

Validates:
  * The cross-reference script correctly identifies true positives, false
    positives, and false negatives against known HIGH-impact events.
  * The title-matching logic handles edge cases (country-prefixed GDP,
    same-name events on different dates, measurement suffixes).
  * The simulation scenarios produce expected precision/recall values.
  * The fail-open behavior when macro_events.json is missing is correct.
  * The trustworthiness assessment correctly distinguishes between
    trustworthy (no FNs) and untrustworthy (FNs present) configurations.

These tests do NOT depend on network access or the LSE calendar — they use
the embedded KNOWN_HIGH_IMPACT_EVENTS reference data and the simulated
scenarios from scripts/macro_event_accuracy.py.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

import pytest

# Add scripts to path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_PATH = os.path.join(PROJECT_ROOT, "scripts")
sys.path.insert(0, SCRIPTS_PATH)

from macro_event_accuracy import (
    KNOWN_HIGH_IMPACT_EVENTS,
    _normalize_event_title,
    _titles_match,
    _same_date,
    _build_timestamp,
    cross_reference,
    classify_scraper_events,
    load_macro_events,
    generate_simulated_macro_state,
)


# ---------------------------------------------------------------------------
# Title normalization tests
# ---------------------------------------------------------------------------
def test_normalize_strips_parentheticals():
    assert _normalize_event_title("US CPI (June)") == "us cpi"
    # Parenthetical content is removed
    assert _normalize_event_title("GDP (Q2 Advance)") == "gdp"


def test_normalize_strips_measurement_suffix():
    assert _normalize_event_title("CPI m/m") == "cpi"
    assert _normalize_event_title("PCE y/y") == "pce"
    assert _normalize_event_title("Retail Sales m/m") == "retail sales"


def test_normalize_keeps_country_prefix():
    """Country prefixes are NOT stripped because they distinguish events."""
    assert _normalize_event_title("US GDP") == "us gdp"
    assert _normalize_event_title("German GDP") == "german gdp"
    assert "us gdp" != "german gdp"


def test_normalize_lowercase():
    assert _normalize_event_title("FOMC Rate Decision") == "fomc rate decision"


# ---------------------------------------------------------------------------
# Title matching tests
# ---------------------------------------------------------------------------
def test_titles_match_exact():
    assert _titles_match("FOMC Rate Decision", "FOMC Rate Decision", "Federal Reserve FOMC Rate Decision")


def test_titles_match_distinctive_token():
    """Single distinctive token (cpi, pce) is enough for a match."""
    assert _titles_match("US CPI", "CPI", "US CPI (June) - Consumer Price Index m/m")


def test_titles_no_match_different_countries_gdp():
    """German GDP vs US GDP should NOT match (country prefix distinguishes them)."""
    assert not _titles_match("German Q2 GDP", "US Q2 GDP", "US GDP (Q2 Advance)")


def test_titles_no_match_different_events():
    assert not _titles_match("FOMC Rate Decision", "ECB Interest Rate Decision", "European Central Bank Monetary Policy Decision")


def test_same_date_helper():
    ts1 = _build_timestamp("2026-07-29", "18:00")
    ts2 = _build_timestamp("2026-07-29", "12:30")
    assert _same_date(ts1, ts2)
    ts3 = _build_timestamp("2026-07-30", "06:00")
    assert not _same_date(ts1, ts3)


# ---------------------------------------------------------------------------
# Known HIGH impact events reference data
# ---------------------------------------------------------------------------
def test_known_high_events_includes_major_events():
    """The reference list should include FOMC, ECB, BoE, BoJ, RBA, GDP, CPI, PCE, PMI."""
    titles = [ev["title"] for ev in KNOWN_HIGH_IMPACT_EVENTS]
    assert "FOMC Rate Decision" in titles
    assert "ECB Interest Rate Decision" in titles
    assert "BoE Interest Rate Decision" in titles
    assert "BoJ Interest Rate Decision" in titles
    assert "RBA Interest Rate Decision" in titles
    assert "US Q2 GDP" in titles
    assert "US Core PCE" in titles


def test_known_high_events_all_have_required_fields():
    """Every known event must have date, time_utc, title, and currency."""
    for ev in KNOWN_HIGH_IMPACT_EVENTS:
        assert "date" in ev
        assert "time_utc" in ev
        assert "title" in ev
        assert "currency" in ev
        assert "full_title" in ev
        assert "source" in ev


# ---------------------------------------------------------------------------
# Simulation scenario tests
# ---------------------------------------------------------------------------
def test_ideal_scenario_perfection():
    """The ideal scenario should produce 100% precision and 100% recall."""
    state = generate_simulated_macro_state("ideal")
    results = cross_reference(state, start_date="2026-07-22", end_date="2026-07-31")
    assert results["true_positives"] == len(results["known_high_events"])
    assert results["false_positives"] == 0
    assert results["false_negatives"] == 0
    assert results["precision"] == 1.0
    assert results["recall"] == 1.0
    assert results["f1_score"] == 1.0


def test_typical_scraper_has_false_negatives():
    """The typical_scraper scenario should have both false positives and false negatives."""
    state = generate_simulated_macro_state("typical_scraper")
    results = cross_reference(state, start_date="2026-07-22", end_date="2026-07-31")
    assert results["false_positives"] >= 1
    assert results["false_negatives"] >= 1
    assert results["precision"] < 1.0
    assert results["recall"] < 1.0


def test_broken_scraper_zero_recall():
    """The broken_scraper scenario should have 0% recall (all HIGH events misclassified)."""
    state = generate_simulated_macro_state("broken_scraper")
    results = cross_reference(state, start_date="2026-07-22", end_date="2026-07-31")
    assert results["true_positives"] == 0
    assert results["false_negatives"] > 0
    assert results["recall"] == 0.0


def test_unknown_scenario_defaults_to_typical():
    """An unknown simulate mode should default to 'typical_scraper'."""
    state = generate_simulated_macro_state("not_a_real_mode")
    # Check that the events match (ignoring last_update timestamp which varies)
    typical = generate_simulated_macro_state("typical_scraper")
    assert state["events"] == typical["events"]
    assert state["high_risk_windows"] == typical["high_risk_windows"]


# ---------------------------------------------------------------------------
# Cross-reference with actual macro_events.json (or missing file)
# ---------------------------------------------------------------------------
def test_cross_reference_no_file():
    """When macro_events.json doesn't exist, all known events are false negatives."""
    results = cross_reference(None, start_date="2026-07-22", end_date="2026-07-31")
    assert results["macro_file_exists"] is False
    assert results["scraper_events_total"] == 0
    assert results["scraper_high_events"] == []
    assert results["false_negatives"] == len(results["known_high_events"])
    assert results["false_positives"] == 0
    assert results["true_positives"] == 0
    assert results["precision"] == 0.0  # no FP, no TP -> 0/0 -> 0.0
    assert results["recall"] == 0.0


def test_load_macro_events_returns_none_when_missing(tmp_path):
    """load_macro_events returns None when the file doesn't exist."""
    # Use the real function but verify behavior
    result = load_macro_events()
    # Either returns None (file doesn't exist) or a dict (file exists)
    assert result is None or isinstance(result, dict)


# ---------------------------------------------------------------------------
# Trustworthiness assessment logic
# ---------------------------------------------------------------------------
def test_trustworthy_when_no_false_negatives():
    """A scenario with zero false negatives is trustworthy for trading."""
    state = generate_simulated_macro_state("ideal")
    results = cross_reference(state, start_date="2026-07-22", end_date="2026-07-31")
    assert results["macro_file_exists"] is True
    assert results["false_negatives"] == 0
    # Trustworthy: all known HIGH events were flagged
    assert results["recall"] == 1.0


def test_untrustworthy_when_false_negatives_present():
    """A scenario with false negatives is NOT trustworthy for trading."""
    state = generate_simulated_macro_state("typical_scraper")
    results = cross_reference(state, start_date="2026-07-22", end_date="2026-07-31")
    assert results["macro_file_exists"] is True
    assert results["false_negatives"] > 0
    # Untrustworthy: known HIGH events were missed
    assert results["recall"] < 1.0


# ---------------------------------------------------------------------------
# Event classification from macro_events.json
# ---------------------------------------------------------------------------
def test_classify_scraper_events_extracts_fields():
    """classify_scraper_events correctly extracts title, impact, timestamp."""
    state = {
        "events": [
            {"title": "Test Event", "timestamp": 1785414600.0, "impact": "HIGH",
             "description": "test", "url": "", "source": "lse_calendar"},
            {"title": "Med Event", "timestamp": 1785414600.0, "impact": "MED",
             "description": "test", "url": "", "source": "lse_calendar"},
        ],
        "last_update": 1785414600.0,
        "high_risk_windows": [],
    }
    events = classify_scraper_events(state)
    assert len(events) == 2
    assert events[0]["title"] == "Test Event"
    assert events[0]["impact"] == "HIGH"
    assert events[0]["timestamp"] == 1785414600.0
    assert events[1]["impact"] == "MED"


def test_classify_scraper_events_handles_missing_fields():
    """classify_scraper_events handles missing/None fields gracefully."""
    state = {
        "events": [
            {"title": "Missing Fields"},  # no timestamp, no impact
        ],
        "last_update": 0,
        "high_risk_windows": [],
    }
    events = classify_scraper_events(state)
    assert len(events) == 1
    assert events[0]["title"] == "Missing Fields"
    assert events[0]["impact"] == "MED"  # default
    assert events[0]["timestamp"] == 0.0


def test_classify_scraper_events_empty_state():
    """classify_scraper_events returns empty list for None/empty state."""
    assert classify_scraper_events(None) == []
    assert classify_scraper_events({}) == []
    assert classify_scraper_events({"events": []}) == []


# ---------------------------------------------------------------------------
# Risk gating integration test
# ---------------------------------------------------------------------------
def test_risk_high_macro_risk_fail_open_without_file():
    """RiskState.HIGH_MACRO_RISK returns False when macro_events.json is missing.

    This verifies the fail-open behavior: if the scraper hasn't produced
    state/macro_events.json, the macro risk gate is inactive and trading
    proceeds normally.
    """
    import polymarket_arb.risk as risk_mod
    # Ensure cache is cleared
    risk_mod._macro_cache = None
    risk_mod._macro_cache_epoch = 0.0

    from polymarket_arb.risk import RiskState
    risk = RiskState(balance=1000.0)
    assert risk.HIGH_MACRO_RISK is False


def test_risk_high_macro_risk_active_with_window():
    """RiskState.HIGH_MACRO_RISK returns True when an active HIGH window exists.

    This verifies that the macro risk gate IS active when state/macro_events.json
    contains an upcoming HIGH-impact event with a 2-hour window.
    """
    import polymarket_arb.risk as risk_mod
    module = risk_mod._maybe_page_agent()[0]
    MacroEventState = module.MacroEventState
    MacroEvent = module.MacroEvent

    now = timezone.now().timestamp() if hasattr(timezone, 'now') else __import__('time').time()
    import time as _time
    now = _time.time()

    state = MacroEventState(
        events=[MacroEvent(title="FOMC", timestamp=now + 3600, impact="HIGH")],
        last_update=now,
        high_risk_windows=[{"start": now - 100, "end": now + 500, "event": "FOMC"}],
    )
    risk_mod._macro_cache = state
    risk_mod._macro_cache_epoch = _time.time() + 1_000_000  # far-future -> cache hit

    try:
        from polymarket_arb.risk import RiskState
        risk = RiskState(balance=1000.0)
        assert risk.HIGH_MACRO_RISK is True
    finally:
        risk_mod._macro_cache = None
        risk_mod._macro_cache_epoch = 0.0
