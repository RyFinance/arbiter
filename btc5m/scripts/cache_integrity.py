#!/usr/bin/env python3
"""Cache-integrity checker — emits one JSON report covering every cached source.

Scans ``state/cache/`` (the new LSE vault cache) plus the long-standing project
state artifacts that the dashboard's data-coverage panel needs to reason about
(``state/btc_1m_candles_lse_v3.parquet``, ``state/market_cache.json``,
``state/macro_events.json``, ``state/alerts.jsonl``).

For each artifact the report records:
  - ``exists``          : bool
  - ``path``            : absolute path on this machine
  - ``size_bytes``      : on-disk size
  - ``sha256``          : content hash (when present)
  - ``row_count``       : row count for tabular artifacts (parquet/csv/jsonl)
  - ``date_range``      : {start, end} for time-series artifacts, when known
  - ``age_s``           : age since last write in seconds (file mtime)
  - ``fetched_at_utc``  : recorded fetch timestamp (when a ``*_meta.json`` sidecar exists)
  - ``completeness``    : subjective verdict from gap/coverage heuristics
  - ``gap_report``      : {gaps_over_cadence, max_gap_s} for candles (from sidecar)
  - ``note``            : any anomaly explanation (orphan meta, empty file, etc.)

Output goes to stdout AND ``state/cache/integrity_report.json`` so OpenCode's
dashboard can read the file directly. Designed to be re-run cheaply (only
recomputes SHA-256 of small JSON artifacts; large parquet/CSV hashes are re-read
from their sidecar meta to avoid a multi-second rehash on every dashboard poll).

USAGE:
    python scripts/cache_integrity.py            # full report (stdout + file)
    python scripts/cache_integrity.py --quiet    # write file only
    python scripts/cache_integrity.py --rehash   # force SHA-256 recompute of large files
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

CACHE_DIR = PROJECT_ROOT / "state" / "cache"
STATE_DIR = PROJECT_ROOT / "state"
OUT_FILE = CACHE_DIR / "integrity_report.json"

UTC = timezone.utc

# Tabular artifact registry. Each entry: (relative_path, kind, date_columns...)
#   kind in {"parquet","csv","jsonl","json","meta"}
ARTIFACTS: list[tuple[str, str, tuple[str, ...]]] = [
    # New vault cache (state/cache/)
    ("state/cache/btc_usd_1m_candles.parquet", "parquet", ("timestamp",)),
    ("state/cache/btc_usd_1m_candles_meta.json", "meta", ()),
    ("state/cache/economic_calendar.csv", "csv", ("date",)),
    ("state/cache/economic_calendar_meta.json", "meta", ()),
    # Long-standing state cache that the dashboard also surfaces
    ("state/btc_1m_candles_lse_v3.parquet", "parquet", ("timestamp",)),
    ("state/market_cache.json", "json", ()),
    ("state/macro_events.json", "json", ()),
    ("state/alerts.jsonl", "jsonl", ()),
]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _row_count_jsonl(path: Path) -> int:
    n = 0
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for _ in f:
            n += 1
    return n


# --------------------------------------------------------------------------
# Full-state scan (TASK 2 — every file under state/ and state/cache/).
# --------------------------------------------------------------------------
# Files / dirs that are engine-internal noise to the inventory — they are NOT
# produced by a script-style producer and have no auditable schema, so we fold
# them under verdict buckets instead of emitting one row per leaf.
#
# Verdict = FRESH | STALE | EMPTY | UNAUDITABLE | MISSING-PRODUCER
# The verdict is the verdict LINE the dashboard needs to act on; details stay
# dashboard-consumable JSON. Stale = mtime age > STALE_AGE_S OR an internal
# timestamp inside the artifact disagrees with file mtime (the very class the
# prior audit caught on market_cache.json, claiming generated_at 2026-07-10
# against a file rewritten during a live session).
STALE_AGE_S = 7 * 86400.0   # 7 days — older than this is STALE unless the
                             # artifact is a frozen reference dataset (flagged).

# Literal producer map — each entry is a list of repo-relative paths (relative
# to PROJECT_ROOT) that open or write the artifact. Used to detect orphan state
# files that no code path in this repo ever (re)creates (the "abandoned
# experiments" pile — those keep a file but lose their producer).
_PRODUCER_INDEX: dict[str, list[str]] | None = None


def _build_producer_index() -> dict[str, list[str]]:
    """Grep the repo once for any file that names a ``state/...`` string literal.

    Returns a mapping ``state_relative_path -> [producer.py, ...]`` so we can
    adjudicate MISSING-PRODUCER without guessing. Skips binary/__pycache__. The
    index is built once per process and cached as a module global."""
    global _PRODUCER_INDEX
    if _PRODUCER_INDEX is not None:
        return _PRODUCER_INDEX
    index: dict[str, set[str]] = {}
    scan_dirs = [
        PROJECT_ROOT / "polymarket_arb",
        PROJECT_ROOT / "scripts",
        PROJECT_ROOT / "scrapers",
        PROJECT_ROOT / "agent",
        PROJECT_ROOT / "tests",
    ]
    src_exts = (".py", ".md")
    for base in scan_dirs:
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix not in src_exts:
                continue
            if "__pycache__" in path.parts:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            rel = str(path.relative_to(PROJECT_ROOT))
            # find every "state/..." literal we reference
            import re as _re
            for m in _re.finditer(r"(state/[A-Za-z0-9_.\-/]+)", text):
                lit = m.group(1).rstrip("',\"();\n\t ")
                # normalise trailing slashes/quoting noise
                lit = lit.split()[0] if lit else lit
                if not lit.startswith("state/"):
                    continue
                index.setdefault(lit, set()).add(rel)
    _PRODUCER_INDEX = {k: sorted(v) for k, v in index.items()}
    return _PRODUCER_INDEX


# Static reference datasets — one-time fixtures that are EXPECTED to be STALE
# because they are not live caches. We surface them as FRESH-as-reference under
# a separate "reference_dataset" flag so they don't trip a cross-the-board STALE.
REFERENCE_DATASETS = frozenset([
    "state/btc_ticks_jul26-28.parquet",
    "state/btc_ticks_jul29.parquet",
    "state/btc_ticks_jul30-31.parquet",
    "state/btc_ticks_real.parquet",
    "state/btc_1m_candles.parquet",
    "state/btc_1m_candles_lse.parquet",
    "state/btc_1m_candles_lse_v3.parquet",
    "state/kronos_volatility_classifier.pth",
    "state/lightgbm_scaler.pkl",
    "state/lightgbm_scaler_v2.pkl",
])

# Internal timestamp fields whose presence inside a JSON/JSONL artifact lets us
# cross-check the file mtime vs the artifact's own "I was written at" claim.
# (the prior-audit failure mode: file rewritten live while claiming a stale ts).
INTERNAL_TS_FIELDS = ("generated_at", "fetched_at_utc", "last_update",
                      "ts", "t", "timestamp", "written_at")


def _kind_of(name: str) -> str:
    """Coarse-kind classifier for the full-scan path."""
    low = name.lower()
    for ext, kind in ((".parquet", "parquet"), (".csv", "csv"),
                      (".jsonl", "jsonl"), (".json", "json"),
                      (".meta.json", "meta"), (".log", "log"),
                      (".pkl", "pkl"), (".pth", "pth"),
                      (".txt", "txt"), (".md", "md")):
        if low.endswith(ext):
            return kind
    return "other"


def _empty_size(kind: str) -> int:
    """Size below which we call an artifact EMPTY rather than POPULATED."""
    if kind in ("jsonl", "log", "csv"):
        return 1   # zero bytes = empty log/jsonl/csv
    if kind in ("json", "meta"):
        return 3   # "{}\n" or "[]\n" — degenerate content
    return 0


def _parquet_schema(path: Path) -> dict:
    """Return {columns, has_timestamp, row_count} — minimal sanity probe.

    ``has_timestamp`` flags unauditable-the-bug-class: btc_1m_candles_lse.parquet
    was reported in the task brief as having NO timestamp column, which makes it
    impossible to reason about freshness against the candle stream. Flag and
    surface, do not silently drop the file.
    """
    try:
        df = pd.read_parquet(path, columns=None)
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}", "columns": [],
                "has_timestamp": False, "row_count": 0}
    cols = list(df.columns)
    has_ts = any(c.lower() in ("timestamp", "ts", "datetime", "open_time",
                              "start", "date") for c in cols)
    return {"columns": cols, "has_timestamp": bool(has_ts),
            "row_count": int(len(df))}


def _json_internal_ts(path: Path) -> tuple[Optional[str], Optional[str]]:
    """Best-effort read of an INTERNAL timestamp (generated_at, last_update, ...)
    plus the field name we found it under. Returns (value_iso_or_None, key_or_None).

    For JSON: introspect top-level keys + nested Meta JSON known shapes. For
    JSONL: scan first/last record's INTERNAL_TS_FIELDS."""
    try:
        kind = _kind_of(path.name)
        if kind == "meta":
            import json as _json
            data = _json.loads(path.read_text())
            for k in ("generated_at", "fetched_at_utc"):
                if k in data:
                    return str(data[k]), k
        elif kind == "json":
            import json as _json
            data = _json.loads(path.read_text())
            for k in INTERNAL_TS_FIELDS:
                if isinstance(data, dict) and k in data and data[k]:
                    return str(data[k]), k
        elif kind == "jsonl":
            # read the LAST line — the most recent write is what we want to
            # compare against mtime. If the live session keeps appending, the
            # last ts and the mtime must agree (the bug class the prior audit
            # could not catch).
            last_ts: Optional[str] = None
            last_key: Optional[str] = None
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    for k in ("t", "timestamp", "ts"):
                        v = row.get(k)
                        if v:
                            last_ts = str(v)
                            last_key = k
                            break
            return last_ts, last_key
    except Exception:  # noqa: BLE001
        return None, None
    return None, None


def _scan_one(path: Path) -> dict:
    """Emit the verdict-bearing row for ONE file under state/."""
    rel = str(path.relative_to(PROJECT_ROOT))
    st = path.stat()
    kind = _kind_of(path.name)
    age_s = time.time() - st.st_mtime
    mtime_iso = datetime.fromtimestamp(st.st_mtime, UTC).isoformat(timespec="seconds")
    row: dict[str, object] = {
        "artifact": rel,
        "kind": kind,
        "path": str(path),
        "exists": True,
        "size_bytes": int(st.st_size),
        "mtime_utc": mtime_iso,
        "age_s": round(age_s, 1),
    }
    # internal-timestamp vs mtime mismatch detection
    internal_ts, internal_key = _json_internal_ts(path)
    row["internal_ts_field"] = internal_key
    row["internal_ts_value"] = internal_ts
    if internal_ts:
        try:
            ts_dt = None
            # Reject bare time-of-day values (e.g. bot.jsonl's "t":"HH:MM:SS.fff"
            # in local-TZ) — these don't encode a date, so an internal-vs-mtime
            # comparison is meaningless and would only produce false alarms.
            if not any(c.isdigit() for c in str(internal_ts)[:1]):
                pass
            elif isinstance(internal_ts, str) and "T" not in str(internal_ts) and \
                    str(internal_ts).count(":") in (2,) and \
                    not any(ch.isdigit() and ch for ch in str(internal_ts)[:4] if False):
                # Heuristic: bare HH:MM:SS(.fff) — no date, no epoch, skip.
                _bare_time = (":" in str(internal_ts) and
                               "-" not in str(internal_ts)[:11] and
                               not str(internal_ts).strip().replace(".", "")
                                .replace(":", "").isdigit())
                if _bare_time:
                    raise ValueError("bare time-of-day, no date")
            if ts_dt is None:
                try:
                    ts_dt = pd.to_datetime(internal_ts, utc=True, errors="raise")
                except (ValueError, TypeError):
                    ts_dt = pd.to_datetime(float(internal_ts), unit="s", utc=True,
                                           errors="raise")
            ts_naive = ts_dt.tz_convert(UTC).replace(tzinfo=None)
            mtime_dt = datetime.fromtimestamp(st.st_mtime, UTC).replace(tzinfo=None)
            delta_s = (ts_naive - mtime_dt).total_seconds()
            row["internal_vs_mtime_s"] = round(delta_s, 1)
        except Exception:  # noqa: BLE001
            row["internal_vs_mtime_s"] = None
    # empty detection
    if st.st_size == 0 or (st.st_size <= _empty_size(kind)):
        row["verdict"] = "EMPTY"
        row["note"] = ("0 bytes" if st.st_size == 0
                       else f"degenerate content ({st.st_size} bytes)")
        return row
    # schema sanity (parquet): probe for a timestamp column
    if kind == "parquet":
        schema = _parquet_schema(path)
        row["columns"] = schema["columns"]
        row["has_timestamp"] = schema["has_timestamp"]
        row["row_count"] = schema["row_count"]
        if "error" in schema:
            row["note"] = f"parquet unreadable: {schema['error']}"
            row["verdict"] = "UNAUDITABLE"
            return row
        if not schema["has_timestamp"]:
            row["verdict"] = "UNAUDITABLE"
            row["note"] = "parquet has no timestamp column — freshness unauditable"
            return row
    elif kind == "jsonl":
        row["row_count"] = _row_count_jsonl(path)
    elif kind in ("json", "meta"):
        try:
            data = json.loads(path.read_text())
            if isinstance(data, list):
                row["row_count"] = len(data)
            elif isinstance(data, dict):
                row["row_count"] = sum(1 for _ in data)
            else:
                row["row_count"] = 0
        except Exception as e:  # noqa: BLE001
            row["note"] = f"malformed JSON: {e}"
            row["verdict"] = "UNAUDITABLE"
            return row
    elif kind == "csv":
        try:
            df = pd.read_csv(path, low_memory=False)
            row["row_count"] = int(len(df))
            row["columns"] = list(df.columns)
        except Exception as e:  # noqa: BLE001
            row["note"] = f"csv unreadable: {e}"
            row["verdict"] = "UNAUDITABLE"
            return row
    # producer adjudication
    producers = _build_producer_index().get(rel, [])
    row["producers"] = producers
    if not producers:
        row["verdict"] = "MISSING-PRODUCER"
        row["note"] = "no repo script/code path references this state path"
        # Stay on MISSING-PRODUCER even when reference; the human dashboard
        # triages which are intentional references vs abandoned experiments.
        if rel in REFERENCE_DATASETS:
            row["note"] = ("reference dataset (no live producer) — "
                           "intentionally frozen, MISSING-PRODUCER is informational")
        return row
    # stale verdict (uses internal_ts vs mtime, then file age)
    delta = row.get("internal_vs_mtime_s")
    if delta is not None and abs(delta) > 60.0:
        # internal ts differs from mtime by more than 1 minute — the very class
        # the prior audit caught on market_cache.json.
        row["verdict"] = "STALE"
        row["note"] = (f"internal_ts ({internal_key}={internal_ts}) disagrees "
                       f"with file mtime by {delta:.0f}s")
        return row
    is_reference = rel in REFERENCE_DATASETS
    if not is_reference and age_s > STALE_AGE_S:
        row["verdict"] = "STALE"
        row["note"] = f"age {age_s:.0f}s exceeds {STALE_AGE_S:.0f}s threshold"
        return row
    row["verdict"] = "FRESH"
    row["note"] = ""
    return row


def scan_full_state(*, include_kronos_cache: bool = False,
                    include_session_archives: bool = True) -> dict:
    """Full-scan mode: verb ένα per file under state/ and state/cache/.
    Traverses recursively. By default folds the HuggingFace internal *.lock /
    *.metadata cache files (under state/kronos/.../.cache/) into a single
    per-parent 'UNAUDITABLE/internal-cache' pseudo-verdict; surfaces the live
    HF model files (README.md, config.json, model.safetensors) as real rows.

    Returns the dashboard-consumable summary, including the verdict buckets and
    one ``artifacts`` entry per file (excluding the HF internals suppressed
    above)."""
    artefacts: list[dict] = []
    folded_internals: list[dict] = []
    for path in sorted(STATE_DIR.rglob("*")):
        if not path.is_file():
            continue
        # Skip integrity_report.json's own previous write so the report doesn't
        # claim its own artifact as STALE right after we write it.
        rel = str(path.relative_to(PROJECT_ROOT))
        if rel == "state/cache/integrity_report.json":
            continue
        # Folding rule for HF model cache internals (.lock / .metadata /
        # .gitignore / .gitattributes) — they're unstructured companion files
        # the HF hub writes blindly and don't inform freshness.
        if ("/.cache/huggingface/" in rel
                and path.suffix in (".lock", ".metadata", ".gitignore",
                                    ".gitattributes")):
            st = path.stat()
            folded_internals.append({
                "artifact": rel,
                "kind": "hf-cache-internal",
                "size_bytes": int(st.st_size),
                "mtime_utc": datetime.fromtimestamp(st.st_mtime, UTC)
                                  .isoformat(timespec="seconds"),
                "verdict": "UNAUDITABLE",
                "note": "HuggingFace cache internal — folded, not a real cache",
            })
            continue
        if not include_session_archives and "paper_session_archives/" in rel:
            continue
        artefacts.append(_scan_one(path))
    artefacts.extend(folded_internals)
    # verdict rollup
    verdict_counts: dict[str, int] = {}
    for r in artefacts:
        verdict_counts[r["verdict"]] = verdict_counts.get(r["verdict"], 0) + 1
    summary = {
        "generated_at_utc": _now_iso(),
        "generated_at_epoch": time.time(),
        "project_root": str(PROJECT_ROOT),
        "scan_mode": "full_state",
        "artifact_count": len(artefacts),
        "verdict_counts": verdict_counts,
        "missing_producer": [a["artifact"] for a in artefacts
                              if a["verdict"] == "MISSING-PRODUCER"],
        "empty": [a["artifact"] for a in artefacts if a["verdict"] == "EMPTY"],
        "stale": [a["artifact"] for a in artefacts if a["verdict"] == "STALE"],
        "unauditable": [a["artifact"] for a in artefacts
                         if a["verdict"] == "UNAUDITABLE"],
        "fresh": [a["artifact"] for a in artefacts if a["verdict"] == "FRESH"],
        "artifacts": artefacts,
    }
    return summary





def _date_range_jsonl(path: Path, ts_key: str) -> dict:
    """Best-effort first/last timestamp from a JSONL log."""
    first = last = None
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # bot.jsonl events store time in 't'; alerts.jsonl in 'timestamp'
                ts = row.get(ts_key) or row.get("t") or row.get("timestamp")
                if ts:
                    if first is None:
                        first = ts
                    last = ts
    except FileNotFoundError:
        return {}
    return {"start": first, "end": last} if first else {}


def _completeness_candles(rows: int, gaps: int, max_gap_s: float) -> str:
    """Heuristic verdict on candle-cache completeness."""
    if rows == 0:
        return "EMPTY"
    # Coverage ratio: if gaps are small relative to total rows, call it COMPLETE;
    # any large gap > ~1 day flags GAPS_PRESENT.
    if max_gap_s and max_gap_s > 86400:
        return "GAPS_PRESENT"
    if gaps == 0:
        return "COMPLETE"
    return "MINOR_GAPS"


def _completeness_calendar(rows: int, date_start: str, date_end: str) -> str:
    """Economic-calendar coverage versus the known 2015-01 → 2026-11 span."""
    if rows == 0:
        return "EMPTY"
    expected_start = "2015-01-01"
    expected_end = "2026-11-10"
    ok_start = date_start <= expected_start
    ok_end = date_end >= expected_end
    return "COMPLETE" if (ok_start and ok_end) else "PARTIAL"


def _load_sidecar(parquet_path: Path) -> dict:
    """Read the matching ``*_meta.json`` sidecar for a parquet/csv artifact."""
    sidecar = parquet_path.parent / (parquet_path.stem + "_meta.json")
    if not sidecar.exists():
        return {}
    try:
        return json.loads(sidecar.read_text())
    except json.JSONDecodeError:
        return {"note": f"sidecar {sidecar.name} is malformed JSON"}


def inspect_artifact(rel: str, kind: str, date_cols: tuple[str, ...], rehash: bool) -> dict:
    path = PROJECT_ROOT / rel
    info: dict[str, Any] = {
        "artifact": rel,
        "kind": kind,
        "path": str(path),
        "exists": path.exists(),
    }
    if not path.exists():
        info["completeness"] = "MISSING"
        return info

    st = path.stat()
    info["size_bytes"] = int(st.st_size)
    info["age_s"] = round(time.time() - st.st_mtime, 1)
    info["mtime_utc"] = datetime.fromtimestamp(st.st_mtime, UTC).isoformat(timespec="seconds")

    if kind == "meta":
        # Sidecar JSON — read through, surface key fields and SHA-256 (small).
        info["sha256"] = _sha256_file(path)
        try:
            data = json.loads(path.read_text())
            info["identifies"] = data.get("artifact")
            info["fetched_at_utc"] = data.get("fetched_at_utc")
            info["row_count"] = data.get("row_count")
            info["sha256_of_referenced"] = data.get("sha256")
            info["note"] = (
                "orphan meta: referenced artifact missing"
                if data.get("artifact")
                and not (path.parent / data["artifact"]).exists()
                else ""
            )
        except json.JSONDecodeError:
            info["note"] = "malformed JSON sidecar"
        return info

    if kind == "json":
        info["sha256"] = _sha256_file(path)
        try:
            data = json.loads(path.read_text())
            # market_cache.json -> {generated_at, source, markets: [...]}; macro_events.json -> {...events}
            if "markets" in data and isinstance(data["markets"], list):
                info["row_count"] = len(data["markets"])
                info["source"] = data.get("source")
                info["generated_at"] = data.get("generated_at")
            elif "events" in data and isinstance(data["events"], list):
                info["row_count"] = len(data["events"])
            else:
                info["row_count"] = len(data) if isinstance(data, (list, dict)) else 0
        except json.JSONDecodeError:
            info["note"] = "malformed JSON"
        return info

    if kind == "jsonl":
        info["row_count"] = _row_count_jsonl(path)
        info["date_range"] = _date_range_jsonl(path, "t")
        info["completeness"] = "POPULATED" if info["row_count"] > 0 else "EMPTY"
        return info

    if kind == "parquet":
        sidecar = _load_sidecar(path)
        info["sha256"] = sidecar.get("sha256") if not rehash else _sha256_file(path)
        info["fetched_at_utc"] = sidecar.get("fetched_at_utc")
        try:
            cols = date_cols or ("timestamp",)
            df = pd.read_parquet(path, columns=list(cols))
            info["row_count"] = int(len(df))
            ts = pd.to_datetime(df[cols[0]], utc=True, errors="coerce").dropna()
            if not ts.empty:
                info["date_range"] = {
                    "start": ts.min().isoformat(),
                    "end": ts.max().isoformat(),
                }
                if cols[0] == "timestamp":
                    gaps = int(ts.diff().dropna().gt(pd.Timedelta(minutes=1)).sum())
                    max_gap_s = float(ts.diff().dropna().max().total_seconds()) if len(ts) > 1 else 0.0
                    info["gap_report"] = {
                        "gaps_over_1min": gaps,
                        "max_gap_s": max_gap_s,
                    }
                    info["completeness"] = _completeness_candles(len(ts), gaps, max_gap_s)
                else:
                    info["completeness"] = "POPULATED"
            else:
                info["completeness"] = "EMPTY"
        except Exception as e:  # noqa: BLE001
            info["note"] = f"read error: {type(e).__name__}: {e}"
            info["completeness"] = "UNREADABLE"
        return info

    if kind == "csv":
        sidecar = _load_sidecar(path)
        info["sha256"] = sidecar.get("sha256") if not rehash else _sha256_file(path)
        info["fetched_at_utc"] = sidecar.get("fetched_at_utc")
        try:
            df = pd.read_csv(path, low_memory=False)
            info["row_count"] = int(len(df))
            if date_cols and date_cols[0] in df.columns:
                dates = pd.to_datetime(df[date_cols[0]], errors="coerce").dropna()
                if not dates.empty:
                    info["date_range"] = {
                        "start": dates.min().date().isoformat(),
                        "end": dates.max().date().isoformat(),
                    }
                    info["completeness"] = _completeness_calendar(
                        len(dates), dates.min().date().isoformat(), dates.max().date().isoformat()
                    )
                else:
                    info["completeness"] = "EMPTY"
            else:
                info["completeness"] = "POPULATED"
        except Exception as e:  # noqa: BLE001
            info["note"] = f"read error: {type(e).__name__}: {e}"
            info["completeness"] = "UNREADABLE"
        return info

    return info


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--quiet", action="store_true", help="write file only, no stdout")
    ap.add_argument("--rehash", action="store_true", help="force SHA-256 recompute of large files")
    ap.add_argument(
        "--full", action="store_true",
        help="full-state scan: emit a verdict line for EVERY file under state/ "
             "and state/cache/ (the original 8-artifact scan is preserved as the "
             "default; --full is the dashboard-inventory mode)",
    )
    args = ap.parse_args(argv)

    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    if args.full:
        summary = scan_full_state()
        OUT_FILE.write_text(json.dumps(summary, indent=2, default=str))
        if not args.quiet:
            print(json.dumps(summary, indent=2, default=str))
            print(f"\n[wrote] {OUT_FILE}", file=sys.stderr)
        return 0

    artifacts = [inspect_artifact(rel, kind, dc, args.rehash) for rel, kind, dc in ARTIFACTS]

    summary = {
        "generated_at_utc": _now_iso(),
        "generated_at_epoch": time.time(),
        "project_root": str(PROJECT_ROOT),
        "artifact_count": len(artifacts),
        "missing": [a["artifact"] for a in artifacts if not a["exists"]],
        "complete": [a["artifact"] for a in artifacts if a.get("completeness") in {"COMPLETE", "POPULATED"}],
        "issues": [
            {"artifact": a["artifact"], "completeness": a.get("completeness"), "note": a.get("note", "")}
            for a in artifacts
            if a.get("completeness") not in {"COMPLETE", "POPULATED"} or a.get("note")
        ],
        "artifacts": artifacts,
    }

    OUT_FILE.write_text(json.dumps(summary, indent=2, default=str))
    if not args.quiet:
        print(json.dumps(summary, indent=2, default=str))
        print(f"\n[wrote] {OUT_FILE}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
