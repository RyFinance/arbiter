"""Pure, evidence-backed status helpers for the operator dashboard."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time
from typing import Any, Iterable, TypedDict
import xml.etree.ElementTree as ET


class DashboardStatus(TypedDict):
    """Bot-liveness status derived from heartbeat and sealed-session evidence."""

    state: str
    detail: str
    age_s: float


def _load_json(path: str | os.PathLike[str]) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _timestamp(value: Any) -> float:
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    if not isinstance(value, str) or not value.strip():
        return 0.0
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _file_age(path: str | os.PathLike[str], now: float) -> float:
    try:
        return max(0.0, now - Path(path).stat().st_mtime)
    except OSError:
        return float("inf")


def bot_liveness_status(
    heartbeat_path: str,
    timeout_s: float,
    now: float | None = None,
) -> DashboardStatus:
    """Return the legacy heartbeat-only status used by stress-test assertions."""
    current = time.time() if now is None else now
    age_s = _file_age(heartbeat_path, current)
    if not math.isfinite(age_s):
        return {
            "state": "disconnected",
            "detail": "Heartbeat missing; bot is disconnected or never started.",
            "age_s": age_s,
        }
    if age_s > timeout_s:
        return {
            "state": "stale/disconnected",
            "detail": f"Heartbeat is {age_s:.1f}s old (limit {timeout_s:.1f}s).",
            "age_s": age_s,
        }
    return {
        "state": "running",
        "detail": f"Heartbeat is current ({age_s:.1f}s old).",
        "age_s": age_s,
    }


def _session_report_is_sealed(report_path: Path) -> bool:
    """A sealed report must be named in SHA256SUMS and match its digest."""
    sums_path = report_path.parent / "SHA256SUMS"
    try:
        expected = next(
            line.split()[0]
            for line in sums_path.read_text(encoding="utf-8").splitlines()
            if len(line.split()) >= 2 and line.split()[-1].lstrip("*") == report_path.name
        )
        actual = hashlib.sha256(report_path.read_bytes()).hexdigest()
    except (OSError, StopIteration, IndexError):
        return False
    return expected == actual


def latest_sealed_task_session(
    archive_root: str | os.PathLike[str],
    prefix: str = "task_2_4_",
) -> dict[str, Any]:
    """Load the newest valid sealed task report, ordered by recorded time."""
    root = Path(archive_root)
    candidates: list[tuple[float, Path, dict[str, Any]]] = []
    for report_path in root.glob(f"{prefix}*/session_report.json"):
        if not _session_report_is_sealed(report_path):
            continue
        report = _load_json(report_path)
        if not report:
            continue
        recorded = _timestamp(report.get("finished_at")) or _timestamp(report.get("started_at"))
        if recorded <= 0.0:
            try:
                recorded = report_path.stat().st_mtime
            except OSError:
                continue
        candidates.append((recorded, report_path, report))
    if not candidates:
        return {}
    _, report_path, report = max(candidates, key=lambda item: (item[0], str(item[1])))
    result = dict(report)
    result["_report_path"] = str(report_path)
    result["_sealed"] = True
    return result


def task_bot_liveness_status(
    heartbeat_paths: Iterable[str | os.PathLike[str]],
    archive_root: str | os.PathLike[str],
    timeout_s: float,
    now: float | None = None,
) -> DashboardStatus:
    """Prefer real heartbeat truth, but never call a completed task log running."""
    current = time.time() if now is None else now
    report = latest_sealed_task_session(archive_root)
    finished_ts = _timestamp(report.get("finished_at"))
    session_id = str(report.get("session_id") or Path(str(report.get("_report_path", ""))).parent.name)

    heartbeat_samples: list[tuple[float, float, Path]] = []
    for raw_path in heartbeat_paths:
        path = Path(raw_path)
        try:
            modified = path.stat().st_mtime
        except OSError:
            continue
        heartbeat_samples.append((modified, max(0.0, current - modified), path))
    freshest = max(heartbeat_samples, default=None, key=lambda item: item[0])

    # A heartbeat written after a completed report is evidence of a newer run.
    if freshest is not None:
        modified, age_s, _ = freshest
        if age_s <= timeout_s and (finished_ts <= 0.0 or modified > finished_ts):
            return {
                "state": "running",
                "detail": f"Heartbeat is current ({age_s:.1f}s old).",
                "age_s": age_s,
            }

    if finished_ts > 0.0:
        age_s = max(0.0, current - finished_ts)
        completed = bool(report.get("shutdown", {}).get("bot_task_completed", True))
        qualifier = "completed" if completed else "finished"
        return {
            "state": "completed/offline",
            "detail": f"Session {session_id} {qualifier} at {report.get('finished_at')}; no newer live heartbeat.",
            "age_s": age_s,
        }

    if freshest is not None:
        _, age_s, _ = freshest
        return {
            "state": "stale/disconnected",
            "detail": f"Newest heartbeat is {age_s:.1f}s old (limit {timeout_s:.1f}s).",
            "age_s": age_s,
        }
    return {
        "state": "disconnected",
        "detail": "No sealed task session or heartbeat is available.",
        "age_s": float("inf"),
    }


def paper_session_status(report: dict[str, Any]) -> dict[str, Any]:
    """Extract the latest sealed PAPER_TRADE metrics without inventing a sample."""
    summary = report.get("metrics", {}).get("summary", {})
    convergence = report.get("event_level_pnl_convergence", {})
    if not isinstance(summary, dict):
        summary = {}
    if not isinstance(convergence, dict):
        convergence = {}
    opportunities = int(summary.get("opportunities", 0) or 0)
    fills = int(summary.get("fills", 0) or 0)
    sample_available = bool(convergence.get("sample_available", opportunities > 0))
    finished_at = report.get("finished_at")
    return {
        "available": bool(report),
        "sealed": bool(report.get("_sealed")),
        "session_id": report.get("session_id", "unknown"),
        "report_path": report.get("_report_path", ""),
        "finished_at": finished_at,
        "completed": bool(finished_at) and bool(report.get("shutdown", {}).get("bot_task_completed", True)),
        "paper_only": report.get("paper_only") is True,
        "real_orders_submitted": report.get("real_orders_submitted"),
        "duration_seconds": float(report.get("duration_seconds", 0.0) or 0.0),
        "connected_observation_seconds": float(report.get("connected_observation_seconds", 0.0) or 0.0),
        "opportunities": opportunities,
        "orders_sent": int(summary.get("orders_sent", 0) or 0),
        "fills": fills,
        "partial_fills": int(summary.get("partial_fills", 0) or 0),
        "missed": int(summary.get("missed", 0) or 0),
        "locked_pnl": float(summary.get("locked_pnl", 0.0) or 0.0),
        "session_pnl": float(summary.get("session_pnl", 0.0) or 0.0),
        "would_lock": float(summary.get("would_lock", 0.0) or 0.0),
        "avg_latency_ms": float(summary.get("opp_to_order_avg_ms", 0.0) or 0.0),
        "p50_latency_ms": float(summary.get("p50_ms", 0.0) or 0.0),
        "p95_latency_ms": float(summary.get("p95_ms", 0.0) or 0.0),
        "p99_latency_ms": float(summary.get("p99_ms", 0.0) or 0.0),
        "sample_available": sample_available,
        "convergence_pass": bool(convergence.get("PASS")) if sample_available else None,
        "convergence_pct": float(convergence.get("convergence_pct", 0.0) or 0.0),
        "deviation_pct": float(convergence.get("deviation_pct", 0.0) or 0.0),
    }


def kill_switch_status(
    emergency_stop_path: str | os.PathLike[str],
    report: dict[str, Any],
) -> dict[str, Any]:
    """Separate the current emergency-stop state from event verification evidence."""
    armed = Path(emergency_stop_path).exists()
    kill = report.get("alerting", {}).get("kill_switch", {})
    if not isinstance(kill, dict):
        kill = {}
    orders_before = kill.get("orders_sent_before_post_halt_probe")
    orders_after = kill.get("orders_sent_after_post_halt_probe")
    verified = all(
        (
            report.get("alerting", {}).get("enabled") is True,
            kill.get("requested") is True,
            kill.get("halted") is True,
            kill.get("alert_recorded") is True,
            kill.get("webhook_delivered") is True,
            kill.get("post_halt_probe_blocked") is True,
            kill.get("post_halt_rejection_reason") == "halted/emergency-stop",
            orders_before is not None,
            orders_before == orders_after,
        )
    )
    if armed:
        state = "armed"
        detail = "emergency.stop exists; order submission is currently halted."
    elif verified:
        state = "verified-pass"
        detail = "Alert recorded and delivered; post-halt probe was blocked with no additional orders."
    else:
        state = "not-verified"
        detail = "Emergency stop is not armed and no complete alert-enabled verification is available."
    return {
        "state": state,
        "armed": armed,
        "verified": verified,
        "detail": detail,
        "session_id": report.get("session_id", "unknown"),
        "orders_before": orders_before,
        "orders_after": orders_after,
    }


_DATED_JUNIT_RE = re.compile(r"(\d{4}-\d{2}-\d{2})\.xml$")


def _junit_counts(path: Path) -> dict[str, Any]:
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return {}
    if root.tag.rsplit("}", 1)[-1] == "testsuite":
        suites = [root]
    else:
        suites = [node for node in root if node.tag.rsplit("}", 1)[-1] == "testsuite"]
    if not suites:
        return {}
    try:
        tests = sum(int(suite.attrib.get("tests", 0)) for suite in suites)
        failures = sum(int(suite.attrib.get("failures", 0)) for suite in suites)
        errors = sum(int(suite.attrib.get("errors", 0)) for suite in suites)
        skipped = sum(int(suite.attrib.get("skipped", 0)) for suite in suites)
        seconds = sum(float(suite.attrib.get("time", 0.0)) for suite in suites)
    except ValueError:
        return {}
    return {
        "tests": tests,
        "passed": max(0, tests - failures - errors - skipped),
        "failures": failures,
        "errors": errors,
        "skipped": skipped,
        "seconds": seconds,
        "timestamp": suites[0].attrib.get("timestamp", ""),
    }


def discover_pytest_status(
    state_dir: str | os.PathLike[str],
    ground_truth_path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Discover the broadest current-date JUnit; use ground truth as fallback.

    Focused suites are often written after full regressions, so recency alone is
    not evidence for the dashboard's *Test Suite* count.  On the newest date we
    select the report with the largest collected test count, then the newest of
    equally broad reports.
    """
    state = Path(state_dir)
    parsed: list[tuple[str, float, Path, dict[str, Any]]] = []
    for path in state.glob("pytest_*.xml"):
        match = _DATED_JUNIT_RE.search(path.name)
        if not match:
            continue
        counts = _junit_counts(path)
        if not counts:
            continue
        try:
            modified = path.stat().st_mtime
        except OSError:
            continue
        parsed.append((match.group(1), modified, path, counts))
    if parsed:
        date, _, path, counts = max(
            parsed, key=lambda item: (item[0], int(item[3].get("tests", 0)), item[1], item[2].name)
        )
        signature = tuple(counts.get(key) for key in ("tests", "passed", "failures", "errors", "skipped"))
        matching_runs = sum(
            1
            for item_date, _, _, item in parsed
            if item_date == date
            and tuple(item.get(key) for key in ("tests", "passed", "failures", "errors", "skipped")) == signature
        )
        return {
            **counts,
            "available": True,
            "date": date,
            "source": str(path),
            "source_kind": "junit",
            "matching_runs": matching_runs,
            "status": "pass" if counts["failures"] + counts["errors"] == 0 else "fail",
        }

    truth_path = Path(ground_truth_path) if ground_truth_path else state / "ground_truth_2026-08-15.json"
    truth = _load_json(truth_path)
    runs = truth.get("pytest", [])
    if not isinstance(runs, list) or not runs:
        return {"available": False, "status": "unavailable", "source": ""}
    run = runs[-1] if isinstance(runs[-1], dict) else {}
    tests = int(run.get("tests", 0) or 0)
    failures = int(run.get("failed", 0) or 0)
    skipped = int(run.get("skipped", 0) or 0)
    passed = int(run.get("passed", max(0, tests - failures - skipped)) or 0)
    signature = (tests, passed, failures, skipped)
    matching_runs = sum(
        1
        for candidate in runs
        if isinstance(candidate, dict)
        and (
            int(candidate.get("tests", 0) or 0),
            int(candidate.get("passed", 0) or 0),
            int(candidate.get("failed", 0) or 0),
            int(candidate.get("skipped", 0) or 0),
        )
        == signature
    )
    return {
        "available": True,
        "tests": tests,
        "passed": passed,
        "failures": failures,
        "errors": 0,
        "skipped": skipped,
        "seconds": float(run.get("seconds", 0.0) or 0.0),
        "date": str(truth.get("observed_at_utc", ""))[:10],
        "timestamp": truth.get("observed_at_utc", ""),
        "source": str(truth_path),
        "source_kind": "ground_truth",
        "matching_runs": matching_runs,
        "status": "pass" if failures == 0 else "fail",
    }


def btc_model_status(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Summarize every BTC proxy sizing policy without promoting it to execution."""
    data = _load_json(path)
    models = data.get("pnl", {}).get("aggregate", {}).get("models", {})
    if not isinstance(models, dict) or not models:
        return {"available": False, "status": "unavailable", "policies": []}
    policies: list[dict[str, Any]] = []
    preferred = {"binary_f1": 0, "top_decile": 1, "confidence_weighted": 2}
    for model_name, model_data in sorted(models.items()):
        if not isinstance(model_data, dict):
            continue
        for policy_name, policy_data in sorted(
            model_data.items(), key=lambda item: (preferred.get(item[0], 99), item[0])
        ):
            net = policy_data.get("net_pnl_bps", {}) if isinstance(policy_data, dict) else {}
            value_range = net.get("range", []) if isinstance(net, dict) else []
            exposure = (
                policy_data.get("pnl_per_unit_exposure_bps", {})
                if isinstance(policy_data, dict)
                else {}
            )
            exposure_range = exposure.get("range", []) if isinstance(exposure, dict) else []
            try:
                mean = float(net["mean"])
                low = float(value_range[0])
                high = float(value_range[1])
            except (KeyError, IndexError, TypeError, ValueError):
                continue
            try:
                exposure_mean = float(exposure["mean"])
                exposure_low = float(exposure_range[0])
                exposure_high = float(exposure_range[1])
            except (KeyError, IndexError, TypeError, ValueError):
                exposure_mean = None
                exposure_low = None
                exposure_high = None
            policies.append(
                {
                    "model": model_name,
                    "policy": policy_name,
                    "mean_net_pnl_bps": mean,
                    "range_low_bps": low,
                    "range_high_bps": high,
                    "negative_on_all_holdouts": high < 0.0,
                    "mean_pnl_per_unit_exposure_bps": exposure_mean,
                    "exposure_range_low_bps": exposure_low,
                    "exposure_range_high_bps": exposure_high,
                }
            )
    if not policies:
        return {"available": False, "status": "unavailable", "policies": []}
    negative_count = sum(bool(policy["negative_on_all_holdouts"]) for policy in policies)
    all_negative = negative_count == len(policies)
    best = max(policies, key=lambda policy: policy["mean_net_pnl_bps"])
    exposure_policies = [
        policy for policy in policies if policy["mean_pnl_per_unit_exposure_bps"] is not None
    ]
    best_exposure = (
        max(exposure_policies, key=lambda policy: policy["mean_pnl_per_unit_exposure_bps"])
        if exposure_policies
        else None
    )
    executable = data.get("pnl", {}).get("executable_polymarket_pnl", {})
    return {
        "available": True,
        "status": "not deployable" if all_negative else "review required",
        "all_policies_negative": all_negative,
        "negative_policy_count": negative_count,
        "policy_count": len(policies),
        "policies": policies,
        "best_policy": best,
        "best_exposure_policy": best_exposure,
        "proxy_status": data.get("pnl", {}).get("status", "unknown"),
        "executable_status": executable.get("status", "unavailable") if isinstance(executable, dict) else "unavailable",
        "warning": data.get("pnl", {}).get("warning", ""),
        "source": str(path),
    }


def depth_capture_status(
    consolidated_path: str | os.PathLike[str],
    ground_truth_path: str | os.PathLike[str],
    replay_validation_path: str | os.PathLike[str],
) -> dict[str, Any]:
    """Report captured real depth separately from the strict historical replay gate."""
    consolidated = Path(consolidated_path)
    truth = _load_json(ground_truth_path)
    replay = _load_json(replay_validation_path)
    observation = truth.get("depth_capture_observation", {})
    replay_input = replay.get("historical_input", {})
    coverage = replay_input.get("depth_snapshot_coverage", {}) if isinstance(replay_input, dict) else {}
    replay_status = replay.get("depth_snapshot", {})
    operational_gate = replay.get("operational_window_gate", {})
    row_gate = replay.get("row_count_gate", {})
    try:
        actual_bytes = consolidated.stat().st_size
        exists = True
    except OSError:
        actual_bytes = 0
        exists = False
    replay_bytes = replay_input.get("bytes") if isinstance(replay_input, dict) else None
    replay_matches = bool(exists and replay_bytes is not None and actual_bytes == int(replay_bytes))
    if replay_matches:
        rows = replay_input.get("source_rows")
        evidence_bytes = replay_bytes
        evidence_source = str(replay_validation_path)
    else:
        rows = observation.get("consolidated_rows") if isinstance(observation, dict) else None
        evidence_bytes = observation.get("consolidated_bytes") if isinstance(observation, dict) else None
        evidence_source = str(ground_truth_path)
    evidence_matches = bool(exists and evidence_bytes is not None and actual_bytes == int(evidence_bytes))
    quantity = replay_status.get("quantity", {}) if isinstance(replay_status, dict) else {}
    metrics = replay_status.get("after_metrics", {}) if isinstance(replay_status, dict) else {}
    return {
        "available": exists,
        "capture_status": "available" if exists else "unavailable",
        "rows": int(rows) if rows is not None else None,
        "bytes": actual_bytes,
        "evidence_bytes": int(evidence_bytes) if evidence_bytes is not None else None,
        "evidence_matches_file": evidence_matches,
        "duplicate_keys": observation.get("consolidated_duplicate_keys") if isinstance(observation, dict) else None,
        "chronology_violations": observation.get("consolidated_chronology_violations") if isinstance(observation, dict) else None,
        "strict_gate_status": replay_status.get("status", "not-yet-tested") if isinstance(replay_status, dict) else "not-yet-tested",
        "strict_gate_reason": replay_status.get("reason", "") if isinstance(replay_status, dict) else "",
        "replay_total_ticks": int(coverage.get("total_ticks", 0) or 0) if isinstance(coverage, dict) else 0,
        "replay_complete_ticks": int(coverage.get("complete_ticks", 0) or 0) if isinstance(coverage, dict) else 0,
        "row_gate_status": row_gate.get("status", "not-yet-tested") if isinstance(row_gate, dict) else "not-yet-tested",
        "complete_windows": int(operational_gate.get("observed_complete_windows", 0) or 0) if isinstance(operational_gate, dict) else 0,
        "required_complete_windows": int(operational_gate.get("required_complete_windows", 0) or 0) if isinstance(operational_gate, dict) else 0,
        "requested_quantity_fill_rate": float(quantity.get("requested_quantity_fill_rate", 0.0) or 0.0) if isinstance(quantity, dict) else 0.0,
        "replay_fills": int(metrics.get("fills", 0) or 0) if isinstance(metrics, dict) else 0,
        "replay_partials": int(metrics.get("partial_fills", 0) or 0) if isinstance(metrics, dict) else 0,
        "replay_halted_windows": int(metrics.get("halted_windows", 0) or 0) if isinstance(metrics, dict) else 0,
        "source": str(consolidated),
        "evidence_source": evidence_source,
    }


def macro_state_status(
    path: str | os.PathLike[str],
    now: float | None = None,
    max_age_s: float = 900.0,
) -> dict[str, Any]:
    """Treat missing, invalid, or stale macro state as unavailable/fail-safe."""
    current = time.time() if now is None else now
    macro_path = Path(path)
    if not macro_path.exists():
        return {
            "available": False,
            "state": "unavailable/fail-safe",
            "detail": "Macro state is missing; trading precheck must fail safe.",
            "age_s": float("inf"),
            "events": [],
            "high_risk_windows": [],
            "active_windows": [],
        }
    data = _load_json(macro_path)
    try:
        last_update = float(data.get("last_update", 0.0))
    except (TypeError, ValueError):
        last_update = 0.0
    if not data or not math.isfinite(last_update) or last_update <= 0.0:
        return {
            "available": False,
            "state": "unavailable/fail-safe",
            "detail": "Macro state is unreadable or has an invalid last_update; trading precheck must fail safe.",
            "age_s": float("inf"),
            "events": [],
            "high_risk_windows": [],
            "active_windows": [],
        }
    age_s = current - last_update
    events = data.get("events", []) if isinstance(data.get("events"), list) else []
    windows = data.get("high_risk_windows", []) if isinstance(data.get("high_risk_windows"), list) else []
    active = [
        window
        for window in windows
        if isinstance(window, dict)
        and isinstance(window.get("start"), (int, float))
        and isinstance(window.get("end"), (int, float))
        and float(window["start"]) <= current <= float(window["end"])
    ]
    if age_s < 0.0 or age_s > max_age_s:
        return {
            "available": False,
            "state": "stale/fail-safe",
            "detail": f"Macro state age {age_s:.1f}s is outside the {max_age_s:.1f}s freshness limit; trading precheck must fail safe.",
            "age_s": age_s,
            "last_update": last_update,
            "events": events,
            "high_risk_windows": windows,
            "active_windows": active,
        }
    return {
        "available": True,
        "state": "high-risk" if active else "available",
        "detail": "Macro state is fresh; a HIGH window is active." if active else "Macro state is fresh; no HIGH window is active.",
        "age_s": age_s,
        "last_update": last_update,
        "events": events,
        "high_risk_windows": windows,
        "active_windows": active,
    }
