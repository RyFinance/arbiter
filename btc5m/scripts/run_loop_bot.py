#!/usr/bin/env python3
"""Isolated PAPER sequential loop bot for BTC up/down markets.

A self-contained paper-testing loop managed from the dashboard. It is fully
isolated from the main bot:

  * reads ONLY public order books (CLOB REST) and Gamma market metadata —
    no signing, no auth, no trading client, no websockets. There is no code
    path in this file that can place a real order;
  * keeps its own state under ``state/loop_bot/`` — own PnL, own events,
    own stop file. It neither reads nor writes the main bot's state files;
  * runs as a continuous loop until stopped (dashboard button writes
    ``state/loop_bot/STOP`` or SIGTERM).

Strategy per timeframe (5m / 15m / 30m): during a short early-window period,
paper-buy one near-even outcome only after its local order-book quote has moved
enough to demonstrate volatility. Later, buy the opposite outcome only when
the *full-size depth VWAP*, including Polymarket's crypto taker fee, produces
the configured net edge. If that never happens before a strict deadline, sell
the first leg into full bid depth and realize the paper loss. There is at most
one completed or aborted sequence per market window.

This runner deliberately has no authenticated client and no order endpoint.
Every fill is a FOK-like paper simulation against one public ``/book``
snapshot. The event/state schema is ``sequential_v2`` so the old fee-free
simulated history remains recoverable but is never mixed into new results.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
import time
import urllib.parse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = PROJECT_ROOT / "state" / "loop_bot"
STOP_FILE = STATE_DIR / "STOP"
CONFIG_PATH = STATE_DIR / "config.json"
EVENTS_FILE = STATE_DIR / "events.jsonl"
STATE_FILE = STATE_DIR / "state.json"
LOG_FILE = STATE_DIR / "bot.log"
REGIME_STATE_FILE = PROJECT_ROOT / "state" / "regime_active.json"

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"
FINNHUB_BASE = "https://finnhub.io/api/v1"

TIMEFRAMES: Dict[str, int] = {"5m": 300, "15m": 900, "30m": 1800}
ASSET = "btc"
POLL_S = 2.0
BTC_SAMPLE_S = 30.0

USER_AGENT = "pm-loop-bot-paper/1.0 (dashboard-managed, read-only)"
SCHEMA_VERSION = "sequential_v2"
CRYPTO_TAKER_FEE_RATE = 0.07
FIVE_DP = Decimal("0.00001")
FINNHUB_KEY_ENV = "POLYMARKET_ARB_LOOPBOT_FINNHUB_KEY"


@dataclass(frozen=True)
class DepthFill:
    """A full-size paper fill walked through one side of one book snapshot."""

    action: str
    shares: float
    vwap: float
    worst_price: float
    notional: float
    fee: float
    levels_used: int
    available_shares: float
    observed_at: float


def _round5(value: float) -> float:
    return float(Decimal(str(value)).quantize(FIVE_DP, rounding=ROUND_HALF_UP))


def crypto_taker_fee(contracts: float, price: float) -> float:
    """Crypto-market taker fee: C * 0.07 * p * (1-p), rounded to 5 dp."""
    if contracts <= 0 or not 0.0 < price < 1.0:
        return 0.0
    return _round5(contracts * CRYPTO_TAKER_FEE_RATE * price * (1.0 - price))


def _book_level(level: Any) -> Optional[Tuple[float, float]]:
    try:
        if isinstance(level, dict):
            price, size = float(level["price"]), float(level["size"])
        else:
            price, size = float(level[0]), float(level[1])
    except (TypeError, ValueError, KeyError, IndexError):
        return None
    if not 0.0 < price < 1.0 or size <= 0.0:
        return None
    return price, size


def depth_fill(
    levels: Iterable[Any], requested_shares: float, action: str, observed_at: Optional[float] = None,
) -> Optional[DepthFill]:
    """Return full-size VWAP/fee for BUY asks or SELL bids; never partial-fill."""
    if requested_shares <= 0 or action not in {"BUY", "SELL"}:
        return None
    parsed = [item for raw in levels if (item := _book_level(raw)) is not None]
    parsed.sort(key=lambda item: item[0], reverse=action == "SELL")
    available = sum(size for _, size in parsed)
    if available + 1e-9 < requested_shares:
        return None
    remaining = requested_shares
    notional = 0.0
    fee_unrounded = 0.0
    worst = 0.0
    used = 0
    for price, level_size in parsed:
        take = min(remaining, level_size)
        if take <= 0:
            continue
        notional += take * price
        fee_unrounded += take * CRYPTO_TAKER_FEE_RATE * price * (1.0 - price)
        remaining -= take
        worst = price
        used += 1
        if remaining <= 1e-9:
            break
    if remaining > 1e-9:
        return None
    return DepthFill(
        action=action,
        shares=float(requested_shares),
        vwap=notional / requested_shares,
        worst_price=worst,
        notional=notional,
        fee=_round5(fee_unrounded),
        levels_used=used,
        available_shares=available,
        observed_at=float(observed_at if observed_at is not None else time.time()),
    )


def pair_net_pnl(first: DepthFill, second: DepthFill) -> float:
    """Guaranteed net PnL when equal YES+NO shares are held to resolution."""
    shares = min(first.shares, second.shares)
    return _round5(shares - first.notional - second.notional - first.fee - second.fee)


def protective_exit_pnl(first: DepthFill, exit_fill: DepthFill) -> float:
    """Realized net PnL when the first BUY is unwound with a SELL."""
    return _round5(exit_fill.notional - first.notional - first.fee - exit_fill.fee)


def quote_range(samples: Sequence[Tuple[float, float]], now: float, lookback_s: float) -> float:
    prices = [price for ts, price in samples if now - ts <= lookback_s]
    return (max(prices) - min(prices)) if len(prices) >= 2 else 0.0


# --------------------------------------------------------------------------
# Pure helpers (unit-testable without network)
# --------------------------------------------------------------------------

def window_ts(now: float, seconds: int) -> int:
    """Start epoch of the rolling window containing ``now``."""
    return int(now // seconds) * seconds


def window_slug(asset: str, tf: str, ts: int) -> str:
    """Deterministic market slug, same contract as the main bot."""
    return f"{asset}-updown-{tf}-{int(ts)}"


def should_enter(combined: Optional[float], min_edge: float, already_entered: bool,
                 window_open_at: float, now: float, no_entry_seconds: float) -> bool:
    """Pure entry gate. One entry per window, outside the no-entry zone."""
    if combined is None or already_entered:
        return False
    if no_entry_seconds > 0 and now - window_open_at < no_entry_seconds:
        return False
    return combined < 1.0 - min_edge


def locked_pnl(combined: float, size: float) -> float:
    """Both legs held to close pay exactly `size`; cost was combined*size."""
    return (1.0 - combined) * size


def parse_updown_tokens(market: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """Return {"yes": up_token_id, "no": down_token_id} for an up/down market."""
    tokens = market.get("tokens") or []
    up_tok = next((t for t in tokens if str(t.get("outcome", "")).lower() == "up"), None)
    down_tok = next((t for t in tokens if str(t.get("outcome", "")).lower() == "down"), None)
    if up_tok and down_tok and up_tok.get("token_id") and down_tok.get("token_id"):
        return {"yes": str(up_tok["token_id"]), "no": str(down_tok["token_id"])}
    outcomes = market.get("outcomes") or []
    clob = market.get("clobTokenIds") or []
    if isinstance(outcomes, str):
        outcomes = json.loads(outcomes)
    if isinstance(clob, str):
        clob = json.loads(clob)
    if not outcomes or not clob or len(outcomes) != len(clob):
        return None
    low = [str(o).lower() for o in outcomes]
    if "up" not in low or "down" not in low:
        return None
    return {"yes": str(clob[low.index("up")]), "no": str(clob[low.index("down")])}


# --------------------------------------------------------------------------
# Loop bot
# --------------------------------------------------------------------------

class LoopBot:
    """Polling sequential paper engine. No authenticated/live order path exists."""

    _NUMERIC_CONFIG = {
        "order_size": (5.0, "order_size"),
        "min_edge": (0.003, "min_edge"),
        "no_entry_seconds": (0.5, "no_entry_seconds"),
        "entry_window_seconds": (60.0, "entry_window_seconds"),
        "first_price_min": (0.45, "first_price_min"),
        "first_price_max": (0.49, "first_price_max"),
        "hedge_timeout_seconds": (45.0, "hedge_timeout_seconds"),
        "force_exit_before_close_seconds": (10.0, "force_exit_before_close_seconds"),
        "local_vol_lookback_seconds": (30.0, "local_vol_lookback_seconds"),
        "local_vol_threshold": (0.01, "local_vol_threshold"),
        "regime_refresh_seconds": (60.0, "regime_refresh_seconds"),
        "regime_forecast_interval_seconds": (600.0, "regime_forecast_interval_seconds"),
        "regime_max_age_seconds": (900.0, "regime_max_age_seconds"),
        "regime_min_confidence": (0.45, "regime_min_confidence"),
    }
    _CRITICAL_EVENTS = {
        "first_filled", "pair_completed", "protective_exit", "exit_failed",
        "manual_review", "sequence_aborted", "phase",
    }

    def __init__(self, config: Dict[str, Any], finnhub_key: Optional[str]):
        self.config = dict(config)
        for key, (default, attr) in self._NUMERIC_CONFIG.items():
            setattr(self, attr, float(config.get(key, default)))
        self.timeframes = [
            tf for tf in config.get("timeframes", ["5m", "15m", "30m"]) if tf in TIMEFRAMES
        ]
        self.finnhub_key = finnhub_key
        self.started_at = time.time()
        self.session_id = f"seq-{int(self.started_at)}-{os.getpid()}"
        self.last_btc_sample = 0.0
        self.latest_btc: Optional[float] = None
        self.totals = self._empty_totals()
        self.session_totals = self._empty_totals()
        self.unresolved_positions: List[Dict[str, Any]] = []
        self.rejections: Dict[str, int] = {}
        self.tf_state = {tf: self._blank_tf_state() for tf in self.timeframes}
        self.regime: Dict[str, Any] = {
            "status": "missing", "regime": None, "source": None, "confidence": None,
            "age_s": None, "adjusted_local_vol_threshold": self.local_vol_threshold,
            "model": "lightgbm_volatility_model_v3.pkl", "last_checked_at": None,
        }
        self._last_regime_check = 0.0
        self._last_regime_forecast_attempt = 0.0
        self._regime_forecast_task: Optional[asyncio.Task] = None
        self._stop = False
        self._config_mtime: Optional[float] = None
        self._token_retry_at: Dict[str, float] = {}
        self._restore_manual_review_count = 0
        recovered = self._restore_state()
        self._write_meta_event("start", {
            "strategy_schema": SCHEMA_VERSION, "timeframes": self.timeframes,
            "order_size": self.order_size, "min_net_edge": self.min_edge,
            "recovered_active_positions": recovered,
            "stale_positions_marked_manual_review": self._restore_manual_review_count,
        })

    @staticmethod
    def _empty_totals() -> Dict[str, float]:
        return {
            "realized": 0.0, "fees": 0.0, "first_fills": 0,
            "completed": 0, "forced_exits": 0, "aborted": 0,
            "manual_review": 0, "closed": 0,
        }

    @staticmethod
    def _blank_tf_state() -> Dict[str, Any]:
        return {
            "window": None, "window_open_at": None, "slug": None, "tokens": None,
            "phase": "waiting_market", "sequence_done": False, "position": None,
            "yes": None, "no": None, "combined": None, "best_combined": None,
            "books": {}, "quote_samples": {"yes": [], "no": []},
            "local_vol": {"yes": 0.0, "no": 0.0}, "second_preview": None,
            "rejections": {}, "last_rejection": None, "last_rejection_event_at": 0.0,
        }

    # ------------------------------------------------------------- persistence/events

    def _event(self, kind: str, payload: Dict[str, Any]) -> None:
        row: Dict[str, Any] = {
            "t": time.time(), "kind": kind, "schema": SCHEMA_VERSION,
            "session_id": self.session_id,
        }
        row.update(payload)
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            with EVENTS_FILE.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, separators=(",", ":")) + "\n")
                if kind in self._CRITICAL_EVENTS:
                    fh.flush()
                    os.fsync(fh.fileno())
        except OSError:
            pass

    def _write_meta_event(self, kind: str, payload: Dict[str, Any]) -> None:
        self._event("meta", {"meta_kind": kind, **payload})

    def _log(self, message: str) -> None:
        try:
            with LOG_FILE.open("a", encoding="utf-8") as fh:
                fh.write(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) + " " + message + "\n")
        except OSError:
            pass

    def _restore_state(self) -> int:
        """Restore only same-window v2 positions; legacy totals never migrate."""
        try:
            previous = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return 0
        if previous.get("strategy_schema") != SCHEMA_VERSION:
            return 0
        saved_totals = previous.get("strategy_totals") or {}
        if isinstance(saved_totals.get("realized_pnl"), (int, float)):
            self.totals["realized"] = float(saved_totals["realized_pnl"])
        for key, value in saved_totals.items():
            if key in self.totals and isinstance(value, (int, float)):
                self.totals[key] = value
        unresolved = previous.get("unresolved_positions")
        if isinstance(unresolved, list):
            self.unresolved_positions = [row for row in unresolved if isinstance(row, dict)]
        recovered = 0
        now = time.time()
        for tf in self.timeframes:
            old = (previous.get("timeframes") or {}).get(tf)
            if not isinstance(old, dict):
                continue
            current_window = window_ts(now, TIMEFRAMES[tf])
            if old.get("window") != current_window:
                if old.get("phase") in {"waiting_second", "exit_pending", "manual_review"}:
                    unresolved = {
                        "tf": tf, "slug": old.get("slug"), "window_ts": old.get("window"),
                        "position": old.get("position"),
                        "reason": "restart_after_window_rollover", "marked_at": now,
                    }
                    position_id = (old.get("position") or {}).get("position_id")
                    already = any(
                        (row.get("position") or {}).get("position_id") == position_id
                        for row in self.unresolved_positions
                    )
                    if not already:
                        self.unresolved_positions.append(unresolved)
                        self.totals["manual_review"] += 1
                        self.session_totals["manual_review"] += 1
                        self._restore_manual_review_count += 1
                        self._event("manual_review", unresolved)
                continue
            if old.get("phase") not in {
                "waiting_second", "exit_pending", "paired", "aborted", "manual_review", "no_entry",
            }:
                continue
            fresh = self._blank_tf_state()
            for key in fresh:
                if key in old and key != "quote_samples":
                    fresh[key] = old[key]
            fresh["quote_samples"] = {"yes": [], "no": []}
            self.tf_state[tf] = fresh
            if fresh["phase"] in {"waiting_second", "exit_pending"}:
                recovered += 1
        return recovered

    # ------------------------------------------------------------- model state

    @staticmethod
    def _utc_timestamp(value: Any) -> Optional[float]:
        if not isinstance(value, str):
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
        except ValueError:
            return None

    def _refresh_regime(self, now: float, force: bool = False) -> None:
        if not force and now - self._last_regime_check < self.regime_refresh_seconds:
            return
        self._last_regime_check = now
        prior_status = self.regime.get("status")
        try:
            raw = json.loads(REGIME_STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {}
        applied_at = self._utc_timestamp(raw.get("applied_at_utc"))
        age = max(0.0, now - applied_at) if applied_at is not None else None
        source = raw.get("source")
        confidence = raw.get("confidence") if isinstance(raw.get("confidence"), (int, float)) else None
        regime = raw.get("regime") if raw.get("regime") in {"LOW", "MED", "HIGH"} else None
        if not raw:
            status = "missing"
        elif source != "lightgbm":
            status = "non_lightgbm"
        elif age is None or age > self.regime_max_age_seconds:
            status = "stale"
        elif confidence is None or confidence < self.regime_min_confidence:
            status = "low_confidence"
        elif regime is None:
            status = "invalid"
        else:
            status = "active"
        # LightGBM is volatility-only and is a soft threshold adjustment. A
        # stale/missing/low-confidence model uses the neutral base threshold.
        multiplier = {"HIGH": 0.75, "MED": 1.0, "LOW": 1.25}.get(regime, 1.0) if status == "active" else 1.0
        self.regime = {
            "status": status, "regime": regime, "source": source,
            "confidence": confidence, "age_s": round(age, 1) if age is not None else None,
            "adjusted_local_vol_threshold": round(self.local_vol_threshold * multiplier, 6),
            "model": "lightgbm_volatility_model_v3.pkl", "last_checked_at": now,
            "snapshot_path": str(REGIME_STATE_FILE),
        }
        if status != prior_status:
            self._write_meta_event("regime", self.regime)

    async def _run_regime_forecast(self) -> None:
        """Refresh v3 in a bounded subprocess, completely off the quote hot path."""
        proc: Optional[asyncio.subprocess.Process] = None
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "polymarket_arb.kronos_session",
                cwd=str(PROJECT_ROOT), stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=45.0)
            self._refresh_regime(time.time(), force=True)
            self._write_meta_event("regime_refresh", {
                "exit_code": proc.returncode, "status": self.regime.get("status"),
                "source": self.regime.get("source"),
            })
        except asyncio.TimeoutError:
            if proc and proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    proc.kill()
            self._write_meta_event("regime_refresh", {"exit_code": None, "status": "timeout"})
        except asyncio.CancelledError:
            if proc and proc.returncode is None:
                proc.terminate()
            raise
        except Exception as exc:  # noqa: BLE001
            self._write_meta_event("regime_refresh", {
                "exit_code": None, "status": "error", "message": f"{type(exc).__name__}: {exc}",
            })

    def _maybe_refresh_regime_forecast(self, now: float) -> None:
        """Schedule a cold-path v3 refresh when absent/stale or periodically due."""
        if self._regime_forecast_task is not None and not self._regime_forecast_task.done():
            return
        age = self.regime.get("age_s")
        due = self.regime.get("status") in {"missing", "stale", "invalid"}
        due = due or (isinstance(age, (int, float)) and age >= self.regime_forecast_interval_seconds)
        if not due or now - self._last_regime_forecast_attempt < min(self.regime_forecast_interval_seconds, 900.0):
            return
        self._last_regime_forecast_attempt = now
        self._regime_forecast_task = asyncio.create_task(self._run_regime_forecast())

    # ------------------------------------------------------------- network

    async def _resolve_tokens(self, client: httpx.AsyncClient, slug: str) -> Optional[Dict[str, str]]:
        for path, key in (("/events", "data"), ("/markets", "data")):
            try:
                resp = await client.get(f"{GAMMA_BASE}{path}", params={"slug": slug}, timeout=8.0)
                if resp.status_code != 200:
                    continue
                payload = resp.json()
                raw = payload if isinstance(payload, list) else (payload.get(key, []) if isinstance(payload, dict) else [])
                for item in raw:
                    if not isinstance(item, dict):
                        continue
                    market = item
                    if path == "/events":
                        markets = item.get("markets") or []
                        if not markets:
                            continue
                        market = markets[0]
                    tokens = parse_updown_tokens(market)
                    if tokens:
                        return tokens
            except Exception:  # noqa: BLE001
                continue
        return None

    async def _book_snapshot(self, client: httpx.AsyncClient, token_id: str) -> Optional[Dict[str, Any]]:
        try:
            resp = await client.get(f"{CLOB_BASE}/book", params={"token_id": token_id}, timeout=3.0)
            if resp.status_code != 200:
                return None
            book = resp.json()
            observed_at = time.time()
            asks = book.get("asks") or []
            bids = book.get("bids") or []
            buy = depth_fill(asks, self.order_size, "BUY", observed_at)
            sell = depth_fill(bids, self.order_size, "SELL", observed_at)
            parsed_asks = [item for raw in asks if (item := _book_level(raw)) is not None]
            parsed_bids = [item for raw in bids if (item := _book_level(raw)) is not None]
            return {
                "observed_at": observed_at,
                "best_ask": min((p for p, _ in parsed_asks), default=None),
                "best_bid": max((p for p, _ in parsed_bids), default=None),
                "buy": asdict(buy) if buy else None,
                "sell": asdict(sell) if sell else None,
                "ask_depth": sum(s for _, s in parsed_asks),
                "bid_depth": sum(s for _, s in parsed_bids),
            }
        except Exception:  # noqa: BLE001
            return None

    async def _fetch_pair(self, client: httpx.AsyncClient, tokens: Dict[str, str]) -> Dict[str, Any]:
        yes_task = asyncio.create_task(self._book_snapshot(client, tokens["yes"]))
        no_task = asyncio.create_task(self._book_snapshot(client, tokens["no"]))
        yes, no = await asyncio.gather(yes_task, no_task)
        return {"yes": yes, "no": no}

    async def _btc_price(self, client: httpx.AsyncClient) -> Optional[float]:
        if not self.finnhub_key:
            return None
        now = time.time()
        if now - self.last_btc_sample < BTC_SAMPLE_S:
            return None
        self.last_btc_sample = now
        params = urllib.parse.urlencode({"symbol": "BINANCE:BTCUSDT", "token": self.finnhub_key})
        try:
            resp = await client.get(f"{FINNHUB_BASE}/quote?{params}", timeout=8.0)
            if resp.status_code != 200:
                return None
            data = resp.json()
            price = data.get("c") if isinstance(data, dict) else None
            return float(price) if isinstance(price, (int, float)) else None
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------- state machine

    def _reject(self, tf: str, st: Dict[str, Any], reason: str, now: float, **details: Any) -> None:
        self.rejections[reason] = self.rejections.get(reason, 0) + 1
        st["rejections"][reason] = st["rejections"].get(reason, 0) + 1
        emit = reason != st.get("last_rejection") or now - st.get("last_rejection_event_at", 0.0) >= 10.0
        st["last_rejection"] = reason
        if emit:
            st["last_rejection_event_at"] = now
            self._event("rejected", {"tf": tf, "slug": st.get("slug"), "reason": reason, **details})

    def _record_quotes(self, st: Dict[str, Any], books: Dict[str, Any], now: float) -> None:
        for side in ("yes", "no"):
            book = books.get(side) or {}
            price = book.get("best_ask")
            samples: List[Tuple[float, float]] = st["quote_samples"][side]
            if isinstance(price, (int, float)):
                samples.append((now, float(price)))
            cutoff = now - max(self.local_vol_lookback_seconds * 2.0, 10.0)
            st["quote_samples"][side] = [(ts, p) for ts, p in samples if ts >= cutoff]
            st["local_vol"][side] = round(
                quote_range(st["quote_samples"][side], now, self.local_vol_lookback_seconds), 6
            )

    @staticmethod
    def _fill_from_dict(raw: Any) -> Optional[DepthFill]:
        if not isinstance(raw, dict):
            return None
        try:
            return DepthFill(**{field: raw[field] for field in DepthFill.__dataclass_fields__})
        except (KeyError, TypeError, ValueError):
            return None

    def _phase(self, tf: str, st: Dict[str, Any], phase: str, now: float, **details: Any) -> None:
        st["phase"] = phase
        self._event("phase", {"tf": tf, "slug": st.get("slug"), "phase": phase, "at": now, **details})

    def _first_leg(self, tf: str, st: Dict[str, Any], side: str, fill: DepthFill, now: float) -> None:
        opposite = "no" if side == "yes" else "yes"
        position_id = f"{tf}-{st['window']}-{side}"
        deadline = min(
            now + self.hedge_timeout_seconds,
            float(st["window"] + TIMEFRAMES[tf] - self.force_exit_before_close_seconds),
        )
        st["position"] = {
            "position_id": position_id, "side": side, "opposite": opposite,
            "first": asdict(fill), "first_at": now, "deadline_at": deadline,
            "btc": self.latest_btc, "second": None, "exit": None,
        }
        for totals in (self.totals, self.session_totals):
            totals["first_fills"] += 1
            totals["fees"] += fill.fee
        self._phase(tf, st, "waiting_second", now, position_id=position_id)
        payload = {
            "tf": tf, "slug": st["slug"], "window_ts": st["window"],
            "position_id": position_id, "side": side, "opposite": opposite,
            "size": fill.shares, "vwap": fill.vwap, "worst_price": fill.worst_price,
            "notional": fill.notional, "fee": fill.fee, "deadline_at": deadline,
            "local_vol": st["local_vol"].get(side), "model": self.regime,
            "btc": self.latest_btc,
        }
        self._event("first_filled", payload)
        self._log(
            f"FIRST {tf} {side.upper()} size={fill.shares:g} vwap={fill.vwap:.5f} "
            f"fee={fill.fee:.5f} deadline={deadline:.0f}"
        )

    def _complete_pair(self, tf: str, st: Dict[str, Any], second: DepthFill, now: float) -> None:
        position = st["position"]
        first = self._fill_from_dict(position.get("first"))
        if first is None:
            self._mark_manual_review(tf, st, now, "corrupt_first_fill")
            return
        pnl = pair_net_pnl(first, second)
        net_edge = pnl / first.shares
        position.update({
            "second": asdict(second), "second_at": now, "combined_vwap": first.vwap + second.vwap,
            "fees": _round5(first.fee + second.fee), "pnl": pnl, "net_edge": net_edge,
        })
        st["sequence_done"] = True
        st["second_preview"] = None
        for totals in (self.totals, self.session_totals):
            totals["completed"] += 1
            totals["closed"] += 1
            totals["realized"] += pnl
            totals["fees"] += second.fee
        self._phase(tf, st, "paired", now, position_id=position["position_id"])
        self._event("pair_completed", {
            "tf": tf, "slug": st["slug"], "window_ts": st["window"],
            "position_id": position["position_id"], "first_side": position["side"],
            "size": first.shares, "first_vwap": first.vwap, "second_vwap": second.vwap,
            "combined": first.vwap + second.vwap, "fees": position["fees"],
            "pnl": pnl, "net_edge": net_edge,
            "capital_deployed": first.notional + second.notional + first.fee + second.fee,
            "wait_seconds": round(now - position["first_at"], 3), "btc": self.latest_btc,
        })
        self._log(
            f"PAIRED {tf} combined={first.vwap + second.vwap:.5f} fees={position['fees']:.5f} "
            f"net={pnl:+.5f} wait={now - position['first_at']:.1f}s"
        )

    def _protective_exit(
        self, tf: str, st: Dict[str, Any], books: Dict[str, Any], now: float, reason: str,
    ) -> bool:
        position = st.get("position") or {}
        first = self._fill_from_dict(position.get("first"))
        side = position.get("side")
        exit_fill = self._fill_from_dict(((books.get(side) or {}).get("sell"))) if side else None
        if first is None:
            self._mark_manual_review(tf, st, now, "corrupt_first_fill")
            return False
        if exit_fill is None:
            if st.get("phase") != "exit_pending":
                self._phase(tf, st, "exit_pending", now, reason=reason)
            self._reject(tf, st, "protective_exit_depth", now, side=side, required_shares=first.shares)
            if now - float(position.get("last_exit_failed_at") or 0.0) >= 10.0:
                position["last_exit_failed_at"] = now
                self._event("exit_failed", {
                    "tf": tf, "slug": st["slug"], "position_id": position.get("position_id"),
                    "side": side, "reason": reason, "required_shares": first.shares,
                })
            return False
        pnl = protective_exit_pnl(first, exit_fill)
        position.update({"exit": asdict(exit_fill), "exit_at": now, "pnl": pnl, "exit_reason": reason})
        st["sequence_done"] = True
        for totals in (self.totals, self.session_totals):
            totals["forced_exits"] += 1
            totals["aborted"] += 1
            totals["closed"] += 1
            totals["realized"] += pnl
            totals["fees"] += exit_fill.fee
        self._phase(tf, st, "aborted", now, reason=reason, position_id=position.get("position_id"))
        self._event("protective_exit", {
            "tf": tf, "slug": st["slug"], "window_ts": st["window"],
            "position_id": position.get("position_id"), "side": side, "size": first.shares,
            "first_vwap": first.vwap, "exit_vwap": exit_fill.vwap,
            "fees": _round5(first.fee + exit_fill.fee), "pnl": pnl,
            "capital_deployed": first.notional + first.fee, "reason": reason,
            "unhedged_seconds": round(now - float(position.get("first_at") or now), 3),
            "btc": self.latest_btc,
        })
        self._log(
            f"PROTECTIVE EXIT {tf} {str(side).upper()} buy={first.vwap:.5f} "
            f"sell={exit_fill.vwap:.5f} net={pnl:+.5f} reason={reason}"
        )
        return True

    def _mark_manual_review(self, tf: str, st: Dict[str, Any], now: float, reason: str) -> None:
        if st.get("phase") == "manual_review":
            return
        for totals in (self.totals, self.session_totals):
            totals["manual_review"] += 1
        st["sequence_done"] = True
        unresolved = {
            "tf": tf, "slug": st.get("slug"), "window_ts": st.get("window"),
            "position": st.get("position"), "reason": reason, "marked_at": now,
        }
        position_id = (st.get("position") or {}).get("position_id")
        if not any((row.get("position") or {}).get("position_id") == position_id for row in self.unresolved_positions):
            self.unresolved_positions.append(unresolved)
        self._phase(tf, st, "manual_review", now, reason=reason)
        self._event("manual_review", unresolved)

    async def _roll_window(self, client: httpx.AsyncClient, tf: str, st: Dict[str, Any], now: float) -> None:
        if st.get("phase") in {"waiting_second", "exit_pending"} and st.get("tokens"):
            old_books = await self._fetch_pair(client, st["tokens"])
            if not self._protective_exit(tf, st, old_books, now, "window_rollover"):
                self._mark_manual_review(tf, st, now, "window_rollover_exit_unavailable")

    async def _handle_window(self, client: httpx.AsyncClient, tf: str, seconds: int, now: float) -> None:
        st = self.tf_state[tf]
        w = window_ts(now, seconds)
        if st["window"] != w:
            if st["window"] is not None:
                await self._roll_window(client, tf, st, now)
            st.clear()
            st.update(self._blank_tf_state())
            st.update({
                "window": w, "window_open_at": w, "slug": window_slug(ASSET, tf, w),
                "phase": "resolving_market",
            })
        elapsed = now - float(w)
        if st["tokens"] is None:
            if elapsed > self.entry_window_seconds:
                if st.get("phase") != "no_entry":
                    st["sequence_done"] = True
                    self._phase(tf, st, "no_entry", now, reason="market_unavailable_before_entry_cutoff")
                    self._event("sequence_aborted", {
                        "tf": tf, "slug": st["slug"], "window_ts": w,
                        "reason": "market_unavailable_before_entry_cutoff", "capital_deployed": 0.0,
                    })
                return
            if now < self._token_retry_at.get(tf, 0.0):
                return
            tokens = await self._resolve_tokens(client, st["slug"])
            if tokens is None:
                # Retry quickly inside the early entry window, then back off.
                self._token_retry_at[tf] = now + min(10.0, max(2.0, self.entry_window_seconds - elapsed))
                self._reject(tf, st, "market_tokens_unavailable", now)
                return
            self._token_retry_at.pop(tf, None)
            st["tokens"] = tokens
            self._phase(tf, st, "seeking_first", now)
            self._event("watch", {"tf": tf, "slug": st["slug"], "window_ts": w})
            self._log(f"WATCH {tf} {st['slug']}")

        books = await self._fetch_pair(client, st["tokens"])
        st["books"] = books
        yes, no = (books.get("yes") or {}), (books.get("no") or {})
        st["yes"], st["no"] = yes.get("best_ask"), no.get("best_ask")
        st["combined"] = (
            st["yes"] + st["no"] if isinstance(st["yes"], (int, float)) and isinstance(st["no"], (int, float)) else None
        )
        if st["combined"] is not None and (
            st["best_combined"] is None or st["combined"] < st["best_combined"]
        ):
            st["best_combined"] = st["combined"]
        self._record_quotes(st, books, now)

        if st["phase"] in {"waiting_second", "exit_pending"}:
            position = st.get("position") or {}
            deadline = min(
                float(position.get("deadline_at") or now),
                float(w + seconds - self.force_exit_before_close_seconds),
            )
            if now >= deadline or st["phase"] == "exit_pending":
                self._protective_exit(tf, st, books, now, "hedge_timeout")
                return
            first = self._fill_from_dict(position.get("first"))
            opposite = position.get("opposite")
            second = self._fill_from_dict(((books.get(opposite) or {}).get("buy"))) if opposite else None
            if first is None:
                self._mark_manual_review(tf, st, now, "corrupt_first_fill")
                return
            if second is None:
                self._reject(tf, st, "second_leg_depth", now, opposite=opposite, required_shares=first.shares)
                return
            pnl = pair_net_pnl(first, second)
            edge = pnl / first.shares
            st["second_preview"] = {
                "side": opposite, "vwap": second.vwap, "fee": second.fee,
                "net_pnl": pnl, "net_edge": edge, "required_net_edge": self.min_edge,
                "deadline_at": deadline,
            }
            if edge >= self.min_edge:
                self._event("second_candidate", {
                    "tf": tf, "slug": st["slug"], "position_id": position.get("position_id"),
                    **st["second_preview"],
                })
                self._complete_pair(tf, st, second, now)
            else:
                self._reject(
                    tf, st, "second_leg_net_edge", now, net_edge=round(edge, 6),
                    required=round(self.min_edge, 6), second_vwap=second.vwap,
                )
            return

        if st["phase"] != "seeking_first":
            return
        if elapsed < self.no_entry_seconds:
            self._reject(tf, st, "entry_not_open", now, opens_in_s=round(self.no_entry_seconds - elapsed, 2))
            return
        if elapsed > self.entry_window_seconds:
            st["sequence_done"] = True
            self._phase(tf, st, "no_entry", now, reason="entry_window_expired")
            self._event("sequence_aborted", {
                "tf": tf, "slug": st["slug"], "window_ts": w,
                "reason": "entry_window_expired", "capital_deployed": 0.0,
            })
            return
        threshold = float(self.regime["adjusted_local_vol_threshold"])
        candidates: List[Tuple[str, DepthFill]] = []
        for side in ("yes", "no"):
            fill = self._fill_from_dict(((books.get(side) or {}).get("buy")))
            if fill is None:
                continue
            if self.first_price_min <= fill.vwap <= self.first_price_max:
                candidates.append((side, fill))
        if not candidates:
            reason = "first_leg_depth" if any((books.get(side) or {}).get("best_ask") is not None for side in ("yes", "no")) else "book_unavailable"
            self._reject(
                tf, st, reason if reason == "book_unavailable" else "first_price_band_or_depth", now,
                first_price_min=self.first_price_min, first_price_max=self.first_price_max,
                yes=st["yes"], no=st["no"], required_shares=self.order_size,
            )
            return
        candidates.sort(key=lambda item: (item[1].vwap, item[0]))
        side, fill = candidates[0]
        observed_vol = float(st["local_vol"].get(side) or 0.0)
        if observed_vol < threshold:
            self._reject(
                tf, st, "local_volatility", now, side=side, observed=observed_vol,
                required=threshold, model_status=self.regime.get("status"),
            )
            return
        self._event("first_candidate", {
            "tf": tf, "slug": st["slug"], "window_ts": w, "side": side,
            "size": fill.shares, "vwap": fill.vwap, "fee": fill.fee,
            "local_vol": observed_vol, "required_local_vol": threshold,
            "model": self.regime,
        })
        self._first_leg(tf, st, side, fill, now)

    # ------------------------------------------------------------- config/state

    def _reload_config(self) -> bool:
        try:
            if not CONFIG_PATH.is_file():
                return False
            mtime = CONFIG_PATH.stat().st_mtime
            if self._config_mtime is not None and mtime <= self._config_mtime:
                return False
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if raw.get("strategy_schema") != SCHEMA_VERSION:
                # CLI values are already the controller's migrated v2 values.
                # Never let an old gross-edge config overwrite them. Record
                # this mtime so the same rejected file is not re-read forever;
                # a later atomic v2 write has a newer mtime and will apply.
                self._config_mtime = mtime
                self._write_meta_event("config_reload_rejected", {
                    "reason": "strategy_schema_mismatch",
                    "found": raw.get("strategy_schema"), "required": SCHEMA_VERSION,
                })
                return False
            self._config_mtime = mtime
        except (OSError, json.JSONDecodeError):
            return False
        changed: Dict[str, float] = {}
        for key, (_, attr) in self._NUMERIC_CONFIG.items():
            value = float(raw.get(key, getattr(self, attr)))
            if value != getattr(self, attr):
                setattr(self, attr, value)
                changed[key] = value
        if changed:
            self._write_meta_event("config_reload", {**changed, "mode": raw.get("mode")})
            self._log(f"CONFIG HOT-RELOAD {json.dumps(changed, sort_keys=True)} mode={raw.get('mode')}")
            self._refresh_regime(time.time(), force=True)
        return True

    def _config_payload(self) -> Dict[str, Any]:
        payload = {key: getattr(self, attr) for key, (_, attr) in self._NUMERIC_CONFIG.items()}
        payload["timeframes"] = self.timeframes
        payload["strategy_schema"] = SCHEMA_VERSION
        return payload

    def _write_state(self, running: bool) -> None:
        open_positions: Dict[str, DepthFill] = {}
        now = time.time()
        for st in self.tf_state.values():
            if st.get("phase") in {"waiting_second", "exit_pending", "manual_review"}:
                position = st.get("position") or {}
                first = self._fill_from_dict(position.get("first"))
                if first:
                    key = str(position.get("position_id") or f"current-{id(st)}")
                    open_positions[key] = first
        for unresolved in self.unresolved_positions:
            position = unresolved.get("position") or {}
            first = self._fill_from_dict(position.get("first"))
            if first:
                key = str(position.get("position_id") or f"unresolved-{id(unresolved)}")
                open_positions[key] = first
        open_exposure = sum(first.notional + first.fee for first in open_positions.values())
        unhedged = len(open_positions)
        strategy_totals = {
            "realized_pnl": _round5(float(self.totals["realized"])),
            "fees": _round5(float(self.totals["fees"])),
            **{k: int(v) for k, v in self.totals.items() if k not in {"realized", "fees"}},
            "open_exposure": _round5(open_exposure), "unhedged_positions": unhedged,
        }
        session_totals = {
            "realized_pnl": _round5(float(self.session_totals["realized"])),
            "fees": _round5(float(self.session_totals["fees"])),
            **{k: int(v) for k, v in self.session_totals.items() if k not in {"realized", "fees"}},
            "open_exposure": _round5(open_exposure), "unhedged_positions": unhedged,
        }
        payload = {
            "strategy_schema": SCHEMA_VERSION, "session_id": self.session_id,
            "legacy_metrics_included": False, "running": running,
            "started_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.started_at)),
            "uptime_s": round(now - self.started_at, 1), "config": self._config_payload(),
            "strategy_totals": strategy_totals, "session_totals": session_totals,
            # Compatibility keys point only at sequential_v2 results.
            "totals": {
                "realized_pnl": strategy_totals["realized_pnl"],
                "open_locked_pnl": 0.0, "entries": strategy_totals["first_fills"],
                "exits": strategy_totals["closed"], "fees": strategy_totals["fees"],
                "open_exposure": strategy_totals["open_exposure"],
            },
            "rejections": self.rejections, "regime": self.regime,
            "unresolved_positions": self.unresolved_positions,
            "paper_only": True, "real_orders_possible": False,
            "timeframes": self.tf_state,
        }
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            tmp = STATE_FILE.with_name(f".{STATE_FILE.name}.tmp")
            tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            os.replace(tmp, STATE_FILE)
        except OSError:
            pass

    # ------------------------------------------------------------- run

    async def _unwind_open_positions(self, client: httpx.AsyncClient, now: float) -> None:
        for tf, st in self.tf_state.items():
            if st.get("phase") not in {"waiting_second", "exit_pending"}:
                continue
            if not st.get("tokens"):
                self._mark_manual_review(tf, st, now, "shutdown_without_tokens")
                continue
            books = await self._fetch_pair(client, st["tokens"])
            if not self._protective_exit(tf, st, books, now, "shutdown"):
                self._mark_manual_review(tf, st, now, "shutdown_exit_unavailable")

    async def run(self) -> None:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        stop_signal = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop_signal.set)
            except (NotImplementedError, ValueError):
                pass
        async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, timeout=10.0) as client:
            while not self._stop and not stop_signal.is_set():
                try:
                    now = time.time()
                    self._reload_config()
                    self._refresh_regime(now)
                    self._maybe_refresh_regime_forecast(now)
                    btc_task = asyncio.create_task(self._btc_price(client))
                    window_tasks = [
                        asyncio.create_task(self._handle_window(client, tf, TIMEFRAMES[tf], now))
                        for tf in self.timeframes
                    ]
                    await asyncio.gather(btc_task, *window_tasks)
                    btc = btc_task.result()
                    if btc is not None:
                        self.latest_btc = btc
                        self._event("btc", {"price": btc})
                    self._write_state(True)
                except Exception as exc:  # noqa: BLE001
                    self._event("error", {"message": f"{type(exc).__name__}: {exc}"})
                if STOP_FILE.exists():
                    self._log("STOP file detected — shutting down")
                    break
                try:
                    await asyncio.wait_for(stop_signal.wait(), timeout=POLL_S)
                except asyncio.TimeoutError:
                    pass
            await self._unwind_open_positions(client, time.time())
            if self._regime_forecast_task is not None and not self._regime_forecast_task.done():
                self._regime_forecast_task.cancel()
                try:
                    await self._regime_forecast_task
                except asyncio.CancelledError:
                    pass
        self._write_state(False)
        self._write_meta_event("stop", {"reason": "stop file or signal"})
        self._log("loop bot stopped")


def main() -> int:
    parser = argparse.ArgumentParser(description="Isolated sequential_v2 paper loop bot (5m/15m/30m)")
    parser.add_argument("--order-size", type=float, default=5.0)
    parser.add_argument("--min-edge", type=float, default=0.003, help="minimum fee-aware net edge per share")
    parser.add_argument("--no-entry-seconds", type=float, default=0.5, help="delay after window open")
    parser.add_argument("--entry-window-seconds", type=float, default=60.0)
    parser.add_argument("--first-price-min", type=float, default=0.45)
    parser.add_argument("--first-price-max", type=float, default=0.49)
    parser.add_argument("--hedge-timeout-seconds", type=float, default=45.0)
    parser.add_argument("--force-exit-before-close-seconds", type=float, default=10.0)
    parser.add_argument("--local-vol-lookback-seconds", type=float, default=30.0)
    parser.add_argument("--local-vol-threshold", type=float, default=0.01)
    parser.add_argument("--regime-refresh-seconds", type=float, default=60.0)
    parser.add_argument("--regime-forecast-interval-seconds", type=float, default=600.0)
    parser.add_argument("--regime-max-age-seconds", type=float, default=900.0)
    parser.add_argument("--regime-min-confidence", type=float, default=0.45)
    parser.add_argument("--timeframes", default="5m,15m,30m")
    parser.add_argument(
        "--finnhub-key", default=os.environ.get(FINNHUB_KEY_ENV, ""),
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args()
    finnhub_key = args.finnhub_key or None
    # The narrow environment variable is only a bootstrap transport from the
    # dashboard. Keep the credential in memory after parsing and remove it
    # from this process environment so it is not inherited by model-refresh
    # subprocesses.
    os.environ.pop(FINNHUB_KEY_ENV, None)
    config = {
        "order_size": args.order_size,
        "min_edge": args.min_edge,
        "no_entry_seconds": args.no_entry_seconds,
        "entry_window_seconds": args.entry_window_seconds,
        "first_price_min": args.first_price_min,
        "first_price_max": args.first_price_max,
        "hedge_timeout_seconds": args.hedge_timeout_seconds,
        "force_exit_before_close_seconds": args.force_exit_before_close_seconds,
        "local_vol_lookback_seconds": args.local_vol_lookback_seconds,
        "local_vol_threshold": args.local_vol_threshold,
        "regime_refresh_seconds": args.regime_refresh_seconds,
        "regime_forecast_interval_seconds": args.regime_forecast_interval_seconds,
        "regime_max_age_seconds": args.regime_max_age_seconds,
        "regime_min_confidence": args.regime_min_confidence,
        "timeframes": [tf.strip() for tf in args.timeframes.split(",") if tf.strip()],
    }
    bot = LoopBot(config, finnhub_key=finnhub_key)
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
