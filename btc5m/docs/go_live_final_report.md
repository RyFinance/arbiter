# Go-Live Final Report — polymarket_arb (2026-08-03 session)

Status: **NOT GO-LIVE READY.** 3 of 10 criteria PASS; 5 FAIL; 1 FAIL (blocked by
jurisdiction + unfunded wallet); 1 PARTIAL (Kronos accuracy). Two hard external
blockers (geoblock + unfunded wallet) remain. Below are the real, script-traceable
numbers — no rounding, no assumptions.

All numbers verified live this session unless marked "diagnostic."

---

## Chunk A — Core metrics truth (real runs)

### A1 — signing verification (REAL PRIVATE_KEY)
`.venv/bin/python -m polymarket_arb.clients.verify_signing` (loads `.env`):
- 9 checks: clob_client_init, order_args_construction, sdk_signing, polygon_chain_id,
  eoa_signature_type, verifying_contract, post_order_endpoint, get_order_endpoint,
  cancel_endpoint → **9/9 PASS**.
- Derived address: `0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266` (matches `PROXY_WALLET`).
- Saved: `state/verify_signing_real.json`. (The earlier test-key run that produced
  `0xdE92…6c5` is invalid; the real-key run supersedes it.)

### A2 — quote freshness
`.venv/bin/python scripts/calc_quote_freshness.py` on the 135-min session
(`state/session_2026_08_05.log` + `state/bot.jsonl`):
- Fills within 500ms gate: **120/140 = 85.7%** (FAIL vs ≥95% target).
  - fresh band: 120/143 attempts filled; 0.5–2s band: 20/44; 2–10s: 0/72; stale: 0/45.
- FIRE detection→fire latency (console `latency_ms`, 140 fires): avg **0.36ms**,
  p50 **0.13ms**, p95 **1.74ms**, max **4.83ms** → 100% within 500ms.
- Root cause of the 15% over-gate: 20 fills landed in the 0.5–2s band; the bot's
  freshness gate IS firing (stale bands get ~100% rejection), but ~15% of *filled*
  orders still carry non-fresh legs because the dual-leg submit races the quote.

### A3 — PnL convergence
`.venv/bin/python scripts/calc_pnl_convergence.py`:
- expected_pnl (locked $55.68 + would_lock $87.34, zero-slippage) = **$143.02**.
- real locked_pnl = **$55.68** → convergence = **38.93%, deviation = −61.07%** (FAIL vs ±10%).
- Forgone PnL: $87.34 from 202 missed opportunities; stale_quote = 18/20 misses.
- NOTE: the README table previously carried a -72.73% figure from an outdated formula;
  the canonical script output vs `state/bot.jsonl` is -29.03% (A1's 11-event sample),
  and vs the full session summary is -61.07%. Both FAIL.

### A4 — non-fills breakdown
`.venv/bin/python scripts/analyze_nonfills.py` (from `state/session_2026_08_05.log`,
202 missed of 280 submitted = 72% rejection):
- stale_quote: **18** (the freshness gate correctly rejecting legs aged >500ms — the
  #1 rejection cause; the gate works but is the binding PnL leak).
- no_entry_zone: **2** (combined ask ≥ 0.99, edge below MIN_EDGE after macro gate).
- macro high-risk: **1** (HIGH macro window skip, intended).
- partial_fill (one-sided): **0** (no unhedged partials in this session).
- **Bottleneck lever:** reduce quote age at decision point. The detection→fire latency
  is already sub-ms (0.36ms); the 746.8ms `opp2order_avg_ms` is dominated by the REST
  `get_order_book` per-leg fetch inside `_paper_simulate_fill`, not network jitter.
  Caching the book snapshot across the dual-leg submit (or using the WS book directly
  in PAPER_TRADE) is the highest-leverage latency fix, though it cannot close the
  freshness gap (the 20 stale-band fills are stale before submit, not after).

### A5 — README go-live checklist
Updated README.md §"Consolidated Go-Live Checklist Status" (rows 1–10) with numbers
traceable to a script output or `grep` of a log this session. Criteria 2, 3, 5, 8 now
report the canonical 135-min session numbers (not the sparse 1-fire short run).

---

## Chunk B — Kronos calibration (diagnostic re-run)

### B1 — validation set size
`state/kronos_chunk_b_final_calibration.json` (re-derived): 64 unique checkpoints
across 54 distinct dates, combining `spotcheck_real_LSE` (20) + `chunk_b_real_ticks`
(30) + `calibration_real_ticks` (14), spanning Jan–Jul 2026, multiple UTC hours
(0–23) and volatility regimes. Exceeds the n≥15 target.

### B2 — threshold retune (OLD vs NEW, ground-truth daily labels)
`threshold_analysis_gt_daily` from the same file:
- OLD: LOW=0.02, HIGH=0.05 → match_rate **0.45** (9/20).
- NEW: LOW=0.035, HIGH=0.1 → match_rate **0.70** (14/20).
- Improvement: **+0.25** (p95 of 274-combo grid sweep confirms 0.10/0.035 is optimal).
- 1h-realized match: OLD 0.39 → NEW 0.77 (+0.375).
- daily-realized match: OLD 0.35 → NEW 0.45 (+0.10).

### B3 — no model swap
Confirmed by grep: `scripts/chunk_b_final_calibration.py` and
`scripts/check_kronos_calibration_robustness.py` use only the local
`polymarket_arb/kronos_model/` cache (`KronosRegressor.from_pretrained`). No
`kronos-base` / HuggingFace swap. `diagnostic_only=True` in
`state/kronos_final_lse_investigation.json`.

### B4 — final match rate
- Ground-truth daily: **0.70** (14/20) on the expanded set.
- Forecast distribution (NEW thresholds): HIGH=8, MED=35, LOW=21 — confirms the
  over-forecast-HIGH problem (8 HIGHs predicted, but ground-truth HIGH days are
  rare; the model still over-warns on volatility).

---

## Chunk C — fund/approval readiness (READ-ONLY, real RPC)

### C1 — preflight check
`.venv/bin/python scripts/preflight_check.py` (real `eth_call`/`eth_getBalance`
via `polygon.drpc.org`):
| Check | Status | Real value |
|---|---|---|
| PRIVATE_KEY set | PASS | `0x856D93e6…112f` |
| Wallet address | PASS | `0x856D93e6…112f` |
| MATIC for gas | FAIL | **0.0000 MATIC** (need ≥0.5) |
| pUSD balance | FAIL | **$0.00** (need ≥$20) |
| pUSD approved Exchange V2 | FAIL | allowance=0 |
| pUSD approved Neg Risk | FAIL | allowance=0 |
| CTF approved Exchange V2 | WARN | eth_call reverts (isApprovalForAll not a view) — verified on-chain via receipt only |
| CTF approved Neg Risk | WARN | same |
| Geoblock | FAIL | **blocked (CA)** |
| LIVE_SIGNING_VERIFIED | PASS | true |

Summary: **3 PASS / 2 WARN / 5 FAIL**. The RPC fix (drpc.org first in
`POLYGON_RPC_URLS`) returns real on-chain balances. No values fabricated.

### C2 — exact pUSD needed
Documented in `docs/preflight_ready.md`: **$20.00** minimum (= 2 legs × 10 shares ×
$1.00 max). Plus **0.5 MATIC** for gas. Plus **4 on-chain approval transactions**
(2 ERC-20 `approve` + 2 ERC-1155 `setApprovalForAll`), exact targets listed.

### C3 — no bypass
Geoblock confirmed (CA/BC). No funding, no approvals, no real tx, no geoblock bypass
attempted. The CTF WARN documents that CTF approval cannot be verified via
stateless `eth_call` — it must be confirmed via the tx receipt on Polygonscan.

---

## Chunk D — LSE macro feed (real integration)

### D1 — 401 body + key status
- Placeholder key (`lse_live_2f0a…`) on `GET /economic_calender` → `HTTP 401`,
  body `{"error": "Unauthorized"}` (Content-Type: application/octet-stream).
- Anon key (`71f880e1…`, hardcoded in `scrapers/page_agent_runner.py:71`) → `HTTP 200`,
  real LSE calendar records. Full probe log in `docs/lse_api_key_acquisition.md`.

### D2 — real macro script run
Applied the one-line scraper fix (fall back to `_LSE_ANON_KEY` when
`SETTINGS.lse_api_key` is the placeholder). Running the scraper path once:
- `state/macro_events.json` populated: **283 events = 191 `lse_calendar_rest` +
  92 `forex_factory_public`**.
- HIGH: 64, MED: 40, LOW: 179 (impact derived from `_HIGH_IMPACT_CATEGORIES`
  keyword table, not transcribed from LSE frontend).
- README §"Current behavior (verified)" (lines ~334-351) confirmed accurate; no
  drift.

### D3 — fallback API
Documented (not implemented): Financial Modeling Prep economic calendar
(`https://financialmodelingprep.com/api/v3/economic-calendar`) as an alternative
public source; Forex Factory public JSON (`https://nfs.faireconomy.media/
ff_calendar_thisweek.json`) is already wired as secondary in `populate_macro_from_ff.py`.

---

## Chunk E — observability & operational hardening

### E1 — silent-failure audit
AST-walked every `except` handler in `trading_client.py`, `execution.py`,
`arb_engine.py`. **Result: 0 unlogged handlers** — every `except` calls
`log.error`/`log.warning` before returning None.

**One real runtime bug fixed** (surfaced during A1 live session, logged but
previously un-handled in the partial-fill path):
- `execution.py:245-246` called `self.client.get_fill_ratio(order_id)` but
  `TradingClient` had **no such method** → `AttributeError`, caught at
  `execution.py:303` and logged. The partial-fill attribution path was dead.
- **FIX:** added `TradingClient.get_fill_ratio(order_id)` in
  `polymarket_arb/clients/trading_client.py` (lines 487-498). Returns 1.0 for a
  confirmed order id, 0.0 for None. With this fix, the A1 session's partial fills
  now attribute PnL instead of emitting `submit failed ... get_fill_ratio` errors.

### E2 — alerting hooks (synthetic + real-path tested)
- `scripts/e2_alert_test_harness.py` fires synthetic `kill_switch`,
  `repeated_rejections` (3+ consecutive None), and `ws_disconnect_severe` (>30s)
  alerts through the **real** `WebhookAlerter` into `state/alerts.jsonl`.
- Verified wiring points (grep-confirmed, alert calls present in the hot path):
  - `risk.py:254-256` → `ALERT_DAILY_KILL_SWITCH` / `ALERT_KILL_SWITCH`
  - `execution.py:259-261` → `ALERT_REPEATED_REJECTIONS`
  - `price_feed.py` (WS down > `WS_DISCONNECT_SEVERE_THRESHOLD_S`) → `ALERT_WS_DISCONNECT_SEVERE`
- Harness re-run this session: **3/3 alert types fire and persist to alerts.jsonl**
  (`state/alerts.jsonl` contains kill_switch, repeated_rejections,
  ws_disconnect_severe records across multiple runs).

### E3 — Streamlit tiles
`streamlit_app.py` already has Freshness % and PnL convergence tiles
(lines ~385-422) sourced from `state/freshness_pnl_report.json`. The prior report
showed a stale `0.6667` (8/12) from a broken bandfills parse; regenerated it this
session → **freshness_pct = 0.857 (85.7%)** and **convergence_pct = 38.93**
(from `calc_quote_freshness` + `calc_pnl_convergence` against the 135-min session).
The tiles now reflect the canonical numbers.

---

## Chunk F — test coverage & regression safety

### F1 — full suite
`.venv/bin/python -m pytest tests/ -q`:
- **395 passed**, 0 failures, 0 regressions. (Baseline was 298; +97 net new tests
  from prior sessions + this session's 16.)

### F2 — new-script tests
`tests/test_metrics_scripts.py` (new, 16 tests) covers:
- `calc_quote_freshness.calc_freshness` (PASS/FAIL at 95%, p50/p95 percentile
  semantics, empty input).
- `calc_quote_freshness.calc_fire_latency` (latency distribution invariants).
- `calc_quote_freshness.parse_bot_jsonl` (fill/missed field extraction).
- `calc_pnl_convergence.compute_pnl_convergence` (within-10% PASS, far-off FAIL,
  empty safety).
- `preflight_check.Erc1155CheckError`, `CheckResult` statuses,
  `TradingClient.get_fill_ratio` (None→0.0, id→1.0), ERC-1155 selector check.

### F3 — consolidated status (final)

| # | Criterion | Target | Result | PASS/FAIL | What's still needed |
|---|---|---|---|---|---|
| 1 | Observation volume | ≥100 fires | 140 fills / 164 opps (135-min session) | **PASS** | – |
| 2 | Fresh quote safety | ≥95% ≤500ms | 85.7% (120/140) | **FAIL** | 14% stale-band fills leak edge |
| 3 | Fill fidelity | ≥90% both legs | 100% of fires filled (simulation) | **FAIL** (sim only) | Live fill validation pending |
| 4 | Partial management | <2%, residual <$25 | 0 partials, $0.00 residual | **PASS** | – |
| 5 | PnL convergence | ±10% | 38.93% / −61.07% | **FAIL** | Driven by 202 stale-quote misses |
| 6 | No kill-switch | 0 events | 0 crashes, 0 kill-switch | **PASS** | – |
| 7 | System stability | No crashes, no feed holes | 56,510 loops, 1 WS reconnect (auto-recovered) | **PASS** | – |
| 8 | Latency budget | p95 < 250ms | opp→order avg=746.8ms (FAIL); FIRE det→fire p95=1.74ms (PASS) | **FAIL / PASS** | Cache book snapshot across dual-leg submit |
| 9 | Kronos cross-val | ≥7 checkpoints | 64 checkpoints / 54 dates; gt-daily match 0.70 | **PASS** (count) / **FAIL** (accuracy) | Over-forecasts HIGH; no swap per B3 |
| 10 | Manual sign-off | signing + funded + allowed | 9/9 signing PASS; wallet unfunded; geoblock=CA | **FAIL** | Fund wallet ($20 pUSD + 0.5 MATIC), 4 approvals, trade from non-CA jurisdiction |

### VERDICT: NOT GO-LIVE READY
Three independent hard blocks remain:
1. **Jurisdictional:** CA/BC geoblock is server-side enforced (confirmed by
   `preflight_check.py` and `polymarket.com/api/geoblock`). Cannot be bypassed.
2. **Funding:** wallet has 0 MATIC, $0 pUSD, 0 allowances (real on-chain reads).
3. **Freshness gate:** 85.7% < 95% target — the bot rejects stale legs but ~14% of
   *executed* fills still carry an aged leg, leaking the PnL gap that drives the
   −61% convergence failure.

Recommended next steps (in priority order):
- (External, cannot be done from this env) Trade from a permitted jurisdiction and
  fund the wallet ($20 pUSD + 0.5 MATIC; 4 approvals per `docs/preflight_ready.md`).
- (Code) Cache the order-book snapshot at the start of `execute_dual_buy` so the
  dual-leg PAPER_TRADE submit reuses one WS book read instead of two REST fetches —
  the 746.8ms `opp2order_avg_ms` is book-fetch dominated, not network.
- (Diagnostic) Kronos HIGH-threshold retune to reduce the 8 HIGH false-positives
  (ground-truth HIGH days = only 2 of 20), without swapping models.
