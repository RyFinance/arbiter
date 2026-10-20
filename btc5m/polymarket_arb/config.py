"""Configuration — load from .env / environment, expose a single frozen Settings object.

COLD PATH only (read once at startup). Nothing here runs in the trading loop.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List

from dotenv import load_dotenv

load_dotenv()


# LSE (London Strategic Edge) economic-calendar API key placeholder sentinel.
# This value is synthetic and rejects on every LSE endpoint. It exists so
# scrapers/page_agent_runner.py can detect "no key configured" and fall back to
# the SPA's hardcoded anon-key (which the calendar endpoint accepts). Keep it
# in sync with scrapers/page_agent_runner.py:_LSE_PLACEHOLDER_KEY.
#
# 2026-08-19: this constant previously held a real, working LSE credential
# (it authenticated against /vault/candles with HTTP 200) that had been
# mistaken for an inert sentinel and hardcoded across ~8 files. Rotated to a
# genuinely synthetic all-zero value; the real key now lives only in .env.
LSE_API_PLACEHOLDER_KEY = "lse_live_00000000000000000000000000000000"


def _bool(name: str, default: bool) -> bool:
    """Parse a boolean from environment variable.
    
    Args:
        name: Environment variable name
        default: Default value if not set
        
    Returns:
        True if value is "1", "true", "yes", or "on" (case-insensitive), else default
    """
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _float(name: str, default: float) -> float:
    """Parse a float from environment variable.
    
    Args:
        name: Environment variable name
        default: Default value if not set or empty
        
    Returns:
        Parsed float value, or default
    """
    v = os.getenv(name)
    return float(v) if v is not None and v != "" else default


def _int(name: str, default: int) -> int:
    """Parse an int from environment variable.
    
    Args:
        name: Environment variable name
        default: Default value if not set or empty
        
    Returns:
        Parsed int value, or default
    """
    v = os.getenv(name)
    return int(v) if v is not None and v != "" else default


def _list(name: str, default: List[str]) -> List[str]:
    """Parse a comma-separated list from environment variable.
    
    Args:
        name: Environment variable name
        default: Default list if not set or empty
        
    Returns:
        List of stripped string values
    """
    v = os.getenv(name)
    if not v:
        return list(default)
    return [s.strip() for s in v.split(",") if s.strip()]


@dataclass(frozen=True)
class Settings:
    """Frozen configuration dataclass loaded from environment variables.
    
    COLD PATH only (read once at startup). All settings are loaded from .env
    or environment variables with sensible defaults.
    """
    # endpoints
    gamma_base_url: str = os.getenv("GAMMA_BASE_URL", "https://gamma-api.polymarket.com")
    clob_base_url: str = os.getenv("CLOB_BASE_URL", "https://clob.polymarket.com")
    clob_ws_url: str = os.getenv("CLOB_WS_URL", "wss://ws-subscriptions-clob.polymarket.com")  # host only; client appends /ws/market

    # market scope
    market_whitelist: List[str] = field(default_factory=lambda: _list("MARKET_WHITELIST", []))
    market_filter_tag: str = os.getenv("MARKET_FILTER_TAG", "Crypto")
    market_keywords: List[str] = field(default_factory=lambda: _list("MARKET_KEYWORDS", ["btc", "bitcoin"]))

    # When true, track ALL binary YES/NO markets (max edge capture). When false,
    # apply MARKET_KEYWORDS to narrow scope. Whitelist always wins if set.
    market_broad_scan: bool = field(default_factory=lambda: _bool("MARKET_BROAD_SCAN", True))

    # strategy
    min_edge: float = field(default_factory=lambda: _float("MIN_EDGE", 0.03))
    order_size: float = field(default_factory=lambda: _float("ORDER_SIZE", 10.0))

    # ---- Rolling window targeting (FIX #3): Polymarket BTC "Up or Down" 5-min ----
    # When window_mode is on, the bot targets the deterministic rolling-window
    # series `{asset}-updown-{tf}-{unix_ts}` (e.g. btc-updown-5m-1780319100) and
    # rotates to the next window before the current one settles. When off, the bot
    # falls back to the original broad Gamma discovery path.
    window_mode: bool = field(default_factory=lambda: _bool("WINDOW_MODE", True))
    window_asset: str = os.getenv("WINDOW_ASSET", "btc")        # btc | eth | sol ...
    window_timeframe: str = os.getenv("WINDOW_TIMEFRAME", "5m")  # 5m | 15m
    window_seconds: int = field(default_factory=lambda: _int("WINDOW_SECONDS", 300))
    # Pre-resolve the NEXT window's token ids this many seconds before close so the
    # hot path never stalls on discovery at the boundary.
    window_pre_resolve_s: float = field(default_factory=lambda: _float("WINDOW_PRE_RESOLVE_S", 30.0))
    # FIX #4: debounce near close — no NEW entries in the final N seconds before
    # settlement (spreads whip as the Chainlink reference price becomes near-certain).
    no_entry_seconds: float = field(default_factory=lambda: _float("NO_ENTRY_SECONDS", 5.0))

    # FIX #5: slippage stress buffer applied ON TOP OF the entry edge when judging
    # whether a time-of-day slice survives. A window's best (min) combined must clear
    # `1.0 - MIN_EDGE - SLIPPAGE_STRESS_BUFFER` to "survive"; slices that never survive
    # are discarded. This MUST be > 0 or the stress test degenerates to the entry-edge
    # condition and can never fail (the enforcement becomes a no-op).
    slippage_stress_buffer: float = field(default_factory=lambda: _float("SLIPPAGE_STRESS_BUFFER", 0.01))

    # ---- Quote freshness gating (FIX #1) ----
    # Reject any fire where either leg's quote is older than this at decision time.
    # Median freshness is a vanity metric; p95 staleness is what blows up fills.
    max_quote_age_ms: int = field(default_factory=lambda: _int("MAX_QUOTE_AGE_MS", 500))
    # CLOB streams paired leg books in adjacent frames. Hold evaluation briefly so
    # the second leg can replace the prior-frame quote before edge detection.
    cross_leg_settle_ms: float = field(
        default_factory=lambda: _float("CROSS_LEG_SETTLE_MS", 10.0)
    )

    # ---- Partial-fill / unhedged-leg protection (FIX #2) ----
    # If one leg fills and the other does not confirm within this gap, treat it as a
    # partial-fill event and manage it (cancel resting leg -> hedge or exit).
    max_leg_gap_ms: int = field(default_factory=lambda: _int("MAX_LEG_GAP_MS", 300))
    # When hedging the missing leg at a worse (re-priced) ask, only do so if the
    # remaining edge still covers this floor (separate from MIN_EDGE so we can be
    # more aggressive about completing a hedge than about opening a new arb).
    partial_hedge_min_edge: float = field(default_factory=lambda: _float("PARTIAL_HEDGE_MIN_EDGE", 0.0))

    # auth
    private_key: str = os.getenv("PRIVATE_KEY", "") or ""
    proxy_wallet: str = os.getenv("PROXY_WALLET", "") or ""
    clob_api_key: str = os.getenv("CLOB_API_KEY", "") or ""
    # LSE (London Strategic Edge) economic-calendar API key. There is NO
    # real-key default: LSE_API_PLACEHOLDER_KEY is a NAMED placeholder sentinel
    # (scrapers/page_agent_runner.py compares against it to decide whether to
    # fall back to the SPA's hardcoded anon-key). A bare key-shaped string here
    # used to read as a real live credential and silently misled operators;
    # seeing it in SETTINGS/state means the operator has NOT configured a key —
    # main.py warns loudly about that at startup. Set the real key via the
    # LSE_API_KEY environment variable (see docs/lse_api_key_acquisition.md).
    lse_api_key: str = os.getenv("LSE_API_KEY", LSE_API_PLACEHOLDER_KEY) or ""
    # Polymarket CLOB API credentials (L2 HMAC auth). These are derived from the
    # wallet via create_or_derive_api_key() on first live run and cached in .env.
    polymarket_api_key: str = os.getenv("POLYMARKET_API_KEY", "") or ""
    polymarket_api_secret: str = os.getenv("POLYMARKET_API_SECRET", "") or ""
    polymarket_api_passphrase: str = os.getenv("POLYMARKET_API_PASSPHRASE", "") or ""

    # ---- On-chain trading approvals (COLD PATH only) ----
    # Polygon RPC endpoint used for ERC-20 approve + ERC-1155 setApprovalForAll calls.
    # Needed because the py_clob_client_v2 SDK does NOT provide a
    # setup_trading_approvals() method — we issue these txns directly via eth-tx.
    polygon_rpc_url: str = os.getenv("POLYGON_RPC_URL", "https://polygon-rpc.com") or ""
    # Maximum gas price (gwei) for approval transactions at startup.
    approval_max_gas_price_gwei: float = field(
        default_factory=lambda: _float("APPROVAL_MAX_GAS_PRICE_GWEI", 80.0)
    )

    # transport
    use_ws: bool = field(default_factory=lambda: _bool("USE_WS", True))
    poll_interval_s: float = field(default_factory=lambda: _float("POLL_INTERVAL_S", 0.25))
    cache_refresh_interval_s: float = field(default_factory=lambda: _float("CACHE_REFRESH_INTERVAL_S", 300.0))
    force_cache_rebuild: bool = field(default_factory=lambda: _bool("FORCE_CACHE_REBUILD", False))

    # risk
    # Macro-calendar state is refreshed every 300s by the page agent. Block new
    # entries after three missed refresh cycles rather than trading through an
    # unknown news regime. Freshness is evaluated on every RiskState precheck.
    macro_state_max_age_s: float = field(
        default_factory=lambda: _float("MACRO_STATE_MAX_AGE_S", 900.0)
    )
    # ---- partial-fill recovery policy (FIX #2) ----
    # When one leg has filled and the other cannot be hedged profitably, exit the
    # filled leg at market (take best bid immediately, slippage accepted, limit losses).
    # Hard cap on residual losses per partial-fill event before the kill switch trips.
    partial_loss_cap_usd: float = field(default_factory=lambda: _float("PARTIAL_LOSS_CAP_USD", 25.0))

    max_position_per_market: float = field(default_factory=lambda: _float("MAX_POSITION_PER_MARKET", 200.0))
    max_total_capital: float = field(default_factory=lambda: _float("MAX_TOTAL_CAPITAL", 1000.0))
    min_liquidity_per_side: float = field(default_factory=lambda: _float("MIN_LIQUIDITY_PER_SIDE", 5.0))
    stale_book_timeout_s: float = field(default_factory=lambda: _float("STALE_BOOK_TIMEOUT_S", 5.0))
    daily_kill_switch_usd: float = field(default_factory=lambda: _float("DAILY_KILL_SWITCH_USD", 500.0))
    emergency_stop_file: str = os.getenv("EMERGENCY_STOP_FILE", "state/EMERGENCY_STOP")

    # observability
    log_jsonl: str = os.getenv("LOG_JSONL", "state/bot.jsonl")
    log_level: str = os.getenv("LOG_LEVEL", "INFO")

    # ---- TASK B: crash recovery & alerting (OPTIONAL, config-gated) ----
    # Empty = alerting entirely disabled (never required). When non-empty the bot writes a
    # heartbeat file on every tick and the webhook fires on crash / emergency-stop /
    # PARTIAL_LOSS_CAP_USD kill switch / extended WS disconnect.
    alert_webhook_url: str = os.getenv("ALERT_WEBHOOK_URL", "") or ""
    # File the bot touches every tick; the standalone monitor script checks its mtime.
    alert_heartbeat_file: str = os.getenv("ALERT_HEARTBEAT_FILE", "state/HEARTBEAT")
    # Seconds without a heartbeat before the monitor declares a crash/hang and alerts.
    alert_heartbeat_timeout_s: float = field(
        default_factory=lambda: _float("ALERT_HEARTBEAT_TIMEOUT_S", 10.0)
    )

    # safety
    dry_run: bool = field(default_factory=lambda: _bool("DRY_RUN", True))
    # PAPER_TRADE: still run the full real auth flow (L1 API-key derivation + trading
    # approvals) and simulate fills using live order-book prices — WITHOUT posting
    # anything to the exchange. Synthetic ids and pair events retain explicit paper
    # mode and fill-source metadata so reports cannot promote them as live fills.
    # Requires dry_run=False (authenticated client mode) so that the
    # full auth path and price-book wiring are active. When True, submit_order() never
    # hits an exchange order endpoint: it reads executable REST depth and synthesizes
    # a paper fill record.
    paper_trade: bool = field(default_factory=lambda: _bool("PAPER_TRADE", False))
    # HIGH (FIX#6): live signing is delegated to the current Polymarket CLOB V2 SDK,
    # but live order submission is still gated until an operator runs the SDK
    # verifier with their real wallet and explicitly sets LIVE_SIGNING_VERIFIED=true.
    live_signing_verified: bool = field(default_factory=lambda: _bool("LIVE_SIGNING_VERIFIED", False))

    @property
    def live(self) -> bool:
        """Check if authenticated client initialization is configured.
        
        Returns:
            True if not dry_run, a private key is configured, and signing setup
            has been verified. This does not authorize order posting: the separate
            live-control layer and unbuilt endpoint remain hard blocks.
        """
        # Historical name retained for compatibility. It means authenticated mode,
        # not permission to submit an exchange order.
        return (not self.dry_run) and bool(self.private_key) and self.live_signing_verified


# Single process-wide config instance.
SETTINGS = Settings()
