"""main.py — startup sequence + wiring. CORE ORCHESTRATION.

Order of operations (READ THIS — it encodes the cold/hot split):
  COLD PATH
    1. build persistent httpx session (Gamma + reused for CLOB REST fallback)
    2. resolve target markets:
         - window_mode ON  -> deterministic rolling BTC "Up or Down" series (FIX #3)
         - window_mode OFF -> original broad Gamma discovery path
    3. build MarketState map + register precomputed order templates
    4. build token->state index for O(1) WS routing
    5. warm the signing client (TradingClient ctor) so first order isn't slow
    6. preload balance (placeholder; real balances via CLOB/chain in live mode)
  HOT PATH
    7. start PriceFeed (WS preferred, REST poll fallback) -> arb_engine -> execution
    8. background: window tracker (rotate + pre-resolve next window, FIX #3/FIX #5)
  SHUTDOWN
    9. on signal: stop feed, stop tracker, close sessions, print metrics summary
"""
from __future__ import annotations

import asyncio
import os
import signal
import sys
from urllib.parse import urlparse

import httpx
from eth_account import Account
from eth_utils import to_checksum_address

from .alerting import build_alerter, build_heartbeat
from .arb_engine import ArbEngine
from .cache import is_fresh, load, save
from .clients.gamma_client import discover_markets
from .clients.trading_client import TradingClient
from .config import LSE_API_PLACEHOLDER_KEY, SETTINGS
from .execution import Execution
from .logger import BotLogger, log
from .metrics import Metrics
from .order_ledger import OrderLedger
from .price_feed import PriceFeed
from .risk import RiskState
from .window_tracker import WindowTracker


def validate_startup_configuration(settings=SETTINGS) -> list[str]:
    """Return actionable errors that must be fixed before the bot starts."""
    errors: list[str] = []
    if bool(settings.dry_run) == bool(settings.paper_trade):
        errors.append(
            "select exactly one supported mode: DRY_RUN=true/PAPER_TRADE=false "
            "or DRY_RUN=false/PAPER_TRADE=true"
        )
    if settings.paper_trade and not settings.live:
        errors.append(
            "paper mode requires PRIVATE_KEY and LIVE_SIGNING_VERIFIED=true for "
            "authenticated read-only market access; order posting remains disabled"
        )
    private_key = str(getattr(settings, "private_key", "") or "")
    if settings.paper_trade:
        try:
            Account.from_key(private_key)
        except Exception as exc:
            errors.append(f"PRIVATE_KEY must be a valid paper-mode signer: {exc}")
    proxy_wallet = str(getattr(settings, "proxy_wallet", "") or "")
    if proxy_wallet:
        try:
            to_checksum_address(proxy_wallet)
        except ValueError as exc:
            errors.append(f"PROXY_WALLET must be a valid address: {exc}")
    for name, required_scheme in (
        ("gamma_base_url", "https"),
        ("clob_base_url", "https"),
        ("clob_ws_url", "wss"),
        ("polygon_rpc_url", "https"),
    ):
        parsed = urlparse(str(getattr(settings, name, "")))
        if parsed.scheme != required_scheme or not parsed.netloc:
            errors.append(f"{name} must be a valid {required_scheme} URL")
    alert_webhook_url = str(getattr(settings, "alert_webhook_url", "") or "")
    if alert_webhook_url:
        parsed_webhook = urlparse(alert_webhook_url)
        loopback_http = (
            parsed_webhook.scheme == "http"
            and parsed_webhook.hostname in {"127.0.0.1", "localhost", "::1"}
        )
        if not parsed_webhook.netloc or not (
            parsed_webhook.scheme == "https" or loopback_http
        ):
            errors.append(
                "alert_webhook_url must use https; http is allowed only for loopback"
            )
    for name in (
        "order_size",
        "max_quote_age_ms",
        "cross_leg_settle_ms",
        "max_position_per_market",
        "max_total_capital",
    ):
        if float(getattr(settings, name, 0.0)) <= 0.0:
            errors.append(f"{name} must be positive")
    if not 0.0 <= float(settings.min_edge) < 1.0:
        errors.append("min_edge must be in [0, 1)")
    if float(getattr(settings, "max_leg_gap_ms", -1.0)) < 0.0:
        errors.append("max_leg_gap_ms must be non-negative")
    if float(settings.max_position_per_market) > float(settings.max_total_capital):
        errors.append("max_position_per_market cannot exceed max_total_capital")
    credentials = (
        str(getattr(settings, "polymarket_api_key", "") or ""),
        str(getattr(settings, "polymarket_api_secret", "") or ""),
        str(getattr(settings, "polymarket_api_passphrase", "") or ""),
    )
    if any(credentials) and not all(credentials):
        errors.append(
            "POLYMARKET_API_KEY/SECRET/PASSPHRASE must be all present or all blank"
        )
    return errors


def _warn_placeholder_lse_key():
    """Loud startup signal when LSE_API_KEY is unset or still the placeholder.

    The placeholder is NOT a credential (LSE returns 401); the macro calendar
    falls back to the SPA anon-key and LSE price history degrades, but the
    operator must hear about it at startup rather than discover a silent
    "lse_live_..." string in SETTINGS later.
    """
    key = SETTINGS.lse_api_key
    if not key or key == LSE_API_PLACEHOLDER_KEY:
        log.warning(
            f"LSE_API_KEY is {'unset' if not key else 'the placeholder sentinel'}: "
            f"macro calendar will use the SPA anon-key fallback and live LSE price "
            f"history will 401. Set the real key via the LSE_API_KEY environment "
            f"variable (docs/lse_api_key_acquisition.md) before production trading."
        )


def _setup_logger():
    global log
    log_instance = BotLogger(jsonl_path=SETTINGS.log_jsonl, level=SETTINGS.log_level)
    log = log_instance
    # Modules import ``log`` by value, so updating only this module silently
    # drops transport, execution, and alert events from the configured JSONL log.
    # Keep all process-local users on the one audit logger.
    from . import logger as logger_module

    logger_module.log = log_instance
    for module_name in (
        "polymarket_arb.alerting",
        "polymarket_arb.arb_engine",
        "polymarket_arb.execution",
        "polymarket_arb.price_feed",
        "polymarket_arb.risk",
        "polymarket_arb.kronos_session",
        "polymarket_arb.window_tracker",
        "polymarket_arb.cache",
        "polymarket_arb.clients.clob_client",
        "polymarket_arb.clients.trading_client",
        "polymarket_arb.clients.gamma_client",
        "polymarket_arb.clients.lse_client",
        "polymarket_arb.scan",
        "polymarket_arb.metrics",
    ):
        module = sys.modules.get(module_name)
        if module is not None and hasattr(module, "log"):
            module.log = log_instance


async def build_broad_states() -> dict:
    """COLD PATH (legacy): Gamma discovery -> MarketState map."""
    if not SETTINGS.force_cache_rebuild:
        cached = load()
        if cached.markets and is_fresh(cached):
            log.info(f"loaded {len(cached.markets)} markets from cache")
            return {m.market_id: m.to_state() for m in cached.markets}
    async with httpx.AsyncClient(base_url=SETTINGS.gamma_base_url, timeout=20.0) as g:
        metas = await discover_markets(g)
    if not metas:
        cached = load()
        if cached.markets:
            return {m.market_id: m.to_state() for m in cached.markets}
        return {}
    save(metas)
    return {m.market_id: m.to_state() for m in metas}


async def main():
    """Boot the live/paper bot: derive auth, mount the price feed, and run the loop.

    Cold path: settings load, L1 CLOB API-key derivation, trading-approval
    readiness (gated by PAPER_TRADE), then hot loop assembly — ``PriceFeed`` +
    ``ArbEngine`` + ``Execution`` wired so every WS tick flows
    feed → engine gate → execution fire → deferred resolution. Honors the
    ``PAPER_TRADE`` impenetrable safety barrier: even with DRY_RUN=False and a
    configured private key, submit_order synthesizes fills from the live book
    and NEVER broadcasts a real signed txn under paper-trade.
    """
    startup_errors = validate_startup_configuration()
    if startup_errors:
        details = "\n - ".join(startup_errors)
        raise RuntimeError(
            "Startup configuration invalid. Run scripts/setup_wizard.py --check "
            f"and fix:\n - {details}"
        )
    _setup_logger()
    # TASK 5: loud startup signal when the LSE calendar API key is missing or
    # still the placeholder sentinel. The placeholder is NOT a credential (the
    # LSE API returns 401); page_agent_runner will fall back to the SPA anon-key
    # for the calendar endpoint, and lse_client/kronos degrade to their own
    # documented fallbacks — but the operator must hear about it at startup,
    # not discover a silent "lse_live_..." string in SETTINGS later.
    _warn_placeholder_lse_key()
    # PAPER_TRADE config precedence guard:
    # PAPER_TRADE=true means "simulate fills, never broadcast real orders."
    # The auth flow (API key derivation) can still run real, but trading-approval
    # on-chain transactions (ERC-20 approve + ERC-1155 setApprovalForAll) must NEVER
    # be attempted when PAPER_TRADE=true, even if DRY_RUN=false and a private key
    # is configured. This is an impenetrable safety barrier: there is NO path where
    # PAPER_TRADE=true results in a real signed transaction reaching Polygon RPC.
    #
    # Config precedence:
    #   DRY_RUN=true  -> live=False, no auth, no approvals, synthetic orders
    #   DRY_RUN=false + PAPER_TRADE=true + live=True -> real auth (API key derivation),
    #        paper fill simulation, NO on-chain approval transactions (signed locally, not broadcast)
    #   DRY_RUN=false + PAPER_TRADE=false -> rejected at startup (unsupported)
    #
    # The trading_client.py _paper_trade flag prevents order broadcasting.
    # The setup_trading_approvals() _send_approval_txn() also checks _paper_trade
    # and skips the `_rpc_send_raw_tx` broadcast call, signing only locally.
    # Read-only RPC calls (eth_getTransactionCount, eth_gasPrice) are still made
    # for nonce/gas estimation but do NOT constitute real transactions.
    if SETTINGS.paper_trade and not SETTINGS.dry_run and SETTINGS.live:
        log.info(
            "PAPER_TRADE=true with DRY_RUN=false: real auth flow will run, "
            "order submission will be SIMULATED, on-chain approval txns will be "
            "signed locally but NOT broadcast. This is the intended paper-trade path."
        )
    elif SETTINGS.paper_trade and SETTINGS.dry_run:
        log.info(
            "PAPER_TRADE=true with DRY_RUN=true: auth flow is DRY_RUN (no real "
            "credentials), orders are synthetic. Both paper_trade and dry_run active."
        )
    log.info(f"polymarket arb bot starting | dry_run={SETTINGS.dry_run} | live={SETTINGS.live} | paper_trade={SETTINGS.paper_trade}")
    if SETTINGS.paper_trade:
        _fill_source = os.getenv("PAPER_FILL_SOURCE", "rest_book").strip().lower()
        log.info(f"paper_fill_source={_fill_source}")
    log.info(f"strategy: submit if YES+NO < {1.0 - SETTINGS.min_edge:.2f}")
    log.info(
        f"fixes active: freshness_gate={SETTINGS.max_quote_age_ms}ms "
        f"cross_leg_settle={SETTINGS.cross_leg_settle_ms}ms "
        f"leg_gap={SETTINGS.max_leg_gap_ms}ms no_entry={SETTINGS.no_entry_seconds}s "
        f"window_mode={SETTINGS.window_mode} cross_leg_pair_barrier=True"
    )

    # ---- TASK B: crash recovery & alerting (OPTIONAL, config-gated) ----
    # build_alerter / build_heartbeat return no-op instances when ALERT_WEBHOOK_URL
    # is empty — alerting is never required, the bot runs fine without it.
    alerter = build_alerter()
    heartbeat = build_heartbeat()
    if alerter.enabled:
        log.info(f"alerting ENABLED via webhook (heartbeat={SETTINGS.alert_heartbeat_file} "
                 f"timeout={SETTINGS.alert_heartbeat_timeout_s}s)")
    else:
        log.info("alerting disabled (ALERT_WEBHOOK_URL empty)")

    # ---- COLD PATH: forecast session volatility regime (Kronos) ----
    # Runs once at startup to adjust NO_ENTRY_SECONDS and MAX_QUOTE_AGE_MS
    # based on the BTC volatility regime for the upcoming session.
    try:
        from .kronos_session import forecast_session_regime, apply_regime_to_settings
        regime = await forecast_session_regime()
        apply_regime_to_settings(regime)
    except Exception as e:
        log.warning(f"regime forecast failed: {e!r} — using default parameters")

    # ---- COLD PATH: resolve target markets ----
    if SETTINGS.window_mode:
        states = {}  # window tracker populates these as windows go live
        log.info(
            f"WINDOW MODE: targeting {SETTINGS.window_asset}-updown-{SETTINGS.window_timeframe} "
            f"series ({SETTINGS.window_seconds}s windows)"
        )
    else:
        states = await build_broad_states()
        if not states:
            log.error("no markets to track; exiting")
            return "no_markets_available"

    metrics = Metrics()
    risk = RiskState(balance=SETTINGS.max_total_capital, alerter=alerter)  # placeholder preload
    client = TradingClient()  # warms signing client (HOT readiness)
    ledger = OrderLedger("state/order_ledger.jsonl")
    recovery = await ledger.reconcile(client, risk)
    if recovery.restored_amount or recovery.manual_review:
        log.warning(
            f"order-ledger recovery: restored=${recovery.restored_amount:.2f} "
            f"resolved={len(recovery.resolved)} manual_review={recovery.manual_review}"
        )
    execution = Execution(client, risk, metrics, alerter=alerter, ledger=ledger)
    for st in states.values():
        execution.register(st)

    # ---- HOT PATH wiring ----
    arb = ArbEngine(on_fire=execution.execute_dual_buy, metrics=metrics)
    # TASK 4: a fire that dies with zero exposure (both legs cancelled at the
    # no-fill timeout, or a submit failure) must clear the engine's per-market
    # debounce — otherwise a book returning to the same combined price never
    # re-fires and the arb is silently lost.
    execution.set_invalidator(arb.invalidate)

    # FIX #6: combine the tick callback so a price update both (a) runs the arb gate
    # and (b) drives the deferred non-blocking fill-resolution cycle. execute_dual_buy
    # returns immediately after submission; on_price_update probes pending fills on
    # the NEXT tick instead of blocking inside the fire coroutine.
    #
    # NOTE: _on_tick MUST be synchronous — PriceFeed calls self.on_update(st) directly
    # (no await). The previous `async def` silently dropped the coroutine. The fire
    # path itself is already async (on_fire schedules a task); the tick callback only
    # does cheap sync work + schedules.
    # TASK B: track emergency-stop transitions so we fire exactly one alert when it
    # is first triggered (not on every tick while it persists).
    _emergency_alerted = False

    def _on_tick(st):
        nonlocal _emergency_alerted
        # TASK B: check emergency stop on every tick (cheap file existence test).
        if risk.emergency_triggered():
            if not _emergency_alerted and alerter is not None:
                alerter.alert(
                    "emergency_stop",
                    f"Emergency stop file detected: {SETTINGS.emergency_stop_file}",
                    stop_file=SETTINGS.emergency_stop_file,
                )
                _emergency_alerted = True
        else:
            _emergency_alerted = False
        arb.evaluate(st)                # hot-path: gate + schedule execute_dual_buy task
        execution.on_price_update(st)   # FIX #6: kick deferred fill checks
        heartbeat.touch()               # TASK B: write heartbeat on every tick
    feed = PriceFeed(states, on_update=_on_tick, alerter=alerter)
    feed.build_token_index()

    stop = asyncio.Event()
    # The hot loop below only ever exits via stop.set(), which only ever
    # happens from _sig() — so a clean (non-exception) return from main()
    # always means a deliberate SIGINT/SIGTERM (dashboard Stop, Ctrl+C), not
    # a crash. Callers that race main() against a duration timeout (e.g. the
    # paper-session harness) need this to tell "stopped on purpose" apart
    # from "exited unexpectedly."
    shutdown_reason = "signal"

    def _sig(*_):
        log.info("shutdown signal received")
        stop.set()

    loop = asyncio.get_running_loop()
    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(s, _sig)
        except (NotImplementedError, ValueError):
            pass

    await feed.start()

    # ---- WINDOW MODE: start the tracker that rotates + pre-resolves windows (FIX #3/#5) ----
    tracker: object = None
    if SETTINGS.window_mode:
        shared_session = httpx.AsyncClient(base_url=SETTINGS.gamma_base_url, timeout=20.0)

        async def _on_window_ready(meta):
            # Turn the resolved MarketMeta into a live MarketState, stamp window bounds,
            # register the execution template, and subscribe the price feed to it.
            st = meta.to_state()
            st.window_start = meta.window_start
            st.window_close = meta.window_close
            states[meta.market_id] = st
            execution.register(st)
            # C2/FIX #3: add_market is async (builds token->state index + awaits the WS
            # resubscribe). MUST be awaited or rotated windows get no book data.
            await feed.add_market(st)
            log.info(f"registered window market {meta.market_id} tokens for live feed")

        def _state_lookup(market_id):
            return states.get(market_id)

        # HIGH: free the settled window's deployed capital so total_deployed does not
        # grow without bound and eventually trip the max-capital precheck.
        def _on_window_settled(market_id):
            execution.release_window(market_id)
            states.pop(market_id, None)  # LOW: bound the window-mode states dict

        tracker = WindowTracker(shared_session, _on_window_ready, _state_lookup,
                                alerter=alerter,
                                on_window_settled=_on_window_settled)
        await tracker.start()

    # background cache refresh (cold path, isolated) — only in legacy broad mode.
    if not SETTINGS.window_mode:
        async def _refresh():
            async with httpx.AsyncClient(base_url=SETTINGS.gamma_base_url, timeout=20.0) as g:
                metas = await discover_markets(g)
                if metas:
                    save(metas)
        refresh_task = asyncio.create_task(_refresh(), name="cache-refresh")
        refresh_task.add_done_callback(
            lambda t: log.error(f"cache-refresh failed: {t.exception()!r}")
            if (not t.cancelled() and t.exception() is not None) else None
        )

    # ---- Background: page-agent macro event monitoring ----
    page_agent = None
    try:
        # scrapers is at project root, not inside the package
        import sys
        sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
        from scrapers.page_agent_runner import PageAgentRunner
        page_agent = PageAgentRunner(refresh_interval_s=300.0)
        page_agent.start()
    except Exception as e:
        log.warning(f"page-agent startup failed: {e!r}")

    log.info("HOT PATH live — watching books")
    try:
        while not stop.is_set():
            await asyncio.sleep(1.0)
    finally:
        if page_agent is not None:
            page_agent.stop()
        if tracker is not None:
            tracker.stop()
        await feed.aclose()
        if SETTINGS.window_mode and shared_session is not None:
            await shared_session.aclose()
        await client.aclose()
        log.info(
            "PAIR_COHERENCE_SUMMARY "
            f"coherent_evaluations={feed.coherent_pair_evaluations} "
            f"incomplete_suppressions={feed.incomplete_pair_suppressions} "
            f"settle_ms={SETTINGS.cross_leg_settle_ms}"
        )
        log.info("session metrics: " + metrics.summary())
        log.close()
        # TASK B: signal clean shutdown to the external monitor so it doesn't
        # fire a false crash alert on graceful exit.
        heartbeat.stop()
        alerter.close()
    return shutdown_reason


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
