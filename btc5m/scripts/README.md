# scripts/ — Index

Scripts here are operator CLI tools and one-shot investigations/business-logic
verifications — **NOT unit tests; the unit-test tree is `tests/`.** Each entry
below lists a one-line purpose, the operator vs. one-shot classification, and
its state/ surface (what it reads / writes). A leading **★** marks scripts that
write to **production `state/`** files as a side-effect (cache/log writers) —
a hygiene signal for the reader: re-run those deliberately.

---

## Live / session operation

- ★ **run_a1_session.py** — 25-min PAPER_TRADE session using cached .env creds, measuring one-time cold-path auth latency. *Operator CLI.* Reads .env; writes `state/bot.jsonl`, `state/session_A1_cached.log`, `state/paper_session_monitor_aug3.json`.
- ★ **run_execution_stress_session.py** — Controlled PAPER_TRADE stress session driving the real execution/risk/alert/heartbeat paths with a paper order adapter; archives evidence. *Operator CLI.* Reads `state/market_cache.json`; writes `state/paper_session_archives/execution_stress_*/` (bot.jsonl, alerts.jsonl, evidence.json, SHA256SUMS).
- ★ **run_task_2_4_paper_session.py** — Tasks 2-4 paper-only validation run using the normal app process + CLOB WS + REST fallback + paper fill sim; archives results. *Operator CLI.* Writes `state/paper_session_archives/task_2_4_*/` (+ `state/alerts.jsonl` clear).
- ★ **run_latency_profile.py** — Runs PAPER_TRADE ~120s to capture ≥50 FIRE events, parses `EXEC_TIMING` lines, writes per-stage avg/p50/p95. *One-shot.* Reads live `state/session_*.log`; writes `state/latency_profile.json`.
- ★ **paper_trade_v3_gated.py** — Offline gated replay of the persisted TwoStagePredictionModel (v3 variant C); confidence-gates predictions at `--threshold` and logs JSONL. *One-shot.* Reads `state/lightgbm_two_stage_v3.pkl`, candle parquet; writes `state/paper_trade_v3_gated.jsonl` (unless `--no-write`).
- **preflight_check.py** — READ-ONLY pre-flight: verify PRIVATE_KEY derivation, MATIC/pUSD balances, Exchange/NegRisk/CTF approvals, geoblock, LIVE_SIGNING_VERIFIED via `eth_call`. *Operator CLI.* Reads .env + Polygon RPC; writes nothing.
- ★ **e2_alert_test_harness.py** — Fires synthetic triggers for kill_switch / repeated_rejections / ws_disconnect_severe and verifies each lands in `alerts.jsonl`. *One-shot (operator-facing).* Writes real records to `state/alerts.jsonl` (webhook POST mocked).
- ★ **capture_live_depth.py** — Long-running WS subscriber persisting full Polymarket CLOB depth-ladder snapshots (all levels) per book update, with file rotation. *Operator CLI (daemon).* Reads `state/market_cache.json`; writes `state/depth_ladders/<market_id>/<date>.jsonl`.
- ★ **capture_depth_ws.py** — Standalone depth-capture daemon for the active BTC Up/Down 5-min markets; auto-re-resolves token IDs across window boundaries, reconnect/backoff, disk-space monitoring. *Operator CLI (daemon).* Writes `state/cache/polymarket_depth_capture.jsonl`.
- **verify_depth_capture.py** — Completeness report over `state/depth_ladders/`: snapshot counts, per-file spans, gaps >60s, per-token update counts, completeness %. *One-shot.* Reads depth JSONL; writes nothing (stdout/report).

## Data fetch & caching

- ★ **cache_lse_history.py** — Pull & cache max LSE BTC/USD 1m candles + full economic calendar (idempotent, SHA-256/gap sidecars, live API-key probe, partial-failure-safe). *Operator CLI.* Reads SETTINGS.lse_api_key; writes `state/cache/{btc_usd_1m_candles.parquet,economic_calendar.csv}` + `*_meta.json` + `cache_run_log.json`.
- ★ **cache_integrity.py** — Single JSON report across 8 cached sources (state/cache/* + btc_1m_candles_lse_v3.parquet, market_cache.json, macro_events.json, alerts.jsonl): rows, date range, SHA-256, gaps, freshness verdict. *Operator CLI.* Reads cache files; writes `state/cache/integrity_report.json`.
- ★ **fetch_lse_max_history_v3.py** — Date-windowed pagination against the LSE vault /candles endpoint (5000-row cap), reports real total row count + date range; sidecar metadata. *One-shot.* Writes `state/btc_1m_candles_lse_v3.parquet` + JSON report.
- **convert_ticks_to_candles.py** — Convert BTC tick parquet → 1-min OHLCV candles via timestamp-unit detection + resample. *One-shot CLI.* Reads tick parquet; writes parquet output path.

## Backtest & calibration

- ★ **fill_and_test_arb_v3.py** — Honest final ML eval of LightGBM volatility-regime classifier on 138k LSE rows: 3 time-disjoint holdouts, vol-regime annotation, permutation importance, two-stage variant C. *One-shot.* Writes `state/lightgbm_*_v3.{pkl,json}` (does NOT overwrite v2).
- ★ **fill_and_test_arb_v2.py** — Codex-audit-fixed pipeline (5 leakage bugs): train-only cutoffs, three time-disjoint holdouts, removed dead constant feature, prediction_rate vs signal. *One-shot.* Writes `state/lightgbm_*_v2.{pkl,json}`.
- ★ **fill_and_test_arb.py** — SUPERSEDED (v1) Kronos+LightGBM training/cbacktest with known leakage; do NOT use for live. *One-shot.* Writes legacy `state/lightgbm_volatility_model.pkl`, `_scaler.pkl`, `lightgbm_training_metrics.json`, `kronos_training_metrics.json`, `arbitrage_test_results.json`, `btc_1m_candles_lse.parquet`.
- ★ **train_lightgbm_comprehensive.py** — Train LightGBM LOW/MED/HIGH classifier on all LSE data with optional hyperparameter optimization, full metrics dump. *One-shot.* Writes `state/lightgbm_volatility_model.pkl` + `_training_metrics.json`.
- ★ **train_kronos_comprehensive.py** — Train Kronos Transformer (KronosTokenizer + decoder) to predict BTC vol regimes; full data, configurable epochs/days-back. *One-shot.* Writes Kronos model artifacts.
- ★ **calibrate_kronos_chunk_b.py** — Build 20+ real-tick validation checkpoints, run real Kronos-small, grid-search LOW/HIGH ATR thresholds, report old-vs-new match rates. *One-shot.* Reads `state/btc_ticks_real.parquet`; writes `state/kronos_calibration_chunk_b.json`.
- ★ **chunk_b_final_calibration.py** — Merge all checkpoint datasets (spotcheck + threshold_calibration + chunk_b), dedupe to 64 unique rows across 24 dates, three reference label sets, grid-search thresholds. *One-shot.* Writes calibration JSON report.
- ★ **calibrate_kronos_thresholds.py** — Recalibrate ATR regimes from real `btc_ticks_*.parquet` checkpoints only (no synthetic); freezes pre-recalibration baseline for reproducibility. *One-shot.* Writes `state/kronos_threshold_calibration.json`.
- **run_kronos_final_lse_investigation.py** — Single capped Kronos investigation: one fixed calibration/holdout split on real LSE candles, threshold pair derived once, evaluated once date-disjoint. *One-shot.* Reads LSEClient + kronos_session thresholds; writes `state/kronos_final_lse_investigation.json`.
- ★ **spotcheck_kronos_calibration.py** — 20+ real LSE REST-candle checkpoints, real Kronos-small inference, re-tunes HIGH/MED/LOW ATR buckets; realized-vol and 30-day ground-truth labels. *One-shot.* Writes `state/kronos_calibration_expanded.json`.
- ★ **check_kronos_calibration_robustness.py** — Audit skew of the 27-row threshold calibration (regime/date/UTC-hour), then bounded out-of-sample LSE holdout with frozen old vs current thresholds. *One-shot.* Writes solution-state JSON + stdout.
- ★ **regime_backtest.py** — Replay a session's `bot.jsonl` (+ optional archive dir) and annotate each filled/missed event with its active BTC vol regime from candle parquet; per-regime fe/miss summary. *One-shot.* Writes report JSON.
- **validate_backtest.py** — Task B: pull ≥3 days real LSE ticks, convert BTC spot → Polymarket YES/NO probability space, run backtest engine, compare simulated PnL vs paper-session PnL (flag if Δ>5x). *One-shot.* Reads LSE ticks + `state/bot.jsonl`; writes report.
- **evaluate_btc_offline_profitability.py** — Chronological offline eval of the causal BTC proxy-profit label (train block + 3 disjoint future holdouts) using HistGBM/LogReg; no network, no execution. *One-shot.* Reads feature parquet; writes metrics JSON.
- **btc_offline_features.py** — Build causal OHLCV feature frame (no leakage: features at t use only bars through t) + leakage-audited research report; sklearn-safe vol_regime. *One-shot CLI.* Reads candle parquet; writes report file.

## Macro / macro_cross

- ★ **populate_macro_from_ff.py** — Mirror Forex Factory public JSON this-week feed into `state/macro_events.json` so the macro risk gate has real current data; `--check-stale` for cron-health. *Operator CLI.* Writes `state/macro_events.json` (and stale-warning to stderr).
- ★ **populate_macro_from_jblanked.py** — Optional JBlanked-backed macro-calendar fallback (only if JBLANKED_API_KEY set); never writes state unless `--write-state`; `--compare-ff` audit signal. *Operator CLI.* Conditionally writes `state/macro_events.json`.
- **macro_cross_reference.py** — Task D.2: flags HIGH-impact label disagreement between LSE `state/macro_events.json` and FF public JSON (30-min + keyword matching). *One-shot.* Reads `state/macro_events.json`; writes report JSON (default stdout).
- **macro_event_accuracy.py** — Task C: cross-reference HIGH-impact flags vs independent public calendars over 7d window; reports false-positive/false-negative rates. *One-shot.* Reads `state/macro_events.json`; writes configurable `results.json`.

## Diagnostics (latency / pnl / freshness / non-fills)

- ★ **freshness_pnl_report.py** — Task E3: compute dashboard-ready `freshness_pct` (≤500ms fill ratio) + `convergence_pct` (real_locked_pnl / expected_pnl) from `bot.jsonl` and/or session logs. *One-shot.* Writes `state/freshness_pnl_report.json`.
- **calc_quote_freshness.py** — A2: parse `bot.jsonl`/console logs for TRADE events and compute % of fills within the 500ms freshness gate; PASS/FAIL verdict. *One-shot.* Reads `state/bot.jsonl`, session logs; writes nothing.
- **calc_pnl_convergence.py** — A3: real locked_pnl vs theoretical expected_pnl (1-combined × size summed); targets within ±10%. *One-shot.* Reads `state/bot.jsonl`; writes nothing (stdout).
- **latency_report.py** — Permanent operational post-hoc per-stage latency telemetry (avg/p50/p95 per stage + freshness band, any date range) from `stage_timings` in bot.jsonl. *Operator CLI.* Reads `state/bot.jsonl` / archive; writes report JSON.
- **analyze_nonfills.py** — A4: decompose a session's misses (stale_quote / no_fill_in_window / snapshot_no_edge / order_rejected / partial-resolved / window). *One-shot.* Reads `state/session_*.log` + `bot.jsonl`; writes nothing.
- **analyze_feed_drift.py** — Run LSE WS vs CLOB WS simultaneously for N seconds, log ticks to JSONL, compute feed-drift report (simulated mode supported). *Operator CLI.* Writes JSONL + drift report JSONL under `--out-dir` (state/).
- ★ **report_paper_sessions.py** — Summarize paper-session archives' bot.jsonl without treating paper fills as live proof; loop/opps/fills/missed/locked_pnl + percentile parsing. *One-shot.* Reads archive JSONL; writes `state/paper_session_aggregate_report.json`.
- ★ **diagnose_lightgbm_macro_regimes.py** (Codex-owned) — Diagnostic-only LightGBM volatility-regime experiment; no production imports beyond cold-path LSE client, no Kronos, no cross-venue. *One-shot.* Reads LSE candles; writes `state/lightgbm_task_c_diagnostic.json`.

## Spot-checks & adversarial

- ★ **spotcheck_kronos.py** — Re-run regime classification (Kronos or ATR fallback) at 5 historical timestamps, fetch following-session candles, compute realized ATR, compare vs forecast. *One-shot.* Writes `state/kronos_spotcheck.json`.
- ★ **spotcheck_kronos_6_7.py** — Validation checkpoints 6 (HIGH, modeled on Black Thursday) and 7 (LOW, calm summer) on seeded *synthetic* 1-min candles fed to REAL Kronos-small; completes 7/7 set. *One-shot.* Writes `state/kronos_checkpoints_6_7.json`.
- **adversarial_validate_mistral_v1.py** — Adversarial independent repro of the reported 66.13% LightGBM accuracy claim (v1 SUPERSEDED): faithful v1 repro + corrected chronological eval on 5k and 139k rows + leakage audit + verdict. *One-shot.* Reads candle parquet + `state/lightgbm_training_metrics.json`; writes `/tmp/adversarial_validation_results.json` (NOT in state/).

---

## Underlying scripts ownership

- **`scripts/diagnose_lightgbm_macro_regimes.py`** is owned by **Codex** — do not modify without coordinating with the Codex-side author. All edits to this file must come through Codex (or via a coordinated PR), not direct edits in this OpenCode workspace.
- Any scripts `[camel]`-style references under **`dashboard/`** are owned by **OpenCode** — the current `scripts/` tree contains no such files (verified by Glob + grep on 2026-08-11). If a `dashboard/`-owned script is added to `scripts/`, it must be marked "OpenCode-owned" here and excluded from Codex-side bulk refactors.

(Inventory compiled from `ls scripts/*.py` — 44 files total. The user's spec listed ~41 names; this index adds 2 scripts found in the dir but unlisted-by-spec (`adversarial_validate_mistral_v1.py`, `capture_depth_ws.py`) and the 2 new Kilo-authored scripts called out in the task (`cache_lse_history.py`, `cache_integrity.py`). Cross-check: `comm` between README `.py` references and `ls scripts/*.py` returns zero diff.)
