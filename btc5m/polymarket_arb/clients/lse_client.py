"""London Strategic Edge market data client — COLD PATH + background.

Provides:
  * WebSocket redundant price feed for BTC/USD (secondary validation alongside CLOB)
  * REST OHLCV candles for Kronos cold path input
  * Bulk tick export for backtest_engine.py
  * Macro series (US10Y, bond yields) for opportunity context
  * Options chain for IV spike detection

All endpoints use the LSE API key from config. WebSocket is non-blocking and
runs in a background task — never in the trading loop.

Usage:
    from polymarket_arb.clients.lse_client import LSEClient
    client = LSEClient(api_key=SETTINGS.lse_api_key)

    # REST candles for Kronos
    candles = await client.fetch_candles("BTC/USD", timeframe="1m", start="2026-06-01")

    # WebSocket secondary feed
    await client.start_ws(["BTC/USD"], on_tick)

    # Bulk tick export for backtesting
    job_id = await client.start_tick_export("BTC/USD")
    data = await client.poll_export_result(job_id)
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Dict, List, Optional, Callable

import httpx

from ..config import SETTINGS
from ..logger import log

# ---------------------------------------------------------------------------
# LSE (London Strategic Edge) endpoints
#
# The WebSocket host was previously "wss://stream.londonstrategicedge.com/v1"
# but that hostname does NOT resolve (NXDOMAIN).  The correct, live endpoint
# published by LSE's production frontend is:
#
#     wss://ws.londonstrategicedge.com           (no path; root)
#
# The frontend (bundled in the SPA at londonstrategicedge.com) connects raw
# to that host over the bare `/`-style URL and authenticates by sending an
# `action: "auth"` message carrying `api_key` inside the JSON payload — NOT via
# an `x-api-key` HTTP header.  See the message protocol below.
#
# LSE API base URL
LSE_BASE_URL = "https://api.londonstrategicedge.com"
LSE_VAULT_URL = f"{LSE_BASE_URL}/vault"

# WebSocket URL for real-time feeds (was: wss://stream.londonstrategicedge.com/v1 — dead DNS)
LSE_WS_URL = "wss://ws.londonstrategicedge.com"

# Placeholder LSE API key baked into config.py (line 101).  This value is a
# sentinel that is *not* a real credential.  The client detects it at runtime
# and logs a loud warning so the failure mode is obvious instead of a silent
# auth-reject.  Replace the env var LSE_API_KEY (or SETTINGS.lse_api_key) with
# a real LSE key to silence the warning.
_LSE_PLACEHOLDER_KEY = "lse_live_00000000000000000000000000000000"

# Default timeout for REST requests
_REST_TIMEOUT = 30.0


class LSEClient:
    """London Strategic Edge market data client.

    All methods are safe to call from COLD PATH or background tasks.
    WebSocket runs as a background asyncio task with auto-reconnect.
    """

    def __init__(self, api_key: str = None, base_url: str = LSE_VAULT_URL):
        self.api_key = api_key or SETTINGS.lse_api_key
        self.base_url = base_url
        self._session: Optional[httpx.AsyncClient] = None
        self._ws_task: Optional[asyncio.Task] = None
        self._ws: Any = None  # websockets connection
        self._ws_callback: Optional[Callable] = None
        self._ws_symbols: List[str] = []
        self._stop = False
        self._healthy = False

        # --- Fix #3: detect placeholder API key at construction time ---
        if self.api_key == _LSE_PLACEHOLDER_KEY:
            log.warning(
                "LSEClient created with the placeholder LSE_API_KEY "
                f"({_LSE_PLACEHOLDER_KEY}).  This is NOT a real credential.  "
                "Replace it with a valid LSE API key via the LSE_API_KEY "
                "environment variable before relying on the data feed."
            )

    @property
    def healthy(self) -> bool:
        """True if the WebSocket connection is active."""
        return self._healthy

    async def _get_session(self) -> httpx.AsyncClient:
        """Lazy-init a shared httpx session with auth headers."""
        if self._session is None:
            self._session = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=_REST_TIMEOUT,
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": self.api_key,
                },
            )
        return self._session

    async def aclose(self):
        """Close all connections."""
        self._stop = True
        if self._ws_task is not None:
            self._ws_task.cancel()
            self._ws_task = None
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        if self._session is not None:
            await self._session.aclose()
            self._session = None

    # ---- REST: OHLCV Candles ----
    async def fetch_candles(
        self,
        symbol: str = "BTC/USD",
        timeframe: str = "1m",
        start: str = None,
        end: str = None,
        limit: int = 10000,
    ) -> List[Dict[str, Any]]:
        """Fetch OHLCV candles for a symbol.

        Used by Kronos cold path for BTC volatility regime forecasting.
        Returns a list of {timestamp, open, high, low, close, volume} dicts.
        """
        session = await self._get_session()
        params = {
            "symbol": symbol,
            "timeframe": timeframe,
            "limit": str(limit),
        }
        if start:
            params["start"] = start
        if end:
            params["end"] = end

        try:
            resp = await session.get("/candles", params=params)
            resp.raise_for_status()
            data = resp.json()
            # Normalize: LSE returns candles as list of dicts
            if isinstance(data, dict):
                data = data.get("data", [])
            return data if isinstance(data, list) else []
        except Exception as e:
            log.error(f"LSE fetch_candles {symbol} failed: {e!r}")
            return []

    # ---- REST: Macro Series ----
    async def fetch_series(
        self,
        symbol: str = "US10Y",
        start: str = "2000-01-01",
        end: str = None,
        limit: int = 10000,
    ) -> List[Dict[str, Any]]:
        """Fetch macro economic series (bond yields, etc.).

        Used for opportunity context — high macro risk windows tighten NO_ENTRY_SECONDS.
        """
        session = await self._get_session()
        params = {
            "symbol": symbol,
            "start": start,
            "limit": str(limit),
        }
        if end:
            params["end"] = end

        try:
            resp = await session.get("/series", params=params)
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict):
                data = data.get("data", [])
            return data if isinstance(data, list) else []
        except Exception as e:
            log.error(f"LSE fetch_series {symbol} failed: {e!r}")
            return []

    # ---- REST: Options Chain ----
    async def fetch_options_chain(
        self,
        underlying: str = "BTC",
        option_type: str = "call",
        max_dte: int = 30,
    ) -> List[Dict[str, Any]]:
        """Fetch options chain for IV spike detection.

        Leading indicator for arb window widening — high IV often precedes
        periods where YES+NO spreads widen, creating more arb opportunities.
        """
        session = await self._get_session()
        params = {
            "underlying": underlying,
            "type": option_type,
            "max_dte": str(max_dte),
        }

        try:
            resp = await session.get("/options/chain", params=params)
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict):
                data = data.get("data", [])
            return data if isinstance(data, list) else []
        except Exception as e:
            log.error(f"LSE fetch_options_chain {underlying} failed: {e!r}")
            return []

    # ---- REST: Bulk Tick Export (for backtesting) ----
    async def start_tick_export(
        self,
        symbol: str = "BTC/USD",
        timeframe: str = "tick",
        start: str = None,
        end: str = None,
    ) -> Optional[str]:
        """Start a bulk tick export job (server-side build).

        Returns a job_id to poll with poll_export_result().
        Used by backtest_engine.py to get historical tick data.
        """
        session = await self._get_session()
        body = {
            "dataset": "crypto",
            "symbol": symbol,
            "timeframe": timeframe,
        }
        if start:
            body["start"] = start
        if end:
            body["end"] = end

        try:
            resp = await session.post("/export", json=body)
            resp.raise_for_status()
            data = resp.json()
            return data.get("job_id") or data.get("jobId")
        except Exception as e:
            log.error(f"LSE start_tick_export {symbol} failed: {e!r}")
            return None

    async def poll_export_result(
        self,
        job_id: str,
        max_polls: int = 120,
        poll_interval: float = 5.0,
    ) -> Optional[List[Dict[str, Any]]]:
        """Poll an export job until complete or timeout.

        Returns the tick data list, or None if the job failed/timed out.
        """
        session = await self._get_session()

        for i in range(max_polls):
            try:
                resp = await session.get(f"/export/{job_id}")
                resp.raise_for_status()
                data = resp.json()

                status = data.get("status", "").lower()
                if status == "completed":
                    # Result may be inline or a download URL
                    result = data.get("result") or data.get("data")
                    if result:
                        return result if isinstance(result, list) else []
                    # If there's a download URL, fetch it
                    download_url = data.get("download_url")
                    if download_url:
                        return await self._download_export(download_url)
                    return []
                elif status in ("failed", "error"):
                    log.error(f"LSE export job {job_id} failed")
                    return None
                # status is "pending" or "processing"
            except Exception as e:
                log.debug(f"LSE export poll {job_id} attempt {i+1}: {e!r}")

            await asyncio.sleep(poll_interval)

        log.error(f"LSE export job {job_id} timed out after {max_polls} polls")
        return None

    async def _download_export(self, url: str) -> List[Dict[str, Any]]:
        """Download export result from a presigned URL."""
        try:
            resp = await httpx.get(url, timeout=60.0)
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, list) else []
        except Exception as e:
            log.error(f"LSE export download failed: {e!r}")
            return []

    # ---- WebSocket: Real-time feed ----
    async def start_ws(
        self,
        symbols: List[str],
        on_tick: Callable[[str, Dict[str, Any]], None],
    ):
        """Start the WebSocket feed for real-time price updates.

        on_tick(symbol, data) is called for each tick.
        Runs in a background task with auto-reconnect.
        """
        self._ws_symbols = symbols
        self._ws_callback = on_tick
        self._stop = False

        if self._ws_task is None:
            self._ws_task = asyncio.create_task(self._ws_loop(), name="lse-ws")
            self._ws_task.add_done_callback(self._log_task_error)

    @staticmethod
    def _log_task_error(task):
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.error(f"LSE WS task failed: {exc!r}")

    async def _ws_loop(self):
        """WebSocket connection loop with auto-reconnect.

        LSE WS protocol (reverse-engineered from the official frontend bundle):
          * No HTTP auth header — authenticate by sending {"action":"auth","api_key":"..."}
            as the first message after connect.
          * Subscribe per-symbol: {"action":"subscribe","symbol":"BTC/USD"}
          * Unsubscribe per-symbol: {"action":"unsubscribe","symbol":"BTC/USD"}
          * Heartbeat: {"action":"ping"} (server responds with {"type":"heartbeat"})
          * Server messages use "type" at the top level:
              {"type":"welcome","max_symbols":16}
              {"type":"subscribed","symbol":"BTC/USD","count":1}
              {"type":"tick","symbol":"BTC/USD","price":...,"bid":...,"ask":...,"ts":...}
              {"type":"auth","status":"ok","l3_access":false}
              {"type":"heartbeat"}
              {"type":"error","code":"INVALID_SYMBOL",...}
        """
        import websockets

        backoff = 1.0
        max_backoff = 30.0

        while not self._stop:
            try:
                # --- Fix #2: websockets >= 13 renamed `extra_headers` to
                # `additional_headers`.  We now send auth inside the WS payload
                # (the LSE protocol is designed that way), so we no longer need
                # a handshake header at all.  `additional_headers` is kept as an
                # empty dict for forward-compat / explicitness.
                async with websockets.connect(
                    LSE_WS_URL,
                    additional_headers={},
                    ping_interval=30,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    self._ws = ws
                    self._healthy = True
                    log.info(f"LSE WS connected to {self._ws_symbols}")

                    backoff = 1.0  # reset on successful connect

                    # --- Authenticate (if a key is configured) ---
                    if self.api_key and self.api_key != _LSE_PLACEHOLDER_KEY:
                        auth_msg = json.dumps({
                            "action": "auth",
                            "api_key": self.api_key,
                        })
                        await ws.send(auth_msg)
                        log.debug("LSE WS auth sent")
                    elif self.api_key == _LSE_PLACEHOLDER_KEY:
                        # --- Fix #3: loud warning for placeholder key ---
                        log.warning(
                            "LSE_API_KEY is still the placeholder "
                            f"({_LSE_PLACEHOLDER_KEY}).  "
                            "Replace it with a real LSE API key in your environment "
                            "or config.py to receive authenticated ticks."
                        )

                    # --- Subscribe to each symbol individually ---
                    for sym in self._ws_symbols:
                        sub_msg = json.dumps({
                            "action": "subscribe",
                            "symbol": sym,
                        })
                        await ws.send(sub_msg)

                    async for raw in ws:
                        if self._stop:
                            break
                        await self._handle_ws_message(raw)

                    self._healthy = False

            except Exception as e:
                self._healthy = False
                if self._stop:
                    break
                log.warning(f"LSE WS disconnected: {e!r}; reconnecting in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.5, max_backoff)

    async def _handle_ws_message(self, raw):
        """Parse and dispatch a WebSocket message.

        LSE WS message format (reverse-engineered from official frontend):
            {"type":"welcome","max_symbols":16}
            {"type":"subscribed","symbol":"BTC/USD","count":1}
            {"type":"tick","symbol":"BTC/USD","price":...,"bid":...,"ask":...,"ts":...}
            {"type":"auth","status":"ok","l3_access":false}
            {"type":"heartbeat"}
            {"type":"error","code":"INVALID_SYMBOL",...}
        """
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", "replace")

        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            return

        msg_type = msg.get("type", "")

        # Only "tick" messages carry price data for the callback.
        if msg_type == "tick":
            symbol = msg.get("symbol")
            if symbol and self._ws_callback:
                # Pass the full tick payload so downstream code has access to
                # all fields (price, bid, ask, ts, etc.).
                self._ws_callback(symbol, msg)

        # Non-tick messages (welcome, subscribed, auth, heartbeat, error) are
        # informational — log noteworthy ones and otherwise ignore.

    # ---- Convenience: fetch recent BTC candles for Kronos ----
    async def fetch_btc_candles_for_kronos(
        self,
        days: int = 30,
        timeframe: str = "1m",
    ) -> List[Dict[str, Any]]:
        """Fetch recent BTC/USD candles for Kronos volatility regime forecasting.

        This is the COLD PATH entry point called once at startup.
        Returns candles with {ts, open, high, low, close, volume} — the raw
        LSE response key is "ts", not "timestamp" (confirmed against a live
        fetch); callers needing the timestamp must check "ts" first, same as
        normalize_lse_candles() in scripts/train_kronos_comprehensive.py.
        """
        from datetime import datetime, timedelta, timezone

        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)

        return await self.fetch_candles(
            symbol="BTC/USD",
            timeframe=timeframe,
            start=start.strftime("%Y-%m-%d"),
            end=end.strftime("%Y-%m-%d"),
        )

    # ---- Convenience: check for high macro risk ----
    async def check_macro_risk(self) -> bool:
        """Check if current macro conditions indicate high risk.

        Fetches recent US10Y yield changes and returns True if there's
        significant movement (indicating potential market volatility).
        """
        series = await self.fetch_series("US10Y", start="2026-01-01", limit=100)
        if len(series) < 2:
            return False

        # Check for significant yield movement in recent data
        recent = series[-10:] if len(series) >= 10 else series
        if len(recent) < 2:
            return False

        yields = [float(r.get("value", 0)) for r in recent if r.get("value") is not None]
        if len(yields) < 2:
            return False

        # High risk if yield moved more than 20bps in the recent window
        max_move = max(yields) - min(yields)
        return max_move > 0.20  # 20 basis points
