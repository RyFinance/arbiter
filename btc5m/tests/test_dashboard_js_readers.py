"""Unit tests for dashboard_js.readers — pure file readers over synthetic fixtures."""
from __future__ import annotations

import json
import time
from pathlib import Path

from dashboard_js import readers


def _write(path: Path, lines) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as fh:
        for line in lines:
            if isinstance(line, str):
                fh.write(line + "\n")
            else:
                fh.write(json.dumps(line) + "\n")


def test_iter_jsonl_skips_truncated_tail_and_blank_lines(tmp_path: Path):
    p = tmp_path / "x.jsonl"
    p.write_text('{"a": 1}\n\n{"a": 2}\n{"a": 3}\n{"a": 4 TRUNCATED\n')
    rows = list(readers.iter_jsonl(p))
    assert rows == [{"a": 1}, {"a": 2}, {"a": 3}]


def test_iter_jsonl_missing_file_yields_nothing(tmp_path: Path):
    assert list(readers.iter_jsonl(tmp_path / "nope.jsonl")) == []


def test_iter_jsonl_limit(tmp_path: Path):
    p = tmp_path / "x.jsonl"
    _write(p, [{"i": i} for i in range(10)])
    assert [r["i"] for r in readers.iter_jsonl(p, max_lines=3)] == [0, 1, 2]


def test_parse_session_metrics_realistic_line():
    msg = (
        "session metrics: loops=1846 opps=0 sent=0 fills=0 partial=0 resolved=0 missed=0 "
        "template_unavailable=0 modes_by_order/fill[dry_run=0s/0f paper=0s/0f live=0s/0f] "
        "opp2order_avg_ms=0.00 p50_ms=0.00 p95_ms=0.00 p99_ms=0.00 ack_avg_ms=0.00 "
        "stage_reserve_p50_ms=0.00 stage_submit_p50_ms=0.00 stage_postsubmit_p50_ms=0.00 "
        "locked_pnl=0.0000 residual_pnl=0.0000 session_pnl=0.0000 would_lock=0.0000 "
        "macro_skipped=0 macro_skipped_pnl=0.0000 bandfills[fresh=0/0 0.5-2s=0/0 2-10s=0/0 stale=0/0]"
    )
    parsed = readers.parse_session_metrics(msg)
    assert parsed is not None
    assert parsed["loops"] == 1846
    assert parsed["missed"] == 0
    assert parsed["opp2order_avg_ms"] == 0.0
    assert parsed["locked_pnl"] == 0.0
    assert parsed["would_lock"] == 0.0
    assert "modes_by_order" not in parsed
    assert "bandfills" not in parsed
    assert "fresh" not in parsed


def test_parse_session_metrics_negative_values():
    msg = "session metrics: loops=5 opps=2 sent=1 fills=0 partial=0 resolved=0 missed=1 locked_pnl=-0.5 residual_pnl=-0.1 session_pnl=-0.6 would_lock=0.2"
    parsed = readers.parse_session_metrics(msg)
    assert parsed is not None
    assert parsed["locked_pnl"] == -0.5
    assert parsed["session_pnl"] == -0.6


def test_parse_session_metrics_rejects_non_metrics():
    assert readers.parse_session_metrics("INFO: hello") is None
    assert readers.parse_session_metrics("") is None
    assert readers.parse_session_metrics(None) is None


def test_parse_bot_ts_hhmmss_with_anchor():
    anchor = 1786846800.0  # 2026-08-16T02:20:00Z
    ts = readers.parse_bot_ts("01:00:00.500", anchor)
    assert ts is not None
    assert abs(ts - 1786842000.5) < 0.01


def test_parse_bot_ts_iso():
    ts = readers.parse_bot_ts("2026-08-16T02:20:00.123+00:00")
    assert ts is not None
    assert abs(ts - 1786846800.123) < 0.001


def test_parse_bot_ts_midnight_rollover():
    # file mtime is just after midnight but last log line was before midnight
    anchor = 1786849800.0  # 2026-08-16T02:30:00Z
    ts = readers.parse_bot_ts("23:59:59.000", anchor)
    assert ts is not None
    assert ts < anchor - 3600  # rolled back a day


def test_parse_bot_ts_garbage():
    assert readers.parse_bot_ts("not-a-time") is None
    assert readers.parse_bot_ts("") is None
    assert readers.parse_bot_ts(123) is None


def test_read_heartbeat(tmp_path: Path):
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    (tmp_path / "state" / "HEARTBEAT").write_text("1786853090.541\n")
    assert readers.read_heartbeat(tmp_path) == 1786853090.541
    (tmp_path / "state" / "HEARTBEAT").write_text("garbage")
    assert readers.read_heartbeat(tmp_path) is None


def test_extract_trade_rows(tmp_path: Path):
    bot = tmp_path / "state" / "bot.jsonl"
    _write(
        bot,
        [
            {"t": "01:00:00.000", "event": "INFO", "msg": "polymarket arb bot starting | paper_trade=True"},
            {
                "t": "01:00:01.000",
                "event": "TRADE",
                "msg": "missed",
                "data": {"reason": "stale_quote", "market_id": "btc-updown-5m-1786851600", "combined": 0.97, "age_ms": 650.5, "size": 10.0},
            },
            {
                "t": "01:00:02.000",
                "event": "TRADE",
                "msg": "filled",
                "data": {"market_id": "btc-updown-5m-1786851600", "combined": 0.98, "pnl_locked": 0.2, "size": 10.0},
            },
            {"t": "01:00:03.000", "event": "TRADE", "msg": "window", "data": {"slug": "x"}},
            {"t": "01:00:04.000", "event": "INFO", "msg": "noise"},
        ],
    )
    trades = readers.extract_trade_rows(bot)
    assert [t["msg"] for t in trades] == ["missed", "filled"]
    assert trades[0]["data"]["reason"] == "stale_quote"
    assert trades[0]["ts_epoch"] is not None
    assert trades[0]["ts_utc"] is not None


def test_extract_session_metrics_rows(tmp_path: Path):
    bot = tmp_path / "state" / "bot.jsonl"
    _write(
        bot,
        [
            {"t": "01:00:00.000", "event": "INFO", "msg": "start"},
            {
                "t": "01:01:00.000",
                "event": "INFO",
                "msg": "session metrics: loops=10 opps=3 sent=2 fills=1 partial=1 resolved=0 missed=1 opp2order_avg_ms=1.2 p50_ms=1.1 p95_ms=2.2 p99_ms=3.3 ack_avg_ms=1.0 locked_pnl=0.2 residual_pnl=-0.1 session_pnl=0.1 would_lock=0.3",
            },
        ],
    )
    rows = readers.extract_session_metrics_rows(bot)
    assert len(rows) == 1
    row = rows[0]
    assert row["loops"] == 10
    assert row["locked_pnl"] == 0.2
    assert row["p95_ms"] == 2.2
    assert row["would_lock"] == 0.3
    assert row["ts_utc"] is not None


def test_find_session_start(tmp_path: Path):
    bot = tmp_path / "state" / "bot.jsonl"
    _write(
        bot,
        [
            {"t": "01:00:00.000", "event": "INFO", "msg": "polymarket arb bot starting | dry_run=False | live=True | paper_trade=True"},
        ],
    )
    start = readers.find_session_start(bot)
    assert start is not None
    assert start["ts_epoch"] is not None


def test_detect_paper_mode(tmp_path: Path):
    bot = tmp_path / "state" / "bot.jsonl"
    _write(
        bot,
        [
            {"t": "01:00:00.000", "event": "INFO", "msg": "polymarket arb bot starting | dry_run=False | live=True | paper_trade=True"},
        ],
    )
    flags = readers.detect_paper_mode(bot)
    assert flags == {"paper": True, "dry_run": False}

    _write(bot, [{"t": "01:00:00.000", "event": "INFO", "msg": "polymarket arb bot starting | dry_run=True | paper_trade=False"}])
    flags = readers.detect_paper_mode(bot)
    assert flags == {"paper": False, "dry_run": True}


def _depth_row(window_start: int, token: str, offset: float) -> dict:
    return {
        "type": "depth_snapshot",
        "captured_at": window_start + offset,
        "market_id": f"btc-updown-5m-{window_start}",
        "token_id": token,
        "asks": [[0.5, 10.0]],
        "bids": [[0.49, 10.0]],
    }


def _build_depth_fixture(data_root: Path) -> None:
    shard = data_root / "state" / "cache" / "polymarket_depth_capture.jsonl"
    shard.parent.mkdir(parents=True, exist_ok=True)
    w = 1786851600
    rows = []
    # window w: both tokens, complete (first<=5, last>=295, gaps<=60)
    for tok, offs in {
        "tokA": [1.0, 50.0, 100.0, 150.0, 200.0, 250.0, 299.0],
        "tokB": [2.0, 51.0, 101.0, 151.0, 201.0, 251.0, 296.0],
    }.items():
        for off in offs:
            rows.append(_depth_row(w, tok, off))
    # window w+300: single token only -> not complete
    for off in [1.0, 100.0, 200.0, 299.0]:
        rows.append(_depth_row(w + 300, "tokA", off))
    # non-window market_id rows count as snapshots but not windows
    rows.append({"type": "depth_snapshot", "captured_at": w + 999.0, "market_id": "other-market", "token_id": "tokA"})
    with open(shard, "w") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def test_scan_depth_shards_continuity(tmp_path: Path):
    data_root = tmp_path / "root"
    _build_depth_fixture(data_root)
    scan = readers.scan_depth_shards(data_root)
    assert scan["snapshot_count"] == 14 + 4 + 1
    assert len(scan["shards"]) == 1
    shard = scan["shards"][0]
    assert shard["name"] == "polymarket_depth_capture.jsonl"
    assert shard["rows"] == 14 + 4 + 1
    assert shard["first_ts"] is not None and shard["last_ts"] is not None
    cont = scan["continuity"]
    assert cont["windows"] == 2
    assert cont["complete"] == 1
    assert cont["broken"] == 1
    assert abs(cont["ratio"] - 0.5) < 1e-9
    assert cont["criterion"] == "both tokens: first<=5s, last>=295s, max gap<=60s"
    assert scan["last_30_windows"] == {"windows": 2, "complete": 1}


def test_scan_depth_shards_gap_breaks_window(tmp_path: Path):
    data_root = tmp_path / "root"
    shard = data_root / "state" / "cache" / "polymarket_depth_capture.jsonl"
    shard.parent.mkdir(parents=True, exist_ok=True)
    w = 1786851600
    # both tokens present but token B has a 120s gap -> not complete
    rows = (
        [_depth_row(w, "tokA", o) for o in [1.0, 50.0, 100.0, 150.0, 200.0, 250.0, 299.0]]
        + [_depth_row(w, "tokB", o) for o in [2.0, 150.0, 296.0]]
    )
    with open(shard, "w") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    scan = readers.scan_depth_shards(data_root)
    assert scan["continuity"] == {
        "windows": 1,
        "complete": 0,
        "broken": 1,
        "ratio": 0.0,
        "criterion": "both tokens: first<=5s, last>=295s, max gap<=60s",
    }


def test_scan_depth_shards_cache_and_ttl(tmp_path: Path, monkeypatch):
    data_root = tmp_path / "root"
    _build_depth_fixture(data_root)
    first = readers.scan_depth_shards(data_root)
    assert readers._DEPTH_SCAN_CACHE["result"] is not None
    monkeypatch.setattr(readers, "DEPTH_CACHE_TTL_S", 0.0)
    readers.invalidate_depth_cache()
    second = readers.scan_depth_shards(data_root)
    assert second == first
    monkeypatch.setattr(readers, "DEPTH_CACHE_TTL_S", 1e9)
    readers.invalidate_depth_cache()
    readers.scan_depth_shards(data_root)
    shard = data_root / "state" / "cache" / "polymarket_depth_capture.jsonl"
    with open(shard, "a") as fh:
        fh.write(json.dumps(_depth_row(1786851900, "tokA", 1.0)) + "\n")
    time.sleep(0.01)
    third = readers.scan_depth_shards(data_root)  # size/mtime changed -> rescan despite TTL
    assert third["snapshot_count"] == first["snapshot_count"] + 1


def test_depth_scan_truncated_tail_tolerated(tmp_path: Path):
    data_root = tmp_path / "root"
    shard = data_root / "state" / "cache" / "polymarket_depth_capture.jsonl"
    shard.parent.mkdir(parents=True, exist_ok=True)
    rows = [_depth_row(1786851600, "tokA", o) for o in [1.0, 100.0, 299.0]]
    with open(shard, "w") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
        fh.write('{"type": "depth_snapshot", "captured_at": 1786851 TRUNC\n')
    scan = readers.scan_depth_shards(data_root)
    assert scan["snapshot_count"] == 3


def test_depth_scan_empty_cache_dir(tmp_path: Path):
    data_root = tmp_path / "root"
    (data_root / "state" / "cache").mkdir(parents=True)
    scan = readers.scan_depth_shards(data_root)
    assert scan["shards"] == []
    assert scan["snapshot_count"] == 0
    assert scan["continuity"]["windows"] == 0


def test_depth_lock_and_pid_alive(tmp_path: Path):
    data_root = tmp_path / "root"
    lock = data_root / "state" / "cache" / "capture_depth_ws.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(json.dumps({"pid": 999999999, "script": "x"}))
    assert readers.read_depth_lock(data_root) is not None
    assert readers.pid_alive(999999999) is False
    assert readers.pid_alive(None) is False
    lock.write_text(json.dumps({"pid": __import__("os").getpid(), "script": "x"}))
    found = readers.find_capture_pid(data_root)
    assert found == __import__("os").getpid()


def test_list_session_archives_sorted(tmp_path: Path):
    base = tmp_path / "state" / "paper_session_archives"
    for name in ("z_20260815T090124Z", "a_20260804T033625Z", "m_20260816T015647Z"):
        (base / name).mkdir(parents=True)
    archives = readers.list_session_archives(tmp_path)
    assert [a.name for a in archives] == ["a_20260804T033625Z", "m_20260816T015647Z", "z_20260815T090124Z"]
    assert readers.list_session_archives(tmp_path / "nope") == []


def test_read_latency_profile_missing(tmp_path: Path):
    assert readers.read_latency_profile(tmp_path) == {}
