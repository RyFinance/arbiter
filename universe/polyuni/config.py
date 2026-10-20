"""Configuration — load from .env / environment, expose a single frozen Settings object.

COLD PATH only (read once at startup). Nothing here runs in the trading loop.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Optional

from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _float(name: str, default: float) -> float:
    v = os.getenv(name)
    return float(v) if v is not None and v != "" else default


def _optional_float(name: str) -> Optional[float]:
    v = os.getenv(name)
    return float(v) if v is not None and v.strip() != "" else None


def _int(name: str, default: int) -> int:
    v = os.getenv(name)
    return int(v) if v is not None and v != "" else default


def _list(name: str, default: List[str]) -> List[str]:
    v = os.getenv(name)
    if not v:
        return list(default)
    return [s.strip() for s in v.split(",") if s.strip()]


@dataclass(frozen=True)
class Settings:
    """Frozen configuration dataclass loaded from environment variables."""

    # endpoints
    gamma_base_url: str = os.getenv("GAMMA_BASE_URL", "https://gamma-api.polymarket.com")
    clob_base_url: str = os.getenv("CLOB_BASE_URL", "https://clob.polymarket.com")
    clob_ws_url: str = os.getenv("CLOB_WS_URL", "wss://ws-subscriptions-clob.polymarket.com")

    # ---- market universe (replaces polymarket_arb's BTC-only window mode) ----
    market_whitelist: List[str] = field(default_factory=lambda: _list("MARKET_WHITELIST", []))
    min_liquidity_usd: float = field(default_factory=lambda: _float("MIN_LIQUIDITY_USD", 500.0))
    min_volume_24h_usd: float = field(default_factory=lambda: _float("MIN_VOLUME_24H_USD", 1000.0))
    max_spread_at_mid: float = field(default_factory=lambda: _float("MAX_SPREAD_AT_MID", 0.05))
    min_time_to_resolution_s: float = field(
        # A 300-second floor excluded a five-minute market by the time the first
        # discovery response arrived. Thirty seconds still avoids already-closing
        # books while allowing an early-window sequential entry.
        default_factory=lambda: _float("MIN_TIME_TO_RESOLUTION_S", 30.0)
    )
    # None = no cap.
    max_time_to_resolution_s: Optional[float] = field(
        default_factory=lambda: _optional_float("MAX_TIME_TO_RESOLUTION_S")
    )
    exclude_neg_risk: bool = field(default_factory=lambda: _bool("EXCLUDE_NEG_RISK", True))
    universe_rescan_interval_s: float = field(
        default_factory=lambda: _float("UNIVERSE_RESCAN_INTERVAL_S", 60.0)
    )
    universe_requalify_interval_s: float = field(
        default_factory=lambda: _float("UNIVERSE_REQUALIFY_INTERVAL_S", 60.0)
    )
    max_concurrent_markets: int = field(
        default_factory=lambda: _int("MAX_CONCURRENT_MARKETS", 50)
    )
    # Gamma's bulk /markets pagination omits the rapidly rotating crypto windows.
    # Resolve current+next BTC 5m/15m slugs explicitly so the early-entry strategy
    # can subscribe before or just after each boundary.
    include_btc_short_windows: bool = field(
        default_factory=lambda: _bool("INCLUDE_BTC_SHORT_WINDOWS", True)
    )

    # strategy
    # ``sequential`` is the practical default: acquire one discounted outcome
    # early in a volatile market, then wait a bounded amount of time for the
    # opposite outcome to become cheap enough that the completed pair has a
    # positive fee-adjusted edge. ``dual`` preserves the legacy simultaneous
    # YES+NO crossed-book detector for comparison/replay.
    strategy_mode: str = os.getenv("STRATEGY_MODE", "sequential").strip().lower()
    min_edge: float = field(default_factory=lambda: _float("MIN_EDGE", 0.01))
    order_size: float = field(default_factory=lambda: _float("ORDER_SIZE", 10.0))

    # ---- Sequential two-leg strategy ----
    sequential_entry_window_s: float = field(
        default_factory=lambda: _float("SEQUENTIAL_ENTRY_WINDOW_S", 120.0)
    )
    sequential_volatility_lookback_s: float = field(
        default_factory=lambda: _float("SEQUENTIAL_VOLATILITY_LOOKBACK_S", 20.0)
    )
    sequential_min_local_move: float = field(
        default_factory=lambda: _float("SEQUENTIAL_MIN_LOCAL_MOVE", 0.015)
    )
    sequential_min_samples: int = field(
        default_factory=lambda: _int("SEQUENTIAL_MIN_SAMPLES", 3)
    )
    sequential_near_even_band: float = field(
        default_factory=lambda: _float("SEQUENTIAL_NEAR_EVEN_BAND", 0.08)
    )
    sequential_first_leg_max_price: float = field(
        default_factory=lambda: _float("SEQUENTIAL_FIRST_LEG_MAX_PRICE", 0.48)
    )
    sequential_second_leg_max_price: float = field(
        default_factory=lambda: _float("SEQUENTIAL_SECOND_LEG_MAX_PRICE", 0.48)
    )
    sequential_second_leg_timeout_s: float = field(
        default_factory=lambda: _float("SEQUENTIAL_SECOND_LEG_TIMEOUT_S", 30.0)
    )
    sequential_min_net_edge: float = field(
        default_factory=lambda: _float("SEQUENTIAL_MIN_NET_EDGE", 0.0025)
    )
    sequential_cooldown_s: float = field(
        default_factory=lambda: _float("SEQUENTIAL_COOLDOWN_S", 30.0)
    )
    strategy_snapshot_path: str = os.getenv(
        "STRATEGY_SNAPSHOT_PATH", "state/strategy_snapshot.json"
    )

    # Official Polymarket taker-fee curve parameters. Market-specific Gamma
    # feeSchedule values override these; the crypto/default rates are only a
    # conservative fallback when Gamma omits fee metadata.
    crypto_taker_fee_rate: float = field(
        default_factory=lambda: _float("CRYPTO_TAKER_FEE_RATE", 0.07)
    )
    default_taker_fee_rate: float = field(
        default_factory=lambda: _float("DEFAULT_TAKER_FEE_RATE", 0.05)
    )

    # Fresh, sibling-produced LightGBM regime signal. This model is BTC-only and
    # is used only to adjust the *observed local-volatility* threshold for BTC
    # Up/Down first-leg entries. It never chooses direction and never gates an
    # already-open hedge/exit.
    lightgbm_regime_path: str = os.getenv(
        "LIGHTGBM_REGIME_PATH", "../btc5m/state/regime_active.json"
    )
    lightgbm_regime_max_age_s: float = field(
        default_factory=lambda: _float("LIGHTGBM_REGIME_MAX_AGE_S", 900.0)
    )
    lightgbm_min_confidence: float = field(
        # Keep aligned with polymarket_arb's deployed regime gate.
        default_factory=lambda: _float("POLYUNI_LIGHTGBM_MIN_CONFIDENCE", 0.45)
    )
    lightgbm_high_vol_multiplier: float = field(
        default_factory=lambda: _float("LIGHTGBM_HIGH_VOL_MULTIPLIER", 0.75)
    )
    lightgbm_low_vol_multiplier: float = field(
        default_factory=lambda: _float("LIGHTGBM_LOW_VOL_MULTIPLIER", 1.25)
    )

    # Slippage stress buffer applied ON TOP OF the entry edge when judging whether a
    # market's edge history survives. Must be > 0 or the stress test degenerates to
    # the entry-edge condition and can never fail — validated at startup (see
    # validate_startup_configuration in main.py); this was a known-missing check in
    # polymarket_arb, carried forward as a real check here, not just a comment.
    slippage_stress_buffer: float = field(default_factory=lambda: _float("SLIPPAGE_STRESS_BUFFER", 0.01))

    # ---- Quote freshness gating ----
    max_quote_age_ms: int = field(default_factory=lambda: _int("MAX_QUOTE_AGE_MS", 500))
    cross_leg_settle_ms: float = field(default_factory=lambda: _float("CROSS_LEG_SETTLE_MS", 10.0))
    # No NEW entries in the final `no_entry_seconds` before a market's own end_time --
    # spreads tend to whip right before resolution becomes certain. This is distinct
    # from min_time_to_resolution_s (the universe filter's much coarser bar for
    # tracking a market at all): this is the tight, per-fire gate applied to an
    # already-ACTIVE market's live decision, not a discovery-time filter.
    no_entry_seconds: float = field(default_factory=lambda: _float("NO_ENTRY_SECONDS", 5.0))

    # ---- Partial-fill / unhedged-leg protection ----
    max_leg_gap_ms: int = field(default_factory=lambda: _int("MAX_LEG_GAP_MS", 300))
    partial_hedge_min_edge: float = field(default_factory=lambda: _float("PARTIAL_HEDGE_MIN_EDGE", 0.0))

    # auth
    private_key: str = os.getenv("PRIVATE_KEY", "") or ""
    proxy_wallet: str = os.getenv("PROXY_WALLET", "") or ""
    polymarket_api_key: str = os.getenv("POLYMARKET_API_KEY", "") or ""
    polymarket_api_secret: str = os.getenv("POLYMARKET_API_SECRET", "") or ""
    polymarket_api_passphrase: str = os.getenv("POLYMARKET_API_PASSPHRASE", "") or ""

    # ---- On-chain read-only status (COLD PATH only; no transaction method exists) ----
    polygon_rpc_url: str = os.getenv("POLYGON_RPC_URL", "https://polygon-rpc.com") or ""
    approval_max_gas_price_gwei: float = field(
        default_factory=lambda: _float("APPROVAL_MAX_GAS_PRICE_GWEI", 80.0)
    )

    # transport
    use_ws: bool = field(default_factory=lambda: _bool("USE_WS", True))
    poll_interval_s: float = field(default_factory=lambda: _float("POLL_INTERVAL_S", 0.25))

    # Note: PAPER_FILL_SOURCE is deliberately NOT a Settings field. It's resolved and
    # validated by _resolve_paper_fill_source() in clients/trading_client.py (tested
    # there against env-var isolation directly), the same design polymarket_arb's fix
    # used -- duplicating it here as a second, unvalidated read of the same env var
    # would just be a second source of truth to drift out of sync with the first.

    # risk
    max_position_per_market: float = field(default_factory=lambda: _float("MAX_POSITION_PER_MARKET", 200.0))
    max_total_capital: float = field(default_factory=lambda: _float("MAX_TOTAL_CAPITAL", 1000.0))
    min_liquidity_per_side: float = field(default_factory=lambda: _float("MIN_LIQUIDITY_PER_SIDE", 5.0))
    stale_book_timeout_s: float = field(default_factory=lambda: _float("STALE_BOOK_TIMEOUT_S", 5.0))
    daily_kill_switch_usd: float = field(default_factory=lambda: _float("DAILY_KILL_SWITCH_USD", 500.0))
    partial_loss_cap_usd: float = field(default_factory=lambda: _float("PARTIAL_LOSS_CAP_USD", 25.0))
    emergency_stop_file: str = os.getenv("EMERGENCY_STOP_FILE", "state/EMERGENCY_STOP")

    # observability
    log_jsonl: str = os.getenv("LOG_JSONL", "state/bot.jsonl")
    log_level: str = os.getenv("LOG_LEVEL", "INFO")
    # How often the running bot publishes a PnL snapshot (state/pnl_snapshot.json +
    # dashboard WS push). polymarket_arb only logged PnL once, at shutdown, so a
    # "live" dashboard showed nothing until the bot stopped -- fixed here by making
    # this a real periodic publish, not a one-shot summary.
    pnl_publish_interval_s: float = field(default_factory=lambda: _float("PNL_PUBLISH_INTERVAL_S", 5.0))

    # ---- crash recovery & alerting (OPTIONAL, config-gated) ----
    alert_webhook_url: str = os.getenv("ALERT_WEBHOOK_URL", "") or ""
    alert_heartbeat_file: str = os.getenv("ALERT_HEARTBEAT_FILE", "state/HEARTBEAT")
    alert_heartbeat_timeout_s: float = field(
        default_factory=lambda: _float("ALERT_HEARTBEAT_TIMEOUT_S", 10.0)
    )

    # safety
    dry_run: bool = field(default_factory=lambda: _bool("DRY_RUN", True))
    paper_trade: bool = field(default_factory=lambda: _bool("PAPER_TRADE", False))
    live_signing_verified: bool = field(default_factory=lambda: _bool("LIVE_SIGNING_VERIFIED", False))

    # ---- LIVE/PAPER control plane (control-plane only -- see live_control.py) ----
    # There is no real exchange order-submission code path in polyuni. LIVE_CONTROL_*
    # governs an audited operator-intent state machine only: arming LIVE changes what
    # the dashboard displays and what every order-submission boundary logs, never
    # whether a real order can be sent. This is intentional, not a placeholder for a
    # future flip -- see README.md "Safety status".
    live_control_state_path: str = os.getenv("LIVE_CONTROL_STATE_PATH", "state/live_control.json")
    live_control_audit_path: str = os.getenv("LIVE_CONTROL_AUDIT_PATH", "state/live_control_audit.jsonl")

    @property
    def live(self) -> bool:
        """True if authenticated client initialization is configured.

        Does not authorize order posting: the live-control layer and the fact that
        no exchange order-submission code exists remain independent hard blocks.
        """
        return (not self.dry_run) and bool(self.private_key) and self.live_signing_verified


# Single process-wide config instance.
SETTINGS = Settings()
