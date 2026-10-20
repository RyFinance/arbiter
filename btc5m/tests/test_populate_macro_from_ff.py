"""Tests for scripts/populate_macro_from_ff.py.

Covers:
  - Impact classification logic (FF-impact-only events, keyword-override events,
    empty-impact fallback, country tags).
  - High-risk-window computation (future HIGH events only, ±1h window around each,
    past HIGH events excluded).
  - Staleness guard (fresh file → OK, stale file → WARNING, missing file → stale,
    LSE-sourced file → not stale — different refresh schedule, malformed JSON → stale).
  - Schema round-trip (build_state_from_ff_records → to_dict reparseable by
    MacroEventState.from_dict).
  - CLI flag dispatch: --check-stale short-circuits and never fetches.

These tests use the pure functions (build_state_from_ff_records / check_stale /
_classify_impact / _parse_dt) and do NOT hit the network.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from scripts.populate_macro_from_ff import (  # noqa: E402
    SOURCE_TAG,
    MAX_STALENESS_HOURS_DEFAULT,
    _classify_impact,
    _parse_dt,
    build_state_from_ff_records,
    check_stale,
    main,
)
from scrapers.page_agent_runner import MacroEventState  # noqa: E402


# ---------- _classify_impact --------------------------------------------------


def test_classify_impact_keyword_overrides_ff_low():
    # keywords trump FF impact ratings — "CPI" always HIGH even if FF rated it Low
    assert _classify_impact("USD CPI y/y", "low") == "HIGH"
    assert _classify_impact("Core PCE Price Index m/m", "low") == "HIGH"
    assert _classify_impact("FOMC Member Speech", "medium") == "HIGH"
    assert _classify_impact("EBP Trade Balance", "low") == "HIGH"  # 'trade balance is HIGH'


def test_classify_impact_keyword_substring_unemployment_matches_employment():
    # "employment" keyword matches "Spanish Unemployment Change" → HIGH even with FF=medium.
    # This documents the keyword-table's substring-scan behavior (not whole-word match).
    assert _classify_impact("Spanish Unemployment Change", "medium") == "HIGH"


def test_classify_impact_uses_ff_label_when_no_keyword_match():
    assert _classify_impact("Spanish Unemployment Change", "high") == "HIGH"
    # "Average Hourly Earnings" matches no HIGH keyword, so the FF "medium" label
    # decides → MED
    assert _classify_impact("Average Hourly Earnings m/m", "medium") == "MED"
    assert _classify_impact("Baker Hughes Oil Rig Count", "low") == "LOW"


def test_classify_impact_empty_or_unknown_falls_back_to_med():
    assert _classify_impact("", "") == "MED"
    assert _classify_impact("Unknown Event", "") == "MED"
    assert _classify_impact("Unknown Event", "weird-label") == "MED"
    assert _classify_impact(None, None) == "MED"


def test_classify_impact_case_insensitive():
    assert _classify_impact("USD cpi y/y", "HIGH") == "HIGH"
    assert _classify_impact("ISM Manufacturing PMI", "high") == "HIGH"
    assert _classify_impact("Core pce Price Index", "LOW") == "HIGH"


# ---------- _parse_dt --------------------------------------------------------


def test_parse_dt_iso_with_offset():
    # FF date field: ISO8601 with timezone offset. 10:00 EDT == 14:00 UTC.
    ts = _parse_dt("2026-08-03T10:00:00-04:00")
    assert ts == pytest.approx(1785765600.0, abs=1.0)


def test_parse_dt_utc():
    ts = _parse_dt("2026-08-03T14:00:00+00:00")
    assert ts == pytest.approx(1785765600.0, abs=1.0)


def test_parse_dt_invalid_string_raises():
    with pytest.raises(Exception):
        _parse_dt("not a date at all")


# ---------- build_state_from_ff_records --------------------------------------


def _ff_rec(title, when_iso, impact, country="USD",
            forecast="0.5%", previous="0.4%"):
    return {
        "title": title, "date": when_iso, "impact": impact, "country": country,
        "forecast": forecast, "previous": previous,
    }


def test_build_state_basic_counts_and_source_tag():
    now = datetime(2026, 8, 3, 0, 0, tzinfo=timezone.utc).timestamp()
    records = [
        _ff_rec("CPI y/y",        "2026-08-03T10:00:00-04:00", "high",   "USD"),
        _ff_rec("Test Low Event", "2026-08-04T08:30:00-04:00", "low",    "EUR"),
        _ff_rec("BoJ Rate Decision", "2026-08-05T03:00:00+09:00", "low", "JPY"),
    ]
    state, stats = build_state_from_ff_records(records, now_ts=now)
    assert stats["events"] == 3
    assert stats["source_tag"] == SOURCE_TAG
    # CPI and BoJ Rate Decision classify HIGH (both keywords); the EUR Low Medium-tag
    # event has no HIGH keyword → MED (FF label fallback intended to be `low` here)
    assert stats["by_impact"]["HIGH"] == 2
    assert all(e.source == SOURCE_TAG for e in state.events)
    # events sorted ascending by timestamp
    assert state.events == sorted(state.events, key=lambda e: e.timestamp)


def test_build_state_high_risk_windows_only_future_high():
    # `now` sits between two HIGH-event timestamps
    now = datetime(2026, 8, 5, 0, 0, tzinfo=timezone.utc).timestamp()
    past_high_ts  = datetime(2026, 8, 3, 14, 0, tzinfo=timezone.utc)
    future_high_ts = datetime(2026, 8, 7, 14, 0, tzinfo=timezone.utc)
    records = [
        _ff_rec("Past CPI",    past_high_ts.isoformat(),    "high", "USD"),
        _ff_rec("Future CPI",  future_high_ts.isoformat(),  "high", "USD"),
        _ff_rec("Low event",   (future_high_ts).isoformat(),"low",  "EUR"),
    ]
    state, stats = build_state_from_ff_records(records, now_ts=now)
    # Only the FUTURE HIGH event should produce a window
    assert stats["high_risk_windows"] == 1
    window = state.high_risk_windows[0]
    assert window["event"] == "Future CPI"
    assert window["start"] == future_high_ts.timestamp() - 3600
    assert window["end"]   == future_high_ts.timestamp() + 3600


def test_build_state_high_risk_window_width_exactly_two_hours():
    now = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc).timestamp()
    evt_ts = datetime(2026, 8, 9, 0, 30, tzinfo=timezone.utc)
    records = [_ff_rec("CPI y/y", evt_ts.isoformat(), "high", "USD")]
    state, _ = build_state_from_ff_records(records, now_ts=now)
    w = state.high_risk_windows[0]
    assert w["end"] - w["start"] == 2 * 3600  # exactly 2h
    assert w["start"] == evt_ts.timestamp() - 3600
    assert w["end"]   == evt_ts.timestamp() + 3600


def test_build_state_skips_records_without_title():
    now = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc).timestamp()
    records = [
        {"title": "", "date": "2026-08-10T14:00:00+00:00", "impact": "high"},
        {"date": "2026-08-10T14:00:00+00:00", "impact": "high"},  # no title key at all
        _ff_rec("Real Event", "2026-08-10T14:00:00+00:00", "high", "USD"),
    ]
    state, stats = build_state_from_ff_records(records, now_ts=now)
    assert stats["events"] == 1
    assert state.events[0].title == "Real Event"


def test_build_state_description_includes_country_forecast_previous():
    now = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc).timestamp()
    records = [_ff_rec("CPI y/y",
                       "2026-08-09T10:00:00-04:00", "high",
                       country="US", forecast="2.1%", previous="2.0%")]
    state, _ = build_state_from_ff_records(records, now_ts=now)
    desc = state.events[0].description
    assert "US" in desc
    assert "F: 2.1%" in desc
    assert "P: 2.0%" in desc


def test_build_state_description_handles_missing_fields():
    now = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc).timestamp()
    rec = {"title": "CPI y/y", "date": "2026-08-09T10:00:00-04:00",
           "impact": "high", "country": "", "forecast": "", "previous": ""}
    state, _ = build_state_from_ff_records([rec], now_ts=now)
    desc = state.events[0].description
    # No country, no forecast, no previous → empty description with no separators
    assert desc == ""


def test_build_state_last_update_recorded():
    now = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc).timestamp()
    state, _ = build_state_from_ff_records([], now_ts=now)
    assert state.last_update == now


def test_build_state_roundtrips_through_MacroEventState():
    now = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc).timestamp()
    records = [
        _ff_rec("CPI y/y", "2026-08-09T10:00:00-04:00", "high", "USD"),
        _ff_rec("Spanish Unemployment Change",
                "2026-08-04T08:00:00+02:00", "high", "ES"),
    ]
    state, _ = build_state_from_ff_records(records, now_ts=now)
    d = state.to_dict()
    recovered = MacroEventState.from_dict(d)
    assert len(recovered.events) == len(state.events)
    assert recovered.high_risk_windows == state.high_risk_windows
    assert recovered.last_update == state.last_update
    # source tag survives the roundtrip
    assert all(e.source == SOURCE_TAG for e in recovered.events)


# ---------- check_stale ------------------------------------------------------


def _write_state_file(path, *, source=SOURCE_TAG, last_update, events_n=3):
    """Helper: write a minimal macro_events.json-shaped file."""
    events = [{f"event{i}": "",
               "timestamp": time.time(), "impact": "MED",
               "source": source, "title": f"Event {i}", "description": "",
               "url": ""} for i in range(events_n)]
    with open(path, "w") as f:
        json.dump({"source": source,
                   "events": events,
                   "last_update": last_update,
                   "high_risk_windows": []}, f)


def test_check_stale_fresh_file_returns_false(tmp_path):
    p = tmp_path / "macro.json"
    now = time.time()
    _write_state_file(p, last_update=now, events_n=3, source=SOURCE_TAG)
    is_stale, age_hours = check_stale(path=str(p), now_ts=now)
    assert is_stale is False
    assert age_hours == pytest.approx(0.0, abs=0.5)


def test_check_stale_stale_file_returns_true(tmp_path):
    p = tmp_path / "macro.json"
    now = time.time()
    _write_state_file(p, last_update=now - 48 * 3600,
                      events_n=3, source=SOURCE_TAG)
    is_stale, age_hours = check_stale(path=str(p), max_staleness_hours=24.0,
                                      now_ts=now)
    assert is_stale is True
    assert age_hours == pytest.approx(48.0, abs=0.5)


def test_check_stale_just_under_threshold_is_fresh(tmp_path):
    p = tmp_path / "macro.json"
    now = time.time()
    _write_state_file(p, last_update=now - 23 * 3600,
                      events_n=3, source=SOURCE_TAG)
    is_stale, age_hours = check_stale(path=str(p), max_staleness_hours=24.0,
                                      now_ts=now)
    assert is_stale is False
    assert age_hours == pytest.approx(23.0, abs=0.5)


def test_check_stale_missing_file_considered_stale(tmp_path):
    p = tmp_path / "does_not_exist.json"
    is_stale, age_hours = check_stale(path=str(p))
    assert is_stale is True
    assert age_hours == float("inf")


def test_check_stale_malformed_json_is_stale(tmp_path):
    p = tmp_path / "broken.json"
    p.write_text("{ this is not valid json")
    is_stale, _ = check_stale(path=str(p))
    assert is_stale is True


def test_check_stale_lse_sourced_file_is_not_stale_so_we_dont_overstep(tmp_path):
    # A file sourced from the LSE scraper should be left to the LSE scraper's refresh
    # schedule; the FF staleness guard must not flag it stale.
    p = tmp_path / "lse_sourced.json"
    now = time.time()
    _write_state_file(p, last_update=now - 72 * 3600,
                      events_n=3, source="lse_calendar_rest")
    is_stale, _ = check_stale(path=str(p), max_staleness_hours=24.0, now_ts=now)
    assert is_stale is False


def test_check_stale_file_with_no_events_field_still_classified_by_absence(tmp_path):
    p = tmp_path / "empty_state.json"
    with open(p, "w") as f:
        json.dump({"events": [], "last_update": time.time() - 100 * 3600,
                   "high_risk_windows": []}, f)
    # Empty events list → no source tag to identify → guard treats it as not-FF and
    # not-stale (the LSE scraper writes empty events when it cannot fetch).
    is_stale, age_hours = check_stale(path=str(p), max_staleness_hours=24.0)
    assert is_stale is False
    assert age_hours == 0.0


def test_check_stale_uses_root_source_for_an_empty_ff_state(tmp_path):
    p = tmp_path / "empty_ff_state.json"
    now = time.time()
    p.write_text(json.dumps({
        "source": SOURCE_TAG,
        "events": [],
        "last_update": now - 25 * 3600,
        "high_risk_windows": [],
    }))
    is_stale, age_hours = check_stale(path=str(p), now_ts=now)
    assert is_stale is True
    assert age_hours == pytest.approx(25.0, abs=0.5)


def test_check_stale_default_threshold_is_24h(tmp_path):
    p = tmp_path / "m.json"
    now = time.time()
    _write_state_file(p, last_update=now - 25 * 3600,
                      events_n=3, source=SOURCE_TAG)
    is_stale, _ = check_stale(path=str(p), now_ts=now)
    # 25h > 24h default → stale even with no explicit threshold
    assert is_stale is True


# ---------- CLI dispatch: --check-stale short-circuits, never fetches --------


def test_cli_check_stale_flag_runs_check_and_returns_zero(tmp_path, capsys, monkeypatch):
    p = tmp_path / "macro.json"
    now = time.time()
    _write_state_file(p, last_update=now - 48 * 3600,
                      events_n=3, source=SOURCE_TAG)
    # remove any chance the real fetch path is taken
    def _never_fetch():
        raise AssertionError("main() must not fetch when --check-stale is passed")
    monkeypatch.setattr("scripts.populate_macro_from_ff.fetch_ff_records", _never_fetch)
    rc = main(["--check-stale", "--path", str(p), "--max-staleness-hours", "24"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "STALE" in captured.out
    assert "Re-run" in captured.out


def test_cli_check_stale_fresh_prints_ok_and_returns_zero(tmp_path, capsys, monkeypatch):
    p = tmp_path / "macro.json"
    now = time.time()
    _write_state_file(p, last_update=now, events_n=3, source=SOURCE_TAG)
    def _never_fetch():
        raise AssertionError("must not fetch on --check-stale")
    monkeypatch.setattr("scripts.populate_macro_from_ff.fetch_ff_records", _never_fetch)
    rc = main(["--check-stale", "--path", str(p)])
    assert rc == 0
    assert "OK:" in capsys.readouterr().out


def test_cli_check_stale_missing_file_logs_warning_but_returns_zero(tmp_path, capsys, monkeypatch):
    p = tmp_path / "no_such_file.json"
    def _never_fetch():
        raise AssertionError("must not fetch on --check-stale")
    monkeypatch.setattr("scripts.populate_macro_from_ff.fetch_ff_records", _never_fetch)
    rc = main(["--check-stale", "--path", str(p)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "missing" in out.lower() or "unreadable" in out.lower()


def test_cli_default_run_invokes_fetch_and_writes_state(tmp_path, monkeypatch):
    p = tmp_path / "macro.json"
    fake_records = [
        _ff_rec("CPI y/y", "2026-08-09T10:00:00-04:00", "high", "USD"),
        _ff_rec("Rubbish Event m/m", "2026-08-05T08:00:00-04:00", "low", "XY"),
    ]
    monkeypatch.setattr("scripts.populate_macro_from_ff.fetch_ff_records",
                        lambda: fake_records)
    monkeypatch.setattr("scripts.populate_macro_from_ff.time.time",
                        lambda: datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc).timestamp())
    rc = main(["--path", str(p)])
    assert rc == 0
    with open(p) as f:
        d = json.load(f)
    assert d["source"] == SOURCE_TAG
    assert len(d["events"]) == 2
    assert d["events"][0]["source"] == SOURCE_TAG
    # CPI is HIGH (keyword), Rubbish+Low → classified LOW
    impacts = {e["title"]: e["impact"] for e in d["events"]}
    assert impacts["CPI y/y"] == "HIGH"
    assert impacts["Rubbish Event m/m"] == "LOW"
    # CPI is in the future → produces a high-risk window
    assert len(d["high_risk_windows"]) == 1


def test_cli_check_stale_with_lse_source_does_not_warn(tmp_path, capsys, monkeypatch):
    p = tmp_path / "lse.json"
    now = time.time()
    _write_state_file(p, last_update=now - 100 * 3600,
                      events_n=3, source="lse_calendar_rest")
    def _never_fetch():
        raise AssertionError("must not fetch on --check-stale")
    monkeypatch.setattr("scripts.populate_macro_from_ff.fetch_ff_records", _never_fetch)
    rc = main(["--check-stale", "--path", str(p)])
    assert rc == 0
    out = capsys.readouterr().out
    # The LSE-sourced file is not flagged as stale (it's not the FF script's concern)
    assert "STALE" not in out
