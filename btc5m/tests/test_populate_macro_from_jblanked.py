from __future__ import annotations

from datetime import datetime, timezone

import pytest

from scripts.populate_macro_from_jblanked import (
    SOURCE_TAG,
    _parse_jblanked_dt,
    build_state_from_jblanked_records,
    compare_high_impact_week,
    fetch_jblanked_records,
    main,
)


def _record(name: str, date: str, impact: str, currency: str = "USD") -> dict:
    return {
        "Name": name,
        "Date": date,
        "Impact": impact,
        "Currency": currency,
        "Category": "Test",
        "Forecast": "1.0",
        "Previous": "0.9",
    }


def test_parse_jblanked_date_uses_documented_gmt_plus_3_default():
    ts = _parse_jblanked_dt("2026.08.03 15:30:00")
    expected = datetime(2026, 8, 3, 12, 30, tzinfo=timezone.utc).timestamp()
    assert ts == expected


def test_build_state_is_source_tagged_and_has_future_high_windows():
    now = datetime(2026, 8, 3, tzinfo=timezone.utc).timestamp()
    state, stats = build_state_from_jblanked_records(
        [
            _record("CPI y/y", "2026.08.04 15:30:00", "Low"),
            _record("Minor survey", "2026.08.04 16:00:00", "Low"),
        ],
        now,
    )
    assert stats["by_impact"] == {"HIGH": 1, "MED": 0, "LOW": 1}
    assert len(state.high_risk_windows) == 1
    assert all(event.source == SOURCE_TAG for event in state.events)


def test_same_week_high_impact_comparison_reports_matches_and_disagreements():
    now = datetime(2026, 8, 5, 12, tzinfo=timezone.utc).timestamp()
    ff = [
        {"title": "CPI y/y", "date": "2026-08-06T08:30:00-04:00", "impact": "High"},
        {"title": "FOMC Statement", "date": "2026-08-05T14:00:00-04:00", "impact": "High"},
        {"title": "Prior Week CPI", "date": "2026-07-31T08:30:00-04:00", "impact": "High"},
    ]
    jblanked = [
        _record("CPI y/y", "2026.08.06 15:30:00", "High"),
        _record("ECB Interest Rate Decision", "2026.08.06 16:15:00", "High", "EUR"),
    ]
    report = compare_high_impact_week(ff, jblanked, now)
    assert report["week_start_utc"] == "2026-08-03"
    assert report["ff_high_count"] == 2
    assert report["jblanked_high_count"] == 2
    assert report["high_impact_agreement"] == [{"date": "2026-08-06", "title": "CPI y/y"}]
    assert report["ff_only_high_impact"] == [{"date": "2026-08-05", "title": "FOMC Statement"}]
    assert report["jblanked_only_high_impact"] == [
        {"date": "2026-08-06", "title": "ECB Interest Rate Decision"}
    ]


def test_jblanked_fetch_requires_opt_in_api_key(monkeypatch):
    monkeypatch.delenv("JBLANKED_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="JBLANKED_API_KEY"):
        fetch_jblanked_records()


def test_cli_missing_key_returns_a_concise_error(monkeypatch, capsys):
    monkeypatch.delenv("JBLANKED_API_KEY", raising=False)
    assert main(["--compare-ff"]) == 2
    assert "JBLANKED_API_KEY" in capsys.readouterr().err
