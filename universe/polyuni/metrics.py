"""Metrics — counters, latency accumulators, session PnL. HOT PATH friendly.

Pure in-memory ints/floats; no allocations in the steady state. JSONL persistence of
events lives in logger.py. We keep simple running sums + a tiny ring buffer for the
latency histogram so callers never pay for sorting in the loop.

We keep THREE distinct PnL accounts (FIX #2): clean-arb PnL (both legs filled),
residual PnL (the leftover from a managed partial fill), and missed-edge PnL that we
would have locked. Real bots lose in the residual bucket, so it must never be blended
into the clean number when we report. Same for freshness bands (FIX #1): we bucket
every trade attempt by the age of the quote at fill time so PnL can be sliced by band
post-hoc to find where the damage concentrates.
"""
from __future__ import annotations

import math
from typing import Dict, List

# Freshness bands (ms) used for bucketing trade attempts at decision time (FIX #1).
# "fresh" = within max_quote_age_ms; then widening buckets to localize staleness damage.
FRESHNESS_BANDS = [500, 2000, 10000]  # boundaries: <500, 500-2000, 2000-10000, >10000
FRESHNESS_BAND_LABELS = ["fresh", "0.5-2s", "2-10s", "stale"]


def classify_freshness_ms(age_ms: float) -> str:
    """Return the band label for a quote age (ms) at decision time."""
    for i, b in enumerate(FRESHNESS_BANDS):
        if age_ms < b:
            return FRESHNESS_BAND_LABELS[i]
    return FRESHNESS_BAND_LABELS[-1]


class Metrics:
    """Per-session counters shared by the engine and execution layer.

    Tracks three distinct PnL accounts (FIX #2): clean-arb PnL (both legs filled),
    residual/hedge PnL (one-leg, managed separately), and would-lock PnL (edge
    seen but rejected). Trade attempts are bucketed by quote-freshness band (FIX #1)
    so p95-staleness effects on fills are observable, not just median. All counters
    are in-event integers; PnL sums are floats in USD. Reset per session only.
    """

    def __init__(self):
        # counts
        self.loops = 0
        self.opportunities = 0
        self.orders_sent = 0
        # TASK 2: orders + fills broken out by execution mode (dry_run/paper/live)
        # so a mixed-mode session summary separates paper from live instead of
        # treating every order id as the same.
        self.orders_sent_by_mode: Dict[str, int] = {"dry_run": 0, "paper": 0, "live": 0}
        self.fills_by_mode: Dict[str, int] = {"dry_run": 0, "paper": 0, "live": 0}
        self.fills = 0
        self.partial_fills = 0
        self.partial_resolved = 0   # partials that we actively managed to close/hedge
        self.missed = 0           # opportunity seen but skipped by risk/stale
        # Sequential strategy observability. Rejection reasons are intentionally
        # counted before the order boundary so "zero trades" has an actionable
        # explanation instead of looking like a dead bot.
        self.sequential_first_fills = 0
        self.sequential_pairs = 0
        self.sequential_timeouts = 0
        self.sequential_exits = 0
        self.strategy_rejections: Dict[str, int] = {}
        # A fire reached execution after its market's precomputed template was removed
        # (for example during window rotation).  Keep this separate from generic misses.
        self.template_unavailable = 0
        self.acks = 0
        # freshness-at-fill (FIX #1): attempts bucketed by quote age band
        self.fresh_attempts: dict = {b: 0 for b in FRESHNESS_BAND_LABELS}
        self.fills_by_band: dict = {b: 0 for b in FRESHNESS_BAND_LABELS}
        # latency accumulators (seconds)
        self.opp_to_order_sum = 0.0
        self.opp_to_order_max = 0.0
        self.ack_latency_sum = 0.0
        # pnl — three distinct accounts (FIX #2)
        self.locked_pnl = 0.0          # clean-arb: both legs filled, locked at resolution
        self.residual_pnl = 0.0        # managed partial-fill residual (hedge + exit PnL)
        self.session_pnl = 0.0         # locked + residual (reported alongside, not blended silently)
        self.realized_pnl = 0.0        # cumulative total (locked + residual), kept for reporting
        self.would_lock_pnl = 0.0      # edge seen but rejected (freshness/stale/etc.)
        # MACRO-RISK (TASK A): opportunities skipped because a HIGH-impact macro
        # window was active at decision time. Mirrors the would_lock pattern: a count
        # plus the forgone edge value so the gates' cost is measurable post-hoc.
        self.macro_skipped = 0          # attempts rejected during a HIGH macro-risk window
        self.macro_skipped_pnl = 0.0    # edge value forgone to the macro gate
        # Bounded samples retain the tail of the hot path for percentile reporting.
        # The session harness archives these summaries; events remain in JSONL.
        self._ring: List[float] = []
        self._ring_max = 256
        self._stage_samples: Dict[str, List[float]] = {}

    def record_latency(
        self, opp_to_order_s: float, ack_s: float,
        stages_s: Dict[str, float] | None = None,
    ):
        """Record one accepted dual-order submission.

        ``opp_to_order_s`` ends at the first leg's actual submission start, not at
        both acknowledgements. This makes the tail metric an admission-latency
        measure instead of accidentally charging it for paper fill simulation.
        """
        self.opp_to_order_sum += opp_to_order_s
        if opp_to_order_s > self.opp_to_order_max:
            self.opp_to_order_max = opp_to_order_s
        self.ack_latency_sum += ack_s
        self.acks += 1  # LOW: was never incremented, so avg_ack_ms was always 0
        self._ring.append(opp_to_order_s)
        if len(self._ring) > self._ring_max:
            self._ring.pop(0)
        for name, value in (stages_s or {}).items():
            samples = self._stage_samples.setdefault(name, [])
            samples.append(max(0.0, value))
            if len(samples) > self._ring_max:
                samples.pop(0)

    @staticmethod
    def _percentile_ms(samples: List[float], percentile: float) -> float:
        if not samples:
            return 0.0
        values = sorted(samples)
        index = max(0, math.ceil(len(values) * percentile) - 1)
        return values[index] * 1000.0

    def record_would_lock(self, pnl: float):
        """LOW: accumulate edge we SAW but rejected (stale/no-entry/risk/snapshot).
        Tells us how much PnL we forwent to the gates — useful calibration signal."""
        self.would_lock_pnl += pnl

    def record_macro_skipped(self, pnl: float = 0.0):
        """TASK A: a trade opportunity was rejected during an active HIGH-impact macro
        window. Increment the skip counter and accumulate the forgone edge value so the
        macro gate's cost is observable alongside `would_lock`."""
        self.macro_skipped += 1
        self.macro_skipped_pnl += pnl

    def record_freshness_attempt(self, age_ms: float, filled: bool):
        band = classify_freshness_ms(age_ms)
        self.fresh_attempts[band] += 1
        if filled:
            self.fills_by_band[band] += 1

    def record_orders_sent(self, mode: str, count: int = 1):
        """TASK 2: count orders sent under a specific execution mode."""
        mode = mode or "live"
        self.orders_sent_by_mode[mode] = self.orders_sent_by_mode.get(mode, 0) + int(count)

    def record_fills_for_mode(self, mode: str, count: int = 1):
        """TASK 2: count filled orders under a specific execution mode."""
        mode = mode or "live"
        self.fills_by_mode[mode] = self.fills_by_mode.get(mode, 0) + int(count)

    def record_strategy_rejection(self, reason: str) -> None:
        self.strategy_rejections[reason] = self.strategy_rejections.get(reason, 0) + 1

    def p50_opp_to_order_ms(self) -> float:
        return self._percentile_ms(self._ring, 0.50)

    def p95_opp_to_order_ms(self) -> float:
        return self._percentile_ms(self._ring, 0.95)

    def p99_opp_to_order_ms(self) -> float:
        return self._percentile_ms(self._ring, 0.99)

    def stage_latency_ms(self, name: str, percentile: float = 0.50) -> float:
        """Return a bounded-sample stage percentile in milliseconds."""
        return self._percentile_ms(self._stage_samples.get(name, []), percentile)

    def avg_opp_to_order_ms(self) -> float:
        if self.acks == 0:
            return 0.0
        return (self.opp_to_order_sum / self.acks) * 1000.0

    def avg_ack_ms(self) -> float:
        if self.acks == 0:
            return 0.0
        return (self.ack_latency_sum / self.acks) * 1000.0

    @property
    def partial_rate(self) -> float:
        if self.opportunities == 0:
            return 0.0
        return self.partial_fills / self.opportunities

    def summary(self) -> str:
        bands = " ".join(
            f"{b}={self.fills_by_band[b]}/{self.fresh_attempts[b]}" for b in FRESHNESS_BAND_LABELS
        )
        modes = " ".join(
            f"{m}={self.orders_sent_by_mode.get(m, 0)}s/{self.fills_by_mode.get(m, 0)}f"
            for m in ("dry_run", "paper", "live")
        )
        return (
            f"loops={self.loops} opps={self.opportunities} sent={self.orders_sent} "
            f"fills={self.fills} partial={self.partial_fills} resolved={self.partial_resolved} "
            f"missed={self.missed} template_unavailable={self.template_unavailable} "
            f"modes_by_order/fill[{modes}] "
            f"opp2order_avg_ms={self.avg_opp_to_order_ms():.2f} "
            f"p50_ms={self.p50_opp_to_order_ms():.2f} "
            f"p95_ms={self.p95_opp_to_order_ms():.2f} "
            f"p99_ms={self.p99_opp_to_order_ms():.2f} "
            f"ack_avg_ms={self.avg_ack_ms():.2f} "
            f"stage_reserve_p50_ms={self.stage_latency_ms('reserve', 0.50):.2f} "
            f"stage_submit_p50_ms={self.stage_latency_ms('dual_submit', 0.50):.2f} "
            f"stage_postsubmit_p50_ms={self.stage_latency_ms('post_submit', 0.50):.2f} "
            f"locked_pnl={self.locked_pnl:.4f} residual_pnl={self.residual_pnl:.4f} "
            f"session_pnl={self.session_pnl:.4f} would_lock={self.would_lock_pnl:.4f} "
            f"macro_skipped={self.macro_skipped} macro_skipped_pnl={self.macro_skipped_pnl:.4f} "
            f"sequential_first={self.sequential_first_fills} "
            f"sequential_pairs={self.sequential_pairs} "
            f"sequential_timeouts={self.sequential_timeouts} "
            f"sequential_exits={self.sequential_exits} "
            f"bandfills[{bands}]"
        )
