"""Unit tests for dashboard_js.parsers using synthetic tmp_path fixtures only."""

import json
from datetime import datetime

import pytest

from dashboard_js import parsers


FROZEN_SHA = parsers.FROZEN_CANDLE_SHA256

NEW_STYLE_DOC = """# Authoritative product and go-live status — 2026-08-15

**VERIFIED-FAIL — DO NOT ENABLE LIVE TRADING.**

Generated `2026-08-16T02:37:00.891721+00:00` from machine artifacts.

| # | Gate | Status | Exact current finding | Criterion | Evidence |
| ---: | --- | --- | --- | --- | --- |

| 1 | Today's execution safety invariant | **verified-pass** | sealed PAPER_TRADE sessions=3/3; real orders=0 | Every market session today is sealed, paper-only. | [session_report.json](../state/a/session_report.json), [session_report.json](../state/b/session_report.json) |

| 2 | REST paper closeout batch | **verified-fail** | sessions=0/15 minimum; fills=0 | Run 15–20 additional sealed REST-book paper sessions. | [rest_paper_batch_2026-08-15.json](../state/rest_paper_batch_2026-08-15.json) |

| 3 | Live master flag | **verified-fail** | `operator_enabled`=False; `effective`=False | A live endpoint may only be considered after every gate passes. | [live_control.json](../state/live_control.json) |

| 4 | Jurisdiction / geoblock | **blocked-external** | status=blocked; country=CA; region=BC | Polymarket geoblock response is clear from the actual machine. | [wallet_status.json](../state/wallet_status.json) |

| 5 | PnL convergence | **not-yet-tested** | expected=None; n=0 | Realized PnL is within ±10% of expected. | [rest_paper_batch_2026-08-15.json](../state/rest_paper_batch_2026-08-15.json) |

| 6 | Unknown status row | **something-else** | mystery finding | mystery criterion | [x](y) |

| 7 | Extra trailing column | **verified-pass** | ok | ok | [primary](a) | [extra-link](b) |
"""

OLD_STYLE_DOC = """# Historical go-live checklist snapshot — superseded 2026-08-15

## Decision

**VERIFIED-FAIL — DO NOT ENABLE LIVE TRADING.**

| # | Gate | Current status | Exact current finding | Evidence | Last verified / tonight | Owner |
| ---: | --- | --- | --- | --- | --- | --- |

| 1 | Overall go-live authorization | **verified-fail** | Multiple mandatory gates below fail. | [Ground truth](ground_truth_2026-08-15.md), [final status](final_status_2026-08-15.md) | 2026-08-15, rechecked tonight | operator |

| 2 | Jurisdiction / geoblock | **blocked-external** | Polymarket reported the machine blocked in CA/BC. | [2026-08-13 reconciliation](go_live_reconciliation_2026-08-13.md#x) | 2026-08-13 | operator/legal | [extra](x) |

| 3 | Kill-switch behavior | **verified-pass** | Alerting enabled; halt set; orders `0 -> 0`. | [sealed report](../state/report.json) | 2026-08-15 | execution |
"""


def _write(tmp_path, rel, content):
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _policy(total_net, ppe, sharpe_trade, sharpe_ann, trades):
    return {
        "max_drawdown": 0.5,
        "per_holdout_entries": [1, 1, 1],
        "pnl_per_unit_exposure_bps": ppe,
        "sharpe_annualized": sharpe_ann,
        "sharpe_per_trade": sharpe_trade,
        "total_net_pnl_bps": total_net,
        "total_trades": trades,
    }


def _pooled_models():
    return {
        "logistic_regression": {
            "binary_f1": _policy(-14395.16, -19.2706, -0.5951, -111.3956, 747),
            "top_decile": _policy(-3537.89, -16.6882, -0.4048, -75.7831, 212),
            "confidence_weighted": _policy(-546.38, -14.2184, -0.1401, -26.2227, 747),
            "probability_proportional": _policy(-5488.52, -19.1701, -0.3865, -72.3525, 4175),
        },
        "gradient_boosted_trees": {
            "binary_f1": _policy(-12500.63, -18.6855, -0.6054, -113.3260, 669),
            "top_decile": _policy(-13004.28, -18.6308, -0.6178, -115.6493, 698),
            "confidence_weighted": _policy(-2572.99, -18.7774, -0.3544, -66.3407, 669),
            "probability_proportional": _policy(-7852.70, -19.3188, -0.3072, -57.4984, 4175),
        },
    }


def _holdouts():
    pooled = _pooled_models()
    out = {}
    for hname in ("holdout_a", "holdout_b", "holdout_c"):
        models = {}
        for mname, policies in pooled.items():
            sp = {}
            for pname, cell in policies.items():
                sp[pname] = {
                    "actual_entries": cell["total_trades"],
                    "net_pnl_bps": cell["total_net_pnl_bps"] / 3,
                    "pnl_per_unit_exposure_bps": cell["pnl_per_unit_exposure_bps"],
                    "sharpe_annualized": cell["sharpe_annualized"],
                    "sharpe_per_trade": cell["sharpe_per_trade"],
                }
            models[mname] = {"sizing_policies": sp}
        out[hname] = {"models": models}
    return out


def _btc_sizing_doc(sha=FROZEN_SHA):
    return {
        "input": {"sha256": {"candles": sha, "macro": "othersha"}},
        "sequence_model": {"status": "skipped", "reason": "no baseline beat majority"},
        "pnl": {
            "status": "available_btc_proxy_only",
            "executable_polymarket_pnl": {"status": "unavailable", "reason": "no proxy quotes"},
            "pooled": {"models": _pooled_models()},
        },
        "holdouts": _holdouts(),
    }


def _build_all_artifacts(tmp_path, sha=FROZEN_SHA, corrupt_v2=False):
    _write(
        tmp_path,
        "state/lightgbm_training_metrics.json",
        json.dumps(
            {
                "model": "lightgbm",
                "accuracy": 0.6612576064908722,
                "f1_macro": 0.3375031571176002,
                "training_samples": 4446,
                "test_samples": 493,
                "timestamp": "2026-08-04T17:08:42.789405+00:00",
            }
        ),
    )
    v2 = json.dumps(
        {
            "status": "completed",
            "generated_at_utc": "2026-08-04T19:14:18.554485+00:00",
            "horizon_minutes": 15,
            "best_iteration": 6,
            "stability_vs_kronos": {
                "mean_holdout_accuracy_pct": 62.259265,
                "holdout_accuracy_range_pct_points": 32.305194,
                "holdout_accuracy_stddev_pct_points": 14.121197,
                "all_holdouts_beat_kronos": True,
                "reference": "fixed 43.75% Kronos accuracy",
            },
        }
    )
    _write(tmp_path, "state/lightgbm_training_metrics_v2.json", "not json{{{" if corrupt_v2 else v2)
    _write(
        tmp_path,
        "state/lightgbm_training_metrics_v3.json",
        json.dumps(
            {
                "status": "completed",
                "generated_at_utc": "2026-08-06T07:31:21.199624+00:00",
                "data_source": {"candle_rows": 139362},
                "holdout_summary": {
                    "A_balanced": {
                        "holdout_mean_pct": 62.539904,
                        "holdout_stddev_pp": 1.642257,
                        "all_beat_majority": True,
                        "precision_at_0_7": {
                            "holdout_a": {"precision_pct": 94.084507},
                            "holdout_b": {"precision_pct": 91.452991},
                            "holdout_c": {"precision_pct": 93.522907},
                        },
                    }
                },
            }
        ),
    )
    _write(
        tmp_path,
        "state/kronos_training_metrics.json",
        json.dumps(
            {
                "timestamp": "2026-08-04T09:11:18.743584+00:00",
                "data_stats": {
                    "total_sequences": 808,
                    "train_sequences": 607,
                    "val_sequences": 80,
                    "test_sequences": 121,
                },
                "training_results": {
                    "best_epoch": 4,
                    "best_val_accuracy": 0.7375,
                    "final_test_metrics": {
                        "accuracy": 0.2727272727272727,
                        "f1_macro": 0.2217340254603881,
                        "n_samples": 121,
                    },
                },
            }
        ),
    )
    _write(
        tmp_path,
        "docs/research/btc_offline_sizing_sharpe_pooled_2026-08-15.json",
        json.dumps(_btc_sizing_doc(sha)),
    )


# --------------------------------------------------------------------------
# parse_checklist
# --------------------------------------------------------------------------


def test_parse_checklist_new_style(tmp_path):
    doc = _write(tmp_path, "product.md", NEW_STYLE_DOC)
    result = parsers.parse_checklist(doc)
    assert result["doc"] == "product.md"
    assert result["generated_utc"] == "2026-08-16T02:37:00.891721+00:00"
    assert result["verdict_line"] == "Authoritative product and go-live status — 2026-08-15"

    gates = result["gates"]
    assert [g["n"] for g in gates] == [1, 2, 3, 4, 5, 6, 7]
    assert [g["status"] for g in gates] == [
        "verified-pass",
        "verified-fail",
        "verified-fail",
        "blocked-external",
        "not-yet-tested",
        "unknown",
        "verified-pass",
    ]
    assert gates[0]["finding"] == "sealed PAPER_TRADE sessions=3/3; real orders=0"
    assert gates[0]["criterion"] == "Every market session today is sealed, paper-only."
    assert gates[0]["evidence"] == "session_report.json, session_report.json"
    assert gates[1]["evidence"] == "rest_paper_batch_2026-08-15.json"
    assert gates[2]["finding"] == "operator_enabled=False; effective=False"
    assert gates[6]["evidence"] == "primary"
    assert result["summary"] == {
        "verified_pass": 2,
        "verified_fail": 2,
        "blocked_external": 1,
        "not_yet_tested": 1,
        "unknown": 1,
    }


def test_parse_checklist_fallback_old_style(tmp_path, monkeypatch):
    monkeypatch.setattr(parsers, "PROJECT_ROOT", tmp_path)
    _write(tmp_path, "docs/go_live_checklist_2026-08-11.md", OLD_STYLE_DOC)
    result = parsers.parse_checklist(tmp_path / "missing.md")
    assert result["doc"] == "go_live_checklist_2026-08-11.md"
    assert result["generated_utc"] is None
    assert result["verdict_line"] == "Historical go-live checklist snapshot — superseded 2026-08-15"

    gates = result["gates"]
    assert [g["n"] for g in gates] == [1, 2, 3]
    assert gates[0]["status"] == "verified-fail"
    assert gates[0]["criterion"] is None
    assert gates[0]["evidence"] == "Ground truth, final status"
    assert gates[1]["status"] == "blocked-external"
    assert gates[1]["evidence"] == "2026-08-13 reconciliation"
    assert gates[2]["status"] == "verified-pass"
    assert gates[2]["finding"] == "Alerting enabled; halt set; orders 0 -> 0."
    assert result["summary"]["verified_pass"] == 1


def test_parse_checklist_missing_everywhere_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(parsers, "PROJECT_ROOT", tmp_path)
    with pytest.raises(FileNotFoundError):
        parsers.parse_checklist(tmp_path / "nope.md")


# --------------------------------------------------------------------------
# parse_model_status
# --------------------------------------------------------------------------


def test_parse_model_status_all_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(parsers, "PROJECT_ROOT", tmp_path)
    result = parsers.parse_model_status()
    datetime.fromisoformat(result["as_of_utc"])
    models = result["models"]
    assert [m["id"] for m in models] == [
        "lightgbm_v1",
        "lightgbm_v2",
        "lightgbm_v3",
        "kronos",
        "btc_sizing",
    ]
    for model in models:
        assert model["verdict"] == "artifact missing"
        assert model["metrics"] == {}
        assert model["source"]


def test_parse_model_status_full(tmp_path, monkeypatch):
    monkeypatch.setattr(parsers, "PROJECT_ROOT", tmp_path)
    _build_all_artifacts(tmp_path)
    result = parsers.parse_model_status()
    models = {m["id"]: m for m in result["models"]}

    v1 = models["lightgbm_v1"]
    assert "0.6613" in v1["verdict"] and "0.3375" in v1["verdict"]
    assert v1["metrics"]["training_samples"] == 4446
    assert v1["metrics"]["test_samples"] == 493

    v2 = models["lightgbm_v2"]
    assert "62.26" in v2["verdict"] and "43.75" in v2["verdict"]
    assert v2["metrics"]["mean_holdout_accuracy_pct"] == 62.259265
    assert v2["metrics"]["all_holdouts_beat_kronos"] is True

    v3 = models["lightgbm_v3"]
    assert "139,362" in v3["verdict"]
    assert v3["metrics"]["candle_rows"] == 139362
    assert v3["metrics"]["A_balanced_all_beat_majority"] is True
    assert v3["metrics"]["A_balanced_precision_at_0_7_pct"] == [
        94.084507,
        91.452991,
        93.522907,
    ]

    kronos = models["kronos"]
    assert "0.7375" in kronos["verdict"] and "0.2727" in kronos["verdict"]
    assert kronos["metrics"]["total_sequences"] == 808
    assert kronos["metrics"]["final_test_n_samples"] == 121

    btc = models["btc_sizing"]
    assert btc["verdict"].startswith("not deployable — all policies negative in all holdouts")
    assert "logistic_regression confidence_weighted" in btc["verdict"]
    assert "(-14.22 bps/unit)" in btc["verdict"]
    assert btc["metrics"]["input_sha256_matches_frozen"] is True
    assert btc["metrics"]["sequence_model_status"] == "skipped"
    assert btc["metrics"]["executable_polymarket_pnl_status"] == "unavailable"
    assert btc["metrics"]["pnl_status"] == "available_btc_proxy_only"
    lr_cw = btc["metrics"]["logistic_regression"]["confidence_weighted"]
    assert lr_cw["total_net_pnl_bps"] == pytest.approx(-546.38)
    assert lr_cw["pnl_per_unit_exposure_bps"] == pytest.approx(-14.2184)
    assert lr_cw["sharpe_annualized"] == pytest.approx(-26.2227)
    assert lr_cw["sharpe_per_trade"] == pytest.approx(-0.1401)
    assert lr_cw["total_trades"] == 747
    gbt_pp = btc["metrics"]["gradient_boosted_trees"]["probability_proportional"]
    assert gbt_pp["total_trades"] == 4175
    assert gbt_pp["total_net_pnl_bps"] == pytest.approx(-7852.70)


def test_btc_sizing_sha_mismatch(tmp_path, monkeypatch):
    monkeypatch.setattr(parsers, "PROJECT_ROOT", tmp_path)
    _build_all_artifacts(tmp_path, sha="deadbeef")
    result = parsers.parse_model_status()
    btc = next(m for m in result["models"] if m["id"] == "btc_sizing")
    assert btc["metrics"]["input_sha256_matches_frozen"] is False


def test_parse_model_status_corrupt_artifact_isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(parsers, "PROJECT_ROOT", tmp_path)
    _build_all_artifacts(tmp_path, corrupt_v2=True)
    result = parsers.parse_model_status()
    models = {m["id"]: m for m in result["models"]}
    v2 = models["lightgbm_v2"]
    assert v2["verdict"] == "artifact unreadable"
    assert v2["metrics"] == {}
    assert "error" in v2
    assert models["lightgbm_v1"]["metrics"]
    assert models["lightgbm_v3"]["metrics"]
    assert models["kronos"]["metrics"]
    assert models["btc_sizing"]["metrics"]
