"""Shared pytest fixtures for the polymarket_arb test suite.

The most important role of this file is keeping tests hermetic by sandboxing
the project's on-disk alert sink. ``polymarket_arb.alerting.ALERTS_LOG_FILE`` is a
module-level relative path (``state/alerts.jsonl``) that ``_write_alert_record``
falls back to whenever a ``WebhookAlerter`` is constructed without an explicit
``alerts_path``. Several historical tests built alerters that way, so every
``alerter.alert(...)`` append-wrote to the *real* project file under ``state/``.

That leak caused two classes of nondeterminism across pytest runs:

1. The shared file grew across runs, so any later script/test that enumerated or
   counted ``state/alerts.jsonl`` records saw different totals depending on the
   history of which-tests-ran-when.
2. Author-alerters-created-without-explicit-routes silently produced on-disk
   side effects that could escape the test process (e.g. a follow-up integration
   script reading the polluted file).

The ``isolate_alerts_log`` autouse fixture below redirects the module attribute
to a per-test tmp_path for *every* test in the suite, so no test can write to
real ``state/alerts.jsonl`` regardless of how it constructs its alerter. Tests
that explicitly patch ``ALERTS_LOG_FILE`` themselves still see their per-test
tmp path (the patch shadows the fixture's setattr).
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolate_alerts_log(tmp_path, monkeypatch):
    """Redirect ``polymarket_arb.alerting.ALERTS_LOG_FILE`` to a per-test tmp file.

    Guarantees no test in the suite touches the real ``state/alerts.jsonl``.
    Individual tests that already pass ``alerts_path=...`` to ``WebhookAlerter``
    are unaffected (the explicit arg precedence at alerting.py:130 takes over).
    Tests that patch ``polymarket_arb.alerting.ALERTS_LOG_FILE`` themselves
    likewise work — monkeypatch.setattr inside the test body shadows the
    fixture's assignment until the fixture unwinds.

    The fixture also nests the path under a real ``state/`` subdirectory so any
    code that interpolates the dirname (`os.path.dirname(alerts_path)`) at
    alerting.py:131 sees a directory that needs to be created exactly as in
    production.
    """
    alerts_dir = tmp_path / "state"
    alerts_dir.mkdir(parents=True, exist_ok=True)
    alerts_file = alerts_dir / "alerts.jsonl"
    import polymarket_arb.alerting as _alerting
    monkeypatch.setattr(_alerting, "ALERTS_LOG_FILE", str(alerts_file))
    # Neutralize macro risk so tests don't pick up whatever is in
    # state/macro_events.json on this machine (which can be populated with
    # 284 real events whenever the LSE scraper has been run). Resetting the
    # cache globals was insufficient — _high_macro_risk -> _load_macro_events
    # reloads from disk when the cache is None. Patch `_read_macro_state`
    # instead so the default no-cache path returns a neutral state, while
    # tests that explicitly populate `_macro_cache` (e.g. test_macro_risk.py /
    # test_macro_event_accuracy.py) are unaffected — they bypass the read at
    # risk.py:123 (`_macro_cache is not None` short-circuit) entirely, so the
    # patched reader never runs for them.
    import polymarket_arb.risk as _risk
    # Missing, unreadable, zero-timestamp, and stale states are hard failures.
    # Return a legitimately EMPTY and freshly timestamped MacroEventState so
    # unrelated tests exercise normal risk checks rather than the macro fail-safe.
    # Tests that inject their own state into `_macro_cache` bypass this reader.
    module, ok = _risk._maybe_page_agent()
    if ok and module is not None:
        neutral_path = alerts_dir / "macro_events.json"
        neutral_path.touch()
        monkeypatch.setattr(module, "MACRO_EVENTS_PATH", str(neutral_path))

        def _fresh_empty_macro_state():
            return module.MacroEventState.from_dict({
                "events": [],
                "last_update": time.time(),
                "high_risk_windows": [],
            })

        monkeypatch.setattr(_risk, "_read_macro_state", _fresh_empty_macro_state)
    else:
        monkeypatch.setattr(_risk, "_read_macro_state", lambda: None)
    # Keep process-global macro state hermetic across tests. Individual macro tests
    # may still install a cache after fixture setup; monkeypatch restores these
    # neutral values at teardown.
    monkeypatch.setattr(_risk, "_macro_cache", None)
    monkeypatch.setattr(_risk, "_macro_cache_epoch", 0.0)
    monkeypatch.setattr(_risk, "_macro_state_error", None)
    monkeypatch.setattr(_risk, "_macro_state_error_code", None)
    # Keep live-control submission checks and state-change audits hermetic too.
    # TradingClient's default control is process-global and otherwise writes to
    # state/live_control_audit.jsonl even when a unit test uses a paper client.
    import polymarket_arb.live_control as _live_control
    test_control = _live_control.LiveTradingControl(
        alerts_dir / "live_control.json",
        alerts_dir / "live_control_audit.jsonl",
    )
    monkeypatch.setenv("LIVE_CONTROL_STATE_PATH", str(test_control.state_path))
    monkeypatch.setenv("LIVE_CONTROL_AUDIT_PATH", str(test_control.audit_path))
    monkeypatch.setattr(_live_control, "_DEFAULT_CONTROL", test_control)
    # Some tests construct WebhookAlerter lazily so the module attribute is read
    # at alert() call-time (alerting.py:130). Monkeypatch is enough because the
    # attribute lookup hits the module namespace every call — no chdir needed.
    # Tests that themselves patch ALERTS_LOG_FILE or pass an explicit
    # `alerts_path` are unaffected (explicit arg precedence at alerting.py:130
    # takes over; a test's own monkeypatch.setattr shadows the fixture's setattr
    # for the duration of that test and unwinds after).
    # apply_regime_to_settings() writes a small regime-state JSON file for the
    # dashboard alongside its SETTINGS mutation — same leak class as alerts
    # above (a relative "state/..." path resolved against cwd), so it gets the
    # same treatment.
    import polymarket_arb.kronos_session as _kronos_session
    monkeypatch.setattr(_kronos_session, "REGIME_STATE_PATH", str(alerts_dir / "regime_active.json"))
    yield alerts_file


# Tests that need local artifacts kept out of version control: trained models,
# recorded paper-session archives, raw market data and a machine-specific launchd plist.
# A fresh clone has none of them, so these skip with a reason instead of failing.
# Set ARBITER_LOCAL_ARTIFACTS=1 to run them on a machine that has the artifacts.
_LOCAL_ARTIFACT_TESTS = {
    "tests/test_calc_pnl_convergence.py::test_parse_bot_jsonl_extracts_fills_and_missed",
    "tests/test_dashboard_product.py::test_dashboard_demo_mode_replays_sealed_archive_offline",
    "tests/test_depth_capture_integration.py::TestIsolationGuarantees::test_settings_paper_trade_true",
    "tests/test_label_leakage_fix.py::test_tercile_cutoffs_from_train_only",
    "tests/test_label_leakage_fix.py::test_persisted_v2_cutoffs_match_recomputed_train_partition_quantiles",
    "tests/test_label_leakage_fix.py::test_three_holdout_evaluation",
    "tests/test_label_leakage_fix.py::test_missing_dead_feature",
    "tests/test_label_leakage_fix.py::test_prediction_not_called_signal",
    "tests/test_label_leakage_fix.py::test_no_forward_looking_contamination",
    "tests/test_paper_trade_v3_gated.py::test_load_two_stage_model_exists",
    "tests/test_paper_trade_v3_gated.py::test_feature_frame_reproducibility",
    "tests/test_paper_trade_v3_gated.py::test_two_stage_predict_shape",
    "tests/test_paper_trade_v3_gated.py::test_two_stage_predict_holdout_c_reproducibility",
    "tests/test_paper_trade_v3_gated.py::test_abstention_gate_at_0_7",
    "tests/test_paper_trade_v3_gated.py::test_abstention_gate_empty_when_threshold_too_high",
    "tests/test_supervise_bootstrap.py::TestRepoPlist::test_plist_semantics_are_complete",
    "tests/test_validate_backtest.py::test_extract_paper_trading_pnl_matches_fill_fidelity_archive",
}


def pytest_collection_modifyitems(config, items):
    import os
    if os.environ.get("ARBITER_LOCAL_ARTIFACTS") == "1":
        return
    skip = pytest.mark.skip(reason="needs local model/data artifacts that are not shipped in the repo")
    for item in items:
        if item.nodeid in _LOCAL_ARTIFACT_TESTS:
            item.add_marker(skip)
