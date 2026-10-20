# BTC Up/Down edge search — 2026-08-27

## Conclusion

No statistically defensible, economically tradable Polymarket edge is demonstrated by the supplied data. A Binance/OHLCV signal has a small positive point estimate on exact historical Polymarket labels, but its 95% clustered net-EV interval includes losses at a 50¢ taker ask. More importantly, the only fresh period with real CLOB asks (200 5-minute windows, 2026-08-25 09:05–2026-08-27 00:05 UTC) is negative under an optimistic immediate-fill quote backtest at every Brownian fair-value threshold tested. There are no historical CLOB asks/depth/fills for the 15m, hourly, or 4h holdout period.

## Data and settlement

The Binance parquet contains 845,763 clean 1-minute rows from 2025-01-01 through 2026-08-11 08:03 UTC, with one missing minute (2026-06-11 06:04 UTC). Terminal Polymarket metadata yielded 104,916 rows; after excluding one ambiguous hourly row and collapsing 14 duplicate hourly listings with identical labels, 104,901 decisive outcomes remained. A random CLOB-terminal check found zero label mismatches in 152 historical samples (five-minute endpoint returned eight retired-market 404s); all 479 current OOS five-minute labels checked against CLOB terminal winners matched.

The settlement regime is not stationary: Polymarket switched 15m/4h crypto markets to Chainlink 60-second TWAP and 5m markets to 30-second TWAP on 2026-08-07, then switched 5m to 60-second TWAP on 2026-08-14. The long historical holdout therefore spans multiple rules, and the supplied Binance data has no labels after 2026-08-11—there is no historical sample of the current 5m 60-second regime in the OHLCV holdout.

## Search and validation

The locked library has 195 configurations per horizon:

- return momentum/reversal and autocorrelation (1–240 minute lookbacks),
- volume-weighted signed-body and CLV flow proxies,
- candle shape, wick, CLV, and price-z reversal/continuation,
- volatility-regime interactions,
- large-single-candle reactions with high-volume gating,
- hour/minute/day-of-week seasonal rules,
- shallow histogram-gradient-boosted trees,
- expanding-majority and no-trade baselines.

Features are sampled strictly from the last completed Binance candle before each market start. Four anchored walk-forward tests are generated per product after a 30-day warm-up and end at 2026-05-01. The selected rule is frozen, the code/data/outcome-cache hashes are locked, and the final period is opened once. The artifacts are [the study script](./btc_polymarket_edge_study.py), [development results](./btc_polymarket_edge_out_v4/development_results.json), [the lock](./btc_polymarket_edge_out_v4/locked_configs.json), and [holdout results](./btc_polymarket_edge_out_v4/holdout_results.json).

## Walk-forward selections and exact-label holdout

Net EV below includes the current crypto taker fee `0.07 × p × (1−p)` and assumes a 50¢ ask. The interval is a day-cluster bootstrap interval for accuracy, converted to net EV.

| Product | Frozen signal | Dev trades / accuracy / EV-trade | Holdout trades / accuracy | Point net EV/share | One-sided 95% lower net EV/share | Block-permutation p (Holm) |
|---|---|---:|---:|---:|---:|---:|
| 5m | 3-minute momentum reversal, trade only top 20% absolute scores | 6,467 / 54.03% / +2.28¢ | 4,570 / 52.89% | +1.14¢ | −0.09¢ | 0.00090 |
| 15m | 60-minute price-z reversal, trade only top 20% absolute scores | 3,325 / 56.96% / +5.21¢ | 1,958 / 53.01% | +1.26¢ | −0.67¢ | 0.01140 |
| 1h | 5-minute EWM reversal, top 20% absolute scores | 2,015 / 55.19% / +3.44¢ | 383 / 52.22% | +0.47¢ | −4.06¢ | 0.19198 |
| 4h | no-trade gate (best active rule's dev lower bound was negative) | — | — | — | — | — |

The permutation test keeps the frozen decisions fixed and shuffles contiguous seven-day outcome blocks. It is a test of the already-selected rule, not a claim that the 195-rule development search had zero selection risk; the untouched holdout is what protects against that search bias.

At the point estimate, the fee-only maximum ask is 51.14¢ (5m) and 51.26¢ (15m). At the one-sided lower accuracy bound it falls to 49.91¢ and 49.33¢ respectively. Thus the apparent 50¢ edge is not lower-confidence positive.

The OHLCV endpoint-proxy audit also matters: exact Polymarket labels disagree with the primary Binance endpoint proxy on 9.50% of 5m events, 3.84% of 15m events, and 1.14% of 4h events. The exact labels were used for the reported model results; the proxies are not silently treated as settlement truth.

## Fee-adjusted hypothetical ask frontier

These are mathematical frontiers, not observed historical fills. At 50¢, the fee is 1.75¢/share and break-even accuracy is 51.75%.

| Signal | Ask | Fee | Point EV/share | Lower EV/share |
|---|---:|---:|---:|---:|
| 5m reversal | 45¢ | 1.73¢ | +6.16¢ | +4.93¢ |
| 5m reversal | 47.5¢ | 1.75¢ | +3.64¢ | +2.42¢ |
| 5m reversal | 50¢ | 1.75¢ | +1.14¢ | −0.09¢ |
| 15m price-z reversal | 45¢ | 1.73¢ | +6.28¢ | +4.35¢ |
| 15m price-z reversal | 47.5¢ | 1.75¢ | +3.77¢ | +1.84¢ |
| 15m price-z reversal | 50¢ | 1.75¢ | +1.26¢ | −0.67¢ |
| 1h EWM reversal | 45¢ | 1.73¢ | +5.49¢ | +0.96¢ |
| 1h EWM reversal | 50¢ | 1.75¢ | +0.47¢ | −4.06¢ |

Because no historical CLOB quotes exist for those holdout events, assigning a fixed 45–47.5¢ ask is counterfactual. It cannot establish an executable edge.

## Current-regime real-CLOB test

The [exact-label CLOB backtest](./clob_exact_oos_backtest.py) uses captured best asks, authoritative token mapping, terminal Polymarket labels, and the current fee curve. It assumes an immediate fill at the displayed best ask, which is optimistic; it does not claim fills. The fresh period has 200 windows and 16,464 paired book rows, all 200 windows label-matched. Every Brownian fair-value threshold was negative:

| Fair-value threshold | Trades | Hit rate | Average ask | Net EV/share | Net PnL at 75-share cap |
|---:|---:|---:|---:|---:|---:|
| 0.01 | 248 | 40.32% | 40.47¢ | −1.53¢ | −$165.67 |
| 0.02 | 241 | 39.42% | 39.43¢ | −1.41¢ | −$200.58 |
| 0.03 | 237 | 39.66% | 39.53¢ | −1.28¢ | −$66.33 |
| 0.05 | 227 | 37.89% | 38.64¢ | −2.18¢ | −$200.74 |
| 0.08 | 215 | 37.67% | 38.01¢ | −1.76¢ | −$317.24 |
| 0.12 | 190 | 36.32% | 36.38¢ | −1.48¢ | −$191.67 |

This test is short (roughly three days), so it is not proof that every future period will lose. It is nevertheless the only current-regime check with real venue prices, and it fails even before modeling queue position, latency, partial fills, or adverse selection.

The older maker simulation is not a survivor either: it used an idealized cross-through fill rule on roughly 13 days, with filled accuracy around 50.4–51.7% and apparent +1.4–2.7¢/share at zero maker fee. There are no confirmed fills, it is too short, and the prior fresh OOS fixed-rule check was negative (Edge A −$48/day; Edge D −$57 to −$107/day; E0 −$36 to −$75/day). Treat it as a hypothesis for a prospective paper-fill study, not a tradable result.

## Decision

`NO TRADE`. The historical OHLCV study finds small statistical directional effects, but none clears a lower-confidence fee-adjusted edge at a plausible near-mid taker price, and the only current-period real-CLOB quote test is negative across thresholds. A future go/no-go test would need at least several weeks of current-regime 5m/15m/hourly/4h CLOB depth plus confirmed fills, with a pre-registered signal and a genuinely untouched final period.

Primary mechanics references: [Polymarket trading fees](https://docs.polymarket.com/trading/fees), [Predictions Changelog](https://docs.polymarket.com/changelog/predictions), and [Chainlink TWAP prices](https://docs.polymarket.com/market-data/chainlink-twap).
