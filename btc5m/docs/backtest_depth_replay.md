# Backtest Depth Replay

`BacktestConfig.fill_model` has two explicit modes:

- `top_of_book` is the legacy comparison model. It uses `yes_size` or `no_size`
  at the current best ask and is not a depth reconstruction.
- `depth_snapshot` is a strict cumulative buy-limit replay. Each tick must contain
  `yes_asks` and `no_asks`, each a list of `{ "price": number, "size": number }`
  levels (two-item arrays are also accepted). It consumes only levels whose price
  is at or below the submitted limit and records the fill ratio and VWAP.

Missing, malformed, or partial ladders block the full replay with
`DepthSnapshotUnavailable`. The implementation never turns a retained best ask
and its size into an invented one-level depth ladder.

## Available Data

The retained `state/btc_ticks_*.parquet` files contain LSE BTC/USD spot fields
`ts`, `symbol`, `price`, `volume`, `side`, and `ask`; they have no Polymarket
order-book levels. `state/clob_feed.jsonl` has 1,190 records with only
`best_ask`, `best_bid`, and one `size` value per `BTC-YES`/`BTC-NO` observation.
It has no price ladders and no market identifier, so it cannot be joined to the
historical validation window or replayed as cumulative depth.

Consequently, depth validation remains blocked until a timestamped, market- and
token-keyed Polymarket snapshot capture stores both ask ladders. The legacy run
is retained only to quantify the model gap; it is not depth-validated.

The legacy validator also uses a five-share liquidity cap against ten-share BUY
orders. Its resolved-pair count is therefore not a requested-quantity fill rate;
the accompanying validation artifact reports both values explicitly.

## Task D: Polymarket Depth Feasibility

The official CLOB surface does not provide a retroactive L2-depth history. The
[order-book endpoint](https://docs.polymarket.com/api-reference/market-data/get-order-book)
returns a book summary for one token at request time, while the official
[market-data overview](https://docs.polymarket.com/market-data/overview) lists
`GET /prices-history` as historical *price* data but lists no historical-book or
historical-depth endpoint. The official
[market WebSocket channel](https://docs.polymarket.com/market-data/websocket/market-channel)
is a live public L2 stream at
`wss://ws-subscriptions-clob.polymarket.com/ws/market`: it emits a `book` snapshot
when first subscribed and on book-affecting trades, plus `price_change` deltas.
Accordingly, historical depth cannot be reconstructed from the official API after
the fact; a depth replay needs forward capture of those snapshots and deltas.

The forward-capture record for every subscribed token must include receipt UTC time,
exchange timestamp/hash when supplied, condition ID, token ID, the full bid/ask
ladders, and every delta in order. Retain the initial `book` snapshot before applying
any `price_change`; any reconnect or sequence gap invalidates that interval rather
than filling it from top-of-book data.

The minimum operational feasibility sample is **30 independently resolved five-minute
market windows**, with both YES and NO ladders continuously reconstructable from the
initial snapshot through resolution. At one five-minute market window per interval,
that is **150 minutes (2.5 hours)** of uninterrupted capture. This is a deliberately
small feasibility threshold, not a go-live performance claim: if all 30 windows meet
the completeness criterion, the one-sided 95% Wilson lower confidence bound for the
true complete-window rate is 91.7%, exceeding the 90% threshold. Any incomplete
window is a failure; collect additional complete windows until the same bound exceeds
90%, and do not call the top-of-book replay depth-validated.

The current historical report remains blocked: 0 of 500,000 replay ticks include
both complete ask ladders. This task adds no trading, production, cross-venue, or
Kronos changes.

## 2026-08-15 captured-depth addendum

The preceding `0/500,000` statement is historical. Forward capture has now
produced enough real L2 rows to execute the strict model.

The sealed consolidation contains `698,862` rows
(`1,094,328,910` bytes; SHA-256
`410bfb9c3e83a391b63d073a93fc604a1df6876aaf7b446671bcc236fbc7d6a7`).
Causal same-market pairing yields `672,906` complete ticks, so the
`500,000` row gate passes. The operational gate still fails: `0/30` windows
have both tokens from at most `5s` after open through at least `295s` after
open with no gap over `60s`.

The corrected strict replay produced `27,987` opportunities, `8,186` leg
orders, `76` clean pairs, `510` partials, `428` resolved partials,
`24,097` misses, and `68` halted windows. Requested-quantity fill rate was
`0.9830264837337552` (`29,597.620000000003 / 30,108.67` BUY shares).
Its `47.19710000000002` simulated session PnL is diagnostic only.

Current verdict: **verified-fail**, not blocked for row count. The remaining
external/data gap is exactly `30` complete operational windows. See the
[dated replay report](depth_replay_2026-08-15.md) and
[machine artifact](../state/chunk_b_depth_replay_validation_2026-08-15.json).
