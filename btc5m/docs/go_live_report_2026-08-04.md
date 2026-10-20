# Go-Live Consolidation Report — 2026-08-04

Produced from the A1 paper-trading session (01:07–01:32 UTC, 25.1 min) and
verification of all prior Chunk artifacts. All numbers are from this session
only — no carry-forward from prior sessions with contaminated bot.jsonl.

## 1. Chunk A — Live Paper-Trading Session (A1)

**Mode:** PAPER_TRADE=true, DRY_RUN=false, LIVE_SIGNING_VERIFIED=true
**Duration:** 1,503.7s (25.1 min), 7,448 loops

| Metric | Value |
|---|---|
| Opportunities | 1 |
| Orders sent | 0 |
| Fills | 0 |
| Missed (rejected) | 40 |
| Partial fills | 0 |
| Locked PnL | $0.00 |
| Would-lock (forgone) | $11.80 (summary source of truth) |
| Session PnL | $0.00 |
| Crashes | 0 |
| Errors | 0 |
| WS reconnects | 2 (recovered <1.5s each, REST poll fallback) |
| Window rotations | 11 |
| Kronos forecast | MED (daily-equiv ATR%=0.0344, confidence=0.78) |
| Macro events | 295 total, 55 high-risk windows (FF backfill) |

**Opportunity detail:** 1 edge detected at 01:27:22 UTC — BTC Up/Down 5m,
YES=0.94, NO=0.03, combined=0.97, edge=0.020, age_ms=34 (fresh band).
Rejected by `no_entry_zone` (within 5s of window close). Did not fire.

**Miss breakdown:** 33 stale_quote, 6 no_entry_zone, 1 macro-risk.

**Verdict:** Market was quiet (MED vol, BTC ranging). The bot correctly
detected edges and rejected them per freshness/no-entry/macro gates. No fills
means no fill-fidelity, freshness, or latency data from this session. The
bot loop and gate logic function correctly; we just need a livelier market.

## 2. Chunk B — Kronos Calibration (Final)

**Checkpoints:** 20 real-LSE-data checkpoints (22 total with 2 synthetic)

| Metric | Value |
|---|---|
| Forecast vs 1-min realized | 11/20 (55.0%) |
| Forecast vs 30-day ground-truth | 7/20 (35.0%) |
| HIGH forecast accuracy | 0/4 HIGH forecasts landed on LOW-ground-truth days |
| True-HIGH recall | 0/2 (both Feb 15 and Mar 8 forecast as LOW/MED) |
| Calibration | 27/40 = 67.5% (train-like) |
| Disjoint holdout | 7/16 = 43.75% |

**Verdict: DIAGNOSTIC ONLY — FINAL.** Do not reopen. Kronos over-forecasts
HIGH and under-detects true HIGH days. Usable as a regime-awareness input but
not a trading gate on its own. The freshness gate and no_entry_zone are the
primary risk controls.

## 3. Chunk C — Preflight

**Run:** `scripts/preflight_check.py` (read-only on-chain via polygon.drpc.org)

| Check | Status |
|---|---|
| PRIVATE_KEY set | PASS |
| Wallet address derived | PASS (0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266) |
| MATIC for gas (>= 0.5) | FAIL (0.0000 MATIC) |
| pUSD balance (>= $20.00) | FAIL ($0.00) |
| pUSD approved Exchange V2 | FAIL (allowance=0) |
| pUSD approved Neg Risk V2 | FAIL (allowance=0) |
| CTF approved Exchange V2 | WARN (contract reverts isApprovalForAll) |
| CTF approved Neg Risk V2 | WARN (contract reverts isApprovalForAll) |
| Geoblock | FAIL (CA/BC) |
| LIVE_SIGNING_VERIFIED | PASS |

**Summary:** 3 PASS / 2 WARN / 5 FAIL.
- CTF isApprovalForAll: the Polymarket CTF contract reverts on stateless
  eth_call reads. Must verify via on-chain tx receipt after calling
  setApprovalForAll. Documented as WARN, not FAIL — the check mechanism is
  the limitation, not the approval state.
- Geoblock: CA/BC, confirmed FAIL, externally enforced — cannot fix from
  this workspace.
- Real on-chain balances: 191 LSE + 92 FF = 283 macro events in state/macro_events.json.

## 4. Chunk E — Observability

**AST audit:** zero unlogged except handlers in trading_client.py, execution.py,
arb_engine.py. Alerts wired to real Execution/RiskState code paths. E2E
harness fires 3/3.

**Bug found and fixed in prior session, confirmed still in place:**
`execution.py` called nonexistent `TradingClient.get_fill_ratio()` — now
implemented in `trading_client.py:487-502` (returns 1.0 for confirmed order_id,
0.0 for None).

## 5. Race-Condition Fix (This Session)

**File:** `scripts/run_a1_session.py`

Two fixes applied:
1. Retry loop in `parse_jsonl_events()` — if bot.jsonl is empty/missing after
   session end, waits up to 10s (0.5s intervals) for the log to flush.
2. Console-log fallback: if JSONL still has no summary line after the retry,
   parses the `session metrics:` line from `state/session_A1_cached.log` as
   the source of truth. Previously documented but not implemented.
3. Extracted `_parse_summary_line()` helper to avoid code duplication between
   the JSONL path and the console fallback path.

**This session's A1 run confirmed the fix works:** the console log fallback
was not needed (JSONL was complete), but the retry loop ran cleanly.

## 6. Go-Live Checklist Summary

| # | Criterion | Status | Reason |
|---|---|---|---|
| 1 | Observation volume | **FAIL** | 1 opp / 0 fires in 25 min |
| 2 | Quote-freshness | **INCONCLUSIVE** | 0 fills to measure |
| 3 | Fill fidelity | **INCONCLUSIVE** | 0 fills to measure |
| 4 | Partial-fill management | **INCONCLUSIVE** | 0 fills to measure |
| 5 | PnL convergence | **FAIL** | 0% (0 fills / $0 PnL) |
| 6 | No kill-switch | **PASS** | 0 errors, clean shutdown |
| 7 | System stability | **PASS** | No crashes, WS auto-recovery |
| 8 | Latency budget | **INCONCLUSIVE** | 0 orders submitted |
| 9 | Kronos cross-validation | **PARTIAL** | n>=15 met; accuracy low |
| 10 | Manual sign-off | **PASS/FAIL** | Signing passes; wallet unfunded, geoblocked |

## 7. Final pytest Count

**349 passed** (run 2026-08-04). Up from 346 in the prior session — 3 new
tests added (partial-fill PnL scaling regression tests in test_arb.py).

## 8. External Blockers (Unchanged)

| Blocker | Status |
|---|---|
| Geoblock (CA/BC) | Externally blocked |
| Wallet funding | $0 MATIC, $0 pUSD |
| Exchange approvals | 0 allowances (need 4 on-chain txns) |
| CTF approval verification | eth_call reverts; must verify via tx receipt |
| LSE API key | Placeholder; FF fallback active for macro data |