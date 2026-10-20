# Real captured-depth replay — 2026-08-15

## Verdict

**Verified-fail.** There is now enough retained real Polymarket L2 volume to
run the strict depth model: the row gate passes with `672,906` causally paired
ticks against a `500,000` requirement. The operational gate does not pass:
`0/30` five-minute windows satisfy the documented continuity criterion, and
the replay halted `68` windows while managing partial exposure.

The replay completed and produced useful diagnostics, but its positive
simulated PnL is not go-live evidence. It is a deterministic passive-fill
simulation over an incomplete capture, not exchange-confirmed execution.

## Immutable input

The external-sort consolidation consumed a fixed snapshot of `1,317,980`
JSONL rows:

| Measure | Exact value |
| --- | ---: |
| Malformed rows | 0 |
| Redundant rows removed | 619,118 |
| Consolidated rows | 698,862 |
| Consolidated bytes | 1,094,328,910 |
| Markets | 249 |
| Markets with two observed tokens | 249 |
| Chronology violations | 0 |
| SHA-256 | `410bfb9c3e83a391b63d073a93fc604a1df6876aaf7b446671bcc236fbc7d6a7` |

The capture process continued writing a separate live shard after this
snapshot. Every replay cited here uses the immutable consolidated checksum
above.

## Causal pairing and coverage

Token orientation is symmetric for a dual buy, so the first-observed token is
assigned YES. Pairing uses the latest observation available at each event and
never looks forward. Maximum allowed leg skew is `500.0 ms`.

| Pairing outcome | Exact value |
| --- | ---: |
| Pair candidates | 694,944 |
| Complete paired ticks | 672,906 |
| Complete ticks / consolidated rows | 96.2859620354233% |
| Skew rejected | 12,800 |
| Missing or invalid ask ladder | 9,238 |
| Awaiting other token at end | 254 |
| Outside market-window rows | 3,664 |

The row-count gate is `672,906 / 500,000`, status `pass`, gap `0`.

The operational criterion is: for both tokens, first retained snapshot at most
`5s` after window open, last at least `295s` after open, and no inter-snapshot
gap greater than `60s`. Result: `0/30` complete windows, status `fail`, gap
`30`.

## Final strict replay

Settings were `fill_model=depth_snapshot`, `min_edge=0.01`,
`order_size=10.0`, `max_quote_age_ms=500`, `no_entry_seconds=5.0`,
`max_position_per_market=200.0`, and `max_total_capital=1000.0`. Historical
point-in-time macro state was unavailable, so the replay explicitly used a
neutral macro assumption; it did not pretend to reconstruct historical macro
gates.

| Measure | Exact value |
| --- | ---: |
| Replayed paired ticks / loops | 672,906 |
| Replayed markets | 238 |
| Opportunities | 27,987 |
| Leg orders sent | 8,186 |
| Clean pair fills | 76 |
| Partial fills | 510 |
| Partials resolved | 428 |
| Missed | 24,097 |
| Pending after drain | 0 |
| Halted windows | 68 |
| Locked PnL | 42.089900000000014 |
| Residual PnL | 5.107200000000016 |
| Session PnL | 47.19710000000002 |
| Would-lock diagnostic | 19,743.9 |

Miss reasons were `18,208` `halted/emergency-stop`, `5,167` max-position,
`266` low-NO-liquidity, `253` low-YES-liquidity, `201` no-fill timeout, and
`2` no-entry-zone.

Critical-path counts were `510` partial-fill-management events, `60` halts
after an unconfirmed partial exit, `8` halts after an incompletely filled
partial hedge, `71` rejected exit submissions, and `3` exits with no bid.
There were `0` uncaught replay errors.

## Quantity accounting

The replay records actual ladder consumption rather than treating a resolved
pair as a full requested fill:

| Measure | Exact value |
| --- | ---: |
| BUY orders measured | 3,014 |
| Requested BUY quantity | 30,108.67 |
| Simulated filled BUY quantity | 29,597.620000000003 |
| Requested-quantity fill rate | 0.9830264837337552 |
| Sub-full BUY orders | 110 |

That rate is **simulation only**. It does not satisfy a live two-leg fill gate.

## Bug exposed and corrected

The first strict replay completed `4,400` orders, `37` clean fills, `295`
partials, `101` resolved partials, and `138` halted windows, with
`17.289900000000006` locked and `70.95029999999998` residual PnL. Inspection
showed that recovery SELL orders incorrectly consumed ask ladders. A sell must
consume bids.

`BacktestClient` now retains bids and asks separately and
`cumulative_sell_fill` consumes only eligible bid levels. After this fix the
replay produced the final numbers above. A second unchanged run reproduced
every headline metric exactly, establishing deterministic behavior for the
sealed input.

The first attempt is preserved at
[`state/chunk_b_depth_replay_validation_2026-08-15_attempt1.json`](../state/chunk_b_depth_replay_validation_2026-08-15_attempt1.json);
the post-fix attempt is
[`state/chunk_b_depth_replay_validation_2026-08-15_attempt2_sell_fix.json`](../state/chunk_b_depth_replay_validation_2026-08-15_attempt2_sell_fix.json).

## Evidence

- [Final machine-readable replay](../state/chunk_b_depth_replay_validation_2026-08-15.json)
- [Replay driver](../scripts/replay_captured_depth.py)
- [Depth fill engine](../polymarket_arb/backtest_engine.py)
- [Replay tests](../tests/test_replay_captured_depth.py)
- [Consolidator](../scripts/consolidate_depth.py)

No real order, Kalshi/cross-venue component, model artifact, risk threshold, or
signing path was used or changed.
