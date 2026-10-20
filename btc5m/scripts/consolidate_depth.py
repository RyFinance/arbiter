"""
Standalone, read-only consolidation adapter for Polymarket depth-capture data.

Reads two source locations, normalizes both to a canonical schema, deduplicates
exact capture keys, and atomically rebuilds one chronological output. NEVER
modifies, moves, renames, or deletes any source file.

Sources:
  - Source A: ``state/cache/polymarket_depth_capture.jsonl`` (our capture_depth_ws.py output)
    Schema: {type, captured_at, captured_at_iso, market_id, token_id, sequence, bids, asks, source}
  - Source B: ``state/depth_ladders/<market_id>/`` directory tree (capture_live_depth.py output)
    Schema: {ts, wall_time, event, market_id, token_id, update_id, received_at, asks, bids}

Output:
  ``state/cache/polymarket_depth_consolidated.jsonl`` (one snapshot per line)

Canonical output schema:
{
    "type":           "depth_snapshot",          # constant: record type tag
    "captured_at":    float,                     # epoch seconds when snapshot was received
    "captured_at_iso": str,                      # ISO-8601 UTC timestamp
    "market_id":      str,                       # Polymarket market_id / window slug
    "token_id":       str,                       # CLOB asset_id
    "sequence":       int,                       # per-session sequence / update_id
    "bids":           [[price, size], ...],      # sorted descending by price
    "asks":           [[price, size], ...],      # sorted ascending by price
    "source":         str,                       # "clob_ws" or "live_depth_ws"
}
"""
from __future__ import annotations

import argparse
import hashlib
import heapq
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Source locations (READ-ONLY — opened in "r" mode only).
SOURCE_A_PATH = PROJECT_ROOT / "state" / "cache" / "polymarket_depth_capture.jsonl"
SOURCE_A_GLOB = "polymarket_depth_capture*.jsonl"  # includes rotated shards
SOURCE_B_DIR = PROJECT_ROOT / "state" / "depth_ladders"

# Output location (only this file is written — by atomic replacement).
OUTPUT_PATH = PROJECT_ROOT / "state" / "cache" / "polymarket_depth_consolidated.jsonl"


def _read_jsonl(path: Path) -> list[dict]:
    """Read a JSONL file in read-only mode. Returns list of parsed records.

    Opens with 'r' mode — strictly read-only. Never modifies the source.
    """
    if not path.exists():
        print(f"[consolidate_depth] Source A not found: {path}", file=sys.stderr, flush=True)
        return []
    records: list[dict] = []
    with open(path, "r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as e:
                print(f"[consolidate_depth] Warning: failed to parse line {line_no} in {path}: {e}",
                      file=sys.stderr, flush=True)
                continue
    print(f"[consolidate_depth] Source A ({path.name}): {len(records)} records read",
          file=sys.stderr, flush=True)
    return records


def _read_shards(source_a_path: Path) -> list[dict]:
    """Read all JSONL files matching the rotation shard pattern.

    The capture script writes to ``source_a_path`` (e.g.
    ``polymarket_depth_capture.jsonl``) and rotates to sibling files like
    ``polymarket_depth_capture.2026-08-12.jsonl`` or
    ``polymarket_depth_capture.2026-08-12.1.jsonl``.  This function globs
    ``<stem>*.jsonl`` in the same directory as ``source_a_path`` so that
    rotated shards are always included.

    Opens each file in read-only mode ('r'). Never modifies source files.
    """
    pattern = f"{source_a_path.stem}*.jsonl"
    records: list[dict] = []
    for jsonl_file in sorted(source_a_path.parent.glob(pattern)):
        if not jsonl_file.is_file():
            continue
        print(f"[consolidate_depth] Reading shard: {jsonl_file.name}",
              file=sys.stderr, flush=True)
        records.extend(_read_jsonl(jsonl_file))
    return records


def _read_depth_ladders_dir(base_dir: Path) -> list[dict]:
    """Read all JSONL files under ``state/depth_ladders/<market_id>/`` recursively.

    Opens each file in read-only mode ('r'). Never modifies source files.
    """
    if not base_dir.exists():
        print(f"[consolidate_depth] Source B directory not found: {base_dir}",
              file=sys.stderr, flush=True)
        return []
    records: list[dict] = []
    for jsonl_file in sorted(base_dir.rglob("*.jsonl")):
        with open(jsonl_file, "r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError as e:
                    print(f"[consolidate_depth] Warning: failed to parse line {line_no} in {jsonl_file}: {e}",
                          file=sys.stderr, flush=True)
                    continue
    print(f"[consolidate_depth] Source B (depth_ladders): {len(records)} records read",
          file=sys.stderr, flush=True)
    return records


def _iter_jsonl_snapshot(path: Path, size_limit: int, stats: dict, group: str):
    """Yield parsed records up to the file size observed at consolidation start.

    The active capture shard is append-only. Bounding reads to its initial size
    makes one consolidation finite and reproducible while capture continues.
    """
    with open(path, "r", encoding="utf-8") as handle:
        line_no = 0
        while handle.tell() < size_limit:
            line = handle.readline()
            if not line:
                break
            line_no += 1
            if not line.strip():
                continue
            stats[f"source_{group}_records"] += 1
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                stats["skipped_malformed"] += 1
                print(
                    f"[consolidate_depth] Warning: failed to parse line {line_no} "
                    f"in {path}: {exc}", file=sys.stderr, flush=True,
                )


def _snapshot_source_files(source_a_path: Path, source_b_dir: Path,
                           excluded_paths: set[Path] | None = None):
    """Return immutable `(path, size, source_group)` inputs for this run."""
    excluded = {path.resolve() for path in (excluded_paths or set())}
    pattern = f"{source_a_path.stem}*.jsonl"
    paths = [
        (p, p.stat().st_size, "a")
        for p in source_a_path.parent.glob(pattern)
        if p.is_file() and p.resolve() not in excluded
    ]
    if source_b_dir.exists():
        paths.extend(
            (p, p.stat().st_size, "b")
            for p in source_b_dir.rglob("*.jsonl")
            if p.is_file() and p.resolve() not in excluded
        )
    return sorted(paths, key=lambda item: str(item[0]))


def _iter_normalized_file(path: Path, size_limit: int, stats: dict, group: str):
    for raw in _iter_jsonl_snapshot(path, size_limit, stats, group):
        normalized = _normalize_line(raw, "")
        if normalized is not None:
            yield normalized
        else:
            stats["skipped_malformed"] += 1


def _write_sorted_chunks(source_files, stats: dict, temp_dir: Path,
                         chunk_size: int = 20_000) -> list[Path]:
    """Normalize sources into bounded, independently time-sorted chunks.

    Rogue writers interleaved records in a way that left some source shards
    internally out of order. Sorting bounded chunks avoids loading the full
    multi-gigabyte capture into memory while making the final merge reliable.
    """
    chunks: list[Path] = []
    buffer: list[dict] = []

    def flush() -> None:
        if not buffer:
            return
        buffer.sort(key=lambda record: float(record["captured_at"]))
        chunk_path = temp_dir / f"chunk-{len(chunks):06d}.jsonl"
        with open(chunk_path, "w", encoding="utf-8") as handle:
            for record in buffer:
                handle.write(json.dumps(record, separators=(",", ":")) + "\n")
        chunks.append(chunk_path)
        buffer.clear()

    for path, size_limit, group in source_files:
        for record in _iter_normalized_file(path, size_limit, stats, group):
            buffer.append(record)
            if len(buffer) >= chunk_size:
                flush()
    flush()
    return chunks


def _iter_chronological_chunks(chunk_paths: list[Path]):
    """K-way merge sorted normalized chunks."""
    heap = []
    iterators = []
    for index, path in enumerate(chunk_paths):
        handle = open(path, "r", encoding="utf-8")
        iterator = (json.loads(line) for line in handle)
        iterators.append((iterator, handle))
        try:
            record = next(iterator)
        except StopIteration:
            handle.close()
            continue
        heapq.heappush(heap, (float(record["captured_at"]), index, record))

    try:
        while heap:
            _, index, record = heapq.heappop(heap)
            yield record
            iterator = iterators[index][0]
            try:
                next_record = next(iterator)
            except StopIteration:
                iterators[index][1].close()
                continue
            heapq.heappush(heap, (float(next_record["captured_at"]), index, next_record))
    finally:
        for _, handle in iterators:
            handle.close()


def _capture_key_digest(record: dict) -> bytes:
    """Compact state/time key used to remove redundant capture records.

    Rogue capture processes had independent sequence counters, so sequence is
    not a reliable identity. Two records are redundant only when market, token,
    full ladder, and captured whole-second match. A changed level always survives,
    as does the same unchanged book observed in a later second.
    """
    payload = json.dumps(
        [record["market_id"], record["token_id"], int(record["captured_at"]),
         record["bids"], record["asks"]],
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.blake2b(payload, digest_size=16).digest()


def _normalize_levels(levels) -> list[list[float]]:
    """Normalize bid/ask levels to [[price, size], ...] format.

    Handles dict format ({'price': ..., 'size': ...}), list format ([price, size]),
    and tuple format. Results sorted: bids descending, asks ascending (caller decides order).
    """
    result: list[list[float]] = []
    if not levels:
        return result
    for level in levels:
        try:
            if isinstance(level, dict):
                price = float(level["price"])
                size = float(level.get("size", level.get("quantity", 0)))
            elif isinstance(level, (list, tuple)) and len(level) >= 2:
                price = float(level[0])
                size = float(level[1])
            else:
                continue
            if price > 0 and size >= 0:
                result.append([price, size])
        except (TypeError, ValueError, KeyError, IndexError):
            continue
    return result


def _floor_to_second(ts: float) -> int:
    """Floor a timestamp to the nearest whole second (int)."""
    return int(ts)


def _top3_key(bids: list, asks: list) -> tuple:
    """Extract top-3 bids/asks for deduplication (price levels only)."""
    bid_prices = tuple(b[0] for b in bids[:3]) if bids else ()
    ask_prices = tuple(a[0] for a in asks[:3]) if asks else ()
    return (bid_prices, ask_prices)


def _normalize_line(record: dict, source: str) -> dict | None:
    """Normalize a source record to the canonical schema.

    Source A detection: has 'captured_at' and 'source' == 'clob_ws'.
    Source B detection: has 'ts' and 'event' == 'book_update' (or 'wall_time').

    Returns the canonical record dict or None if the record is malformed.
    """
    # --- Source A: capture_depth_ws.py output ---
    if "captured_at" in record and record.get("source") == "clob_ws":
        try:
            bids = _normalize_levels(record.get("bids") or [])
            asks = _normalize_levels(record.get("asks") or [])
            bids.sort(key=lambda x: x[0], reverse=True)
            asks.sort(key=lambda x: x[0])
            return {
                "type": "depth_snapshot",
                "captured_at": float(record["captured_at"]),
                "captured_at_iso": record.get("captured_at_iso", ""),
                "market_id": record.get("market_id", ""),
                "token_id": str(record.get("token_id", "")),
                "sequence": int(record.get("sequence", 0)),
                "bids": bids,
                "asks": asks,
                "source": "clob_ws",
            }
        except (TypeError, ValueError, KeyError) as e:
            print(f"[consolidate_depth] Skip malformed Source A record: {e}",
                  file=sys.stderr, flush=True)
            return None

    # --- Source B: capture_live_depth.py output ---
    if "ts" in record and ("event" in record or "wall_time" in record):
        try:
            ts = float(record["ts"])
            bids = _normalize_levels(record.get("bids") or [])
            asks = _normalize_levels(record.get("asks") or [])
            bids.sort(key=lambda x: x[0], reverse=True)
            asks.sort(key=lambda x: x[0])
            iso_ts = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
            return {
                "type": "depth_snapshot",
                "captured_at": ts,
                "captured_at_iso": iso_ts,
                "market_id": record.get("market_id", ""),
                "token_id": str(record.get("token_id", "")),
                "sequence": int(record.get("update_id", record.get("sequence", 0))),
                "bids": bids,
                "asks": asks,
                "source": "live_depth_ws",
            }
        except (TypeError, ValueError, KeyError) as e:
            print(f"[consolidate_depth] Skip malformed Source B record: {e}",
                  file=sys.stderr, flush=True)
            return None

    print(f"[consolidate_depth] Unrecognized record format, skipping: {str(record)[:80]}",
          file=sys.stderr, flush=True)
    return None


def consolidate(source_a_path: Path = SOURCE_A_PATH,
                source_b_dir: Path = SOURCE_B_DIR,
                output_path: Path = OUTPUT_PATH) -> dict:
    """Stream both sources, deduplicate, and atomically rebuild the output.

    CRITICAL: This function NEVER modifies, moves, or deletes source files.
    It only reads the source snapshots and replaces output_path after a complete
    successful build. Re-running with unchanged inputs is idempotent.
    """
    tmp_output = output_path.with_suffix(output_path.suffix + ".tmp")
    source_files = _snapshot_source_files(
        source_a_path,
        source_b_dir,
        excluded_paths={output_path, tmp_output},
    )
    stats = {
        "source_a_records": 0,
        "source_b_records": 0,
        "skipped_malformed": 0,
    }
    normalized = 0
    seen: set[bytes] = set()
    duplicates = 0
    output_path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    try:
        with tempfile.TemporaryDirectory(
            prefix="depth-consolidate-", dir=output_path.parent,
        ) as temp_dir_name:
            chunk_paths = _write_sorted_chunks(
                source_files, stats, Path(temp_dir_name),
            )
            with open(tmp_output, "w", encoding="utf-8") as handle:
                for record in _iter_chronological_chunks(chunk_paths):
                    normalized += 1
                    digest = _capture_key_digest(record)
                    if digest in seen:
                        duplicates += 1
                        continue
                    seen.add(digest)
                    handle.write(json.dumps(record, separators=(",", ":")) + "\n")
                    written += 1
                handle.flush()
                os.fsync(handle.fileno())
        os.replace(tmp_output, output_path)
    finally:
        if tmp_output.exists():
            tmp_output.unlink()

    summary = {
        "source_a_path": str(source_a_path),
        "source_a_pattern": f"{source_a_path.stem}*.jsonl",
        "source_a_records": stats["source_a_records"],
        "source_b_dir": str(source_b_dir),
        "source_b_records": stats["source_b_records"],
        "total_raw": stats["source_a_records"] + stats["source_b_records"],
        "normalized": normalized,
        "skipped_malformed": stats["skipped_malformed"],
        "duplicates_removed": duplicates,
        "unique_written": written,
        "output_path": str(output_path),
        "source_files": [str(path) for path, _, _ in source_files],
        "write_mode": "atomic_replace",
        "sort_mode": "external_20000_record_chunks",
        "sort_chunks": len(chunk_paths),
        "dedup_policy": "market+token+captured_second+full_book",
        "output_bytes": output_path.stat().st_size,
    }
    print(f"[consolidate_depth] Done: {json.dumps(summary, indent=2)}",
          file=sys.stderr, flush=True)
    return summary


def main():
    parser = argparse.ArgumentParser(
        description="Consolidate depth-capture data from multiple sources into a canonical schema."
    )
    parser.add_argument("--source-a", type=str, default=str(SOURCE_A_PATH),
                        help="Path to Source A JSONL (capture_depth_ws.py output)")
    parser.add_argument("--source-b-dir", type=str, default=str(SOURCE_B_DIR),
                        help="Path to Source B directory tree (capture_live_depth.py output)")
    parser.add_argument("--output", type=str, default=str(OUTPUT_PATH),
                        help="Output consolidated JSONL path")
    parser.add_argument(
        "--report-output",
        type=Path,
        help="Optional machine-readable consolidation summary JSON",
    )
    args = parser.parse_args()

    summary = consolidate(
        source_a_path=Path(args.source_a),
        source_b_dir=Path(args.source_b_dir),
        output_path=Path(args.output),
    )
    if args.report_output:
        args.report_output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.report_output.with_suffix(args.report_output.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(summary, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(args.report_output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
