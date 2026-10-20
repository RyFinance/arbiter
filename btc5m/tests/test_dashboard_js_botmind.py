"""Unit tests for the bot-mind narrative engine (synthetic log fixtures)."""
from __future__ import annotations

import json
import time

import pytest

from dashboard_js import botmind


def _row(ts: str, event: str, msg: str, data=None) -> dict:
    row = {"t": ts, "event": event, "msg": msg}
    if data is not None:
        row["data"] = data
    return row


def _log_rows() -> list[dict]:
    return [
        _row("16:59:39.000", "INFO", "WINDOW ACTIVE btc-updown-5m-1786924500 close=1786924800"),
        _row("16:59:39.100", "INFO", "CROSS_LEG_UPDATE_COALESCED market=btc-updown-5m-1786924500 legs=2 combined=1.01"),
        _row("16:59:39.200", "INFO", "macro events: 227 total, 39 high-risk windows"),
        _row("16:59:40.000", "INFO", "WebSocket recovered after 0.7s; resuming WS feed"),
        _row("16:59:41.000", "TRADE", "missed", {"reason": "risk:max position/market exceeded", "market_id": "btc-updown-5m-1786924500", "combined": 0.98}),
        _row("16:59:42.000", "TRADE", "filled", {"market_id": "btc-updown-5m-1786924500", "fill_yes_price": 0.42, "fill_no_price": 0.57, "pnl_locked": 0.1}),
        _row("16:59:43.000", "INFO", "btc-updown-5m-1786924500 YES 0.42 NO 0.57 SUM 0.98 EDGE 0.012 age_ms=120 band=fresh"),
        _row("16:59:44.000", "TRADE", "partial_resolved", {"market_id": "btc-updown-5m-1786924500", "action": "protective_exit", "realized_pnl": -0.2}),
        _row("16:59:45.000", "INFO", "session metrics: loops=100 opps=50 sent=10 fills=2 partial=5 resolved=4 missed=40 session_pnl=-0.1"),
        _row("16:59:46.000", "INFO", "PARTIAL FILL on backtest-market: yes=FILLED no=MISSING — managing exposure (gap=300ms >= 300ms)"),
        _row("16:59:47.000", "INFO", "RISK HALT: partial exit not confirmed — no further trades will be sent"),
    ]


def test_thought_tags_are_derived_from_real_rows():
    thoughts = botmind.thoughts(_log_rows(), limit=20)
    tags = [t["tag"] for t in thoughts]

    assert "WATCH" in tags
    assert "MACRO" in tags
    assert "NET" in tags
    assert "MISS" in tags
    assert "FILL" in tags
    assert "EDGE" in tags
    assert "EXIT" in tags
    assert "PARTIAL" in tags
    assert "HALT" in tags
    fill = next(t for t in thoughts if t["tag"] == "FILL")
    assert "+0.1000" in fill["text"]


def test_focus_extracts_window_and_combined():
    focus = botmind.focus(_log_rows())
    assert focus["window"] == "btc-updown-5m-1786924500"
    assert focus["close_epoch"] == 1786924800
    assert focus["combined"] == 1.01
    assert focus["macro_high"] == 39


def test_funnel_parses_last_session_metrics():
    funnel = botmind.funnel(_log_rows())
    assert funnel["available"] is True
    assert funnel["opps"] == 50
    assert funnel["fires"] == 5
    assert funnel["fills"] == 2
    assert funnel["partial"] == 5
    assert funnel["clean_rate_pct"] == pytest.approx(4.0)
    assert funnel["session_pnl"] == -0.1


def test_verdict_clean_fill_zero_case():
    funnel = {"available": True, "opps": 100, "fires": 10, "fills": 0, "partial": 9, "session_pnl": -1.0}
    verdict = botmind.verdict({"managed_running": False}, funnel, {}, "ok", False)
    assert verdict["level"] == "fail"
    assert "decays within one tick" in verdict["text"]


def test_verdict_running_and_halted():
    ok = botmind.verdict({"managed_running": True}, {"available": False}, {"window": "btc-updown-5m-1", "combined": 0.99}, "ok", False)
    assert ok["level"] == "ok" and "watching btc-updown-5m-1" in ok["text"]

    # global min-edge change: the verdict must advertise the 0.97 threshold
    with_risk = botmind.verdict(
        {"managed_running": True, "risk": {"min_edge": 0.03}},
        {"available": False}, {"window": "btc-updown-5m-1", "combined": 0.99}, "ok", False,
    )
    assert "YES+NO < 0.97" in with_risk["text"]

    halted = botmind.verdict({"managed_running": True}, {"available": False}, {}, "halted", False)
    assert halted["level"] == "halt"

    stopped_idle = botmind.verdict({"managed_running": False}, {"available": False}, {}, "ok", False)
    assert stopped_idle["level"] == "idle"


def test_mind_payload_shape():
    mind = botmind.mind()
    assert set(mind) >= {"source", "managed", "kill_switch", "focus", "funnel", "verdict", "thoughts"}
    assert mind["paper_only"] is True
    assert isinstance(mind["thoughts"], list)
