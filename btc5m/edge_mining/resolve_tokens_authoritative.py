"""Rebuild the up/down token map from Polymarket's own API for ANY snapshot file.

WHY THIS EXISTS: edge_mining/out/token_map.csv (built by resolve_tokens.py) has up and
down SWAPPED on roughly 30% of windows -- verified by sampling 30 windows against the
Gamma API, where 9 mismatched and every mismatch was a clean inversion (csv_up ==
auth_down). resolve_tokens.py reads a `tokens` array with `outcome`/`token_id` fields
that the Gamma /markets endpoint does not actually return in that shape, so its mapping
is unreliable.

A 30% inversion rate silently corrupts anything that maps prices to outcomes:
every backtest in edge_mining, and the adverse-selection test built on top of it.

This reads `outcomes` (e.g. ["Up","Down"]) positionally against `clobTokenIds`, which
is the documented Gamma contract and matches what the live API returns.

Usage: python3 edge_mining/resolve_tokens_authoritative.py <snapshots.parquet> <out.csv>
"""
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

import pandas as pd

GAMMA = "https://gamma-api.polymarket.com/events"


def fetch(slug: str):
    url = GAMMA + "?" + urllib.parse.urlencode({"slug": slug})
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (research script)"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def main():
    snap_path = sys.argv[1] if len(sys.argv) > 1 else "edge_mining/out/snapshots.parquet"
    out_path = Path(sys.argv[2] if len(sys.argv) > 2 else "edge_mining/out/token_map_authoritative.csv")

    windows = sorted(pd.read_parquet(snap_path, columns=["mts"])["mts"].unique())
    print(f"{snap_path}: {len(windows)} windows -> {out_path}")

    rows, done = [], set()
    if out_path.exists():
        old = pd.read_csv(out_path, dtype={"up": str, "down": str})
        rows = old.to_dict("records")
        done = set(old["mts"].astype(int))
        print(f"  resuming, {len(done)} already resolved")

    fails = 0
    for i, mts in enumerate(windows):
        if int(mts) in done:
            continue
        try:
            m = fetch(f"btc-updown-5m-{mts}")[0]["markets"][0]
            outs = json.loads(m["outcomes"])
            ids = json.loads(m["clobTokenIds"])
            mp = {str(o).lower(): str(t) for o, t in zip(outs, ids)}
            if "up" in mp and "down" in mp:
                rows.append({"mts": int(mts), "up": mp["up"], "down": mp["down"]})
            else:
                fails += 1
        except Exception as e:
            fails += 1
            if fails <= 3:
                print(f"  fail {mts}: {e!r}")
        if (i + 1) % 100 == 0:
            pd.DataFrame(rows).to_csv(out_path, index=False)
            print(f"  {i+1}/{len(windows)} ok={len(rows)} fails={fails}", flush=True)
        time.sleep(0.06)

    pd.DataFrame(rows).to_csv(out_path, index=False)
    print(f"wrote {out_path}: {len(rows)} windows, {fails} failures")


if __name__ == "__main__":
    main()
