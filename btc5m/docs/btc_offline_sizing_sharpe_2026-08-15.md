# BTC confidence-weighted sizing vs binary threshold — full PnL + Sharpe — 2026-08-15

## Verdict

Confidence-weighted and probability-proportional position sizing do **not**
beat the binary F1-threshold baseline, and no policy turns the BTC proxy
signal profitable. Every model × policy combination has negative PnL in all
three chronological holdouts **and** a negative pooled Sharpe. The earlier
finding is now extended with Sharpe ratios and a fourth sizing policy
(size equal to predicted probability):

- **Binary F1 baseline:** pooled PnL `-14,395.16` bps (logistic) and
  `-12,500.63` bps (GBT); pooled per-trade Sharpe `-0.595` and `-0.605`
  (annualized `-111.4` / `-113.3`).
- **Top-decile:** improves pooled PnL for logistic (`-3,537.89` bps) but is
  worse for GBT (`-13,004.28` bps); Sharpe `-0.405` / `-0.618`. All
  per-holdout PnL negative.
- **Confidence-weighted (rescaled):** reduces loss only through reduced
  exposure (38.4 / 137.0 units vs 747 / 669). PnL per unit exposure stays
  negative (`-14.22` / `-18.78` bps); pooled Sharpe `-0.140` / `-0.354`
  (annualized `-26.2` / `-66.3`).
- **Probability-proportional (new):** `-5,488.52` bps (logistic) and
  `-7,852.70` bps (GBT) pooled; Sharpe `-0.387` / `-0.307`. Worse than
  binary per unit exposure for GBT (`-19.32` vs `-18.69`) and effectively
  tied for logistic (`-19.17` vs `-19.27`).

**Decision:** the sizing variants are not added to `BacktestEngine` as a
strategy mode. The documented requirement for adding a mode is beating the
binary approach on real holdouts; every variant loses money in every
holdout, and where a variant's absolute loss is smaller, the reduction is
explained by lower exposure, not better per-trade economics.

## Frozen protocol

Unchanged from the published evaluation; the three original policies and
every published number reproduce exactly on the frozen inputs:

- Models: scaled logistic regression and histogram gradient-boosted
  classifier (`RANDOM_SEED=20260811`), fitted on the first 55%
  chronologically (`76,197` retained train rows).
- Holdouts: three disjoint future blocks (`20,771` / `20,770` / `20,785`
  retained rows), 15-row + 15-minute embargo at every boundary (`45` rows
  removed).
- Calibration frozen from fitted-train predictions only
  (logistic F1 threshold `0.12795263383358718`, q90 `0.2108908741559322`;
  GBT `0.23461459380404626`, q90 `0.22576412402061702`).
- Positions: enter only while flat, hold 15 minutes, next entry eligible at
  the exact prior exit time, reset flat at every holdout boundary.
- Cost: `forward_net_return_bps` already subtracts `20.0` bps round trip.
- Inputs: candles SHA-256
  `e40835922f491e0d7c97b05f6f5967a92d903aa856e9044dae03a2442d6b7ec1`;
  macro snapshot SHA-256
  `48a2a0bb5dba6d46af54ee20d4f525ef59bff9abd45f747cd2fbd883e60b1d33`.

New in this run (additive only, all prior numbers reproduced):

- fourth policy `probability_proportional`: size = predicted positive-class
  probability, clipped to `[0, 1]`;
- `sharpe_per_trade` = mean / std (ddof=1) of trade PnL in bps, undefined
  with fewer than two trades or zero dispersion;
- `sharpe_annualized` = `sharpe_per_trade × √(35,040)` for the 15-minute
  horizon (equal to the equity-curve Sharpe because equity returns are
  proportional to trade PnL in bps);
- pooled cross-holdout block concatenating all trades in holdout order.

## Per-holdout results

`sharpe` is per-trade (mean/std, ddof=1); `sharpe_ann` is the annualized
value for the 15-minute horizon. `PnL/exp` is net PnL bps per unit exposure.

| Model | Holdout | Policy | Entries | Exposure | Net PnL bps | PnL/exp | Sharpe | Sharpe ann | MaxDD |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Logistic | a | binary_f1 | 574 | 574.000 | -10911.801 | -19.010 | -0.559 | -104.7 | 0.665 |
| Logistic | a | top_decile | 196 | 196.000 | -3364.137 | -17.164 | -0.408 | -76.3 | 0.291 |
| Logistic | a | confidence_weighted | 574 | 34.420 | -478.022 | -13.888 | -0.140 | -26.2 | 0.048 |
| Logistic | a | probability_proportional | 1392 | 154.513 | -2887.054 | -18.685 | -0.380 | -71.1 | 0.251 |
| Logistic | b | binary_f1 | 136 | 136.000 | -2555.267 | -18.789 | -0.757 | -141.7 | 0.226 |
| Logistic | b | top_decile | 12 | 12.000 | -83.561 | -6.963 | -0.223 | -41.7 | 0.014 |
| Logistic | b | confidence_weighted | 136 | 3.264 | -52.683 | -16.139 | -0.496 | -92.8 | 0.005 |
| Logistic | b | probability_proportional | 1389 | 78.840 | -1504.911 | -19.088 | -0.676 | -126.6 | 0.140 |
| Logistic | c | binary_f1 | 37 | 37.000 | -928.096 | -25.084 | -0.799 | -149.7 | 0.092 |
| Logistic | c | top_decile | 4 | 4.000 | -90.196 | -22.549 | -1.194 | -223.5 | 0.009 |
| Logistic | c | confidence_weighted | 37 | 0.743 | -15.672 | -21.093 | -0.561 | -105.0 | 0.002 |
| Logistic | c | probability_proportional | 1394 | 52.954 | -1096.557 | -20.708 | -0.659 | -123.4 | 0.104 |
| GBT | a | binary_f1 | 591 | 591.000 | -11331.851 | -19.174 | -0.603 | -112.9 | 0.681 |
| GBT | a | top_decile | 611 | 611.000 | -11715.469 | -19.174 | -0.616 | -115.4 | 0.692 |
| GBT | a | confidence_weighted | 591 | 129.695 | -2442.839 | -18.835 | -0.360 | -67.4 | 0.217 |
| GBT | a | probability_proportional | 1392 | 285.655 | -5486.500 | -19.207 | -0.391 | -73.3 | 0.423 |
| GBT | b | binary_f1 | 26 | 26.000 | -146.562 | -5.637 | -0.197 | -36.8 | 0.022 |
| GBT | b | top_decile | 30 | 30.000 | -166.513 | -5.550 | -0.203 | -37.9 | 0.022 |
| GBT | b | confidence_weighted | 26 | 2.094 | -31.202 | -14.902 | -0.537 | -100.6 | 0.003 |
| GBT | b | probability_proportional | 1389 | 59.034 | -1104.197 | -18.704 | -0.525 | -98.3 | 0.105 |
| GBT | c | binary_f1 | 52 | 52.000 | -1022.213 | -19.658 | -1.132 | -211.9 | 0.097 |
| GBT | c | top_decile | 57 | 57.000 | -1122.301 | -19.689 | -1.151 | -215.4 | 0.106 |
| GBT | c | confidence_weighted | 52 | 5.237 | -98.950 | -18.894 | -0.741 | -138.7 | 0.010 |
| GBT | c | probability_proportional | 1394 | 61.790 | -1262.001 | -20.424 | -0.583 | -109.2 | 0.119 |

## Pooled cross-holdout results

All trades concatenated in holdout order (A → B → C).

| Model | Policy | Trades | Exposure | Net PnL bps | PnL/exp | Sharpe | Sharpe ann | MaxDD |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Logistic | binary_f1 | 747 | 747.000 | -14395.163 | -19.271 | -0.595 | -111.4 | 0.764 |
| Logistic | top_decile | 212 | 212.000 | -3537.894 | -16.688 | -0.405 | -75.8 | 0.298 |
| Logistic | confidence_weighted | 747 | 38.427 | -546.377 | -14.218 | -0.140 | -26.2 | 0.053 |
| Logistic | probability_proportional | 4175 | 286.307 | -5488.522 | -19.170 | -0.387 | -72.4 | 0.422 |
| GBT | binary_f1 | 669 | 669.000 | -12500.626 | -18.686 | -0.605 | -113.3 | 0.714 |
| GBT | top_decile | 698 | 698.000 | -13004.283 | -18.631 | -0.618 | -115.6 | 0.728 |
| GBT | confidence_weighted | 669 | 137.026 | -2572.990 | -18.777 | -0.354 | -66.3 | 0.227 |
| GBT | probability_proportional | 4175 | 406.479 | -7852.697 | -19.319 | -0.307 | -57.5 | 0.544 |

Pooled totals for the three original policies reconcile exactly with the
published cross-holdout sums (`-14395.163`, `-3537.894`, `-546.377`;
`-12500.626`, `-13004.283`, `-2572.990`).

## Read on the comparison

- The best Sharpe observed anywhere is `-0.140` per trade (logistic,
  confidence-weighted) — still deeply negative and only reachable at
  ~5% of the binary exposure.
- Confidence weighting's positive PnL delta vs binary in all holdouts is a
  de-risking effect: per-trade economics stay negative
  (pooled PnL/exposure `-14.22` vs `-19.27` for logistic — a partial
  improvement that does not cross zero).
- Top-decile selection is unstable: it helps logistic (fewer bad trades)
  and hurts GBT (its q90 slice behaves differently), and remains negative
  everywhere.
- `probability_proportional` enters ~5.6× more trades (4,175) with the
  worst pooled PnL/exposure for GBT and no material improvement for
  logistic.
- Model quality gates are unchanged and still fail: the sequence model
  remains `skipped` (no baseline beats majority PR-AUC and train-calibrated
  accuracy on every valid holdout).

## Backtest-engine decision

Item 3 of the work plan: a sizing strategy mode is added to the backtest
engine only if it beats binary on real holdouts. It does not, so **no mode
is added** to `polymarket_arb/backtest_engine.py` and no live-execution
path is touched. The complete negative comparison above is the record.

## Evidence

- [Full-precision pooled artifact](research/btc_offline_sizing_sharpe_pooled_2026-08-15.json)
- [First extension artifact, same holdout cells](research/btc_offline_sizing_sharpe_2026-08-15.json)
- [Evaluator implementation](../scripts/evaluate_btc_offline_profitability.py)
  (additive: `probability_proportional`, Sharpe metrics, pooled block)
- [New tests](../tests/test_btc_offline_sizing_sharpe.py)
- [Prior evaluation](btc_offline_confidence_weighted_sizing_2026-08-15.md)
- [Chronological protocol](btc_offline_chronological_evaluation_2026-08-11.md)

Test status: `tests/test_btc_offline_sizing_sharpe.py` and
`tests/test_btc_offline_chronological_evaluation.py` — `18 passed`.
The artifact records `executable_polymarket_pnl.status=unavailable` as
required: this is a BTC-proxy strategy evaluation, not Polymarket PnL.
