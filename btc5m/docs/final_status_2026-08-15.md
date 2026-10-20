# Final project status — 2026-08-15

> Superseded by the machine-generated
> [authoritative product status](product_status_2026-08-15.md). This file remains
> unchanged below this notice as historical handoff evidence.

## Bottom line

**Do not enable live trading.** The kill switch, alert durability, rollover,
capture ownership, backtest fail-closed behavior, dashboard evidence, and test
suite now pass their narrow safety criteria. The trading evidence does not:
fill fidelity, quote freshness, PnL convergence, latency, BTC-model economics,
and depth continuity remain failed or unproven. Jurisdiction and wallet
readiness remain externally blocked.

All sessions in this work were `PAPER_TRADE`. Real orders submitted: `0`.

## PnL convergence resolution

The claimed `$1.80 actual / $2.50 expected` comparison mixed submitted PnL
with `$0.70` of pre-submit stale-quote threshold surplus. The seven submitted
snapshot pairs were:

| Pair | YES / NO | Expected at detection | Realized paper PnL | Delta |
| ---: | ---: | ---: | ---: | ---: |
| 1 | 0.60 / 0.38 | $0.2000 | $0.2000 | $0.0000 |
| 2 | 0.88 / 0.08 | $0.4000 | $0.4000 | $0.0000 |
| 3 | 0.82 / 0.15 | $0.3000 | $0.3000 | $0.0000 |
| 4 | 0.50 / 0.48 | $0.2000 | $0.2000 | $0.0000 |
| 5 | 0.14 / 0.83 | $0.3000 | $0.3000 | $0.0000 |
| 6 | 0.03 / 0.95 | $0.2000 | $0.2000 | $0.0000 |
| 7 | 0.34 / 0.64 | $0.2000 | $0.2000 | $0.0000 |
| **Total** |  | **$1.8000** | **$1.8000** | **$0.0000** |

That apparent `100.00%` is mechanical because the old paper client filled at
the decision snapshot. It is not evidence of slippage convergence.

The corrected REST-book sample produced the opposite result: two opportunities,
`$0.5000` expected, two one-leg fills and protective exits, `-$0.5000`
realized, `-100.00%` convergence, and `-200.00%` deviation. `n=2` is too
small to estimate a stable rate, but it is enough to fail the current gate and
prove that zero-slippage snapshot fills were unrealistic. The final clean
session saw zero opportunities, so its convergence status is unavailable, not
a pass.

Full forensic evidence:
[PnL and kill-switch follow-up](pnl_and_kill_switch_followup_2026-08-15.md).

## Alert-enabled kill switch

The sealed final run completed `901.055` seconds total and `900.306`
connected seconds. It ran `2,124` loops and completed three BTC window
rollovers. Alerting was on. The deliberate halt produced one session alert and
one webhook delivery; the post-halt reserve reason was exactly
`halted/emergency-stop` and orders remained `0 -> 0`.

The operational sink now contains `3` genuine kill-switch rows / `654` bytes.
The `2` synthetic fixture rows are preserved in a separate `807`-byte archive.

Evidence: [sealed session](../state/paper_session_archives/task_2_4_20260815T092241Z/session_report.json),
[operational alerts](../state/alerts.jsonl), and
[pollution archive](../state/alerts_test_pollution_archive_2026-08-15.jsonl).

## Tier-A audit findings

### Candle fallback

The silent `no_ask = 1.0 - close` fabrication is removed.
`BacktestEngine` now raises instead of presenting candles as independent
YES/NO quotes. The validator explicitly marks converted LSE ticks
`NOT VALIDATED / NON-COMPARABLE` and suppresses paper comparison, discrepancy,
and go-live PnL.

Evidence: [hardening report](silent_failure_and_supervisor_hardening_2026-08-15.md#candle-fallback)
and [validation artifact](../state/backtest_validation_report.json).

### Missing execution template

The branch is reachable when window release races a scheduled execution task.
It now records `missed` and `template_unavailable` and emits structured trade
and error logs. It did not cause the historical zero-order incident: that
archive had zero template-unavailable events; the root cause was the repaired
macro-state fail-closed fixture interaction.

Evidence: [hardening report](silent_failure_and_supervisor_hardening_2026-08-15.md#missing-execution-template).

## Depth gate

The real captured-depth row gate now passes: `672,906 / 500,000` paired ticks.
The operational gate fails: `0/30` complete windows, gap `30`. The corrected
replay produced `27,987` opportunities, `8,186` leg orders, `76` clean pairs,
`510` partials, `428` resolved partials, `24,097` misses, and `68` halted
windows. Requested-quantity fill rate was `0.9830264837337552`.

The simulated `47.19710000000002` session PnL is not executable evidence.
Verdict: **verified-fail**.

Evidence: [depth report](depth_replay_2026-08-15.md) and
[machine artifact](../state/chunk_b_depth_replay_validation_2026-08-15.json).

## BTC model

Both existing models beat prevalence PR-AUC on every holdout, but neither
train-calibrated threshold beats majority accuracy on every holdout. The
sequence gate remains skipped. All six model/policy combinations lose money in
all three holdouts; best aggregate PnL per exposure is
`-14.218422191135646` bps. This track is closed.

Evidence: [threshold correction](btc_offline_chronological_evaluation_2026-08-11.md#metric-correction-addendum--2026-08-11)
and [sizing evaluation](btc_offline_confidence_weighted_sizing_2026-08-15.md).

## Stability and dashboard

Three unchanged full suites each passed `581` tests with zero
failures/errors/skips. JUnit times were `93.183`, `92.915`, and `93.001`
seconds. Each had the same six known NumPy warnings; no torch segfault occurred.

The dashboard-focused suite passed `29` tests and Streamlit AppTest had `0`
exceptions. It now exposes the sealed session as completed/offline, depth as
`verified-fail` with `0/30` continuity, the latest `581`-test evidence, and no
green/executable PnL from the structural LSE conversion.

Evidence: [ground truth](ground_truth_2026-08-15.md),
[checklist](go_live_checklist_2026-08-11.md), and
[JUnit run 1](../state/pytest_final1_2026-08-15.xml),
[run 2](../state/pytest_final2_2026-08-15.xml),
[run 3](../state/pytest_final3_2026-08-15.xml).
