"""Resolve AUTHORITATIVE up/down token ids for the OOS windows from Polymarket's Gamma API.

Needed because model_vs_market_oos.py's inferred token map was circular: it identified
the winning token by which price converged to ~1.0, then labeled it by the TWAP outcome,
which by construction makes the market's late price perfectly predict the label. Only
the venue's own token->outcome mapping is safe for a model-vs-market comparison.
"""
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

import pandas as pd

OUTD = Path(__file__).parent / "out"
OUT = OUTD / "token_map_oos.csv"
GAMMA = "https://gamma-api.polymarket.com/events"


def fetch(slug: str):
    url = GAMMA + "?" + urllib.parse.urlencode({"slug": slug})
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (research script)"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def main():
    snaps = pd.read_parquet(OUTD / "snapshots_oos.parquet", columns=["mts"])
    windows = sorted(snaps["mts"].unique())
    print(f"resolving {len(windows)} OOS windows from Gamma...")

    done = {}
    if OUT.exists():
        old = pd.read_csv(OUT, dtype={"up": str, "down": str})
        done = {int(r.mts): r for r in old.itertuples()}
        print(f"  resuming, {len(done)} already resolved")

    rows = [{"mts": k, "up": v.up, "down": v.down} for k, v in done.items()]
    fails = 0
    for i, mts in enumerate(windows):
        if mts in done:
            continue
        slug = f"btc-updown-5m-{mts}"
        try:
            data = fetch(slug)
            mkt = data[0]["markets"][0]
            outcomes = json.loads(mkt["outcomes"])          # e.g. ["Up", "Down"]
            ids = json.loads(mkt["clobTokenIds"])           # aligned with outcomes
            m = {str(o).lower(): str(t) for o, t in zip(outcomes, ids)}
            if "up" in m and "down" in m:
                rows.append({"mts": mts, "up": m["up"], "down": m["down"]})
            else:
                fails += 1
        except Exception as e:
            fails += 1
            if fails <= 5:
                print(f"  fail {slug}: {e!r}")
        if (i + 1) % 25 == 0:
            pd.DataFrame(rows).to_csv(OUT, index=False)
            print(f"  {i+1}/{len(windows)} resolved={len(rows)} fails={fails}", flush=True)
        time.sleep(0.08)

    pd.DataFrame(rows).to_csv(OUT, index=False)
    print(f"wrote {OUT}: {len(rows)} windows mapped, {fails} failures")


if __name__ == "__main__":
    main()
