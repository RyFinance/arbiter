"""Stream all depth captures -> compact parquet of per-snapshot book features."""
import json, glob, sys
from pathlib import Path
import pandas as pd

try:
    import orjson as _oj
    def loads(b): return _oj.loads(b)
except ImportError:
    def loads(b): return json.loads(b)

CACHE = Path("~/ccp/polymarket_arb/state/cache")
OUT = Path(__file__).parent / "out" / "snapshots.parquet"

def main():
    files = sorted(glob.glob(str(CACHE / "polymarket_depth_capture*.jsonl")))
    rows = []
    append = rows.append
    n = 0
    for fp in files:
        with open(fp, "rb") as f:
            for line in f:
                try:
                    d = loads(line)
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
        print(f"done {Path(fp).name} cumulative_rows={n}", flush=True)
    df = pd.DataFrame(rows, columns=["ts", "mts", "token", "bb", "bq", "ba", "qa",
                                     "imb5", "depth5"])
    df.to_parquet(OUT)
    print("wrote", OUT, len(df))

if __name__ == "__main__":
    main()
