"""Feature extraction and training gate for cross-venue match confidence.

This reads real observer logs from state/cross_venue_log.jsonl.  It does not
fabricate labels or synthetic samples.  Training is deliberately refused until
resolved, labeled cross-venue records exist.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from typing import Any, Optional


DEFAULT_LOG_PATH = Path("state/cross_venue_log.jsonl")
DEFAULT_MODEL_PATH = Path("models/cross_venue_confidence.pkl")
MIN_LABELED_WINDOWS = 30
PREFERRED_LABELED_WINDOWS = 100


@dataclass(frozen=True)
class FeatureRow:
    observed_at: str
    kalshi_ticker: str
    status: str
    label: Optional[float]
    features: dict[str, float]


def load_cross_venue_records(path: Path = DEFAULT_LOG_PATH) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            records.append(json.loads(line))
    return records


def _parse_ts(value: str) -> Optional[float]:
    from datetime import datetime

    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _window_overlap_pct(record: dict[str, Any]) -> float:
    kalshi = record.get("kalshi") or {}
    k_start = _parse_ts(str(kalshi.get("open_time") or ""))
    k_end = _parse_ts(str(kalshi.get("close_time") or ""))
    windows = record.get("polymarket_windows") or []
    if k_start is None or k_end is None or not windows:
        return 0.0
    covered = 0.0
    for window in windows:
        p_start = float(window.get("start_ts") or 0)
        p_end = float(window.get("end_ts") or 0)
        overlap = max(0.0, min(k_end, p_end) - max(k_start, p_start))
        covered += overlap
    kalshi_duration = max(1.0, k_end - k_start)
    return min(1.0, covered / kalshi_duration)


def _spread(ask: Any, bid: Any) -> Optional[float]:
    try:
        if ask is None or bid is None:
            return None
        return max(0.0, float(ask) - float(bid))
    except (TypeError, ValueError):
        return None


def extract_feature_row(record: dict[str, Any]) -> FeatureRow:
    kalshi = record.get("kalshi") or {}
    windows = record.get("polymarket_windows") or []
    priced = [
        w for w in windows
        if w.get("up_ask") is not None and w.get("down_ask") is not None
    ]
    poly_spreads = []
    for window in priced:
        up_spread = _spread(window.get("up_ask"), window.get("up_bid"))
        down_spread = _spread(window.get("down_ask"), window.get("down_bid"))
        if up_spread is not None:
            poly_spreads.append(up_spread)
        if down_spread is not None:
            poly_spreads.append(down_spread)

    kalshi_spreads = [
        s for s in (
            _spread(kalshi.get("yes_ask"), kalshi.get("yes_bid")),
            _spread(kalshi.get("no_ask"), kalshi.get("no_bid")),
        )
        if s is not None
    ]

    settlement_sources = " ".join(
        str(src.get("name") or "") for src in kalshi.get("settlement_sources") or []
        if isinstance(src, dict)
    ).lower()
    poly_sources = " ".join(
        str(w.get("resolution_source") or "") for w in windows
    ).lower()
    settlement_source_match = 1.0 if (
        "cf benchmarks" in settlement_sources
        and "chain.link" not in poly_sources
        and "chainlink" not in poly_sources
    ) else 0.0

    label = record.get("match_label")
    if label is not None:
        label = float(label)

    features = {
        "window_overlap_pct": _window_overlap_pct(record),
        "poly_windows_count": float(len(windows)),
        "priced_poly_windows_count": float(len(priced)),
        "full_mapping": 1.0 if len(windows) == 3 and len(priced) == 3 else 0.0,
        "settlement_source_match": settlement_source_match,
        "oracle_mismatch_risk": 1.0 if record.get("oracle_mismatch_risk") else 0.0,
        "best_gap": float(record.get("best_gap") or 0.0),
        "abs_poly_up_minus_kalshi_yes": abs(
            float(record.get("poly_up_proxy") or 0.0) - float(record.get("kalshi_yes_ask") or 0.0)
        ),
        "abs_poly_down_minus_kalshi_no": abs(
            float(record.get("poly_down_proxy") or 0.0) - float(record.get("kalshi_no_ask") or 0.0)
        ),
        "kalshi_mean_spread": mean(kalshi_spreads) if kalshi_spreads else 1.0,
        "poly_mean_spread": mean(poly_spreads) if poly_spreads else 1.0,
        "both_open": 1.0 if kalshi.get("status") in {"active", "open"} and all(
            bool(w.get("accepting_orders")) and bool(w.get("active")) and not bool(w.get("closed"))
            for w in windows
        ) else 0.0,
    }
    return FeatureRow(
        observed_at=str(record.get("observed_at") or ""),
        kalshi_ticker=str(kalshi.get("ticker") or ""),
        status=str(record.get("status") or ""),
        label=label,
        features=features,
    )


def extract_feature_rows(records: list[dict[str, Any]]) -> list[FeatureRow]:
    return [extract_feature_row(record) for record in records]


def training_readiness(rows: list[FeatureRow]) -> dict[str, Any]:
    labeled = [row for row in rows if row.label is not None]
    labeled_windows = {row.kalshi_ticker for row in labeled if row.kalshi_ticker}
    observed_windows = {row.kalshi_ticker for row in rows if row.kalshi_ticker}
    labels = {row.label for row in labeled}

    needed_min = max(0, MIN_LABELED_WINDOWS - len(labeled_windows))
    needed_preferred = max(0, PREFERRED_LABELED_WINDOWS - len(labeled_windows))
    return {
        "records": len(rows),
        "observed_windows": len(observed_windows),
        "labeled_records": len(labeled),
        "labeled_windows": len(labeled_windows),
        "label_values": sorted(labels),
        "can_train": (
            len(labeled_windows) >= MIN_LABELED_WINDOWS
            and len(labels) >= 2
        ),
        "minimum_labeled_windows_required": MIN_LABELED_WINDOWS,
        "preferred_labeled_windows": PREFERRED_LABELED_WINDOWS,
        "additional_windows_for_minimum": needed_min,
        "additional_observation_time_for_minimum_minutes": needed_min * 15,
        "additional_windows_for_preferred": needed_preferred,
        "additional_observation_time_for_preferred_minutes": needed_preferred * 15,
        "reason": (
            "Need settled/labeled records with at least two label classes."
            if len(labels) < 2 else
            f"Need at least {MIN_LABELED_WINDOWS} labeled Kalshi windows."
        ),
    }


def train_if_ready(
    rows: list[FeatureRow],
    model_path: Path = DEFAULT_MODEL_PATH,
) -> dict[str, Any]:
    readiness = training_readiness(rows)
    if not readiness["can_train"]:
        return {"trained": False, **readiness}

    raise NotImplementedError(
        "Training is intentionally not implemented until the real labeled dataset "
        "exists and the chosen sklearn/lightgbm dependency is reviewed."
    )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Extract cross-venue match-confidence features.")
    parser.add_argument("--log-path", default=str(DEFAULT_LOG_PATH))
    parser.add_argument("--model-path", default=str(DEFAULT_MODEL_PATH))
    args = parser.parse_args(argv)

    records = load_cross_venue_records(Path(args.log_path))
    rows = extract_feature_rows(records)
    result = train_if_ready(rows, Path(args.model_path))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
