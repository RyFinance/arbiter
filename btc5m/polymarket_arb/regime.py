"""Regime-aware execution parameters — LIVE runtime ATR computation (Task 3).

This module computes a rolling Average True Range (ATR) from the live BTC feed
at runtime (reusing the existing candle ingestion in LSEClient), classifies the
current volatility regime (LOW/MED/HIGH) using the already-proven train-only
cutoffs, and exposes it as a read-only signal.

Design:
  * Cold path: ``forecast_session_regime`` (in kronos_session.py) runs once at
    startup to set the initial regime. It is the COLD PATH entry point.
  * Hot path: this module provides ``RuntimeRegime`` — a lightweight class that
    fetches fresh BTC candles periodically (on window rotation) and recomputes
    ATR. It does NOT auto-adjust MAX_QUOTE_AGE_MS or any risk parameter — it
    only computes the signal, logs it into bot.jsonl, and exposes it as a
    read-only attribute.

The ATR heuristic and thresholds are reused directly from kronos_session.py:
  * ``_compute_atr`` — standard 14-period ATR from OHLCV candles
  * ``_normalize_atr_to_pct`` — dollar ATR -> percentage of mean close
  * ``_scale_atr_to_daily`` — 1-min ATR% scaled to daily via sqrt(1440)
  * ``_regime_from_atr`` — HIGH (>5%), MED (3-5%), LOW (<3%)

Usage in main.py:
    regime = RuntimeRegime(lse_client)
    await regime.update()  # called on each window rotation
    log.info(f"Regime: {regime.current}")  # read-only

Usage in scripts/regime_backtest.py:
    Replays historical bot.jsonl TRADE events and annotates each fe/miss with
    the regime that was active at that timestamp (via historical candle data).
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .clients.lse_client import LSEClient
from .config import SETTINGS
from .logger import log
from .kronos_session import (
    ATR_HIGH_THRESHOLD,
    ATR_LOW_THRESHOLD,
    _compute_atr,
    _normalize_atr_to_pct,
    _regime_from_atr,
    _scale_atr_to_daily,
    ATR_TIME_SCALING_FACTOR,
    MINUTES_PER_DAY,
)


@dataclass
class RegimeSignal:
    """Immutable read-only regime signal computed at runtime.

    Attributes:
        regime: "LOW" | "MED" | "HIGH"
        atr_14d: daily-equivalent ATR% (scaled from 1-min via sqrt(1440))
        confidence: 0.0 to 1.0
        source: "live" | "heuristic" | "initial" (startup forecast) | "default"
        ts: epoch seconds when this signal was computed
        window_start: the window rotation timestamp this signal corresponds to
    """
    regime: str
    atr_14d: float
    confidence: float
    source: str
    ts: float
    window_start: float = 0.0

    def to_log(self) -> dict:
        """Serialize to a JSON-safe dict for bot.jsonl logging."""
        return {
            "regime": self.regime,
            "atr_14d": round(self.atr_14d, 6),
            "confidence": round(self.confidence, 4),
            "source": self.source,
            "ts": round(self.ts, 3),
            "window_start": self.window_start,
        }


class RuntimeRegime:
    """Computes a rolling ATR-based regime signal from the live BTC feed.

    This is NOT auto-adjusting — it only COMPUTES and EXPOSES the regime.
    The caller (main.py) reads ``self.current`` and decides what to do with it.
    By design, no risk parameters are modified here; that remains a future
    enhancement gated on operator review.

    The regime is recomputed on every window rotation (default: every 5 minutes,
    matching SETTINGS.window_seconds). It fetches the latest 1-minute BTC/USD
    candles from LSEClient (the existing ingestion path) and computes ATR over
    a 14-period window.
    """

    def __init__(self, lse_client: Optional[LSEClient] = None,
                 atr_period: int = 14,
                 candle_window_hours: int = 1):
        """Initialize the runtime regime computer.

        Args:
            lse_client: LSEClient instance for fetching BTC candles. If None,
                        a new one is created lazily.
            atr_period: ATR lookback period (default 14, matching the proven heuristic).
            candle_window_hours: How many hours of 1-min candles to fetch for
                                 ATR computation (default 1h = 60 candles, enough
                                 for a 14-period ATR plus margin).
        """
        self._lse_client = lse_client
        self._owns_client = lse_client is None
        self._atr_period = atr_period
        self._candle_window = candle_window_hours
        # Read-only signal: callers read this, never write it.
        self._current: Optional[RegimeSignal] = None
        self._lock: Optional[asyncio.Lock] = None

    @property
    def current(self) -> Optional[RegimeSignal]:
        """The most recent regime signal, or None if not yet computed."""
        return self._current

    @property
    def regime(self) -> str:
        """Convenience accessor for the current regime label."""
        if self._current is None:
            return "UNKNOWN"
        return self._current.regime

    def _get_lock(self) -> asyncio.Lock:
        """Lazy-init the asyncio lock (avoids breaking non-async construction)."""
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def _get_client(self) -> LSEClient:
        """Lazy-init the LSE client if not provided at construction."""
        if self._lse_client is None:
            self._lse_client = LSEClient()
        return self._lse_client

    async def update(self, window_start: float = 0.0) -> RegimeSignal:
        """Fetch fresh BTC candles, compute ATR, classify regime, and log to bot.jsonl.

        This should be called on every window rotation. The signal is stored
        in ``self._current`` and also logged as a TRADE event with outcome
        "regime_update" so it appears persistently in bot.jsonl.

        Args:
            window_start: The epoch timestamp of the window this rotation corresponds to.

        Returns:
            The computed RegimeSignal.
        """
        async with self._get_lock():
            client = await self._get_client()
            now = time.time()
            # Fetch BTC/USD 1-minute candles for the ATR window.
            candles = await self._fetch_recent_candles(client)

            if not candles:
                # Fall back to a default MED regime if no candle data is available.
                signal = RegimeSignal(
                    regime="MED",
                    atr_14d=0.0,
                    confidence=0.5,
                    source="default",
                    ts=now,
                    window_start=window_start,
                )
                self._current = signal
                log.trade("regime_update", {
                    "regime": signal.regime,
                    "atr_14d": signal.atr_14d,
                    "confidence": signal.confidence,
                    "source": signal.source,
                    "window_start": window_start,
                    "stage_timings": {},
                    "opp_to_order_ms": 0,
                    "ack_ms": 0,
                })
                return signal

            # Compute ATR using the proven heuristic from kronos_session.
            atr_dollars = _compute_atr(candles, period=self._atr_period)
            atr_pct = _normalize_atr_to_pct(atr_dollars, candles)
            atr_scaled = _scale_atr_to_daily(atr_pct)
            regime, confidence = _regime_from_atr(atr_scaled)

            signal = RegimeSignal(
                regime=regime,
                atr_14d=atr_scaled,
                confidence=confidence,
                source="live",
                ts=now,
                window_start=window_start,
            )
            self._current = signal

            # Log the regime signal into bot.jsonl as a TRADE event.
            log.trade("regime_update", {
                "regime": signal.regime,
                "atr_14d": round(signal.atr_14d, 6),
                "confidence": round(signal.confidence, 4),
                "source": signal.source,
                "window_start": window_start,
                "stage_timings": {},
                "opp_to_order_ms": 0,
                "ack_ms": 0,
            })
            log.info(
                f"REGIME {signal.regime} (atr_14d={signal.atr_14d:.6f}, "
                f"conf={signal.confidence:.2f}, source={signal.source}, "
                f"window={window_start})"
            )
            return signal

    async def _fetch_recent_candles(self, client: LSEClient) -> List[Dict[str, Any]]:
        """Fetch recent 1-minute BTC/USD candles from the existing ingestion path.

        Uses LSEClient.fetch_candles (the same path kronos_session uses) — no
        new data path is built.
        """
        session = await client._get_session()
        # Fetch enough candles for the ATR period + margin.
        limit = max(self._atr_period * 2, 30)
        params = {
            "symbol": "BTC/USD",
            "timeframe": "1m",
            "limit": str(limit),
        }
        try:
            resp = await session.get("/candles", params=params, timeout=10.0)
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict):
                data = data.get("data", [])
            if isinstance(data, list):
                return data
            return []
        except Exception as e:
            log.warning(f"RuntimeRegime candle fetch failed: {e!r}")
            return []

    @classmethod
    def classify_atr(cls, candles: List[Dict[str, Any]], period: int = 14) -> RegimeSignal:
        """Classify a regime from a static set of candles (for backtesting).

        Pure function — no I/O. Returns a RegimeSignal with source="heuristic".
        """
        atr_dollars = _compute_atr(candles, period=period)
        atr_pct = _normalize_atr_to_pct(atr_dollars, candles)
        atr_scaled = _scale_atr_to_daily(atr_pct)
        regime, confidence = _regime_from_atr(atr_scaled)
        return RegimeSignal(
            regime=regime,
            atr_14d=atr_scaled,
            confidence=confidence,
            source="heuristic",
            ts=time.time(),
        )

    async def aclose(self):
        """Clean up the LSE client if we own it."""
        if self._owns_client and self._lse_client is not None:
            await self._lse_client.aclose()
        self._lse_client = None
