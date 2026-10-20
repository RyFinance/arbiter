from __future__ import annotations

import json

from polymarket_arb.product_status import (
    VALID_STATUSES,
    build_product_status,
    render_markdown,
)


def test_empty_workspace_fails_closed_with_valid_status_vocabulary(tmp_path):
    (tmp_path / "state" / "paper_session_archives").mkdir(parents=True)
    (tmp_path / "docs" / "research").mkdir(parents=True)
    report = build_product_status(tmp_path, evidence_date="2026-08-15")
    assert report["overall_status"] == "verified-fail"
    assert report["decision"] == "DO NOT ENABLE LIVE TRADING"
    assert report["real_orders_submitted"] is False
    assert report["gates"]
    assert {gate["Status"] for gate in report["gates"]} <= VALID_STATUSES
    live_gate = next(
        gate for gate in report["gates"] if gate["Gate"] == "Live master flag / exchange endpoint"
    )
    assert live_gate["Status"] == "verified-fail"


def test_markdown_uses_same_gate_rows_and_exact_batch_values(tmp_path):
    report = {
        "verification_date_local": "2026-08-15",
        "overall_status": "verified-fail",
        "decision": "DO NOT ENABLE LIVE TRADING",
        "generated_at": "2026-08-16T00:00:00+00:00",
        "gates": [
            {
                "Gate": "PnL convergence",
                "Status": "verified-fail",
                "Current value": "expected=0.6; realized=-1.0",
                "Criterion": "within ±10%",
                "Evidence": [str(tmp_path / "state" / "batch.json")],
                "Mandatory": True,
            }
        ],
        "rest_batch": {
            "verdict": "negative_convergence_failure_persists",
            "observed_session_count": 15,
            "expected_session_count": 15,
            "aggregate": {
                "opportunities": 9,
                "fills": 0,
                "partial_fills": 1,
                "expected_committed_pnl": 0.6,
                "realized_committed_pnl": -1.0,
                "convergence_pct": -166.66666666666669,
                "deviation_pct": -266.6666666666667,
            },
            "sessions": [],
            "exact_executions": [],
        },
    }
    text = render_markdown(
        report,
        output=tmp_path / "docs" / "status.md",
        root=tmp_path,
    )
    assert "negative_convergence_failure_persists" in text
    assert "-166.66666666666669%" in text
    assert "expected=0.6; realized=-1.0" in text
    assert "real orders submitted: `0`" in text
    assert "](../state/batch.json)" in text


def test_live_status_prefers_newer_interim_until_final_batch_is_complete(tmp_path):
    state = tmp_path / "state"
    (state / "paper_session_archives").mkdir(parents=True)
    (tmp_path / "docs" / "research").mkdir(parents=True)
    final_path = state / "rest_paper_batch_2026-08-15.json"
    interim_path = state / "rest_paper_batch_interim_2026-08-15.json"
    final_path.write_text(
        json.dumps({"observed_session_count": 3, "batch_complete": False}),
        encoding="utf-8",
    )
    interim_path.write_text(
        json.dumps({"observed_session_count": 7, "batch_complete": False}),
        encoding="utf-8",
    )

    report = build_product_status(tmp_path, evidence_date="2026-08-15")

    assert report["rest_batch"]["observed_session_count"] == 7
    batch_gate = next(
        gate for gate in report["gates"] if gate["Gate"] == "REST paper closeout batch"
    )
    assert str(interim_path) in batch_gate["Evidence"]

    final_path.write_text(
        json.dumps({"observed_session_count": 15, "batch_complete": True}),
        encoding="utf-8",
    )
    report = build_product_status(tmp_path, evidence_date="2026-08-15")
    assert report["rest_batch"]["observed_session_count"] == 15
    batch_gate = next(
        gate for gate in report["gates"] if gate["Gate"] == "REST paper closeout batch"
    )
    assert str(final_path) in batch_gate["Evidence"]


def test_wallet_gate_requires_approvals_not_only_balances(tmp_path):
    state = tmp_path / "state"
    (state / "paper_session_archives").mkdir(parents=True)
    (tmp_path / "docs" / "research").mkdir(parents=True)
    (state / "wallet_status.json").write_text(
        json.dumps(
            {
                "read_only": True,
                "transaction_methods_available": False,
                "rpc_methods_allowed": ["eth_call", "eth_getBalance"],
                "funding_ready": True,
                "approvals_ready": False,
                "trading_ready": False,
                "balances": {
                    "polygon_native": 1.0,
                    "pusd": 50.0,
                    "required_pusd": 20.0,
                },
            }
        ),
        encoding="utf-8",
    )

    report = build_product_status(tmp_path, evidence_date="2026-08-15")
    gate = next(
        row for row in report["gates"] if row["Gate"] == "Wallet funding / approvals"
    )
    assert gate["Status"] == "verified-fail"
    assert "funding_ready=True" in gate["Current value"]
    assert "approvals_ready=False" in gate["Current value"]
