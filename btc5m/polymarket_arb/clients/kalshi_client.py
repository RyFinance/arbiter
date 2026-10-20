"""Read-only Kalshi public market-data client.

This module intentionally has no authentication, no order submission, and no
trading methods.  It only fetches public BTC 15-minute market metadata and
books for cross-venue observation.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import httpx


KALSHI_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_BTC_15M_SERIES = "KXBTC15M"


def _parse_dt(value: str) -> datetime:
    if not value:
        raise ValueError("empty datetime")
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _float_or_none(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class KalshiBTCMarket:
    event_ticker: str
    ticker: str
    title: str
    status: str
    open_time: datetime
    close_time: datetime
    expected_expiration_time: Optional[datetime]
    settlement_sources: list[dict[str, Any]]
    rules_primary: str
    rules_secondary: str
    yes_ask: Optional[float]
    yes_bid: Optional[float]
    no_ask: Optional[float]
    no_bid: Optional[float]
    yes_ask_size: Optional[float]
    yes_bid_size: Optional[float]
    no_ask_size: Optional[float]
    no_bid_size: Optional[float]
    raw: dict[str, Any]

    @property
    def is_tradable(self) -> bool:
        return self.status in {"active", "open"} and (
            self.yes_ask is not None or self.no_ask is not None
        )

    @classmethod
    def from_market_payload(
        cls,
        payload: dict[str, Any],
        settlement_sources: Optional[list[dict[str, Any]]] = None,
    ) -> "KalshiBTCMarket":
        expected = payload.get("expected_expiration_time") or ""
        return cls(
            event_ticker=str(payload.get("event_ticker") or ""),
            ticker=str(payload.get("ticker") or ""),
            title=str(payload.get("title") or ""),
            status=str(payload.get("status") or ""),
            open_time=_parse_dt(str(payload.get("open_time") or "")),
            close_time=_parse_dt(str(payload.get("close_time") or "")),
            expected_expiration_time=_parse_dt(expected) if expected else None,
            settlement_sources=list(settlement_sources or payload.get("settlement_sources") or []),
            rules_primary=str(payload.get("rules_primary") or ""),
            rules_secondary=str(payload.get("rules_secondary") or ""),
            yes_ask=_float_or_none(payload.get("yes_ask_dollars")),
            yes_bid=_float_or_none(payload.get("yes_bid_dollars")),
            no_ask=_float_or_none(payload.get("no_ask_dollars")),
            no_bid=_float_or_none(payload.get("no_bid_dollars")),
            yes_ask_size=_float_or_none(payload.get("yes_ask_size_fp")),
            yes_bid_size=_float_or_none(payload.get("yes_bid_size_fp")),
            no_ask_size=_float_or_none(payload.get("no_ask_size_fp")),
            no_bid_size=_float_or_none(payload.get("no_bid_size_fp")),
            raw=payload,
        )

    def to_log(self) -> dict[str, Any]:
        return {
            "event_ticker": self.event_ticker,
            "ticker": self.ticker,
            "title": self.title,
            "status": self.status,
            "open_time": self.open_time.isoformat(),
            "close_time": self.close_time.isoformat(),
            "expected_expiration_time": (
                self.expected_expiration_time.isoformat()
                if self.expected_expiration_time else None
            ),
            "settlement_sources": self.settlement_sources,
            "rules_primary": self.rules_primary,
            "rules_secondary": self.rules_secondary,
            "yes_ask": self.yes_ask,
            "yes_bid": self.yes_bid,
            "no_ask": self.no_ask,
            "no_bid": self.no_bid,
            "yes_ask_size": self.yes_ask_size,
            "no_ask_size": self.no_ask_size,
        }


@dataclass(frozen=True)
class KalshiOrderBook:
    ticker: str
    yes: list[tuple[float, float]]
    no: list[tuple[float, float]]
    raw: dict[str, Any]

    @classmethod
    def from_payload(cls, ticker: str, payload: dict[str, Any]) -> "KalshiOrderBook":
        book = payload.get("orderbook_fp") or payload.get("orderbook") or {}
        return cls(
            ticker=ticker,
            yes=_parse_levels(book.get("yes_dollars") or book.get("yes") or []),
            no=_parse_levels(book.get("no_dollars") or book.get("no") or []),
            raw=payload,
        )


def _parse_levels(levels: list[Any]) -> list[tuple[float, float]]:
    parsed: list[tuple[float, float]] = []
    for level in levels:
        try:
            price, size = level[0], level[1]
            parsed.append((float(price), float(size)))
        except (TypeError, ValueError, IndexError):
            continue
    return parsed


class KalshiClient:
    def __init__(
        self,
        base_url: str = KALSHI_BASE_URL,
        session: Optional[httpx.AsyncClient] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self._session = session
        self._owns_session = session is None

    async def __aenter__(self) -> "KalshiClient":
        if self._session is None:
            self._session = httpx.AsyncClient(base_url=self.base_url, timeout=10.0)
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.aclose()
        self._session = None

    @property
    def session(self) -> httpx.AsyncClient:
        if self._session is None:
            self._session = httpx.AsyncClient(base_url=self.base_url, timeout=10.0)
            self._owns_session = True
        return self._session

    async def list_btc_15m_markets(
        self,
        status: str = "open",
        limit: int = 100,
    ) -> list[KalshiBTCMarket]:
        """Fetch public Kalshi BTC 15-minute markets.

        `status="open"` returns currently tradable markets. Use `status=""` to
        omit the filter and inspect future initialized markets too.
        """
        params: dict[str, Any] = {
            "series_ticker": KALSHI_BTC_15M_SERIES,
            "limit": limit,
        }
        if status:
            params["status"] = status
        response = await self.session.get("/markets", params=params)
        response.raise_for_status()
        data = response.json()
        markets = data.get("markets") or []
        return [KalshiBTCMarket.from_market_payload(m) for m in markets]

    async def get_event_markets(self, event_ticker: str) -> list[KalshiBTCMarket]:
        response = await self.session.get(f"/events/{event_ticker}")
        response.raise_for_status()
        data = response.json()
        event = data.get("event") or {}
        settlement_sources = event.get("settlement_sources") or []
        return [
            KalshiBTCMarket.from_market_payload(m, settlement_sources=settlement_sources)
            for m in data.get("markets", [])
        ]

    async def get_orderbook(self, market_ticker: str, depth: int = 10) -> KalshiOrderBook:
        response = await self.session.get(
            f"/markets/{market_ticker}/orderbook",
            params={"depth": depth},
        )
        response.raise_for_status()
        return KalshiOrderBook.from_payload(market_ticker, response.json())
