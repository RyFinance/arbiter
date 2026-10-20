# Kronos Results Reconciliation

Generated from the checked-in result artifacts on 2026-08-03. This report
reconciles `state/kronos_threshold_calibration.json`,
`state/kronos_threshold_robustness.json`, and `state/kronos_spotcheck.json`.

## Status

- Diversified robustness holdout: PASS for sample construction. It has 12 valid
  Kronos predictions over 11,520 read-only LSE BTC/USD one-minute candles,
  eight unused dates, and UTC hours 00/04/16/20 three times each.
- Production threshold validation: FAIL against the 80% gate. The loaded
  0.03/0.05 thresholds matched 6/12 holdout labels (50.00%).
- Retuning: triggered because 50.00% is below 80%. It trained only on the 27
  calibration rows, selected 0.03/0.05 again (24/27, 88.89% training), and
  validated 6/12 (50.00%) on the untouched diversified holdout. The candidate
  is identical to production, so no threshold constants were changed.

## Result Definitions

| Result | Forecast input | Realized target and match definition |
| --- | --- | --- |
| Calibration | Last 512 BTC/USD one-minute candles; Kronos-small predicts 60 one-minute candles. | ATR is computed over the contemporaneous next 60 one-minute candles, normalized by mean close and multiplied by sqrt(1440). The realized label is fixed using the old 0.02/0.05 boundaries; the reported old/new match rates classify the forecast against that label. |
| Robustness | Same 512-minute context and 60-minute Kronos forecast. | Same 60-minute, normalized, daily-equivalent realized target and old-boundary label definition as calibration. The refreshed holdout is disjoint from calibration dates. |
| Spot-check | 30-day historical request (the artifact reports 5,000 one-minute candles per checkpoint); Kronos-small predicts 60 minutes. | Forecast label at T is compared with one-minute ATR over T+24h through T+48h, a 24-hour session delayed by one day. It also reports a separate historical daily-ATR ground-truth comparison. This is not the calibration/robustness 60-minute target. |

The calibration and robustness match rates are therefore comparable to each
other, but neither is directly comparable to the spot-check's 55.00% rate.
They use different realized-window lengths and different target alignment.

## ATR Scale And Thresholds

All three paths use raw ATR divided by mean close to obtain an ATR fraction and
apply square-root-of-time scaling to daily-equivalent values. For one-minute
candles the multiplier is `sqrt(1440) = 37.95`. Kronos forecast ATR is also a
one-minute percentage ATR, then scaled by the same factor before regime
classification.

- Calibration: frozen baseline is LOW/MED 0.02 and HIGH 0.05. It does not load
  the production constants; its grid search happened to select 0.03/0.05 and
  reported 24/27 (88.89%), versus 17/27 (62.96%) at the frozen baseline.
- Robustness: directly imports production `ATR_LOW_THRESHOLD` and
  `ATR_HIGH_THRESHOLD`; its artifact records 0.03/0.05. The same holdout gives
  3/12 (25.00%) under 0.02/0.05 and 6/12 (50.00%) under 0.03/0.05.
- Spot-check: directly imports the production constants and records LOW/MED
  0.03 and HIGH 0.05 in its configuration. Its forecast-vs-realized-session
  result is 11/20 (55.00%); all 20 forecasts used the Kronos source.

## Checkpoint Separation

Exact duplicate means identical UTC timestamp. Near duplicate means timestamps
within 60 minutes; a separate 24-hour check was used to expose adjacent-session
dependence.

- Calibration versus robustness: 0 exact timestamp matches, 0 matching dates,
  and 0 pairs within 24 hours. Calibration spans 2026-07-26 through 2026-07-30;
  the refreshed holdout spans 2026-06-30 through 2026-07-19.
- Calibration versus spot-check: 0 exact timestamp matches, 0 matching dates,
  and 0 pairs within 60 minutes. Eleven pairs are within 24 hours, all around
  the spot-check checkpoint at 2026-07-28T00:00Z and calibration rows from
  2026-07-27/2026-07-29; these are adjacent sessions, not duplicates.
- Robustness versus spot-check: 0 exact timestamp matches. There are two
  60-minute near pairs: 2026-07-01T00:02Z versus 2026-07-01T00:00Z, and
  2026-07-15T00:02Z versus 2026-07-15T00:00Z. The forecast starts are nearly
  identical, but robustness scores the next 60 minutes while spot-check scores
  T+24h through T+48h; do not pool them as independent same-target examples.

## Exact Current Metrics

- Calibration: 27/27 valid, realized labels 22 LOW and 5 MED; 24/27 (88.89%)
  at the 0.03/0.05 grid-search result.
- Robustness: 12/12 valid, realized labels 9 LOW and 3 MED; 6/12 (50.00%) at
  loaded production 0.03/0.05. Its dates are 2026-06-30 (2), 2026-07-01 (2),
  2026-07-07 (2), 2026-07-08 (2), 2026-07-14/15/18/19 (1 each).
- Spot-check: 20/20 valid, 20 Kronos-sourced forecasts; 11/20 (55.00%) for its
  delayed 24-hour realized-session label and 7/20 (35.00%) for its separately
  reported historical daily-ground-truth comparison.

## Verification

`pytest -q tests/test_kronos_calibration_robustness.py tests/test_calibrate_kronos_thresholds.py tests/test_spotcheck_kronos.py` completed with 24 passed. The refreshed robustness tool was run with its defaults and rewrote only the Kronos robustness JSON and Markdown artifacts.

## Phase 1 Final LSE Investigation (Capped)

**FINAL VERDICT: DIAGNOSTIC-ONLY, NOT A TRADING SIGNAL.** The fresh holdout
did not exceed 50%, so this investigation is final and no further Kronos
threshold cycles are permitted. Production thresholds remain `0.03/0.05`;
they were not changed.

- Data: real LSE BTC/USD one-minute candles; 512-minute Kronos context and
  the contemporaneous next 60-minute realized window. No synthetic data or
  fallback predictions were used.
- Excluded before selection: the original 27-row calibration dates
  `2026-07-26`, `2026-07-27`, `2026-07-29`, `2026-07-30`, and the current
  eight-date holdout `2026-06-30`, `2026-07-01`, `2026-07-07`, `2026-07-08`,
  `2026-07-14`, `2026-07-15`, `2026-07-18`, `2026-07-19`.
- Labels: realized 60-minute ATR is normalized by mean close, scaled by
  `sqrt(1440)`, and labelled with the frozen `0.02/0.05` boundaries.
- Threshold derivations: exactly one. The calibration-only grid search chose
  LOW/MED `0.031` and MED/HIGH `0.087`; it scored 27/40 (67.50%) on the
  calibration rows. The holdout did not participate in selection.
- Fresh untouched holdout result: 7/16 (43.75%) with the same `0.031/0.087`
  pair. Realized labels were 8 LOW, 6 MED, and 2 HIGH. All 16 predictions were
  valid Kronos predictions.

### Exact Inputs

| Split | Date: UTC hours |
| --- | --- |
| Calibration (40 checkpoints) | `2026-01-05`: 00, 12; `2026-01-12`: 04, 16; `2026-01-19`: 08, 20; `2026-01-26`: 00, 12 |
| Calibration continued | `2026-02-02`: 04, 16; `2026-02-09`: 08, 20; `2026-02-16`: 00, 12; `2026-02-23`: 04, 16 |
| Calibration continued | `2026-03-02`: 08, 20; `2026-03-09`: 00, 12; `2026-03-16`: 04, 16; `2026-03-23`: 08, 20 |
| Calibration continued | `2026-04-06`: 00, 12; `2026-04-13`: 04, 16; `2026-04-20`: 08, 20; `2026-04-27`: 00, 12 |
| Calibration continued | `2026-05-04`: 04, 16; `2026-05-11`: 08, 20; `2026-05-18`: 00, 12; `2026-05-25`: 04, 16 |
| Fresh holdout (16 checkpoints) | `2026-06-04`: 00, 12; `2026-06-07`: 04, 16; `2026-06-11`: 08, 20; `2026-06-14`: 00, 12; `2026-06-18`: 04, 16; `2026-06-21`: 08, 20; `2026-06-25`: 00, 12; `2026-06-28`: 04, 16 |

The complete per-checkpoint inputs, realized labels, forecasts, candle counts,
and errors (none) are recorded in
`state/kronos_final_lse_investigation.json`. The executable, fixed split and
the guards that enforce its disjointness are in
`scripts/run_kronos_final_lse_investigation.py`.
