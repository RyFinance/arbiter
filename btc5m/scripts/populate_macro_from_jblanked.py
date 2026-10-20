"""Optional JBlanked-backed macro-calendar fallback.

This script is intentionally independent from the primary public Forex Factory
refresh. It only calls JBlanked when ``JBLANKED_API_KEY`` is configured and it
never writes ``state/macro_events.json`` unless ``--write-state`` is supplied.

``--compare-ff`` fetches both sources during the same run and reports exact
same-week HIGH-impact title/date agreement plus each source's unmatched events.
JBlanked documents that its week endpoint is populated day by day and can omit
future days, so a comparison is an audit signal, not a replacement guarantee.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from scrapers.page_agent_runner import MacroEvent, MacroEventState, _HIGH_IMPACT_CATEGORIES  # noqa: E402


JBLANKED_API_KEY_ENV = "JBLANKED_API_KEY"
JBLANKED_WEEK_URL = "https://www.jblanked.com/news/api/forex-factory/calendar/week/"
FF_WEEK_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
SOURCE_TAG = "jblanked_forex_factory"
JBLANKED_DEFAULT_TIMEZONE = timezone(timedelta(hours=3))


def _classify_impact(title: str, source_impact: str) -> str:
    normalized_title = (title or "").lower()
    normalized_impact = (source_impact or "").lower()
    if any(keyword in normalized_title for keyword in _HIGH_IMPACT_CATEGORIES):
        return "HIGH"
    if normalized_impact == "high":
        return "HIGH"
    if normalized_impact == "medium":
        return "MED"
    if normalized_impact == "low":
        return "LOW"
    return "MED"


def _parse_jblanked_dt(value: str) -> float:
    """Parse JBlanked dates, whose documented default timezone is GMT+3."""
    value = (value or "").strip()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        parsed = datetime.strptime(value, "%Y.%m.%d %H:%M:%S")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=JBLANKED_DEFAULT_TIMEZONE)
    return parsed.timestamp()


def _parse_ff_dt(value: str) -> float:
    return datetime.fromisoformat((value or "").strip()).timestamp()


def _jblanked_record_to_event(record: dict[str, Any]) -> MacroEvent:
    title = str(record.get("Name") or "").strip()
    if not title:
        raise ValueError("JBlanked record is missing Name")
    currency = str(record.get("Currency") or "").strip()
    category = str(record.get("Category") or "").strip()
    forecast = record.get("Forecast")
    previous = record.get("Previous")
    description = " | ".join(
        str(value) for value in (currency, category, forecast, previous)
        if value not in (None, "")
    )
    return MacroEvent(
        title=title,
        timestamp=_parse_jblanked_dt(str(record.get("Date") or "")),
        impact=_classify_impact(title, str(record.get("Impact") or "")),
        description=description,
        url="",
        source=SOURCE_TAG,
    )


def build_state_from_jblanked_records(
    records: Iterable[dict[str, Any]], now_ts: float,
) -> tuple[MacroEventState, dict[str, Any]]:
    events = [_jblanked_record_to_event(record) for record in records if record.get("Name")]
    events.sort(key=lambda event: event.timestamp)
    windows = [
        {"start": event.timestamp - 3600, "end": event.timestamp + 3600, "event": event.title}
        for event in events
        if event.impact == "HIGH" and event.timestamp > now_ts
    ]
    state = MacroEventState(events=events, last_update=float(now_ts), high_risk_windows=windows)
    by_impact = {"HIGH": 0, "MED": 0, "LOW": 0}
    for event in events:
        by_impact[event.impact] = by_impact.get(event.impact, 0) + 1
    return state, {
        "events": len(events),
        "by_impact": by_impact,
        "high_risk_windows": len(windows),
        "source_tag": SOURCE_TAG,
    }


def fetch_jblanked_records(api_key: str | None = None) -> list[dict[str, Any]]:
    api_key = api_key or os.getenv(JBLANKED_API_KEY_ENV)
    if not api_key:
        raise RuntimeError(f"{JBLANKED_API_KEY_ENV} is required for the optional JBlanked fallback")
    request = urllib.request.Request(
        JBLANKED_WEEK_URL,
        headers={
            "Accept": "application/json",
            "Authorization": f"Api-Key {api_key}",
            "User-Agent": "polymarket-arb-macro-fallback/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"JBlanked calendar request failed with HTTP {exc.code}: {body}") from exc
    if not isinstance(payload, list):
        raise ValueError(f"JBlanked returned {type(payload).__name__}, expected a JSON list")
    return payload


def fetch_ff_records() -> list[dict[str, Any]]:
    request = urllib.request.Request(FF_WEEK_URL, headers={"User-Agent": "polymarket-arb-macro-audit/1.0"})
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.loads(response.read())
    if not isinstance(payload, list):
        raise ValueError(f"Forex Factory returned {type(payload).__name__}, expected a JSON list")
    return payload


def _event_key(event: MacroEvent) -> tuple[str, str]:
    event_date = datetime.fromtimestamp(event.timestamp, tz=timezone.utc).date().isoformat()
    normalized_title = re.sub(r"[^a-z0-9]+", " ", event.title.lower()).strip()
    return event_date, normalized_title


def _ff_record_to_event(record: dict[str, Any]) -> MacroEvent:
    title = str(record.get("title") or "").strip()
    if not title:
        raise ValueError("Forex Factory record is missing title")
    return MacroEvent(
        title=title,
        timestamp=_parse_ff_dt(str(record.get("date") or "")),
        impact=_classify_impact(title, str(record.get("impact") or "")),
        source="forex_factory_public",
    )


def _current_week_events(events: Iterable[MacroEvent], now_ts: float) -> list[MacroEvent]:
    current = datetime.fromtimestamp(now_ts, tz=timezone.utc).date()
    week_start = current - timedelta(days=current.weekday())
    week_end = week_start + timedelta(days=6)
    return [
        event for event in events
        if week_start <= datetime.fromtimestamp(event.timestamp, tz=timezone.utc).date() <= week_end
    ]


def _render_event(event: MacroEvent) -> dict[str, str]:
    return {
        "date": datetime.fromtimestamp(event.timestamp, tz=timezone.utc).date().isoformat(),
        "title": event.title,
    }


def compare_high_impact_week(
    ff_records: Iterable[dict[str, Any]],
    jblanked_records: Iterable[dict[str, Any]],
    now_ts: float,
) -> dict[str, Any]:
    ff_events = _current_week_events((_ff_record_to_event(record) for record in ff_records if record.get("title")), now_ts)
    jblanked_events = _current_week_events(
        (_jblanked_record_to_event(record) for record in jblanked_records if record.get("Name")), now_ts
    )
    ff_high = {_event_key(event): event for event in ff_events if event.impact == "HIGH"}
    jblanked_high = {_event_key(event): event for event in jblanked_events if event.impact == "HIGH"}
    shared = sorted(ff_high.keys() & jblanked_high.keys())
    ff_only = sorted(ff_high.keys() - jblanked_high.keys())
    jblanked_only = sorted(jblanked_high.keys() - ff_high.keys())
    current = datetime.fromtimestamp(now_ts, tz=timezone.utc).date()
    week_start = current - timedelta(days=current.weekday())
    return {
        "week_start_utc": week_start.isoformat(),
        "week_end_utc": (week_start + timedelta(days=6)).isoformat(),
        "ff_high_count": len(ff_high),
        "jblanked_high_count": len(jblanked_high),
        "high_impact_agreement": [_render_event(ff_high[key]) for key in shared],
        "ff_only_high_impact": [_render_event(ff_high[key]) for key in ff_only],
        "jblanked_only_high_impact": [_render_event(jblanked_high[key]) for key in jblanked_only],
    }


def _write_state(state: MacroEventState, path: str) -> None:
    payload = state.to_dict()
    payload["source"] = SOURCE_TAG
    with open(path, "w") as handle:
        json.dump(payload, handle, indent=2)


def _print_comparison(report: dict[str, Any]) -> None:
    print(f"Week (UTC): {report['week_start_utc']} through {report['week_end_utc']}")
    print(f"HIGH impact: FF={report['ff_high_count']} JBlanked={report['jblanked_high_count']}")
    print(f"Agreement: {len(report['high_impact_agreement'])}")
    for event in report["high_impact_agreement"]:
        print(f"  MATCH {event['date']} | {event['title']}")
    for event in report["ff_only_high_impact"]:
        print(f"  FF ONLY {event['date']} | {event['title']}")
    for event in report["jblanked_only_high_impact"]:
        print(f"  JBLANKED ONLY {event['date']} | {event['title']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Optional JBlanked macro-calendar fallback and FF comparator")
    parser.add_argument("--write-state", metavar="PATH", help="Explicitly write JBlanked state to PATH")
    parser.add_argument("--compare-ff", action="store_true", help="Compare same-week HIGH-impact events with live FF")
    parser.add_argument("--report-json", metavar="PATH", help="Write --compare-ff output to PATH")
    args = parser.parse_args(argv)
    if args.report_json and not args.compare_ff:
        parser.error("--report-json requires --compare-ff")

    try:
        now_ts = time.time()
        jblanked_records = fetch_jblanked_records()
        state, stats = build_state_from_jblanked_records(jblanked_records, now_ts)
    except (OSError, RuntimeError, ValueError, urllib.error.URLError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(f"Fetched {stats['events']} JBlanked Forex Factory events.")
    print(f"Impact distribution: {stats['by_impact']}")
    print(f"High-risk windows (future +/-1h): {stats['high_risk_windows']}")
    if args.write_state:
        _write_state(state, args.write_state)
        print(f"Wrote {args.write_state}")
    if args.compare_ff:
        try:
            report = compare_high_impact_week(fetch_ff_records(), jblanked_records, now_ts)
        except (OSError, RuntimeError, ValueError, urllib.error.URLError) as exc:
            print(f"ERROR: FF comparison fetch failed: {exc}", file=sys.stderr)
            return 2
        _print_comparison(report)
        if args.report_json:
            with open(args.report_json, "w") as handle:
                json.dump(report, handle, indent=2)
            print(f"Wrote {args.report_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
