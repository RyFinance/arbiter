from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from polymarket_arb.dashboard_status import (
    btc_model_status,
    depth_capture_status,
    discover_pytest_status,
    kill_switch_status,
    latest_sealed_task_session,
    macro_state_status,
    paper_session_status,
    task_bot_liveness_status,
)


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _write_sealed_report(archive_root: Path, session_id: str, report: dict) -> Path:
    session_dir = archive_root / session_id
    report_path = session_dir / "session_report.json"
    _write_json(report_path, report)
    digest = hashlib.sha256(report_path.read_bytes()).hexdigest()
    (session_dir / "SHA256SUMS").write_text(f"{digest}  session_report.json\n", encoding="utf-8")
    return report_path


def _clean_report(session_id: str = "task_2_4_20260815T092241Z") -> dict:
    return {
        "session_id": session_id,
        "started_at": "1970-01-01T00:16:40+00:00",
        "finished_at": "1970-01-01T00:16:50+00:00",
        "duration_seconds": 10.0,
        "connected_observation_seconds": 9.5,
        "paper_only": True,
        "real_orders_submitted": False,
        "shutdown": {"bot_task_completed": True, "bot_task_timeout": False},
        "metrics": {
            "summary": {
                "opportunities": 0,
                "orders_sent": 0,
                "fills": 0,
                "partial_fills": 0,
                "missed": 0,
                "locked_pnl": 0.0,
                "session_pnl": 0.0,
                "would_lock": 0.0,
            }
        },
        "event_level_pnl_convergence": {
            "sample_available": False,
            "PASS": False,
            "convergence_pct": 0.0,
            "deviation_pct": 0.0,
        },
        "alerting": {
            "enabled": True,
            "kill_switch": {
                "requested": True,
                "halted": True,
                "alert_recorded": True,
                "webhook_delivered": True,
                "post_halt_probe_blocked": True,
                "post_halt_rejection_reason": "halted/emergency-stop",
                "orders_sent_before_post_halt_probe": 0,
                "orders_sent_after_post_halt_probe": 0,
            },
        },
    }


def test_completed_session_wins_over_its_recent_heartbeat(tmp_path: Path) -> None:
    archives = tmp_path / "archives"
    _write_sealed_report(archives, "task_2_4_20260815T092241Z", _clean_report())
    heartbeat = tmp_path / "HEARTBEAT"
    heartbeat.write_text("1009", encoding="utf-8")
    os.utime(heartbeat, (1009.0, 1009.0))

    status = task_bot_liveness_status((heartbeat,), archives, timeout_s=300.0, now=1020.0)

    assert status["state"] == "completed/offline"
    assert "completed" in status["detail"]


def test_newer_live_heartbeat_supersedes_completed_session(tmp_path: Path) -> None:
    archives = tmp_path / "archives"
    _write_sealed_report(archives, "task_2_4_old", _clean_report("task_2_4_old"))
    heartbeat = tmp_path / "HEARTBEAT"
    heartbeat.write_text("1015", encoding="utf-8")
    os.utime(heartbeat, (1015.0, 1015.0))

    status = task_bot_liveness_status((heartbeat,), archives, timeout_s=300.0, now=1020.0)

    assert status["state"] == "running"
    assert status["age_s"] == 5.0


def test_latest_task_session_requires_a_matching_seal(tmp_path: Path) -> None:
    archives = tmp_path / "archives"
    valid = _clean_report("task_2_4_valid")
    _write_sealed_report(archives, "task_2_4_valid", valid)
    newer = _clean_report("task_2_4_unsealed")
    newer["finished_at"] = "1970-01-01T00:20:00+00:00"
    _write_json(archives / "task_2_4_unsealed" / "session_report.json", newer)

    report = latest_sealed_task_session(archives)

    assert report["session_id"] == "task_2_4_valid"
    assert report["_sealed"] is True


def test_paper_session_preserves_zero_opportunity_no_sample_truth() -> None:
    report = _clean_report()
    report["_sealed"] = True
    report["_report_path"] = "/evidence/session_report.json"

    status = paper_session_status(report)

    assert status["completed"] is True
    assert status["opportunities"] == 0
    assert status["fills"] == 0
    assert status["session_pnl"] == 0.0
    assert status["sample_available"] is False
    assert status["convergence_pass"] is None


def test_kill_switch_separates_verified_event_from_current_arming(tmp_path: Path) -> None:
    stop = tmp_path / "emergency.stop"
    report = _clean_report()

    verified = kill_switch_status(stop, report)
    stop.write_text("halt", encoding="utf-8")
    armed = kill_switch_status(stop, report)

    assert verified["state"] == "verified-pass"
    assert verified["armed"] is False
    assert verified["verified"] is True
    assert verified["orders_before"] == verified["orders_after"] == 0
    assert armed["state"] == "armed"
    assert armed["verified"] is True


def _write_junit(path: Path, *, tests: int, failures: int = 0, errors: int = 0, skipped: int = 0) -> None:
    path.write_text(
        f'<testsuites><testsuite tests="{tests}" failures="{failures}" errors="{errors}" '
        f'skipped="{skipped}" time="12.345" timestamp="2026-08-15T00:00:00+00:00"/></testsuites>',
        encoding="utf-8",
    )


def test_test_status_uses_broadest_current_dated_junit_not_newer_focus_run(tmp_path: Path) -> None:
    full = tmp_path / "pytest_full_2026-08-15.xml"
    focused = tmp_path / "pytest_focused_2026-08-15.xml"
    old = tmp_path / "pytest_full_2026-08-14.xml"
    _write_junit(full, tests=526)
    _write_junit(focused, tests=97)
    _write_junit(old, tests=900, failures=1)
    os.utime(full, (1000.0, 1000.0))
    os.utime(focused, (2000.0, 2000.0))

    status = discover_pytest_status(tmp_path)

    assert Path(status["source"]) == full
    assert status["tests"] == 526
    assert status["passed"] == 526
    assert status["failures"] == 0
    assert status["status"] == "pass"


def test_test_status_falls_back_to_ground_truth(tmp_path: Path) -> None:
    truth = tmp_path / "ground_truth.json"
    _write_json(
        truth,
        {
            "observed_at_utc": "2026-08-15T08:20:15Z",
            "pytest": [
                {"tests": 520, "passed": 520, "failed": 0, "skipped": 0, "seconds": 84.33},
                {"tests": 520, "passed": 520, "failed": 0, "skipped": 0, "seconds": 83.86},
            ],
        },
    )

    status = discover_pytest_status(tmp_path, truth)

    assert status["source_kind"] == "ground_truth"
    assert status["passed"] == 520
    assert status["matching_runs"] == 2


def test_btc_model_status_reports_every_policy_negative_and_not_deployable(tmp_path: Path) -> None:
    evidence = tmp_path / "btc.json"
    models = {}
    means = iter((-400.0, -300.0, -200.0, -500.0, -100.0, -250.0))
    for model in ("gradient_boosted_trees", "logistic_regression"):
        models[model] = {}
        for policy in ("binary_f1", "top_decile", "confidence_weighted"):
            mean = next(means)
            models[model][policy] = {
                "net_pnl_bps": {"mean": mean, "range": [mean - 50.0, mean + 50.0]},
                "pnl_per_unit_exposure_bps": {
                    "mean": mean / 10.0,
                    "range": [(mean - 50.0) / 10.0, (mean + 50.0) / 10.0],
                },
            }
    _write_json(
        evidence,
        {
            "pnl": {
                "status": "available_btc_proxy_only",
                "warning": "BTC proxy only",
                "executable_polymarket_pnl": {"status": "unavailable"},
                "aggregate": {"models": models},
            }
        },
    )

    status = btc_model_status(evidence)

    assert status["status"] == "not deployable"
    assert status["all_policies_negative"] is True
    assert status["negative_policy_count"] == status["policy_count"] == 6
    assert status["best_policy"]["mean_net_pnl_bps"] == -100.0
    assert status["best_exposure_policy"]["mean_pnl_per_unit_exposure_bps"] == -10.0
    assert status["executable_status"] == "unavailable"


def test_depth_status_uses_consolidated_file_and_ground_truth_counts(tmp_path: Path) -> None:
    consolidated = tmp_path / "polymarket_depth_consolidated.jsonl"
    consolidated.write_bytes(b"abc")
    truth = tmp_path / "truth.json"
    replay = tmp_path / "replay.json"
    _write_json(
        truth,
        {
            "depth_capture_observation": {
                "consolidated_rows": 668483,
                "consolidated_bytes": 3,
                "consolidated_duplicate_keys": 0,
                "consolidated_chronology_violations": 0,
            }
        },
    )
    _write_json(
        replay,
        {
            "historical_input": {
                "bytes": 3,
                "source_rows": 672906,
                "depth_snapshot_coverage": {"total_ticks": 672906, "complete_ticks": 672906},
            },
            "row_count_gate": {"status": "pass"},
            "operational_window_gate": {
                "observed_complete_windows": 0,
                "required_complete_windows": 30,
            },
            "depth_snapshot": {
                "status": "verified-fail",
                "reason": "0/30 complete windows",
                "quantity": {"requested_quantity_fill_rate": 0.9830264837337552},
                "after_metrics": {"fills": 76, "partial_fills": 510, "halted_windows": 68},
            },
        },
    )

    status = depth_capture_status(consolidated, truth, replay)

    assert status["available"] is True
    assert status["rows"] == 672906
    assert status["bytes"] == 3
    assert status["evidence_matches_file"] is True
    assert status["strict_gate_status"] == "verified-fail"
    assert (status["replay_complete_ticks"], status["replay_total_ticks"]) == (672906, 672906)
    assert status["row_gate_status"] == "pass"
    assert (status["complete_windows"], status["required_complete_windows"]) == (0, 30)
    assert status["requested_quantity_fill_rate"] == 0.9830264837337552
    assert status["replay_fills"] == 76
    assert status["replay_partials"] == 510
    assert status["replay_halted_windows"] == 68


def test_macro_missing_and_stale_are_fail_safe_not_clear(tmp_path: Path) -> None:
    macro = tmp_path / "macro.json"

    missing = macro_state_status(macro, now=1000.0, max_age_s=900.0)
    _write_json(macro, {"last_update": 50.0, "events": [], "high_risk_windows": []})
    stale = macro_state_status(macro, now=1000.0, max_age_s=900.0)

    assert missing["state"] == "unavailable/fail-safe"
    assert missing["available"] is False
    assert stale["state"] == "stale/fail-safe"
    assert stale["available"] is False
    assert "clear" not in missing["state"]
    assert "clear" not in stale["state"]


def test_fresh_macro_state_reports_active_window(tmp_path: Path) -> None:
    macro = tmp_path / "macro.json"
    _write_json(
        macro,
        {
            "last_update": 990.0,
            "events": [{"title": "FOMC"}],
            "high_risk_windows": [{"start": 995.0, "end": 1005.0, "event": "FOMC"}],
        },
    )

    status = macro_state_status(macro, now=1000.0, max_age_s=900.0)

    assert status["available"] is True
    assert status["state"] == "high-risk"
    assert len(status["active_windows"]) == 1
