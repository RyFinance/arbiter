from __future__ import annotations

import json
from datetime import datetime, timezone

from polyuni.regime_signal import LightGbmRegimeReader


def _stamp(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


def test_fresh_lightgbm_signal_is_usable(tmp_path):
    now = 1_800_000_000.0
    path = tmp_path / "regime.json"
    path.write_text(json.dumps({
        "regime": "HIGH",
        "confidence": 0.81,
        "source": "lightgbm",
        "applied_at_utc": _stamp(now - 5),
    }))

    signal = LightGbmRegimeReader(path, max_age_s=60, refresh_interval_s=0).read(now)

    assert signal.usable is True
    assert signal.regime == "HIGH"
    assert signal.age_s == 5


def test_stale_or_missing_signal_falls_back_safely(tmp_path):
    now = 1_800_000_000.0
    missing = LightGbmRegimeReader(tmp_path / "missing.json", 60, 0).read(now)
    assert missing.status == "missing"
    assert missing.usable is False

    path = tmp_path / "regime.json"
    path.write_text(json.dumps({
        "regime": "LOW",
        "source": "lightgbm",
        "applied_at_utc": _stamp(now - 61),
    }))
    stale = LightGbmRegimeReader(path, 60, 0).read(now)
    assert stale.status == "stale"
    assert stale.usable is False


def test_wrong_source_is_never_applied(tmp_path):
    now = 1_800_000_000.0
    path = tmp_path / "regime.json"
    path.write_text(json.dumps({
        "regime": "HIGH",
        "source": "manual",
        "applied_at_utc": _stamp(now),
    }))
    signal = LightGbmRegimeReader(path, 60, 0).read(now)
    assert signal.status == "wrong_source"
    assert signal.usable is False


def test_fresh_but_low_confidence_signal_is_not_applied(tmp_path):
    now = 1_800_000_000.0
    path = tmp_path / "regime.json"
    path.write_text(json.dumps({
        "regime": "HIGH",
        "confidence": 0.44,
        "source": "lightgbm",
        "applied_at_utc": _stamp(now),
    }))
    signal = LightGbmRegimeReader(
        path, 60, refresh_interval_s=0, min_confidence=0.45,
    ).read(now)
    assert signal.status == "low_confidence"
    assert signal.usable is False
