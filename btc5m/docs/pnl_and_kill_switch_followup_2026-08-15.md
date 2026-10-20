# PnL convergence and kill-switch follow-up — 2026-08-15

## Verdict

- The historical `$1.8000 actual / $2.5000 expected = 72.00%` claim was an
  accounting error. The seven submitted pairs had `$1.8000` of decision-time
  expected PnL and `$1.8000` of paper PnL. The three rejected signals were not
  committed executions.
- Those seven fills used `decision_snapshot` prices, so their apparent `100.00%`
  convergence was mechanical and did not validate fill-time slippage.
- A later 15-minute `rest_book` run observed two committed opportunities. Both
  became one-leg fills and protective exits: `$0.5000` expected at detection
  versus `-$0.5000` realized, or `-100.00%` convergence and `-200.00%`
  deviation. This is a real failure for that sample, but `n=2` is too thin to
  estimate a stable long-run rate.
- The final clean rerun completed `900.306` connected seconds and observed zero
  opportunities. It therefore has no PnL-convergence denominator and is not a
  pass or a fail on PnL.
- Kill-switch and alerting verification passed in the final clean rerun.

## Where `$2.5000` came from

The old dashboard combined two unlike quantities:

```text
$1.8000 locked PnL from seven submitted pairs
+$0.7000 historical would_lock on three pre-submit stale-quote rejections
=$2.5000
```

The historical `would_lock` field was itself threshold surplus,
`(0.99 - combined) * 10`, rather than gross binary-arbitrage PnL. Recomputing
the rejected signals with `(1.00 - combined) * 10` gives `$1.0000`, so the
all-detected gross total is `$2.8000`, not `$2.5000`. Neither number is a fair
execution-convergence denominator because the three stale quotes were rejected
before order submission.

The corrected denominator is:

```text
expected_committed_pnl
  = sum((1.00 - decision_combined) * realized_size)
    over clean fills, resolved partials, and submitted no-fill outcomes
```

Realized committed PnL now includes clean two-leg PnL plus hedge/protective-exit
PnL. Pre-submit rejections remain visible only in raw opportunity accounting.

## Seven-fill forensic breakdown

All seven fills below used the exact decision snapshot as the simulated fill
price; they do not contain an independent slippage observation.

| Filled at | YES / NO decision and fill | Combined | Expected at detection | Realized | Delta |
| --- | ---: | ---: | ---: | ---: | ---: |
| 08:43:19.848 | 0.60 / 0.38 | 0.98 | `$0.2000` | `$0.2000` | `$0.0000` |
| 08:44:19.738 | 0.88 / 0.08 | 0.96 | `$0.4000` | `$0.4000` | `$0.0000` |
| 08:48:53.371 | 0.82 / 0.15 | 0.97 | `$0.3000` | `$0.3000` | `$0.0000` |
| 08:49:07.468 | 0.50 / 0.48 | 0.98 | `$0.2000` | `$0.2000` | `$0.0000` |
| 08:49:39.040 | 0.14 / 0.83 | 0.97 | `$0.3000` | `$0.3000` | `$0.0000` |
| 08:49:52.226 | 0.03 / 0.95 | 0.98 | `$0.2000` | `$0.2000` | `$0.0000` |
| 08:52:45.379 | 0.34 / 0.64 | 0.98 | `$0.2000` | `$0.2000` | `$0.0000` |
| **Total** |  |  | **`$1.8000`** | **`$1.8000`** | **`$0.0000`** |

Evidence: [sealed seven-fill log](../state/paper_session_archives/task_2_4_20260811T154243Z/bot.jsonl) and
[corrected calculator](../scripts/calc_pnl_convergence.py).

## REST-book execution evidence

The first corrected REST-book run completed the market-observation interval but
failed its later kill-switch harness probe because shutdown released all market
states before the probe selected one. It is sealed as a failed kill-switch run;
the already-recorded trade outcomes remain direct paper-execution evidence.

| Event time | Decision YES / NO | Expected edge | Actual REST result | Realized | Delta |
| --- | ---: | ---: | --- | ---: | ---: |
| 02:03:36.987 | 0.17 / 0.80 | `$0.3000` | YES bought at 0.14; NO best ask moved to 0.87 and did not fill; YES exited at 0.13 | `-$0.1000` | `-$0.4000` |
| 02:03:38.450 | 0.12 / 0.86 | `$0.2000` | YES bought at 0.12; NO best ask moved to 0.89 and did not fill; YES exited at 0.08 | `-$0.4000` | `-$0.6000` |
| **Total** |  | **`$0.5000`** | 2 partials, 2 protective exits | **`-$0.5000`** | **`-$1.0000`** |

Exact result: `-$0.5000 / $0.5000 = -100.00%` convergence; deviation from
the decision-time target is `-200.00%`. This is consistent with rapid
cross-leg price movement between detection and REST-book execution, not with
the old formula's zero-slippage assumption. With only two events, it does not
establish whether the loss rate is systematic; it does establish that
decision-snapshot fills were unrealistic.

The run recorded `3717` loops, `2` opportunities, `4` leg submissions, `0`
clean pair fills, `2` partial fills, `2` resolved exits, `-$0.5000` session
PnL, opportunity-to-first-order average `4.34 ms`, p50 `1.82 ms`, p95/p99
`6.86 ms`, and average acknowledgement `210.43 ms`. Evidence:
[sealed failed run](../state/paper_session_archives/task_2_4_20260815T090124Z/session_report.json).

The implementation now records decision edge, actual entry source/price,
actual hedge or exit price, expected PnL, realized PnL, and delta for both clean
and partial outcomes. The hedge calculation also uses the recorded filled-leg
price rather than a later live ask.

## Clean kill-switch verification

The replacement archive is a completed, checksum-sealed paper-only run:

- Connected observation: `900.306` seconds; total duration `901.055` seconds.
- Loops: `2124`; opportunities/orders/fills/partials/misses: `0/0/0/0/0`.
- Window rollovers: three completed; stale-token errors: `0`.
- WebSocket recovery records: `4`, each logged as `0.7s`; severe disconnect
  alerts: `0`.
- Alerting enabled: `true`; session kill-switch records: `1`; local webhook
  deliveries: `1`; append-only operational alert rows after the run: `3`.
- `RiskState.halt()` set `halted=true`.
- The post-halt production reserve gate returned exactly
  `halted/emergency-stop`.
- Orders before and after the post-halt probe: `0 -> 0`;
  `post_halt_probe_blocked=true`.
- Real exchange orders: `0`; paper fill source: `rest_book`; geoblock bypass:
  none; cross-venue components: not started.

Evidence: [clean session report](../state/paper_session_archives/task_2_4_20260815T092241Z/session_report.json),
[session alert](../state/paper_session_archives/task_2_4_20260815T092241Z/alerts.jsonl), and
[archive checksums](../state/paper_session_archives/task_2_4_20260815T092241Z/SHA256SUMS).

The final clean run's event-level report correctly records
`sample_available=false`; zero opportunities are not reported as `0%`
convergence or as a pass.
