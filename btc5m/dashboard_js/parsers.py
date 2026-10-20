"""Read-only parsers for the dashboard_js server.

Parses the authoritative go-live checklist markdown (with fallback to the
2026-08-11 snapshot) and the real machine training/evaluation artifacts into
JSON-safe dicts. Stdlib only; every artifact read is isolated in try/except so
one bad file cannot break an endpoint.
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

FALLBACK_CHECKLIST_REL = Path("docs") / "go_live_checklist_2026-08-11.md"
AUTHORITATIVE_CHECKLIST_REL = Path("docs") / "product_status_2026-08-15.md"

FROZEN_CANDLE_SHA256 = (
    "e40835922f491e0d7c97b05f6f5967a92d903aa856e9044dae03a2442d6b7ec1"
)

_STATUS_VALUES = ("verified-pass", "verified-fail", "blocked-external", "not-yet-tested")

_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_BACKTICK_RE = re.compile(r"`([^`]*)`")
_HEADING_RE = re.compile(r"^#{1,6}\s+(.*)$")
_GENERATED_RE = re.compile(r"Generated\s+`?(\d{4}-\d{2}-\d{2}T[^\s`]+)`?")
_SEPARATOR_RE = re.compile(r"^:?-{3,}:?$")


def _strip_markdown(text):
    if not text:
        return ""
    text = _LINK_RE.sub(r"\1", text)
    text = _BACKTICK_RE.sub(r"\1", text)
    text = text.replace("**", "")
    return text.strip()


def _table_rows(lines):
    rows = []
    for line in lines:
        if not line.strip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if all(_SEPARATOR_RE.fullmatch(c) for c in cells):
            continue
        rows.append(cells)
    return rows


def _column_map(header):
    cols = {}
    for idx, cell in enumerate(header):
        lower = cell.lower()
        if idx == 0:
            cols["n"] = idx
        elif idx == 1:
            cols["name"] = idx
        elif "finding" in lower:
            cols.setdefault("finding", idx)
        elif "criterion" in lower:
            cols.setdefault("criterion", idx)
        elif "evidence" in lower:
            cols.setdefault("evidence", idx)
        elif "status" in lower:
            cols.setdefault("status", idx)
    return cols


def _cell(row, idx):
    if idx is None or idx >= len(row):
        return None
    return row[idx]


def _extract_status(raw):
    cleaned = _strip_markdown(raw).lower()
    for status in _STATUS_VALUES:
        if status in cleaned:
            return status
    return "unknown"


def _summary(gates):
    summary = {s.replace("-", "_"): 0 for s in _STATUS_VALUES}
    summary["unknown"] = 0
    for gate in gates:
        summary[gate["status"].replace("-", "_")] += 1
    return summary


def parse_checklist(doc_path) -> dict:
    doc = Path(doc_path)
    if not doc.is_file():
        authoritative = PROJECT_ROOT / AUTHORITATIVE_CHECKLIST_REL
        if authoritative.is_file():
            doc = authoritative
        else:
            fallback = PROJECT_ROOT / FALLBACK_CHECKLIST_REL
            if fallback.is_file():
                doc = fallback
    if not doc.is_file():
        raise FileNotFoundError(
            f"checklist not found at {doc_path} (fallback {PROJECT_ROOT / FALLBACK_CHECKLIST_REL} missing too)"
        )
    lines = doc.read_text(encoding="utf-8").splitlines()

    generated_utc = None
    for line in lines:
        match = _GENERATED_RE.search(line)
        if match:
            generated_utc = match.group(1)
            break

    verdict_line = None
    for line in lines:
        match = _HEADING_RE.match(line.strip())
        if match:
            verdict_line = match.group(1).strip()
            break

    rows = _table_rows(lines)
    gates = []
    if rows:
        cols = _column_map(rows[0])
        for row in rows[1:]:
            if not row:
                continue
            n_raw = _cell(row, cols.get("n"))
            if n_raw and n_raw.isdigit():
                n = int(n_raw)
            else:
                n = len(gates) + 1
            name = _strip_markdown(_cell(row, cols.get("name")))
            finding = _strip_markdown(_cell(row, cols.get("finding")))
            if not name and not finding:
                continue
            gates.append(
                {
                    "n": n,
                    "name": name,
                    "status": _extract_status(_cell(row, cols.get("status"))),
                    "finding": finding,
                    "criterion": _strip_markdown(_cell(row, cols.get("criterion"))) or None,
                    "evidence": _strip_markdown(_cell(row, cols.get("evidence"))) or None,
                }
            )

    return {
        "doc": doc.name,
        "generated_utc": generated_utc,
        "verdict_line": verdict_line,
        "gates": gates,
        "summary": _summary(gates),
    }


def _model_entry(entry_id, name, rel_path, parser):
    source = str(rel_path)
    path = PROJECT_ROOT / rel_path
    if not path.is_file():
        return {
            "id": entry_id,
            "name": name,
            "verdict": "artifact missing",
            "metrics": {},
            "source": source,
        }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {
            "id": entry_id,
            "name": name,
            "verdict": "artifact unreadable",
            "metrics": {},
            "source": source,
            "error": str(exc),
        }
    try:
        verdict, metrics = parser(data)
    except Exception as exc:
        return {
            "id": entry_id,
            "name": name,
            "verdict": "parse error",
            "metrics": {},
            "source": source,
            "error": str(exc),
        }
    return {
        "id": entry_id,
        "name": name,
        "verdict": verdict,
        "metrics": metrics,
        "source": source,
    }


def _parse_lightgbm_v1(d):
    metrics = {
        k: d.get(k)
        for k in ("accuracy", "f1_macro", "training_samples", "test_samples", "timestamp")
    }
    metrics = {k: v for k, v in metrics.items() if v is not None}
    acc, f1 = d.get("accuracy"), d.get("f1_macro")
    tr, te = d.get("training_samples"), d.get("test_samples")
    if None not in (acc, f1, tr, te):
        verdict = (
            f"v1 baseline LightGBM classifier: test accuracy {acc:.4f}, "
            f"f1_macro {f1:.4f} on {te} test rows ({tr} train rows)"
        )
    else:
        verdict = "v1 baseline LightGBM classifier artifact present"
    return verdict, metrics


def _parse_lightgbm_v2(d):
    stab = d.get("stability_vs_kronos") or {}
    metrics = {
        "status": d.get("status"),
        "generated_at_utc": d.get("generated_at_utc"),
        "horizon_minutes": d.get("horizon_minutes"),
        "best_iteration": d.get("best_iteration"),
        "mean_holdout_accuracy_pct": stab.get("mean_holdout_accuracy_pct"),
        "holdout_accuracy_range_pct_points": stab.get("holdout_accuracy_range_pct_points"),
        "holdout_accuracy_stddev_pct_points": stab.get("holdout_accuracy_stddev_pct_points"),
        "all_holdouts_beat_kronos": stab.get("all_holdouts_beat_kronos"),
    }
    metrics = {k: v for k, v in metrics.items() if v is not None}
    mean = stab.get("mean_holdout_accuracy_pct")
    if mean is not None:
        verdict = f"v2 fixed diagnostic completed: mean 3-holdout accuracy {mean:.2f}% vs Kronos 43.75% reference"
        if stab.get("all_holdouts_beat_kronos") is True:
            verdict += " (all holdouts beat Kronos)"
        rng = stab.get("holdout_accuracy_range_pct_points")
        if rng is not None:
            verdict += f", range {rng:.1f} pp"
    else:
        verdict = f"v2 fixed diagnostic artifact present (status={d.get('status')})"
    return verdict, metrics


def _parse_lightgbm_v3(d):
    hs = d.get("holdout_summary") or {}
    ab = hs.get("A_balanced") or {}
    rows = (d.get("data_source") or {}).get("candle_rows")
    precisions = []
    for hname in ("holdout_a", "holdout_b", "holdout_c"):
        p = (ab.get("precision_at_0_7") or {}).get(hname) or {}
        if p.get("precision_pct") is not None:
            precisions.append(p["precision_pct"])
    metrics = {
        "status": d.get("status"),
        "generated_at_utc": d.get("generated_at_utc"),
        "candle_rows": rows,
        "A_balanced_holdout_mean_pct": ab.get("holdout_mean_pct"),
        "A_balanced_holdout_stddev_pp": ab.get("holdout_stddev_pp"),
        "A_balanced_all_beat_majority": ab.get("all_beat_majority"),
        "A_balanced_precision_at_0_7_pct": precisions,
    }
    metrics = {k: v for k, v in metrics.items() if v is not None}
    mean = ab.get("holdout_mean_pct")
    if mean is not None:
        if rows is not None:
            verdict = (
                f"v3 honest final verdict completed: A_balanced mean 3-holdout accuracy "
                f"{mean:.2f}% on {rows:,} candle rows"
            )
        else:
            verdict = f"v3 honest final verdict completed: A_balanced mean 3-holdout accuracy {mean:.2f}%"
        if precisions:
            verdict += f", precision@0.7 {min(precisions):.1f}–{max(precisions):.1f}%"
    else:
        verdict = f"v3 artifact present (status={d.get('status')})"
    return verdict, metrics


def _parse_kronos(d):
    ds = d.get("data_stats") or {}
    tr = d.get("training_results") or {}
    ft = tr.get("final_test_metrics") or {}
    metrics = {
        "timestamp": d.get("timestamp"),
        "total_sequences": ds.get("total_sequences"),
        "train_sequences": ds.get("train_sequences"),
        "val_sequences": ds.get("val_sequences"),
        "test_sequences": ds.get("test_sequences"),
        "best_epoch": tr.get("best_epoch"),
        "best_val_accuracy": tr.get("best_val_accuracy"),
        "final_test_accuracy": ft.get("accuracy"),
        "final_test_f1_macro": ft.get("f1_macro"),
        "final_test_n_samples": ft.get("n_samples"),
    }
    metrics = {k: v for k, v in metrics.items() if v is not None}
    bva = tr.get("best_val_accuracy")
    fta = ft.get("accuracy")
    if bva is not None and fta is not None:
        verdict = f"Kronos LSTM: best val accuracy {bva:.4f} vs final test accuracy {fta:.4f}"
        if ft.get("n_samples") is not None:
            verdict += f" ({ft['n_samples']} test sequences)"
        verdict += " — poor generalization"
    else:
        verdict = "Kronos LSTM artifact present"
    return verdict, metrics


_SIZING_MODELS = ("logistic_regression", "gradient_boosted_trees")
_SIZING_POLICIES = ("binary_f1", "top_decile", "confidence_weighted", "probability_proportional")
_SIZING_FIELDS = (
    "total_net_pnl_bps",
    "pnl_per_unit_exposure_bps",
    "sharpe_per_trade",
    "sharpe_annualized",
    "total_trades",
)


def _parse_btc_sizing(d):
    metrics = {}
    pooled = (d.get("pnl") or {}).get("pooled") or {}
    pooled_models = pooled.get("models") or {}
    best_ppe = None
    pooled_checked = 0
    pooled_all_negative = True
    for model in _SIZING_MODELS:
        model_data = {}
        for policy in _SIZING_POLICIES:
            cell = (pooled_models.get(model) or {}).get(policy) or {}
            row = {k: cell.get(k) for k in _SIZING_FIELDS}
            row = {k: v for k, v in row.items() if v is not None}
            if row:
                model_data[policy] = row
            ppe = cell.get("pnl_per_unit_exposure_bps")
            if ppe is not None and (best_ppe is None or ppe > best_ppe[0]):
                best_ppe = (ppe, model, policy)
            tpnl = cell.get("total_net_pnl_bps")
            if tpnl is not None:
                pooled_checked += 1
                if tpnl >= 0:
                    pooled_all_negative = False
        if model_data:
            metrics[model] = model_data

    holdout_checked = 0
    holdout_negative = 0
    for hdata in (d.get("holdouts") or {}).values():
        for mdata in (hdata.get("models") or {}).values():
            for pv in (mdata.get("sizing_policies") or {}).values():
                if not isinstance(pv, dict):
                    continue
                value = pv.get("net_pnl_bps")
                if value is None:
                    value = pv.get("total_net_pnl_bps")
                if value is None:
                    continue
                holdout_checked += 1
                if value < 0:
                    holdout_negative += 1

    if holdout_checked > 0:
        all_negative = holdout_negative == holdout_checked and pooled_all_negative
    else:
        all_negative = pooled_all_negative and pooled_checked > 0

    if all_negative:
        verdict = "not deployable — all policies negative in all holdouts"
    elif holdout_checked > 0:
        verdict = (
            f"not deployable — {holdout_negative}/{holdout_checked} "
            "holdout-policy cells negative"
        )
    else:
        verdict = "not deployable — negative pooled policy PnL"
    if best_ppe is not None:
        verdict += (
            f"; best (least negative) per-unit exposure: {best_ppe[1]} {best_ppe[2]} "
            f"({best_ppe[0]:.2f} bps/unit)"
        )

    metrics["sequence_model_status"] = (d.get("sequence_model") or {}).get("status")
    metrics["pnl_status"] = (d.get("pnl") or {}).get("status")
    metrics["executable_polymarket_pnl_status"] = (
        ((d.get("pnl") or {}).get("executable_polymarket_pnl") or {}).get("status")
    )
    sha = (d.get("input") or {}).get("sha256") or {}
    metrics["input_sha256_matches_frozen"] = sha.get("candles") == FROZEN_CANDLE_SHA256
    metrics = {k: v for k, v in metrics.items() if v is not None}
    return verdict, metrics


def parse_model_status() -> dict:
    entries = [
        _model_entry(
            "lightgbm_v1",
            "LightGBM v1 (baseline)",
            "state/lightgbm_training_metrics.json",
            _parse_lightgbm_v1,
        ),
        _model_entry(
            "lightgbm_v2",
            "LightGBM v2 (fixed diagnostic)",
            "state/lightgbm_training_metrics_v2.json",
            _parse_lightgbm_v2,
        ),
        _model_entry(
            "lightgbm_v3",
            "LightGBM v3 (honest final verdict)",
            "state/lightgbm_training_metrics_v3.json",
            _parse_lightgbm_v3,
        ),
        _model_entry(
            "kronos",
            "Kronos LSTM (cold path)",
            "state/kronos_training_metrics.json",
            _parse_kronos,
        ),
        _model_entry(
            "btc_sizing",
            "BTC offline sizing (binary vs confidence-weighted, pooled Sharpe)",
            "docs/research/btc_offline_sizing_sharpe_pooled_2026-08-15.json",
            _parse_btc_sizing,
        ),
    ]
    return {
        "as_of_utc": datetime.now(timezone.utc).isoformat(),
        "models": entries,
    }
