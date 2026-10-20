#!/usr/bin/env python3
"""Aggregate sealed REST-book PAPER_TRADE sessions without averaging away failures."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from polymarket_arb.demo_replay import verify_archive  # noqa: E402


DEFAULT_ROOT = ROOT / "state" / "paper_session_archives"
DEFAULT_OUTPUT = ROOT / "state" / "rest_paper_batch_2026-08-15.json"
REST_PREFLIGHT_RE = re.compile(
    r"REST_PAIR_PREFLIGHT .*?passed=(True|False) reason=([^ ]+).*?latency_ms=([-+0-9.eE]+)"
)


def _iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _count(value: Any) -> int:
    if isinstance(value, (list, tuple, set, dict)):
        return len(value)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _distribution(values: list[float]) -> dict[str, Any]:
    """Return the same lower-index percentiles used by the runtime metrics."""
    finite = sorted(value for value in values if math.isfinite(value))
    if not finite:
        return {
            "n": 0,
            "avg_ms": None,
            "p50_ms": None,
            "p95_ms": None,
            "p99_ms": None,
            "max_ms": None,
        }

    def percentile(q: float) -> float:
        return finite[min(len(finite) - 1, int(len(finite) * q))]

    return {
        "n": len(finite),
        "avg_ms": sum(finite) / len(finite),
        "p50_ms": percentile(0.50),
        "p95_ms": percentile(0.95),
        "p99_ms": percentile(0.99),
        "max_ms": finite[-1],
    }


def _numeric_distribution(values: list[float]) -> dict[str, Any]:
    finite = [value for value in values if math.isfinite(value)]
    timed = _distribution(finite)
    return {
        "n": timed["n"],
        "avg": timed["avg_ms"],
        "min": min(finite) if finite else None,
        "p50": timed["p50_ms"],
        "p95": timed["p95_ms"],
        "p99": timed["p99_ms"],
        "max": timed["max_ms"],
    }


def _convergence_verdict(summary: dict[str, Any]) -> str:
    if int(summary.get("committed_executions", 0) or 0) == 0:
        return "no_committed_execution_sample"
    deviation = summary.get("deviation_pct")
    if deviation is not None and abs(float(deviation)) <= 10.0:
        return "converged_within_10_percent"
    if deviation is not None and float(deviation) < -10.0:
        return "negative_convergence_failure_persists"
    return "positive_deviation_outside_10_percent"


def _bot_log_counts(directory: Path) -> dict[str, Any]:
    counts = {
        "window_active_messages": 0,
        "stale_token_errors": 0,
        "websocket_recovery_messages": 0,
        "live_control_checks": 0,
        "resolved_trade_ages_ms": [],
        "active_market_ids": [],
        "rest_preflight_passed": 0,
        "rest_preflight_rejected": 0,
        "rest_preflight_latency_ms": [],
        "rest_preflight_reasons": {},
        "rest_preflight_attempts": [],
        "cross_leg_coalescing_enabled": False,
        "cross_leg_coalesced_markets": [],
        "cross_leg_settle_ms": None,
        "cross_leg_incomplete_suppression_markets": [],
        "cross_leg_coherent_evaluations": None,
        "cross_leg_incomplete_suppressions": None,
    }
    try:
        handle = (directory / "bot.jsonl").open("r", encoding="utf-8")
    except OSError:
        return counts
    active_markets: set[str] = set()
    with handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            message = str(row.get("msg", ""))
            lowered = message.lower()
            if message.startswith("WINDOW ACTIVE "):
                parts = message.split()
                if len(parts) >= 3:
                    active_markets.add(parts[2])
            if "stale-token" in lowered or "stale token" in lowered:
                counts["stale_token_errors"] += 1
            if "websocket recovered" in lowered:
                counts["websocket_recovery_messages"] += 1
            if message.startswith("LIVE_CONTROL_CHECK "):
                counts["live_control_checks"] += 1
            if (
                "cross_leg_frame_coalescing=True" in message
                or "cross_leg_pair_barrier=True" in message
            ):
                counts["cross_leg_coalescing_enabled"] = True
                settle_match = re.search(r"\bcross_leg_settle=([-+0-9.eE]+)ms", message)
                if settle_match:
                    counts["cross_leg_settle_ms"] = float(settle_match.group(1))
            if message.startswith("CROSS_LEG_UPDATE_COALESCED "):
                market_match = re.search(r"\bmarket=([^ ]+)", message)
                if market_match:
                    counts["cross_leg_coalesced_markets"].append(market_match.group(1))
            if message.startswith("CROSS_LEG_UPDATE_INCOMPLETE "):
                market_match = re.search(r"\bmarket=([^ ]+)", message)
                if market_match:
                    counts["cross_leg_incomplete_suppression_markets"].append(
                        market_match.group(1)
                    )
            if message.startswith("PAIR_COHERENCE_SUMMARY "):
                coherent_match = re.search(r"\bcoherent_evaluations=(\d+)", message)
                incomplete_match = re.search(r"\bincomplete_suppressions=(\d+)", message)
                if coherent_match:
                    counts["cross_leg_coherent_evaluations"] = int(
                        coherent_match.group(1)
                    )
                if incomplete_match:
                    counts["cross_leg_incomplete_suppressions"] = int(
                        incomplete_match.group(1)
                    )
            if row.get("event") == "TRADE" and message in {"filled", "partial_resolved"}:
                age = row.get("data", {}).get("age_ms")
                if isinstance(age, (int, float)) and math.isfinite(float(age)):
                    counts["resolved_trade_ages_ms"].append(float(age))
            if row.get("event") == "TRADE" and message == "missed":
                data = row.get("data", {})
                reason = str(data.get("reason", "")) if isinstance(data, dict) else ""
                if reason.startswith("rest_preflight_"):
                    counts["rest_preflight_attempts"].append(
                        {
                            "t": row.get("t"),
                            "reason": reason,
                            "market_id": data.get("market_id"),
                            "decision_combined": data.get("combined"),
                            "rest_combined": data.get("rest_combined"),
                            "yes_limit": data.get("rest_yes_limit"),
                            "no_limit": data.get("rest_no_limit"),
                            "yes_vwap": data.get("rest_yes_vwap"),
                            "no_vwap": data.get("rest_no_vwap"),
                            "yes_available": data.get("rest_yes_available"),
                            "no_available": data.get("rest_no_available"),
                            "required_size": data.get("size"),
                            "edge_price": data.get("rest_edge_price"),
                            "would_lock": data.get("would_lock"),
                            "edge_surplus": data.get("edge_surplus"),
                            "quote_age_ms": data.get("age_ms"),
                            "latency_ms": data.get("rest_preflight_latency_ms"),
                        }
                    )
            match = REST_PREFLIGHT_RE.search(message)
            if match:
                passed = match.group(1) == "True"
                key = "rest_preflight_passed" if passed else "rest_preflight_rejected"
                counts[key] += 1
                counts["rest_preflight_latency_ms"].append(float(match.group(3)))
                reason = match.group(2)
                counts["rest_preflight_reasons"][reason] = (
                    counts["rest_preflight_reasons"].get(reason, 0) + 1
                )
    counts["window_active_messages"] = len(active_markets)
    counts["active_market_ids"] = sorted(active_markets)
    return counts


def load_sessions(
    root: Path,
    *,
    started_after: datetime,
    started_before: datetime | None = None,
) -> list[dict[str, Any]]:
    sessions: list[dict[str, Any]] = []
    if not root.is_dir():
        return sessions
    for directory in sorted(root.iterdir()):
        report_path = directory / "session_report.json"
        if not directory.is_dir() or not report_path.is_file():
            continue
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
            started_at = _iso(str(report["started_at"]))
        except (OSError, json.JSONDecodeError, KeyError, ValueError):
            continue
        if started_at < started_after or (started_before and started_at >= started_before):
            continue
        if report.get("paper_only") is not True or report.get("real_orders_submitted") is not False:
            continue
        if report.get("paper_fill_source") != "rest_book":
            continue
        seal_valid, seal_errors = verify_archive(directory)
        sessions.append(
            {
                "directory": directory,
                "report": report,
                "started_at_dt": started_at,
                "seal_valid": seal_valid,
                "seal_errors": list(seal_errors),
            }
        )
    return sessions


def summarize(
    sessions: list[dict[str, Any]],
    *,
    expected_session_count: int,
    change_at: datetime | None = None,
    coherence_change_at: datetime | None = None,
    settle_change_at: datetime | None = None,
    instrumented_change_at: datetime | None = None,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    action_counts: Counter[str] = Counter()
    miss_reasons: Counter[str] = Counter()
    exact_executions: list[dict[str, Any]] = []
    timing_samples_by_session: dict[str, list[dict[str, Any]]] = {}
    quote_ages_by_session: dict[str, list[float]] = {}

    for wrapped in sessions:
        report = wrapped["report"]
        metrics = report.get("metrics", {})
        summary = metrics.get("summary", {}) if isinstance(metrics, dict) else {}
        convergence = report.get("event_level_pnl_convergence", {})
        if not isinstance(convergence, dict):
            convergence = {}
        expected = _finite(convergence.get("expected_committed_pnl"))
        realized = _finite(convergence.get("realized_committed_pnl"))
        committed = int(convergence.get("committed_executions") or 0)
        session_id = str(report.get("session_id") or wrapped["directory"].name)
        timing_samples = [
            sample
            for sample in metrics.get("execution_timing_samples_ms", [])
            if isinstance(sample, dict)
        ]
        bot_counts = _bot_log_counts(wrapped["directory"])
        metric_quote_ages = [
            _finite(value)
            for value in metrics.get("filled_trade_age_ms", [])
            if isinstance(value, (int, float)) and math.isfinite(float(value))
        ]
        quote_ages = bot_counts["resolved_trade_ages_ms"] or metric_quote_ages
        timing_samples_by_session[session_id] = timing_samples
        quote_ages_by_session[session_id] = quote_ages
        failed_attempt = report.get("run_status") == "failed"
        phase = (
            "failed_attempt"
            if failed_attempt
            else
            "post_fix"
            if change_at is not None and wrapped["started_at_dt"] >= change_at
            else "pre_fix"
            if change_at is not None
            else "unpartitioned"
        )
        coherence_phase = (
            "startup_failure"
            if failed_attempt
            else
            "post_settle"
            if instrumented_change_at is not None
            and wrapped["started_at_dt"] >= instrumented_change_at
            else "pair_barrier_probe"
            if settle_change_at is not None
            and wrapped["started_at_dt"] >= settle_change_at
            else "same_frame_only"
            if coherence_change_at is not None
            and wrapped["started_at_dt"] >= coherence_change_at
            else "preflight_only"
            if coherence_change_at is not None
            and change_at is not None
            and wrapped["started_at_dt"] >= change_at
            else "pre_coalescing"
            if coherence_change_at is not None
            else "unpartitioned"
        )
        duration_seconds = _finite(report.get("duration_seconds"))
        connected_observation_seconds = _finite(
            report.get("connected_observation_seconds")
        )
        bot_task_completed = bool(
            report.get("shutdown", {}).get("bot_task_completed")
        )
        bot_task_timeout = bool(
            report.get("shutdown", {}).get("bot_task_timeout")
        )
        qualification_failures: list[str] = []
        if failed_attempt:
            qualification_failures.append("run_status_failed")
        if not wrapped["seal_valid"]:
            qualification_failures.append("archive_seal_invalid")
        if not bot_task_completed:
            qualification_failures.append("bot_task_not_completed")
        if bot_task_timeout:
            qualification_failures.append("bot_task_timeout")
        if duration_seconds < 900.0:
            qualification_failures.append("duration_below_900_seconds")
        if connected_observation_seconds < 900.0:
            qualification_failures.append("connected_observation_below_900_seconds")

        session_row = {
            "session_id": session_id,
            "run_status": report.get("run_status", "completed"),
            "failure_reason": report.get("failure_reason"),
            "started_at": report.get("started_at"),
            "finished_at": report.get("finished_at"),
            "phase": phase,
            "coherence_phase": coherence_phase,
            "seal_valid": wrapped["seal_valid"],
            "seal_errors": wrapped["seal_errors"],
            "duration_seconds": duration_seconds,
            "connected_observation_seconds": connected_observation_seconds,
            "qualifies_as_completed_session": not qualification_failures,
            "qualification_failures": qualification_failures,
            "opportunities": int(summary.get("opportunities") or 0),
            "orders_sent": int(summary.get("orders_sent") or 0),
            "fills": int(summary.get("fills") or 0),
            "partial_fills": int(summary.get("partial_fills") or 0),
            "partial_resolved": int(summary.get("partial_resolved") or 0),
            "unresolved_partials": max(
                0,
                int(summary.get("partial_fills") or 0)
                - int(summary.get("partial_resolved") or 0),
            ),
            "missed": int(summary.get("missed") or 0),
            "opportunity_to_first_order_latency": _distribution(
                [_finite(sample.get("opp2first_ms")) for sample in timing_samples]
            ),
            "dual_submit_latency": _distribution(
                [_finite(sample.get("dual_submit_ms")) for sample in timing_samples]
            ),
            "filled_quote_age": _distribution(quote_ages),
            "filled_quotes_within_500ms": sum(age <= 500.0 for age in quote_ages),
            "ws_recovery_messages": max(
                _count(report.get("ws_recovery_messages")),
                bot_counts["websocket_recovery_messages"],
            ),
            "ws_disconnect_severe_count": _count(
                report.get("ws_disconnect_severe_count")
            ),
            "window_active_messages": bot_counts["window_active_messages"],
            "window_market_ids": bot_counts["active_market_ids"],
            "window_rollovers_observed": max(
                0, bot_counts["window_active_messages"] - 1
            ),
            "stale_token_errors": bot_counts["stale_token_errors"],
            "live_control_checks": bot_counts["live_control_checks"],
            "rest_preflight_passed": bot_counts["rest_preflight_passed"],
            "rest_preflight_rejected": bot_counts["rest_preflight_rejected"],
            "rest_preflight_latency": _distribution(
                bot_counts["rest_preflight_latency_ms"]
            ),
            "rest_preflight_reasons": bot_counts["rest_preflight_reasons"],
            "rest_preflight_attempts": [
                {"session_id": session_id, "phase": phase, **attempt}
                for attempt in bot_counts["rest_preflight_attempts"]
            ],
            "cross_leg_coalescing_enabled": bot_counts[
                "cross_leg_coalescing_enabled"
            ],
            "cross_leg_coalesced_markets": sorted(
                set(bot_counts["cross_leg_coalesced_markets"])
            ),
            "cross_leg_settle_ms": bot_counts["cross_leg_settle_ms"],
            "cross_leg_incomplete_suppression_markets": sorted(
                set(bot_counts["cross_leg_incomplete_suppression_markets"])
            ),
            "cross_leg_coherent_evaluations": bot_counts[
                "cross_leg_coherent_evaluations"
            ],
            "cross_leg_incomplete_suppressions": bot_counts[
                "cross_leg_incomplete_suppressions"
            ],
            "bot_task_completed": bot_task_completed,
            "bot_task_timeout": bot_task_timeout,
            "alert_records": int(report.get("alerting", {}).get("session_alert_records") or 0),
            "expected_committed_pnl": expected,
            "realized_committed_pnl": realized,
            "committed_executions": committed,
            "convergence_pct": (
                realized / expected * 100.0 if expected != 0.0 else None
            ),
            "deviation_pct": (
                (realized - expected) / expected * 100.0 if expected != 0.0 else None
            ),
            "sample_available": bool(convergence.get("sample_available")),
            "kill_switch_alert_recorded": bool(
                report.get("alerting", {}).get("kill_switch", {}).get("alert_recorded")
            ),
            "archive": str(wrapped["directory"]),
        }
        rows.append(session_row)
        miss_reasons.update(metrics.get("miss_reasons", {}))

        for kind, key in (("clean", "fill_breakdown"), ("partial", "partial_breakdown")):
            for index, execution in enumerate(convergence.get(key, []) or []):
                action = str(execution.get("action") or kind)
                action_counts[action] += 1
                exact_executions.append(
                    {
                        "session_id": session_id,
                        "phase": phase,
                        "kind": kind,
                        "index": index,
                        **execution,
                    }
                )

    def phase_summary(phase_rows: list[dict[str, Any]]) -> dict[str, Any]:
        expected = sum(row["expected_committed_pnl"] for row in phase_rows)
        realized = sum(row["realized_committed_pnl"] for row in phase_rows)
        committed = sum(row["committed_executions"] for row in phase_rows)
        sampled = [row for row in phase_rows if row["sample_available"]]
        deviations = [
            row["deviation_pct"]
            for row in sampled
            if row["deviation_pct"] is not None
        ]
        timing_samples = [
            sample
            for row in phase_rows
            for sample in timing_samples_by_session.get(str(row["session_id"]), [])
        ]
        quote_ages = [
            age
            for row in phase_rows
            for age in quote_ages_by_session.get(str(row["session_id"]), [])
        ]
        opportunities = sum(row["opportunities"] for row in phase_rows)
        partial_fills = sum(row["partial_fills"] for row in phase_rows)
        clean_fills = sum(row["fills"] for row in phase_rows)
        fresh_count = sum(age <= 500.0 for age in quote_ages)
        distinct_window_markets = sorted(
            {
                market
                for row in phase_rows
                for market in row.get("window_market_ids", [])
            }
        )
        preflight_latencies = [
            latency
            for row in phase_rows
            for latency in _bot_log_counts(Path(row["archive"]))[
                "rest_preflight_latency_ms"
            ]
        ]
        preflight_reasons: Counter[str] = Counter()
        for row in phase_rows:
            preflight_reasons.update(row.get("rest_preflight_reasons", {}))
        preflight_attempts = [
            attempt
            for row in phase_rows
            for attempt in row.get("rest_preflight_attempts", [])
        ]
        decision_combined = [
            float(attempt["decision_combined"])
            for attempt in preflight_attempts
            if isinstance(attempt.get("decision_combined"), (int, float))
        ]
        rest_combined = [
            float(attempt["rest_combined"])
            for attempt in preflight_attempts
            if isinstance(attempt.get("rest_combined"), (int, float))
        ]
        full_size_pairs = sum(
            isinstance(attempt.get("required_size"), (int, float))
            and isinstance(attempt.get("yes_available"), (int, float))
            and isinstance(attempt.get("no_available"), (int, float))
            and float(attempt["yes_available"]) >= float(attempt["required_size"])
            and float(attempt["no_available"]) >= float(attempt["required_size"])
            for attempt in preflight_attempts
        )
        return {
            "session_count": len(phase_rows),
            "sealed_session_count": sum(row["seal_valid"] for row in phase_rows),
            "duration_seconds": sum(row["duration_seconds"] for row in phase_rows),
            "connected_observation_seconds": sum(
                row["connected_observation_seconds"] for row in phase_rows
            ),
            "opportunities": opportunities,
            "orders_sent": sum(row["orders_sent"] for row in phase_rows),
            "fills": clean_fills,
            "partial_fills": partial_fills,
            "partial_resolved": sum(row["partial_resolved"] for row in phase_rows),
            "unresolved_partials": sum(row["unresolved_partials"] for row in phase_rows),
            "missed": sum(row["missed"] for row in phase_rows),
            "clean_fill_rate_per_opportunity": (
                clean_fills / opportunities if opportunities else None
            ),
            "partial_fill_rate_per_opportunity": (
                partial_fills / opportunities if opportunities else None
            ),
            "opportunity_to_first_order_latency": _distribution(
                [_finite(sample.get("opp2first_ms")) for sample in timing_samples]
            ),
            "dual_submit_latency": _distribution(
                [_finite(sample.get("dual_submit_ms")) for sample in timing_samples]
            ),
            "paper_leg_network_latency": _distribution(
                [_finite(sample.get("paper_leg_max_ms")) for sample in timing_samples]
            ),
            "filled_quote_age": _distribution(quote_ages),
            "filled_quotes_within_500ms": fresh_count,
            "filled_quote_freshness_pct": (
                fresh_count / len(quote_ages) * 100.0 if quote_ages else None
            ),
            "ws_recovery_messages": sum(row["ws_recovery_messages"] for row in phase_rows),
            "ws_disconnect_severe_count": sum(
                row["ws_disconnect_severe_count"] for row in phase_rows
            ),
            "crashed_sessions": sum(not row["bot_task_completed"] for row in phase_rows),
            "timed_out_sessions": sum(row["bot_task_timeout"] for row in phase_rows),
            "alert_records": sum(row["alert_records"] for row in phase_rows),
            "window_active_messages": sum(
                row["window_active_messages"] for row in phase_rows
            ),
            "distinct_window_markets": len(distinct_window_markets),
            "window_market_ids": distinct_window_markets,
            "window_rollovers_observed": sum(
                row["window_rollovers_observed"] for row in phase_rows
            ),
            "stale_token_errors": sum(row["stale_token_errors"] for row in phase_rows),
            "live_control_checks": sum(row["live_control_checks"] for row in phase_rows),
            "rest_preflight_passed": sum(
                row["rest_preflight_passed"] for row in phase_rows
            ),
            "rest_preflight_rejected": sum(
                row["rest_preflight_rejected"] for row in phase_rows
            ),
            "rest_preflight_latency": _distribution(preflight_latencies),
            "rest_preflight_reasons": dict(sorted(preflight_reasons.items())),
            "rest_preflight_attempts": preflight_attempts,
            "rest_preflight_decision_combined": _numeric_distribution(decision_combined),
            "rest_preflight_executable_combined": _numeric_distribution(rest_combined),
            "rest_preflight_full_size_pair_count": full_size_pairs,
            "rest_preflight_theoretical_would_lock": sum(
                float(attempt.get("would_lock") or 0.0) for attempt in preflight_attempts
            ),
            "cross_leg_coalescing_sessions": sum(
                bool(row.get("cross_leg_coalescing_enabled")) for row in phase_rows
            ),
            "cross_leg_coalesced_market_count": len(
                {
                    market
                    for row in phase_rows
                    for market in row.get("cross_leg_coalesced_markets", [])
                }
            ),
            "cross_leg_settle_ms_values": sorted(
                {
                    float(row["cross_leg_settle_ms"])
                    for row in phase_rows
                    if isinstance(row.get("cross_leg_settle_ms"), (int, float))
                }
            ),
            "cross_leg_incomplete_suppression_market_count": len(
                {
                    market
                    for row in phase_rows
                    for market in row.get(
                        "cross_leg_incomplete_suppression_markets", []
                    )
                }
            ),
            "cross_leg_coherent_evaluations": sum(
                int(row.get("cross_leg_coherent_evaluations") or 0)
                for row in phase_rows
            ),
            "cross_leg_incomplete_suppressions": sum(
                int(row.get("cross_leg_incomplete_suppressions") or 0)
                for row in phase_rows
            ),
            "cross_leg_summary_session_count": sum(
                row.get("cross_leg_coherent_evaluations") is not None
                and row.get("cross_leg_incomplete_suppressions") is not None
                for row in phase_rows
            ),
            "sampled_sessions": len(sampled),
            "committed_executions": committed,
            "expected_committed_pnl": expected,
            "realized_committed_pnl": realized,
            "convergence_pct": realized / expected * 100.0 if expected else None,
            "deviation_pct": (realized - expected) / expected * 100.0 if expected else None,
            "positive_realized_sessions": sum(
                row["realized_committed_pnl"] > 0 for row in phase_rows
            ),
            "negative_realized_sessions": sum(
                row["realized_committed_pnl"] < 0 for row in phase_rows
            ),
            "zero_realized_sessions": sum(
                row["realized_committed_pnl"] == 0 for row in phase_rows
            ),
            "session_deviation_min_pct": min(deviations) if deviations else None,
            "session_deviation_max_pct": max(deviations) if deviations else None,
        }

    aggregate = phase_summary(rows)
    qualifying_aggregate = phase_summary(
        [row for row in rows if row["qualifies_as_completed_session"]]
    )
    phase_groups = {
        phase: phase_summary([row for row in rows if row["phase"] == phase])
        for phase in ("pre_fix", "post_fix", "unpartitioned")
        if any(row["phase"] == phase for row in rows)
    }
    coherence_groups = {
        phase: phase_summary(
            [row for row in rows if row["coherence_phase"] == phase]
        )
        for phase in (
            "pre_coalescing",
            "preflight_only",
            "same_frame_only",
            "pair_barrier_probe",
            "post_settle",
            "startup_failure",
            "unpartitioned",
        )
        if any(row["coherence_phase"] == phase for row in rows)
    }
    historical_raw_verdict = _convergence_verdict(aggregate)
    current_phase_name = "aggregate"
    current_evaluation = aggregate
    for candidate in (
        "post_settle",
        "pair_barrier_probe",
        "same_frame_only",
        "preflight_only",
    ):
        if candidate in coherence_groups:
            current_phase_name = candidate
            current_evaluation = coherence_groups[candidate]
            break
    current_verdict = _convergence_verdict(current_evaluation)
    historical_sample_invalidated = bool(
        current_phase_name == "post_settle"
        and aggregate["committed_executions"] > 0
        and current_evaluation["committed_executions"] == 0
    )
    verdict = (
        "historical_sample_invalidated_current_no_committed_execution_sample"
        if historical_sample_invalidated
        else current_verdict
    )

    qualifying_sessions = sum(
        bool(row["qualifies_as_completed_session"]) for row in rows
    )
    failed_attempts = sum(row.get("run_status") == "failed" for row in rows)
    return {
        "scope": "15-20 additional REST-book PAPER_TRADE sessions",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "paper_only": True,
        "real_orders_submitted": False,
        "expected_session_count": expected_session_count,
        "observed_session_count": len(rows),
        "qualifying_session_count": qualifying_sessions,
        "failed_attempt_count": failed_attempts,
        "batch_complete": qualifying_sessions >= expected_session_count,
        "change_at": change_at.isoformat() if change_at else None,
        "coherence_change_at": (
            coherence_change_at.isoformat() if coherence_change_at else None
        ),
        "settle_change_at": settle_change_at.isoformat() if settle_change_at else None,
        "instrumented_change_at": (
            instrumented_change_at.isoformat() if instrumented_change_at else None
        ),
        "verdict": verdict,
        "historical_raw_verdict": historical_raw_verdict,
        "current_evaluation_phase": current_phase_name,
        "current_verdict": current_verdict,
        "convergence_resolution": {
            "prior_minus_100_persists_as_valid_finding": (
                False if historical_sample_invalidated else None
            ),
            "prior_minus_100_reversed": (
                False if historical_sample_invalidated else None
            ),
            "small_sample_noise": False if historical_sample_invalidated else None,
            "measurement_artifact": historical_sample_invalidated,
            "classification": (
                "cross_leg_snapshot_incoherence_measurement_artifact; "
                "corrected_evaluation_has_no_committed_execution_sample"
                if historical_sample_invalidated
                else current_verdict
            ),
            "historical_raw_committed_executions": aggregate[
                "committed_executions"
            ],
            "historical_raw_expected_pnl": aggregate["expected_committed_pnl"],
            "historical_raw_realized_pnl": aggregate["realized_committed_pnl"],
            "historical_raw_convergence_pct": aggregate["convergence_pct"],
            "current_phase": current_phase_name,
            "current_committed_executions": current_evaluation[
                "committed_executions"
            ],
            "current_expected_pnl": current_evaluation["expected_committed_pnl"],
            "current_realized_pnl": current_evaluation["realized_committed_pnl"],
            "current_convergence_pct": current_evaluation["convergence_pct"],
        },
        "aggregate": aggregate,
        "qualifying_aggregate": qualifying_aggregate,
        "phases": phase_groups,
        "coherence_phases": coherence_groups,
        "execution_action_counts": dict(sorted(action_counts.items())),
        "miss_reasons": dict(sorted(miss_reasons.items())),
        "rest_preflight_attempts": [
            attempt for row in rows for attempt in row.get("rest_preflight_attempts", [])
        ],
        "exact_executions": exact_executions,
        "sessions": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive-root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--started-after", required=True)
    parser.add_argument("--started-before")
    parser.add_argument("--expected-sessions", type=int, default=15)
    parser.add_argument("--change-at")
    parser.add_argument("--coherence-change-at")
    parser.add_argument("--settle-change-at")
    parser.add_argument("--instrumented-change-at")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    started_after = _iso(args.started_after)
    started_before = _iso(args.started_before) if args.started_before else None
    change_at = _iso(args.change_at) if args.change_at else None
    coherence_change_at = (
        _iso(args.coherence_change_at) if args.coherence_change_at else None
    )
    settle_change_at = _iso(args.settle_change_at) if args.settle_change_at else None
    instrumented_change_at = (
        _iso(args.instrumented_change_at) if args.instrumented_change_at else None
    )
    sessions = load_sessions(
        args.archive_root,
        started_after=started_after,
        started_before=started_before,
    )
    report = summarize(
        sessions,
        expected_session_count=args.expected_sessions,
        change_at=change_at,
        coherence_change_at=coherence_change_at,
        settle_change_at=settle_change_at,
        instrumented_change_at=instrumented_change_at,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(args.output)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["batch_complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
