"""main.py — startup sequence + wiring. CORE ORCHESTRATION.

Order of operations:
  COLD PATH
    1. validate config, set up logging, build alerter/heartbeat
    2. build persistent httpx session for Gamma discovery
    3. run one universe sync pass so the bot starts with an initial ACTIVE set
    4. warm the signing client (TradingClient ctor) so first order isn't slow
    5. reconcile the order ledger (crash recovery)
  HOT PATH
    6. start PriceFeed (WS preferred, REST poll fallback) -> arb_engine -> execution
    7. background: universe sync loop (discover/qualify/promote/close markets
       on UNIVERSE_RESCAN_INTERVAL_S, replacing polymarket_arb's window rotation)
  SHUTDOWN
    8. on signal: stop feed, stop sync loop, close sessions, print metrics summary
"""
from __future__ import annotations

import asyncio
import signal
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx
from eth_account import Account
from eth_utils import to_checksum_address

from .alerting import build_alerter, build_heartbeat
from .arb_engine import ArbEngine
from .clients.trading_client import TradingClient
from .config import SETTINGS
from .execution import Execution
from .logger import log
from .market_universe import MarketUniverse
from .metrics import Metrics
from .order_ledger import OrderLedger
from .price_feed import PriceFeed
from .risk import RiskState
from .sequential_engine import SequentialPairEngine


def validate_startup_configuration(settings=SETTINGS) -> list[str]:
    """Return actionable errors that must be fixed before the bot starts."""
    errors: list[str] = []
    if getattr(settings, "live_execution", False):
        from .live_control import live_endpoint_enabled
        if settings.dry_run or settings.paper_trade:
            errors.append("LIVE_EXECUTION=true requires DRY_RUN=false and PAPER_TRADE=false")
        if not live_endpoint_enabled():
            errors.append("live trading is not enabled: set ARBITER_LIVE_TRADING=enabled in .env")
        if getattr(settings, "venue", "polymarket") == "kalshi":
            from .venues import CONNECTORS
            if CONNECTORS["kalshi"].credentials() is None:
                errors.append("live Kalshi trading needs a linked Kalshi API key")
            if float(getattr(settings, "order_size", 0)) != int(getattr(settings, "order_size", 0)):
                errors.append("Kalshi orders need a whole-number ORDER_SIZE (contracts)")
            return errors
        if not settings.live:
            errors.append("live trading requires PRIVATE_KEY and LIVE_SIGNING_VERIFIED=true")
        try:
            Account.from_key(str(getattr(settings, "private_key", "") or ""))
        except Exception as exc:
            errors.append(f"PRIVATE_KEY must be a valid signer: {exc}")
        if getattr(settings, "polymarket_signature_type", "eoa") not in {"eoa", "proxy", "safe"}:
            errors.append("POLYMARKET_SIGNATURE_TYPE must be eoa, proxy or safe")
        return errors
    if getattr(settings, "venue", "polymarket") not in {"polymarket", "kalshi"}:
        errors.append("VENUE must be polymarket or kalshi")
    if getattr(settings, "venue", "polymarket") == "kalshi" and settings.paper_trade:
        errors.append("Kalshi paper trading runs as DRY_RUN=true (public books, simulated fills)")
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
        # Both of these were KNOWN-MISSING in polymarket_arb despite a code comment
        # admitting SLIPPAGE_STRESS_BUFFER "must be > 0 or the stress test
        # degenerates to the entry-edge condition and can never fail" -- a .env
        # typo of 0 booted cleanly there and silently disabled the gate. Carried
        # forward as a real check here, not left as a comment-only warning.
        "slippage_stress_buffer",
        "min_liquidity_per_side",
    ):
        if float(getattr(settings, name, 0.0)) <= 0.0:
            errors.append(f"{name} must be positive")
    if not 0.0 <= float(settings.min_edge) < 1.0:
        errors.append("min_edge must be in [0, 1)")
    if float(getattr(settings, "max_leg_gap_ms", -1.0)) < 0.0:
        errors.append("max_leg_gap_ms must be non-negative")
    if float(settings.max_position_per_market) > float(settings.max_total_capital):
        errors.append("max_position_per_market cannot exceed max_total_capital")
    if int(getattr(settings, "max_concurrent_markets", 0)) <= 0:
        errors.append("max_concurrent_markets must be positive")
    strategy_mode = str(getattr(settings, "strategy_mode", "")).lower()
    if strategy_mode not in {"sequential", "dual"}:
        errors.append("strategy_mode must be 'sequential' or 'dual'")
    for name in (
        "sequential_entry_window_s",
        "sequential_volatility_lookback_s",
        "sequential_second_leg_timeout_s",
        "sequential_cooldown_s",
        "lightgbm_regime_max_age_s",
        "lightgbm_high_vol_multiplier",
        "lightgbm_low_vol_multiplier",
    ):
        if float(getattr(settings, name, 0.0)) <= 0.0:
            errors.append(f"{name} must be positive")
    if int(getattr(settings, "sequential_min_samples", 0)) < 2:
        errors.append("sequential_min_samples must be at least 2")
    if not 0.0 < float(getattr(settings, "sequential_near_even_band", 0.0)) <= 0.5:
        errors.append("sequential_near_even_band must be in (0, 0.5]")
    for name in ("sequential_first_leg_max_price", "sequential_second_leg_max_price"):
        if not 0.0 < float(getattr(settings, name, 0.0)) < 1.0:
            errors.append(f"{name} must be in (0, 1)")
    if float(getattr(settings, "sequential_min_local_move", -1.0)) < 0.0:
        errors.append("sequential_min_local_move must be non-negative")
    if float(getattr(settings, "sequential_min_net_edge", -1.0)) < 0.0:
        errors.append("sequential_min_net_edge must be non-negative")
    for name in ("crypto_taker_fee_rate", "default_taker_fee_rate"):
        if not 0.0 <= float(getattr(settings, name, -1.0)) < 1.0:
            errors.append(f"{name} must be in [0, 1)")
    if not 0.0 <= float(getattr(settings, "lightgbm_min_confidence", -1.0)) <= 1.0:
        errors.append("lightgbm_min_confidence must be in [0, 1]")
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


async def main():
    """Boot the paper bot: derive auth, mount the price feed, and run the loop.

    Cold path: settings load, L1 CLOB API-key derivation, trading-approval
    readiness (gated by PAPER_TRADE), then hot loop assembly -- PriceFeed +
    ArbEngine + Execution wired so every WS tick flows feed -> engine gate ->
    execution fire -> deferred resolution, across every ACTIVE market
    market_universe.py has promoted (not one rotating BTC window).
    """
    startup_errors = validate_startup_configuration()
    if startup_errors:
        details = "\n - ".join(startup_errors)
        raise RuntimeError(f"Startup configuration invalid:\n - {details}")

    log.configure(jsonl_path=SETTINGS.log_jsonl, level=SETTINGS.log_level)

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
    log.info(f"polyuni bot starting | dry_run={SETTINGS.dry_run} | live={SETTINGS.live} | paper_trade={SETTINGS.paper_trade}")
    if SETTINGS.paper_trade:
        from .clients.trading_client import _resolve_paper_fill_source
        log.info(f"paper_fill_source={_resolve_paper_fill_source()}")
    if SETTINGS.strategy_mode == "sequential":
        log.info(
            f"strategy=sequential venue={SETTINGS.venue} "
            f"mode={'live' if SETTINGS.live_execution else 'dry_run' if SETTINGS.dry_run else 'paper'} | "
            "first leg early+near-even+volatile; "
            f"second leg fee-adjusted net_edge>={SETTINGS.sequential_min_net_edge:.4f} "
            f"timeout={SETTINGS.sequential_second_leg_timeout_s:.1f}s"
        )
    else:
        log.info(f"strategy=dual: submit if YES+NO < {1.0 - SETTINGS.min_edge:.2f}")
    log.info(
        f"gates active: freshness={SETTINGS.max_quote_age_ms}ms "
        f"cross_leg_settle={SETTINGS.cross_leg_settle_ms}ms "
        f"leg_gap={SETTINGS.max_leg_gap_ms}ms no_entry={SETTINGS.no_entry_seconds}s "
        f"max_concurrent_markets={SETTINGS.max_concurrent_markets}"
    )

    alerter = build_alerter()
    heartbeat = build_heartbeat()
    if alerter.enabled:
        log.info(f"alerting ENABLED via webhook (heartbeat={SETTINGS.alert_heartbeat_file} "
                 f"timeout={SETTINGS.alert_heartbeat_timeout_s}s)")
    else:
        log.info("alerting disabled (ALERT_WEBHOOK_URL empty)")

    # ---- COLD PATH: initial universe sync so the bot starts with an ACTIVE set ----
    gamma_session = httpx.AsyncClient(base_url=SETTINGS.gamma_base_url, timeout=20.0)
    universe = MarketUniverse(SETTINGS)
    initial = await universe.sync(gamma_session)
    log.info(
        f"initial universe sync: discovered={initial.discovered} "
        f"active={len(universe.active_markets())} qualified={len(universe.qualified_markets())} "
        f"state_counts={initial.state_counts}"
    )
    states = {m.market_id: m.to_state() for m in universe.active_markets()}
    if not states:
        log.warning("no markets qualified on initial sync; the bot will keep scanning "
                    "on UNIVERSE_RESCAN_INTERVAL_S but is starting with zero coverage")

    metrics = Metrics()
    risk = RiskState(alerter=alerter)
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
    arb = None
    if SETTINGS.strategy_mode == "dual":
        arb = ArbEngine(on_fire=execution.execute_dual_buy, metrics=metrics)
        # A fire that dies with zero exposure (both legs cancelled at the no-fill
        # timeout, or a submit failure) must clear the engine's per-market debounce.
        execution.set_invalidator(arb.invalidate)
    sequential = SequentialPairEngine(execution, metrics, settings=SETTINGS)
    sequential.restore_manual_reviews(recovery.sequential_manual_review)

    _emergency_alerted = False

    def _check_emergency() -> None:
        nonlocal _emergency_alerted
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

    def _on_tick(st):
        _check_emergency()
        if arb is not None:
            arb.evaluate(st)
        execution.on_price_update(st)
        heartbeat.touch()

    def _on_leg_tick(st, token_id):
        _check_emergency()
        sequential.on_leg_update(st, token_id)
        heartbeat.touch()

    feed_class = PriceFeed
    if SETTINGS.venue == "kalshi":
        from .kalshi import KalshiPriceFeed as feed_class
    feed = feed_class(
        states,
        on_update=_on_tick,
        alerter=alerter,
        on_leg_update=_on_leg_tick if SETTINGS.strategy_mode == "sequential" else None,
    )
    feed.build_token_index()

    stop = asyncio.Event()

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

    universe_snapshot_path = "state/universe_snapshot.json"
    universe.publish(universe_snapshot_path)
    sequential.publish(SETTINGS.strategy_snapshot_path)

    # ---- Readiness gate ----
    # polymarket_arb's dashboard reported "running" the instant the bot
    # subprocess spawned, before the feed/WS/first-paired-book were actually
    # ready -- so a dashboard could show "running" while the bot was still
    # watching nothing. state/READY is written only once real readiness is
    # observed (WS healthy, or REST fallback has completed at least one
    # coherent pair evaluation), so botctl.py's status() can report a distinct
    # WARMING_UP state until this file exists.
    ready_path_obj = Path("state/READY")

    async def _readiness_watcher():
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline and not stop.is_set():
            ws_healthy = feed._feed is not None and feed._feed.healthy
            has_data = (
                feed.coherent_pair_evaluations > 0
                if SETTINGS.strategy_mode == "dual"
                else any(st.last_update_ts > 0 for st in states.values())
            )
            if ws_healthy or has_data:
                ready_path_obj.parent.mkdir(parents=True, exist_ok=True)
                ready_path_obj.write_text(str(time.time()))
                log.info(f"READY: feed is live (ws_healthy={ws_healthy} has_data={has_data})")
                return
            await asyncio.sleep(0.5)
        log.warning("readiness watcher timed out after 60s without a healthy feed")

    readiness_task = asyncio.create_task(_readiness_watcher(), name="readiness-watcher")

    # ---- Background: universe sync loop (replaces window rotation) ----
    async def _universe_sync_loop():
        while not stop.is_set():
            try:
                await asyncio.sleep(SETTINGS.universe_rescan_interval_s)
            except asyncio.CancelledError:
                return
            if stop.is_set():
                return
            try:
                result = await universe.sync(gamma_session)
            except Exception as e:
                log.error(f"universe sync failed: {e!r}")
                continue

            for market_id in result.promoted_to_active:
                meta = universe.markets[market_id]
                st = meta.to_state()
                states[market_id] = st
                execution.register(st)
                await feed.add_market(st)
                log.info(f"PROMOTED {market_id} to ACTIVE: {meta.question[:60]!r} "
                         f"vol24h=${meta.volume_24h_usd:,.0f}")

            closing_or_demoted = list(dict.fromkeys(
                result.newly_disqualified + result.demoted_from_active
            ))
            for market_id in closing_or_demoted:
                if market_id in states:
                    st = states[market_id]
                    flattened = await sequential.release_market(st)
                    if flattened:
                        execution.release_market(market_id)
                    else:
                        log.critical(
                            f"SEQUENTIAL EXPOSURE retained for manual review "
                            f"market={market_id}; capital was not released"
                        )
                    await feed.remove_market(market_id)
                    states.pop(market_id, None)
                    log.info(f"RELEASED {market_id} "
                             f"({'closing' if market_id in result.newly_disqualified else 'demoted'})")
            universe.finalize_closing()
            try:
                universe.publish(universe_snapshot_path)
            except Exception as e:
                log.error(f"universe snapshot publish failed: {e!r}")

    sync_task = asyncio.create_task(_universe_sync_loop(), name="universe-sync")
    sync_task.add_done_callback(
        lambda t: log.error(f"universe-sync loop failed: {t.exception()!r}")
        if (not t.cancelled() and t.exception() is not None) else None
    )

    # Periodic PnL snapshot so a live dashboard shows current numbers instead of
    # only at shutdown -- see PnlLedger.snapshot()'s docstring for why this
    # matters (polymarket_arb only ever logged PnL once, at process exit).
    pnl_snapshot_path = "state/pnl_snapshot.json"

    async def _pnl_publish_loop():
        while not stop.is_set():
            try:
                await asyncio.sleep(SETTINGS.pnl_publish_interval_s)
            except asyncio.CancelledError:
                return
            if stop.is_set():
                return
            try:
                execution.pnl_ledger.publish(pnl_snapshot_path)
            except Exception as e:
                log.error(f"pnl snapshot publish failed: {e!r}")
            try:
                sequential.publish(SETTINGS.strategy_snapshot_path)
            except Exception as e:
                log.error(f"strategy snapshot publish failed: {e!r}")

    pnl_task = asyncio.create_task(_pnl_publish_loop(), name="pnl-publish")
    pnl_task.add_done_callback(
        lambda t: log.error(f"pnl-publish loop failed: {t.exception()!r}")
        if (not t.cancelled() and t.exception() is not None) else None
    )

    log.info("HOT PATH live — watching books")
    try:
        while not stop.is_set():
            await asyncio.sleep(1.0)
    finally:
        sync_task.cancel()
        pnl_task.cancel()
        readiness_task.cancel()
        ready_path_obj.unlink(missing_ok=True)  # next start must re-earn WARMING_UP -> RUNNING
        try:
            await sequential.close(exit_open=True)
        except Exception as e:
            log.critical(f"sequential shutdown flatten failed: {e!r}")
        try:
            execution.pnl_ledger.publish(pnl_snapshot_path)  # final snapshot, not a stale mid-run one
        except Exception as e:
            log.error(f"final pnl snapshot publish failed: {e!r}")
        try:
            universe.publish(universe_snapshot_path)
        except Exception as e:
            log.error(f"final universe snapshot publish failed: {e!r}")
        try:
            sequential.publish(SETTINGS.strategy_snapshot_path)
        except Exception as e:
            log.error(f"final strategy snapshot publish failed: {e!r}")
        await feed.aclose()
        await gamma_session.aclose()
        await client.aclose()
        log.info(
            "PAIR_COHERENCE_SUMMARY "
            f"coherent_evaluations={feed.coherent_pair_evaluations} "
            f"incomplete_suppressions={feed.incomplete_pair_suppressions} "
            f"settle_ms={SETTINGS.cross_leg_settle_ms}"
        )
        log.info("session metrics: " + metrics.summary())
        log.close()
        heartbeat.stop()
        alerter.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
