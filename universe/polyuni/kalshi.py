"""Kalshi venue: market discovery, order books, fees, price feed and orders.

The strategy engine is venue-agnostic; this module adapts Kalshi to the same
shapes the Polymarket side uses:

  * A Kalshi market has ONE order book: YES bids and NO bids. Buying YES lifts
    the best NO bid (YES ask = 1 - NO bid) and vice versa. Each market is
    exposed to the engine as two pseudo tokens, ``"<TICKER>:yes"`` and
    ``"<TICKER>:no"``, so pair maths, fills and P&L reuse the existing code.
  * Market data (markets, order books) is public: dry-run/paper on Kalshi
    needs no key. Prices are polled over REST, not streamed (Kalshi's
    WebSocket requires an API key).
  * Fees follow Kalshi's quadratic schedule: rate * contracts * p * (1 - p),
    rounded UP to the cent (see ``kalshi_fee``).
  * Live orders are signed fill-or-kill limit orders (``KalshiOrderClient``),
    reached only through the same live-control gates as Polymarket.
"""
from __future__ import annotations

import asyncio
import base64
import math
import os
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

from .logger import log
from .models import MarketMeta

KALSHI_BASE_URLS = {
    "prod": "https://api.elections.kalshi.com/trade-api/v2",
    "demo": "https://demo-api.kalshi.co/trade-api/v2",
}
# Kalshi's basic tier allows ~20 reads/s; stay under it when polling many books.
READS_PER_SECOND = 15.0
MAX_DISCOVERY_PAGES = 5
TOKEN_SEP = ":"


def kalshi_env() -> str:
    env = os.getenv("KALSHI_ENV", "prod").strip().lower()
    return env if env in KALSHI_BASE_URLS else "prod"


def kalshi_base(env: Optional[str] = None) -> str:
    return KALSHI_BASE_URLS[env or kalshi_env()]


def make_token(ticker: str, side: str) -> str:
    return f"{ticker}{TOKEN_SEP}{side.lower()}"


def split_token(token_id: str) -> Tuple[str, str]:
    """``"KXBTC15M-..:yes"`` -> ``("KXBTC15M-..", "yes")``."""
    ticker, _, side = str(token_id).rpartition(TOKEN_SEP)
    if not ticker or side not in {"yes", "no"}:
        raise ValueError(f"not a Kalshi token: {token_id!r}")
    return ticker, side


def is_kalshi_token(token_id: str) -> bool:
    try:
        split_token(token_id)
    except ValueError:
        return False
    return True


# --------------------------------------------------------------------------
# Order books
# --------------------------------------------------------------------------

def _levels(raw: Any, cents: bool) -> List[Tuple[float, float]]:
    out = []
    for level in raw or []:
        try:
            price, qty = float(level[0]), float(level[1])
        except (TypeError, ValueError, IndexError):
            continue
        if cents:
            price /= 100.0
        if 0.0 < price < 1.0 and qty > 0:
            out.append((round(price, 4), qty))
    return out


def parse_book(payload: Dict[str, Any]) -> Dict[str, List[Tuple[float, float]]]:
    """Normalize a Kalshi orderbook response into YES/NO bid ladders (dollars)."""
    fp = payload.get("orderbook_fp")
    if isinstance(fp, dict):
        return {"yes": _levels(fp.get("yes_dollars"), False), "no": _levels(fp.get("no_dollars"), False)}
    book = payload.get("orderbook") or {}
    if "yes_dollars" in book or "no_dollars" in book:
        return {"yes": _levels(book.get("yes_dollars"), False), "no": _levels(book.get("no_dollars"), False)}
    return {"yes": _levels(book.get("yes"), True), "no": _levels(book.get("no"), True)}


def side_book(book: Dict[str, List[Tuple[float, float]]], side: str) -> Dict[str, list]:
    """Polymarket-shaped book for one side: asks come from the OTHER side's bids."""
    side = side.lower()
    other = "no" if side == "yes" else "yes"
    asks = sorted((round(1.0 - p, 4), q) for p, q in book.get(other, []))
    bids = sorted(book.get(side, []), reverse=True)
    return {
        "asks": [{"price": p, "size": q} for p, q in asks],
        "bids": [{"price": p, "size": q} for p, q in bids],
    }


def top_of_book(book: Dict[str, List[Tuple[float, float]]]) -> Dict[str, Optional[float]]:
    """Best ask/size/bid for both sides."""
    out: Dict[str, Optional[float]] = {}
    for side in ("yes", "no"):
        ladder = side_book(book, side)
        ask = ladder["asks"][0] if ladder["asks"] else None
        bid = ladder["bids"][0] if ladder["bids"] else None
        out[f"{side}_ask"] = ask["price"] if ask else None
        out[f"{side}_ask_size"] = ask["size"] if ask else None
        out[f"{side}_bid"] = bid["price"] if bid else None
    return out


async def fetch_book(session: httpx.AsyncClient, ticker: str) -> Dict[str, List[Tuple[float, float]]]:
    resp = await session.get(f"{kalshi_base()}/markets/{ticker}/orderbook")
    resp.raise_for_status()
    return parse_book(resp.json())


def fetch_book_sync(ticker: str, timeout: float = 5.0) -> Dict[str, List[Tuple[float, float]]]:
    resp = httpx.get(f"{kalshi_base()}/markets/{ticker}/orderbook", timeout=timeout)
    resp.raise_for_status()
    return parse_book(resp.json())


# --------------------------------------------------------------------------
# Fees
# --------------------------------------------------------------------------

def kalshi_fee(contracts: float, price: float, rate: float = 0.07) -> float:
    """Kalshi taker fee: rate * C * P * (1-P), rounded up to the next cent."""
    if contracts <= 0 or price <= 0 or price >= 1 or rate <= 0:
        return 0.0
    raw = rate * contracts * price * (1.0 - price)
    return math.ceil(round(raw * 100.0, 9)) / 100.0


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------

def _dollars(market: Dict[str, Any], name: str) -> Optional[float]:
    value = market.get(f"{name}_dollars")
    if value is None:
        cents = market.get(name)
        return None if cents is None else float(cents) / 100.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _num(market: Dict[str, Any], *names: str) -> float:
    for name in names:
        try:
            if market.get(name) is not None:
                return float(market[name])
        except (TypeError, ValueError):
            continue
    return 0.0


def _epoch(value: Any) -> float:
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def market_meta(market: Dict[str, Any], fee_rate: float = 0.07) -> Optional[MarketMeta]:
    """Map one Kalshi market to the engine's MarketMeta (None if not tradable)."""
    if market.get("market_type", "binary") != "binary" or not market.get("ticker"):
        return None
    ticker = str(market["ticker"])
    title = str(market.get("title") or ticker)
    sub = str(market.get("yes_sub_title") or "")
    up_down = ticker.startswith("KXBTC15M") or " up " in f" {title.lower()} " or "up or down" in title.lower()
    question = f"{title} ({sub})" if sub and sub.lower() not in title.lower() else title
    yes_bid, yes_ask = _dollars(market, "yes_bid"), _dollars(market, "yes_ask")
    last = _dollars(market, "last_price") or yes_ask or 0.5
    liquidity = _num(market, "yes_bid_size_fp") * (yes_bid or 0) + _num(market, "yes_ask_size_fp") * (yes_ask or 0)
    if not liquidity:
        liquidity = _dollars(market, "liquidity") or 0.0
    return MarketMeta(
        market_id=ticker,
        question=question,
        yes_token_id=make_token(ticker, "yes"),
        no_token_id=make_token(ticker, "no"),
        slug=ticker.lower(),
        tick_size=0.01,
        min_size=1.0,
        outcome_yes="Up" if up_down else "Yes",
        outcome_no="Down" if up_down else "No",
        end_time=_epoch(market.get("close_time")),
        start_time=_epoch(market.get("open_time")),
        fees_enabled=True,
        fee_rate=fee_rate,
        fee_exponent=1.0,
        liquidity_usd=float(liquidity),
        volume_24h_usd=_num(market, "volume_24h_fp", "volume_24h") * float(last),
        enable_order_book=True,
        neg_risk=False,
        accepting_orders=str(market.get("status", "")).lower() in {"active", "open"},
        has_description=bool(market.get("rules_primary")),
        spread_at_discovery=(round(yes_ask - yes_bid, 4) if yes_ask is not None and yes_bid is not None else None),
    )


async def discover_markets(session: httpx.AsyncClient, settings: Any) -> List[MarketMeta]:
    """Open Kalshi binary markets. BTC-only scope reads just the BTC up/down series."""
    base = kalshi_base()
    fee_rate = float(getattr(settings, "kalshi_fee_rate", 0.07))
    series = list(getattr(settings, "kalshi_btc_series", ["KXBTC15M"]))
    raw: Dict[str, Dict[str, Any]] = {}
    if getattr(settings, "btc_only", False) or getattr(settings, "include_btc_short_windows", True):
        for name in series:
            try:
                resp = await session.get(f"{base}/markets", params={"series_ticker": name, "status": "open", "limit": 200})
                resp.raise_for_status()
                for m in resp.json().get("markets") or []:
                    raw[m["ticker"]] = m
            except (httpx.HTTPError, KeyError, ValueError) as exc:
                log.warning(f"kalshi series {name} discovery failed: {exc!r}")
    if not getattr(settings, "btc_only", False):
        cursor = None
        for _ in range(MAX_DISCOVERY_PAGES):
            params = {"status": "open", "limit": 1000, "mve_filter": "exclude"}
            if cursor:
                params["cursor"] = cursor
            try:
                resp = await session.get(f"{base}/markets", params=params)
                resp.raise_for_status()
                body = resp.json()
            except (httpx.HTTPError, ValueError) as exc:
                log.warning(f"kalshi discovery page failed: {exc!r}")
                break
            for m in body.get("markets") or []:
                raw.setdefault(m.get("ticker"), m)
            cursor = body.get("cursor")
            if not cursor:
                break
    metas = [meta for meta in (market_meta(m, fee_rate) for m in raw.values()) if meta is not None]
    log.info(f"kalshi discovery: {len(metas)} binary markets ({kalshi_env()})")
    return metas


# --------------------------------------------------------------------------
# Price feed (REST polling; same callbacks as the Polymarket feed)
# --------------------------------------------------------------------------

def _make_price_feed_class():
    from .config import SETTINGS
    from .price_feed import PriceFeed

    class KalshiPriceFeed(PriceFeed):
        """Polls each active market's public order book and emits both legs."""

        async def start(self):
            self._session = httpx.AsyncClient(timeout=4.0)
            task = asyncio.create_task(self._poll_loop(), name="kalshi-poll-feed")
            task.add_done_callback(self._log_task_error)

        def _start_ws(self, token_ids):  # no unauthenticated Kalshi WebSocket
            return

        async def _poll_loop(self):
            while not self._stop:
                markets = list(self.states.values())
                if not markets:
                    await asyncio.sleep(SETTINGS.poll_interval_s)
                    continue
                t0 = time.time()
                results = await asyncio.gather(
                    *(fetch_book(self._session, st.market_id) for st in markets), return_exceptions=True
                )
                for st, res in zip(markets, results):
                    if isinstance(res, Exception):
                        log.warning(f"kalshi book fetch failed for {st.market_id}: {res!r}")
                        continue
                    top = top_of_book(res)
                    self._on_ws_tick(st.yes_token_id, top["yes_ask"], top["yes_ask_size"], top["yes_bid"])
                    self._on_ws_tick(st.no_token_id, top["no_ask"], top["no_ask_size"], top["no_bid"])
                self._poll_latency = time.time() - t0
                await asyncio.sleep(max(SETTINGS.poll_interval_s, len(markets) / READS_PER_SECOND))

    return KalshiPriceFeed


def KalshiPriceFeed(*args, **kwargs):  # noqa: N802 - factory keeps the import lazy
    return _make_price_feed_class()(*args, **kwargs)


# --------------------------------------------------------------------------
# Live orders (signed; reached only through live_control gates)
# --------------------------------------------------------------------------

class KalshiOrderClient:
    """Signed client for Kalshi fill-or-kill limit orders."""

    def __init__(self, key_id: str, pem: bytes, environment: str = "prod", http: Optional[httpx.Client] = None):
        from cryptography.hazmat.primitives import serialization

        self.key_id = key_id
        self.base = kalshi_base(environment)
        self.prefix = httpx.URL(self.base).path
        self.key = serialization.load_pem_private_key(pem, password=None)
        self.http = http or httpx.Client(timeout=8.0)

    @classmethod
    def from_linked_account(cls) -> "KalshiOrderClient":
        from .venues import CONNECTORS
        creds = CONNECTORS["kalshi"].credentials()
        if not creds:
            raise RuntimeError("Kalshi is not linked: add an API key on the Venues sheet or in .env")
        return cls(creds["key_id"], Path(creds["key_path"]).read_bytes(), creds["environment"])

    def _headers(self, method: str, path: str) -> Dict[str, str]:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        timestamp = str(int(time.time() * 1000))
        signature = self.key.sign(
            f"{timestamp}{method}{self.prefix}{path}".encode(),
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
            "Content-Type": "application/json",
        }

    @staticmethod
    def order_body(token_id: str, price: float, size: float, side: str) -> Dict[str, Any]:
        """Kalshi order JSON for one engine order (BUY/SELL of a YES/NO token)."""
        ticker, outcome = split_token(token_id)
        body: Dict[str, Any] = {
            "ticker": ticker,
            "client_order_id": str(uuid.uuid4()),
            "action": "buy" if side.upper() == "BUY" else "sell",
            "side": outcome,
            "count": int(size),
            "type": "limit",
            "time_in_force": "fill_or_kill",
        }
        cents = round(float(price) * 100.0, 6)
        if abs(cents - round(cents)) < 1e-6 and 1 <= round(cents) <= 99:
            body[f"{outcome}_price"] = int(round(cents))
        else:
            body[f"{outcome}_price_dollars"] = f"{float(price):.4f}"
        return body

    def post_fok(self, token_id: str, price: float, size: float, side: str) -> Optional[Tuple[str, float]]:
        """Post one REAL fill-or-kill order. Returns (order_id, avg fill price) on a full fill."""
        if int(size) != size or size < 1:
            log.warning(f"KALSHI order size must be whole contracts, got {size}")
            return None
        body = self.order_body(token_id, price, size, side)
        path = "/portfolio/orders"
        resp = self.http.post(f"{self.base}{path}", json=body, headers=self._headers("POST", path))
        if resp.status_code >= 400:
            log.warning(f"KALSHI order rejected {resp.status_code}: {resp.text[:200]}")
            return None
        order = (resp.json() or {}).get("order") or {}
        filled = _num(order, "fill_count_fp", "fill_count")
        status = str(order.get("status", "")).lower()
        if status != "executed" or filled + 1e-9 < float(size):
            log.warning(f"KALSHI FOK not filled status={status!r} filled={filled}")
            return None
        fill_price = float(price)  # FOK fills at or better than the limit
        cost = _dollars(order, "taker_fill_cost")
        if body["action"] == "buy" and cost and filled:
            fill_price = cost / filled
        return str(order.get("order_id")), fill_price
