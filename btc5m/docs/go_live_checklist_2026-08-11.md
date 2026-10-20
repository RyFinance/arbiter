# Historical go-live checklist snapshot — superseded 2026-08-15

> This snapshot is preserved for audit history. The single authoritative current
> checklist is [product_status_2026-08-15.md](product_status_2026-08-15.md), generated
> from the same live gate rows shown in the dashboard. Do not use the values below as
> current operating status.

The filename and frozen contents below are retained only for compatibility and
audit history. They are not the current checklist.

## Decision

**VERIFIED-FAIL — DO NOT ENABLE LIVE TRADING.**

The software safety paths and regression suite are materially stronger, but
venue access/funding are externally blocked, executable fill and PnL gates do
not pass, depth continuity is `0/30`, and no live-order validation exists.
Every market session performed for this update was `PAPER_TRADE`. No real
order was submitted, no signing/risk threshold was relaxed, and no geoblock was
bypassed.

Status vocabulary:

- **verified-pass** — the cited evidence met the narrowly stated criterion.
- **verified-fail** — the cited evidence did not meet the criterion.
- **blocked-external** — venue, jurisdiction, wallet, or unavailable future
  data prevents completion from this workspace.
- **not-yet-tested** — no qualifying real-world evidence exists.

## Checklist

| # | Gate | Current status | Exact current finding | Evidence | Last verified / tonight | Owner |
| ---: | --- | --- | --- | --- | --- | --- |
| 1 | Overall go-live authorization | **verified-fail** | Multiple mandatory gates below fail or are externally blocked. | [Ground truth](ground_truth_2026-08-15.md), [final status](final_status_2026-08-15.md) | 2026-08-15, rechecked tonight | operator |
| 2 | Jurisdiction / geoblock | **blocked-external** | Polymarket reported the machine blocked in CA/BC. Acceptance requires `blocked=false` from the actual machine; bypass is prohibited. | [2026-08-13 reconciliation](go_live_reconciliation_2026-08-13.md#10-item-reconciliation-f3-methodology--2026-08-13), [preflight](preflight_ready.md#step-4-verify-geoblock-status) | Last verified 2026-08-13; **not rechecked tonight** | operator/legal |
| 3 | Wallet funding and venue approvals | **blocked-external** | Last read-only check: `0.0000 MATIC`, `$0.00 pUSD`, pUSD allowances `0/0`; CTF approvals still require receipt-based confirmation. | [2026-08-13 reconciliation](go_live_reconciliation_2026-08-13.md#10-item-reconciliation-f3-methodology--2026-08-13), [procedure](preflight_ready.md#summary) | Last verified 2026-08-13; **not rechecked tonight** | operator/wallet |
| 4 | Kill-switch behavior with alerting | **verified-pass** | Alerting enabled; halt set; 1 durable session alert; 1 webhook delivery; post-halt reserve rejected as exactly `halted/emergency-stop`; orders `0 -> 0`. | [sealed report](../state/paper_session_archives/task_2_4_20260815T092241Z/session_report.json), [follow-up](pnl_and_kill_switch_followup_2026-08-15.md#clean-kill-switch-verification) | 2026-08-15, rechecked tonight | execution |
| 5 | Alert persistence | **verified-pass** | Operational sink contains exactly 3 genuine kill-switch rows / 654 bytes. Two synthetic rows are retained separately; the fixture pollution source is fixed. | [operational log](../state/alerts.jsonl), [pollution archive](../state/alerts_test_pollution_archive_2026-08-15.jsonl), [ground truth](ground_truth_2026-08-15.md#live-paper-session-alerting-and-kill-switch) | 2026-08-15, rechecked after full suites | observability |
| 6 | Two-leg fill fidelity | **verified-fail** | Strict replay simulated `0.9830264837337552` requested-quantity rate, but had `510` partials and `68` halted windows. It is passive simulation, not live/exchange-confirmed fill evidence. | [depth replay](depth_replay_2026-08-15.md#quantity-accounting), [artifact](../state/chunk_b_depth_replay_validation_2026-08-15.json) | 2026-08-15, rechecked tonight | execution/data |
| 7 | Quantity-cap interpretation | **verified-pass** | Strict replay now reports actual requested and filled ladder quantity: `30,108.67` requested, `29,597.620000000003` filled. The legacy five-share cap is no longer represented as ten-share fill fidelity. This passes accounting semantics only. | [depth replay](depth_replay_2026-08-15.md#quantity-accounting), [engine](../polymarket_arb/backtest_engine.py) | 2026-08-15, rechecked tonight | execution |
| 8 | Quote freshness | **verified-fail** | Last qualifying sample was `120/140 = 85.7%` within 500 ms, below the `>=95%` gate. Tonight's clean run had 0 fills, so it provides no replacement sample. | [prior final report](go_live_final_report.md#a2--quote-freshness), [clean report](../state/paper_session_archives/task_2_4_20260815T092241Z/session_report.json) | Last sampled 2026-08-05; zero-sample state rechecked 2026-08-15 | execution |
| 9 | PnL convergence | **verified-fail** | The old `$2.5000` denominator was wrong. Seven snapshot fills were mechanically `$1.8000/$1.8000`. The real REST-book sample was `$0.5000` expected versus `-$0.5000` realized: `-100.00%` convergence, `-200.00%` deviation, `n=2`. The clean run had no sample. | [forensic and REST results](pnl_and_kill_switch_followup_2026-08-15.md), [failed REST sample](../state/paper_session_archives/task_2_4_20260815T090124Z/session_report.json) | 2026-08-15, rechecked tonight | execution |
| 10 | End-to-end latency | **verified-fail** | The two-event REST sample had opportunity-to-first-order avg/p50/p95/p99 `4.34/1.82/6.86/6.86 ms` and ack avg `210.43 ms`, but `n=2` is thin. The last qualifying broader sample averaged `746.8 ms` end-to-end against p95 `<250 ms`. | [REST sample detail](pnl_and_kill_switch_followup_2026-08-15.md#rest-book-execution-evidence), [prior final report](go_live_final_report.md#a2--quote-freshness) | Thin sample 2026-08-15; qualifying failure last verified 2026-08-05 | execution |
| 11 | BTC / LightGBM model gate | **verified-fail** | PR-AUC beats prevalence on all holdouts, but train-calibrated accuracy does not beat majority on every holdout. Sequence gate remains skipped. All 6 model/policy combinations lose money in all holdouts; best aggregate is `-14.218422191135646` bps per exposure. | [threshold addendum](btc_offline_chronological_evaluation_2026-08-11.md#metric-correction-addendum--2026-08-11), [sizing evaluation](btc_offline_confidence_weighted_sizing_2026-08-15.md) | 2026-08-15, rechecked tonight | research (track closed) |
| 12 | Real captured-depth gate | **verified-fail** | Row gate passes `672,906/500,000`, but operational continuity is `0/30`, exact gap `30`. Replay: `76` clean fills, `510` partials, `68` halted windows. | [depth report](depth_replay_2026-08-15.md), [artifact](../state/chunk_b_depth_replay_validation_2026-08-15.json) | 2026-08-15, rechecked twice deterministically | data capture |
| 13 | Full test-suite stability | **verified-pass** | Three unchanged runs: `581/581` passed each, 0 failures/errors/skips, 6 identical warnings; JUnit times `93.183/92.915/93.001s`. No torch segfault. | [run 1](../state/pytest_final1_2026-08-15.xml), [run 2](../state/pytest_final2_2026-08-15.xml), [run 3](../state/pytest_final3_2026-08-15.xml) | 2026-08-15, rechecked tonight | test infrastructure |
| 14 | Trading-bot window rollover | **verified-pass** | `900.306` connected seconds, 3 completed five-minute rollovers, 0 stale-token errors, 4 recovery messages at `0.7s`, 0 severe disconnect alerts. | [sealed report](../state/paper_session_archives/task_2_4_20260815T092241Z/session_report.json), [transport hardening](silent_failure_and_supervisor_hardening_2026-08-15.md#websocket-rollover-signaling) | 2026-08-15, rechecked tonight | execution |
| 15 | Depth-capture singleton / supervisor | **verified-pass** | Exactly one launchd process: PID `28436`, PPID `1`, `runs=1`, lock held; duplicate start exits `75`; `BOOTSTRAP_ONLY` never spawns fallback. | [ground truth](ground_truth_2026-08-15.md#capture-ownership), [hardening](silent_failure_and_supervisor_hardening_2026-08-15.md#depth-capture-singleton) | 2026-08-15, rechecked tonight | operations |
| 16 | Macro-state fail-safe | **verified-pass** | Missing/unreadable/invalid/stale state blocks reserve and alerts. Current feed was fresh at observation (`203` events, `46` windows); the stress fixture now uses a fresh neutral state. | [hardening](silent_failure_and_supervisor_hardening_2026-08-15.md#macro-fail-safe-and-test-isolation), [risk tests](../tests/test_macro_risk.py) | 2026-08-15, rechecked tonight | risk/data |
| 17 | Backtest input integrity | **verified-pass** | Candle fabrication is removed. Regenerated LSE conversion is explicitly `NOT VALIDATED / NON-COMPARABLE`, with comparison disabled and go-live PnL `null`. | [artifact](../state/backtest_validation_report.json), [hardening](silent_failure_and_supervisor_hardening_2026-08-15.md#candle-fallback) | 2026-08-15, rechecked tonight | research infrastructure |
| 18 | Missing-template observability | **verified-pass** | Release-race path is reachable and now increments `missed` + `template_unavailable` and emits structured trade/error logs. Historical zero-orders archive had 0 such events. | [hardening](silent_failure_and_supervisor_hardening_2026-08-15.md#missing-execution-template), [test](../tests/test_execution_capital_accounting.py) | 2026-08-15, rechecked tonight | execution |
| 19 | Operator dashboard truthfulness | **verified-pass** | Dashboard shows sealed-session/offline state, kill verification, depth `verified-fail`, `0/30` windows, exact simulated fill rate, latest `581`-test evidence, and does not promote structural LSE PnL. `29` focused tests; 0 AppTest exceptions. | [dashboard](../streamlit_app.py), [status helpers](../polymarket_arb/dashboard_status.py), [tests](../tests/test_dashboard_status.py) | 2026-08-15, rechecked tonight | observability |
| 20 | Real live-order validation | **not-yet-tested** | No qualifying real venue order/fill exists. Paper and replay results cannot substitute for it. This remains intentionally blocked behind jurisdiction, funding, approvals, and failed execution gates. | [ground truth](ground_truth_2026-08-15.md), [preflight](preflight_ready.md) | Never | operator |

## Required evidence before reconsideration

The verdict cannot change until all of these exist:

1. A permitted-jurisdiction preflight with wallet funding and receipt-confirmed
   approvals.
2. At least `30` complete captured-depth windows under the documented
   continuity criterion, followed by a strict replay without the current halt
   rate.
3. A new qualifying paper sample meeting `>=95%` quote freshness,
   `+/-10%` fill-time PnL convergence, and end-to-end p95 `<250 ms`.
4. Only after those pass, a separately authorized minimal live validation.
5. No BTC model promotion: the current model/sizing track is closed as a
   negative result.

## Scope boundary

No Kalshi/cross-venue file, `models/` artifact,
`state/cross_venue_arb_log.jsonl`, execution risk threshold, signing setting,
or real-order path was changed for this pass.
