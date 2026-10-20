# TASK A LightGBM Diagnostic

`scripts/diagnose_lightgbm_macro_regimes.py` is an offline diagnostic only. It has no trading, production configuration, cross-venue, or Kronos integration points.

## Inputs and labels

- Candles are fetched exclusively from LSE as `BTC/USD`, `1m`, with raw `open`, `high`, `low`, `close`, and `volume` fields.
- The only calendar input is `state/macro_events.json`. The runner retains HIGH-impact timestamps only; it neither emits nor models titles or descriptions.
- No candle is resampled, forward filled, backfilled, interpolated, or generated. Observed gaps are counted in the artifact.
- The target is 15-minute forward realized volatility: the square root of the sum of squared next one-minute log returns, in basis points. LOW/MED/HIGH thresholds are the train partition's 33.3% and 66.7% quantiles, then applied unchanged to validation and both holdouts.

## Features and split

The model receives OHLCV, log volume, one-minute return, 5/15/60-minute rolling volatility, 14/60-minute ATR, minutes to the next HIGH event, and a UTC session bucket. Missing future HIGH events remain missing values, which LightGBM handles directly.

The chronological split is train 55%, validation 15%, holdout A 15%, and independent holdout B 15%. A run needs at least 100 eligible real rows in every partition. Otherwise it exits with status `insufficient_history`, writes the exact shortfall to `state/lightgbm_task_a_diagnostic.json`, and does not train.

## Running and interpreting

```bash
python scripts/diagnose_lightgbm_macro_regimes.py
```

Use explicit UTC dates when reproducing a run:

```bash
python scripts/diagnose_lightgbm_macro_regimes.py --start 2026-08-01 --end 2026-08-04
```

The state artifact records the installed LightGBM API/version, hyperparameters, `pip check`, raw history coverage, partition bounds, training/validation/holdout metrics, the top ten gain importances, and each holdout's accuracy difference in percentage points against the Kronos 43.75% reference. It makes no claim of deployability or causal macro effect.
