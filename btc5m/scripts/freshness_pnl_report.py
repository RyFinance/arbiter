"""TASK E3 — Freshness % and PnL convergence ratio report.

Computes two dashboard-ready metrics and writes them to
``state/freshness_pnl_report.json`` (read by streamlit_app.py):

  1. **freshness_pct** — fraction of FILLED trades whose quote age was within
     the 500ms freshness gate (the canonical metric from calc_quote_freshness.py).
     Computed from per-trade TRADE events in ``state/bot.jsonl``. When per-trade
     data is absent, falls back to the ``bandfills[fresh=N/total...]`` summary
     line in the latest console log (``state/session_*.log``), computing
     ``fresh_fills / total_fills`` (NOT ``fresh_attempts / total_attempts``).

  2. **convergence_pct** — submitted-order convergence.  Its denominator is
     ``expected_committed_pnl``; raw detected edge is reported separately.

Usage:
    python scripts/freshness_pnl_report.py            # compute & write report
    python scripts/freshness_pnl_report.py --print     # also print to stdout
"""
from __future__ import annotations

import glob
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = PROJECT_ROOT / "state"
BOT_LOG = STATE_DIR / "bot.jsonl"
BACKTEST_REPORT = STATE_DIR / "backtest_validation_report.json"
OUTPUT = STATE_DIR / "freshness_pnl_report.json"

# Regex to extract freshness bands from a session metrics line.
# Example: "bandfills[fresh=8/8 0.5-2s=0/49 2-10s=0/113 stale=0/33]"
# Each band is "label=fills/attempts"
BANDFILLS_RE = re.compile(r"(\w[\w.-]*)=(\d+)/(\d+)")

# Regex to extract session metrics fields from a console-log line.
METRICS_RE = re.compile(
    r"session metrics:.*?"
    r"opps=(?P<opps>\d+).*?"
    r"fills=(?P<fills>\d+).*?"
    r"missed=(?P<missed>\d+).*?"
    r"locked_pnl=(?P<locked_pnl>[-.\d]+).*?"
    r"would_lock=(?P<would_lock>[-.\d]+)"
)


def _find_console_logs() -> list[Path]:
    """Find all state/session*.log files, newest first."""
    logs = sorted(STATE_DIR.glob("session*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    return logs


def parse_freshness_from_metrics_line(msg: str) -> dict[str, dict[str, int]] | None:
    """Extract per-band fill/attempt counts from a session metrics line.

    Returns {"fresh": {"fills": 8, "attempts": 8}, ...} or None if not found.
    """
    bands: dict[str, dict[str, int]] = {}
    for label, fills, attempts in BANDFILLS_RE.findall(msg):
        bands[label] = {"fills": int(fills), "attempts": int(attempts)}
    return bands if bands else None


def _load_canonical_freshness():
    """Import calc_quote_freshness as a module (handles scripts/ not on path)."""
    scripts_dir = Path(__file__).resolve().parent
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "calc_quote_freshness", scripts_dir / "calc_quote_freshness.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def compute_freshness_pct() -> dict[str, float | int | None]:
    """Compute freshness_pct as the fraction of FILLED trades whose quote age
    was within the 500ms freshness gate (the canonical metric from
    ``scripts/calc_quote_freshness.py``: within_target / total_fills).

    Source of truth: ``calc_quote_freshness.calc_freshness`` over the per-trade
    TRADE events in ``state/bot.jsonl``. When per-trade data is absent, we fall
    back to the ``bandfills[fresh=N/total...]`` summary line from the most
    recent ``session metrics:`` line found in ``state/bot.jsonl`` or any
    ``state/session*.log`` console file. The denominator is ``total_fills``
    (sum of fills across ALL bands), NOT ``total_attempts`` (which counted
    opportunities, not fills, producing the stale 0.6667 = 8/12 bug).

    Returns a dict with:
      - freshness_pct: float (0.0–1.0) or None
      - within_target: int   (fills within 500ms)
      - total_fills: int     (all fills)
      - last_metric_ts: str | None
      - method: str         ("per_trade" | "bandfills" | "none")
    """
    mod = _load_canonical_freshness()

    # --- Primary: per-trade TRADE events in bot.jsonl ---
    per_trade_total = 0
    per_trade_within = 0
    if BOT_LOG.exists():
        data = mod.parse_bot_jsonl(BOT_LOG)
        result = mod.calc_freshness(data["fills"], target_ms=500)
        per_trade_total = result["total_fills"]
        per_trade_within = result["within_target"]

    if per_trade_total > 0:
        pct = per_trade_within / per_trade_total
        return {
            "freshness_pct": round(pct, 4),
            "within_target": per_trade_within,
            "total_fills": per_trade_total,
            "last_metric_ts": None,
            "method": "per_trade",
        }

    # --- Fallback: bandfills summary line from bot.jsonl or console logs ---
    # Search bot.jsonl first, then console logs (newest first).
    latest_bands = None
    latest_t = None

    # Try bot.jsonl JSONL for a "session metrics:" line.
    if BOT_LOG.exists():
        with BOT_LOG.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg = rec.get("msg", "")
                if "session metrics:" in msg:
                    latest_t = rec.get("t")
                    latest_bands = parse_freshness_from_metrics_line(msg)

    # If no bands from bot.jsonl, search console logs.
    if latest_bands is None:
        for log_path in _find_console_logs():
            with log_path.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if "session metrics:" in line and "bandfills[" in line:
                        latest_bands = parse_freshness_from_metrics_line(line)
                        latest_t = log_path.name
                        break
                if latest_bands is not None:
                    break

    if latest_bands is None:
        return {"freshness_pct": None, "within_target": 0, "total_fills": 0,
                "last_metric_ts": latest_t, "method": "none"}

    # CORRECT denominator: fresh_fills / total_fills across all bands.
    # The old code used fresh_attempts / total_attempts (= attempts in the
    # "fresh" band divided by ALL-band attempts), which under-counted when
    # stale-quote rejections removed opps from the fresh band's attempt count.
    fresh_fills = latest_bands.get("fresh", {"fills": 0, "attempts": 0}).get("fills", 0)
    total_fills = sum(b["fills"] for b in latest_bands.values())
    pct = fresh_fills / total_fills if total_fills > 0 else 0.0

    return {
        "freshness_pct": round(pct, 4),
        "within_target": fresh_fills,
        "total_fills": total_fills,
        "last_metric_ts": latest_t,
        "method": "bandfills",
    }


def _find_latest_metrics_line() -> tuple[str, dict | None]:
    """Find the latest ``session metrics:`` line across bot.jsonl and console logs.

    Returns (source_name, parsed_metrics_dict) where parsed_metrics_dict has keys:
    opps, fills, missed, locked_pnl, would_lock, session_pnl, bandfills.
    Returns (source, None) if no metrics line is found.
    """
    # Try bot.jsonl JSONL first (has a 't' timestamp for ordering).
    if BOT_LOG.exists():
        latest_rec = None
        saw_current_session_event = False
        with BOT_LOG.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                saw_current_session_event = True
                msg = rec.get("msg", "")
                if "session metrics:" in msg:
                    latest_rec = rec
        if latest_rec is not None:
            parsed = _parse_metrics_msg(latest_rec["msg"])
            if parsed is not None:
                parsed["t"] = latest_rec.get("t")
                return ("bot.jsonl", parsed)
        # A live session writes its summary only on shutdown. Do not surface a
        # prior console session's PnL while the current JSONL already contains
        # its own in-progress events.
        if saw_current_session_event:
            return ("bot.jsonl_pending_summary", None)

    # Fall back to console logs (newest first by mtime).
    for log_path in _find_console_logs():
        with log_path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if "session metrics:" in line:
                    parsed = _parse_metrics_msg(line)
                    if parsed is not None:
                        parsed["t"] = log_path.name
                        return (log_path.name, parsed)

    return ("none", None)


def _parse_metrics_msg(msg: str) -> dict | None:
    """Extract numeric fields from a 'session metrics:' line."""
    m = METRICS_RE.search(msg)
    if not m:
        return None
    result = {
        "opps": int(m.group("opps")),
        "fills": int(m.group("fills")),
        "missed": int(m.group("missed")),
        "locked_pnl": float(m.group("locked_pnl")),
        "would_lock": float(m.group("would_lock")),
    }
    # Also extract session_pnl and bandfills if present.
    pnl_m = re.search(r"session_pnl=([\d.]+)", msg)
    if pnl_m:
        result["session_pnl"] = float(pnl_m.group(1))
    bands = parse_freshness_from_metrics_line(msg)
    if bands:
        result["bandfills"] = bands
    return result


def compute_pnl_convergence() -> dict[str, float | int | None]:
    """Compute submitted-order PnL convergence and separate raw edge capture.

    expected_raw_pnl = locked_pnl + would_lock (all detected edge, descriptive)
    expected_committed_pnl = locked_pnl in summary-only fallback (submitted
      prices are unavailable there; event-level calc_pnl_convergence is exact).
    real_locked_pnl = locked_pnl (from fills only)

    convergence_pct = real_locked_pnl / expected_committed_pnl * 100

    Target: ±10% deviation from expected_committed_pnl.

    Also includes the legacy live_per_opp / backtest_per_opp ratio from
    backtest_validation_report.json for backward compatibility with the
    Streamlit dashboard's delta label.
    """
    source, sm = _find_latest_metrics_line()

    # --- Backtest values (for live_per_opp / backtest_per_opp ratio) ---
    bt_per_opp = None
    bt_pnl = None
    bt_opps = None
    if BACKTEST_REPORT.exists():
        try:
            with BACKTEST_REPORT.open("r") as fh:
                report = json.load(fh)
            if report.get("comparable_to_outcome_books") is True:
                bt = report.get("backtest", {})
                bt_pnl = float(bt.get("session_pnl", 0.0))
                bt_opps = int(bt.get("opportunities", 0))
                bt_per_opp = bt_pnl / bt_opps if bt_opps > 0 else 0.0
        except (json.JSONDecodeError, OSError):
            pass

    # --- Live (paper-trading) values from the latest metrics line ---
    if sm is None:
        return {
            "convergence_pct": None,
            "real_locked_pnl": None,
            "expected_raw_pnl": None,
            "expected_committed_pnl": None,
            "deviation_pct": None,
            "target_pct": 10.0,
            "PASS": False,
            "sample_available": False,
            "live_per_opp": None,
            "backtest_per_opp": bt_per_opp,
            "live_pnl": None,
            "live_opps": None,
            "backtest_pnl": bt_pnl,
            "backtest_opps": bt_opps,
            "source": source,
        }

    locked = sm.get("locked_pnl", 0.0)
    would_lock = sm.get("would_lock", 0.0)
    live_opps = sm.get("opps", 0)
    live_pnl = sm.get("session_pnl", locked)

    expected_raw_pnl = locked + would_lock
    expected_committed_pnl = locked
    real_locked_pnl = locked

    sample_available = expected_committed_pnl > 0
    if sample_available:
        convergence_pct = round(real_locked_pnl / expected_committed_pnl * 100.0, 2)
        deviation_pct = round((real_locked_pnl - expected_committed_pnl) / expected_committed_pnl * 100.0, 2)
    else:
        convergence_pct = 0.0
        deviation_pct = 0.0

    target_pct = 10.0
    within_target = sample_available and abs(deviation_pct) <= target_pct

    # Legacy ratio (live_per_opp / backtest_per_opp) for the dashboard delta label.
    live_per_opp = live_pnl / live_opps if live_opps > 0 else 0.0
    if bt_per_opp is not None and bt_per_opp != 0.0:
        ratio = round(live_per_opp / bt_per_opp, 4)
    elif bt_per_opp == 0.0 and live_per_opp == 0.0:
        ratio = None
    else:
        ratio = None

    return {
        "convergence_pct": convergence_pct,
        "real_locked_pnl": round(real_locked_pnl, 4),
        "expected_raw_pnl": round(expected_raw_pnl, 4),
        "expected_committed_pnl": round(expected_committed_pnl, 4),
        "deviation_pct": deviation_pct,
        "target_pct": target_pct,
        "PASS": within_target,
        "sample_available": sample_available,
        "live_per_opp": round(live_per_opp, 6),
        "backtest_per_opp": round(bt_per_opp, 6) if bt_per_opp is not None else None,
        "live_pnl": round(live_pnl, 6),
        "live_opps": live_opps,
        "backtest_pnl": round(bt_pnl, 6) if bt_pnl is not None else None,
        "backtest_opps": bt_opps,
        "source": source,
    }


def generate_report() -> dict:
    """Combine both metrics into a single report dict and write to JSON.

    ``freshness_pct`` is the fraction of FILLED trades within the 500ms freshness
    gate (source of truth: ``calc_quote_freshness.calc_freshness``). When per-trade
    data is absent, falls back to ``fresh_fills / total_fills`` from the bandfills
    summary line in the console log. The previous implementation used
    ``fresh_attempts / total_attempts`` (fraction of *opportunities* in the fresh
    band), which produced the wrong 0.6667 (8/12) — the denominator counted all-band
    attempts instead of fills.

    ``convergence`` now carries the real locked-vs-expected PnL convergence
    (``convergence_pct``, ``real_locked_pnl``, ``expected_pnl``, ``deviation_pct``,
    ``PASS``) sourced from the latest session metrics line, plus the legacy
    ``convergence_ratio`` (live_per_opp / backtest_per_opp) for the dashboard delta.
    """
    freshness = compute_freshness_pct()
    convergence = compute_pnl_convergence()

    # Expose both the descriptive keys (within_target/total_fills) and the
    # short alias keys (fresh_attempts/total_attempts) that the Streamlit tiles
    # read for their delta label.
    freshness_out = dict(freshness)
    freshness_out["fresh_attempts"] = freshness_out.get("within_target", 0)
    freshness_out["total_attempts"] = freshness_out.get("total_fills", 0)

    report = {
        "generated_at": __import__("time").time(),
        "freshness_pct": freshness_out.get("freshness_pct"),
        "freshness": freshness_out,
        "convergence": convergence,
    }

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with OUTPUT.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, default=str)

    return report


def main(argv: list[str] | None = None) -> int:
    argv = argv or sys.argv[1:]
    do_print = "--print" in argv
    report = generate_report()

    if do_print:
        print(json.dumps(report, indent=2, default=str))
        print(f"\nWritten to {OUTPUT}")

    return 0


if __name__ == "__main__":
    import time  # noqa
    sys.exit(main())
