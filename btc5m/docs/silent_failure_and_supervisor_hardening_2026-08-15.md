# Silent-failure and supervisor hardening — 2026-08-15

## Verdict

The two Tier-A silent paths are closed:

1. Candle fallback no longer fabricates a complementary NO quote.
2. A missing execution template is reachable during a market-release race and
   is now counted and logged instead of silently dropped.

The WebSocket rotation and depth-capture ownership paths were also hardened.
The final full suite passed `581` tests three times with no failures or native
crash.

## Candle fallback

The old fallback converted one BTC/USD candle close into
`yes_ask = close` and `no_ask = 1.0 - close`. That forces
`yes_ask + no_ask == 1.0`, so a zero-opportunity result can be manufactured by
the fallback itself.

Current behavior:

- `BacktestEngine` raises `CandleFallbackUnavailable` rather than manufacturing
  outcome-book quotes.
- The validator permits candle-derived books only under explicit structural
  opt-in and labels them `NOT VALIDATED / NON-COMPARABLE`.
- `lse_tick_export` and `lse_tick_export_converted` are also non-comparable to
  independent Polymarket outcome books.
- For non-comparable input, paper-vs-backtest opportunities, PnL, normalized
  economics, direction, and discrepancy are suppressed. `go_live_pnl_metric`
  is `null`.

The regenerated [validation artifact](../state/backtest_validation_report.json)
records `comparable_to_outcome_books=false` and
`validated_against_real_data=false`. Its legacy `34.487500000000004`
structural PnL is retained for auditability but is not an executable PnL claim.

Evidence: [engine](../polymarket_arb/backtest_engine.py),
[validator](../scripts/validate_backtest.py), and
[validator tests](../tests/test_validate_backtest.py).

## Missing execution template

The `tpl is None` branch is live/reachable. `execute_dual_buy` is scheduled
asynchronously from an opportunity snapshot; window rotation can release that
market's execution template before the scheduled coroutine reads it. This is a
release race, not dead code.

Current behavior on that race:

- increments `metrics.missed`;
- increments `metrics.template_unavailable`;
- records miss reason `template_unavailable`;
- emits a structured missed `TRADE` event with market and price context; and
- writes a loud error log.

The final session summary exposes `template_unavailable=0`. The historical
11-opportunity/zero-order archive also contains zero template-unavailable
events, so this branch did **not** cause that incident. That incident was the
separately repaired macro-state interaction: a present macro file plus a
`None` reader result was treated as corruption and every `risk.reserve()`
failed closed.

Evidence: [execution path](../polymarket_arb/execution.py),
[race regression](../tests/test_execution_capital_accounting.py), and
[historical reconciliation](go_live_reconciliation_2026-08-13.md#finding-2--kill-switch-stress-harness-is-red-root-cause--macro-fail-closed).

## Macro fail-safe and test isolation

Production macro handling now fails closed on a missing, unreadable, invalid,
or stale state, and re-checks the state on each reserve. The execution-stress
fixture previously supplied `last_update=0` and polluted the operational
alert log with two synthetic `macro_state_failure` records. It now supplies an
explicit fresh neutral state.

The two historical synthetic rows were moved to
[`state/alerts_test_pollution_archive_2026-08-15.jsonl`](../state/alerts_test_pollution_archive_2026-08-15.jsonl).
The operational [alert log](../state/alerts.jsonl) contains exactly three
genuine kill-switch records (`3` rows / `654` bytes).

## WebSocket rollover signaling

`RealClobFeed.resubscribe` now always updates the desired token set before a
send. A send failure marks the socket unhealthy, records the error/close
reason, closes the bad socket, and rethrows. `PriceFeed` then keeps REST
fallback active and reconnects with the complete desired token set.

The final paper session completed three BTC five-minute rollovers with zero
stale-token errors. Four recovery records were emitted, each `0.7s`, and no
severe disconnect alert fired.

Evidence: [CLOB client](../polymarket_arb/clients/clob_client.py),
[feed coordinator](../polymarket_arb/price_feed.py),
[transport tests](../tests/test_clob_resubscribe_health.py), and
[sealed session](../state/paper_session_archives/task_2_4_20260815T092241Z/session_report.json).

## Depth-capture singleton

The process-level primitive is an advisory `fcntl` lock held by
`capture_depth_ws.py`. Lock contention exits `75`
(`EX_TEMPFAIL`). The supervisor prefers launchd, performs at most one
bootstrap attempt, and `BOOTSTRAP_ONLY=1` never starts the fallback loop.

At `2026-08-15T10:27:07Z` there was exactly one process: PID `28436`, PPID
`1`. launchd label `com.rytty.depth_capture` reported `state=running`,
`runs=1`, and no exit. The repository plist and installed plist use this
canonical command and lock path.

Evidence: [capture implementation](../scripts/capture_depth_ws.py),
[supervisor](../scripts/supervise_depth_capture.sh),
[plist](../ops/launchd/com.rytty.depth_capture.plist),
[singleton tests](../tests/test_depth_capture_singleton.py), and
[bootstrap tests](../tests/test_supervise_bootstrap.py).

## Regression evidence

Final unchanged full-suite runs:

- [run 1](../state/pytest_final1_2026-08-15.xml): `581` passed,
  `0` failed/errors/skipped, `93.183s`;
- [run 2](../state/pytest_final2_2026-08-15.xml): `581` passed,
  `0` failed/errors/skipped, `92.915s`;
- [run 3](../state/pytest_final3_2026-08-15.xml): `581` passed,
  `0` failed/errors/skipped, `93.001s`.

Each run had the same six known NumPy warnings. No torch segmentation fault
occurred.
