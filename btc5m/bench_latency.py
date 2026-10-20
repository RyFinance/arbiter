"""Dev benchmark (NOT shipped): measure hot-path software latency, INCLUDING a forced
arb to capture real tick->submit timing.

Wires the real PriceFeed -> arb_engine -> execution on the live WS feed, and ALSO
force-injects a below-threshold book on one tracked market to measure the full
decision->dual-submit latency. DRY_RUN: no real orders sent.
"""
from __future__ import annotations
import asyncio, json, statistics, time
from polymarket_arb.arb_engine import ArbEngine
from polymarket_arb.execution import Execution
from polymarket_arb.metrics import Metrics
from polymarket_arb.clients.trading_client import TradingClient
from polymarket_arb.risk import RiskState
from polymarket_arb.price_feed import PriceFeed
from polymarket_arb.cache import load


class _BenchClient(TradingClient):
    def __init__(self):
        self.s = __import__("polymarket_arb.config", fromlist=["SETTINGS"]).SETTINGS
        self.sent_ts = []
    async def submit_order(self, token_id, price, size):
        self.sent_ts.append(time.monotonic())
        return f"bench-{token_id[-6:]}"


async def main():
    cached = load()
    states = {m.market_id: m.to_state() for m in cached.markets}
    print(f"tracking {len(states)} markets on live WS")

    metrics = Metrics()
    risk = RiskState(balance=1_000_000.0)
    client = _BenchClient()
    exec_ = Execution(client, risk, metrics)

    latencies = []
    orig_fire = exec_.execute_dual_buy
    async def timed_fire(st, combined, decision_ts, **kwargs):
        await orig_fire(st, combined, decision_ts, **kwargs)
        if client.sent_ts:
            latencies.append((client.sent_ts[-1] - decision_ts) * 1000.0)
    exec_.execute_dual_buy = timed_fire

    for st in states.values():
        exec_.register(st)
    arb = ArbEngine(on_fire=exec_.execute_dual_buy, metrics=metrics)
    feed = PriceFeed(states, on_update=arb.evaluate)
    feed.build_token_index()

    await feed.start()
    # Force ONE below-threshold arb on the first tracked market to capture live timing.
    target = next(iter(states.values()))
    await asyncio.sleep(1.0)
    feed._on_ws_tick(target.yes_token_id, 0.45, 500.0)
    feed._on_ws_tick(target.no_token_id, 0.50, 500.0)
    await asyncio.sleep(0.5)

    await asyncio.sleep(8)
    feed._stop = True
    if feed._feed: feed._feed.stop()
    if feed._session: await feed._session.aclose()

    print(f"live ticks (loops): {metrics.loops} | opportunities: {metrics.opportunities} | fills: {metrics.fills}")
    if latencies:
        print(f"HOT-PATH latency tick->1st-submit (ms): n={len(latencies)} "
              f"min={min(latencies):.4f} median={statistics.median(latencies):.4f} "
              f"max={max(latencies):.4f}")
    else:
        print("no fire captured")


if __name__ == "__main__":
    asyncio.run(main())
