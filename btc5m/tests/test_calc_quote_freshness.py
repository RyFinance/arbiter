"""Tests for scripts/calc_quote_freshness.py.

Validates the freshness calculations that feed the dashboard Freshness % tile:
  * parse_bot_jsonl — extracts TRADE events with age_ms + freshness_band
  * calc_freshness — pct of fills within target_ms, band distribution, p50/p95
  * parse_console_log — extracts FIRE latencies and summary metrics
  * parse_summary_line — extracts session metrics fields from a summary string
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

# Make scripts/ importable
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from calc_quote_freshness import (
    parse_bot_jsonl,
    calc_freshness,
    parse_console_log,
    parse_summary_line,
    calc_fire_latency,
)


# ---------------------------------------------------------------------------
# parse_bot_jsonl
# ---------------------------------------------------------------------------
def _write_bot_jsonl(path: Path, events: list[dict]) -> None:
    """Write a bot.jsonl file with the given events."""
    with path.open("w") as fh:
        for ev in events:
            fh.write(json.dumps(ev) + "\n")


def test_parse_bot_jsonl_extracts_filled_and_missed(tmp_path):
    """TRADE events are split into fills and missed with their age_ms."""
    events = [
        {"t": "00:00:01.000", "event": "TRADE", "msg": "filled",
         "data": {"market_id": "m1", "age_ms": 100, "freshness_band": "fresh",
                  "pnl_locked": 0.1, "combined": 0.98}},
        {"t": "00:00:02.000", "event": "TRADE", "msg": "missed",
         "data": {"market_id": "m1", "age_ms": 3000, "freshness_band": "0.5-2s",
                  "reason": "stale_quote", "would_lock": 0.1}},
        {"t": "00:00:03.000", "event": "INFO", "msg": "boot"},  # ignored
    ]
    path = tmp_path / "_test_bot.jsonl"
    _write_bot_jsonl(path, events)
    result = parse_bot_jsonl(path)

    assert len(result["fills"]) == 1
    assert result["fills"][0]["age_ms"] == 100
    assert result["fills"][0]["freshness_band"] == "fresh"
    assert len(result["missed"]) == 1
    assert result["missed"][0]["reason"] == "stale_quote"


def test_parse_bot_jsonl_skips_malformed_lines(tmp_path):
    """Malformed JSON lines are skipped without error."""
    path = tmp_path / "_test_bot_bad.jsonl"
    path.write_text(
        '{"t":"00:00:01.000","event":"TRADE","msg":"filled","data":{"market_id":"m1","age_ms":100,"freshness_band":"fresh","pnl_locked":0.1,"combined":0.98}}\n'
        'not valid json\n'
        '{"t":"00:00:02.000","event":"TRADE","msg":"missed","data":{"market_id":"m1","age_ms":3000,"freshness_band":"0.5-2s","reason":"stale_quote","would_lock":0.1}}\n'
    )
    result = parse_bot_jsonl(path)
    assert len(result["fills"]) == 1
    assert len(result["missed"]) == 1


# ---------------------------------------------------------------------------
# calc_freshness
# ---------------------------------------------------------------------------
def test_calc_freshness_empty():
    """With no fills, returns zeroed defaults and FAIL."""
    result = calc_freshness([], target_ms=500)
    assert result["total_fills"] == 0
    assert result["within_target"] == 0
    assert result["pct_within_target"] == 0.0
    assert result["PASS"] is False


def test_calc_freshness_all_within_target():
    """All fills within 500ms -> PASS at 100%."""
    fills = [
        {"age_ms": 100, "freshness_band": "fresh"},
        {"age_ms": 499, "freshness_band": "fresh"},
        {"age_ms": 10, "freshness_band": "fresh"},
    ]
    result = calc_freshness(fills, target_ms=500)
    assert result["total_fills"] == 3
    assert result["within_target"] == 3
    assert result["pct_within_target"] == 100.0
    assert result["PASS"] is True


def test_calc_freshness_partial_within_target():
    """Some fills exceed 500ms -> below 95% target."""
    fills = [
        {"age_ms": 100, "freshness_band": "fresh"},
        {"age_ms": 499, "freshness_band": "fresh"},
        {"age_ms": 600, "freshness_band": "0.5-2s"},
        {"age_ms": 2000, "freshness_band": "0.5-2s"},
        {"age_ms": 5000, "freshness_band": "2-10s"},
    ]
    result = calc_freshness(fills, target_ms=500)
    assert result["total_fills"] == 5
    assert result["within_target"] == 2
    assert result["pct_within_target"] == 40.0
    assert result["PASS"] is False
    # p50 for 5 sorted values [100, 499, 600, 2000, 5000]:
    # _pct uses (n-1)*p: int(4*0.50)=2 -> ages[2] = 600
    # p95: int(4*0.95)=3 -> ages[3] = 2000
    assert result["p50_age_ms"] == 600.0
    assert result["p95_age_ms"] == 2000.0


def test_calc_freshness_band_distribution():
    """The bands dict counts fills per band."""
    fills = [
        {"age_ms": 100, "freshness_band": "fresh"},
        {"age_ms": 600, "freshness_band": "0.5-2s"},
        {"age_ms": 600, "freshness_band": "0.5-2s"},
        {"age_ms": 5000, "freshness_band": "2-10s"},
    ]
    result = calc_freshness(fills)
    assert result["bands"]["fresh"] == 1
    assert result["bands"]["0.5-2s"] == 2
    assert result["bands"]["2-10s"] == 1


# ---------------------------------------------------------------------------
# parse_console_log
# ---------------------------------------------------------------------------
def test_parse_console_log_extracts_fire_latencies(tmp_path):
    """FIRE latency lines are parsed into a list of dicts."""
    log_file = tmp_path / "session.log"
    log_file.write_text(
        "[12:00:01.123] FIRE market=m1 latency_ms=12.34 band=fresh\n"
        "[12:00:02.456] FIRE market=m2 latency_ms=567.89 band=fresh\n"
    )
    result = parse_console_log(log_file)
    assert len(result["fire_latencies"]) == 2
    assert result["fire_latencies"][0]["latency_ms"] == 12.34
    assert result["fire_latencies"][1]["latency_ms"] == 567.89


def test_parse_console_log_extracts_summary(tmp_path):
    """Session metrics summary line is captured."""
    log_file = tmp_path / "session.log"
    summary_line = (
        "session metrics: loops=10 opps=5 sent=2 fills=1 partial=0 resolved=0 "
        "missed=0 opp2order_avg_ms=12.34 p50_ms=11.00 ack_avg_ms=10.00 "
        "locked_pnl=5.0000 residual_pnl=0.0000 session_pnl=5.0000 would_lock=0.00 "
        "bandfills[fresh=1/5 0.5-2s=0/0 2-10s=0/0 stale=0/0]"
    )
    log_file.write_text(f"[12:00:01.123] {summary_line}\n")
    result = parse_console_log(log_file)
    assert result["summary"] is not None
    assert summary_line.strip() in result["summary"]


# ---------------------------------------------------------------------------
# parse_summary_line
# ---------------------------------------------------------------------------
def test_parse_summary_line_extracts_fields():
    """All metric fields are parsed from a session metrics string."""
    line = (
        "session metrics: loops=100 opps=50 sent=20 fills=10 partial=2 resolved=2 "
        "missed=30 opp2order_avg_ms=123.45 p50_ms=50.00 ack_avg_ms=25.00 "
        "locked_pnl=42.5000 residual_pnl=-3.2000 session_pnl=39.3000 would_lock=15.0000 "
        "bandfills[fresh=8/8 0.5-2s=0/49 2-10s=0/113 stale=0/33]"
    )
    sm = parse_summary_line(line)
    assert sm["loops"] == 100
    assert sm["opps"] == 50
    assert sm["fills"] == 10
    assert sm["missed"] == 30
    assert sm["locked_pnl"] == 42.5
    assert sm["session_pnl"] == 39.3
    assert sm["would_lock"] == 15.0
    assert "bandfills" in sm
    assert sm["bandfills"]["fresh"]["filled"] == 8
    assert sm["bandfills"]["fresh"]["attempts"] == 8
    assert sm["bandfills"]["0.5-2s"]["attempts"] == 49


# ---------------------------------------------------------------------------
# calc_fire_latency
# ---------------------------------------------------------------------------
def test_calc_fire_latency_basic():
    """Latency stats computed from a list of FIRE latency values."""
    latencies = [{"latency_ms": 10.0, "market_id": "m1", "band": "fresh"},
                 {"latency_ms": 20.0, "market_id": "m2", "band": "fresh"},
                 {"latency_ms": 300.0, "market_id": "m3", "band": "fresh"},
                 {"latency_ms": 600.0, "market_id": "m4", "band": "fresh"}]
    result = calc_fire_latency(latencies)
    assert result["total_fires"] == 4
    assert result["avg_ms"] == 232.5
    # For 4 sorted values [10, 20, 300, 600]:
    # p50 = vals[int(3*0.5)] = vals[1] = 20.0
    # p95 = vals[int(3*0.95)] = vals[2] = 300.0
    assert result["p50_ms"] == 20.0
    assert result["p95_ms"] == 300.0
    assert result["p99_ms"] == 300.0
    assert result["max_ms"] == 600.0
    assert result["within_500ms"] == 3
    assert result["pct_within_500ms"] == 75.0


def test_calc_fire_latency_empty():
    """Empty latencies return zeroed defaults."""
    result = calc_fire_latency([])
    assert result["total_fires"] == 0
    assert result["avg_ms"] == 0.0
