"""Tests for the Macro & Vol Regime tab helpers in streamlit_app.py.

Covers the pure, Streamlit-free helpers:
  * load_macro_events      — reads state/macro_events.json (or empty shell when missing)
  * get_high_macro_risk    — delegates to RiskState.HIGH_MACRO_RISK (fail-open False)
  * get_vol_regime         — reads kronos_session.SESSION_VOL_REGIME
  * parse_pnl_curve        — extracts locked_pnl / residual_pnl from metrics samples
  * _parse_session_time    — HH:MM:SS.mmm -> fractional seconds
"""
from __future__ import annotations

import json
import time

import pandas as pd
import pytest

from streamlit_app import (
    load_macro_events,
    get_high_macro_risk,
    get_vol_regime,
    parse_pnl_curve,
    _parse_session_time,
    MACRO_EVENTS_PATH,
)


# ---------------------------------------------------------------------------
# _parse_session_time
# ---------------------------------------------------------------------------
def test_parse_session_time_basic():
    assert _parse_session_time("00:00:00.000") == 0.0


def test_parse_session_time_partial():
    assert _parse_session_time("00:00:01.500") == pytest.approx(1.5)


def test_parse_session_time_full():
    # 01:02:03.250 -> 3600 + 120 + 3 + 0.25
    assert _parse_session_time("01:02:03.250") == pytest.approx(3723.25)


def test_parse_session_time_empty():
    assert _parse_session_time("") == 0.0
    assert _parse_session_time(None) == 0.0


def test_parse_session_time_malformed():
    assert _parse_session_time("garbage") == 0.0
    assert _parse_session_time("12:00") == 0.0


def test_parse_session_time_two_digit_ms():
    # "00:00:01.50" -> millisecond "50" parsed as int("50")/1000
    assert _parse_session_time("00:00:01.50") == pytest.approx(1.05)


# ---------------------------------------------------------------------------
# parse_pnl_curve
# ---------------------------------------------------------------------------
def _metrics_event(t: str, locked: float, residual: float) -> dict:
    """Build a bot-log event dict whose msg contains a parseable metrics line.

    NOTE: must match METRIC_RE in streamlit_app.py exactly so the sample
    carries all the regex-named keys (loops, opps, ... would_lock).
    """
    msg = (
        "session metrics: loops=10 opps=5 sent=2 fills=1 partial=0 resolved=0 "
        f"missed=0 opp2order_avg_ms=12.34 p50_ms=11.00 ack_avg_ms=10.00 "
        f"locked_pnl={locked:.4f} residual_pnl={residual:.4f} "
        f"session_pnl={locked + residual:.4f} would_lock=0.0000 "
        "bandfills[fresh=0/0 0.5-2s=0/0 2-10s=0/0 stale=0/0]"
    )
    return {"t": t, "event": "INFO", "msg": msg, "data": {}}


def test_parse_pnl_curve_empty():
    assert parse_pnl_curve([]).empty


def test_parse_pnl_curve_no_metrics_lines():
    events = [
        {"t": "00:00:00.000", "event": "INFO", "msg": "polymarket arb bot starting"},
        {"t": "00:00:01.000", "event": "TRADE", "msg": "filled", "data": {}},
    ]
    assert parse_pnl_curve(events).empty


def test_parse_pnl_curve_single_sample():
    events = [_metrics_event("00:00:05.123", 1.5, -0.3)]
    df = parse_pnl_curve(events)
    assert not df.empty
    assert len(df) == 1
    assert df.iloc[0]["locked_pnl"] == 1.5
    assert df.iloc[0]["residual_pnl"] == -0.3
    assert df.iloc[0]["t_sec"] == pytest.approx(5.123)


def test_parse_pnl_curve_multiple_samples():
    events = [
        _metrics_event("00:00:00.000", 0.0, 0.0),
        _metrics_event("00:00:10.000", 2.0, -0.5),
        _metrics_event("00:00:20.000", 3.5, 0.1),
    ]
    df = parse_pnl_curve(events)
    assert len(df) == 3
    assert list(df["locked_pnl"]) == [0.0, 2.0, 3.5]
    assert list(df["residual_pnl"]) == [0.0, -0.5, 0.1]
    assert df["t_sec"].is_monotonic_increasing


def test_parse_pnl_curve_ignores_unrelated_events():
    events = [
        {"t": "00:00:00.000", "event": "INFO", "msg": "boot"},
        _metrics_event("00:00:01.000", 1.0, 0.0),
        {"t": "00:00:02.000", "event": "TRADE", "msg": "filled", "data": {}},
        _metrics_event("00:00:03.000", 1.5, 0.2),
    ]
    df = parse_pnl_curve(events)
    assert len(df) == 2  # only the two metrics lines


def test_parse_pnl_curve_malformed_metrics_line():
    """A 'session metrics:' line that doesn't match METRIC_RE is skipped."""
    events = [
        {"t": "00:00:00.000", "event": "INFO", "msg": "session metrics: garbage"},
    ]
    assert parse_pnl_curve(events).empty


# ---------------------------------------------------------------------------
# get_vol_regime
# ---------------------------------------------------------------------------
def test_get_vol_regime_returns_known_value():
    """SESSION_VOL_REGIME is one of the documented regimes."""
    regime = get_vol_regime()
    assert regime in ("HIGH", "MED", "LOW")


# ---------------------------------------------------------------------------
# get_high_macro_risk
# ---------------------------------------------------------------------------
def test_get_high_macro_risk_returns_bool():
    """HIGH_MACRO_RISK resolves to a bool and fails open when no macro file exists."""
    val = get_high_macro_risk()
    assert isinstance(val, bool)
    # No macro_events.json present by default in tests -> fail-open to False.
    assert val is False


def test_get_high_macro_risk_true_when_window_active(tmp_path, monkeypatch):
    """With an active HIGH window in state/macro_events.json, HIGH_MACRO_RISK is True."""
    monkeypatch.setattr("streamlit_app.MACRO_EVENTS_PATH", tmp_path / "macro_events.json")
    now = time.time()
    state = {
        "events": [
            {
                "title": "HIGH-FOMC",
                "timestamp": now,
                "impact": "HIGH",
                "description": "test",
                "url": "",
                "source": "lse_calendar",
            }
        ],
        "last_update": now,
        "high_risk_windows": [
            {"start": now - 100, "end": now + 500, "event": "HIGH-FOMC"},
        ],
    }
    with open(tmp_path / "macro_events.json", "w") as f:
        json.dump(state, f)

    # Re-load the macro state into risk.py's cache so _high_macro_risk picks it up.
    import polymarket_arb.risk as risk_mod
    module = risk_mod._maybe_page_agent()[0]
    assert module is not None
    MacroEventState = module.MacroEventState
    risk_mod._macro_cache = MacroEventState.from_dict(state)
    risk_mod._macro_cache_epoch = time.time() + 1_000_000  # far-future -> cache hit
    try:
        assert get_high_macro_risk() is True
    finally:
        risk_mod._macro_cache = None
        risk_mod._macro_cache_epoch = 0.0


# ---------------------------------------------------------------------------
# load_macro_events
# ---------------------------------------------------------------------------
def _clear_cache():
    """`load_macro_events` is wrapped in @st.cache_data; monkeypatching the module-level
    MACRO_EVENTS_PATH does not invalidate the cache, so clear it before each such test."""
    import streamlit_app as sa
    sa.st.cache_data.clear()


def test_load_macro_events_missing_file(tmp_path, monkeypatch):
    """When state/macro_events.json is absent, returns an empty-shell dict."""
    _clear_cache()
    monkeypatch.setattr("streamlit_app.MACRO_EVENTS_PATH", tmp_path / "does_not_exist.json")
    result = load_macro_events()
    assert result == {"events": [], "last_update": 0.0, "high_risk_windows": []}


def test_load_macro_events_valid_file(tmp_path, monkeypatch):
    _clear_cache()
    payload = {
        "events": [{"title": "X", "timestamp": 1.0, "impact": "HIGH"}],
        "last_update": 1.0,
        "high_risk_windows": [{"start": 0.0, "end": 10.0, "event": "X"}],
    }
    f = tmp_path / "macro_events.json"
    f.write_text(json.dumps(payload))
    monkeypatch.setattr("streamlit_app.MACRO_EVENTS_PATH", f)
    result = load_macro_events()
    assert result["last_update"] == 1.0
    assert len(result["events"]) == 1
    assert len(result["high_risk_windows"]) == 1


def test_load_macro_events_corrupt_file(tmp_path, monkeypatch):
    """A corrupt macro_events.json yields the empty-shell dict (no exception)."""
    _clear_cache()
    f = tmp_path / "macro_events.json"
    f.write_text("{not valid json")
    monkeypatch.setattr("streamlit_app.MACRO_EVENTS_PATH", f)
    result = load_macro_events()
    assert result == {"events": [], "last_update": 0.0, "high_risk_windows": []}
