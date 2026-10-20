"""Resolve up/down token ids for every captured market via Gamma API."""
import glob, json, time, urllib.request, re
from pathlib import Path
import pandas as pd

OUT = Path(__file__).parent / "out" / "token_map.csv"
CACHE = Path("~/ccp/polymarket_arb/state/cache")

def main():
    pairs = set()
    for fp in glob.glob(str(CACHE / "polymarket_depth_capture*.jsonl")):
        with open(fp, "rb") as f:
            for line in f:
                m = re.search(rb'"market_id":"(btc-updown-5m-\d+)","token_id":"(\d+)"', line)
                if m:
                    pairs.add((m.group(1).decode(), m.group(2).decode()))
    print("unique (market, token) pairs:", len(pairs))
    markets = sorted({p[0] for p in pairs})
    rows = []
    done = set()
    if OUT.exists():
        old = pd.read_csv(OUT, dtype=str)
        done = set(old["mts"])
        rows = old.to_dict("records")
        print(f"resuming, {len(done)} already resolved")
    for i, mts in enumerate(markets):
        if mts in done:
            continue
        url = f"https://gamma-api.polymarket.com/markets?slug={mts}"
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                data = json.loads(r.read())
            rec = {"mts": mts}
            for mkt in data or []:
                toks = mkt.get("tokens") or []
                for t in toks:
                    out = str(t.get("outcome", "")).lower()
                    if out in ("up", "down"):
                        rec[out] = str(t.get("token_id"))
                rec["closed"] = mkt.get("closed")
                rec["outcome_prices"] = mkt.get("outcomePrices")
            rows.append(rec)
        except Exception as e:
            print("fail", mts, e, flush=True)
        if len(rows) % 200 == 0:
            pd.DataFrame(rows).to_csv(OUT, index=False)
        time.sleep(0.05)
    pd.DataFrame(rows).to_csv(OUT, index=False)
    print("wrote", OUT, len(rows))

if __name__ == "__main__":
    main()
