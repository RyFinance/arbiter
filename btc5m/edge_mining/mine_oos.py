"""Mine ONLY the genuinely fresh capture period never used by any prior
discovery/filter-selection pass: 2026-08-25T09:00:00Z onward. This is a
true out-of-sample test set for edge A / edge D / E0 baseline -- those
filters were all chosen by exploring 2026-08-12 -> 2026-08-25T08:07 data
only (verified: out/snapshots.parquet's max timestamp)."""
import json, glob
from pathlib import Path
from datetime import datetime, timezone
import pandas as pd

try:
    import orjson as _oj
    def loads(b): return _oj.loads(b)
except ImportError:
    def loads(b): return json.loads(b)

CACHE = Path("~/polymarket_arb/state/cache")
OUT = Path(__file__).parent / "out" / "snapshots_oos.parquet"
CUTOFF = datetime(2026, 8, 25, 9, 0, 0, tzinfo=timezone.utc).timestamp()


def main():
    files = sorted(glob.glob(str(CACHE / "polymarket_depth_capture.2026-08-2[5-9]*.jsonl")))
    print("files:", files)
    rows = []
    append = rows.append
    n = 0
    n_skipped_old = 0
    for fp in files:
        with open(fp, "rb") as f:
            for line in f:
                try:
                    d = loads(line)
                    if d["captured_at"] < CUTOFF:
                        n_skipped_old += 1
                        continue
                    bids, asks = d["bids"], d["asks"]
                    if not bids or not asks:
                        continue
                    bb, bq = bids[0]
                    ba, qa = asks[0]
                    b5 = sum(s for _, s in bids[:5])
                    a5 = sum(s for _, s in asks[:5])
                    append((d["captured_at"], int(d["market_id"].rsplit("-", 1)[1]),
                            d["token_id"], float(bb), float(bq), float(ba), float(qa),
                            b5 - a5, b5 + a5))
                    n += 1
                except Exception:
                    continue
        print(f"done {Path(fp).name} cumulative_rows={n} skipped_pre_cutoff={n_skipped_old}", flush=True)
    df = pd.DataFrame(rows, columns=["ts", "mts", "token", "bb", "bq", "ba", "qa",
                                     "imb5", "depth5"])
    df.to_parquet(OUT)
    print("wrote", OUT, len(df), "windows:", df.mts.nunique())


if __name__ == "__main__":
    main()
