# Chunk E — Walk-Forward and Retraining Policy

**Date:** 2026-08-11  
**Scope:** Offline BTC volatility-regime/arbitrage research only.  
**Status:** Design note; no scheduler or production wiring is included.

## Evidence reviewed

- `state/lightgbm_task_c_recent_contiguous_diagnostic.json` (generated 2026-08-04): 4,783 observed LSE `BTC/USD` 1-minute rows from 2026-08-01 00:00 UTC through 2026-08-04 07:42 UTC; zero gaps over one minute; zero synthetic rows; 4,708 eligible feature rows.
- The same artifact’s chronological partitions: train 2,354, validation 588, holdout A 589, holdout B 588, holdout C 589.
- `state/lightgbm_task_c_recent_contiguous_diagnostic.json`: validation accuracy 25.510204%; holdout A/B/C accuracy 29.541596% / 22.619048% / 19.864177%; holdout mean 24.008274%, standard deviation 4.071083 percentage points. All three holdouts were below the fixed 43.75% Kronos reference by 14.208404, 21.130952, and 23.885823 percentage points. Holdout B lacked LOW labels.
- `state/lightgbm_task_c_diagnostic.json`: an unbounded LSE response returned 5,000 rows but only 93 eligible feature rows after gaps/warm-up, so the required five-way evaluation was correctly marked `insufficient_history`; observed gaps over one minute: 993. This demonstrates that provider row caps and continuity must be measured, not inferred away.
- `FIXED_TRAINING_REPORT_V2.md`: corrected historical run had holdout sizes 616/615/616, mean accuracy 62.26%, 14.12-point standard deviation, and failed the majority baseline on holdout C by 13.15 points. This is evidence of regime/class-distribution instability, not deployability.
- `scripts/diagnose_lightgbm_macro_regimes.py`: requires chronological disjoint partitions, train-only label quantiles, real observed rows only, and a minimum of 100 eligible rows per partition (500 total); it reports baselines, class presence, gaps, and holdout metrics.

## Proposed walk-forward protocol

1. Acquire a bounded UTC interval of observed LSE `BTC/USD` 1-minute candles. Record requested range, returned rows, observed bounds, gaps, and synthetic-row count. Reject any run that resamples, pads, forward-fills, interpolates, or silently treats a capped response as complete.
2. Construct the 15-minute forward-realized-volatility target only after ordering timestamps. Split chronologically into train, validation, and at least three mutually disjoint future holdouts. Fit LOW/MED/HIGH cutoffs on train only and freeze them for validation and holdouts.
3. Use validation only for model selection/early stopping. Keep holdouts untouched until the candidate policy is frozen. Re-run the frozen candidate across each holdout and report accuracy, macro F1, log loss, confusion matrix, class counts, majority-baseline delta, and the fixed Kronos reference delta.
4. Repeat this as rolling origins: advance the origin by a fixed calendar block, append only data that was available at that origin, refit train-only cutoffs, and evaluate on the immediately subsequent embargoed block. The 15-minute target horizon implies at least a 15-minute label embargo at each boundary; a wider operational embargo is preferable if feature publication latency requires it.
5. Select a retraining candidate only when performance is stable across origins and classes. A single favorable holdout, aggregate accuracy, or improvement over Kronos alone is insufficient. A candidate must beat the train-majority baseline on every required holdout, retain all three classes where the data supports them, and show no material degradation versus the currently frozen candidate on the newest untouched block.

## Retraining cadence and gates

The design recommendation is event-driven review on newly accumulated data, with a slower periodic research review; the exact calendar trigger is intentionally left to the eventual operator and is not implemented here. A retrain proposal should require:

- a new contiguous, observed-data evaluation window;
- no unresolved data-quality failure (provider cap ambiguity, excessive gaps, or missing class);
- train/validation/three-holdout metrics and majority-baseline comparisons;
- comparison against the frozen candidate on the same newest block;
- reproducible artifact metadata: UTC bounds, row counts, feature schema, quantiles, library versions, seed, and source path.

If any gate fails, retain the existing research candidate and label the run `insufficient_history`, `data_quality_failure`, or `not_better_than_baseline`; do not tune repeatedly on the failed holdout.

## What is defensible now

- Defensible: an offline, chronological diagnostic framework with train-only quantiles, multiple future holdouts, explicit class/baseline metrics, and rejection of synthetic or unverified history.
- Defensible: stating that the current recent 4,708-row eligible slice provides evidence against deployment; its model’s 24.008274% mean holdout accuracy is below the 43.75% reference and is unstable.
- Defensible: requiring substantially more contiguous history and multiple market regimes before making a retraining decision. The existing artifacts show only roughly 3.3 days of contiguous candles in the strongest recent run and a separate capped/gappy 5,000-row response.

## What is not defensible

- Not defensible: claiming a deployable edge, production-safe arbitrage signal, or causal macro effect from these diagnostics.
- Not defensible: treating the superseded 54.08%/66.13%-era headline results as valid; the corrected report documents leakage, a broken test split, and majority-baseline failures.
- Not defensible: declaring a minimum of 50,000 or 100,000 rows a proven sufficiency threshold. Those numbers appear as heuristic recommendations in the superseded report, not as an empirically validated policy. They may be useful acquisition targets, but they do not replace rolling-origin evidence across regimes.
- Not defensible: retraining or selecting hyperparameters against the same holdout repeatedly, or using the fixed 43.75% Kronos reference as the sole acceptance criterion.

This note changes no production, execution, risk, signing, live-trading, Kalshi, cross-venue, model, or state-log file and contains no scheduler implementation.
