"""Populate state/macro_events.json from the public Forex Factory calendar.

The LSE macro-scraper fails open to `{"events":[]}` when LSE_API_KEY is the shipped
placeholder. The FF public JSON feed (already trusted by
`scripts/macro_cross_reference.py`) is reachable without a key and returns real
current economic-calendar data. This script mirrors the FF this-week feed into the
exact MacroEventState schema the bot's risk gate reads, so the macro risk gate is no
longer blind pending a corrected LSE-key configuration (see
`docs/lse_api_key_acquisition.md` — the LSE calendar endpoint is actually reachable
with the SPA's hardcoded anon-key that already lives in the repo; the recommended
long-term fix is to wire that into the LSE scraper. This FF backfill is a stopgap that
gives the bot real current data TODAY and is also useful as a redundant cross-source
for the LSE feed).

Reuses the SAME `_HIGH_IMPACT_CATEGORIES` keyword table from the scraper for
HIGH-impact classification consistency.

Staleness guard (network optional): `--check-stale` reads state/macro_events.json;
if its `source` is `forex_factory_public` and `last_update` is more than
`--max-staleness-hours` (default 24) hours ago, it logs a loud WARNING to stderr with
the file age in hours and exits with status 0 (informational, never fatal). Intended
for cron-health monitoring.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Iterable, List, Tuple

# Ensure project root (parent of scripts/) is on the path so `scrapers` resolves.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from scrapers.page_agent_runner import MacroEventState, MacroEvent, _HIGH_IMPACT_CATEGORIES  # noqa: E402

FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
MACRO_EVENTS_PATH_DEFAULT = os.path.join(_ROOT, "state", "macro_events.json")
MAX_STALENESS_HOURS_DEFAULT = 24.0
SOURCE_TAG = "forex_factory_public"


def _classify_impact(title: str, ff_impact: str) -> str:
    t = (title or "").lower()
    ff = (ff_impact or "").lower()
    if any(kw in t for kw in _HIGH_IMPACT_CATEGORIES):
        return "HIGH"
    if ff == "high":
        return "HIGH"
    if ff == "medium":
        return "MED"
    if ff == "low":
        return "LOW"
    return "MED"


def _parse_dt(s: str) -> float:
    # FF returns ISO-8601 with offset, e.g. 2026-08-03T10:00:00-04:00
    try:
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        # fallback to RFC850-ish via email parsing if needed
        return parsedate_to_datetime(s).timestamp()


def build_state_from_ff_records(
    records: Iterable[dict],
    now_ts: float,
) -> Tuple[MacroEventState, dict]:
    """Pure function: convert FF records (already parsed JSON) into a MacroEventState
    using the same HIGH/MED/LOW classifier the LSE scraper uses. Returns (state, stats)
    so tests can assert distribution without touching disk.

    `records` is the parsed JSON list returned by the FF feed; `now_ts` is the
    wall-clock reference used to decide which HIGH events are still in the future and
    therefore contribute to high_risk_windows.
    """
    events: List[MacroEvent] = []
    for rec in records:
        title = (rec.get("title") or "").strip()
        if not title:
            continue
        ts = _parse_dt(rec.get("date") or "")
        impact = _classify_impact(title, rec.get("impact") or "")
        country = (rec.get("country") or "").strip()
        forecast = rec.get("forecast") or ""
        previous = rec.get("previous") or ""
        desc_parts = [
            p for p in (
                country,
                f"F: {forecast}" if forecast else None,
                f"P: {previous}" if previous else None,
            ) if p
        ]
        events.append(MacroEvent(
            title=title,
            timestamp=ts,
            impact=impact,
            description=" | ".join(desc_parts),
            url="",
            source=SOURCE_TAG,
        ))

    events.sort(key=lambda e: e.timestamp)

    high_risk_windows: List[dict] = []
    for e in events:
        if e.timestamp <= now_ts:
            continue
        if e.impact == "HIGH":
            high_risk_windows.append({
                "start": e.timestamp - 3600,
                "end": e.timestamp + 3600,
                "event": e.title,
            })

    state = MacroEventState(
        events=events,
        last_update=float(now_ts),
        high_risk_windows=high_risk_windows,
    )

    by_impact = {"HIGH": 0, "MED": 0, "LOW": 0}
    for e in events:
        by_impact[e.impact] = by_impact.get(e.impact, 0) + 1

    stats = {
        "events": len(events),
        "by_impact": by_impact,
        "high_risk_windows": len(high_risk_windows),
        "source_tag": SOURCE_TAG,
    }
    return state, stats


def fetch_ff_records() -> List[dict]:
    """Fetch the live Forex Factory this-week JSON. Returns the parsed list."""
    req = urllib.request.Request(
        FF_URL, headers={"User-Agent": "Mozilla/5.0 (macro-backfill)"}
    )
    raw = urllib.request.urlopen(req, timeout=20).read()
    data = json.loads(raw)
    if not isinstance(data, list):
        raise ValueError(f"FF feed returned {type(data).__name__}, expected list")
    return data


def write_state(state: MacroEventState, path: str = MACRO_EVENTS_PATH_DEFAULT) -> None:
    payload = state.to_dict()
    # MacroEventState preserves provenance per event. Keep it at the document
    # level too so a stale empty/partially-written FF state remains identifiable.
    payload["source"] = SOURCE_TAG
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)


def check_stale(
    path: str = MACRO_EVENTS_PATH_DEFAULT,
    max_staleness_hours: float = MAX_STALENESS_HOURS_DEFAULT,
    now_ts: float = None,
) -> Tuple[bool, float]:
    """Return (is_stale, age_hours) for `path`.

    Stale iff:
      - file exists, has `source == forex_factory_public` in the first event, AND
        `last_update` is older than `max_staleness_hours`.
    Files with `source != forex_factory_public` (e.g. lse_calendar_rest) are NOT
    considered stale by this guard — they belong to a different refresh schedule.

    `now_ts` is the wall-clock reference; defaults to `time.time()` if not given. The
    `age_hours` returned is `max(0, (now_ts - last_update) / 3600)`.
    """
    if now_ts is None:
        now_ts = time.time()
    try:
        with open(path, "r") as f:
            d = json.load(f)
    except FileNotFoundError:
        # No file at all counts as fully stale.
        return True, float("inf")
    except Exception:
        return True, float("inf")

    last_update = float(d.get("last_update", 0) or 0)
    events = d.get("events") or []
    src = d.get("source") or (events[0].get("source", "") if events else "")
    if src != SOURCE_TAG:
        # Not our file (e.g. lse_calendar_rest) — leave to the LSE scraper's schedule.
        return False, 0.0
    age_hours = max(0.0, (now_ts - last_update) / 3600.0)
    return age_hours > max_staleness_hours, age_hours


def _parse_args(argv: List[str]) -> dict:
    args = {"check_stale": False, "max_staleness_hours": MAX_STALENESS_HOURS_DEFAULT,
            "path": MACRO_EVENTS_PATH_DEFAULT, "fetch": True}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--check-stale":
            args["check_stale"] = True
            args["fetch"] = False
        elif a == "--max-staleness-hours":
            i += 1
            args["max_staleness_hours"] = float(argv[i])
        elif a == "--path":
            i += 1
            args["path"] = argv[i]
        elif a in ("-h", "--help"):
            print(__doc__)
            sys.exit(0)
        i += 1
    return args


def main(argv: List[str] = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    if args["check_stale"]:
        is_stale, age_hours = check_stale(
            path=args["path"],
            max_staleness_hours=args["max_staleness_hours"],
        )
        if age_hours == float("inf"):
            print(f"WARNING: {args['path']} missing or unreadable — risk gate has NO macro source")
            return 0
        if is_stale:
            print(
                f"WARNING: {args['path']} is STALE — "
                f"source=forex_factory_public, age={age_hours:.1f}h "
                f"(max staleness {args['max_staleness_hours']}h). "
                f"Re-run: python scripts/populate_macro_from_ff.py"
            )
        else:
            print(f"OK: {args['path']} age={age_hours:.1f}h "
                  f"(within max staleness {args['max_staleness_hours']}h).")
        return 0

    # Standard path: fetch live + write state.
    records = fetch_ff_records()
    state, stats = build_state_from_ff_records(records, now_ts=time.time())
    write_state(state, path=args["path"])

    print(f"Fetched {stats['events']} FF events for the current week.")
    print(f"Impact distribution: {stats['by_impact']}")
    print(f"High-risk windows (future ±1h): {stats['high_risk_windows']}")
    for w in state.high_risk_windows:
        dt = datetime.fromtimestamp(w["start"], tz=timezone.utc).isoformat()
        print(f"  {dt} | {w['event']}")
    print(f"Wrote {args['path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
