"""In-memory data models.

Keep these lean to avoid per-tick dict/object overhead in the hot path.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import List, Optional


# Market lifecycle states (replaces polymarket_arb's single-slot window rotation
# with N-concurrent tracking). See market_universe.py.
DISCOVERED = "discovered"
QUALIFIED = "qualified"
ACTIVE = "active"
CLOSING = "closing"
RELEASED = "released"


@dataclass()
class MarketState:
    """HOT-PATH mutable book state for one market. Updated on every price tick.

    Freshness: each leg carries its OWN update timestamp (`yes_ts`, `no_ts`). The
    arb decision path uses `max_age_ms()`, which checks BOTH legs independently —
    what kills fills is one stale leg while the other looks fresh, so gating uses
    the older of the two, not an average.
    """

    market_id: str
    question: str
    yes_token_id: str
    no_token_id: str
    slug: str = ""
    outcome_yes: str = "Yes"
    outcome_no: str = "No"
    best_yes_ask: Optional[float] = None
    best_no_ask: Optional[float] = None
    yes_size: Optional[float] = None
    no_size: Optional[float] = None
    # best bids per leg — needed for the partial-fill market-exit path (sell filled leg back).
    best_yes_bid: Optional[float] = None
    best_no_bid: Optional[float] = None
    last_update_ts: float = 0.0
    yes_ts: float = 0.0
    no_ts: float = 0.0
    # epoch seconds this market is expected to resolve. 0.0 = unknown/no-cap.
    end_time: float = 0.0
    # Epoch seconds at which the market/window opened. Sequential entry is
    # intentionally limited to a short interval after this timestamp.
    start_time: float = 0.0
    # Gamma market fee schedule. ``None`` means metadata was absent; callers
    # then use a conservative category fallback rather than assuming zero.
    fees_enabled: Optional[bool] = None
    fee_rate: float = 0.0
    fee_exponent: float = 1.0

    @property
    def combined_ask(self) -> Optional[float]:
        """Sum of best ask prices for YES and NO legs. None if either leg missing."""
        if self.best_yes_ask is None or self.best_no_ask is None:
            return None
        return self.best_yes_ask + self.best_no_ask

    def max_age_ms(self, now: Optional[float] = None) -> float:
        """Age (ms) of the OLDER leg quote at `now`. Returns inf if a leg has no
        quote yet."""
        if self.best_yes_ask is None or self.best_no_ask is None:
            return float("inf")
        t = now if now is not None else time.time()
        older = min(self.yes_ts, self.no_ts)
        return max(0.0, (t - older) * 1000.0)

    def in_no_entry_zone(self, no_entry_seconds: float, now: Optional[float] = None) -> bool:
        """True in the final `no_entry_seconds` before resolution. Markets with an
        unknown end_time never enter the no-entry zone."""
        if not self.end_time:
            return False
        t = now if now is not None else time.time()
        return (self.end_time - t) <= no_entry_seconds

    def quote_age_ms(self, side: str, now: Optional[float] = None) -> float:
        """Age of one outcome quote, used by sequential leg decisions."""
        side = side.upper()
        ts = self.yes_ts if side == "YES" else self.no_ts
        price = self.best_yes_ask if side == "YES" else self.best_no_ask
        if price is None or not ts:
            return float("inf")
        t = now if now is not None else time.time()
        return max(0.0, (t - ts) * 1000.0)

    @property
    def is_btc_up_down(self) -> bool:
        text = f"{self.slug} {self.question}".lower()
        labels = {self.outcome_yes.lower(), self.outcome_no.lower()}
        is_btc = "bitcoin" in text or "btc" in text
        is_up_down = labels == {"up", "down"} or "up or down" in text or "updown" in text
        return is_btc and is_up_down


@dataclass()
class MarketMeta:
    """COLD-PATH static + slow-changing metadata for one market, refreshed on each
    universe rescan/requalify pass. This is everything the execution loop and the
    universe filter need about a market without re-parsing the Gamma payload.
    """

    market_id: str
    question: str
    yes_token_id: str
    no_token_id: str
    slug: str = ""
    tick_size: float = 0.01
    min_size: float = 1.0
    outcome_yes: str = "Yes"
    outcome_no: str = "No"
    end_time: float = 0.0
    start_time: float = 0.0
    fees_enabled: Optional[bool] = None
    fee_rate: float = 0.0
    fee_exponent: float = 1.0

    # ---- universe-filter fields (Gamma field names in comments; confirmed live
    # 2026-08-19 against GET /markets) ----
    liquidity_usd: float = 0.0        # Gamma `liquidityNum`
    volume_24h_usd: float = 0.0       # Gamma `volume24hr`
    enable_order_book: bool = False   # Gamma `enableOrderBook`
    neg_risk: bool = False            # Gamma `negRisk`
    accepting_orders: bool = False    # Gamma `acceptingOrders`
    has_description: bool = False     # non-empty Gamma `description`
    spread_at_discovery: Optional[float] = None  # sampled from CLOB book, not from Gamma

    # ---- lifecycle ----
    lifecycle_state: str = DISCOVERED
    last_qualified_at: float = 0.0

    @property
    def is_btc_up_down(self) -> bool:
        text = f"{self.slug} {self.question}".lower()
        labels = {self.outcome_yes.lower(), self.outcome_no.lower()}
        return (
            ("bitcoin" in text or "btc" in text)
            and (labels == {"up", "down"} or "up or down" in text or "updown" in text)
        )

    def to_state(self) -> MarketState:
        """Create a MarketState from this MarketMeta."""
        return MarketState(
            market_id=self.market_id,
            question=self.question,
            yes_token_id=self.yes_token_id,
            no_token_id=self.no_token_id,
            slug=self.slug,
            outcome_yes=self.outcome_yes,
            outcome_no=self.outcome_no,
            end_time=self.end_time,
            start_time=self.start_time,
            fees_enabled=self.fees_enabled,
            fee_rate=self.fee_rate,
            fee_exponent=self.fee_exponent,
        )

    def to_dict(self) -> dict:
        return {
            "market_id": self.market_id,
            "question": self.question,
            "yes_token_id": self.yes_token_id,
            "no_token_id": self.no_token_id,
            "slug": self.slug,
            "tick_size": self.tick_size,
            "min_size": self.min_size,
            "outcome_yes": self.outcome_yes,
            "outcome_no": self.outcome_no,
            "end_time": self.end_time,
            "start_time": self.start_time,
            "fees_enabled": self.fees_enabled,
            "fee_rate": self.fee_rate,
            "fee_exponent": self.fee_exponent,
            "liquidity_usd": self.liquidity_usd,
            "volume_24h_usd": self.volume_24h_usd,
            "enable_order_book": self.enable_order_book,
            "neg_risk": self.neg_risk,
            "accepting_orders": self.accepting_orders,
            "has_description": self.has_description,
            "spread_at_discovery": self.spread_at_discovery,
            "lifecycle_state": self.lifecycle_state,
            "last_qualified_at": self.last_qualified_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "MarketMeta":
        return cls(
            market_id=d["market_id"],
            question=d["question"],
            yes_token_id=d["yes_token_id"],
            no_token_id=d["no_token_id"],
            slug=d.get("slug", ""),
            tick_size=float(d.get("tick_size", 0.01)),
            min_size=float(d.get("min_size", 1.0)),
            outcome_yes=d.get("outcome_yes", "Yes"),
            outcome_no=d.get("outcome_no", "No"),
            end_time=float(d.get("end_time", 0.0)),
            start_time=float(d.get("start_time", 0.0)),
            fees_enabled=d.get("fees_enabled"),
            fee_rate=float(d.get("fee_rate", 0.0)),
            fee_exponent=float(d.get("fee_exponent", 1.0)),
            liquidity_usd=float(d.get("liquidity_usd", 0.0)),
            volume_24h_usd=float(d.get("volume_24h_usd", 0.0)),
            enable_order_book=bool(d.get("enable_order_book", False)),
            neg_risk=bool(d.get("neg_risk", False)),
            accepting_orders=bool(d.get("accepting_orders", False)),
            has_description=bool(d.get("has_description", False)),
            spread_at_discovery=d.get("spread_at_discovery"),
            lifecycle_state=d.get("lifecycle_state", DISCOVERED),
            last_qualified_at=float(d.get("last_qualified_at", 0.0)),
        )


@dataclass()
class MarketCache:
    """Cold-path bundle persisted to state/market_cache.json."""

    generated_at: float = 0.0
    source: str = "gamma"
    markets: List[MarketMeta] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "generated_at": self.generated_at,
            "source": self.source,
            "markets": [m.to_dict() for m in self.markets],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "MarketCache":
        return cls(
            generated_at=float(d.get("generated_at", 0.0)),
            source=d.get("source", "gamma"),
            markets=[MarketMeta.from_dict(m) for m in d.get("markets", [])],
        )


@dataclass()
class TradeRecord:
    """Immutable record of one dual-leg fill attempt (for PnL + audit)."""

    market_id: str
    ts: float
    yes_price: float
    no_price: float
    combined: float
    size: float
    yes_order_id: Optional[str] = None
    no_order_id: Optional[str] = None
    yes_filled: bool = False
    no_filled: bool = False
    pnl_locked: Optional[float] = None
    # explicit order-execution mode — "dry_run" | "paper" | "live". Paper-trade
    # order ids are shaped identically to live CLOB ids, so records must carry the
    # mode explicitly for later summary/audit separation.
    mode: str = "live"
    max_quote_age_ms: Optional[float] = None
    freshness_band: str = ""          # fresh | 0.5-2s | 2-10s | stale | none
    recovery: str = ""                # clean | hedged | exited | unmanaged
    residual_pnl: Optional[float] = None
    fees_paid: float = 0.0
    strategy: str = "dual"
    first_leg_side: str = ""
    leg_gap_s: float = 0.0

    @property
    def partial(self) -> bool:
        """True if exactly one leg filled (partial fill)."""
        return self.yes_filled != self.no_filled
