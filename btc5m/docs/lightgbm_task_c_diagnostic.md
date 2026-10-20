# TASK C LightGBM Continuation Diagnostic

`scripts/diagnose_lightgbm_macro_regimes.py` remains an offline diagnostic. It does not modify trading, production configuration, cross-venue code, transactions, or Kronos.

## Data and labels

With no date flags, the runner asks LSE for its unbounded `BTC/USD` `1m` vault response and records the returned row count and observed UTC bounds. The LSE response can cap rows, so the artifact records both the requested limit and observed range; it does not infer unseen history. Supplying both `--start` and `--end` performs a reproducible bounded UTC request.

Only observed LSE OHLCV rows are used. The diagnostic never resamples, pads, forward fills, interpolates, or creates candles. It constructs LOW/MED/HIGH 15-minute forward-realized-volatility classes using only the training partition's 33.3% and 66.7% quantiles.

## Evaluation

Rows are ordered chronologically into train (50%), validation (12.5%), holdout A (12.5%), holdout B (12.5%), and holdout C (12.5%). The three holdouts are mutually disjoint. The artifact reports class counts and whether all three classes appear in every split; absence of a class is reported as evidence, never corrected with synthetic data.

The model is compared with a train-only majority-class baseline. Gain importance is paired with fixed-seed, 20-repeat permutation importance evaluated only on validation. Each split carries correct-row counts, accuracy, macro F1, confusion matrix, and model-minus-baseline accuracy. Kronos is not invoked; the artifact compares every holdout with the fixed 43.75% reference and reports holdout mean, standard deviation, range, and whether every holdout exceeds that reference.

## Running

```bash
python scripts/diagnose_lightgbm_macro_regimes.py
```

For an explicit bounded run:

```bash
python scripts/diagnose_lightgbm_macro_regimes.py --start 2017-08-17 --end 2017-08-23
```

The diagnostics-only artifact is `state/lightgbm_task_c_diagnostic.json`.

The current unbounded-vault attempt is retained even when it cannot train. A separately named bounded artifact may be used to preserve diagnostics from an observed contiguous interval; it is not a substitute for the full-vault result.

## Current verdict (2026-08-04 UTC)

`state/lightgbm_task_c_recent_contiguous_diagnostic.json` is the current bounded,
real-data result. It fetched **4,783 contiguous observed LSE BTC/USD 1-minute rows**
from `2026-08-01T00:00:00Z` through `2026-08-04T07:42:00Z`, with zero gaps over one
minute, no resampling/padding, and 4,708 eligible feature rows. The chronological
partitions were 2,354 train, 588 validation, and 589/588/589 for holdouts A/B/C.

The 10-iteration model has **no usable signal**: validation accuracy was 25.510204%
(macro F1 0.164365; log loss 1.076616), while holdouts A/B/C were 29.541596%,
22.619048%, and 19.864177% accuracy with macro F1 0.196451, 0.145547, and 0.123871.
Their mean accuracy was 24.008274%, range 9.677419 percentage points, and standard
deviation 4.071083 percentage points. Every holdout was below the fixed 43.75%
reference by 14.208404, 21.130952, and 23.885823 points respectively; holdout B also
lacked the LOW class. The model is therefore **not deployable**, no production,
trading, cross-venue, or Kronos path is affected, and **no further tuning is
authorized on this data slice**. More data may justify a new diagnostic, not a
retrospective parameter search.
