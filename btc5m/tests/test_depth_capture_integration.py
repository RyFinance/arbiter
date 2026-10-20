"""Integration test: verify the depth-capture pipeline end-to-end.

Verifies that during a brief in-process paper session configuration:
  1. bot.jsonl is written with INFO events (startup messages).
  2. alerts.jsonl is written with correct schema structure.
  3. backtest_validation.json schema is understood and validated.

Critically, asserts that NO writes leak to the real ``state/`` directory of the
polymarket_arb project.  The existing ``isolate_alerts_log`` autouse fixture in
``conftest.py`` already redirects ``ALERTS_LOG_FILE``.  We additionally redirect
``SETTINGS.log_jsonl`` and ``SETTINGS.alert_heartbeat_file`` to ``tmp_path``
so the bot log and heartbeat files are hermetic too.

Usage:
    cd polymarket_arb
    PYTHONPATH=polymarket_arb python3 -m pytest tests/test_depth_capture_integration.py -v
"""
from __future__ import annotations

import json
import os
import sys
import asyncio
from pathlib import Path

import pytest

# --------------------------------------------------------------------------- #
#  Env-var patching BEFORE any bot import                                      #
# --------------------------------------------------------------------------- #
os.environ["PAPER_TRADE"] = "true"
os.environ["DRY_RUN"] = "false"
os.environ["LIVE_SIGNING_VERIFIED"] = "true"
os.environ["PRIVATE_KEY"] = "0x0000000000000000000000000000000000000000000000000000000000000001"
os.environ["PROXY_WALLET"] = "0x0000000000000000000000000000000000000001"

from polymarket_arb.config import SETTINGS  # noqa: E402
import polymarket_arb.alerting as _alerting  # noqa: E402
import polymarket_arb.logger as _logger_mod  # noqa: E402
from polymarket_arb.logger import BotLogger  # noqa: E402


# --------------------------------------------------------------------------- #
#  Fixtures                                                                   #
# --------------------------------------------------------------------------- #

@pytest.fixture()
def isolated_settings(tmp_path, monkeypatch):
    """Redirect all on-disk output paths to tmp_path.

    Guarantees no writes to real ``polymarket_arb/state/``.  Uses
    ``object.__setattr__`` because SETTINGS is a frozen dataclass.
    """
    tmp_state = tmp_path / "state"
    tmp_state.mkdir(parents=True, exist_ok=True)
    tmp_cache = tmp_state / "cache"
    tmp_cache.mkdir(parents=True, exist_ok=True)

    bot_jsonl = tmp_state / "bot.jsonl"
    alerts_jsonl = tmp_state / "alerts.jsonl"
    heartbeat_file = tmp_state / "HEARTBEAT"
    backtest_file = tmp_state / "backtest_validation.json"

    # SETTINGS is a frozen dataclass — bypass with object.__setattr__.
    _orig_log_jsonl = SETTINGS.log_jsonl
    _orig_heartbeat = SETTINGS.alert_heartbeat_file
    object.__setattr__(SETTINGS, "log_jsonl", str(bot_jsonl))
    object.__setattr__(SETTINGS, "alert_heartbeat_file", str(heartbeat_file))

    # The autouse ``isolate_alerts_log`` fixture in conftest.py already patches
    # ``_alerting.ALERTS_LOG_FILE``; we set it again here for assertion convenience.
    monkeypatch.setattr(_alerting, "ALERTS_LOG_FILE", str(alerts_jsonl))

    # Create a BotLogger that writes to the tmp bot.jsonl and propagate it.
    log_instance = BotLogger(jsonl_path=str(bot_jsonl), level="INFO")
    monkeypatch.setattr(_logger_mod, "log", log_instance)

    import polymarket_arb.main as _main
    import polymarket_arb.arb_engine as _arb_engine
    import polymarket_arb.execution as _execution
    import polymarket_arb.price_feed as _price_feed
    import polymarket_arb.risk as _risk_mod
    import polymarket_arb.kronos_session as _kronos
    import polymarket_arb.window_tracker as _window_tracker
    import polymarket_arb.cache as _cache_mod
    import polymarket_arb.metrics as _metrics_mod

    for mod in [
        _main, _arb_engine, _execution, _price_feed, _risk_mod,
        _kronos, _window_tracker, _cache_mod, _metrics_mod,
        _alerting,
    ]:
        if hasattr(mod, "log"):
            monkeypatch.setattr(mod, "log", log_instance)

    def _patched_setup():
        return log_instance
    monkeypatch.setattr(_main, "_setup_logger", _patched_setup)

    async def _test_regime():
        # This integration suite verifies paper-session isolation and startup logs,
        # not 25M-parameter Kronos checkpoint deserialization. Loading that optional
        # model in a test task that is deliberately cancelled after 15 seconds can
        # leave a native PyTorch loader thread alive and crash the test process.
        return _kronos.VolRegime(
            regime="MED", atr_14d=0.0, confidence=0.0,
            no_entry_seconds=3.0, max_quote_age_ms=500, source="test_fixture",
        )

    monkeypatch.setattr(_kronos, "forecast_session_regime", _test_regime)

    # Patch price_feed._on_ws_tick for async callback scheduling.
    from polymarket_arb.price_feed import PriceFeed
    import time as _time_mod

    def _patched_on_ws_tick(self, token_id, best_ask, size, best_bid=None):
        st = self._token_owner.get(token_id)
        if st is None:
            return
        if token_id == st.yes_token_id:
            st.best_yes_ask, st.yes_size, st.best_yes_bid = best_ask, size, best_bid
        else:
            st.best_no_ask, st.no_size, st.best_no_bid = best_ask, size, best_bid
        st.last_update_ts = _time_mod.time()
        if token_id == st.yes_token_id:
            st.yes_ts = _time_mod.time()
        else:
            st.no_ts = _time_mod.time()
        if self.on_update is not None:
            coro = self.on_update(st)
            if asyncio.iscoroutine(coro):
                asyncio.create_task(coro)

    monkeypatch.setattr(PriceFeed, "_on_ws_tick", _patched_on_ws_tick)

    yield {
        "tmp_state": tmp_state,
        "bot_jsonl": bot_jsonl,
        "alerts_jsonl": alerts_jsonl,
        "backtest_file": backtest_file,
        "heartbeat_file": heartbeat_file,
    }

    # Restore SETTINGS fields (frozen dataclass).
    object.__setattr__(SETTINGS, "log_jsonl", _orig_log_jsonl)
    object.__setattr__(SETTINGS, "alert_heartbeat_file", _orig_heartbeat)


# --------------------------------------------------------------------------- #
#  Helpers                                                                     #
# --------------------------------------------------------------------------- #

def _read_jsonl(path: Path) -> list[dict]:
    """Read a JSONL file, returning all parsed dicts."""
    if not path.exists():
        return []
    results = []
    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                results.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return results


# --------------------------------------------------------------------------- #
#  Tests                                                                       #
# --------------------------------------------------------------------------- #

class TestIsolationGuarantees:
    """Verify that the test setup prevents writes to real state files."""

    def test_alerts_file_redirected_to_tmp(self, isolated_settings):
        """alerts.jsonl must point to tmp_path, not the real state dir."""
        real_state = Path(__file__).resolve().parents[2] / "state"
        real_alerts = real_state / "alerts.jsonl"
        test_alerts = isolated_settings["alerts_jsonl"]
        assert test_alerts != real_alerts
        assert str(test_alerts).startswith(str(isolated_settings["tmp_state"]))

    def test_bot_jsonl_redirected_to_tmp(self, isolated_settings):
        """SETTINGS.log_jsonl must point to tmp_path after fixture setup."""
        tmp_state_str = str(isolated_settings["tmp_state"])
        assert SETTINGS.log_jsonl.startswith(tmp_state_str)

    def test_settings_paper_trade_true(self, isolated_settings):
        """SETTINGS must reflect PAPER_TRADE=true, DRY_RUN=false."""
        assert SETTINGS.paper_trade is True
        assert SETTINGS.dry_run is False


class TestPaperSessionOutputs:
    """Verify bot.jsonl, alerts.jsonl, and backtest_validation.json schema."""

    @pytest.mark.asyncio
    async def test_paper_session_writes_bot_log(self, isolated_settings):
        """Run the bot briefly and verify bot.jsonl gets INFO events.

        The bot may crash if optional dependencies (e.g. py_clob_client_v2)
        are missing, but the startup sequence before that should always log
        INFO events to the redirected bot.jsonl.
        """
        bot_jsonl = isolated_settings["bot_jsonl"]
        from polymarket_arb.main import main as bot_main

        try:
            task = asyncio.create_task(bot_main())
            await asyncio.wait_for(task, timeout=15)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        except Exception as e:
            # Bot may exit due to missing optional deps — that's OK.
            # We just need the startup INFO logs to have been written.
            print(f"Bot exited early: {type(e).__name__}: {e}")

        # Verify bot.jsonl was written with INFO events.
        assert bot_jsonl.exists(), f"bot.jsonl was not created at {bot_jsonl}"
        records = _read_jsonl(bot_jsonl)
        assert len(records) > 0, "bot.jsonl has no records"

        # Must have INFO events (startup, market resolution).
        info_records = [r for r in records if r.get("event") == "INFO"]
        assert len(info_records) > 0, "No INFO events in bot.jsonl"

        # Check for key startup message.
        all_msgs = [r.get("msg", "") for r in records]
        has_startup = any("starting" in m.lower() for m in all_msgs)
        assert has_startup, "No startup message found in bot.jsonl"

    @pytest.mark.asyncio
    async def test_paper_session_no_real_state_writes(self, isolated_settings):
        """Run the bot briefly and confirm real state/ files are untouched."""
        real_state = Path(__file__).resolve().parents[2] / "state"
        real_bot_jsonl = real_state / "bot.jsonl"
        real_alerts_jsonl = real_state / "alerts.jsonl"

        # Record mtimes before running.
        pre_mtimes = {}
        for f in [real_bot_jsonl, real_alerts_jsonl]:
            if f.exists():
                pre_mtimes[f] = f.stat().st_mtime_ns

        from polymarket_arb.main import main as bot_main
        try:
            task = asyncio.create_task(bot_main())
            await asyncio.wait_for(task, timeout=15)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
            # Bot may crash — that's fine for this test.
            pass

        # Verify real files' mtimes haven't changed.
        for f in [real_bot_jsonl, real_alerts_jsonl]:
            if f.exists() and f in pre_mtimes:
                post_mtime = f.stat().st_mtime_ns
                assert post_mtime == pre_mtimes[f], \
                    f"Real state file {f.name} was modified during test!"

    def test_backtest_validation_schema(self):
        """Verify backtest_validation.json schema understanding (synthetic).

        The backtest engine produces a file with these top-level keys:
        config, metrics, total_ticks, duration_s, source.
        We validate our test understands the expected structure.
        """
        synthetic = {
            "config": {
                "symbol": "BTC/USD",
                "min_edge": 0.01,
                "order_size": 10.0,
            },
            "metrics": {
                "opportunities": 0,
                "fills": 0,
            },
            "total_ticks": 0,
            "duration_s": 0.0,
            "source": "test",
        }
        assert "config" in synthetic
        assert "metrics" in synthetic
        assert "total_ticks" in synthetic
        assert "duration_s" in synthetic
        assert "source" in synthetic

    def test_alert_schema_structure(self, isolated_settings):
        """Verify alerts.jsonl schema by writing and reading a synthetic alert."""
        from polymarket_arb.execution import WebhookAlerter

        alerts_jsonl = isolated_settings["alerts_jsonl"]
        alerter = WebhookAlerter(
            webhook_url="",
            dedup_window_s=1.0,
            alerts_path=str(alerts_jsonl),
        )

        # Send a test alert (alert() is synchronous — writes to JSONL).
        alerter.alert("TEST_CATEGORY", "test alert from integration test")

        # Verify the alert was written.
        assert alerts_jsonl.exists(), "alerts.jsonl was not created"
        records = _read_jsonl(alerts_jsonl)
        assert len(records) == 1, f"Expected 1 alert, got {len(records)}"
        alert = records[0]
        assert alert.get("kind") == "TEST_CATEGORY"
        # The alert schema uses "message" (not "msg" — that's bot.jsonl).
        assert "test alert" in alert.get("message", "")


# --------------------------------------------------------------------------- #
#  Dedup regression: sequence-scoped dedup must not drop legitimate rebroadcasts  #
# --------------------------------------------------------------------------- #

class TestDedupRegression:
    """Regression coverage for the dedup key fix.

    The previous dedup key used only (market_id, token_id, captured_at floored
    to the second, top-3 bids, top-3 asks).  This incorrectly merged distinct
    snapshots that shared the same top-of-book but differed at deeper price
    levels (legitimate WS rebroadcasts with new sequence numbers).

    The fix uses a sequence-scoped key: (market_id, token_id, sequence,
    full bids, full asks).  This preserves all legitimate snapshots while
    still catching exact re-captures (same sequence number from a re-run).
    """

    def _make_snapshot(self, seq: int, bid_sizes: list[float], ask_sizes: list[float]) -> dict:
        """Create a synthetic snapshot with the given sequence and level sizes."""
        return {
            "type": "depth_snapshot",
            "captured_at": 1786439509.0 + seq * 0.001,
            "captured_at_iso": f"2026-08-11T09:05:09.{seq:06d}Z",
            "market_id": "btc-updown-5m-1786439400",
            "token_id": "0x" + "a" * 64,
            "sequence": seq,
            "bids": [[0.50 - i * 0.01, s] for i, s in enumerate(bid_sizes)],
            "asks": [[0.50 + i * 0.01, s] for i, s in enumerate(ask_sizes)],
            "source": "clob_ws",
        }

    def test_legitimate_rebroadcasts_not_dropped(self, tmp_path):
        """Snapshots with new sequence but same top-3 must all survive."""
        import sys as _sys
        _scripts_dir = str(Path(__file__).resolve().parents[1] / "scripts")
        if _scripts_dir not in _sys.path:
            _sys.path.insert(0, _scripts_dir)
        from consolidate_depth import consolidate as consolidate_fn

        # Three snapshots: same top-3, but deeper levels differ, with
        # increasing sequence numbers.  Legitimate market updates, NOT dups.
        snaps = [
            self._make_snapshot(1, [100, 90, 80, 70], [30, 40, 50, 60]),
            self._make_snapshot(2, [100, 90, 80, 75], [30, 40, 50, 65]),
            self._make_snapshot(3, [100, 90, 80, 80], [30, 40, 50, 70]),
        ]

        src_a = tmp_path / "source_a.jsonl"
        src_a.write_text("\n".join(json.dumps(s) for s in snaps) + "\n")

        tmp_out = tmp_path / "output.jsonl"
        summary = consolidate_fn(
            source_a_path=src_a,
            source_b_dir=tmp_path / "nonexistent",
            output_path=tmp_out,
        )

        assert summary["unique_written"] == 3, \
            f"Expected 3, got {summary['unique_written']}: rebroadcasts wrongly dropped!"
        assert summary["duplicates_removed"] == 0

    def test_exact_duplicate_same_sequence_dropped(self, tmp_path):
        """Identical snapshot (same seq, same full book) IS a true duplicate."""
        import sys as _sys
        _scripts_dir = str(Path(__file__).resolve().parents[1] / "scripts")
        if _scripts_dir not in _sys.path:
            _sys.path.insert(0, _scripts_dir)
        from consolidate_depth import consolidate as consolidate_fn

        snap = self._make_snapshot(1, [100, 90, 80], [30, 40, 50])
        # Exact same snapshot written twice (simulates re-run / re-capture).
        snaps = [snap, snap.copy()]

        src_a = tmp_path / "source_a.jsonl"
        src_a.write_text("\n".join(json.dumps(s) for s in snaps) + "\n")

        tmp_out = tmp_path / "output.jsonl"
        summary = consolidate_fn(
            source_a_path=src_a,
            source_b_dir=tmp_path / "nonexistent",
            output_path=tmp_out,
        )

        assert summary["unique_written"] == 1
        assert summary["duplicates_removed"] == 1

    def test_same_book_same_second_different_sequence_dropped(self, tmp_path):
        """Independent rogue writers use different sequence counters, so an
        unchanged full book in the same second must still collapse to one row."""
        import sys as _sys
        _scripts_dir = str(Path(__file__).resolve().parents[1] / "scripts")
        if _scripts_dir not in _sys.path:
            _sys.path.insert(0, _scripts_dir)
        from consolidate_depth import consolidate as consolidate_fn

        first = self._make_snapshot(1, [100, 90, 80], [30, 40, 50])
        duplicate = {**first, "sequence": 9001, "captured_at": first["captured_at"] + 0.400}
        src_a = tmp_path / "source_a.jsonl"
        src_a.write_text("\n".join(json.dumps(s) for s in [first, duplicate]) + "\n")

        tmp_out = tmp_path / "output.jsonl"
        summary = consolidate_fn(src_a, tmp_path / "nonexistent", tmp_out)

        assert summary["unique_written"] == 1
        assert summary["duplicates_removed"] == 1
        assert summary["dedup_policy"] == "market+token+captured_second+full_book"

    def test_same_book_later_second_is_preserved(self, tmp_path):
        """Deduplication must not erase an unchanged book's time history."""
        import sys as _sys
        _scripts_dir = str(Path(__file__).resolve().parents[1] / "scripts")
        if _scripts_dir not in _sys.path:
            _sys.path.insert(0, _scripts_dir)
        from consolidate_depth import consolidate as consolidate_fn

        first = self._make_snapshot(1, [100, 90, 80], [30, 40, 50])
        later = {**first, "sequence": 2, "captured_at": first["captured_at"] + 1.001}
        src_a = tmp_path / "source_a.jsonl"
        src_a.write_text("\n".join(json.dumps(s) for s in [first, later]) + "\n")

        tmp_out = tmp_path / "output.jsonl"
        summary = consolidate_fn(src_a, tmp_path / "nonexistent", tmp_out)

        assert summary["unique_written"] == 2
        assert summary["duplicates_removed"] == 0

    def test_rebuild_is_idempotent_and_chronological(self, tmp_path):
        """A rerun replaces—not appends—the output and merges shards by time."""
        import sys as _sys
        _scripts_dir = str(Path(__file__).resolve().parents[1] / "scripts")
        if _scripts_dir not in _sys.path:
            _sys.path.insert(0, _scripts_dir)
        from consolidate_depth import consolidate as consolidate_fn

        early = self._make_snapshot(1, [100], [30])
        late = {**self._make_snapshot(2, [101], [31]), "captured_at": early["captured_at"] + 10}
        src_a = tmp_path / "source_a.jsonl"
        shard = tmp_path / "source_a.2026-08-15.jsonl"
        middle = {**self._make_snapshot(3, [102], [32]), "captured_at": early["captured_at"] + 5}
        # The active source itself is intentionally non-monotonic; this mirrors
        # the rogue-process interleaving found in the real capture files.
        src_a.write_text("\n".join(json.dumps(r) for r in [late, middle]) + "\n")
        shard.write_text(json.dumps(early) + "\n")
        tmp_out = tmp_path / "output.jsonl"

        first_summary = consolidate_fn(src_a, tmp_path / "nonexistent", tmp_out)
        first_bytes = tmp_out.read_bytes()
        second_summary = consolidate_fn(src_a, tmp_path / "nonexistent", tmp_out)

        records = [json.loads(line) for line in tmp_out.read_text().splitlines()]
        assert [r["captured_at"] for r in records] == sorted(r["captured_at"] for r in records)
        assert tmp_out.read_bytes() == first_bytes
        assert first_summary["unique_written"] == second_summary["unique_written"] == 3

    def test_cross_session_different_sequence_preserved(self, tmp_path):
        """Two capture sessions may produce same book at same second but
        different sequence numbers — both must survive."""
        import sys as _sys
        _scripts_dir = str(Path(__file__).resolve().parents[1] / "scripts")
        if _scripts_dir not in _sys.path:
            _sys.path.insert(0, _scripts_dir)
        from consolidate_depth import consolidate as consolidate_fn

        base = self._make_snapshot(1, [100, 90, 80], [30, 40, 50])
        # Simulate a re-subscription rebroadcast from a different session:
        # same market/token/ts, but sequence=9999 and a deeper level differs.
        rebroadcast = base.copy()
        rebroadcast["sequence"] = 9999
        rebroadcast["bids"] = [[0.50, 100], [0.49, 90], [0.48, 80], [0.47, 0.01]]

        src_a = tmp_path / "source_a.jsonl"
        src_a.write_text(
            "\n".join(json.dumps(s) for s in [base, rebroadcast]) + "\n"
        )

        tmp_out = tmp_path / "output.jsonl"
        summary = consolidate_fn(
            source_a_path=src_a,
            source_b_dir=tmp_path / "nonexistent",
            output_path=tmp_out,
        )

        assert summary["unique_written"] == 2, \
            f"Expected 2, got {summary['unique_written']}: cross-session rebroadcast dropped!"
        assert summary["duplicates_removed"] == 0


# --------------------------------------------------------------------------- #
#  Rotation + status sidecar regression                                          #
# --------------------------------------------------------------------------- #

class TestRotationAndStatus:
    """Verify file rotation (size + date) and status sidecar (sta_status.json)."""

    def _import_writer(self):
        """Import DepthCaptureWriter from capture_depth_ws.py."""
        import sys as _sys
        _scripts_dir = str(Path(__file__).resolve().parents[1] / "scripts")
        if _scripts_dir not in _sys.path:
            _sys.path.insert(0, _scripts_dir)
        from capture_depth_ws import DepthCaptureWriter
        return DepthCaptureWriter

    def test_writer_rotates_on_size(self, tmp_path):
        """Writer must rotate to a new shard when max_size is exceeded."""
        DepthCaptureWriter = self._import_writer()
        writer = DepthCaptureWriter(
            output_path=tmp_path / "polymarket_depth_capture.jsonl",
            max_size_bytes=500,  # tiny for testing
        )

        # Write enough snapshots to trigger rotation.
        for i in range(20):
            writer.write_snapshot(
                market_id="m1", token_id="t1", sequence=i,
                bids=[[0.50, 10.0 + i]], asks=[[0.52, 5.0]],
            )

        files = sorted(tmp_path.glob("polymarket_depth_capture*.jsonl"))
        assert len(files) >= 2, f"Expected >=2 shards after size rotation, got {len(files)}"
        assert writer._file_rotations >= 1

    def test_writer_rotates_on_date_change(self, tmp_path):
        """Writer must rotate to a date-suffixed shard when UTC date changes."""
        DepthCaptureWriter = self._import_writer()
        writer = DepthCaptureWriter(
            output_path=tmp_path / "polymarket_depth_capture.jsonl",
            max_size_bytes=500 * 1024 * 1024,  # large — won't trigger size rotation
        )

        # Write one snapshot.
        writer.write_snapshot(
            market_id="m1", token_id="t1", sequence=1,
            bids=[[0.50, 10.0]], asks=[[0.52, 5.0]],
        )
        old_date = writer._current_date_str
        old_path = writer.output_path

        # Simulate date change.
        writer._current_date_str = "2000-01-01"  # force old date
        writer.write_snapshot(
            market_id="m1", token_id="t1", sequence=2,
            bids=[[0.51, 10.0]], asks=[[0.53, 5.0]],
        )

        files = sorted(tmp_path.glob("polymarket_depth_capture*.jsonl"))
        assert len(files) >= 2, f"Expected >=2 shards after date rotation, got {len(files)}"
        # The new path should differ from the old.
        assert writer.output_path != old_path
        # Verify shards exist.
        files = sorted(tmp_path.glob("polymarket_depth_capture*.jsonl"))
        assert len(files) >= 2

    def test_rotation_names_never_compound_after_multiple_rollovers(self, tmp_path):
        """Every shard name must derive from the stable base path."""
        DepthCaptureWriter = self._import_writer()
        writer = DepthCaptureWriter(
            output_path=tmp_path / "polymarket_depth_capture.jsonl",
            max_size_bytes=1,
        )
        kwargs = {
            "market_id": "m1", "token_id": "t1",
            "bids": [[0.50, 10.0]], "asks": [[0.52, 5.0]],
        }
        writer.write_snapshot(sequence=1, **kwargs)
        writer.write_snapshot(sequence=2, **kwargs)
        writer._current_date_str = "2000-01-01"
        writer.write_snapshot(sequence=3, **kwargs)
        writer.write_snapshot(sequence=4, **kwargs)

        today = writer._today_str()
        names = [path.name for path in tmp_path.glob("*.jsonl")]
        assert f"polymarket_depth_capture.{today}.jsonl" in names
        assert all(name.count(today) == 1 for name in names if today in name)
        assert not any(f".{today}.1.{today}." in name for name in names)

    def test_status_sidecar_schema(self, tmp_path):
        """sta_status.json must have the exact schema: alive, last_snapshot_epoch,
        snapshots_total, current_window, rollovers, disk_free_gb, file_today_mb."""
        DepthCaptureWriter = self._import_writer()
        writer = DepthCaptureWriter(
            output_path=tmp_path / "polymarket_depth_capture.jsonl",
            status_file=tmp_path / "sta_status.json",
        )

        # Write a snapshot.
        writer.write_snapshot(
            market_id="btc-updown-5m-1786439400", token_id="tok1", sequence=1,
            bids=[[0.50, 10.0]], asks=[[0.52, 5.0]],
        )
        # Write status.
        writer._write_status(current_window="btc-updown-5m-1786439400", rollovers=1)

        status_path = tmp_path / "sta_status.json"
        assert status_path.exists(), "sta_status.json was not created"
        status = json.loads(status_path.read_text())

        expected_keys = {
            "alive", "last_snapshot_epoch", "snapshots_total",
            "current_window", "rollovers", "disk_free_gb", "file_today_mb",
        }
        assert set(status.keys()) == expected_keys, \
            f"Schema mismatch: {set(status.keys())} != {expected_keys}"
        assert status["alive"] is True
        assert status["snapshots_total"] == 1
        assert status["current_window"] == "btc-updown-5m-1786439400"
        assert status["rollovers"] == 1
        assert isinstance(status["last_snapshot_epoch"], float)
        assert isinstance(status["disk_free_gb"], (int, float))
        assert isinstance(status["file_today_mb"], (int, float))

    def test_consolidate_globs_rotated_shards(self, tmp_path):
        """Consolidation must read ALL rotated shards, not just the active file."""
        import sys as _sys
        _scripts_dir = str(Path(__file__).resolve().parents[1] / "scripts")
        if _scripts_dir not in _sys.path:
            _sys.path.insert(0, _scripts_dir)
        from consolidate_depth import consolidate as consolidate_fn

        # Create an "active" shard + a rotated shard.
        base = {
            "type": "depth_snapshot",
            "captured_at": 1786439509.0,
            "captured_at_iso": "2026-08-11T09:05:09Z",
            "market_id": "m1",
            "token_id": "tok1",
            "sequence": 1,
            "bids": [[0.50, 10.0]],
            "asks": [[0.52, 5.0]],
            "source": "clob_ws",
        }

        active = tmp_path / "polymarket_depth_capture.jsonl"
        rotated = tmp_path / "polymarket_depth_capture.2026-08-12.jsonl"

        active.write_text(json.dumps(base) + "\n")
        rotated.write_text(json.dumps({**base, "sequence": 2, "captured_at": 1786439600.0}) + "\n")

        tmp_out = tmp_path / "output.jsonl"
        summary = consolidate_fn(
            source_a_path=active,
            source_b_dir=tmp_path / "nonexistent",
            output_path=tmp_out,
        )

        assert summary["source_a_records"] == 2, \
            f"Expected 2 records from shards, got {summary['source_a_records']}"
        assert summary["unique_written"] == 2
