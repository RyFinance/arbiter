# Final project status — 2026-08-16

**This document supersedes every prior status document.** Previous status
docs (`final_status_2026-08-15.md`, `product_status_2026-08-15.md`,
`go_live_checklist_2026-08-11.md`, `go_live_reconciliation_2026-08-13.md`,
`go_live_final_report.md`, and the dashboard research reports) remain on
disk for audit history only. If you are asked "is this done", the answer
and its evidence are here.

## Bottom line

**VERIFIED-FAIL — DO NOT ENABLE LIVE TRADING. Real orders submitted: 0.
All sessions PAPER_TRADE. No geoblock bypass, no real fund movement.**

The project's safety rails, replay harness, capture infrastructure, and
test suite are in their strongest verified state to date. The trading
economics are not: the clean-fill-rate investigation closed today with a
definitive verdict — **the arbitrage edge on these Polymarket BTC 5-minute
books decays within a single tick (~1–3 ms), faster than two-leg execution
can complete, and larger edges decay faster than smaller ones.** That is a
market-microstructure finding, not a code defect, and no further
engineering on the current two-leg execution model fixes it.

## The headline finding (clean-fill rate)

Prior headline: **0.27% clean fills (77 of 28,407 opportunities)** from
`state/depth_replay_validation_combined_2026-08-15.json`. Investigation:
[depth_replay_fill_rate_investigation_2026-08-16.md](depth_replay_fill_rate_investigation_2026-08-16.md).

The 0.27% was artifact-depressed by a replay harness defect and it
overstated nothing:

1. **Harness defect, now fixed.** In replay every window shared synthetic
   token ids, so `Execution._pending` keys collided across fires
   (`execution.py` insert guard) and **76.5% of fired pairs were silently
   discarded** — including 181 clean two-leg fills and 1,913 one-leg pairs.
   Fix: per-fire unique order ids (`polymarket_arb/backtest_engine.py`,
   `_BacktestTradingClient._order_seq`) and a per-fire sequence in the
   pending key (`polymarket_arb/execution.py`, `Execution._fire_seq`).
2. **Corrected replay (canonical artifact
   `state/depth_replay_validation_collision_fix_2026-08-16.json`),
   independently reproduced by the instrumented driver
   `state/depth_fill_rate_investigation_fixed_2026-08-16.json` — the two
   agree on every counter:**

   | Measure | Corrected |
   | --- | ---: |
   | Paired ticks | 975,259 |
   | Opportunities | 28,533 |
   | Orders (legs) / fired pairs | 5,216 / 2,608 |
   | Clean two-leg fills | **142 (5.44% of fired pairs)** |
   | One-leg partials | 1,579 (60.5%) |
   | Neither-leg (no-fill) | 887 (34.0%) |
   | Accounting reconciliation | 142 + 1,579 + 887 = 2,608 — zero dropped |
   | Partial resolved | 1,209 |
   | Halted windows | 123 / 445 |
   | Session PnL | locked +87.156, residual −84.378, **session +2.778** |

   The naive estimate of ~258 clean fills assumed the same opportunity set
   as the broken run; with honest accounting the partial-recovery path runs
   for every one-leg pair, exits fail in the same moving market (342 exit
   rejects), and 123 windows halt early — so fewer pairs fire (2,608) and
   the clean rate over fired pairs is 5.44% (vs 5.8% submit-level truth in
   the pre-fix instrumented run). Same range, honest accounting.
3. **Market finding, not code:** of the 2,608 fired pairs, 60.5% lost one
   leg and 34.0% lost both within a single book update. The missing leg's
   ask moves a median **+3.0 cents** (p90 +11.0 cents) above the decision
   limit in that one tick, while decision-tick depth was ≥5× the 10-share
   order — **price movement, not thin depth, kills the second leg.**
4. **VWAP / latency assumptions were audited and need no fix:** fills
   execute at the decision price (VWAP = limit in every case, no price
   improvement lost), and the one-tick resolve lag (~1–3 ms) matches the
   4.34 ms live opportunity→order measurement — realistic, not the driver
   of the low rate ([investigation §3b](depth_replay_fill_rate_investigation_2026-08-16.md)).
5. **Edge-size verdict:** clean-fill rate **falls** with edge —
   1.15% (0.02–0.03) → 1.07% (0.03–0.05) → 0.83% (0.05–0.10) →
   **0.48% (≥0.10)** ([investigation §4](depth_replay_fill_rate_investigation_2026-08-16.md)).
   Larger mispricings revert faster; tightening `MIN_EDGE` makes the clean
   rate worse, not better.

**Conclusion (plain):** this is a genuine reflection of how thin and fast
real arb liquidity is on this market. No further engineering on the
current model fixes it. A materially different execution approach is
required — accepting single-leg exposure as a designed position (instead
of treating it as a failure to exit), or trading only markets with deeper
resting liquidity — and that is a strategy decision, not a bug fix.

## Verified capabilities (with evidence)

| Capability | Status | Evidence |
| --- | --- | --- |
| Full regression suite | **PASS — 730/730 passed, 0 failed/error/skipped, 3 consecutive runs** (88.61 s / 87.87 s / 88.19 s, 7 identical warnings) | [suite 1](../state/pytest_final_suite_1_2026-08-16.xml), [suite 2](../state/pytest_final_suite_2_2026-08-16.xml), [suite 3](../state/pytest_final_suite_3_2026-08-16.xml) |
| Strict depth replay harness | **Fixed and deterministic** — two independent drivers (official `scripts/replay_captured_depth.py`, instrumented `scripts/analyze_depth_fill_rate.py`) reproduce every counter exactly; pending-pair accounting reconciles with zero dropped pairs | [canonical corrected artifact](../state/depth_replay_validation_collision_fix_2026-08-16.json), [instrumented artifact](../state/depth_fill_rate_investigation_fixed_2026-08-16.json) |
| Depth capture singleton + continuity | **PASS** — 68 live ps samples over 340 s spanning two 5-minute window boundaries: exactly one process (PID 48787, PPID 1, launchd) at every sample; capture 1.6 s fresh at last check | [sampler artifact](../state/capture_supervisor_samples_2026-08-16.json) |
| Depth continuity maturity | Newest shard **58/69 windows complete (84.1%)**; last-30 windows **25/30**; full history 1,709,257 snapshots across 471 windows (58 complete, 12.3% — legacy shards dominate the denominator); operational 30-window gate **pass** | [corrected replay artifact](../state/depth_replay_validation_collision_fix_2026-08-16.json) (`operational_window_gate`), live readers check 2026-08-16 |
| Safety rails | Kill switch verified (post-halt reserve returns exactly `halted/emergency-stop`, orders 0→0); alerts durable; live exchange order endpoint intentionally unbuilt; every session sealed + PAPER_TRADE | [product gates 1–9](../state/product_status_2026-08-15.json), [sealed archive](../state/paper_session_archives/task_2_4_20260815T092241Z/session_report.json) |
| Go-live gate ledger | 33 gates: **22 verified-pass / 5 verified-fail / 1 blocked-external / 5 not-yet-tested** | [product_status_2026-08-15.md](product_status_2026-08-15.md) |
| ML/BTC model economics | Closed as negative: every model × sizing policy loses in every chronological holdout; best pooled PnL/exposure −14.22 bps; Sharpe negative everywhere | [sizing+Sharpe report](btc_offline_sizing_sharpe_2026-08-15.md), [artifact](../docs/research/btc_offline_sizing_sharpe_pooled_2026-08-15.json) |
| Jurisdiction / compliance | Documented, cited, current: US persons blocked; operator machine blocked in CA/BC (close-only tier); live trading from this location violates ToS | [jurisdiction doc](jurisdiction_compliance.md) |
| Dashboards (terminal + canvas) | Built and end-to-end verified (0 console errors, all panels real-data or honest-empty, drag/layout persistence verified in Playwright) | [canvas README](../dashboard_canvas/README.md), verification artifacts in prior session reports |

## Hard limitations (external and technical)

External (not fixable from this workspace):

- **Jurisdiction:** machine geolocates CA/BC — Polymarket geoblock API
  returns `{"blocked":true,"country":"CA","region":"BC"}`; VPN circumvention
  prohibited by ToS §2.1.4. [jurisdiction doc](jurisdiction_compliance.md).
- **Wallet/funding:** 0.0 MATIC, $0.00 pUSD, allowances 0/0, CTF approvals
  false, RPC 401; `funding_ready=false`. [wallet_status.json](../state/wallet_status.json).
- **Venue approvals / real-fill evidence:** no real order has ever been
  submitted; live endpoint unbuilt by design.

Technical (verified today, not fixable without a strategy change):

- **One-tick edge decay** (this document's headline): 94.4% of fired pairs
  lose ≥1 leg within ~1–3 ms; larger edges decay faster. Corrected clean
  rate 5.44% of fired pairs; partial-recovery exits lose money in the same
  moving book (123 halted windows in the corrected replay).
- **Quote freshness and latency gates** remain unproven against their
  targets (≥95% within 500 ms; p95 < 250 ms over ≥100 fires) — no
  qualifying live sample exists. [product gates 11–12](../state/product_status_2026-08-15.json).
- **BTC proxy model economics** negative in all holdouts (see table above).

## Go-live status

**NOT AUTHORIZED — and the technical basis for authorization does not
exist.** The kill switch, capture, harness, and test-suite evidence is as
strong as it has ever been (730/730 ×3; collision-free replay; 84%
continuity on the live shard). The trading evidence is decisively
negative: the two-leg arb edge decays in one tick on this market. Live
trading would additionally require jurisdiction relocation, wallet
funding, venue approvals, and real-fill validation — none of which exist.

## Superseded documents

`final_status_2026-08-15.md`, `product_status_2026-08-15.md`,
`go_live_checklist_2026-08-11.md`, `go_live_reconciliation_2026-08-13.md`,
`go_live_final_report.md`, `go_live_report_2026-08-04.md`,
`depth_replay_2026-08-15.md` (superseded by the corrected artifact + this
report), and `btc_offline_confidence_weighted_sizing_2026-08-15.md`
(superseded by `btc_offline_sizing_sharpe_2026-08-15.md`).
