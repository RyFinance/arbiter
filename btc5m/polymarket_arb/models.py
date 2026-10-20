"""In-memory data models.

Keep these lean () to avoid per-tick dict/object overhead in the hot path.
Annotations use `from __future__ import annotations`, so `float | None` is safe on 3.9+.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import List, Optional

from .config import SETTINGS


@dataclass()
class MarketState:
    """HOT-PATH mutable book state for one market. Updated on every price tick.

    Only cheap scalar fields. No nested objects are allocated in the loop.

    Freshness (FIX #1): each leg carries its OWN update timestamp (`yes_ts`,
    `no_ts`). The single `last_update_ts` is kept for backward-compat with the
    risk precheck, but the arb decision path uses `max_age_ms()` which checks
    BOTH legs independently. Median freshness is a vanity metric — what kills
    fills is one stale leg while the other looks fresh, so we gate on the older
    of the two.
    """

    market_id: str
    question: str
    yes_token_id: str
    no_token_id: str
    best_yes_ask: Optional[float] = None
    best_no_ask: Optional[float] = None
    yes_size: Optional[float] = None
    no_size: Optional[float] = None
    # best bids per leg — needed for the FIX #2 market-exit path (sell filled leg back).
    best_yes_bid: Optional[float] = None
    best_no_bid: Optional[float] = None
    last_update_ts: float = 0.0
    # per-leg quote timestamps (seconds, time.time()). Written by price_feed on
    # every WS/REST tick for the affected leg.
    yes_ts: float = 0.0
    no_ts: float = 0.0
    # rolling-window targeting (FIX #3). start/close epoch seconds (UTC). Zero
    # means "not a windowed market" — the original broad-scan path.
    window_start: float = 0.0
    window_close: float = 0.0

    @property
    def combined_ask(self) -> Optional[float]:
        """Sum of best ask prices for YES and NO legs. None if either leg missing."""
        if self.best_yes_ask is None or self.best_no_ask is None:
            return None
        return self.best_yes_ask + self.best_no_ask

    def max_age_ms(self, now: Optional[float] = None) -> float:
        """Age (ms) of the OLDER leg quote at `now`. Returns inf if a leg has no
        quote yet. The arb gate compares this against MAX_QUOTE_AGE_MS — using the
        older leg is what catches the p95-stale-on-one-side failure mode.
        """
        if self.best_yes_ask is None or self.best_no_ask is None:
            return float("inf")
        t = now if now is not None else time.time()
        # newer of the two legs bounds how stale the book-as-a-whole is; we want
        # the OLDER leg because that is the one at risk of being wrong.
        older = min(self.yes_ts, self.no_ts)
        return max(0.0, (t - older) * 1000.0)

    def in_no_entry_zone(self, now: Optional[float] = None) -> bool:
        """FIX #4: True in the final NO_ENTRY_SECONDS before window close.
        Non-windowed markets never enter the no-entry zone."""
        if not self.window_close:
            return False
        t = now if now is not None else time.time()
        return (self.window_close - t) <= SETTINGS.no_entry_seconds


@dataclass()
class MarketMeta:
    """COLD-PATH static metadata for one market. Built once and cached to disk.

    This is everything the execution loop needs about a market so it NEVER has to
    re-discover or re-parse during trading.
    """

    market_id: str
    question: str
    yes_token_id: str
    no_token_id: str
    tick_size: float = 0.01
    min_size: float = 1.0
    outcome_yes: str = "Yes"
    outcome_no: str = "No"
    # Rolling-window bounds (FIX #3): start/close epoch seconds. Zero = not windowed.
    window_start: float = 0.0
    window_close: float = 0.0
    window_slug: str = ""

    def to_state(self) -> MarketState:
        """Create a MarketState from this MarketMeta.
        
        HOT PATH: tiny, allocation-free-ish object creation once per market at startup.
        """
        return MarketState(
            market_id=self.market_id,
            question=self.question,
            yes_token_id=self.yes_token_id,
            no_token_id=self.no_token_id,
            window_start=self.window_start,
            window_close=self.window_close,
        )

    def to_dict(self) -> dict:
        """Serialize MarketMeta to a dictionary for JSON persistence."""
        return {
            "market_id": self.market_id,
            "question": self.question,
            "yes_token_id": self.yes_token_id,
            "no_token_id": self.no_token_id,
            "tick_size": self.tick_size,
            "min_size": self.min_size,
            "outcome_yes": self.outcome_yes,
            "outcome_no": self.outcome_no,
            "window_start": self.window_start,
            "window_close": self.window_close,
            "window_slug": self.window_slug,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "MarketMeta":
        """Deserialize MarketMeta from a dictionary."""
        return cls(
            market_id=d["market_id"],
            question=d["question"],
            yes_token_id=d["yes_token_id"],
            no_token_id=d["no_token_id"],
            tick_size=float(d.get("tick_size", 0.01)),
            min_size=float(d.get("min_size", 1.0)),
            outcome_yes=d.get("outcome_yes", "Yes"),
            outcome_no=d.get("outcome_no", "No"),
            window_start=float(d.get("window_start", 0.0)),
            window_close=float(d.get("window_close", 0.0)),
            window_slug=d.get("window_slug", ""),
        )


@dataclass()
class MarketCache:
    """Cold-path bundle persisted to state/market_cache.json."""

    generated_at: float = 0.0
    source: str = "gamma"
    markets: List[MarketMeta] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Serialize MarketCache to a dictionary for JSON persistence."""
        return {
            "generated_at": self.generated_at,
            "source": self.source,
            "markets": [m.to_dict() for m in self.markets],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "MarketCache":
        """Deserialize MarketCache from a dictionary."""
        return cls(
            generated_at=float(d.get("generated_at", 0.0)),
            source=d.get("source", "gamma"),
            markets=[MarketMeta.from_dict(m) for m in d.get("markets", [])],
        )


@dataclass()
class TradeRecord:
    """Immutable record of one dual-leg fill attempt (for PnL + audit).

    Freshness (FIX #1): `max_quote_age_ms` + `freshness_band` are logged on EVERY
    attempt (filled AND missed) so PnL can be bucketed by staleness later.
    Partial recovery (FIX #2): `recovery` records which action was taken when one
    leg failed to fill, and `residual_pnl` is the realized outcome of that action,
    tracked separately from clean-arb `pnl_locked`.
    """

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
    # TASK 2: explicit order-execution mode — "dry_run" | "paper" | "live".
    # Paper-trade order ids are shaped identically to live CLOB ids, so records
    # must carry the mode explicitly for later summary/audit separation.
    mode: str = "live"
    # FIX #1 freshness-at-fill
    max_quote_age_ms: Optional[float] = None
    freshness_band: str = ""          # fresh | 0.5-2s | 2-10s | stale | none
    # FIX #2 partial-fill recovery
    recovery: str = ""                # clean | hedged | exited | unmanaged
    residual_pnl: Optional[float] = None
    # FIX #3 windowing
    window_start: float = 0.0
    window_close: float = 0.0

    @property
    def partial(self) -> bool:
        """True if exactly one leg filled (partial fill)."""
        return self.yes_filled != self.no_filled
