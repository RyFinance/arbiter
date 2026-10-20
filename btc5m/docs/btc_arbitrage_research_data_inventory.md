# BTC/arbitrage offline research inventory

Observed from the retained repository parquet files on 2026-08-11 UTC. No rows
were fabricated, padded, or resampled.

| Source | Rows | Actual span (UTC) | Nominal/observed frequency | Missingness / caveat |
|---|---:|---|---|---|
| `state/btc_1m_candles.parquet` | 4,263 | 2026-07-26 00:00 through 2026-07-30 14:56 | 1 minute; median gap 60 s | 0 null rows; 2 gaps >60 s |
| `state/btc_1m_candles_lse.parquet` | 5,000 | not auditable: no timestamp column | nominal 1 minute by filename | 0 null rows; timestamp/span missing |
| `state/btc_1m_candles_lse_v3.parquet` | 139,523 | 2026-05-01 00:00 through 2026-08-05 21:23 | 1 minute; median gap 60 s | 0 null rows; 1 gap >60 s |
| `state/btc_ticks_jul26-28.parquet` | 2,500,000 | 2026-07-26 00:00:00.114 through 2026-07-27 13:46:13.472 | sub-second ticks; median rounded gap 0 s | 0 null rows; 1.5-day file span |
| `state/btc_ticks_jul29.parquet` | 2,500,000 | 2026-07-29 00:00:00.092 through 2026-07-29 18:18:20.123 | sub-second ticks; median rounded gap 0 s | 0 null rows; partial day |
| `state/btc_ticks_jul30-31.parquet` | 2,500,000 | 2026-07-30 00:00:00.179 through 2026-07-30 14:56:27.454 | sub-second ticks; median rounded gap 0 s | 0 null rows; partial day |
| `state/btc_ticks_real.parquet` | 7,500,000 | 2026-07-26 00:00:00.114 through 2026-07-30 14:56:27.454 | sub-second ticks; median rounded gap 0 s | 0 null rows; 2 gaps >60 s; concatenated coverage |

The tick files contain `price`, `ask`, `volume`, `side`, and symbol, but not a
paired Polymarket YES/NO executable book, fills, fee realization, or
detection-to-submit latency. Therefore exact hypothetical Polymarket
arbitrage profitability is not feasible from retained data.

## Primary research label

`label_hypothetical_profitability()` in
`scripts/diagnose_lightgbm_macro_regimes.py` is the defensible alternative:

`forward_net_return_bps(t,K) = 10,000 * sum(log(close[t+i] / close[t+i-1]), i=1..K) - round_trip_cost_bps`

The default explicit cost assumption is 20 bps. `profitability_target` is 1
iff that quantity is positive. Features are computed from observations at or
before cutoff `t`; the label uses only `t+1` through `t+K`. Rows without a
contiguous one-minute future window are excluded. This is a BTC directional
execution-cost proxy, not venue-arbitrage PnL, and must not be used as a live
execution signal without venue quotes and fill data.
