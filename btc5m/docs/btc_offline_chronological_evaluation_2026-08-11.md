# BTC offline chronological evaluation — 2026-08-11

`scripts/evaluate_btc_offline_profitability.py` is an offline-only, reproducible evaluation of the causal BTC feature frame and `profitability_target` created by `scripts/diagnose_lightgbm_macro_regimes.py`.

The regenerated JSON artifact is [`docs/research/btc_offline_chronological_evaluation_2026-08-11.json`](research/btc_offline_chronological_evaluation_2026-08-11.json). It is the source of the exact results recorded below.

It takes a local normalized OHLCV parquet file and the local anonymous macro calendar, builds the t-cutoff proxy label, then joins the expanded causal technical features from `scripts/btc_offline_features.py`. The 38 input columns include the original 14 proxy features plus technical features and a numeric `vol_regime`. That regime compares current realized volatility with expanding tertile thresholds calculated from observations strictly before the current bar; it never uses a full-sample rank or quantile.

The evaluator forms one 55% chronological train block followed by three later blocks: holdout A, B, and C. It purges the final 15 rows (at least 15 minutes of label horizon) from every preceding block: train→A, A→B, and B→C. It additionally verifies the remaining final label window ends strictly before the next block begins. No row is shuffled or used in more than one block.

Run it locally with:

```bash
python scripts/evaluate_btc_offline_profitability.py \
  --candles state/btc_1m_candles_lse_v3.parquet \
  --macro-calendar state/macro_events.json \
  --output /tmp/btc_offline_chronological_evaluation.json
```

The JSON artifact records exact sample accounting, UTC bounds, label distributions, and Pearson feature/label correlations calculated from the embargoed training block only. A holdout without every observed proxy-label class is marked invalid, omitted from aggregate statistics, and never used as evidence for sequence-model eligibility.

The first models are sklearn logistic regression and histogram gradient-boosted trees. For each valid holdout, the artifact reports the train-majority naive baseline and model accuracy, AUC, precision, and recall. Aggregate mean, population SD (`ddof=0`), and min/max range are calculated across valid holdouts only.

A sequence model is explicitly skipped unless at least one of those two baselines strictly exceeds its own majority baseline accuracy on every valid holdout. This evaluation does not add a sequence model or any dependency when that condition fails.

PnL is intentionally unavailable. `profitability_target` is a BTC price proxy, rather than an executable opportunity: it does not carry market-specific quotes, order-book depth, fees, fills, or strict-gate decisions. Routing it through the existing arbitrage backtest would invent those inputs or bypass its gates, so this research track makes no PnL claim.

## Recorded local run

The command above was regenerated on 2026-08-11 with `state/btc_1m_candles_lse_v3.parquet`, `state/macro_events.json`, a 15-minute horizon, and 20.0 round-trip-cost bps. It used 139,523 source candles and 139,374 proxy-labelled rows. Of those, 138,568 rows were complete for all 38 causal features, 806 rows were removed for missing features, and 45 rows were removed by the three embargoes, leaving 138,523 evaluated rows. The label classes were `0` and `1`; class `1` is the positive/profitable proxy label. Train selected class `0` as its majority prediction.

| Block | UTC interval (inclusive) | Rows | Class 0 | Class 1 | Valid |
| --- | --- | ---: | ---: | ---: | --- |
| Train | 2026-05-01T03:24:00Z to 2026-06-23T07:57:00Z | 76,197 | 68,392 | 7,805 | yes |
| Holdout A | 2026-06-23T08:13:00Z to 2026-07-07T20:31:00Z | 20,771 | 17,896 | 2,875 | yes |
| Holdout B | 2026-07-07T20:47:00Z to 2026-07-22T08:07:00Z | 20,770 | 18,960 | 1,810 | yes |
| Holdout C | 2026-07-22T08:23:00Z to 2026-08-05T21:08:00Z | 20,785 | 19,348 | 1,437 | yes |

All holdouts contain both classes, so none was invalidated. Metrics below are exact to six decimal places from the saved JSON artifact; the displayed SD is population SD over the three valid holdouts.

| Model / holdout | Accuracy | AUC | Precision | Recall |
| --- | ---: | ---: | ---: | ---: |
| Majority baseline / A | 0.861586 | 0.500000 | 0.000000 | 0.000000 |
| Majority baseline / B | 0.912855 | 0.500000 | 0.000000 | 0.000000 |
| Majority baseline / C | 0.930864 | 0.500000 | 0.000000 | 0.000000 |
| Logistic regression / A | 0.860671 | 0.683943 | 0.353846 | 0.008000 |
| Logistic regression / B | 0.912855 | 0.708308 | 0.000000 | 0.000000 |
| Logistic regression / C | 0.930864 | 0.683517 | 0.000000 | 0.000000 |
| Gradient-boosted trees / A | 0.809061 | 0.608999 | 0.236091 | 0.169739 |
| Gradient-boosted trees / B | 0.912807 | 0.697131 | 0.000000 | 0.000000 |
| Gradient-boosted trees / C | 0.930142 | 0.592706 | 0.000000 | 0.000000 |

| Model | Accuracy mean / SD / range | AUC mean / SD / range | Precision mean / SD / range | Recall mean / SD / range |
| --- | --- | --- | --- | --- |
| Majority baseline | 0.901768 / 0.029349 / [0.861586, 0.930864] | 0.500000 / 0.000000 / [0.500000, 0.500000] | 0.000000 / 0.000000 / [0.000000, 0.000000] | 0.000000 / 0.000000 / [0.000000, 0.000000] |
| Logistic regression | 0.901463 / 0.029767 / [0.860671, 0.930864] | 0.691923 / 0.011588 / [0.683517, 0.708308] | 0.117949 / 0.166805 / [0.000000, 0.353846] | 0.002667 / 0.003771 / [0.000000, 0.008000] |
| Gradient-boosted trees | 0.884003 / 0.053463 / [0.809061, 0.930142] | 0.632945 / 0.045871 / [0.592706, 0.697131] | 0.078697 / 0.111294 / [0.000000, 0.236091] | 0.056580 / 0.080016 / [0.000000, 0.169739] |

The artifact reports all 38 train-only correlations with `profitability_target` (each has 76,197 complete pairs). The largest absolute correlations were `atr_60m_bps` (0.255931), `atr_14` (0.255311), `rolling_vol_60m_bps` (0.251084), `atr_14m_bps` (0.250308), `realized_vol_30` (0.244308), `rolling_vol_15m_bps` (0.228948), `bb_width` (0.207434), and the causal `vol_regime` (0.184800). These are descriptive correlations on the training block, not holdout performance claims.

Sequence-model decision: **skipped**. Logistic regression was below the majority baseline in A and only tied it in B and C; gradient-boosted trees were below it in A, B, and C. Neither baseline strictly beat its own majority baseline on every valid holdout. PnL remains **unavailable** because assessing it would require executable-market data and strict backtest gates that this proxy label does not provide.

## Metric-correction addendum — 2026-08-11

This bounded pass re-used the same 38 causal features, label, three holdouts, embargoes, and fitted model classes. It added average precision (PR-AUC) and selected one F1-maximizing probability threshold from the training block only. No holdout labels were used to choose thresholds, and no new model or feature class was added.

| Model | PR-AUC mean / SD / range | Train-F1-threshold accuracy mean / SD / range |
| --- | --- | --- |
| Majority baseline | 0.098232 / 0.029349 / [0.069136, 0.138414] | 0.901768 / 0.029349 / [0.861586, 0.930864] |
| Logistic regression | 0.190189 / 0.047035 / [0.134638, 0.249651] | 0.843082 / 0.090514 / [0.715950, 0.923118] |
| Gradient-boosted trees | 0.149784 / 0.048279 / [0.083378, 0.196731] | 0.829426 / 0.114474 / [0.658177, 0.920808] |

The logistic model's train-only F1 threshold was **0.127953**; its holdout accuracies A/B/C were **0.715950 / 0.890178 / 0.923118**. The tree threshold was **0.234615**; its holdout accuracies were **0.658177 / 0.909292 / 0.920808**. Both models exceeded the majority PR-AUC on all three holdouts, confirming ranking signal under class imbalance. Neither exceeded majority accuracy on all three holdouts at the single train-calibrated threshold, and both showed large holdout swings.

The sequence-model gate remains **skipped**: the corrected gate requires both PR-AUC above majority PR-AUC and train-calibrated accuracy above majority accuracy on every holdout. The AUC-versus-default-accuracy tension is real and indicates useful ranking information, but it does not establish stable thresholded trading signal. This track is closed; no further threshold cuts or deployment recommendations are made.
