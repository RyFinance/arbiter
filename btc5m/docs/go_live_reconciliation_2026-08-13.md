# Go-Live Chain Reconciliation — 2026-08-13 (addendum, dated)

This is an ADDENDUM to the authoritative checklist
[`go_live_checklist_2026-08-11.md`](go_live_checklist_2026-08-11.md) and the
final report's 10-item F3 table
[`go_live_final_report.md`](go_live_final_report.md). Nothing in the prior
artifacts was modified. This document records what changed on the night of
2026-08-12/13 after Codex's zero-orders, PnL-convergence, and kill-switch
re-verification work, and re-runs the same 10-item methodology with fresh,
real numbers.

Scope guards honored: `execution.py`, `backtest_engine.py`,
`capture_depth_ws.py` were READ ONLY (no edits). `polymarket_arb/risk.py`
remains do-not-touch (another session owns it).

## What changed tonight (Codex + my hardening pass)

| Change | Where | Verified state tonight |
|---|---|---|
| Zero-orders guards (missing-price bail, snapshot edge re-assert, zero-liquidity rejection, zero-exposure debounce invalidation) | `execution.py:291,:310,:405,:718`, `arb_engine.py` (`invalidate`), `backtest_engine.py:190,:333` | Guard paths present; `test_arb.py` 39/39, `test_pnl_invariants.py` 16/16 PASS. **Residual metric bug still live — see finding 1.** |
| LSE key sentinel + startup warning | `config.py`, `main.py` | Live in tonight's 08:01 PDT bot run (`LSE_API_KEY is the placeholder sentinel` warning observed in `state/bot.jsonl`). |
| Kill-switch re-verification (deliberate trip) | paper harness session 2026-08-13 08:01 PDT | Halt + `ALERT[kill_switch]` persisted to `state/alerts.jsonl` (1 record). Mechanism VERIFIED. Harness test RED — see finding 2. |
| New regression tests | `tests/test_silent_failure_guards.py` (3), `tests/test_pnl_invariants.py` (16) | All PASS tonight. |

## Finding 1 — orders_sent still counts rejected submits (zero-orders accounting)

Empirical experiment (read-only, real code path, rejecting client):

```
client submits attempted: 2
real order ids placed:    0
metrics.orders_sent:      2
metrics.orders_sent_by_mode: {'dry_run': 0, 'paper': 2, 'live': 0}
metrics.missed:           0
```

`execution.py:411` does `orders_sent += 2` unconditionally after the
sibling-exception branch, so a fully rejected fire (both `submit_order` return
None) counts as two orders sent. This inflates `orders_sent`/`fills` ratios and
skews any convergence math built on them. The Aug-5 session already showed the
symptom: opps=164, sent=280 (=140 pairs) → 24 opportunities produced zero
orders. Guard paths for the no-ids case exist, but the metric itself is not
fixed. (Recommended fix, for the owner of `execution.py`: count `orders_sent`
per returned id, and add a `rejected` counter.)

## Finding 2 — kill-switch stress harness is RED; root cause = macro fail-closed

`tests/test_execution_stress.py::test_stress_runner_proves_sigkill_alert_persistence`
FAILED tonight (evidence `pass: false`). Reproduced directly:

```
alerts.counts: {kill_switch: 0, repeated_rejections: 0, ws_disconnect_severe: 1}
partial_unwind.explicit_unwind_submitted: false
kill_switch.submits_before_halt: 0
```

Sequence from the worker archive `alerts.jsonl`:
`macro_state_failure` x2 at startup → all entries `missed` → no submits ever →
partial-unwind scenario never ran → expected fault alerts (`kill_switch`,
`repeated_rejections`) never generated.

Root cause: `scripts/run_execution_stress_session.py:129` patches
`risk_mod._read_macro_state` to `lambda: None`. Since `state/macro_events.json`
NOW EXISTS (82,923 bytes, 540 events, populated 2026-08-13 08:01), `risk.py:133`
interprets "file present but read=None" as corruption → `_macro_state_error =
"macro state unavailable"` → every reserve fail-closed. This is the identical
interaction fixed for pytest in `tests/conftest.py` (patch must return an empty
`MacroEventState.from_dict({"events": []})`, not `None`). The harness
predates the macro file's existence; it has been broken since the file first
appeared (2026-08-12 02:43). Fix belongs to the harness owner: same empty-state
patch.

## 10-item reconciliation (F3 methodology) — 2026-08-13

Legend: same = no new evidence, prior status stands; re-verified = fresh
evidence tonight; degraded = regression observed.

| # | Criterion | Target | Last check (08-11) | Tonight (08-13) | Status |
|---|---|---:|---|---|---|
| 1 | Observation volume | ≥100 fires | PASS (140 fills/164 opps) | 0 fires — session halted at startup for kill-switch verification; no new sample | same |
| 2 | Fresh quote safety | ≥95% ≤500ms | FAIL (85.7%, 120/140) | 0 fills → no sample | same |
| 3 | Fill fidelity | ≥90% both legs (live) | FAIL (sim-only 100%) | no live/paper fills; chaos harness 7/7 synthetic paths pass | same |
| 4 | Partial management | <2%, residual <$25 | PASS (0 partials, $0.00) | no fills; `test_fault_injection.py` partial unwind ends flat, capital released | same |
| 5 | PnL convergence | ±10% | FAIL (38.93%: $55.68 vs $143.02) | no new fills → no new sample. **Trap:** `calc_pnl_convergence.py` fallback now prints "100.00% PASS" when per-trade data is absent (expected := locked by construction) — do not cite it | same (FAIL) |
| 6 | No kill-switch | 0 events healthy + mechanism works | PASS (0 events, 135-min) | **Re-verified (mechanism):** deliberate trip 08:01 PDT → `RISK HALT` CRITICAL + `ALERT[kill_switch]` persisted (1 record in `state/alerts.jsonl`). Healthy-session 0-events claim: no healthy session tonight | re-verified (mechanism) |
| 7 | System stability | no crashes | PASS (56,510 loops, 1 auto-recovered reconnect) | session lived ~1s (deliberate halt); no stability sample | same |
| 8 | Latency budget | p95 <250ms end-to-end | FAIL (opp→order avg 746.8ms; det→fire p95 1.74ms) | 0 orders → no sample | same |
| 9 | Kronos cross-val | ≥7 checkpoints | PASS count 64 / FAIL accuracy 0.70 | artifacts unchanged (2026-08-03); tonight's session applied regime MED | same |
| 10 | Manual sign-off | funded + approved + allowed | FAIL | **Re-verified with real RPC tonight:** `preflight_check.py` → 3 PASS, 2 WARN, 5 FAIL — 0.0000 MATIC, $0.00 pUSD, pUSD allowances 0/0 (Exchange V2 + Neg Risk V2), geoblock blocked (CA), LIVE_SIGNING_VERIFIED=true, CTF approvals WARN (eth_call inconclusive by design) | re-verified (FAIL) |

**Net: 0 of 10 criteria improved to pass tonight. Items 5, 8, 9, 10 remain
FAIL; items 1, 2, 3, 7 have no fresh sample; item 6 mechanism re-verified
while its healthy-session half lacks a sample.**

## Test stability tonight

Batch of Codex-regression areas: `test_metrics_scripts.py`,
`test_latency_telemetry.py`, `test_pnl_invariants.py`,
`test_silent_failure_guards.py`, `test_execution_stress.py` →
49 passed, 1 failed (the stress-runner, finding 2).
My hardening areas: `test_watchdog`, `test_order_mode`, `test_order_ledger`,
`test_kronos_session`, `test_arb` (39), backtest replay/validate,
`test_fault_injection` (7), `test_config_lse_key` → 176 passed.
`TestBootstrapLaunchd`: 5 failed, 2 passed (see finding 3 — separate report).
Full-suite number at the time of writing was 510 passed / 6 failed; a fresh
full run was started during this reconciliation (results appended in the
change log below if the process completed; the machine's disk was under heavy
load from 6 rogue `capture_depth_ws.py` writers during it).

## Depth gate (checklist item 11) — data availability changed

Forward capture now EXISTS: `state/cache/polymarket_depth_capture.2026-08-13.1.jsonl`
(406,914,557 bytes) plus two Aug-12 shards (524MB, 465MB). Integrity report
(`state/cache/integrity_report.json`, generated 2026-08-13 07:57 PDT): 195
artifacts → 129 MISSING-PRODUCER, 6 EMPTY, 25 STALE, 18 FRESH, 17
UNAUDITABLE. The 30 independently-resolved 5-minute-window validation
criterion is still unproven; the capture pipeline itself is currently the
subject of an active rogue-process incident (see finding 3).

## Findings carried forward

1. `orders_sent += 2` inflates on full rejection (execution.py:411).
2. Stress harness RED: `_read_macro_state` patched to None + real macro file →
   fail-closed (`run_execution_stress_session.py:129`; fix = empty-state patch,
   same as conftest).
3. `TestBootstrapLaunchd` 5/5 RED: `try_bootstrap_launchd()` contract never
   implemented in `supervise_depth_capture.sh` (script is a bare infinite
   restart loop, ignores `BOOTSTRAP_ONLY`); plist exists + lints but is NOT
   loaded in launchd (`launchctl print gui/501/com.rytty.depth_capture` →
   "Could not find service"). Live at write time: 6 orphaned
   `capture_depth_ws.py` (PPID=1, started 08:00-08:05 PDT) + 1 fresh
   supervisor (08:05). Claude Code owns the fix; this session touched none of
   it.
