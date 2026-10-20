"""Backtest engine — COLD PATH.

Closes Critical Gap #2: validates arb logic on historical tick data from LSE.
Uses the same execution path (arb_engine → execution) but with simulated
price feeds from historical data instead of live WebSocket.

Design:
  * Loads tick data from LSE bulk export (or CSV fallback)
  * Replays ticks through the arb engine at variable speed
  * Tracks PnL, fill rates, freshness bands, partial rates
  * Reports the same metrics as live mode (locked_pnl, residual_pnl, etc.)

Usage:
    python -m polymarket_arb.backtest_engine --symbol BTC/USD --start 2026-07-01 --end 2026-07-04
    python -m polymarket_arb.backtest_engine --csv data/historical_ticks.csv
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .arb_engine import ArbEngine
from .clients.lse_client import LSEClient
from .config import SETTINGS
from .execution import Execution
from .logger import log, BotLogger
from .metrics import Metrics
from .models import MarketState, TradeRecord
from .risk import RiskState
from .clients.trading_client import TradingClient


@dataclass
class BacktestConfig:
    """Configuration for a backtest run."""
    symbol: str = "BTC/USD"
    start_date: str = "2026-07-01"
    end_date: str = "2026-07-04"
    min_edge: float = 0.01
    order_size: float = 10.0
    max_quote_age_ms: int = 500
    no_entry_seconds: float = 5.0
    slippage_stress_buffer: float = 0.01
    # Replay speed: 1.0 = real-time, 10.0 = 10x faster
    replay_speed: float = 10.0
    # Use LSE for data, or load from CSV
    use_lse: bool = True
    csv_path: Optional[str] = None
    # Cap available liquidity per tick for partial-fill testing.
    # When set to a value < order_size, submit_order produces partial fills.
    # 0.0 = no cap (use tick data as-is).
    max_liquidity: float = 0.0
    # ``top_of_book`` preserves the legacy replay: the best-ask size is the
    # entire executable quantity. ``depth_snapshot`` consumes ask ladders at
    # or below the submitted limit price and never invents missing depth.
    fill_model: str = "top_of_book"
    # In-memory input is required when snapshots carry nested price/size
    # ladders. CSV remains supported for legacy top-of-book replay only.
    ticks: Optional[List[Dict[str, Any]]] = None


@dataclass
class BacktestResult:
    """Results from a backtest run."""
    config: BacktestConfig
    metrics: Metrics
    trades: List[TradeRecord] = field(default_factory=list)
    total_ticks: int = 0
    start_time: float = 0.0
    end_time: float = 0.0
    duration_s: float = 0.0
    fill_summary: Dict[str, float] = field(default_factory=dict)
    data_quality: str = "TICK"
    candle_fallback_ticks: int = 0

    def summary(self) -> str:
        m = self.metrics
        elapsed = self.duration_s
        return (
            f"Backtest complete: {self.total_ticks} ticks in {elapsed:.1f}s "
            f"({self.total_ticks/elapsed:.0f} ticks/s)\n"
            f"  Opportunities: {m.opportunities}\n"
            f"  Fills: {m.fills} | Partial: {m.partial_fills} | Resolved: {m.partial_resolved}\n"
            f"  Missed: {m.missed} | Would-lock: {m.would_lock_pnl:.4f}\n"
            f"  Locked PnL: {m.locked_pnl:.4f} | Residual PnL: {m.residual_pnl:.4f}\n"
            f"  Session PnL: {m.session_pnl:.4f}\n"
            f"  Avg opp->order: {m.avg_opp_to_order_ms():.2f}ms | P50: {m.p50_opp_to_order_ms():.2f}ms\n"
            f"  Partial rate: {m.partial_rate:.2%}\n"
            f"  Data quality: {self.data_quality} (candle_fallback_ticks={self.candle_fallback_ticks})\n"
            f"  Freshness bands: " + " ".join(
                f"{b}={m.fills_by_band[b]}/{m.fresh_attempts[b]}" for b in m.fresh_attempts
            )
        )


class DepthSnapshotUnavailable(ValueError):
    """Raised when cumulative-depth replay is requested without full ladders."""


class CandleFallbackUnavailable(RuntimeError):
    """Raised when a tick backtest has only candles, not outcome-book prices."""


def _normalize_depth_levels(value: Any) -> Optional[List[Tuple[float, float]]]:
    """Parse one retained ask ladder without fabricating a top-of-book level.

    The accepted schema is a list of ``{"price": ..., "size": ...}`` objects
    or two-item sequences. An empty list is a valid observed empty book; ``None``
    means the snapshot itself is absent or malformed.
    """
    if not isinstance(value, list):
        return None

    levels: List[Tuple[float, float]] = []
    for level in value:
        try:
            if isinstance(level, dict):
                price = float(level["price"])
                size = float(level["size"])
            elif isinstance(level, (list, tuple)) and len(level) >= 2:
                price = float(level[0])
                size = float(level[1])
            else:
                return None
        except (KeyError, TypeError, ValueError):
            return None
        if price < 0 or size < 0:
            return None
        levels.append((price, size))
    return sorted(levels)


def depth_snapshot_availability(ticks: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    """Return strict schema coverage for ``yes_asks`` and ``no_asks`` ladders."""
    result = {
        "total_ticks": len(ticks),
        "complete_ticks": 0,
        "missing_yes_asks": 0,
        "missing_no_asks": 0,
        "invalid_yes_asks": 0,
        "invalid_no_asks": 0,
    }
    for tick in ticks:
        yes = _normalize_depth_levels(tick.get("yes_asks"))
        no = _normalize_depth_levels(tick.get("no_asks"))
        if yes is None:
            key = "missing_yes_asks" if "yes_asks" not in tick else "invalid_yes_asks"
            result[key] += 1
        if no is None:
            key = "missing_no_asks" if "no_asks" not in tick else "invalid_no_asks"
            result[key] += 1
        if yes is not None and no is not None:
            result["complete_ticks"] += 1
    return result


def cumulative_limit_fill(
    levels: Sequence[Tuple[float, float]], limit_price: float, requested_size: float
) -> Tuple[float, Optional[float]]:
    """Consume retained ask levels at or below a buy limit and return (size, VWAP)."""
    if requested_size <= 0:
        return 0.0, None
    filled = 0.0
    notional = 0.0
    for level_price, level_size in levels:
        if level_price > limit_price:
            break
        take = min(level_size, requested_size - filled)
        if take <= 0:
            continue
        filled += take
        notional += take * level_price
        if filled >= requested_size:
            break
    return filled, (notional / filled if filled else None)


def cumulative_sell_fill(
    levels: Sequence[Tuple[float, float]], limit_price: float, requested_size: float
) -> Tuple[float, Optional[float]]:
    """Consume retained bids at or above a sell limit and return (size, VWAP)."""
    if requested_size <= 0:
        return 0.0, None
    filled = 0.0
    notional = 0.0
    for level_price, level_size in sorted(levels, reverse=True):
        if level_price < limit_price:
            break
        take = min(level_size, requested_size - filled)
        if take <= 0:
            continue
        filled += take
        notional += take * level_price
        if filled >= requested_size:
            break
    return filled, (notional / filled if filled else None)


class _BacktestTradingClient(TradingClient):
    """Simulated trading client for backtesting.

    Models realistic fill behavior based on top-of-book liquidity:
    - Orders fill proportionally to available ask size (yes_size / no_size)
    - Partial fills when the order size exceeds available liquidity
    - Zero-liquidity orders are rejected entirely (no fill)
    - No random Bernoulli fill — fills are deterministic given the book
    """

    def __init__(self, config: BacktestConfig):
        # Don't call super().__init__() — we don't need real signing
        self.s = SETTINGS
        self.config = config
        self._current_state: Optional[MarketState] = None
        self._fill_ratios: Dict[str, float] = {}
        self._fill_vwaps: Dict[str, float] = {}
        self._fill_history: List[Dict[str, float | str]] = []
        # FIX (fill-rate investigation 2026-08-16): replay windows share the
        # synthetic token ids, so order ids derived from them collided across
        # fires.  execution.py keys its pending-fill stash by (market_id,
        # yes_id) (execution.py:609) and its insert guard silently discarded
        # any fire whose key was already occupied — 76.5% of fired pairs.
        # A per-submission sequence suffix makes every order id unique, so
        # every fire gets its own pending entry.
        self._order_seq = 0
        self._current_depth: Dict[str, Dict[str, Optional[List[Tuple[float, float]]]]] = {
            "yes": {"BUY": None, "SELL": None},
            "no": {"BUY": None, "SELL": None},
        }

    def set_market_state(self, st: MarketState, tick: Optional[Dict[str, Any]] = None) -> None:
        self._current_state = st
        if self.config.fill_model == "depth_snapshot":
            tick = tick or {}
            self._current_depth = {
                "yes": {
                    "BUY": _normalize_depth_levels(tick.get("yes_asks")),
                    "SELL": _normalize_depth_levels(tick.get("yes_bids")),
                },
                "no": {
                    "BUY": _normalize_depth_levels(tick.get("no_asks")),
                    "SELL": _normalize_depth_levels(tick.get("no_bids")),
                },
            }

    def get_fill_ratio(self, order_id: str) -> float:
        return self._fill_ratios.get(order_id, 1.0)

    def get_fill_vwap(self, order_id: str) -> Optional[float]:
        return self._fill_vwaps.get(order_id)

    def fill_summary(self) -> Dict[str, float]:
        """Aggregate executed BUY quantity across every simulated submission."""
        buys = [entry for entry in self._fill_history if entry["side"] == "BUY"]
        requested = sum(float(entry["requested_size"]) for entry in buys)
        filled = sum(float(entry["filled_size"]) for entry in buys)
        return {
            "buy_orders": float(len(buys)),
            "requested_buy_quantity": requested,
            "filled_buy_quantity": filled,
            "requested_quantity_fill_rate": filled / requested if requested else 0.0,
        }

    async def submit_order(
        self, token_id: str, price: float, size: float, side: str = "BUY"
    ) -> Optional[str]:
        """Simulate order submission with liquidity-aware fill behavior.

        ``top_of_book`` uses the current best-ask size as the legacy model.
        ``depth_snapshot`` consumes only retained ask levels priced at or below
        the submitted limit. Missing depth is a rejection, never a fallback.
        """
        # Simulate network latency (scaled by replay speed)
        await asyncio.sleep(0.001 / self.config.replay_speed)

        st = self._current_state
        if st is None or size <= 0:
            return None

        # Determine the requested leg by matching the token_id against the
        # market's YES/NO token identifiers.
        yes_liquidity = float(st.yes_size or 0)
        no_liquidity = float(st.no_size or 0)

        if token_id == st.market_id + "-YES" or (
            hasattr(st, "yes_token_id") and token_id == st.yes_token_id
        ):
            leg = "yes"
            available = yes_liquidity
        elif token_id == st.market_id + "-NO" or (
            hasattr(st, "no_token_id") and token_id == st.no_token_id
        ):
            leg = "no"
            available = no_liquidity
        else:
            # The old model had a permissive unknown-token fallback. Depth
            # replay cannot safely choose a ladder without a known leg.
            if self.config.fill_model == "depth_snapshot":
                return None
            leg = "unknown"
            available = max(yes_liquidity, no_liquidity)

        vwap: Optional[float] = None
        if self.config.fill_model == "depth_snapshot":
            normalized_side = str(side).upper()
            if normalized_side not in {"BUY", "SELL"}:
                return None
            levels = self._current_depth.get(leg, {}).get(normalized_side)
            if levels is None:
                return None
            if normalized_side == "BUY":
                available, vwap = cumulative_limit_fill(levels, price, size)
            else:
                available, vwap = cumulative_sell_fill(levels, price, size)
        elif self.config.fill_model == "top_of_book":
            # Cap available liquidity for partial-fill testing when max_liquidity
            # is set and lower than the actual available size.
            if self.config.max_liquidity > 0:
                available = min(available, self.config.max_liquidity)
        else:
            raise ValueError(f"Unsupported fill_model: {self.config.fill_model!r}")

        if available <= 0:
            return None
        fill_ratio = min(1.0, available / size)
        if fill_ratio <= 0:
            return None

        self._order_seq += 1
        order_id = f"bt-{side.lower()}-{token_id[-8:]}-{self._order_seq}"
        self._fill_ratios[order_id] = fill_ratio
        if vwap is not None:
            self._fill_vwaps[order_id] = vwap
        self._fill_history.append({
            "side": side,
            "requested_size": size,
            "filled_size": available,
        })
        return order_id

    async def is_filled(self, order_id: Optional[str], token_id: str, gap_ms: int = 0) -> bool:
        """Simulate fill confirmation."""
        if order_id is None:
            return False
        # Simulate confirmation latency
        await asyncio.sleep(0.001 / self.config.replay_speed)
        return True

    async def cancel_order(self, token_id: str, missing_order_id: Optional[str] = None) -> bool:
        return True

    async def aclose(self):
        pass


class BacktestEngine:
    """Runs historical arb simulations using LSE tick data.

    Replays ticks through the same arb_engine → execution path as live mode,
    but with simulated fills and configurable replay speed.
    """

    def __init__(self, config: BacktestConfig):
        self.config = config
        self.metrics = Metrics()
        self.risk = RiskState(balance=SETTINGS.max_total_capital)
        self.client = _BacktestTradingClient(config)
        self.execution = Execution(self.client, self.risk, self.metrics)
        self.arb = ArbEngine(on_fire=self.execution.execute_dual_buy, metrics=self.metrics)
        # TASK 4: zero-exposure fires (no-fill timeout / submit failure) clear the
        # per-market debounce so the same combined price re-fires in replay.
        self.execution.set_invalidator(self.arb.invalidate)
        self.trades: List[TradeRecord] = []
        self._states: Dict[str, MarketState] = {}
        # In-memory and CSV replays do not need a network client. Constructing one
        # eagerly emitted a credential warning for every replayed market.
        self._lse: Optional[LSEClient] = None
        self._data_quality = "TICK"
        self._candle_fallback_ticks = 0

    async def run(self) -> BacktestResult:
        """Run the backtest. Returns a BacktestResult with metrics."""
        start_wall = time.time()
        log.info(f"Backtest starting: {self.config.symbol} "
                 f"{self.config.start_date} to {self.config.end_date} "
                 f"speed={self.config.replay_speed}x")

        # Load tick data
        ticks = await self._load_ticks()
        if not ticks:
            log.error("No tick data loaded; backtest cannot run")
            return BacktestResult(
                config=self.config, metrics=self.metrics,
                start_time=start_wall, end_time=start_wall, duration_s=0.0,
                fill_summary=self.client.fill_summary(),
                data_quality=self._data_quality,
                candle_fallback_ticks=self._candle_fallback_ticks,
            )

        if self.config.fill_model == "depth_snapshot":
            availability = depth_snapshot_availability(ticks)
            if availability["complete_ticks"] != availability["total_ticks"]:
                raise DepthSnapshotUnavailable(
                    "depth_snapshot replay requires yes_asks and no_asks price/size ladders "
                    f"on every tick; coverage={availability}"
                )

        log.info(f"Loaded {len(ticks)} ticks")

        # Create a simulated market state for the backtest
        # In backtest mode, we simulate a single market with YES/NO legs
        state = MarketState(
            market_id="backtest-market",
            question=f"{self.config.symbol} Up/Down backtest",
            yes_token_id="0xBACKTESTYES",
            no_token_id="0xBACKTESTNO",
            window_start=0,
            window_close=0,
        )
        self._states["backtest-market"] = state
        self.execution.register(state)

        # Replay ticks
        await self._replay_ticks(ticks, state)

        duration = time.time() - start_wall
        log.info(f"Backtest complete in {duration:.1f}s")

        # Collect trade records
        self.trades = list(self.execution.records.values())

        return BacktestResult(
            config=self.config,
            metrics=self.metrics,
            trades=self.trades,
            total_ticks=len(ticks),
            start_time=start_wall,
            end_time=time.time(),
            duration_s=duration,
            fill_summary=self.client.fill_summary(),
            data_quality=self._data_quality,
            candle_fallback_ticks=self._candle_fallback_ticks,
        )

    async def _load_ticks(self) -> List[Dict[str, Any]]:
        """Load historical tick data from LSE or CSV."""
        if self.config.ticks is not None:
            return self.config.ticks
        if self.config.csv_path:
            return self._load_csv(self.config.csv_path)

        if self.config.use_lse:
            # Use LSE bulk tick export
            if self._lse is None:
                self._lse = LSEClient()
            job_id = await self._lse.start_tick_export(
                symbol=self.config.symbol,
                timeframe="tick",
                start=self.config.start_date,
                end=self.config.end_date,
            )
            if job_id:
                log.info(f"LSE export job started: {job_id}")
                ticks = await self._lse.poll_export_result(job_id)
                if ticks:
                    log.info(f"Downloaded {len(ticks)} ticks from LSE")
                    return ticks

            # A BTC/USD candle cannot supply the two independent Polymarket
            # outcome asks.  In particular, ``no_ask = 1 - close`` fabricates
            # a combined price of exactly 1.0, making a zero-opportunity result
            # non-comparable to an outcome-book backtest.  Fail loudly rather
            # than manufacture a neutral book.
            log.warning("LSE tick export failed; refusing non-comparable candle fallback")
            candles = await self._lse.fetch_candles(
                symbol=self.config.symbol,
                timeframe="1m",
                start=self.config.start_date,
                end=self.config.end_date,
            )
            self._data_quality = "UNAVAILABLE_CANDLE_FALLBACK_NOT_COMPARABLE"
            self._candle_fallback_ticks = len(candles)
            raise CandleFallbackUnavailable(
                "LSE tick export unavailable and only BTC/USD candles were returned; "
                f"refusing to fabricate YES/NO asks from {len(candles)} candles. "
                "This backtest is not comparable without retained outcome-book ticks."
            )

        return []

    def _load_csv(self, path: str) -> List[Dict[str, Any]]:
        """Load ticks from a CSV file.

        Expected columns: timestamp, yes_ask, no_ask, yes_bid, no_bid, yes_size, no_size
        """
        ticks = []
        try:
            with open(path, "r") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    ticks.append({
                        "timestamp": float(row["timestamp"]),
                        "yes_ask": float(row.get("yes_ask", 0)),
                        "no_ask": float(row.get("no_ask", 0)),
                        "yes_bid": float(row.get("yes_bid", 0)),
                        "no_bid": float(row.get("no_bid", 0)),
                        "yes_size": float(row.get("yes_size", 100)),
                        "no_size": float(row.get("no_size", 100)),
                    })
        except Exception as e:
            log.error(f"CSV load failed: {e!r}")
        return ticks

    def _candles_to_ticks(self, candles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Reject OHLCV-to-outcome-book fabrication on every engine path."""
        raise CandleFallbackUnavailable(
            "BTC/USD candles do not contain independent YES/NO outcome-book quotes; "
            f"refusing to fabricate comparable ticks from {len(candles)} candles."
        )

    async def _replay_ticks(self, ticks: List[Dict[str, Any]], state: MarketState):
        """Replay ticks through the arb engine.

        This is synchronous for simplicity — the arb engine schedules fire tasks
        via asyncio.create_task, which we drain between ticks.

        CRITICAL (TASK B FIX): In backtest mode, state timestamps must reflect
        the REPLAY clock, not wall-clock time. The arb engine's freshness gate
        (arb_engine.evaluate -> MarketState.max_age_ms) compares the older leg's
        quote timestamp against time.time(). If we set yes_ts/no_ts to historical
        tick timestamps, max_age_ms() returns years of staleness and ALL
        opportunities are rejected as "stale_quote". We patch the time module's
        time() and monotonic() functions to advance with the replay.
        """
        _real_time = time.time
        _real_monotonic = time.monotonic
        # Track the simulated clock: starts at the first tick's timestamp
        first_ts = ticks[0].get("timestamp", 0.0) if ticks else _real_time()
        sim_clock = [first_ts]
        sim_monotonic = [0.0]

        # Patch the time module's functions directly — all code that does
        # `import time; time.time()` or `time.monotonic()` will see our simulated
        # clock. This is safe because we restore in the finally block.
        time.time = lambda: sim_clock[0]
        time.monotonic = lambda: sim_monotonic[0]

        try:
            for i, tick in enumerate(ticks):
                # Update market state with tick data
                ts = tick.get("timestamp", time.time())
                state.best_yes_ask = tick.get("yes_ask")
                state.best_no_ask = tick.get("no_ask")
                state.best_yes_bid = tick.get("yes_bid")
                state.best_no_bid = tick.get("no_bid")
                state.yes_size = tick.get("yes_size")
                state.no_size = tick.get("no_size")
                state.last_update_ts = ts
                # Retained paired-book captures can carry the actual receipt time
                # of each leg.  Preserve that skew so the production freshness gate
                # is exercised during replay instead of making both legs appear to
                # have arrived simultaneously.  Legacy ticks retain their old
                # single-timestamp behaviour.
                state.yes_ts = tick.get("yes_timestamp", ts)
                state.no_ts = tick.get("no_timestamp", ts)
                # Likewise, real window captures can exercise the production
                # no-entry zone.  These fields are optional for legacy inputs.
                state.window_start = tick.get("window_start", state.window_start)
                state.window_close = tick.get("window_close", state.window_close)

                # Advance the simulated clock to this tick's timestamp.
                # This ensures max_age_ms() in the arb engine returns ~0 (fresh).
                sim_clock[0] = ts
                # Simulated monotonic time: linear with replay ticks.
                # The gap window in execution.py (max_leg_gap_ms=300ms) needs
                # monotonic to advance by at least a few hundred ms between
                # fire and fill-check. Use real-time spacing scaled by replay speed.
                if i > 0:
                    prev_ts = ticks[i - 1].get("timestamp", ts)
                    dt = max(0.001, (ts - prev_ts) / self.config.replay_speed)
                else:
                    dt = 0.001
                sim_monotonic[0] += dt

                # Run arb engine evaluation
                self.client.set_market_state(state, tick)
                self.arb.evaluate(state)

                # FIX #6: drive deferred fill-resolution (non-blocking) on each replayed tick
                self.execution.on_price_update(state)

                # Drain pending fire tasks + fill checks
                await asyncio.sleep(0)

                # Throttle based on replay speed
                if i > 0 and i % 100 == 0:
                    prev_ts = ticks[i - 100].get("timestamp", ts)
                    elapsed = ts - prev_ts
                    if elapsed > 0:
                        sleep_time = elapsed / self.config.replay_speed
                        if sleep_time > 0:
                            # time.sleep uses the OS clock, NOT time.time(), so it
                            # is unaffected by our time.time/monotonic simulation patch.
                            # We throttle the replay here to avoid flooding.
                            time.sleep(min(sleep_time, 0.1))  # cap at 100ms

        finally:
            # Restore original time functions
            time.time = _real_time
            time.monotonic = _real_monotonic

    async def aclose(self):
        if self._lse is not None:
            await self._lse.aclose()
        await self.client.aclose()


def main():
    p = argparse.ArgumentParser(description="Backtest polymarket arb strategy")
    p.add_argument("--symbol", default="BTC/USD", help="Trading pair")
    p.add_argument("--start", default="2026-07-01", help="Start date (YYYY-MM-DD)")
    p.add_argument("--end", default="2026-07-04", help="End date (YYYY-MM-DD)")
    p.add_argument("--csv", help="Load ticks from CSV instead of LSE")
    p.add_argument("--speed", type=float, default=10.0, help="Replay speed multiplier")
    p.add_argument("--min-edge", type=float, default=0.01, help="Minimum edge threshold")
    p.add_argument("--order-size", type=float, default=10.0, help="Order size per leg")
    p.add_argument(
        "--fill-model",
        choices=("top_of_book", "depth_snapshot"),
        default="top_of_book",
        help="Legacy top-of-book or strict retained-depth replay",
    )
    p.add_argument("--output", help="Output JSON results to file")
    args = p.parse_args()

    config = BacktestConfig(
        symbol=args.symbol,
        start_date=args.start,
        end_date=args.end,
        min_edge=args.min_edge,
        order_size=args.order_size,
        replay_speed=args.speed,
        csv_path=args.csv,
        use_lse=args.csv is None,
        fill_model=args.fill_model,
    )

    # Override settings for backtest
    import polymarket_arb.config as cfg
    original_min_edge = cfg.SETTINGS.min_edge
    original_order_size = cfg.SETTINGS.order_size
    cfg.SETTINGS = cfg.Settings(
        min_edge=config.min_edge,
        order_size=config.order_size,
        dry_run=True,
    )

    # Setup logger
    global log
    log = BotLogger(jsonl_path="state/backtest.jsonl", level="INFO")

    async def _run():
        engine = BacktestEngine(config)
        result = await engine.run()
        await engine.aclose()
        return result

    result = asyncio.run(_run())
    print(result.summary())

    if args.output:
        with open(args.output, "w") as f:
            json.dump({
                "config": config.__dict__,
                "metrics": {
                    "opportunities": result.metrics.opportunities,
                    "fills": result.metrics.fills,
                    "partial_fills": result.metrics.partial_fills,
                    "partial_resolved": result.metrics.partial_resolved,
                    "missed": result.metrics.missed,
                    "locked_pnl": result.metrics.locked_pnl,
                    "residual_pnl": result.metrics.residual_pnl,
                    "session_pnl": result.metrics.session_pnl,
                    "would_lock_pnl": result.metrics.would_lock_pnl,
                    "avg_opp_to_order_ms": result.metrics.avg_opp_to_order_ms(),
                    "p50_opp_to_order_ms": result.metrics.p50_opp_to_order_ms(),
                    "partial_rate": result.metrics.partial_rate,
                    "fills_by_band": result.metrics.fills_by_band,
                    "fresh_attempts": result.metrics.fresh_attempts,
                },
                "total_ticks": result.total_ticks,
                "duration_s": result.duration_s,
            }, f, indent=2)
        print(f"Results written to {args.output}")

    # Restore settings
    cfg.SETTINGS = cfg.Settings(min_edge=original_min_edge, order_size=original_order_size)


if __name__ == "__main__":
    main()
