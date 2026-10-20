# Hardening Pass — 2026-08-12

Scope: six backend modules in `polymarket_arb` (PAPER_TRADE / dry-run only; all
numbers below are real values from the live paper run). Each task below lists
root cause -> fix -> test evidence.

Caveats that shaped this pass:
- `scrapers/page_agent_runner.py`, `polymarket_arb/risk.py`,
  `polymarket_arb/window_tracker.py` were modified by another session
  (Aug 12 02:46-02:47) — treated as do-not-touch; tests must interoperate
  without editing them.
- `state/macro_events.json` now exists (144KB, 284 real LSE events, scraper ran
  Aug 12 02:43). Conftest's autouse patch `_read_macro_state -> lambda: None`
  made `risk.reserve()` interpret "file exists but read returned None" as
  corruption -> `_macro_state_error` set -> EVERY reserve failed closed with
  `macro risk state unreadable` -> every fire silently "missed". The suite only
  survived because `test_macro_risk.py` pre-populated the module `_macro_cache`,
  bypassing the disk read. Standalone execution tests hit the fail-closed path.
  **Fix (`tests/conftest.py`): the patched reader now returns a legitimately
  EMPTY `MacroEventState.from_dict({"events": []})` — semantically "no macro
  data", never mistaken for corrupt.** `_macro_cache` injection still works.

## TASK 1 — `monitoring/watchdog.py` (spawn watchdog)
Root cause: single-run watchdog died on transient supervisor failures; no retry,
no disk fallback for alert/status persistence.
Fix: exponential backoff + bounded retries; JSONL fallback sink; flush queue on
shutdown.
Evidence: `tests/test_watchdog.py` — 26 tests pass.

## TASK 2 — paper vs live execution mode
Root cause: `TradeRecord` had no execution-mode tag; mixed-mode sessions (dry_run
+ paper) couldn't be summarized correctly, and per-mode fill accounting was
missing.
Fix: `TradeRecord.mode`; per-mode order/fill counters (`orders_sent_by_mode`,
`fills_by_mode`); mode-aware summary in `order_ledger`; `mode_of()` /
`get_fill_report()` lookups; legacy `get_fill_ratio()` float contract intact.
Evidence: `tests/test_order_ledger.py:87` updated (expected dict now carries
`"mode": "live"`); new `tests/test_order_mode.py` — 5 tests pass (mixed-mode
summary 2s/1f paper + 2s/1f live + 2s/1f dry_run, per-mode records, dry_run /
paper / live ID lookups).

## TASK 3 — `polymarket_arb/kronos_session.py` zero-ATR guard
Root cause: sparse or flat candle windows produced `atr_dollars == 0.0`, then a
0.0-capital regime made the session uninvestable / degenerate downstream.
Fix: guard immediately after `atr_dollars = _compute_atr(candles, period=14)`:
`if atr_dollars <= 0.0:` -> loud `log.warning("ATR heuristic produced zero ATR
from {len} candles ...")`, conservative MED regime (5.0s, 500ms quote cap,
confidence 0.5, `source="sparse"`), `SESSION_VOL_REGIME="MED"`, and an early
return that skips Kronos inference entirely.
Evidence: 4 new tests in `tests/test_kronos_session.py` (sparse <15 candles,
flat zero-range series, warning emitted via `patch.object(ks.log, "warning")` —
custom logger does not propagate to caplog, Kronos inference skipped). 107 tests
pass across kronos/watchdog/order_mode/order_ledger/arb/macro_risk.

## TASK 4 — fire debounce invalidation on zero exposure
Root cause: when a fire produced ZERO exposure (sibling-submit failure, or
no-fill timeout cancelling both legs), the per-market combined-price debounce in
ArbEngine stayed set — a book returning to the same combined price would never
re-fire and the arb was silently lost.
Fix:
- `polymarket_arb/arb_engine.py`: new `invalidate(market_id)` pops
  `_last_fired[market_id]`.
- `polymarket_arb/execution.py`: `_invalidate_fire` callback +
  `set_invalidator(cb)` + `_notify_no_exposure(market_id)`, invoked at BOTH
  zero-exposure paths: sibling-failure cancel (after cancellation confirmed +
  unreserve) and no-fill timeout (after both cancels + committed-cost release).
- Wired post-construction in `main.py` and `backtest_engine.py` (engine is built
  after Execution; callbacks avoid import cycles).
Evidence: 3 new tests in `tests/test_arb.py` (39 pass total; +11 across
backtest replay/validate): per-market debounce cleared, `invalidate` is
per-market (`dataclasses.replace` on `_state`), and a full integration loop with
a never-fill client: fire -> no-fill timeout -> cancel -> debounce cleared ->
same price re-fires and resolves (cancelled == 4).
Note: a self-introduced edit gluing a `return` and metrics line together in
`arb_engine.py:97` was caught by the suite and repaired immediately.

## TASK 5 — LSE API key sentinel hygiene
Root cause: the placeholder key `lse_live_00000000000000000000000000000000` was
inlined as a bare default in `config.py:142`; no named constant, no startup
warning when an operator deploys without `LSE_API_KEY`.
Fix (design decision): NOT a hard startup error — that would break
page_agent_runner's anon-key fallback (compares `_LSE_API_KEY ==
_LSE_PLACEHOLDER_KEY` at `scrapers/page_agent_runner.py:70,395,526-533`) and
macro-data population. Instead:
- `config.py`: module constant `LSE_API_PLACEHOLDER_KEY` (canonical sentinel,
  asserted equal to `page_agent_runner._LSE_PLACEHOLDER_KEY` in tests);
  `lse_api_key: str = os.getenv("LSE_API_KEY", LSE_API_PLACEHOLDER_KEY) or ""`.
- `main.py`: `_warn_placeholder_lse_key()` — loud `log.warning` naming
  `LSE_API_KEY` and pointing at `docs/lse_api_key_acquisition.md` when the key is
  unset or still the placeholder; silent for real keys. Called as the first
  action of `main()` after `_setup_logger()`.
Evidence: new `tests/test_config_lse_key.py` — 6 tests (constant exported and
equal to page_agent's, unset-env default == sentinel, SETTINGS never empty,
placeholder warns, unset warns, real key silent). 59 tests pass with
`test_page_agent_runner.py`. Repairs during verification: a glued
`def _setup_logger():    global log` (syntax error on import) and a test reading
`.default_factory` on a plain-expression field (must read `.default`).

## TASK 6 — fault-injection chaos harness
New `tests/test_fault_injection.py` — 7 scenarios driving the REAL
arb_engine -> execution -> risk pipeline through hostile conditions; every
scenario asserts zero unhandled fire/probe exceptions (patches both H1/E1
`_log_task_error` sinks), no leaked reservations (`pending_deployed == 0`), and
no orders after a blocking gate:
1. WS drop mid-fill-probe — `ConnectionError` from `is_filled` contained
   (execution.py:605/611), entry stays pending, resolves after feed heals.
2. Stale-quote flood — gated as `stale_quote` misses, never fired.
3. Partial one-leg fill — YES fills / NO rests -> cancel + hedge/exit -> flat
   position, capital released, record written.
4. Book moves between gate and submit — snapshot prices win (C1), orders go out
   at the gated prices, snapshot edge re-asserted.
5. Kill-switch — `halted=True` blocks the fire at the reserve gate.
6. Daily-kill-switch breach — alert persisted to the alerts.jsonl sink
   (kind `daily_kill_switch`, message `DAILY_KILL_SWITCH_USD cap hit...`), fire
   blocked.
7. Multi-fault session — fire + probe drop + heal + clean fill + no-fill
   timeout: ends flat, capital fully released, both legs cancelled.

## Final suite status
Full run (Aug 12): **490 passed, 6 failed (222.52s)**. The 6 failures are all
`tests/test_supervise_bootstrap.py::TestBootstrapLaunchd` —
`subprocess.TimeoutExpired` (30s) on
`~/scripts/supervise_depth_capture.sh` (a zsh script OUTSIDE this
repo that loops `capture_depth_ws.py` forever, killed -9). The test file was
modified Aug 12 08:57 by another session and the script imports none of the
modules touched here — environmental, not caused by this pass.
