# BTC confidence-weighted sizing evaluation — 2026-08-15

## Verdict

Confidence weighting and top-decile selection do **not** turn the offline BTC
proxy signal into a profitable strategy. Every model/policy combination had
negative net PnL in every one of the three chronological holdouts. The apparent
improvement in confidence-weighted total PnL is primarily lower exposure; PnL
per unit exposure remains negative.

This closes the explored sizing variant as a negative result. It is not a
Polymarket execution backtest and does not support deployment or a sequence
model.

## Frozen protocol and inputs

- Models: the existing scaled logistic regression and existing histogram
  gradient-boosted classifier; no new model class.
- Training block: first 55% chronologically, `76197` retained rows.
- Holdouts: three disjoint future blocks with `20771`, `20770`, and `20785`
  retained rows.
- Embargo: 15 rows and at least 15 minutes at train/A, A/B, and B/C boundaries;
  `45` rows removed by embargo.
- Calibration: fitted-train predictions only, frozen before all holdouts.
- Position simulation: enter only while flat, hold 15 minutes, permit the next
  entry at the exact prior exit time, reset flat at every holdout boundary.
- Cost: `forward_net_return_bps` already subtracts `20.0` bps round trip; no
  second cost deduction.
- Candle input SHA-256:
  `e40835922f491e0d7c97b05f6f5967a92d903aa856e9044dae03a2442d6b7ec1`.
- Immutable macro snapshot SHA-256:
  `48a2a0bb5dba6d46af54ee20d4f525ef59bff9abd45f747cd2fbd883e60b1d33`.
  It contains `205` rows, including `48` HIGH rows and `23` unique valid HIGH
  timestamps used by the evaluator.

Train-only cutoffs:

| Model | F1 threshold | Train probability q90 |
| --- | ---: | ---: |
| Logistic regression | 0.12795263383358718 | 0.2108908741559322 |
| Gradient-boosted trees | 0.23461459380404626 | 0.22576412402061702 |

## Ranking and threshold gate

Both models beat the positive-rate PR-AUC baseline in every holdout, but neither
beat majority accuracy with its train-calibrated F1 threshold in every holdout.
The sequence-model gate therefore remains `skipped`.

| Holdout | Positive rate / majority accuracy | Model | ROC-AUC | PR-AUC | Train-calibrated accuracy |
| --- | ---: | --- | ---: | ---: | ---: |
| A | 0.13841413509219586 / 0.8615858649078042 | Logistic | 0.683942955433325 | 0.2496513920139936 | 0.7159501227673198 |
| A | 0.13841413509219586 / 0.8615858649078042 | GBT | 0.6089986200462576 | 0.19673121616198092 | 0.658177266381012 |
| B | 0.08714492055849783 / 0.9128550794415021 | Logistic | 0.7083083607245262 | 0.1862765597900083 | 0.8901781415503129 |
| B | 0.08714492055849783 / 0.9128550794415021 | GBT | 0.6971314136186679 | 0.16924433683244045 | 0.9092922484352431 |
| C | 0.06913639643974019 / 0.9308636035602598 | Logistic | 0.6827412549604224 | 0.13441956682250567 | 0.9231176329083474 |
| C | 0.06913639643974019 / 0.9308636035602598 | GBT | 0.5927056955856251 | 0.0833777915922714 | 0.9208082751984604 |

## Exact proxy-PnL results

`delta` is net PnL minus the same model's binary-F1 result in that holdout.
All PnL values are basis points from sequential, non-overlapping positions.

| Model | Holdout | Policy | Entries | Exposure | Net PnL bps | PnL / exposure bps | Delta bps |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: |
| Logistic | A | Binary F1 | 574 | 574.0 | -10911.800684355025 | -19.010105721872865 | 0.0 |
| Logistic | A | Top decile | 196 | 196.0 | -3364.137287156167 | -17.16396575079677 | 7547.663397198858 |
| Logistic | A | Confidence weighted | 574 | 34.420017766014425 | -478.02220390207407 | -13.887912759128868 | 10433.778480452951 |
| Logistic | B | Binary F1 | 136 | 136.0 | -2555.266672814793 | -18.78872553540289 | 0.0 |
| Logistic | B | Top decile | 12 | 12.0 | -83.56078313561866 | -6.963398594634889 | 2471.705889679174 |
| Logistic | B | Confidence weighted | 136 | 3.264386818882498 | -52.68266820994357 | -16.13861075078673 | 2502.5840046048493 |
| Logistic | C | Binary F1 | 37 | 37.0 | -928.0961358288502 | -25.08367934672568 | 0.0 |
| Logistic | C | Top decile | 4 | 4.0 | -90.19625382555844 | -22.54906345638961 | 837.8998820032917 |
| Logistic | C | Confidence weighted | 37 | 0.7430059579532569 | -15.672274698323376 | -21.093067330840075 | 912.4238611305268 |
| GBT | A | Binary F1 | 591 | 591.0 | -11331.850741415132 | -19.174028327267568 | 0.0 |
| GBT | A | Top decile | 611 | 611.0 | -11715.468582992644 | -19.17425299998796 | -383.6178415775121 |
| GBT | A | Confidence weighted | 591 | 129.69517113257737 | -2442.838650232894 | -18.835232097699063 | 8889.012091182238 |
| GBT | B | Binary F1 | 26 | 26.0 | -146.5620057137724 | -5.637000219760477 | 0.0 |
| GBT | B | Top decile | 30 | 30.0 | -166.51322078785782 | -5.550440692928594 | -19.95121507408541 |
| GBT | B | Confidence weighted | 26 | 2.0937966655202493 | -31.20171736004097 | -14.90198063348631 | 115.36028835373143 |
| GBT | C | Binary F1 | 52 | 52.0 | -1022.2130421676825 | -19.65794311860928 | 0.0 |
| GBT | C | Top decile | 57 | 57.0 | -1122.3009536762443 | -19.689490415372706 | -100.08791150856177 |
| GBT | C | Confidence weighted | 52 | 5.237165836036688 | -98.94993545728701 | -18.89379457423655 | 923.2631067103955 |

Cross-holdout totals independently reconciled from the artifact:

| Model | Policy | Total exposure | Total net PnL bps | PnL / exposure bps |
| --- | --- | ---: | ---: | ---: |
| Logistic | Binary F1 | 747 | -14395.163492998667 | -19.270633859435968 |
| Logistic | Top decile | 212 | -3537.8943241173442 | -16.688180774138416 |
| Logistic | Confidence weighted | 38.42741054285018 | -546.377146810341 | -14.218422191135646 |
| GBT | Binary F1 | 669 | -12500.625789296588 | -18.685539296407455 |
| GBT | Top decile | 698 | -13004.282757456745 | -18.630777589479578 |
| GBT | Confidence weighted | 137.0261336341343 | -2572.9903030502223 | -18.777369212797154 |

Confidence weighting produced a positive delta versus binary in all three
holdouts for both models, but every delta is relative to a larger loss. It did
not produce one profitable holdout. GBT top-decile sizing was worse than binary
in all three holdouts. Logistic top-decile sizing reduced loss but remained
negative in all three.

## Evidence

- [Full-precision JSON artifact](research/btc_offline_confidence_weighted_sizing_2026-08-15.json)
- [Immutable macro snapshot](../state/macro_events_snapshot_2026-08-15T093742Z.json)
- [Evaluator implementation](../scripts/evaluate_btc_offline_profitability.py)
- [Focused tests](../tests/test_btc_offline_chronological_evaluation.py): `10 passed`, `2` existing NumPy constant-column warnings.

The JSON explicitly records `executable_polymarket_pnl.status=unavailable`.
