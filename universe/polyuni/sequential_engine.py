"""Sequential two-leg paper strategy.

Unlike the legacy crossed-book detector, this engine deliberately accepts a
bounded interval of directional exposure: it buys one discounted outcome near
the start of a volatile market, then waits for the opposite outcome to become
cheap enough to complete a fee-positive pair. A wall-clock deadline exits the
first leg even if no more market-data ticks arrive.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from collections import Counter, defaultdict, deque
from pathlib import Path
from typing import Deque, Dict, Optional

from .config import SETTINGS, Settings
from .execution import Execution
from .fees import effective_fee_rate, taker_fee
from .logger import log
from .metrics import Metrics
from .models import MarketState
from .regime_signal import LightGbmRegimeReader, RegimeSignal


class SequentialPairEngine:
    """Tick-driven state machine with timeout-driven risk reduction."""

    def __init__(
        self,
        execution: Execution,
        metrics: Metrics,
        *,
        settings: Settings = SETTINGS,
        regime_reader: Optional[LightGbmRegimeReader] = None,
    ):
        self.execution = execution
        self.metrics = metrics
        self.settings = settings
        self.regime_reader = regime_reader or LightGbmRegimeReader(
            settings.lightgbm_regime_path,
            settings.lightgbm_regime_max_age_s,
            min_confidence=settings.lightgbm_min_confidence,
        )
        self.open_positions: Dict[str, dict] = {}
        self._phases: Dict[str, str] = {}
        self._cooldown_until: Dict[str, float] = {}
        self._history: Dict[tuple[str, str], Deque[tuple[float, float]]] = defaultdict(deque)
        self._market_states: Dict[str, MarketState] = {}
        self._tasks: set[asyncio.Task] = set()
        self._timeout_tasks: Dict[str, asyncio.Task] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        self._events: Deque[dict] = deque(maxlen=100)
        self._rejections: Counter[str] = Counter()
        self._startup_manual_reviews: list[dict] = []
        self._last_regime = RegimeSignal(status="not_applicable")
        self._closed = False

    def _lock(self, market_id: str) -> asyncio.Lock:
        lock = self._locks.get(market_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[market_id] = lock
        return lock

    def _spawn(self, coroutine, *, name: str) -> asyncio.Task:
        task = asyncio.create_task(coroutine, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(self._log_task_error)
        return task

    @staticmethod
    def _log_task_error(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.error(f"sequential strategy task failed: {exc!r}")

    def _event(self, event: str, **fields) -> None:
        record = {"ts": time.time(), "event": event, **fields}
        self._events.append(record)
        log.trade(event, fields)

    def _reject(self, reason: str) -> None:
        self._rejections[reason] += 1
        self.metrics.record_strategy_rejection(reason)

    def restore_manual_reviews(self, entries: list[dict]) -> None:
        """Surface conservatively restored first-leg exposure after a crash."""
        for entry in entries:
            row = dict(entry)
            market_id = str(row.get("market_id") or "unknown")
            self._startup_manual_reviews.append(row)
            self._phases[market_id] = "manual_review"
            self._event(
                "sequential_startup_manual_review",
                market_id=market_id,
                sequence_id=row.get("sequence_id"),
                side=row.get("side"),
                cost=round(float(row.get("cost") or 0.0), 5),
            )

    @staticmethod
    def _side_for_token(st: MarketState, token_id: str) -> Optional[str]:
        if token_id == st.yes_token_id:
            return "YES"
        if token_id == st.no_token_id:
            return "NO"
        return None

    @staticmethod
    def _ask(st: MarketState, side: str) -> Optional[float]:
        return st.best_yes_ask if side == "YES" else st.best_no_ask

    @staticmethod
    def _size(st: MarketState, side: str) -> Optional[float]:
        return st.yes_size if side == "YES" else st.no_size

    @staticmethod
    def _opposite(side: str) -> str:
        return "NO" if side == "YES" else "YES"

    def _fee_rate(self, st: MarketState) -> float:
        text = f"{st.slug} {st.question}".lower()
        is_crypto = any(term in text for term in ("bitcoin", "btc", "ethereum", "eth", "solana"))
        return effective_fee_rate(
            fees_enabled=st.fees_enabled,
            market_rate=st.fee_rate,
            is_crypto=is_crypto,
            crypto_fallback=self.settings.crypto_taker_fee_rate,
            default_fallback=self.settings.default_taker_fee_rate,
        )

    def _record_sample(self, market_id: str, side: str, now: float, price: float) -> None:
        samples = self._history[(market_id, side)]
        samples.append((now, price))
        cutoff = now - self.settings.sequential_volatility_lookback_s
        while samples and samples[0][0] < cutoff:
            samples.popleft()

    def _observed_move(self, market_id: str, side: str) -> tuple[float, int]:
        samples = self._history[(market_id, side)]
        if not samples:
            return 0.0, 0
        prices = [price for _, price in samples]
        return max(prices) - min(prices), len(prices)

    def _required_move(self, st: MarketState, now: float) -> tuple[float, RegimeSignal]:
        required = self.settings.sequential_min_local_move
        signal = RegimeSignal(status="not_applicable")
        if st.is_btc_up_down:
            signal = self.regime_reader.read(now)
            self._last_regime = signal
            if signal.usable and signal.regime == "HIGH":
                required *= self.settings.lightgbm_high_vol_multiplier
            elif signal.usable and signal.regime == "LOW":
                required *= self.settings.lightgbm_low_vol_multiplier
            # MED or stale/missing stays at the observed-volatility base threshold.
        return required, signal

    def on_leg_update(self, st: MarketState, token_id: str) -> None:
        """Receive every raw leg update; called before pair coalescing."""
        if self._closed or self.settings.strategy_mode != "sequential":
            return
        side = self._side_for_token(st, token_id)
        if side is None:
            return
        # A completed or protectively exited market window is terminal. Raw
        # book frames can continue arriving until the universe releases it;
        # those frames are expected input, not rejected strategy candidates.
        if self._phases.get(st.market_id) == "consumed":
            return
        price = self._ask(st, side)
        if price is None:
            self._reject("missing_price")
            return
        now = time.time()
        self.metrics.loops += 1
        self._market_states[st.market_id] = st
        self._phases.setdefault(st.market_id, "watching")
        if st.start_time and now < st.start_time:
            # Upcoming short windows are intentionally pre-subscribed. Do not let
            # pre-open quote motion satisfy the post-open volatility requirement.
            self._reject("market_not_started")
            return
        self._record_sample(st.market_id, side, now, float(price))

        phase = self._phases.get(st.market_id, "watching")
        if phase == "cooldown" and now >= self._cooldown_until.get(st.market_id, 0.0):
            phase = "watching"
            self._phases[st.market_id] = phase

        open_leg = self.open_positions.get(st.market_id)
        if open_leg is not None:
            if phase != "seeking_second":
                self._reject("position_busy")
                return
            if side == open_leg["side"]:
                self._reject("waiting_for_opposite")
                return
            self._consider_second(st, side, float(price), now, open_leg)
            return

        if phase != "watching":
            self._reject(phase)
            return
        self._consider_first(st, side, float(price), now)

    def _consider_first(self, st: MarketState, side: str, price: float, now: float) -> None:
        if not st.start_time:
            self._reject("unknown_start_time")
            return
        age = now - st.start_time
        if age < 0 or age > self.settings.sequential_entry_window_s:
            self._reject("outside_entry_window")
            return
        if st.in_no_entry_zone(self.settings.no_entry_seconds, now):
            self._reject("no_entry_zone")
            return
        if abs(price - 0.5) > self.settings.sequential_near_even_band:
            self._reject("outside_near_even_band")
            return
        if price > self.settings.sequential_first_leg_max_price:
            self._reject("first_price_too_high")
            return
        if st.quote_age_ms(side, now) > self.settings.max_quote_age_ms:
            self._reject("stale_first_quote")
            return
        visible = self._size(st, side)
        if visible is not None and visible < self.settings.order_size:
            self._reject("insufficient_first_depth")
            return
        observed_move, samples = self._observed_move(st.market_id, side)
        required_move, signal = self._required_move(st, now)
        if samples < self.settings.sequential_min_samples:
            self._reject("volatility_warmup")
            return
        if observed_move < required_move:
            self._reject("local_volatility_too_low")
            return

        self.metrics.opportunities += 1
        self._phases[st.market_id] = "first_submitting"
        sequence_id = f"{st.market_id}-{uuid.uuid4().hex[:10]}"
        self._event(
            "sequential_first_candidate",
            market_id=st.market_id,
            sequence_id=sequence_id,
            side=side,
            price=price,
            observed_move=round(observed_move, 6),
            required_move=round(required_move, 6),
            regime=signal.regime,
            regime_status=signal.status,
        )
        self._spawn(
            self._submit_first(st, side, price, sequence_id, signal, observed_move, required_move),
            name=f"sequential-first-{st.market_id}",
        )

    async def _submit_first(
        self,
        st: MarketState,
        side: str,
        price: float,
        sequence_id: str,
        signal: RegimeSignal,
        observed_move: float,
        required_move: float,
    ) -> None:
        async with self._lock(st.market_id):
            # A market can be released between candidate detection and this task's
            # first event-loop turn. Do not resurrect an entry after release/shutdown.
            if self._closed or self._phases.get(st.market_id) != "first_submitting":
                return
            await self._submit_first_locked(
                st,
                side,
                price,
                sequence_id,
                signal,
                observed_move,
                required_move,
            )

    async def _submit_first_locked(
        self,
        st: MarketState,
        side: str,
        price: float,
        sequence_id: str,
        signal: RegimeSignal,
        observed_move: float,
        required_move: float,
    ) -> None:
        rate = self._fee_rate(st)
        result = await self.execution.execute_sequential_buy(
            st,
            side=side,
            limit_price=price,
            size=self.settings.order_size,
            fee_rate=rate,
            fee_exponent=st.fee_exponent,
            sequence_id=sequence_id,
            stage="first",
        )
        if not result.get("ok"):
            self._phases[st.market_id] = "watching"
            reason = "first_" + str(result.get("reason", "failed"))
            self._reject(reason)
            self._event(
                "sequential_first_rejected",
                market_id=st.market_id,
                sequence_id=sequence_id,
                reason=reason,
            )
            return
        opened_at = time.time()
        result.update({
            "sequence_id": sequence_id,
            "market_id": st.market_id,
            "slug": st.slug,
            "opened_at": opened_at,
            "deadline_at": opened_at + self.settings.sequential_second_leg_timeout_s,
            "fee_rate": rate,
            "fee_exponent": st.fee_exponent,
            "regime": signal.regime,
            "regime_status": signal.status,
            "observed_move": observed_move,
            "required_move": required_move,
        })
        self.open_positions[st.market_id] = result
        self._phases[st.market_id] = "seeking_second"
        self._event(
            "sequential_first_filled",
            market_id=st.market_id,
            sequence_id=sequence_id,
            side=side,
            price=round(float(result["price"]), 5),
            fee=round(float(result["fee"]), 5),
            deadline_at=result["deadline_at"],
        )
        timeout_task = self._spawn(
            self._timeout_position(st.market_id, sequence_id),
            name=f"sequential-timeout-{st.market_id}",
        )
        self._timeout_tasks[st.market_id] = timeout_task

    def _expected_net_edge(self, open_leg: dict, second_price: float) -> float:
        size = float(open_leg["size"])
        second_fee = taker_fee(
            size,
            second_price,
            float(open_leg["fee_rate"]),
            float(open_leg["fee_exponent"]),
        )
        second_cost = second_price * size + second_fee
        return (size - float(open_leg["cost"]) - second_cost) / size

    def _max_second_price(self, open_leg: dict) -> float:
        high = min(0.999, self.settings.sequential_second_leg_max_price)
        low = 0.0
        target = self.settings.sequential_min_net_edge
        if self._expected_net_edge(open_leg, low) < target:
            return 0.0
        for _ in range(40):
            mid = (low + high) / 2.0
            if self._expected_net_edge(open_leg, mid) >= target:
                low = mid
            else:
                high = mid
        return low

    def _consider_second(
        self, st: MarketState, side: str, price: float, now: float, open_leg: dict
    ) -> None:
        if now >= float(open_leg["deadline_at"]):
            self._reject("second_deadline_reached")
            return
        if price > self.settings.sequential_second_leg_max_price:
            self._reject("second_price_too_high")
            return
        if st.quote_age_ms(side, now) > self.settings.max_quote_age_ms:
            self._reject("stale_second_quote")
            return
        visible = self._size(st, side)
        if visible is not None and visible < float(open_leg["size"]):
            self._reject("insufficient_second_depth")
            return
        net_edge = self._expected_net_edge(open_leg, price)
        if net_edge < self.settings.sequential_min_net_edge:
            self._reject("pair_net_edge_too_low")
            return
        self._phases[st.market_id] = "second_submitting"
        self._event(
            "sequential_second_candidate",
            market_id=st.market_id,
            sequence_id=open_leg["sequence_id"],
            side=side,
            price=price,
            expected_net_edge=round(net_edge, 6),
        )
        self._spawn(
            self._submit_second(st, side, price, str(open_leg["sequence_id"])),
            name=f"sequential-second-{st.market_id}",
        )

    async def _submit_second(
        self, st: MarketState, side: str, price: float, sequence_id: str
    ) -> None:
        async with self._lock(st.market_id):
            open_leg = self.open_positions.get(st.market_id)
            if open_leg is None or open_leg["sequence_id"] != sequence_id:
                return
            if time.time() >= float(open_leg["deadline_at"]):
                await self._exit_locked(st, open_leg, "second_leg_timeout")
                return
            result = await self.execution.execute_sequential_buy(
                st,
                side=side,
                limit_price=price,
                size=float(open_leg["size"]),
                fee_rate=float(open_leg["fee_rate"]),
                fee_exponent=float(open_leg["fee_exponent"]),
                sequence_id=sequence_id,
                stage="second",
            )
            if not result.get("ok"):
                reason = "second_" + str(result.get("reason", "failed"))
                self._reject(reason)
                if time.time() >= float(open_leg["deadline_at"]):
                    await self._exit_locked(st, open_leg, "second_leg_timeout")
                else:
                    self._phases[st.market_id] = "seeking_second"
                return

            leg_gap_s = max(0.0, time.time() - float(open_leg["opened_at"]))
            self.execution.pnl_ledger.record_sequential_pair(
                st,
                mode=str(open_leg["mode"]),
                first_side=str(open_leg["side"]),
                first_price=float(open_leg["price"]),
                second_price=float(result["price"]),
                first_fee=float(open_leg["fee"]),
                second_fee=float(result["fee"]),
                size=float(open_leg["size"]),
                first_order_id=open_leg.get("order_id"),
                second_order_id=result.get("order_id"),
                leg_gap_s=leg_gap_s,
            )
            self.open_positions.pop(st.market_id, None)
            timeout = self._timeout_tasks.pop(st.market_id, None)
            if timeout is not None and timeout is not asyncio.current_task():
                timeout.cancel()
            self._consume_market(st.market_id)
            self._event(
                "sequential_pair_completed",
                market_id=st.market_id,
                sequence_id=sequence_id,
                combined=round(float(open_leg["price"]) + float(result["price"]), 5),
                fees=round(float(open_leg["fee"]) + float(result["fee"]), 5),
                leg_gap_s=round(leg_gap_s, 3),
            )

    def _consume_market(self, market_id: str) -> None:
        """Enforce at most one completed/aborted sequence per market window."""
        self._phases[market_id] = "consumed"
        self._cooldown_until.pop(market_id, None)

    async def _timeout_position(self, market_id: str, sequence_id: str) -> None:
        open_leg = self.open_positions.get(market_id)
        if open_leg is None:
            return
        delay = max(0.0, float(open_leg["deadline_at"]) - time.time())
        await asyncio.sleep(delay)
        async with self._lock(market_id):
            current = self.open_positions.get(market_id)
            if current is None or current["sequence_id"] != sequence_id:
                return
            st = self._market_states.get(market_id)
            if st is None:
                self.execution.risk.halt("sequential timeout lost market state")
                self._phases[market_id] = "manual_review"
                return
            await self._exit_locked(st, current, "second_leg_timeout")

    async def _exit_locked(self, st: MarketState, open_leg: dict, reason: str) -> None:
        self._phases[st.market_id] = "exit_submitting"
        if reason == "second_leg_timeout":
            self.metrics.sequential_timeouts += 1
        result = await self.execution.execute_sequential_exit(
            st,
            open_leg=open_leg,
            fee_rate=float(open_leg["fee_rate"]),
            fee_exponent=float(open_leg["fee_exponent"]),
            reason=reason,
        )
        if not result.get("ok"):
            self._phases[st.market_id] = "manual_review"
            self._reject(str(result.get("reason", "exit_failed")))
            return
        self.open_positions.pop(st.market_id, None)
        self._timeout_tasks.pop(st.market_id, None)
        self._consume_market(st.market_id)
        self._event(
            "sequential_position_exited",
            market_id=st.market_id,
            sequence_id=open_leg["sequence_id"],
            reason=reason,
            exit_price=round(float(result["price"]), 5),
            pnl=round(float(result["pnl"]), 5),
        )

    async def release_market(self, st: MarketState) -> bool:
        """Flatten a first leg before universe tracking drops the market."""
        self._phases[st.market_id] = "releasing"
        async with self._lock(st.market_id):
            open_leg = self.open_positions.get(st.market_id)
            if open_leg is not None:
                await self._exit_locked(st, open_leg, "market_released")
            flattened = st.market_id not in self.open_positions
            if flattened:
                self._phases.pop(st.market_id, None)
                self._cooldown_until.pop(st.market_id, None)
                self._history.pop((st.market_id, "YES"), None)
                self._history.pop((st.market_id, "NO"), None)
                self._market_states.pop(st.market_id, None)
            return flattened

    def snapshot(self) -> dict:
        now = time.time()
        if any(st.is_btc_up_down for st in self._market_states.values()):
            self._last_regime = self.regime_reader.read(now)
        phase_counts = Counter(self._phases.values())
        open_rows = []
        exposure = sum(float(row.get("cost") or 0.0) for row in self._startup_manual_reviews)
        for market_id, position in sorted(self.open_positions.items()):
            st = self._market_states.get(market_id)
            second_side = self._opposite(str(position["side"]))
            current_second = self._ask(st, second_side) if st is not None else None
            exposure += float(position["cost"])
            open_rows.append({
                "sequence_id": position["sequence_id"],
                "market_id": market_id,
                "slug": position.get("slug", ""),
                "phase": self._phases.get(market_id, "seeking_second"),
                "first_side": position["side"],
                "first_price": round(float(position["price"]), 5),
                "first_fee": round(float(position["fee"]), 5),
                "size": float(position["size"]),
                "opened_at": float(position["opened_at"]),
                "deadline_at": float(position["deadline_at"]),
                "seconds_remaining": round(max(0.0, float(position["deadline_at"]) - now), 3),
                "max_second_price": round(self._max_second_price(position), 5),
                "current_second_price": current_second,
                "current_second_age_ms": (
                    round(st.quote_age_ms(second_side, now), 1)
                    if st is not None and current_second is not None else None
                ),
                "expected_net_edge": (
                    round(self._expected_net_edge(position, float(current_second)), 6)
                    if current_second is not None else None
                ),
                "observed_move": round(float(position.get("observed_move", 0.0)), 6),
                "required_move": round(float(position.get("required_move", 0.0)), 6),
                "regime": position.get("regime", "UNKNOWN"),
                "regime_status": position.get("regime_status", "missing"),
                "fill_source": position.get("fill_source", "unknown"),
            })
        signal = self._last_regime
        return {
            "generated_at": now,
            "mode": self.settings.strategy_mode,
            "paper_only": True,
            "risk_halted": bool(getattr(self.execution.risk, "halted", False)),
            "risk_capital_deployed_usd": round(
                float(getattr(self.execution.risk, "total_deployed", 0.0)), 5
            ),
            "risk_capital_pending_usd": round(
                float(getattr(self.execution.risk, "pending_deployed", 0.0)), 5
            ),
            "phase_counts": dict(sorted(phase_counts.items())),
            "open_exposure_usd": round(exposure, 5),
            "completed_pairs": self.metrics.sequential_pairs,
            "timeouts": self.metrics.sequential_timeouts,
            "exits": self.metrics.sequential_exits,
            "rejections": dict(sorted(self._rejections.items())),
            "lightgbm": {
                "regime": signal.regime,
                "confidence": signal.confidence,
                "source": signal.source,
                "status": signal.status,
                "age_s": round(signal.age_s, 1) if signal.age_s is not None else None,
                "usable": signal.usable,
            },
            "open_sequences": open_rows,
            "manual_review_sequences": list(self._startup_manual_reviews),
            "events": list(self._events)[-30:],
        }

    def publish(self, path: str | Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        payload = json.dumps(self.snapshot(), indent=2, sort_keys=True)
        with tmp.open("w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(target)

    async def close(self, *, exit_open: bool = True) -> None:
        self._closed = True
        # Stop deadline jobs first, then let any in-flight paper submissions reach
        # a known filled/rejected state before deciding what must be flattened.
        for task in list(self._timeout_tasks.values()):
            task.cancel()
        timeout_tasks = set(self._timeout_tasks.values())
        active_submissions = [
            task for task in list(self._tasks)
            if task not in timeout_tasks and task is not asyncio.current_task()
        ]
        if active_submissions:
            await asyncio.gather(*active_submissions, return_exceptions=True)
        if exit_open:
            for market_id in list(self.open_positions):
                st = self._market_states.get(market_id)
                if st is not None:
                    async with self._lock(market_id):
                        open_leg = self.open_positions.get(market_id)
                        if open_leg is not None:
                            await self._exit_locked(st, open_leg, "shutdown")
        for task in list(self._timeout_tasks.values()):
            task.cancel()
        if self._timeout_tasks:
            await asyncio.gather(*self._timeout_tasks.values(), return_exceptions=True)
        self._timeout_tasks.clear()
