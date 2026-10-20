"""Unit tests for the isolated paper loop bot (runner math + controller).

Runner tests import ``scripts/run_loop_bot`` — network paths are never
touched here, only the pure helpers. Controller tests mirror the botctl
test style: all state paths monkeypatched to tmp dirs.
"""
from __future__ import annotations

import json
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import scripts.run_loop_bot as runner  # noqa: E402

from dashboard_js import envs, loopbot  # noqa: E402


@pytest.fixture(autouse=True)
def isolate_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(loopbot, "STATE_DIR", tmp_path)
    monkeypatch.setattr(loopbot, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(loopbot, "STOP_PATH", tmp_path / "STOP")
    monkeypatch.setattr(loopbot, "EVENTS_PATH", tmp_path / "events.jsonl")
    monkeypatch.setattr(loopbot, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(loopbot, "LOG_PATH", tmp_path / "bot.log")
    monkeypatch.setattr(loopbot, "RUNNER_SCRIPT", "scripts/run_loop_bot.py")
    monkeypatch.setattr(loopbot, "_STATE", {"proc": None, "started_at": None})
    monkeypatch.setattr(runner, "STATE_DIR", tmp_path)
    monkeypatch.setattr(runner, "STOP_FILE", tmp_path / "STOP")
    monkeypatch.setattr(runner, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(runner, "EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(runner, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(runner, "LOG_FILE", tmp_path / "bot.log")
    monkeypatch.setattr(runner, "REGIME_STATE_FILE", tmp_path / "regime_active.json")
    monkeypatch.setattr(loopbot, "_PID_CACHE", {"lock": __import__("threading").Lock(), "t": 0.0, "pids": []})
    yield


# ---------------------------------------------------------------- runner math

def test_window_ts_and_slug_contract():
    assert runner.window_ts(1786925100.0, 300) == 1786925100
    assert runner.window_ts(1786925150.0, 300) == 1786925100
    assert runner.window_ts(1786925401.0, 900) == 1786924800
    assert runner.window_slug("btc", "15m", 1786924800) == "btc-updown-15m-1786924800"


def test_should_enter_gate():
    # edge present, no entry yet -> enter
    assert runner.should_enter(0.98, 0.01, False, 1000.0, 1010.0, 0.0) is True
    # no edge -> no
    assert runner.should_enter(0.995, 0.01, False, 1000.0, 1010.0, 0.0) is False
    # exactly at edge -> no (strict)
    assert runner.should_enter(0.99, 0.01, False, 1000.0, 1010.0, 0.0) is False
    # already entered this window -> no
    assert runner.should_enter(0.98, 0.01, True, 1000.0, 1010.0, 0.0) is False
    # one leg missing -> no
    assert runner.should_enter(None, 0.01, False, 1000.0, 1010.0, 0.0) is False
    # inside no-entry zone -> no
    assert runner.should_enter(0.98, 0.01, False, 1000.0, 1002.0, 5.0) is False
    # after no-entry zone -> yes
    assert runner.should_enter(0.98, 0.01, False, 1000.0, 1006.0, 5.0) is True


def test_locked_pnl():
    assert runner.locked_pnl(0.98, 10.0) == pytest.approx(0.2)
    assert runner.locked_pnl(1.01, 10.0) == pytest.approx(-0.1)


def test_crypto_fee_and_depth_vwap_are_full_size_and_five_decimals():
    fill = runner.depth_fill(
        [{"price": "0.47", "size": "3"}, {"price": "0.48", "size": "4"}],
        5.0, "BUY", observed_at=123.0,
    )
    assert fill is not None
    assert fill.vwap == pytest.approx((0.47 * 3 + 0.48 * 2) / 5)
    assert fill.worst_price == pytest.approx(0.48)
    assert fill.levels_used == 2
    expected_fee = round(3 * 0.07 * 0.47 * 0.53 + 2 * 0.07 * 0.48 * 0.52, 5)
    assert fill.fee == pytest.approx(expected_fee)
    assert runner.depth_fill([{"price": 0.48, "size": 4.99}], 5.0, "BUY") is None
    assert runner.crypto_taker_fee(10, 0.48) == pytest.approx(0.17472)


def _paper_book(ask: float, bid: float, shares: float = 5.0):
    buy = runner.depth_fill([{"price": ask, "size": shares + 1}], shares, "BUY", observed_at=1.0)
    sell = runner.depth_fill([{"price": bid, "size": shares + 1}], shares, "SELL", observed_at=1.0)
    return {
        "observed_at": 1.0, "best_ask": ask, "best_bid": bid,
        "buy": asdict(buy), "sell": asdict(sell),
        "ask_depth": shares + 1, "bid_depth": shares + 1,
    }


@pytest.mark.asyncio
async def test_sequential_first_then_later_second_is_fee_aware(monkeypatch, tmp_path):
    bot = runner.LoopBot({
        "order_size": 5.0, "min_edge": 0.003, "no_entry_seconds": 0.0,
        "entry_window_seconds": 60.0, "first_price_min": 0.45, "first_price_max": 0.49,
        "local_vol_threshold": 0.005, "local_vol_lookback_seconds": 30.0,
        "hedge_timeout_seconds": 20.0, "timeframes": ["5m"],
    }, finnhub_key=None)
    bot._resolve_tokens = AsyncMock(return_value={"yes": "y", "no": "n"})
    responses = iter([
        {"yes": _paper_book(0.49, 0.47), "no": _paper_book(0.52, 0.50)},
        {"yes": _paper_book(0.48, 0.46), "no": _paper_book(0.52, 0.50)},
        {"yes": _paper_book(0.50, 0.46), "no": _paper_book(0.48, 0.46)},
    ])

    async def fetch_pair(client, tokens):
        return next(responses)

    monkeypatch.setattr(bot, "_fetch_pair", fetch_pair)
    w = runner.window_ts(time.time(), 300)
    await bot._handle_window(None, "5m", 300, w + 1)
    assert bot.tf_state["5m"]["phase"] == "seeking_first"  # volatility needs history
    await bot._handle_window(None, "5m", 300, w + 3)
    assert bot.tf_state["5m"]["phase"] == "waiting_second"
    await bot._handle_window(None, "5m", 300, w + 5)
    st = bot.tf_state["5m"]
    assert st["phase"] == "paired"
    assert st["position"]["first_at"] < st["position"]["second_at"]
    assert st["position"]["pnl"] > 0
    assert st["position"]["net_edge"] >= 0.003
    kinds = [json.loads(line)["kind"] for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert "first_filled" in kinds and "pair_completed" in kinds


@pytest.mark.asyncio
async def test_sequential_timeout_uses_full_bid_depth_and_realizes_loss(monkeypatch):
    bot = runner.LoopBot({
        "order_size": 5.0, "min_edge": 0.003, "no_entry_seconds": 0.0,
        "entry_window_seconds": 60.0, "first_price_min": 0.45, "first_price_max": 0.49,
        "local_vol_threshold": 0.005, "local_vol_lookback_seconds": 30.0,
        "hedge_timeout_seconds": 2.0, "timeframes": ["5m"],
    }, finnhub_key=None)
    bot._resolve_tokens = AsyncMock(return_value={"yes": "y", "no": "n"})
    responses = iter([
        {"yes": _paper_book(0.49, 0.47), "no": _paper_book(0.52, 0.50)},
        {"yes": _paper_book(0.48, 0.46), "no": _paper_book(0.52, 0.50)},
        {"yes": _paper_book(0.51, 0.46), "no": _paper_book(0.55, 0.53)},
    ])

    async def fetch_pair(client, tokens):
        return next(responses)

    monkeypatch.setattr(bot, "_fetch_pair", fetch_pair)
    w = runner.window_ts(time.time(), 300)
    await bot._handle_window(None, "5m", 300, w + 1)
    await bot._handle_window(None, "5m", 300, w + 3)
    await bot._handle_window(None, "5m", 300, w + 6)
    st = bot.tf_state["5m"]
    assert st["phase"] == "aborted"
    assert st["position"]["exit"]["vwap"] == pytest.approx(0.46)
    assert st["position"]["pnl"] < 0
    assert bot.session_totals["forced_exits"] == 1


def test_lightgbm_v3_is_soft_adjustment_and_stale_is_neutral(tmp_path):
    bot = runner.LoopBot({"local_vol_threshold": 0.01, "timeframes": ["5m"]}, finnhub_key=None)
    now = time.time()
    (tmp_path / "regime_active.json").write_text(json.dumps({
        "regime": "HIGH", "confidence": 0.8, "source": "lightgbm",
        "applied_at_utc": datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }))
    bot._refresh_regime(now, force=True)
    assert bot.regime["status"] == "active"
    assert bot.regime["adjusted_local_vol_threshold"] == pytest.approx(0.0075)
    stale_now = now + bot.regime_max_age_seconds + 1
    bot._refresh_regime(stale_now, force=True)
    assert bot.regime["status"] == "stale"
    assert bot.regime["adjusted_local_vol_threshold"] == pytest.approx(0.01)


def test_parse_updown_tokens_prefers_token_labels():
    market = {
        "tokens": [
            {"outcome": "Up", "token_id": "111"},
            {"outcome": "Down", "token_id": "222"},
        ]
    }
    assert runner.parse_updown_tokens(market) == {"yes": "111", "no": "222"}


def test_parse_updown_tokens_fallback_index():
    market = {
        "outcomes": '["Up", "Down"]',
        "clobTokenIds": '["333", "444"]',
    }
    assert runner.parse_updown_tokens(market) == {"yes": "333", "no": "444"}


def test_parse_updown_tokens_rejects_non_updown():
    assert runner.parse_updown_tokens({"outcomes": '["Yes", "No"]', "clobTokenIds": '["1", "2"]'}) is None
    assert runner.parse_updown_tokens({}) is None


def test_loopbot_event_and_state_writes(tmp_path):
    bot = runner.LoopBot({"order_size": 5.0, "min_edge": 0.003, "timeframes": ["5m"]}, finnhub_key=None)
    now = time.time()
    w = runner.window_ts(now, 300)
    bot.tf_state["5m"]["window"] = 1786925100
    bot.tf_state["5m"]["slug"] = "btc-updown-5m-1786925100"
    bot.tf_state["5m"]["window"] = w
    bot.tf_state["5m"]["slug"] = runner.window_slug("btc", "5m", w)
    first = runner.depth_fill([{"price": 0.48, "size": 6}], 5.0, "BUY", observed_at=now)
    bot._first_leg("5m", bot.tf_state["5m"], "yes", first, now)
    bot._write_state(True)
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["running"] is True
    assert state["strategy_schema"] == "sequential_v2"
    assert state["legacy_metrics_included"] is False
    assert state["totals"]["entries"] == 1
    assert state["totals"]["open_exposure"] > 0
    assert state["timeframes"]["5m"]["phase"] == "waiting_second"
    assert state["paper_only"] is True
    assert state["real_orders_possible"] is False
    books = {"yes": _paper_book(0.50, 0.46), "no": _paper_book(0.55, 0.53)}
    assert bot._protective_exit("5m", bot.tf_state["5m"], books, now + 5, "test") is True
    assert bot.totals["realized"] < 0
    assert bot.totals["forced_exits"] == 1
    events = (tmp_path / "events.jsonl").read_text().strip().splitlines()
    exit_rows = [json.loads(line) for line in events if json.loads(line)["kind"] == "protective_exit"]
    assert exit_rows[0]["pnl"] < 0
    assert exit_rows[0]["tf"] == "5m"


def test_legacy_state_is_not_migrated_into_sequential_totals(tmp_path):
    (tmp_path / "state.json").write_text(json.dumps({
        "running": False, "totals": {"realized_pnl": 106.2, "entries": 257, "exits": 257},
    }))
    bot = runner.LoopBot({"timeframes": ["5m"]}, finnhub_key=None)
    bot._write_state(False)
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["strategy_totals"]["realized_pnl"] == 0
    assert state["strategy_totals"]["first_fills"] == 0
    assert state["legacy_metrics_included"] is False


def test_v2_terminal_phase_restores_and_prevents_second_sequence(tmp_path):
    bot = runner.LoopBot({"timeframes": ["5m"]}, finnhub_key=None)
    now = time.time()
    w = runner.window_ts(now, 300)
    st = bot.tf_state["5m"]
    st.update({
        "window": w, "window_open_at": w, "slug": runner.window_slug("btc", "5m", w),
        "phase": "aborted", "sequence_done": True,
    })
    bot._write_state(False)
    restarted = runner.LoopBot({"timeframes": ["5m"]}, finnhub_key=None)
    assert restarted.tf_state["5m"]["phase"] == "aborted"
    assert restarted.tf_state["5m"]["sequence_done"] is True


def test_stale_window_first_leg_is_preserved_as_unresolved_exposure(tmp_path):
    now = time.time()
    current = runner.window_ts(now, 300)
    old_window = current - 300
    first = runner.depth_fill([{"price": 0.48, "size": 6}], 5.0, "BUY", observed_at=now - 250)
    (tmp_path / "state.json").write_text(json.dumps({
        "strategy_schema": "sequential_v2",
        "strategy_totals": {"realized_pnl": 0.0, "fees": first.fee, "first_fills": 1},
        "unresolved_positions": [],
        "timeframes": {
            "5m": {
                "window": old_window, "slug": runner.window_slug("btc", "5m", old_window),
                "phase": "waiting_second", "position": {
                    "position_id": f"5m-{old_window}-yes", "side": "yes", "opposite": "no",
                    "first": asdict(first), "first_at": now - 250, "deadline_at": now - 200,
                },
            },
        },
    }))
    restarted = runner.LoopBot({"timeframes": ["5m"]}, finnhub_key=None)
    assert restarted._restore_manual_review_count == 1
    assert len(restarted.unresolved_positions) == 1
    assert restarted.unresolved_positions[0]["reason"] == "restart_after_window_rollover"
    restarted._write_state(False)
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["strategy_totals"]["unhedged_positions"] == 1
    assert state["strategy_totals"]["open_exposure"] > 0


def test_regime_defaults_match_fifteen_minute_forecast_horizon():
    assert loopbot.DEFAULT_CONFIG["regime_max_age_seconds"] == 900.0
    assert loopbot.DEFAULT_CONFIG["regime_forecast_interval_seconds"] <= 900.0


# ---------------------------------------------------------------- controller

def test_default_config_when_missing():
    assert loopbot.load_config() == loopbot.DEFAULT_CONFIG


def test_save_and_load_config_roundtrip():
    saved, err = loopbot.save_config({"order_size": 12.0, "min_edge": 0.02, "timeframes": ["5m", "15m"]})
    assert err is None
    assert saved["order_size"] == 12.0
    loaded = loopbot.load_config()
    assert loaded["order_size"] == 12.0
    assert loaded["timeframes"] == ["5m", "15m"]


def test_save_config_rejects_bad_values():
    _, err = loopbot.save_config({"order_size": 5000.0})
    assert err and "outside allowed range" in err
    _, err = loopbot.save_config({"timeframes": ["1h"]})
    assert err and "unknown timeframes" in err
    _, err = loopbot.save_config({"min_edge": "not-a-number"})
    assert err and "numeric" in err
    _, err = loopbot.save_config({"first_price_min": 0.50, "first_price_max": 0.40})
    assert err and "first_price_min" in err


def test_legacy_gross_edge_config_migrates_to_v2_net_default(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({
        "mode": "manual", "order_size": 10.0, "min_edge": 0.02,
        "no_entry_seconds": 1.0, "timeframes": ["5m"],
    }))
    loaded = loopbot.load_config()
    assert loaded["strategy_schema"] == "sequential_v2"
    assert loaded["order_size"] == 10.0
    assert loaded["min_edge"] == loopbot.DEFAULT_CONFIG["min_edge"]


def test_start_persists_migrated_v2_config_and_runner_rejects_legacy_reload(tmp_path, monkeypatch):
    finnhub_secret = "unit-test-finnhub-secret"
    legacy = {
        "mode": "manual", "order_size": 10.0, "min_edge": 0.02,
        "no_entry_seconds": 1.0, "timeframes": ["5m"],
    }
    (tmp_path / "config.json").write_text(json.dumps(legacy))
    monkeypatch.setattr(loopbot, "_loop_bot_pids", lambda fresh=False: [])
    monkeypatch.setattr(envs, "finnhub_api_key", lambda: finnhub_secret)
    captured = {}

    class FakeProc:
        pid = 42420

        @staticmethod
        def poll():
            return None

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs.get("env")
        return FakeProc()

    monkeypatch.setattr(loopbot.subprocess, "Popen", fake_popen)
    result = loopbot.start_bot()
    assert result["started"] is True
    assert result["config"]["min_edge"] == 0.003
    command = captured["command"]
    assert float(command[command.index("--min-edge") + 1]) == 0.003
    assert "--finnhub-key" not in command
    assert finnhub_secret not in " ".join(command)
    assert captured["env"][loopbot.FINNHUB_KEY_ENV] == finnhub_secret
    assert finnhub_secret not in json.dumps(result)

    migrated = json.loads((tmp_path / "config.json").read_text())
    assert migrated["strategy_schema"] == "sequential_v2"
    assert migrated["min_edge"] == 0.003
    assert "entry_window_seconds" in migrated
    assert finnhub_secret not in (tmp_path / "config.json").read_text()
    assert finnhub_secret not in (tmp_path / "bot.log").read_text()

    bot = runner.LoopBot(result["config"], finnhub_key=None)
    assert bot.min_edge == 0.003
    # Even if a stale writer puts the old five-field file back, the runner
    # must retain its migrated CLI value and reject that hot reload.
    (tmp_path / "config.json").write_text(json.dumps(legacy))
    bot._config_mtime = 0.0
    assert bot._reload_config() is False
    assert bot.min_edge == 0.003
    rows = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert finnhub_secret not in json.dumps(rows)
    assert any(
        row.get("kind") == "meta" and row.get("meta_kind") == "config_reload_rejected"
        for row in rows
    )


def test_stop_with_no_bot_is_honest(tmp_path, monkeypatch):
    monkeypatch.setattr(loopbot, "_loop_bot_pids", lambda fresh=False: [])
    result = loopbot.stop_bot()
    assert result["stopped"] is False
    assert (tmp_path / "STOP").is_file()


def test_status_reports_paper_only_invariants(monkeypatch):
    monkeypatch.setattr(loopbot, "_loop_bot_pids", lambda fresh=False: [])
    status = loopbot.status()
    assert status["paper_only"] is True
    assert status["real_orders_possible"] is False
    assert status["managed_running"] is False
    assert "config" in status


def test_status_archives_legacy_pnl_instead_of_showing_it_as_current(tmp_path):
    (tmp_path / "state.json").write_text(json.dumps({
        "totals": {"realized_pnl": 106.2, "entries": 257, "exits": 257},
        "started_at_utc": "2026-08-20T00:00:00Z",
    }))
    status = loopbot.status()
    assert status["totals"]["realized_pnl"] == 0
    assert status["legacy_metrics_included"] is False
    assert status["legacy_snapshot"]["totals"]["realized_pnl"] == 106.2


def test_external_process_is_adopted_for_status_and_stop(monkeypatch):
    monkeypatch.setattr(loopbot, "_loop_bot_pids", lambda fresh=False: [9191])
    status = loopbot.status()
    assert status["managed_running"] is True
    assert status["controller_owned"] is False
    assert status["pid"] == 9191
    started = loopbot.start_bot()
    assert started["already_running"] is True and "error" not in started
    stopped = loopbot.stop_bot()
    assert stopped["stopped"] is True and stopped["pids"] == [9191]


def test_pid_discovery_matches_exact_runner_argv_not_broad_substrings(monkeypatch):
    target = loopbot.PROJECT_ROOT / "scripts" / "run_loop_bot.py"
    ps_output = "\n".join([
        "101 /bin/zsh -c python scripts/run_loop_bot.py --help",
        "102 /usr/bin/python -c print('run_loop_bot.py')",
        "103 /usr/bin/python /tmp/other/run_loop_bot.py",
        f"104 /usr/bin/python {target} --order-size 5",
    ])

    class Result:
        pass

    result = Result()
    result.stdout = ps_output
    monkeypatch.setattr(loopbot.subprocess, "run", lambda *args, **kwargs: result)
    loopbot._PID_CACHE.update({"t": 0.0, "pids": []})
    assert loopbot._loop_bot_pids(fresh=True) == [104]


def test_relative_runner_argv_requires_matching_process_cwd(monkeypatch):
    monkeypatch.setattr(loopbot, "_process_cwd", lambda pid: loopbot.PROJECT_ROOT if pid == 201 else Path("/tmp"))
    assert loopbot._argv_runs_this_loopbot(201, "/usr/bin/python scripts/run_loop_bot.py") is True
    assert loopbot._argv_runs_this_loopbot(202, "/usr/bin/python scripts/run_loop_bot.py") is False


def test_events_and_chart_when_no_files():
    assert loopbot.events()["events"] == []
    chart = loopbot.chart()
    assert chart["btc"] == [] and chart["entries"] == [] and chart["exits"] == []
    assert chart["totals"] == {}


def test_chart_builds_series_from_events(tmp_path):
    events = [
        {"t": 1000.0, "kind": "meta", "meta_kind": "start", "timeframes": ["5m"]},
        {"t": 1001.0, "kind": "btc", "price": 98000.0},
        {"t": 1002.0, "kind": "first_filled", "schema": "sequential_v2", "tf": "5m", "slug": "btc-updown-5m-1", "vwap": 0.48, "fee": 0.08736, "size": 5.0, "side": "yes", "btc": 98100.0},
        {"t": 1003.0, "kind": "pair_completed", "schema": "sequential_v2", "tf": "5m", "slug": "btc-updown-5m-1", "pnl": 0.02528, "combined": 0.96, "fees": 0.17472, "btc": 98200.0},
    ]
    (tmp_path / "events.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n")
    chart = loopbot.chart()
    assert len(chart["btc"]) == 1
    assert len(chart["entries"]) == 1 and chart["entries"][0]["tf"] == "5m"
    assert len(chart["exits"]) == 1
    assert chart["per_tf"]["5m"]["entries"] == 1
    assert chart["per_tf"]["5m"]["realized"] == pytest.approx(0.0253)


def test_loopbot_config_hot_reload(tmp_path):
    """Dashboard config writes must hot-reload into the running bot."""
    bot = runner.LoopBot({"order_size": 5.0, "min_edge": 0.03, "timeframes": ["5m"]}, finnhub_key=None)
    bot._config_mtime = 0.0
    (tmp_path / "config.json").write_text(json.dumps({
        "strategy_schema": "sequential_v2",
        "mode": "manual", "order_size": 25.0, "min_edge": 0.05,
        "no_entry_seconds": 4.0, "timeframes": ["5m", "15m"],
    }))
    assert bot._reload_config() is True
    assert bot.order_size == 25.0
    assert bot.min_edge == 0.05
    assert bot.no_entry_seconds == 4.0
    events = (tmp_path / "events.jsonl").read_text().strip().splitlines()
    reloaded = [json.loads(e) for e in events if json.loads(e).get("kind") == "meta" and json.loads(e).get("meta_kind") == "config_reload"]
    assert reloaded and reloaded[-1]["order_size"] == 25.0

    # unchanged file → no reload, no duplicate event
    events_before = len((tmp_path / "events.jsonl").read_text().strip().splitlines())
    assert bot._reload_config() is False
    events_after = len((tmp_path / "events.jsonl").read_text().strip().splitlines())
    assert events_after == events_before


def test_performance_roi_and_risk_adjusted_from_exits(tmp_path):
    """ROI = realized ÷ deployed; risk-adjusted = mean/std of per-trade returns."""
    events = [
        {"t": 900, "kind": "exit", "tf": "5m", "combined": 0.80, "size": 100, "pnl": 20.0},
        {"t": 1100, "kind": "pair_completed", "schema": "sequential_v2", "tf": "5m", "slug": "a", "capital_deployed": 4.9, "pnl": 0.1},
        {"t": 1300, "kind": "protective_exit", "schema": "sequential_v2", "tf": "15m", "slug": "b", "capital_deployed": 9.6, "pnl": 0.4},
    ]
    (tmp_path / "events.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n")
    perf = loopbot.performance()

    # cost = 0.98*5 + 0.96*10 = 4.9 + 9.6 = 14.5 ; pnl = 0.5 → ROI ≈ 3.45%
    assert perf["trades"] == 2
    assert perf["total_cost"] == pytest.approx(14.5)
    assert perf["total_pnl"] == pytest.approx(0.5)
    assert perf["roi_pct"] == pytest.approx(0.5 / 14.5 * 100, abs=0.01)
    # per-trade returns: 0.1/4.9 ≈ 0.020408, 0.4/9.6 ≈ 0.041667 → mean ≈ 0.031037
    assert perf["mean_trade_ret_pct"] == pytest.approx(3.10, abs=0.01)
    assert perf["std_trade_ret_pct"] is not None and perf["std_trade_ret_pct"] > 0
    assert perf["sharpe"] is not None and perf["sharpe"] > 0
    assert perf["legacy_events_excluded"] == 1


def test_performance_empty_and_single_trade(tmp_path):
    assert loopbot.performance()["roi_pct"] is None
    (tmp_path / "events.jsonl").write_text(json.dumps(
        {"t": 1, "kind": "pair_completed", "schema": "sequential_v2", "tf": "5m", "slug": "s", "capital_deployed": 4.85, "pnl": 0.15}
    ) + "\n")
    perf = loopbot.performance()
    assert perf["trades"] == 1
    assert perf["roi_pct"] == pytest.approx(0.15 / 4.85 * 100, abs=0.01)
    assert perf["sharpe"] is None  # need 2+ trades for std
    assert perf["std_trade_ret_pct"] is None


def test_status_includes_performance(tmp_path):
    (tmp_path / "events.jsonl").write_text(json.dumps(
        {"t": 1, "kind": "pair_completed", "schema": "sequential_v2", "tf": "5m", "slug": "s", "capital_deployed": 9.0, "pnl": 1.0}
    ) + "\n")
    status = loopbot.status()
    assert "performance" in status
    assert status["performance"]["roi_pct"] is not None


class _StubProc:
    def __init__(self):
        self.pid = 4242
        self.terminated = False
        self.killed = False
        self.exit = None

    def poll(self):
        return self.exit

    def terminate(self):
        self.terminated = True
        self.exit = 15

    def kill(self):
        self.killed = True
        self.terminated = True

    def wait(self, timeout=0):
        return self.exit


def test_stop_returns_immediately_and_terminates_in_background(tmp_path):
    """STOP must not block the request until the process exits."""
    import time

    proc = _StubProc()
    loopbot._STATE["proc"] = proc
    start = time.time()
    result = loopbot.stop_bot()
    elapsed = time.time() - start
    assert result["stopped"] is True
    assert "background" in result["note"]
    assert elapsed < 1.0, f"stop_bot blocked for {elapsed:.1f}s"
    deadline = time.time() + 5
    while not proc.terminated and time.time() < deadline:
        time.sleep(0.02)
    assert proc.terminated


def test_stop_of_already_exited_proc_is_honest(tmp_path):
    proc = _StubProc()
    proc.exit = 0
    loopbot._STATE["proc"] = proc
    result = loopbot.stop_bot()
    assert result["stopped"] is True
    assert "already exited" in result["note"]
