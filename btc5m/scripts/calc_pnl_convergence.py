"""A3: Calculate PnL convergence — real locked_pnl vs theoretical expected_pnl.

Separates three quantities that earlier reports mixed together:

* expected committed PnL: decision-price edge on submitted pairs;
* realized locked PnL: PnL at recorded simulated/real fill prices; and
* raw detected PnL: decision-price edge across submitted and rejected signals.

Target: real locked_pnl within +/-10% of expected_pnl.

expected committed PnL = sum over submitted executions:
``(1.0 - combined_at_decision) * realized_size``.  Pre-submit rejections are
reported separately as raw detected/forgone edge.

For clean fills, actual PnL is ``(1.0 - fill_combined) * realized_size``.
For resolved partials, actual PnL is the recorded hedge or protective-exit result.
For missed trades, gross forgone PnL is recomputed as
``(1.0 - combined) * size``. Historical ``would_lock`` fields used threshold
surplus and are retained only as a diagnostic comparison.

Usage:
    .venv/bin/python scripts/calc_pnl_convergence.py [path_to_bot_jsonl]
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path


def parse_summary_line(path: Path) -> dict:
    """Parse the 'session metrics:' summary line from the console log or JSONL."""
    patterns = {
        "loops": r"loops=(\d+)",
        "opps": r"opps=(\d+)",
        "sent": r"sent=(\d+)",
        "fills": r"fills=(\d+)",
        "partial": r"partial=(\d+)",
        "resolved": r"resolved=(\d+)",
        "missed": r"missed=(\d+)",
        "opp2order_avg_ms": r"opp2order_avg_ms=([\d.]+)",
        "p50_ms": r"p50_ms=([\d.]+)",
        "ack_avg_ms": r"ack_avg_ms=([\d.]+)",
        "locked_pnl": r"locked_pnl=([\d.]+)",
        "residual_pnl": r"residual_pnl=([\d.]+)",
        "session_pnl": r"session_pnl=([\d.]+)",
        "would_lock": r"would_lock=([\d.]+)",
        "macro_skipped": r"macro_skipped=(\d+)",
        "fresh_fills": r"fresh_fills=(\d+)",
        "fresh_attempts": r"fresh_attempts=(\d+)",
    }
    metrics = {}
    with open(path, "r") as f:
        for line in f:
            if "session metrics:" in line:
                for key, pat in patterns.items():
                    match = re.search(pat, line)
                    if match:
                        val = match.group(1)
                        if key in ("opps", "sent", "fills", "partial", "resolved",
                                   "missed", "macro_skipped", "fresh_fills",
                                   "fresh_attempts", "loops"):
                            metrics[key] = int(val)
                        else:
                            metrics[key] = float(val)
                break
    return metrics


def parse_bot_jsonl(path: Path) -> dict:
    """Parse bot.jsonl for TRADE events and compute PnL from individual events."""
    fills = []      # filled events with combined + pnl
    partials = []   # one-leg fills resolved by hedge/exit
    missed = []     # missed events with combined + would_lock

    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue

            if rec.get("event") != "TRADE":
                continue

            msg = rec.get("msg", "")
            data = rec.get("data", {})

            if msg == "filled":
                fills.append({
                    "t": rec.get("t", ""),
                    "market_id": data.get("market_id", ""),
                    "combined": data.get("combined", 0.0),
                    "decision_combined": data.get("decision_combined", data.get("combined", 0.0)),
                    "decision_yes_ask": data.get("decision_yes_ask"),
                    "decision_no_ask": data.get("decision_no_ask"),
                    "fill_yes_price": data.get("fill_yes_price"),
                    "fill_no_price": data.get("fill_no_price"),
                    "fill_yes_source": data.get("fill_yes_source"),
                    "fill_no_source": data.get("fill_no_source"),
                    "size": data.get("size"),
                    "expected_pnl": data.get("expected_pnl"),
                    "pnl_locked": data.get("pnl_locked", 0.0),
                    "age_ms": data.get("age_ms", 0),
                    "freshness_band": data.get("freshness_band", "unknown"),
                })
            elif msg == "partial_resolved":
                partials.append({
                    "t": rec.get("t", ""),
                    "market_id": data.get("market_id", ""),
                    "action": data.get("action", "unknown"),
                    "decision_combined": data.get("decision_combined"),
                    "filled_entry_price": data.get("filled_entry_price"),
                    "filled_entry_source": data.get("filled_entry_source"),
                    "hedge_fill_price": data.get("hedge_fill_price"),
                    "hedge_fill_source": data.get("hedge_fill_source"),
                    "exit_price": data.get("exit_price"),
                    "resolved_combined": data.get("resolved_combined"),
                    "size": data.get("size"),
                    "expected_pnl": data.get("expected_pnl"),
                    "realized_pnl": data.get(
                        "realized_pnl",
                        data.get("residual_pnl", data.get("pnl_locked")),
                    ),
                    "age_ms": data.get("age_ms", 0),
                    "freshness_band": data.get("freshness_band", "unknown"),
                })
            elif msg == "missed":
                missed.append({
                    "t": rec.get("t", ""),
                    "combined": data.get("combined", 0.0),
                    "size": data.get("size"),
                    "would_lock": data.get("would_lock", 0.0),
                    "reason": data.get("reason", "unknown"),
                    "age_ms": data.get("age_ms", 0),
                    "freshness_band": data.get("freshness_band", "unknown"),
                })

    return {"fills": fills, "partials": partials, "missed": missed}


def compute_pnl_convergence(
    fills: list, missed: list, order_size: float = 10.0,
    partials: list | None = None,
) -> dict:
    """Separate raw detected edge from submitted-order convergence.

    ``expected_raw_pnl`` is an accounting decomposition, not a convergence target:
    realized locked PnL + stale-quote misses + every other miss.  It therefore
    reconciles exactly even when a fill has slippage.

    The convergence gate intentionally compares actual realized PnL with
    ``expected_committed_pnl``: theoretical edge on orders that were actually
    submitted (clean fills, resolved partial fills, and explicit
    ``no_fill_in_window`` submissions).
    Pre-submit rejections remain visible in the raw decomposition, but cannot
    make submitted execution fail the gate.

    For each opportunity (fill or miss), expected_pnl_contribution = (1.0 - combined) * order_size.
    For fills, real_pnl_contribution = (1.0 - combined) * order_size (same if fill at observed price).
    For misses, real_pnl_contribution = 0 (no PnL realized).

    The convergence measures: did the bot actually realize the PnL that the edges it saw
    would have generated, or did freshness rejections / no-fills cause slippage?
    """
    partials = partials or []
    total_opportunities = len(fills) + len(partials) + len(missed)

    def fill_size(fill: dict) -> float:
        return float(fill.get("size") if fill.get("size") is not None else order_size)

    def fill_expected(fill: dict) -> float:
        if fill.get("expected_pnl") is not None:
            return float(fill["expected_pnl"])
        decision_combined = float(fill.get("decision_combined", fill.get("combined", 0.0)))
        return (1.0 - decision_combined) * fill_size(fill)

    def partial_size(partial: dict) -> float:
        return float(partial.get("size") if partial.get("size") is not None else order_size)

    def partial_expected(partial: dict) -> float | None:
        if partial.get("expected_pnl") is not None:
            return float(partial["expected_pnl"])
        if partial.get("decision_combined") is None:
            return None
        return (1.0 - float(partial["decision_combined"])) * partial_size(partial)

    def partial_realized(partial: dict) -> float | None:
        value = partial.get("realized_pnl")
        return None if value is None else float(value)

    def missed_expected(miss: dict) -> float:
        size = float(miss.get("size") if miss.get("size") is not None else order_size)
        return (1.0 - float(miss.get("combined", 0.0))) * size

    realized_clean = sum(float(f.get("pnl_locked", 0.0)) for f in fills)
    known_partial_expected = [value for value in map(partial_expected, partials) if value is not None]
    known_partial_realized = [value for value in map(partial_realized, partials) if value is not None]
    unknown_partial_expected_count = len(partials) - len(known_partial_expected)
    unknown_partial_realized_count = len(partials) - len(known_partial_realized)
    expected_partial = sum(known_partial_expected)
    realized_partial = sum(known_partial_realized)
    realized_committed = realized_clean + realized_partial
    expected_filled = sum(fill_expected(f) for f in fills)
    expected_committed = expected_filled + expected_partial
    # A no-fill window is the one missed event emitted after both legs were sent;
    # it must remain in the submitted baseline so failed execution is measurable.
    expected_committed += sum(
        missed_expected(m)
        for m in missed if m.get("reason") == "no_fill_in_window"
    )
    missed_stale_quote = sum(missed_expected(m)
                             for m in missed if m.get("reason") == "stale_quote")
    missed_other = sum(missed_expected(m)
                       for m in missed if m.get("reason") != "stale_quote")
    expected_raw = expected_filled + expected_partial + missed_stale_quote + missed_other
    logged_would_lock = sum(float(m.get("would_lock", 0.0)) for m in missed)

    # Also compute from combined for verification
    real_locked_from_combined = 0.0
    for f in fills:
        real_locked_from_combined += (1.0 - float(f["combined"])) * fill_size(f)

    # Forgone PnL: from misses
    would_lock_pnl = missed_stale_quote + missed_other

    # Convergence ratio
    sample_available = (
        expected_committed > 0
        and unknown_partial_expected_count == 0
        and unknown_partial_realized_count == 0
    )
    if sample_available:
        convergence_pct = (realized_committed / expected_committed) * 100.0
        deviation_pct = ((realized_committed - expected_committed) / expected_committed) * 100.0
    else:
        convergence_pct = 0.0
        deviation_pct = 0.0

    # Target: +/-10% deviation
    target_pct = 10.0
    within_target = sample_available and abs(deviation_pct) <= target_pct

    return {
        "total_opportunities": total_opportunities,
        "fills": len(fills),
        "partials": len(partials),
        "committed_executions": len(fills) + len(partials),
        "missed": len(missed),
        # Deprecated compatibility alias for raw detected edge; never used as
        # a convergence denominator or displayed as one by this CLI.
        "expected_pnl": round(expected_raw, 4),
        "expected_raw_pnl": round(expected_raw, 4),
        "expected_committed_pnl": round(expected_committed, 4),
        "expected_filled_pnl": round(expected_filled, 4),
        "expected_partial_pnl": round(expected_partial, 4),
        "realized_clean_pnl": round(realized_clean, 4),
        "realized_partial_pnl": round(realized_partial, 4),
        "realized_committed_pnl": round(realized_committed, 4),
        "realized_locked": round(realized_committed, 4),
        "missed_stale_quote": round(missed_stale_quote, 4),
        "missed_other": round(missed_other, 4),
        # Compatibility alias retained for dashboard consumers; this now includes
        # resolved partial PnL and is therefore total realized committed PnL.
        "real_locked_pnl": round(realized_committed, 4),
        "real_locked_from_combined": round(real_locked_from_combined, 4),
        "would_lock_pnl": round(would_lock_pnl, 4),
        "logged_would_lock_pnl": round(logged_would_lock, 4),
        "convergence_pct": round(convergence_pct, 2),
        "deviation_pct": round(deviation_pct, 2),
        "target_pct": target_pct,
        "PASS": within_target,
        "sample_available": sample_available,
        "raw_accounting_complete": unknown_partial_expected_count == 0,
        "unknown_partial_expected_count": unknown_partial_expected_count,
        "unknown_partial_realized_count": unknown_partial_realized_count,
        "order_size": order_size,
        "fill_breakdown": [
            {
                "t": f.get("t", ""),
                "market_id": f.get("market_id", ""),
                "size": fill_size(f),
                "decision_combined": float(f.get("decision_combined", f.get("combined", 0.0))),
                "decision_yes_ask": f.get("decision_yes_ask"),
                "decision_no_ask": f.get("decision_no_ask"),
                "fill_combined": float(f.get("combined", 0.0)),
                "fill_yes_price": f.get("fill_yes_price"),
                "fill_no_price": f.get("fill_no_price"),
                "fill_yes_source": f.get("fill_yes_source"),
                "fill_no_source": f.get("fill_no_source"),
                "expected_pnl": round(fill_expected(f), 4),
                "realized_pnl": round(float(f.get("pnl_locked", 0.0)), 4),
                "pnl_delta": round(float(f.get("pnl_locked", 0.0)) - fill_expected(f), 4),
            }
            for f in fills
        ],
        "partial_breakdown": [
            {
                "t": p.get("t", ""),
                "market_id": p.get("market_id", ""),
                "action": p.get("action", "unknown"),
                "size": partial_size(p),
                "decision_combined": p.get("decision_combined"),
                "filled_entry_price": p.get("filled_entry_price"),
                "filled_entry_source": p.get("filled_entry_source"),
                "hedge_fill_price": p.get("hedge_fill_price"),
                "hedge_fill_source": p.get("hedge_fill_source"),
                "exit_price": p.get("exit_price"),
                "resolved_combined": p.get("resolved_combined"),
                "expected_pnl": (
                    round(partial_expected(p), 4)
                    if partial_expected(p) is not None else None
                ),
                "realized_pnl": (
                    round(partial_realized(p), 4)
                    if partial_realized(p) is not None else None
                ),
                "pnl_delta": (
                    round(partial_realized(p) - partial_expected(p), 4)
                    if partial_realized(p) is not None and partial_expected(p) is not None
                    else None
                ),
            }
            for p in partials
        ],
    }


def analyze_by_band(
    fills: list, missed: list, order_size: float = 10.0,
    partials: list | None = None,
) -> list:
    """Analyze PnL convergence by freshness band."""
    bands = ["fresh", "0.5-2s", "2-10s", "stale"]
    results = []

    for band in bands:
        band_fills = [f for f in fills if f["freshness_band"] == band]
        band_partials = [p for p in (partials or []) if p["freshness_band"] == band]
        band_missed = [m for m in missed if m["freshness_band"] == band]

        if not band_fills and not band_partials and not band_missed:
            continue

        conv = compute_pnl_convergence(
            band_fills, band_missed, order_size, partials=band_partials,
        )
        conv["band"] = band
        results.append(conv)

    return results


def compute_pnl_from_summary(sm: dict) -> dict:
    """Compute PnL convergence from session summary metrics.

    expected_pnl = locked_pnl + would_lock (if all detected opportunities had filled)
    real_locked_pnl = locked_pnl (from fills only)

    The deviation measures the PnL forgone by rejecting stale/no-entry opportunities
    and by execution no-fills.
    """
    locked = sm.get("locked_pnl", 0.0)
    would_lock = sm.get("would_lock", 0.0)
    opps = sm.get("opps", 0)
    fills = sm.get("fills", 0)
    missed = sm.get("missed", 0)
    sent = sm.get("sent", 0)

    # Summary-only data cannot reconstruct the reason buckets or submitted prices.
    # Treat locked PnL as the best available committed baseline, so misses do not
    # poison the submitted-order gate; event-level reports provide the full split.
    expected_pnl = locked + would_lock
    expected_committed = locked
    real_locked_pnl = locked

    sample_available = expected_committed > 0
    if sample_available:
        convergence_pct = (real_locked_pnl / expected_committed) * 100.0
        deviation_pct = ((real_locked_pnl - expected_committed) / expected_committed) * 100.0
    else:
        convergence_pct = 0.0
        deviation_pct = 0.0

    target_pct = 10.0
    within_target = sample_available and abs(deviation_pct) <= target_pct

    return {
        "total_opportunities": opps + missed,
        "opps": opps,
        "fills": fills,
        "missed": missed,
        "sent": sent,
        # Deprecated compatibility alias; see expected_raw_pnl.
        "expected_pnl": round(expected_pnl, 4),
        "expected_raw_pnl": round(expected_pnl, 4),
        "expected_committed_pnl": round(expected_committed, 4),
        "realized_locked": round(real_locked_pnl, 4),
        "missed_stale_quote": None,
        "missed_other": round(would_lock, 4),
        "real_locked_pnl": round(real_locked_pnl, 4),
        "would_lock_pnl": round(would_lock, 4),
        "convergence_pct": round(convergence_pct, 2),
        "deviation_pct": round(deviation_pct, 2),
        "target_pct": target_pct,
        "PASS": within_target,
        "sample_available": sample_available,
    }


def main():
    jsonl_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("state/bot.jsonl")
    # Also try to find a console log with session metrics
    console_log = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("state/session_2026_08_05.log")
    if not console_log.exists():
        console_log = Path("state/session_A1_cached.log")

    print("=" * 70)
    print("A3: PnL Convergence Analysis")
    print("=" * 70)
    print(f"Source (JSONL): {jsonl_path}")
    if console_log.exists():
        print(f"Source (console): {console_log}")
    print()

    # Try JSONL first (has per-trade data with combined prices)
    fills = []
    partials = []
    missed = []
    if jsonl_path.exists():
        data = parse_bot_jsonl(jsonl_path)
        fills = data["fills"]
        partials = data["partials"]
        missed = data["missed"]
        print(
            f"From {jsonl_path.name}: {len(fills)} clean fills, "
            f"{len(partials)} resolved partials, {len(missed)} missed TRADE events"
        )

    # Also parse summary from console log
    sm = {}
    if console_log.exists():
        sm = parse_summary_line(console_log)
        if sm:
            print(f"From {console_log.name} summary: opps={sm.get('opps', '?')} "
                  f"fills={sm.get('fills', '?')} missed={sm.get('missed', '?')} "
                  f"locked_pnl={sm.get('locked_pnl', '?')} would_lock={sm.get('would_lock', '?')}")
    print()

    # Compute from JSONL events (per-trade level)
    if fills or partials or missed:
        result = compute_pnl_convergence(
            fills, missed, order_size=10.0, partials=partials,
        )

        print(f"--- Overall PnL Convergence (from JSONL events) ---")
        print(f"Total opportunities:  {result['total_opportunities']}")
        print(f"  Clean fills:        {result['fills']}")
        print(f"  Resolved partials:  {result['partials']}")
        print(f"  Missed:             {result['missed']}")
        print()
        print(f"Raw detected edge (accounting only):                    ${result['expected_raw_pnl']:.4f}")
        print(f"  clean + partial + stale + other:                      ${result['expected_filled_pnl']:.4f} + ${result['expected_partial_pnl']:.4f} + ${result['missed_stale_quote']:.4f} + ${result['missed_other']:.4f}")
        print(f"  historical logged would_lock diagnostic:              ${result['logged_would_lock_pnl']:.4f}")
        print(f"Expected committed PnL (convergence baseline):          ${result['expected_committed_pnl']:.4f}")
        print(f"Realized committed PnL (clean + partial):               ${result['realized_committed_pnl']:.4f}")
        print(f"Forgone PnL (missed opps):                             ${result['would_lock_pnl']:.4f}")
        print()
        if result["sample_available"]:
            print(f"Convergence: {result['real_locked_pnl']:.4f} / {result['expected_committed_pnl']:.4f} = {result['convergence_pct']:.2f}%")
            print(f"Deviation:   {result['deviation_pct']:.2f}%")
        else:
            print("Convergence: UNAVAILABLE (committed decision-edge denominator incomplete)")
            print(
                "Incomplete partial records: "
                f"expected={result['unknown_partial_expected_count']} "
                f"realized={result['unknown_partial_realized_count']}"
            )
        print(f"Target:      ±{result['target_pct']}%")
        print(
            "Status:      "
            + ("PASS" if result["PASS"] else ("FAIL" if result["sample_available"] else "UNAVAILABLE"))
        )
        print()

        # Per-band analysis
        band_results = analyze_by_band(
            fills, missed, order_size=10.0, partials=partials,
        )
        print(f"--- PnL Convergence by Freshness Band ---")
        for br in band_results:
            print(f"  {br['band']:>12s}: opps={br['total_opportunities']:4d} "
                  f"fills={br['fills']} partials={br['partials']} missed={br['missed']} "
                  f"raw=${br['expected_raw_pnl']:.2f} committed=${br['expected_committed_pnl']:.2f} real=${br['real_locked_pnl']:.2f} "
                  f"conv={'%.1f%%' % br['convergence_pct'] if br['sample_available'] else 'UNAVAILABLE'} "
                  f"dev={'%.1f%%' % br['deviation_pct'] if br['sample_available'] else 'UNAVAILABLE'} "
                  f"{'PASS' if br['PASS'] else ('FAIL' if br['sample_available'] else 'UNAVAILABLE')}")
        print()
    else:
        # No JSONL trades available — fall back to summary-based analysis
        print("No per-trade JSONL events found. Using session summary metrics.")
        if sm:
            result = compute_pnl_from_summary(sm)
        else:
            print("  No session summary available either. Cannot compute PnL convergence.")
            print("=" * 70)
            return

        print(f"--- PnL Convergence (from session summary) ---")
        print(f"Total opportunities:  {result['total_opportunities']}")
        print(f"  Opps detected:      {result['opps']}")
        print(f"  Orders sent:        {result['sent']} (individual legs)")
        print(f"  Fills (pairs):      {result['fills']}")
        print(f"  Missed:             {result['missed']}")
        print()
        print(f"Raw detected edge (locked + would_lock):            ${result['expected_raw_pnl']:.4f}")
        print(f"Expected committed PnL (summary fallback baseline): ${result['expected_committed_pnl']:.4f}")
        print(f"Real locked PnL (from fills):                      ${result['real_locked_pnl']:.4f}")
        print(f"Forgone PnL (would_lock from misses):              ${result['would_lock_pnl']:.4f}")
        print()
        print(f"Convergence: {result['real_locked_pnl']:.4f} / {result['expected_committed_pnl']:.4f} = {result['convergence_pct']:.2f}%")
        print(f"Deviation:   {result['deviation_pct']:.2f}%")
        print(f"Target:      ±{result['target_pct']}%")
        print(f"Status:      {'PASS' if result['PASS'] else 'FAIL'}")
        print()

    # Final verdict
    if fills or partials or missed:
        if not result["sample_available"]:
            print("VERDICT: UNAVAILABLE — committed decision-edge evidence is incomplete")
        elif result["PASS"]:
            print(f"VERDICT: {abs(result['deviation_pct']):.2f}% deviation within ±{result['target_pct']}% target")
            print(f"  PASS — real PnL is within tolerance of theoretical")
        else:
            print(f"VERDICT: {result['deviation_pct']:.2f}% deviation exceeds ±{result['target_pct']}% target")
            print(f"  FAIL — gap between expected and real PnL")
            gap = result["expected_committed_pnl"] - result["real_locked_pnl"]
            if gap > 0:
                print(f"  Forgone: ${gap:.4f} from {result['missed']} missed opportunities")
                if result["would_lock_pnl"] > 0:
                    conv_ratio = (result["would_lock_pnl"] / result["expected_raw_pnl"]) * 100 if result["expected_raw_pnl"] else 0
                    print(f"  Would-lock: ${result['would_lock_pnl']:.4f} ({conv_ratio:.1f}% of expected)")

    if sm and not fills and not partials and not missed:
        if result["PASS"]:
            print(f"VERDICT: {abs(result['deviation_pct']):.2f}% deviation within ±{result['target_pct']}% target")
            print(f"  PASS — real PnL is within tolerance of theoretical")
        else:
            print(f"VERDICT: {result['deviation_pct']:.2f}% deviation exceeds ±{result['target_pct']}% target")
            print(f"  FAIL — gap between expected and real PnL")
            gap = result["expected_committed_pnl"] - result["real_locked_pnl"]
            if gap > 0:
                print(f"  Forgone: ${gap:.4f} from {result['missed']} missed opportunities")
                conv_ratio = (result["would_lock_pnl"] / result["expected_raw_pnl"]) * 100 if result["expected_raw_pnl"] else 0
                print(f"  Would-lock: ${result['would_lock_pnl']:.4f} ({conv_ratio:.1f}% of expected)")

    print("=" * 70)


if __name__ == "__main__":
    main()
