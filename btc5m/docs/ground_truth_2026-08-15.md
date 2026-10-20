# Ground truth — 2026-08-15

Observed through `2026-08-15T10:27:07Z`. The machine-readable record is
[`state/ground_truth_2026-08-15.json`](../state/ground_truth_2026-08-15.json).
All market interaction in this work was `PAPER_TRADE`; no real order was
submitted and no geoblock was bypassed.

## Capture ownership

**Verified-pass.** Exactly one capture process was present: PID `28436`, PPID
`1`, owned by launchd label `com.rytty.depth_capture`. `launchctl` reported
`state=running`, `runs=1`, and no prior exit. The process held
`state/cache/capture_depth_ws.lock`; a duplicate start exits `75`. The
`BOOTSTRAP_ONLY` path does not enter the fallback loop when launchd owns the
service.

Evidence: [singleton implementation](../scripts/capture_depth_ws.py),
[supervisor](../scripts/supervise_depth_capture.sh),
[deployed plist source](../ops/launchd/com.rytty.depth_capture.plist), and
[supervisor tests](../tests/test_supervise_bootstrap.py).

## Immutable depth snapshot and replay

The final atomic consolidation snapshot contained `1,317,980` input rows. It
found `0` malformed rows, removed `619,118` redundant rows under
`market+token+captured_second+full_book`, and wrote `698,862` rows /
`1,094,328,910` bytes. SHA-256:
`410bfb9c3e83a391b63d073a93fc604a1df6876aaf7b446671bcc236fbc7d6a7`.
The capture process continued after this point; the replay intentionally uses
this immutable point-in-time file.

The strict causal pairing produced `672,906` paired ticks, or
`96.2859620354233%` of consolidated rows. The `500,000`-tick volume gate
passes with a gap of `0`, but the operational continuity gate fails:
`0/30` complete five-minute windows, gap `30`.

The deterministic passive replay produced `27,987` opportunities, `8,186`
leg orders, `76` clean pairs, `510` partials, `428` resolved partials,
`24,097` misses, and `68` halted windows. It requested `30,108.67` BUY
shares and simulated `29,597.620000000003`, a
`0.9830264837337552` requested-quantity fill rate. Simulated PnL was
`42.089900000000014` locked plus `5.107200000000016` residual =
`47.19710000000002`. These are replay diagnostics, not executable or
exchange-confirmed PnL.

Verdict: **verified-fail** because continuity is `0/30` and `68` windows
halted. Evidence: [depth replay report](depth_replay_2026-08-15.md) and
[machine artifact](../state/chunk_b_depth_replay_validation_2026-08-15.json).

## Live paper session, alerting, and kill switch

The sealed clean session
`task_2_4_20260815T092241Z` completed `901.055` seconds total and
`900.306` connected seconds. It ran `2,124` loops, completed three market
rollovers, recorded `0` stale-token errors, four `0.7s` WebSocket recovery
messages, and `0` severe disconnect alerts. It saw `0` opportunities, orders,
fills, partials, or misses, so PnL convergence is correctly unavailable rather
than a pass.

Alerting was explicitly enabled. The deliberate production kill path produced
one durable session alert and one loopback webhook delivery. The post-halt
reserve probe returned exactly `halted/emergency-stop`; orders stayed `0 -> 0`.
Evidence: [sealed session report](../state/paper_session_archives/task_2_4_20260815T092241Z/session_report.json)
and [archive checksums](../state/paper_session_archives/task_2_4_20260815T092241Z/SHA256SUMS).

The operational [alert log](../state/alerts.jsonl) now has exactly `3` rows /
`654` bytes, all genuine paper-harness kill-switch records. Two synthetic
macro-state rows created by an older stress fixture were moved, not discarded,
to [the test-pollution archive](../state/alerts_test_pollution_archive_2026-08-15.jsonl)
(`2` rows / `807` bytes). The stress fixture now supplies an explicit fresh
neutral macro state and no longer writes those rows to the operational sink.

## Backtest integrity

The regenerated [validation artifact](../state/backtest_validation_report.json)
now states:

- `validation_status = NOT VALIDATED / NON-COMPARABLE`
- `data_source = lse_tick_export_converted`
- `comparable_to_outcome_books = false`
- `go_live_pnl_metric = null`
- `comparison_permitted = false`
- `validated_against_real_data = false`

Its legacy `391` structural opportunities and `34.487500000000004` simulated
locked PnL are no longer compared with PAPER_TRADE or shown as executable PnL.
The candle fallback also fails loudly instead of fabricating
`no_ask = 1 - close`.

## Final regression stability

Three unchanged full-suite runs are stable:

| Run | Passed | Failed | Errors | Skipped | Warnings | JUnit time |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| [1](../state/pytest_final1_2026-08-15.xml) | 581 | 0 | 0 | 0 | 6 | 93.183 s |
| [2](../state/pytest_final2_2026-08-15.xml) | 581 | 0 | 0 | 0 | 6 | 92.915 s |
| [3](../state/pytest_final3_2026-08-15.xml) | 581 | 0 | 0 | 0 | 6 | 93.001 s |

The six warnings are the same NumPy correlation/polyfit conditioning warnings
in offline BTC feature tests. No torch segmentation fault occurred. The
dashboard-focused suite passed `29` tests and Streamlit's app harness reported
`0` exceptions.
