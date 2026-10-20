from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts.summarize_rest_session_batch import summarize


def _session(session_id: str, started_hour: int, expected: float, realized: float):
    started = datetime(2026, 8, 16, started_hour, tzinfo=timezone.utc)
    committed = 2 if expected != 0.0 or realized != 0.0 else 0
    return {
        "directory": Path("/evidence") / session_id,
        "started_at_dt": started,
        "seal_valid": True,
        "seal_errors": [],
        "report": {
            "session_id": session_id,
            "started_at": started.isoformat(),
            "finished_at": started.replace(minute=15).isoformat(),
            "duration_seconds": 900.0,
            "connected_observation_seconds": 900.0,
            "metrics": {
                "summary": {
                    "opportunities": 2,
                    "orders_sent": 4,
                    "fills": 1,
                    "partial_fills": 1,
                    "partial_resolved": 1,
                    "missed": 0,
                },
                "miss_reasons": {},
            },
            "event_level_pnl_convergence": {
                "sample_available": bool(committed),
                "committed_executions": committed,
                "expected_committed_pnl": expected,
                "realized_committed_pnl": realized,
                "fill_breakdown": [
                    {"expected_pnl": expected / 2, "pnl_locked": realized / 2}
                ],
                "partial_breakdown": [
                    {
                        "action": "exit",
                        "expected_pnl": expected / 2,
                        "realized_pnl": realized / 2,
                    }
                ],
            },
            "alerting": {"kill_switch": {"alert_recorded": False}},
            "shutdown": {
                "bot_task_completed": True,
                "bot_task_timeout": False,
            },
        },
    }


def test_aggregate_uses_pooled_expected_and_realized_values():
    report = summarize(
        [_session("one", 1, 1.0, -1.0), _session("two", 2, 3.0, 1.0)],
        expected_session_count=2,
    )
    assert report["batch_complete"] is True
    assert report["aggregate"]["expected_committed_pnl"] == pytest.approx(4.0)
    assert report["aggregate"]["realized_committed_pnl"] == pytest.approx(0.0)
    assert report["aggregate"]["convergence_pct"] == pytest.approx(0.0)
    assert report["aggregate"]["deviation_pct"] == pytest.approx(-100.0)
    assert report["verdict"] == "negative_convergence_failure_persists"


def test_pre_post_partition_uses_session_start_time():
    change = datetime(2026, 8, 16, 2, tzinfo=timezone.utc)
    report = summarize(
        [_session("pre", 1, 1.0, -1.0), _session("post", 2, 1.0, 1.0)],
        expected_session_count=2,
        change_at=change,
    )
    assert report["phases"]["pre_fix"]["session_count"] == 1
    assert report["phases"]["post_fix"]["session_count"] == 1
    assert report["phases"]["post_fix"]["deviation_pct"] == pytest.approx(0.0)
    assert report["execution_action_counts"] == {"clean": 2, "exit": 2}


def test_coherence_partition_preserves_preflight_only_session():
    preflight = datetime(2026, 8, 16, 2, tzinfo=timezone.utc)
    coherence = datetime(2026, 8, 16, 3, tzinfo=timezone.utc)
    settle = datetime(2026, 8, 16, 4, tzinfo=timezone.utc)
    instrumented = datetime(2026, 8, 16, 5, tzinfo=timezone.utc)
    report = summarize(
        [
            _session("pre", 1, 1.0, -1.0),
            _session("preflight", 2, 0.0, 0.0),
            _session("coherent", 3, 0.0, 0.0),
            _session("settled", 4, 0.0, 0.0),
            _session("instrumented", 5, 0.0, 0.0),
        ],
        expected_session_count=5,
        change_at=preflight,
        coherence_change_at=coherence,
        settle_change_at=settle,
        instrumented_change_at=instrumented,
    )

    assert report["coherence_phases"]["pre_coalescing"]["session_count"] == 1
    assert report["coherence_phases"]["preflight_only"]["session_count"] == 1
    assert report["coherence_phases"]["same_frame_only"]["session_count"] == 1
    assert report["coherence_phases"]["pair_barrier_probe"]["session_count"] == 1
    assert report["coherence_phases"]["post_settle"]["session_count"] == 1
    assert (
        report["verdict"]
        == "historical_sample_invalidated_current_no_committed_execution_sample"
    )
    assert report["historical_raw_verdict"] == "negative_convergence_failure_persists"
    assert report["current_evaluation_phase"] == "post_settle"
    assert report["current_verdict"] == "no_committed_execution_sample"
    assert report["convergence_resolution"] == {
        "prior_minus_100_persists_as_valid_finding": False,
        "prior_minus_100_reversed": False,
        "small_sample_noise": False,
        "measurement_artifact": True,
        "classification": (
            "cross_leg_snapshot_incoherence_measurement_artifact; "
            "corrected_evaluation_has_no_committed_execution_sample"
        ),
        "historical_raw_committed_executions": 2,
        "historical_raw_expected_pnl": 1.0,
        "historical_raw_realized_pnl": -1.0,
        "historical_raw_convergence_pct": -100.0,
        "current_phase": "post_settle",
        "current_committed_executions": 0,
        "current_expected_pnl": 0.0,
        "current_realized_pnl": 0.0,
        "current_convergence_pct": None,
    }


def test_no_committed_sample_is_reported_not_divided_by_zero():
    session = _session("zero", 1, 0.0, 0.0)
    session["report"]["event_level_pnl_convergence"]["committed_executions"] = 0
    session["report"]["event_level_pnl_convergence"]["sample_available"] = False
    report = summarize([session], expected_session_count=1)
    assert report["aggregate"]["convergence_pct"] is None
    assert report["verdict"] == "no_committed_execution_sample"


def test_failed_startup_attempt_is_audited_but_does_not_satisfy_batch_target():
    failed = _session("failed", 1, 0.0, 0.0)
    failed["report"].update(
        {
            "run_status": "failed",
            "failure_reason": "startup validation failed",
            "duration_seconds": 180.0,
            "connected_observation_seconds": 0.0,
            "shutdown": {
                "bot_task_completed": False,
                "bot_task_timeout": True,
            },
        }
    )
    completed = _session("completed", 2, 0.0, 0.0)

    report = summarize([failed, completed], expected_session_count=1)

    assert report["observed_session_count"] == 2
    assert report["qualifying_session_count"] == 1
    assert report["failed_attempt_count"] == 1
    assert report["batch_complete"] is True
    assert report["aggregate"]["crashed_sessions"] == 1
    assert report["aggregate"]["timed_out_sessions"] == 1
    assert report["qualifying_aggregate"]["crashed_sessions"] == 0
    assert report["qualifying_aggregate"]["timed_out_sessions"] == 0
    failed_row = next(row for row in report["sessions"] if row["session_id"] == "failed")
    assert failed_row["qualifies_as_completed_session"] is False
    assert failed_row["qualification_failures"] == [
        "run_status_failed",
        "bot_task_not_completed",
        "bot_task_timeout",
        "duration_below_900_seconds",
        "connected_observation_below_900_seconds",
    ]
