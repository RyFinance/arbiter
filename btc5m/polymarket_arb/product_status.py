"""Single-source, evidence-backed product and go-live status reporting."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from polymarket_arb.dashboard_status import btc_model_status, discover_pytest_status
from polymarket_arb.demo_replay import verify_archive
from polymarket_arb.live_control import LiveTradingControl


VALID_STATUSES = {
    "verified-pass",
    "verified-fail",
    "blocked-external",
    "not-yet-tested",
}


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _first_existing(*paths: Path) -> Path:
    return next((path for path in paths if path.is_file()), paths[-1])


def _iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc)


def _gate(
    name: str,
    status: str,
    value: str,
    criterion: str,
    evidence: list[Path | str],
    *,
    mandatory: bool = True,
) -> dict[str, Any]:
    if status not in VALID_STATUSES:
        raise ValueError(f"invalid gate status: {status}")
    return {
        "Gate": name,
        "Status": status,
        "Current value": value,
        "Criterion": criterion,
        "Evidence": [str(item) for item in evidence],
        "Mandatory": mandatory,
    }


def _session_reports_for_local_date(
    archive_root: Path,
    evidence_date: str,
    timezone_name: str,
) -> list[tuple[Path, dict[str, Any], bool]]:
    zone = ZoneInfo(timezone_name)
    rows: list[tuple[Path, dict[str, Any], bool]] = []
    for report_path in archive_root.glob("task_2_4_*/session_report.json"):
        report = _load(report_path)
        timestamp = _iso(report.get("started_at"))
        if (
            timestamp is None
            or timestamp.astimezone(zone).date().isoformat() != evidence_date
        ):
            continue
        sealed, _ = verify_archive(report_path.parent)
        rows.append((report_path, report, sealed))
    return sorted(rows, key=lambda row: str(row[1].get("started_at", "")))


def _kill_verified(report: dict[str, Any]) -> bool:
    kill = report.get("alerting", {}).get("kill_switch", {})
    return all(
        (
            report.get("alerting", {}).get("enabled") is True,
            kill.get("requested") is True,
            kill.get("halted") is True,
            kill.get("alert_recorded") is True,
            kill.get("webhook_delivered") is True,
            kill.get("post_halt_probe_blocked") is True,
            kill.get("post_halt_rejection_reason") == "halted/emergency-stop",
            kill.get("orders_sent_before_post_halt_probe")
            == kill.get("orders_sent_after_post_halt_probe"),
            kill.get("orders_sent_before_post_halt_probe") is not None,
        )
    )


def _depth_values(report: dict[str, Any]) -> dict[str, Any]:
    row = report.get("row_count_gate", {})
    continuity = report.get("operational_window_gate", {})
    snapshot = report.get("depth_snapshot", {})
    metrics = snapshot.get("after_metrics", {}) if isinstance(snapshot, dict) else {}
    quantity = snapshot.get("quantity", {}) if isinstance(snapshot, dict) else {}
    return {
        "status": snapshot.get("status", "not-yet-tested"),
        "rows": int(row.get("observed_complete_paired_ticks", 0) or 0),
        "required_rows": int(
            row.get("required_complete_paired_ticks", 500_000) or 500_000
        ),
        "row_gap": int(row.get("gap_remaining", 500_000) or 0),
        "row_status": row.get("status", "fail"),
        "windows": int(continuity.get("observed_complete_windows", 0) or 0),
        "required_windows": int(continuity.get("required_complete_windows", 30) or 30),
        "window_gap": int(continuity.get("gap_remaining", 30) or 0),
        "window_status": continuity.get("status", "fail"),
        "fills": int(metrics.get("fills", 0) or 0),
        "opportunities": int(metrics.get("opportunities", 0) or 0),
        "orders_sent": int(metrics.get("orders_sent", 0) or 0),
        "partials": int(metrics.get("partial_fills", 0) or 0),
        "partial_resolved": int(metrics.get("partial_resolved", 0) or 0),
        "halted_windows": int(metrics.get("halted_windows", 0) or 0),
        "session_pnl": metrics.get("session_pnl"),
        "fill_ratio": quantity.get("requested_quantity_fill_rate"),
        "requested_quantity": quantity.get("requested_buy_quantity"),
        "filled_quantity": quantity.get("filled_buy_quantity"),
    }


def build_product_status(
    root: Path,
    *,
    evidence_date: str,
    timezone_name: str = "America/Vancouver",
) -> dict[str, Any]:
    """Build every dashboard/checklist row from concrete machine artifacts."""
    root = root.resolve()
    state = root / "state"
    docs = root / "docs"
    final_batch_path = state / f"rest_paper_batch_{evidence_date}.json"
    interim_batch_path = state / f"rest_paper_batch_interim_{evidence_date}.json"
    batch_candidates = [
        (final_batch_path, _load(final_batch_path)),
        (interim_batch_path, _load(interim_batch_path)),
    ]
    batch_path, batch = max(
        batch_candidates,
        key=lambda item: (
            bool(item[1].get("batch_complete")),
            int(item[1].get("observed_session_count", 0) or 0),
            item[0].stat().st_mtime if item[0].exists() else 0.0,
        ),
    )
    aggregate = (
        batch.get("aggregate", {}) if isinstance(batch.get("aggregate"), dict) else {}
    )
    qualifying_aggregate = (
        batch.get("qualifying_aggregate", {})
        if isinstance(batch.get("qualifying_aggregate"), dict)
        else aggregate
    )
    depth_path = _first_existing(
        state / f"depth_replay_validation_combined_{evidence_date}.json",
        state / f"depth_replay_validation_postfix_{evidence_date}.json",
        state / f"depth_replay_validation_supervised_{evidence_date}.json",
        state / f"chunk_b_depth_replay_validation_{evidence_date}.json",
    )
    depth = _depth_values(_load(depth_path))
    consolidation_path = state / f"depth_consolidation_{evidence_date}.json"
    continuity_path = state / f"depth_continuity_progress_{evidence_date}.json"
    continuity = _load(continuity_path)
    supervisor_path = (
        state / f"capture_supervisor_rollover_postfix_{evidence_date}.json"
    )
    supervisor = _load(supervisor_path)
    skew_path = state / f"cross_leg_capture_skew_{evidence_date}.json"
    skew = _load(skew_path)
    control_verify_path = state / f"live_control_verification_{evidence_date}.json"
    control_verify = _load(control_verify_path)
    control = LiveTradingControl(
        state / "live_control.json", state / "live_control_audit.jsonl"
    ).read_state()
    wallet_path = state / "wallet_status.json"
    wallet = _load(wallet_path)
    setup_path = state / f"setup_wizard_verification_{evidence_date}.json"
    setup = _load(setup_path)
    dashboard_path = state / f"dashboard_product_verification_{evidence_date}.json"
    dashboard = _load(dashboard_path)
    dashboard_control_e2e_path = (
        state / f"dashboard_live_control_e2e_{evidence_date}.json"
    )
    dashboard_control_e2e = _load(dashboard_control_e2e_path)
    dashboard_failure_path = (
        state / f"dashboard_product_verification_failure_{evidence_date}.json"
    )
    dashboard_form_failure_path = (
        state / f"dashboard_product_verification_form_failure_{evidence_date}.json"
    )
    btc_path = (
        docs
        / "research"
        / f"btc_offline_confidence_weighted_sizing_{evidence_date}.json"
    )
    btc = btc_model_status(btc_path)
    backtest_path = state / "backtest_validation_report.json"
    backtest = _load(backtest_path)
    backtest_metrics = (
        backtest.get("backtest", {}) if isinstance(backtest, dict) else {}
    )
    macro_path = state / "macro_events.json"
    macro = _load(macro_path)
    try:
        macro_age_s = datetime.now(timezone.utc).timestamp() - float(
            macro["last_update"]
        )
    except (KeyError, TypeError, ValueError):
        macro_age_s = None
    test_status = discover_pytest_status(
        state, state / f"ground_truth_{evidence_date}.json"
    )
    sessions = _session_reports_for_local_date(
        state / "paper_session_archives", evidence_date, timezone_name
    )
    kill_matches = [row for row in sessions if row[2] and _kill_verified(row[1])]
    kill_path, kill_report, kill_sealed = (
        kill_matches[-1]
        if kill_matches
        else (
            state / "paper_session_archives",
            {},
            False,
        )
    )
    kill = kill_report.get("alerting", {}).get("kill_switch", {})

    session_count = len(sessions)
    sealed_count = sum(sealed for _, _, sealed in sessions)
    paper_safe = bool(sessions) and all(
        sealed
        and report.get("paper_only") is True
        and report.get("real_orders_submitted") is False
        for _, report, sealed in sessions
    )
    batch_complete = bool(batch.get("batch_complete"))
    observed_batch = int(batch.get("observed_session_count", 0) or 0)
    qualifying_batch = int(batch.get("qualifying_session_count", observed_batch) or 0)
    opportunities = int(aggregate.get("opportunities", 0) or 0)
    fills = int(aggregate.get("fills", 0) or 0)
    partials = int(aggregate.get("partial_fills", 0) or 0)
    expected = aggregate.get("expected_committed_pnl")
    realized = aggregate.get("realized_committed_pnl")
    convergence = aggregate.get("convergence_pct")
    deviation = aggregate.get("deviation_pct")
    post_fix = batch.get("phases", {}).get("post_fix", {})
    coherent = batch.get("coherence_phases", {}).get("post_settle", {}) or batch.get(
        "coherence_phases", {}
    ).get("post_coalescing", {})
    current_execution = coherent or post_fix
    current_opportunities = int(current_execution.get("opportunities", 0) or 0)
    current_latency = current_execution.get("opportunity_to_first_order_latency", {})
    current_quote_age = current_execution.get("filled_quote_age", {})
    current_quote_pct = current_execution.get("filled_quote_freshness_pct")

    gates: list[dict[str, Any]] = []
    gates.append(
        _gate(
            "Today’s execution safety invariant",
            "verified-pass" if paper_safe else "verified-fail",
            f"sealed PAPER_TRADE sessions={sealed_count}/{session_count}; real orders=0",
            "Every market session today is sealed, paper-only, and records zero real orders.",
            [path for path, _, _ in sessions[-3:]]
            or [state / "paper_session_archives"],
        )
    )
    gates.append(
        _gate(
            "REST paper closeout batch",
            (
                "verified-pass"
                if batch_complete and 15 <= qualifying_batch <= 20
                else "verified-fail"
            ),
            (
                f"qualifying_sessions={qualifying_batch}/15 minimum; "
                f"attempts={observed_batch}; failed_attempts={batch.get('failed_attempt_count')}; "
                f"opportunities={opportunities}; "
                f"fills={fills}; partials={partials}; rest_preflight="
                f"{aggregate.get('rest_preflight_passed')} pass/"
                f"{aggregate.get('rest_preflight_rejected')} reject; post_settle_sessions="
                f"{coherent.get('session_count')}; post_settle_opportunities="
                f"{current_opportunities}"
            ),
            "Run 15–20 additional sealed REST-book paper sessions.",
            [batch_path],
        )
    )
    gates.append(
        _gate(
            "Opportunity observation volume",
            (
                "verified-pass"
                if batch_complete and current_opportunities >= 100
                else "verified-fail"
            ),
            (
                f"post_settle_fire_opportunities={current_opportunities}/100 minimum; "
                f"whole_batch_raw={opportunities}"
            ),
            "At least 100 real-data fire opportunities are observed before any go-live review.",
            [batch_path],
        )
    )
    distinct_windows = int(current_execution.get("distinct_window_markets", 0) or 0)
    gates.append(
        _gate(
            "Market-window coverage",
            (
                "verified-pass"
                if batch_complete and distinct_windows >= 7
                else "verified-fail"
            ),
            (
                f"distinct_window_markets={distinct_windows}; "
                f"rollovers_observed={current_execution.get('window_rollovers_observed')}"
            ),
            "Paper evidence spans at least seven distinct rolling markets.",
            [batch_path],
        )
    )
    gates.append(
        _gate(
            "Cross-leg frame coherence",
            (
                "verified-pass"
                if batch_complete
                and int(coherent.get("session_count", 0) or 0) > 0
                and int(coherent.get("cross_leg_coalescing_sessions", 0) or 0)
                == int(coherent.get("session_count", 0) or 0)
                and int(coherent.get("cross_leg_coalesced_market_count", 0) or 0) > 0
                and coherent.get("cross_leg_settle_ms_values") == [10.0]
                and int(coherent.get("cross_leg_summary_session_count", 0) or 0)
                == int(coherent.get("session_count", 0) or 0)
                and int(skew.get("transient_candidates_within_horizon", 0) or 0) > 0
                and int(skew.get("pair_barrier_edge_candidates", -1)) >= 0
                and int(skew.get("pair_barrier_edge_candidates", -1))
                <= int(skew.get("apparent_edge_candidates", -1))
                and test_status.get("status") == "pass"
                else "verified-fail"
            ),
            (
                f"captured_raw_candidates={skew.get('apparent_edge_candidates')}; "
                f"transient_within_50ms={skew.get('transient_candidates_within_horizon')}; "
                f"transient_pct={skew.get('transient_candidate_pct')}; "
                f"pair_barrier_candidates={skew.get('pair_barrier_edge_candidates')}; "
                f"offline_incomplete_timeouts={skew.get('pair_barrier_incomplete_timeouts')}; "
                f"coalescing_runtime_sessions={coherent.get('cross_leg_coalescing_sessions')}/"
                f"{coherent.get('session_count')}; coalesced_runtime_markets="
                f"{coherent.get('cross_leg_coalesced_market_count')}; settle_ms="
                f"{coherent.get('cross_leg_settle_ms_values')}; incomplete_suppression_markets="
                f"{coherent.get('cross_leg_incomplete_suppression_market_count')}; "
                f"coherent_evaluations={coherent.get('cross_leg_coherent_evaluations')}; "
                f"incomplete_suppressions={coherent.get('cross_leg_incomplete_suppressions')}"
            ),
            "Adjacent paired books are held for 10 ms and fully applied before arbitrage evaluation.",
            [
                skew_path,
                root / "tests" / "test_price_feed_pair_coalescing.py",
                batch_path,
                str(test_status.get("source", state)),
            ],
        )
    )
    gates.append(
        _gate(
            "Live master flag / exchange endpoint",
            "verified-fail",
            (
                f"operator_enabled={control.operator_enabled}; "
                f"effective={control.effective_live_submission_enabled}; "
                f"hard_block={control.hard_block_reason}"
            ),
            "A live endpoint may only be considered after every mandatory gate passes; it is intentionally unbuilt.",
            [state / "live_control.json", control_verify_path],
        )
    )
    gates.append(
        _gate(
            "Submission gate enforcement + audit",
            "verified-pass"
            if control_verify.get("passed") is True
            else "verified-fail",
            (
                "off and armed probes both blocked; exchange POST attempts="
                f"{control_verify.get('operator_off_probe', {}).get('exchange_order_post_attempts', 'n/a')}/"
                f"{control_verify.get('operator_armed_probe', {}).get('exchange_order_post_attempts', 'n/a')}"
            ),
            "Every submission call site checks the control; both flag states make zero exchange POST attempts.",
            [control_verify_path, state / "live_control_audit.jsonl"],
        )
    )
    balances = wallet.get("balances", {})
    approvals = wallet.get("approvals", {})
    wallet_read_only = all(
        (
            wallet.get("read_only") is True,
            wallet.get("transaction_methods_available") is False,
            set(wallet.get("rpc_methods_allowed", []))
            == {"eth_call", "eth_getBalance"},
            _iso(wallet.get("checked_at")) is not None,
            balances.get("polygon_native_wei") is not None,
            balances.get("pusd_raw") is not None,
            all(value is not None for value in approvals.values()),
            len(approvals) == 4,
        )
    )
    gates.append(
        _gate(
            "Read-only wallet checker",
            "verified-pass" if wallet_read_only else "verified-fail",
            (
                f"methods={wallet.get('rpc_methods_allowed')}; "
                f"transaction_methods_available={wallet.get('transaction_methods_available')}; "
                f"checked_at={wallet.get('checked_at')}; "
                f"configured_rpc_healthy={wallet.get('configured_rpc_healthy')}; "
                f"rpc_fallback_used={wallet.get('rpc_fallback_used')}; "
                f"rpc_endpoints={wallet.get('rpc_endpoints_used')}; approval_reads={len(approvals)}/4"
            ),
            "Only eth_getBalance and eth_call are permitted; no transaction method exists.",
            [wallet_path],
        )
    )
    gates.append(
        _gate(
            "Wallet funding / approvals",
            "verified-pass" if wallet.get("trading_ready") is True else "verified-fail",
            (
                f"native={balances.get('polygon_native')}; pUSD={balances.get('pusd')}; "
                f"required_pUSD={balances.get('required_pusd')}; hard_failures="
                f"{wallet.get('hard_failure_count')}; unknown={wallet.get('unknown_count')}; "
                f"configured_rpc_healthy={wallet.get('configured_rpc_healthy')}; "
                f"rpc_fallback_used={wallet.get('rpc_fallback_used')}; "
                f"funding_ready={wallet.get('funding_ready')}; "
                f"approvals_ready={wallet.get('approvals_ready')}; "
                f"trading_ready={wallet.get('trading_ready')}"
            ),
            "Wallet has required pUSD, gas, allowances, approvals, and a healthy configured RPC.",
            [wallet_path],
        )
    )
    jurisdiction = wallet.get("jurisdiction", {})
    jurisdiction_status = (
        "verified-pass"
        if jurisdiction.get("status") == "clear"
        else "blocked-external"
        if jurisdiction.get("status") == "blocked"
        else "not-yet-tested"
    )
    gates.append(
        _gate(
            "Jurisdiction / geoblock",
            jurisdiction_status,
            (
                f"status={jurisdiction.get('status')}; country={jurisdiction.get('country')}; "
                f"region={jurisdiction.get('region')}"
            ),
            "Polymarket geoblock response is clear from the actual machine; bypass is prohibited.",
            [wallet_path],
        )
    )
    gates.append(
        _gate(
            "Kill switch with alerting",
            "verified-pass" if kill_matches else "verified-fail",
            (
                f"halted={kill.get('halted')}; alert_recorded={kill.get('alert_recorded')}; "
                f"webhook_delivered={kill.get('webhook_delivered')}; orders="
                f"{kill.get('orders_sent_before_post_halt_probe')}->"
                f"{kill.get('orders_sent_after_post_halt_probe')}"
            ),
            "Alerting is enabled, halt persists, webhook delivers, and a post-halt probe cannot add an order.",
            [kill_path],
        )
    )
    gates.append(
        _gate(
            "Alert persistence",
            "verified-pass"
            if kill_sealed and kill.get("alert_recorded") is True
            else "verified-fail",
            (
                f"session_alert_records={kill_report.get('alerting', {}).get('session_alert_records')}; "
                f"archive_seal_valid={kill_sealed}"
            ),
            "A real alert-enabled paper halt is durable in a hash-sealed session archive.",
            [kill_path, state / "alerts.jsonl"],
        )
    )
    current_expected = current_execution.get("expected_committed_pnl")
    current_realized = current_execution.get("realized_committed_pnl")
    current_convergence = current_execution.get("convergence_pct")
    current_deviation = current_execution.get("deviation_pct")
    current_committed = int(current_execution.get("committed_executions", 0) or 0)
    pnl_status = "not-yet-tested"
    if batch_complete and current_committed:
        pnl_status = (
            "verified-pass"
            if current_deviation is not None and abs(float(current_deviation)) <= 10
            else "verified-fail"
        )
    gates.append(
        _gate(
            "PnL convergence",
            pnl_status,
            (
                f"expected={expected}; realized={realized}; convergence_pct={convergence}; "
                f"deviation_pct={deviation}; n={aggregate.get('committed_executions', 0)}; "
                f"current_expected={current_expected}; current_realized={current_realized}; "
                f"current_convergence_pct={current_convergence}; "
                f"current_deviation_pct={current_deviation}; current_n={current_committed}; "
                f"fresh_REST_full_size_pairs="
                f"{current_execution.get('rest_preflight_full_size_pair_count')}; "
                "expected_formula=(1-detection_combined)*filled_size"
            ),
            "Realized committed PnL is within ±10% of detection-time expected PnL over the completed REST batch.",
            [batch_path],
        )
    )
    quote_n = int(current_quote_age.get("n", 0) or 0)
    quote_status = "not-yet-tested"
    if batch_complete and quote_n:
        quote_status = (
            "verified-pass"
            if quote_n >= 100 and float(current_quote_pct or 0) >= 95.0
            else "verified-fail"
        )
    gates.append(
        _gate(
            "Quote freshness",
            quote_status,
            (
                f"within_500ms={current_execution.get('filled_quotes_within_500ms')}/{quote_n}; "
                f"pct={current_quote_pct}; p95_ms={current_quote_age.get('p95_ms')}"
            ),
            "At least 100 qualifying fills, with 95% or more no older than 500 ms.",
            [batch_path],
        )
    )
    latency_n = int(current_latency.get("n", 0) or 0)
    latency_status = "not-yet-tested"
    if batch_complete and latency_n:
        latency_status = (
            "verified-pass"
            if latency_n >= 100
            and float(current_latency.get("p95_ms") or float("inf")) < 250.0
            else "verified-fail"
        )
    gates.append(
        _gate(
            "End-to-end opportunity latency",
            latency_status,
            (
                f"n={latency_n}; avg_ms={current_latency.get('avg_ms')}; "
                f"p50_ms={current_latency.get('p50_ms')}; "
                f"p95_ms={current_latency.get('p95_ms')}; "
                f"p99_ms={current_latency.get('p99_ms')}"
            ),
            "At least 100 fires with opportunity-to-first-order p95 below 250 ms.",
            [batch_path],
        )
    )
    clean_rate = current_execution.get("clean_fill_rate_per_opportunity")
    partial_rate = current_execution.get("partial_fill_rate_per_opportunity")
    current_orders_sent = int(current_execution.get("orders_sent", 0) or 0)
    fidelity_status = "not-yet-tested"
    if batch_complete and current_orders_sent:
        fidelity_status = (
            "verified-pass"
            if float(clean_rate or 0) >= 0.90 and float(partial_rate or 0) < 0.02
            else "verified-fail"
        )
    gates.append(
        _gate(
            "Two-leg fill fidelity / partial rate",
            fidelity_status,
            (
                f"opportunities={current_opportunities}; orders_sent={current_orders_sent}; "
                f"clean_rate={clean_rate}; partial_rate={partial_rate}; "
                f"unresolved_partials={current_execution.get('unresolved_partials')}"
            ),
            "After actual paper submission attempts, at least 90% clean two-leg fills and under 2% partial fills per opportunity.",
            [batch_path],
        )
    )
    gates.append(
        _gate(
            "Trading-bot window rollover",
            (
                "verified-pass"
                if batch_complete
                and int(qualifying_aggregate.get("window_rollovers_observed", 0) or 0)
                > 0
                and int(qualifying_aggregate.get("stale_token_errors", 0) or 0) == 0
                else "verified-fail"
            ),
            (
                "qualifying cohort: "
                f"rollovers={qualifying_aggregate.get('window_rollovers_observed')}; "
                f"stale_token_errors={qualifying_aggregate.get('stale_token_errors')}; "
                f"WS recoveries={qualifying_aggregate.get('ws_recovery_messages')}"
            ),
            "The actual paper bot crosses windows with zero stale-token errors.",
            [batch_path],
        )
    )
    gates.append(
        _gate(
            "REST batch process stability",
            (
                "verified-pass"
                if batch_complete
                and int(qualifying_aggregate.get("crashed_sessions", 0) or 0) == 0
                and int(qualifying_aggregate.get("timed_out_sessions", 0) or 0) == 0
                and int(qualifying_aggregate.get("ws_disconnect_severe_count", 0) or 0)
                == 0
                else "verified-fail"
            ),
            (
                "qualifying cohort: "
                f"crashed={qualifying_aggregate.get('crashed_sessions')}; "
                f"timed_out={qualifying_aggregate.get('timed_out_sessions')}; "
                "severe_disconnects="
                f"{qualifying_aggregate.get('ws_disconnect_severe_count')}; "
                f"failed_startup_attempts_preserved={batch.get('failed_attempt_count')}"
            ),
            "No crashes, task timeouts, or severe disconnects across the completed batch.",
            [batch_path],
        )
    )
    macro_fresh = macro_age_s is not None and 0.0 <= macro_age_s <= 900.0
    gates.append(
        _gate(
            "Macro-state fail-safe",
            (
                "verified-pass"
                if macro_fresh and test_status.get("status") == "pass"
                else "verified-fail"
            ),
            (
                f"events={len(macro.get('events', [])) if isinstance(macro.get('events'), list) else None}; "
                f"high_risk_windows="
                f"{len(macro.get('high_risk_windows', [])) if isinstance(macro.get('high_risk_windows'), list) else None}; "
                f"age_s={macro_age_s}; max_age_s=900.0"
            ),
            "Current macro state is fresh; missing, invalid, unreadable, or stale state fails closed in regression tests.",
            [
                macro_path,
                root / "tests" / "test_macro_risk.py",
                str(test_status.get("source", state)),
            ],
        )
    )
    template_misses = int(
        batch.get("miss_reasons", {}).get("template_unavailable", 0) or 0
    )
    gates.append(
        _gate(
            "Missing-template observability",
            "verified-pass" if test_status.get("status") == "pass" else "verified-fail",
            (
                f"runtime_template_unavailable={template_misses}; reachable release-race "
                "path increments missed/template_unavailable and emits structured logs"
            ),
            "A missing execution template is never silently dropped and the runtime batch reports its exact count.",
            [
                batch_path,
                root / "tests" / "test_execution_capital_accounting.py",
                str(test_status.get("source", state)),
            ],
        )
    )
    gates.append(
        _gate(
            "Depth-capture singleton across rollover",
            "verified-pass"
            if supervisor.get("status") == "verified-pass"
            else "verified-fail",
            (
                f"duration_s={supervisor.get('duration_s')}; samples={supervisor.get('sample_count')}; "
                f"process_count={supervisor.get('minimum_process_count')}.."
                f"{supervisor.get('maximum_process_count')}; duplicates="
                f"{supervisor.get('duplicate_process_samples')}; zero_process="
                f"{supervisor.get('zero_process_samples')}; pid_sets="
                f"{supervisor.get('unique_pid_sets')}; rollovers="
                f"{supervisor.get('window_rollovers_observed')}; markets="
                f"{supervisor.get('markets_observed')}"
            ),
            "A live ps sample spans a rollover with exactly one capture process at every sample.",
            [supervisor_path],
        )
    )
    gates.append(
        _gate(
            "Real depth paired-row volume",
            "verified-pass" if depth["row_status"] == "pass" else "verified-fail",
            f"{depth['rows']}/{depth['required_rows']} paired ticks; gap={depth['row_gap']}",
            "At least 500,000 causal, complete paired ticks.",
            [consolidation_path, depth_path],
        )
    )
    gates.append(
        _gate(
            "Real depth 30-window continuity",
            (
                "verified-pass"
                if depth["window_status"] == "pass"
                and continuity.get("status") == "verified-pass"
                else "verified-fail"
            ),
            (
                f"live_capture={continuity.get('observed_complete_windows')}/"
                f"{continuity.get('required_complete_windows')}; "
                f"closed={continuity.get('closed_windows')}; "
                f"incomplete={continuity.get('incomplete_closed_windows')}; "
                f"complete_pct={continuity.get('complete_closed_window_pct')}; "
                f"longest_complete_streak="
                f"{continuity.get('longest_consecutive_complete_windows')}; "
                f"current_complete_streak="
                f"{continuity.get('current_consecutive_complete_windows')}; "
                f"strict_replay={depth['windows']}/{depth['required_windows']}; "
                f"gap={depth['window_gap']}; fills={depth['fills']}; "
                f"partials={depth['partials']}; halted={depth['halted_windows']}"
            ),
            "Both tokens meet first≤5s, last≥295s, max-gap≤60s for 30 windows.",
            [continuity_path, depth_path],
        )
    )
    gates.append(
        _gate(
            "Requested-quantity accounting",
            "verified-pass" if depth["fill_ratio"] is not None else "not-yet-tested",
            (
                f"requested={depth['requested_quantity']}; filled={depth['filled_quantity']}; "
                f"fill_ratio={depth['fill_ratio']}"
            ),
            "Replay reports uncapped requested and actual ladder-filled quantities; this is accounting, not venue fill proof.",
            [depth_path, root / "tests" / "test_backtest_depth_replay.py"],
        )
    )
    depth_opportunities = depth["opportunities"]
    depth_clean_rate = (
        depth["fills"] / depth_opportunities if depth_opportunities else None
    )
    depth_partial_rate = (
        depth["partials"] / depth_opportunities if depth_opportunities else None
    )
    depth_execution_status = "not-yet-tested"
    if depth_opportunities:
        depth_execution_status = (
            "verified-pass"
            if depth_opportunities >= 100
            and float(depth_clean_rate or 0) >= 0.90
            and float(depth_partial_rate or 0) < 0.02
            and depth["halted_windows"] == 0
            else "verified-fail"
        )
    gates.append(
        _gate(
            "Strict depth-replay execution quality",
            depth_execution_status,
            (
                f"opportunities={depth_opportunities}; orders_sent={depth['orders_sent']}; "
                f"clean_fills={depth['fills']}; clean_rate={depth_clean_rate}; "
                f"partials={depth['partials']}; partial_rate={depth_partial_rate}; "
                f"partial_resolved={depth['partial_resolved']}; "
                f"halted_windows={depth['halted_windows']}; "
                f"simulated_session_pnl={depth['session_pnl']}"
            ),
            "On real captured ladders, at least 100 opportunities produce at least 90% clean fills, under 2% partials, and zero risk-halted windows.",
            [depth_path],
        )
    )
    gates.append(
        _gate(
            "Quantity-cap interpretation",
            "verified-pass" if test_status.get("status") == "pass" else "verified-fail",
            "ORDER_SIZE is shares per leg; risk capital is committed from actual per-leg fill ratios.",
            "Capital caps and per-leg fill-ratio scaling pass regression tests.",
            [
                root / "tests" / "test_capital_accounting_bugs.py",
                str(test_status.get("source", state)),
            ],
        )
    )
    gates.append(
        _gate(
            "BTC / LightGBM economics",
            "verified-fail"
            if btc.get("status") == "not deployable"
            else "not-yet-tested",
            (
                f"status={btc.get('status')}; negative_policies={btc.get('negative_policy_count')}/"
                f"{btc.get('policy_count')}; best_raw_total_net_pnl_bps="
                f"{btc.get('best_policy', {}).get('mean_net_pnl_bps')}; "
                f"best_pnl_per_unit_exposure_bps="
                f"{(btc.get('best_exposure_policy') or {}).get('mean_pnl_per_unit_exposure_bps')}"
            ),
            "No model/sizing policy is promoted unless economics and calibrated holdout gates pass.",
            [btc_path, docs / "btc_offline_chronological_evaluation_2026-08-11.md"],
        )
    )
    gates.append(
        _gate(
            "Backtest input integrity",
            (
                "verified-pass"
                if backtest.get("comparable_to_outcome_books") is False
                and backtest_metrics.get("go_live_pnl_metric") is None
                else "verified-fail"
            ),
            (
                f"status={backtest.get('validation_status')}; comparable="
                f"{backtest.get('comparable_to_outcome_books')}; go_live_pnl="
                f"{backtest_metrics.get('go_live_pnl_metric')}"
            ),
            "Fallback candles cannot fabricate complementary books or produce go-live PnL.",
            [backtest_path],
        )
    )
    validation = setup.get("validation", {})
    gates.append(
        _gate(
            "Setup/config wizard",
            "verified-pass"
            if validation.get("status") == "verified-pass"
            else "verified-fail",
            (
                f"pass={validation.get('pass_count')}; fail={validation.get('fail_count')}; "
                f"total={validation.get('total_count')}; secrets_included={setup.get('secrets_included')}"
            ),
            "Current environment validates with zero failures and the report contains no secrets.",
            [setup_path, root / "scripts" / "setup_wizard.py"],
        )
    )
    gates.append(
        _gate(
            "Dashboard control panel",
            (
                "verified-pass"
                if dashboard.get("normal", {}).get("status") == "verified-pass"
                and dashboard_control_e2e.get("status") == "verified-pass"
                else "verified-fail"
            ),
            (
                f"exceptions={len(dashboard.get('normal', {}).get('exceptions', []))}; "
                f"control={dashboard.get('normal', {}).get('control_state_button_present')}; "
                f"wallet={dashboard.get('normal', {}).get('wallet_refresh_button_present')}; "
                f"http_health={dashboard.get('headless_http_server', {}).get('health_body')}; "
                f"audited_ui_transitions={dashboard_control_e2e.get('audited_transitions')}; "
                f"final_operator_enabled={dashboard_control_e2e.get('final_operator_enabled')}; "
                f"failure_artifacts_preserved="
                f"{sum(path.is_file() for path in (dashboard_failure_path, dashboard_form_failure_path))}"
            ),
            "The control, wallet, pre-flight, and audit surfaces render with zero exceptions.",
            [
                dashboard_path,
                dashboard_control_e2e_path,
                dashboard_failure_path,
                dashboard_form_failure_path,
                root / "streamlit_app.py",
            ],
        )
    )
    demo = dashboard.get("demo", {})
    gates.append(
        _gate(
            "Offline historical demo replay",
            "verified-pass"
            if demo.get("status") == "verified-pass"
            else "verified-fail",
            (
                f"network_used={demo.get('network_used')}; visible_events={demo.get('visible_events')}; "
                f"fires={demo.get('fires')}; trade_events={demo.get('trade_events')}"
            ),
            "A hash-sealed real paper session replays with no live connection and nonzero execution events.",
            [dashboard_path, root / "polymarket_arb" / "demo_replay.py"],
        )
    )
    stable_tests = (
        test_status.get("status") == "pass"
        and int(test_status.get("matching_runs", 0) or 0) >= 3
    )
    gates.append(
        _gate(
            "Full regression stability",
            "verified-pass" if stable_tests else "verified-fail",
            (
                f"tests={test_status.get('tests')}; passed={test_status.get('passed')}; "
                f"failures={test_status.get('failures')}; errors={test_status.get('errors')}; "
                f"matching_runs={test_status.get('matching_runs')}"
            ),
            "Three matching full-suite runs pass with zero failures and zero errors today.",
            [str(test_status.get("source", state))],
        )
    )
    gates.append(
        _gate(
            "Real live-order validation",
            "not-yet-tested",
            "endpoint unbuilt; real order and fill count=0",
            "Requires separate authorization only after jurisdiction, funding, and all execution gates pass.",
            [control_verify_path],
        )
    )

    mandatory_failures = [
        gate
        for gate in gates
        if gate["Mandatory"] and gate["Status"] != "verified-pass"
    ]
    counts = {
        status: sum(gate["Status"] == status for gate in gates)
        for status in sorted(VALID_STATUSES)
    }
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "verification_date_local": evidence_date,
        "timezone": timezone_name,
        "decision": "DO NOT ENABLE LIVE TRADING"
        if mandatory_failures
        else "ALL GATES PASS",
        "overall_status": "verified-fail" if mandatory_failures else "verified-pass",
        "paper_only": True,
        "real_orders_submitted": False,
        "gate_counts": counts,
        "mandatory_failure_count": len(mandatory_failures),
        "gates": gates,
        "rest_batch": batch,
        "cross_leg_skew": skew,
        "depth": {"artifact": str(depth_path), **depth},
        "test_status": test_status,
    }


def _relative_link(path: str, output: Path, root: Path) -> str:
    candidate = Path(path)
    try:
        candidate.resolve().relative_to(root.resolve())
        return os.path.relpath(candidate.resolve(), start=output.resolve().parent)
    except (OSError, ValueError):
        return path


def render_markdown(report: dict[str, Any], *, output: Path, root: Path) -> str:
    lines = [
        f"# Authoritative product and go-live status — {report['verification_date_local']}",
        "",
        f"**{report['overall_status'].upper()} — {report['decision']}.**",
        "",
        (
            f"Generated `{report['generated_at']}` from machine artifacts. All market sessions "
            "in scope were PAPER_TRADE; real orders submitted: `0`. The live exchange order "
            "endpoint remains intentionally unbuilt."
        ),
        "",
        "| # | Gate | Status | Exact current finding | Criterion | Evidence |",
        "| ---: | --- | --- | --- | --- | --- |",
    ]
    for index, gate in enumerate(report["gates"], 1):
        links = []
        for raw in gate["Evidence"]:
            target = _relative_link(raw, output, root)
            links.append(f"[{Path(raw).name or 'evidence'}]({target})")
        fields = [
            str(index),
            gate["Gate"],
            f"**{gate['Status']}**",
            gate["Current value"],
            gate["Criterion"],
            ", ".join(links),
        ]
        lines.append(
            "| " + " | ".join(str(field).replace("|", "\\|") for field in fields) + " |"
        )

    batch = report.get("rest_batch", {})
    if batch:
        aggregate = batch.get("aggregate", {})
        coherent = batch.get("coherence_phases", {}).get(
            "post_settle", {}
        ) or batch.get("coherence_phases", {}).get("post_coalescing", {})
        lines.extend(
            [
                "",
                "## REST paper batch closeout",
                "",
                (
                    f"Verdict: **`{batch.get('verdict')}`**. Qualifying sessions "
                    f"`{batch.get('qualifying_session_count')}/"
                    f"{batch.get('expected_session_count')}`; attempts "
                    f"`{batch.get('observed_session_count')}`; failed attempts "
                    f"`{batch.get('failed_attempt_count')}`. Historical raw opportunities "
                    f"`{aggregate.get('opportunities')}`; clean fills "
                    f"`{aggregate.get('fills')}`; partial fills `{aggregate.get('partial_fills')}`; "
                    f"expected committed PnL `{aggregate.get('expected_committed_pnl')}`; "
                    f"realized `{aggregate.get('realized_committed_pnl')}`; convergence "
                    f"`{aggregate.get('convergence_pct')}%`; deviation `{aggregate.get('deviation_pct')}%`. "
                    f"The current synchronized phase contains `{coherent.get('session_count')}` "
                    f"sessions, `{coherent.get('opportunities')}` opportunities, "
                    f"`{coherent.get('committed_executions')}` committed executions, and "
                    f"convergence `{coherent.get('convergence_pct')}%`. Classification: "
                    f"`{batch.get('convergence_resolution', {}).get('classification')}`."
                ),
                "",
                "| Session/attempt | Run status | Qualifies | Execution phase | Coherence phase | Opps | Clean | Partial | Expected | Realized | Convergence % | Qualification/failure |",
                "| --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
            ]
        )
        for session in batch.get("sessions", []):
            lines.append(
                "| "
                + " | ".join(
                    str(value)
                    for value in (
                        session.get("session_id"),
                        session.get("run_status"),
                        session.get("qualifies_as_completed_session"),
                        session.get("phase"),
                        session.get("coherence_phase"),
                        session.get("opportunities"),
                        session.get("fills"),
                        session.get("partial_fills"),
                        session.get("expected_committed_pnl"),
                        session.get("realized_committed_pnl"),
                        session.get("convergence_pct"),
                        session.get("failure_reason")
                        or ",".join(session.get("qualification_failures", []))
                        or "qualified",
                    )
                )
                + " |"
            )
        executions = batch.get("exact_executions", [])
        lines.extend(
            [
                "",
                "### Every committed execution",
                "",
                "| Session | Kind/action | Market | Detection combined | Entry / hedge / exit | Expected | Realized | Delta |",
                "| --- | --- | --- | ---: | --- | ---: | ---: | ---: |",
            ]
        )
        for execution in executions:
            prices = "/".join(
                str(execution.get(key))
                for key in ("filled_entry_price", "hedge_fill_price", "exit_price")
            )
            lines.append(
                "| "
                + " | ".join(
                    str(value)
                    for value in (
                        execution.get("session_id"),
                        f"{execution.get('kind')}/{execution.get('action')}",
                        execution.get("market_id"),
                        execution.get("decision_combined"),
                        prices,
                        execution.get("expected_pnl"),
                        execution.get("realized_pnl"),
                        execution.get("pnl_delta"),
                    )
                )
                + " |"
            )

    skew = report.get("cross_leg_skew", {})
    if skew:
        lines.extend(
            [
                "",
                "## Cross-leg false-signal root cause",
                "",
                (
                    f"Across `{skew.get('rows')}` real captured depth rows and "
                    f"`{skew.get('markets')}` markets, the old immediate-per-leg evaluator "
                    f"would have seen `{skew.get('apparent_edge_candidates')}` apparent edges. "
                    f"`{skew.get('transient_candidates_within_horizon')}` "
                    f"(`{skew.get('transient_candidate_pct')}%`) disappeared when the paired "
                    "leg arrived within 50 ms. Replaying the current 10 ms pair barrier left "
                    f"`{skew.get('pair_barrier_edge_candidates')}` candidates. The live feed "
                    "suppresses one-sided timeouts and applies complete paired updates before evaluation."
                ),
                "",
            ]
        )

    lines.extend(
        [
            "",
            "## Scope boundary",
            "",
            "No live order endpoint was built or called, no geoblock was bypassed, and no real funds moved.",
            "",
        ]
    )
    return "\n".join(lines)


def write_json(report: dict[str, Any], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(output)


def evidence_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
