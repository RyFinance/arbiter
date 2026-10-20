"""Fetch Binance BTCUSDT klines (1m + 1s) covering the depth-capture window."""
import sys, time, urllib.request, json
from pathlib import Path
import pandas as pd

OUT = Path(__file__).parent / "out"
OUT.mkdir(exist_ok=True)
START_MS = 1786516800000  # 2026-08-11T00:00:00Z (a bit before first capture)
END_MS = 1787668800000    # 2026-08-25 end of day UTC


def fetch_klines(interval: str) -> pd.DataFrame:
    rows = []
    cur = START_MS
    while cur < END_MS:
        url = (f"https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval={interval}"
               f"&startTime={cur}&limit=1000")
        for attempt in range(5):
            try:
                with urllib.request.urlopen(url, timeout=15) as r:
                    batch = json.loads(r.read())
                break
            except Exception as e:
                if attempt == 4:
                    raise
                time.sleep(2 ** attempt)
        if not batch:
            break
        rows.extend(batch)
        cur = batch[-1][0] + (60000 if interval == "1m" else 1000)
        time.sleep(0.06)
    df = pd.DataFrame(rows, columns=["open_ms", "open", "high", "low", "close",
                                     "volume", "close_ms", "qv", "n_trades",
                                     "tbv", "tqv", "ignore"])
    df = df.astype({"open": float, "high": float, "low": float, "close": float,
                    "volume": float})
    df["ts"] = pd.to_datetime(df["open_ms"], unit="ms", utc=True)
    return df[["ts", "open", "high", "low", "close", "volume"]]


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    if which in ("1m", "all"):
        p = OUT / "btc_1m_aug12_25.parquet"
        if not p.exists():
            fetch_klines("1m").to_parquet(p)
            print("wrote", p)
    if which in ("1s", "all"):
        p = OUT / "btc_1s_aug12_25.parquet"
        if not p.exists():
            fetch_klines("1s").to_parquet(p)
            print("wrote", p)
