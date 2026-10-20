"""CLOB book client — HOT PATH (price feed transport).

Two entry points, both latency-critical:
  * ClobBookFeed  — WebSocket subscription for best-ask updates (preferred).
  * fetch_book    — single REST orderbook fetch (used to seed + as WS fallback).

Verified against Polymarket CLOB market websocket examples:
  * WS host path is `<host>/ws/market` (NOT `/ws`).
  * Subscription: {"type":"market","markets":[],"assets_ids":[tok1,tok2],"initial_dump":true}
  * Server replies with a JSON ARRAY of book snapshots, each:
      {"market":"0x..","asset_id":"105267..","asks":[{"price":"0.5","size":".."}, ...], ...}
  * Server echoes "PONG" to our "PING" keepalive — must be skipped (not JSON).

REST /book returns asks as a list of {"price": "0.999", "size": "..."} dicts (strings).
Parsing is deliberately tiny: scan asks -> min ask. No object churn beyond the floats.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Callable, List, Optional, Tuple

import httpx
import websockets

from ..config import SETTINGS
from ..logger import log

# on_tick(token_id, best_ask|None, best_ask_size|None, best_bid|None)
TickCallback = Callable[
    [str, Optional[float], Optional[float], Optional[float]], None
]


def _price_size(pair) -> Tuple[Optional[float], Optional[float]]:
    """Normalize one ask/bid entry to (price, size) floats.

    REST /book returns asks as [{"price":"0.999","size":"..."}] (strings). The WS
    snapshot reuses the same shape. Handle dicts; tuple arrays tolerated too.
    """
    try:
        if isinstance(pair, dict):
            return float(pair["price"]), float(pair["size"])
        if isinstance(pair, (list, tuple)) and len(pair) >= 2:
            return float(pair[0]), float(pair[1])
    except (TypeError, ValueError, KeyError, IndexError):
        return None, None
    return None, None


def _best_ask_from_book(book: dict) -> Tuple[Optional[float], Optional[float]]:
    """COLD/HOT shared: asks -> (best_ask, best_size)."""
    asks = (book or {}).get("asks") or []
    best: Optional[float] = None
    best_sz: Optional[float] = None
    for pair in asks:
        p, sz = _price_size(pair)
        if p is None:
            continue
        if best is None or p < best:
            best = p
            best_sz = sz
    return best, best_sz


def _best_bid_from_book(book: dict) -> Tuple[Optional[float], Optional[float]]:
    """COLD/HOT shared: bids -> (best_bid, best_size). Used by the market-exit path."""
    bids = (book or {}).get("bids") or []
    best: Optional[float] = None
    best_sz: Optional[float] = None
    for pair in bids:
        p, sz = _price_size(pair)
        if p is None:
            continue
        if best is None or p > best:
            best = p
            best_sz = sz
    return best, best_sz


async def fetch_book_top(
    session: httpx.AsyncClient, token_id: str,
) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """Single REST book read returning ``(ask, ask_size, bid)``.

    The bid matters even in fallback mode: a sequential first leg must still be
    flattenable on its wall-clock deadline when WebSocket transport is unavailable.
    """
    try:
        r = await session.get("/book", params={"token_id": token_id}, timeout=2.0)
        r.raise_for_status()
        book = r.json()
        ask, size = _best_ask_from_book(book)
        bid, _ = _best_bid_from_book(book)
        return ask, size, bid
    except Exception as e:
        log.debug(f"fetch_book {token_id[:10]} failed: {e!r}")
        return None, None, None


async def fetch_book(
    session: httpx.AsyncClient, token_id: str,
) -> Tuple[Optional[float], Optional[float]]:
    """Compatibility wrapper returning the historical ``(ask, ask_size)`` pair."""
    ask, size, _ = await fetch_book_top(session, token_id)
    return ask, size


class ClobBookFeed:
    """WebSocket orderbook feed. Subscribes to tracked tokens via the CLOB
    `wss://<host>/ws/market` channel, then streams best-ask updates to `on_tick`.
    Auto-reconnects. Exposes `healthy` so PriceFeed can fall back to REST polling
    while the socket is down.
    """

    def __init__(self, token_ids: List[str], on_tick: TickCallback):
        # SETTINGS.clob_ws_url is the base host (e.g. wss://ws-subscriptions-clob.polymarket.com);
        # the real path is /ws/market.
        self.ws_url = f"{SETTINGS.clob_ws_url.rstrip('/')}/ws/market"
        self.token_ids = list(token_ids)
        self.on_tick = on_tick
        self._stop = False
        self.healthy = False
        self._ws = None
        # Paper-session transport fault injection. This is intentionally an explicit
        # method (rather than a hidden env var) so a harness can prove fallback and
        # recovery using the same WebSocket instance the bot is actually running.
        self._forced_disconnect_until = 0.0

    def stop(self):
        self._stop = True

    async def force_paper_disconnect(self, duration_s: float) -> None:
        """Close the live socket and suspend reconnecting for a paper-only interval.

        This exercises the real transport and PriceFeed's REST fallback without
        changing endpoint configuration or sending any orders. It is unavailable
        outside ``PAPER_TRADE`` mode by design.
        """
        if not SETTINGS.paper_trade:
            raise RuntimeError("transport fault injection is PAPER_TRADE-only")
        if duration_s <= 0:
            raise ValueError("duration_s must be positive")

        deadline = time.monotonic() + duration_s
        self._forced_disconnect_until = max(self._forced_disconnect_until, deadline)
        self.healthy = False
        log.warning(
            f"WS transport fault injection started: closing live socket and "
            f"holding reconnect for {duration_s:.1f}s"
        )
        ws = getattr(self, "_ws", None)
        if ws is not None:
            try:
                await ws.close(code=1012, reason="paper transport fault injection")
            except Exception as e:
                log.warning(f"WS transport fault close failed: {e!r}")

    async def resubscribe(self, token_ids: List[str]):
        """Point the running feed at a new token set (a newly-promoted ACTIVE market's ids).

        Update the desired id set unconditionally so an unhealthy feed reconnects with
        the current market set. If a socket exists, send the updated subscription
        immediately. A failed send means that socket is not healthy: close and detach
        it, then re-raise so PriceFeed can visibly enter REST fallback.
        """
        self.token_ids = list(token_ids)
        ws = self._ws
        if ws is not None and not self._stop:
            try:
                # Await so the unsubscribe frame flushes before the next subscribe;
                # otherwise the gateway may reply with the old-market book.
                await ws.send(json.dumps({
                    "type": "market",
                    "markets": [],
                    "assets_ids": self.token_ids,
                    "initial_dump": True,
                }))
            except Exception as e:
                self.healthy = False
                if self._ws is ws:
                    self._ws = None
                log.error(
                    f"WS resubscribe send failed for {len(self.token_ids)} tokens; "
                    f"socket marked unhealthy and REST fallback required: {e!r}"
                )
                try:
                    await ws.close(code=1011, reason="resubscribe send failed")
                except Exception as close_error:
                    log.warning(
                        f"failed to close socket after resubscribe error: {close_error!r}"
                    )
                raise

    def _handle(self, raw):
        # HOT PATH parse: WS sends a JSON array of book snapshots (one per asset).
        # Parse minimally and route each to on_tick. No extra object churn.
        # The server also echoes "PONG" to our keepalive ping — skip non-JSON frames.
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", "replace")
        raw = raw.strip()
        if not raw or raw.upper() == "PONG":
            return
        try:
            msg = json.loads(raw)
        except ValueError as e:
            log.debug(f"_handle json parse failed (non-JSON frame discarded): {e!r}")
            return
        books = msg if isinstance(msg, list) else [msg]
        for book in books:
            if not isinstance(book, dict):
                continue
            token = book.get("asset_id") or book.get("market")
            if token is None:
                continue
            best, sz = _best_ask_from_book(book)
            if best is None:
                continue
            # Also surface the best BID so the execution path can exit a filled leg at
            # market when it must unwind a partial fill.
            bid, _ = _best_bid_from_book(book)
            self.on_tick(token, best, sz, bid)

    async def _subscribe(self, ws):
        # VERIFIED schema from Polymarket CLOB market websocket examples.
        await ws.send(json.dumps({
            "type": "market",
            "markets": [],
            "assets_ids": self.token_ids,
            "initial_dump": True,
        }))
        await ws.send("PING")  # SDK keeps alive every ~50s

    async def run(self):
        """Run forever (until stop()). Explicit connect loop so handshake errors are
        caught cleanly and `healthy` is toggled for the REST-fallback watchdog.

        If the socket can't be established after a few tries, we give up quietly and
        let the REST poll fallback (PriceFeed) own the hot path — no perpetual spam.
        """
        backoff = 0.5
        attempts = 0
        max_attempts = 3
        if not self.token_ids:
            log.warning("ClobBookFeed: no token ids to subscribe")
            return
        while not self._stop and attempts < max_attempts:
            remaining_pause = self._forced_disconnect_until - time.monotonic()
            if remaining_pause > 0:
                self.healthy = False
                await asyncio.sleep(min(remaining_pause, 0.25))
                continue
            if self._forced_disconnect_until:
                self._forced_disconnect_until = 0.0
                log.info("WS transport fault injection ended; reconnect permitted")
            try:
                async with websockets.connect(
                    self.ws_url, ping_interval=10, ping_timeout=10,
                    close_timeout=1, max_queue=1024,
                ) as ws:
                    self._ws = ws  # exposed for live resubscribe (market_universe.py promotions)
                    try:
                        await self._subscribe(ws)
                        self.healthy = True
                        log.info(f"WS subscribed to {len(self.token_ids)} tokens")
                        backoff = 0.5
                        attempts = 0
                        async for raw in ws:
                            if self._stop:
                                break
                            self._handle(raw)
                        if self._stop:
                            break
                        self.healthy = False  # socket closed unexpectedly
                    finally:
                        # Never retain a protocol object after its context exits, or a
                        # resubscribe could send to a closed socket between attempts.
                        self.healthy = False
                        if self._ws is ws:
                            self._ws = None
            except Exception as e:
                self.healthy = False
                if self._stop:
                    break
                attempts += 1
                log.warning(f"WS feed unavailable ({type(e).__name__}); REST poll fallback active"
                            + (f"; {max_attempts - attempts} tries left" if attempts < max_attempts else "; giving up on WS"))
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 8.0)
        if attempts >= max_attempts and not self._stop:
            log.info("WS disabled after retries; running on REST polling only")
