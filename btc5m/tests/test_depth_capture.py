"""Tests for the live depth-ladder capture engine (Task 2).

Validates:
  1. DepthSnapshot serialization (asks sorted ascending, bids descending).
  2. Level parsing handles dict and tuple formats.
  3. DepthFileWriter rotates on size and date boundaries.
  4. verify_depth_capture correctly counts snapshots and detects gaps.
  5. DepthCaptureClient._handle parses book frames and routes to the correct
     market via token-to-market mapping.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

import sys
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import capture_live_depth as cap
from capture_live_depth import DepthSnapshot, DepthFileWriter, parse_levels, parse_levels_directional
import verify_depth_capture as vdc


# --------------------------------------------------------------------------- #
# DepthSnapshot serialization                                                   #
# --------------------------------------------------------------------------- #

def test_depth_snapshot_serializes_with_sorted_levels():
    """Asks must be ascending, bids descending in the serialized output."""
    snap = DepthSnapshot(
        market_id="m1", token_id="tok1", ts=1000.0, wall_time="2026-08-05T10:00:00Z",
        asks=[(0.52, 10), (0.50, 5), (0.48, 3)],  # unsorted input
        bids=[(0.48, 3), (0.50, 5), (0.52, 10)],  # unsorted input
    )
    d = snap.to_dict()
    assert d["asks"] == [[0.48, 3], [0.50, 5], [0.52, 10]]
    assert d["bids"] == [[0.52, 10], [0.50, 5], [0.48, 3]]


def test_depth_snapshot_includes_metadata():
    snap = DepthSnapshot(
        market_id="m1", token_id="tok1", ts=1000.0, wall_time="2026-08-05T10:00:00Z",
        asks=[], bids=[], update_id=42, received_at=1000.01,
    )
    d = snap.to_dict()
    assert d["market_id"] == "m1"
    assert d["token_id"] == "tok1"
    assert d["event"] == "book_update"
    assert d["update_id"] == 42
    assert d["ts"] == 1000.0


# --------------------------------------------------------------------------- #
# Level parsing                                                               #
# --------------------------------------------------------------------------- #

def test_parse_levels_handles_dict_format():
    """CLOB returns asks/bids as [{"price": "0.50", "size": "100"}]."""
    levels = parse_levels([{"price": "0.50", "size": "100"}, {"price": "0.51", "size": "50"}])
    assert levels == [(0.50, 100.0), (0.51, 50.0)]


def test_parse_levels_handles_tuple_format():
    levels = parse_levels([["0.50", "100"], [0.51, 50]])
    assert levels == [(0.50, 100.0), (0.51, 50.0)]


def test_parse_levels_skips_invalid():
    levels = parse_levels([{"price": "abc", "size": "10"}, "bad", None, [1, 2]])
    assert levels == [(1.0, 2.0)]


def test_parse_levels_directional_sorts_correctly():
    asks_raw = [{"price": "0.52", "size": "10"}, {"price": "0.50", "size": "5"}]
    bids_raw = [{"price": "0.48", "size": "3"}, {"price": "0.50", "size": "5"}]
    asks, bids = parse_levels_directional(asks_raw, bids_raw)
    assert asks == [(0.50, 5.0), (0.52, 10.0)]  # ascending
    assert bids == [(0.50, 5.0), (0.48, 3.0)]   # descending


# --------------------------------------------------------------------------- #
# DepthFileWriter                                                             #
# --------------------------------------------------------------------------- #

def test_writer_creates_file_and_directory(tmp_path):
    writer = DepthFileWriter(output_root=str(tmp_path / "depth"))
    snap = DepthSnapshot(
        market_id="m1", token_id="t1", ts=1000.0, wall_time="2026-08-05T10:00:00Z",
        asks=[(0.50, 5.0)], bids=[(0.48, 3.0)],
    )
    writer.write_snapshot(snap)
    writer.close()

    files = list((tmp_path / "depth" / "m1").glob("*.jsonl"))
    assert len(files) == 1
    line = files[0].read_text().strip()
    rec = json.loads(line)
    assert rec["market_id"] == "m1"
    assert rec["token_id"] == "t1"
    assert rec["asks"] == [[0.50, 5.0]]
    assert rec["bids"] == [[0.48, 3.0]]


def test_writer_rotates_on_size_exceeded(tmp_path):
    """Files must rotate when they exceed max_size_bytes."""
    max_size = 100  # tiny for testing
    writer = DepthFileWriter(output_root=str(tmp_path / "depth"), max_size=max_size)

    for i in range(10):
        snap = DepthSnapshot(
            market_id="m1", token_id="t1", ts=float(i),
            wall_time="2026-08-05T10:00:00Z",
            asks=[(0.50 + i * 0.01, 10.0)], bids=[(0.48, 3.0)],
        )
        writer.write_snapshot(snap)

    files = sorted((tmp_path / "depth" / "m1").glob("*.jsonl"))
    assert len(files) >= 2, f"Expected >=2 files after rotation, got {len(files)}"
    writer.close()


def test_writer_rotates_on_date_change(tmp_path):
    """Files must rotate when the date changes (midnight UTC)."""
    writer = DepthFileWriter(output_root=str(tmp_path / "depth"))
    old_date = "2026-08-05"
    new_date = "2026-08-06"
    writer._date_str = lambda: old_date

    snap1 = DepthSnapshot(
        market_id="m1", token_id="t1", ts=1000.0, wall_time="2026-08-05T10:00:00Z",
        asks=[(0.50, 5.0)], bids=[(0.48, 3.0)],
    )
    writer.write_snapshot(snap1)
    writer.close()

    # Simulate date change: patch _date_str to return a different date.
    # Since _get_file and _open_file both call _date_str, we need the first
    # call (in _get_file) to return the OLD date (so _files has the old entry),
    # but then _open_file (called because dates differ) to return the NEW date.
    # Simpler: just patch to always return the new date — the old entry's
    # stored date_str will be "old" while now_date is "new", triggering rotation.
    writer2 = DepthFileWriter(output_root=str(tmp_path / "depth"))

    from datetime import datetime, timezone as tz
    # The _files dict from writer1 was lost (writer.close() doesn't clear it
    # but writer2 is a new instance). So we need to simulate having an open
    # file from "yesterday". Manually inject it.
    old_fh = open(tmp_path / "depth" / "m1" / f"{old_date}.jsonl", "a")
    writer2._files["m1"] = (old_fh, old_date, 0)

    # Now patch _date_str to return new_date.
    writer2._date_str = lambda: new_date

    snap2 = DepthSnapshot(
        market_id="m1", token_id="t1", ts=2000.0, wall_time="2026-08-06T00:00:01Z",
        asks=[(0.51, 5.0)], bids=[(0.49, 3.0)],
    )
    writer2.write_snapshot(snap2)
    writer2.close()

    files = sorted((tmp_path / "depth" / "m1").glob("*.jsonl"))
    assert len(files) == 2, f"Expected 2 files (one per date), got {len(files)}"
    assert any("2026-08-05" in f.name for f in files)
    assert any("2026-08-06" in f.name for f in files)


# --------------------------------------------------------------------------- #
# DepthCaptureClient._handle                                                   #
# --------------------------------------------------------------------------- #

class _DummyWriter:
    """Captures snapshots in-memory for testing."""
    def __init__(self):
        self.snapshots = []
    def write_snapshot(self, snap):
        self.snapshots.append(snap)
    def close(self):
        pass


def test_capture_client_parses_book_frame():
    """_handle must parse a CLOB WS book frame and extract full depth."""
    writer = _DummyWriter()
    token_map = {"tok_yes": "m1", "tok_no": "m1"}
    client = cap.DepthCaptureClient(
        token_ids=["tok_yes", "tok_no"],
        writer=writer,
        token_to_market=token_map,
    )

    frame = json.dumps([{
        "asset_id": "tok_yes",
        "asks": [{"price": "0.52", "size": "100"}, {"price": "0.55", "size": "50"}],
        "bids": [{"price": "0.49", "size": "30"}, {"price": "0.48", "size": "20"}],
    }, {
        "asset_id": "tok_no",
        "asks": [{"price": "0.51", "size": "80"}],
        "bids": [{"price": "0.48", "size": "40"}],
    }])

    client._handle(frame)
    assert len(writer.snapshots) == 2
    snap = writer.snapshots[0]
    assert snap.token_id == "tok_yes"
    assert snap.market_id == "m1"
    assert snap.asks == [(0.52, 100.0), (0.55, 50.0)]
    assert snap.bids == [(0.49, 30.0), (0.48, 20.0)]
    snap2 = writer.snapshots[1]
    assert snap2.token_id == "tok_no"
    assert snap2.asks == [(0.51, 80.0)]
    assert snap2.bids == [(0.48, 40.0)]


def test_capture_client_skips_pong():
    writer = _DummyWriter()
    client = cap.DepthCaptureClient(
        token_ids=["tok"], writer=writer, token_to_market={},
    )
    client._handle("PONG")
    client._handle("")
    assert len(writer.snapshots) == 0


def test_capture_client_handles_non_json():
    writer = _DummyWriter()
    client = cap.DepthCaptureClient(
        token_ids=["tok"], writer=writer, token_to_market={},
    )
    client._handle("not json at all")
    assert len(writer.snapshots) == 0


def test_capture_client_tracks_update_sequence():
    writer = _DummyWriter()
    client = cap.DepthCaptureClient(
        token_ids=["tok"], writer=writer, token_to_market={"tok": "m1"},
    )
    frame = json.dumps({"asset_id": "tok", "asks": [{"price": "0.50", "size": "10"}], "bids": []})
    client._handle(frame)
    client._handle(frame)
    assert writer.snapshots[0].update_id == 1
    assert writer.snapshots[1].update_id == 2
    assert client._updates_per_token["tok"] == 2


# --------------------------------------------------------------------------- #
# verify_depth_capture                                                         #
# --------------------------------------------------------------------------- #

def _write_depth_jsonl(path: Path, snapshots: list[tuple[float, str, list, list]]):
    """Write synthetic depth snapshots to a JSONL file."""
    lines = []
    for i, (ts, tok, asks, bids) in enumerate(snapshots):
        snap = DepthSnapshot(
            market_id="test-market", token_id=tok,
            ts=ts, wall_time="2026-08-05T10:00:00Z",
            asks=asks, bids=bids, update_id=i,
        )
        lines.append(json.dumps(snap.to_dict()))
    path.write_text("\n".join(lines) + "\n")


def test_verify_depth_capture_detects_gaps(tmp_path):
    """verify_depth_capture must detect gaps > 60s between consecutive snapshots."""
    depth_root = tmp_path / "depth_ladders"
    market_dir = depth_root / "test-market"
    market_dir.mkdir(parents=True)

    jsonl = market_dir / "2026-08-05.jsonl"
    snapshots = [
        (1000.0, "tok1", [(0.50, 10)], [(0.48, 5)]),
        (1010.0, "tok1", [(0.51, 10)], [(0.49, 5)]),
        (1130.0, "tok1", [(0.52, 10)], [(0.50, 5)]),  # 120s gap
    ]
    _write_depth_jsonl(jsonl, snapshots)

    report = vdc.verify_depth_capture(depth_root)
    assert report["total_snapshots"] == 3
    assert report["total_gaps"] == 1
    assert len(report["gaps"]) == 1
    assert report["completeness_pct"] == 0.0  # 1 gap means incomplete


def test_verify_depth_capture_complete_file(tmp_path):
    """A file with no gaps should report 100% completeness."""
    depth_root = tmp_path / "depth_ladders"
    market_dir = depth_root / "test-market"
    market_dir.mkdir(parents=True)

    jsonl = market_dir / "2026-08-05.jsonl"
    snapshots = []
    for i in range(100):
        snapshots.append((1000.0 + i * 5.0, "tok1", [(0.50 + i * 0.001, 10)], [(0.48, 5)]))
    _write_depth_jsonl(jsonl, snapshots)

    report = vdc.verify_depth_capture(depth_root)
    assert report["total_snapshots"] == 100
    assert report["total_gaps"] == 0
    assert report["completeness_pct"] == 100.0


def test_verify_depth_capture_nonexistent_root(tmp_path):
    report = vdc.verify_depth_capture(tmp_path / "nonexistent")
    assert report["total_snapshots"] == 0
    assert report["file_count"] == 0
    assert "error" in report


def test_verify_depth_capture_per_token_counts(tmp_path):
    depth_root = tmp_path / "depth_ladders"
    market_dir = depth_root / "test-market"
    market_dir.mkdir(parents=True)
    jsonl = market_dir / "2026-08-05.jsonl"

    snapshots = [
        (1000.0, "tok_yes", [(0.50, 10)], [(0.48, 5)]),
        (1005.0, "tok_no", [(0.51, 10)], [(0.49, 5)]),
        (1010.0, "tok_yes", [(0.52, 10)], [(0.50, 5)]),
    ]
    _write_depth_jsonl(jsonl, snapshots)

    report = vdc.verify_depth_capture(depth_root)
    assert report["tokens"]["tok_yes"] == 2
    assert report["tokens"]["tok_no"] == 1


# --------------------------------------------------------------------------- #
# Token resolution                                                            #
# --------------------------------------------------------------------------- #

def test_resolve_token_ids_from_cache(tmp_path, monkeypatch):
    """_resolve_token_ids must read market_cache.json format."""
    cache = {
        "generated_at": time.time(),
        "source": "gamma",
        "markets": [
            {"market_id": "m1", "yes_token_id": "y1", "no_token_id": "n1"},
            {"market_id": "m2", "yes_token_id": "y2", "no_token_id": "n2"},
        ],
    }
    cache_path = PROJECT_ROOT / "state" / "market_cache.json"
    original = cache_path.read_text() if cache_path.exists() else None
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache))
    try:
        result = cap._resolve_token_ids()
        assert "m1" in result
        assert result["m1"] == ["y1", "n1"]
        assert "m2" in result
    finally:
        if original is not None:
            cache_path.write_text(original)
        else:
            cache_path.unlink()
