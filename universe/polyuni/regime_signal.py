"""Read the sibling bot's cached LightGBM BTC volatility regime safely."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class RegimeSignal:
    regime: str = "UNKNOWN"
    confidence: float = 0.0
    source: str = "fallback"
    status: str = "missing"
    age_s: Optional[float] = None
    applied_at_utc: str = ""
    minimum_confidence: float = 0.45

    @property
    def usable(self) -> bool:
        return (
            self.status == "fresh"
            and self.source == "lightgbm"
            and self.confidence >= self.minimum_confidence
            and self.regime in {
            "LOW", "MED", "HIGH"
            }
        )


class LightGbmRegimeReader:
    """Small cached file reader; no model import or network work in the hot path."""

    def __init__(
        self,
        path: str | Path,
        max_age_s: float,
        refresh_interval_s: float = 2.0,
        min_confidence: float = 0.45,
    ):
        self.path = Path(path).expanduser()
        self.max_age_s = float(max_age_s)
        self.refresh_interval_s = float(refresh_interval_s)
        self.min_confidence = float(min_confidence)
        self._last_checked = 0.0
        self._cached = RegimeSignal()

    @staticmethod
    def _timestamp(raw: object) -> Optional[float]:
        if not raw:
            return None
        try:
            text = str(raw)
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            return datetime.fromisoformat(text).timestamp()
        except (TypeError, ValueError):
            return None

    def read(self, now: Optional[float] = None) -> RegimeSignal:
        wall = time.time() if now is None else float(now)
        if wall - self._last_checked < self.refresh_interval_s:
            return self._cached
        self._last_checked = wall
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self._cached = RegimeSignal(status="missing")
            return self._cached
        except (OSError, json.JSONDecodeError):
            self._cached = RegimeSignal(status="invalid")
            return self._cached

        regime = str(payload.get("regime") or "UNKNOWN").upper()
        source = str(payload.get("source") or "unknown").lower()
        applied = str(payload.get("applied_at_utc") or "")
        timestamp = self._timestamp(applied)
        try:
            file_timestamp = self.path.stat().st_mtime
        except OSError:
            file_timestamp = wall
        source_timestamp = timestamp if timestamp is not None else file_timestamp
        age = max(0.0, wall - source_timestamp)
        try:
            confidence = float(payload.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        if source != "lightgbm":
            status = "wrong_source"
        elif regime not in {"LOW", "MED", "HIGH"}:
            status = "invalid_regime"
        elif age > self.max_age_s:
            status = "stale"
        elif confidence < self.min_confidence:
            status = "low_confidence"
        else:
            status = "fresh"
        self._cached = RegimeSignal(
            regime=regime,
            confidence=confidence,
            source=source,
            status=status,
            age_s=age,
            applied_at_utc=applied,
            minimum_confidence=self.min_confidence,
        )
        return self._cached
