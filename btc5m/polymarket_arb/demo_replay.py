"""Deterministic dashboard replay of sealed real paper-session archives."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class DemoSession:
    session_id: str
    directory: Path
    report_path: Path
    bot_log_path: Path
    duration_seconds: float
    started_at: str
    finished_at: str
    event_count: int
    opportunity_count: int
    trade_event_count: int
    seal_valid: bool
    seal_errors: tuple[str, ...]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_archive(directory: Path) -> tuple[bool, tuple[str, ...]]:
    seal = directory / "SHA256SUMS"
    errors: list[str] = []
    if not seal.is_file():
        return False, ("SHA256SUMS missing",)
    seen: set[str] = set()
    try:
        for raw in seal.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                continue
            parts = raw.split(None, 1)
            if len(parts) != 2:
                errors.append(f"malformed seal line: {raw[:80]}")
                continue
            expected, name = parts[0], parts[1].strip().lstrip("*")
            if Path(name).name != name:
                errors.append(f"unsafe seal path: {name}")
                continue
            path = directory / name
            seen.add(name)
            if not path.is_file():
                errors.append(f"sealed file missing: {name}")
            elif _sha256(path) != expected:
                errors.append(f"hash mismatch: {name}")
    except OSError as exc:
        errors.append(f"seal read failed: {exc}")
    for required in ("session_report.json", "bot.jsonl"):
        if required not in seen:
            errors.append(f"required file not sealed: {required}")
    return not errors, tuple(errors)


def discover_demo_sessions(root: Path) -> list[DemoSession]:
    sessions: list[DemoSession] = []
    if not root.is_dir():
        return sessions
    for directory in sorted(root.iterdir(), reverse=True):
        report_path = directory / "session_report.json"
        bot_path = directory / "bot.jsonl"
        if not directory.is_dir() or not report_path.is_file() or not bot_path.is_file():
            continue
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        valid, errors = verify_archive(directory)
        metrics = report.get("metrics", {}) if isinstance(report.get("metrics"), dict) else {}
        summary = metrics.get("summary", {}) if isinstance(metrics.get("summary"), dict) else {}
        sessions.append(
            DemoSession(
                session_id=str(report.get("session_id") or directory.name),
                directory=directory,
                report_path=report_path,
                bot_log_path=bot_path,
                duration_seconds=float(report.get("duration_seconds") or 0.0),
                started_at=str(report.get("started_at") or ""),
                finished_at=str(report.get("finished_at") or ""),
                event_count=int(metrics.get("event_count") or 0),
                opportunity_count=int(summary.get("opportunities") or 0),
                trade_event_count=int(metrics.get("fill_trade_event_count") or 0),
                seal_valid=valid,
                seal_errors=errors,
            )
        )
    return sessions


def _clock_seconds(value: str) -> float | None:
    try:
        hours, minutes, seconds = value.split(":", 2)
        return int(hours) * 3600.0 + int(minutes) * 60.0 + float(seconds)
    except (ValueError, AttributeError):
        return None


def load_demo_events(session: DemoSession) -> list[dict[str, Any]]:
    if not session.seal_valid:
        raise ValueError(f"demo archive seal is invalid: {', '.join(session.seal_errors)}")
    rows: list[dict[str, Any]] = []
    with session.bot_log_path.open("r", encoding="utf-8") as handle:
        for index, line in enumerate(handle):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            event["_index"] = index
            rows.append(event)
    if not rows:
        return rows
    first_clock = next(
        (value for row in rows if (value := _clock_seconds(str(row.get("t", "")))) is not None),
        0.0,
    )
    previous = first_clock
    day_offset = 0.0
    for row in rows:
        clock = _clock_seconds(str(row.get("t", "")))
        if clock is None:
            row["_elapsed_s"] = rows[-1].get("_elapsed_s", 0.0) if rows else 0.0
            continue
        if clock + day_offset < previous - 12 * 3600:
            day_offset += 24 * 3600
        absolute = clock + day_offset
        row["_elapsed_s"] = max(0.0, absolute - first_clock)
        previous = absolute
    return rows


def replay_snapshot(session: DemoSession, progress: float) -> dict[str, Any]:
    progress = min(1.0, max(0.0, float(progress)))
    events = load_demo_events(session)
    observed_duration = max((float(row.get("_elapsed_s", 0.0)) for row in events), default=0.0)
    duration = max(session.duration_seconds, observed_duration)
    cutoff = duration * progress
    visible = [row for row in events if float(row.get("_elapsed_s", 0.0)) <= cutoff]
    trades = [row for row in visible if row.get("event") == "TRADE"]
    fires = [row for row in visible if str(row.get("msg", "")).startswith("FIRE ")]
    alerts = [row for row in visible if "ALERT[" in str(row.get("msg", ""))]
    summaries = [row for row in visible if "session metrics:" in str(row.get("msg", ""))]
    latest_summary = summaries[-1].get("msg") if summaries else None
    outcomes: dict[str, int] = {}
    for row in trades:
        outcome = str(row.get("msg", "unknown"))
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
    return {
        "mode": "sealed_historical_paper_replay",
        "network_used": False,
        "session_id": session.session_id,
        "seal_valid": session.seal_valid,
        "progress": progress,
        "cutoff_seconds": cutoff,
        "duration_seconds": duration,
        "visible_event_count": len(visible),
        "total_event_count": len(events),
        "fires": len(fires),
        "trade_events": len(trades),
        "trade_outcomes": outcomes,
        "alerts": len(alerts),
        "latest_summary": latest_summary,
        "recent_events": visible[-30:],
    }
