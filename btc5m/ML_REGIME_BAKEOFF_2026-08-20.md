# ML Regime Bake-off — LightGBM vs Kronos-style classifier (2026-08-20)

**Supersedes** the LightGBM/Kronos numbers in `LIGHTGBM_KRONOS_TRAINING_REPORT.md`
(2026-08-04) and `FIXED_TRAINING_REPORT_V2.md` (2026-08-04) — those are dated
and preserved, not deleted, per this repo's convention. **Winner: LightGBM.**
Now wired live as the primary regime forecaster (`polymarket_arb/kronos_session.py`).

## Why this run happened

Both prior reports blocked further work on data scarcity: LightGBM's
leakage-fixed methodology used only 4,783 real rows; the from-scratch
transformer classifier used only 807 sequences and catastrophically overfit
(71.25% validation vs 27.27% test accuracy). Neither was ever retrained after
that — even though a real LSE API key was live-verified on 2026-08-11 and
`state/cache/btc_usd_1m_candles.parquet` grew to 4.47M real rows (2017–2026)
in the meantime, including an ~845,000-row essentially unbroken span from
2025-01-01 to 2026-08-11 (one 2-minute gap). That data was never pointed at
either training pipeline until now.

A real bug was also found and fixed along the way: in
`scripts/fill_and_test_arb_v3.py::calculate_technical_indicators`, the
Bollinger/RSI/MACD/trend features used plain integer-positional
`.rolling(window=N)`, unlike the volatility/ATR features a few lines above
which correctly use time-indexed rolling so a data gap can't blend stale
pre-gap prices into a post-gap value. Fixed to match (verified against a
synthetic gap: affected features now correctly go `NaN` immediately after a
gap instead of silently blending).

## Method

Both models trained on real data only — no synthetic padding, matching this
repo's existing `data_policy.real_lse_1m_btc_usd_only` convention.

- **LightGBM**: `state/cache/btc_usd_1m_candles.parquet` sliced to
  2025-01-01 → 2026-08-11 (845,516 eligible rows after feature/target
  NaN-drop). Same leakage-free methodology as `FIXED_TRAINING_REPORT_V2.md`
  (unchanged, already correct): train-only tercile cutoffs on
  `forward_realized_vol_bps`, 3 independent time-disjoint holdouts
  (train 50% / val 12.5% / holdout A/B/C 12.5% each, chronological).
- **Kronos-style classifier** (`scripts/train_kronos_comprehensive.py`'s
  from-scratch `KronosVolatilityClassifier`, 810,243 params — **not** the
  pretrained NeoQuasar/Kronos-small foundation model, which has no
  fine-tuning loop in this codebase and is used zero-shot in the live
  fallback path): last 180 real days of the same source
  (2026-02-12 → 2026-08-11, 51,811 sequences — reduced from the full span so
  transformer training stayed tractable on this machine's MPS backend,
  still ~64x the previous run's sequence count). Single chronological
  train(70%)/val(10%)/test(20%) split, unchanged from the script's own
  existing discipline.

**Known asymmetry, disclosed honestly:** the classifier's label cutoffs are
computed over its *entire* dataset before splitting (`create_volatility_labels`
in `train_kronos_comprehensive.py`) — the same leakage bug LightGBM's
methodology already fixed (`FIXED_TRAINING_REPORT_V2.md` BUG 1). That biases
its 53.82% figure slightly optimistic. Not re-fixed and re-run here because
it wouldn't change the outcome — see results below — but noted so the
comparison isn't overstated in LightGBM's favor.

## Results

### LightGBM (Variant A, `class_weight="balanced"`)

| Holdout | Rows | Accuracy | Macro F1 | Majority baseline | vs baseline |
|---|---|---|---|---|---|
| A (2026-01-03 → 2026-03-17) | 105,690 | **72.51%** | 0.684 | 20.42% | **+52.09pp** |
| B (2026-03-17 → 2026-05-30) | 105,689 | **62.99%** | 0.633 | 33.90% | **+29.09pp** |
| C (2026-05-30 → 2026-08-11) | 105,690 | **67.03%** | 0.665 | 35.14% | **+31.90pp** |

**Mean: 67.51%** (stddev 3.90pp) — **all 3 holdouts beat majority baseline**
(the old report's holdout C actually *lost* to baseline, −13.15pp; this run
reverses that). At a 0.7-confidence firing threshold, holdout C fires on
20.2% of rows at 90.5% precision — a real, usably-selective signal, not just
raw accuracy inflated by an easy majority class.

Saved to `state/lightgbm_volatility_model_v3.pkl` (joblib).

### Kronos-style classifier (from-scratch transformer)

| Split | Samples | Accuracy | Macro F1 |
|---|---|---|---|
| Train | 36,268 | 59.2% (final epoch) | — |
| Validation | 5,181 | 51.5% (best, epoch 1) | 0.498 |
| Test | 10,362 | **53.82%** | 0.508 |

Best epoch was **epoch 1** — validation accuracy oscillated between 43–58%
for the next 10 epochs without ever beating epoch 1's number, so early
stopping (patience=10) halted training at epoch 11. Unlike the previous
807-sequence run, this is *not* a severe train/val/test overfit gap (train
59% vs test 54% is a normal, small gap) — the honest read is that the
architecture+features found their ceiling almost immediately and 64x more
data didn't move it, not that it's undertrained.

Saved to `state/kronos_volatility_classifier_v2.pth` (versioned — does not
overwrite the old catastrophically-overfit checkpoint).

### Verdict

**LightGBM wins decisively: 67.51% vs 53.82%, on real, comparable, current
BTC data, even before accounting for the classifier's still-present labeling
leakage that mildly favors its number.** Neither the additional data nor a
from-scratch architecture closed the gap for the transformer classifier.
Kronos-small (the pretrained foundation model, unchanged — not fine-tuned
here, see Method) remains a distant third by STATUS.md's existing documented
~0.55 match rate.

## What changed live

`polymarket_arb/kronos_session.py` — `forecast_session_regime()` now tries
regime sources in priority order, each falling back to the next:

1. **LightGBM** (`_try_lightgbm`, new) — primary. Reuses
   `calculate_technical_indicators` from the training script directly (real
   timestamps, not Kronos's synthetic evenly-spaced ones) so live inference
   matches training exactly. Returns `None` (falls through) if the model
   artifact is missing, prediction fails, or there isn't yet 60 real
   contiguous minutes of history for a fully-populated feature row.
2. **Kronos-small** (`_try_kronos`, unchanged) — fallback if LightGBM is
   unavailable.
3. **ATR heuristic** (`_regime_from_atr`, unchanged) — final fallback,
   always computed as the baseline regardless.

This preserves the existing safety boundary unchanged: whichever source
wins, it only ever sets `no_entry_seconds`/`max_quote_age_ms` timing gates
(via the same `_regime_params()`) — no ML model touches the core
deterministic crossed-book (`combined < 1 - min_edge`) decision.

Also moderated this session (see main STATUS.md changelog): Kronos-small's
HIGH-regime params (`no_entry_seconds`/`max_quote_age_ms`) were tightened
disproportionately to its ~0.55 documented match rate; loosened from
10.0s/250ms to 7.0s/400ms, closer to MED. Applies regardless of which
source is currently active, since all three share `_regime_params()`.

## A bug live verification caught that unit tests didn't

First integration attempt against a real live LSE fetch: `_try_lightgbm`
returned `None` every time (silently falling all the way through to
Kronos-small, which then hit its own 30s timeout, then the ATR heuristic —
32.9s total, technically correct but LightGBM never actually ran). Cause:
`LSEClient.fetch_btc_candles_for_kronos`'s docstring claimed candles come
back keyed `timestamp`, but the real response is keyed `ts` — confirmed
against a live fetch. `_try_lightgbm` was built straight from that (wrong)
docstring, so every row's `c.get("timestamp")` was `None` and the whole
candle list got filtered to zero before feature computation even started.
Every mocked unit test passed anyway because the test fixtures construct
candle dicts by hand with whatever key the test author intended — none of
them exercised the real LSE client's actual response shape.

Fixed to check `ts` first (same as `normalize_lse_candles()` in
`scripts/train_kronos_comprehensive.py`, which already had to handle this
same inconsistency), and the docstring corrected. Re-verified against a
fresh live fetch: LightGBM now fires in <1s. This is why the plan's
verification step called for a live smoke test, not just green unit tests —
this exact bug would have shipped past a green `pytest` run.

## Follow-up: hyperparameter search (same day, negative result)

`fill_and_test_arb_v3.py`'s hyperparameters (`num_leaves=63, max_depth=5,
min_child_samples=10, learning_rate=0.05`) were tuned via search on the
4,783-row dataset back on 2026-08-04 and never revisited — a reasonable
thing to check given the model now trains on 175x more data. Ran
`scripts/tune_lightgbm_hyperparams.py` (new): 6 candidates spanning shallow
→ unbounded depth, plus one testing the dead `minutes_to_next_high_macro`
feature (0 permutation importance, per the main report above) dropped
entirely. Selection discipline followed `docs/chunk_e_walk_forward_
retraining_policy_2026-08-11.md`: candidates ranked by **validation**
accuracy only; holdouts A/B/C touched exactly once, for the winner, never
used to pick among candidates.

| Candidate | leaves | depth | min_child | lr | validation acc |
|---|---|---|---|---|---|
| baseline_current (live) | 63 | 5 | 10 | 0.05 | 69.02% |
| deeper_moderate | 127 | 7 | 20 | 0.05 | 68.67% |
| deeper_regularized | 255 | 8 | 100 | 0.05 | 69.03% |
| deep_slow_lr | 255 | 8 | 50 | 0.03 | 68.69% |
| unbounded_depth | 511 | none | 100 | 0.03 | 68.69% |
| **deeper_moderate_no_deadfeat** (winner) | 127 | 7 | 20 | 0.05 | **69.43%** |

**All 6 candidates land within 0.76pp of each other on validation** — deeper
trees, more leaves, slower learning rate, none of it moved the needle in any
consistent direction. That flat landscape is itself the finding: the
ceiling here is the **features**, not model capacity. Widening trees can't
extract signal that isn't in the inputs.

The nominal winner (dropping the dead macro feature, moderate depth
increase) then scored on holdouts for the first and only time:
**63.77% mean (71.92%/57.10%/62.29%)** — worse than the live model's
**67.51% mean** on the same holdouts. A ~0.4pp validation "win" that is
almost certainly noise turned into a real holdout regression. This is
exactly why the walk-forward doc's rule (never select against holdouts)
matters — cherry-picking by holdout instead would have risked locking in
that regression by coincidence.

**Verdict: no improvement found. The live model (`lightgbm_volatility_model_v3.pkl`,
the original baseline hyperparameters) stays deployed, unchanged.** The
tuned candidate is saved as `state/lightgbm_volatility_model_v5_tuned.pkl`
for the record but is not wired anywhere.

**What would actually move this forward:** not more hyperparameter search —
the flat validation landscape rules that out for now. The features
themselves are the bottleneck. The two live-data feature classes not yet
tried (per the original 2026-08-04 report's own recommendations, still
unaddressed): order-book depth/spread (bid-ask, order flow imbalance — this
project already captures raw Polymarket depth snapshots for other purposes,
see `state/cache/polymarket_depth_consolidated.jsonl`, but nothing
equivalent exists for the BTC/USD spot feed this model trains on) and
cross-asset signals (ETH correlation, DXY, etc. — `dashboard_js/providers.py`
already fetches some of this live for the dashboard's cross-asset panel,
unused by the training pipeline). Both are new-data-source undertakings,
not a retrain — out of scope here without confirming that's wanted.

## Follow-up 2: new external signals (2026-08-21, also a negative result)

Acted on the "what would move this forward" list above with 5 concrete,
user-proposed free APIs. Checked real historical depth on each before
building anything — a live/current-snapshot API is not the same as one with
history over the 2025-01-01→2026-08-11 training window:

| Source | Verdict | Why |
|---|---|---|
| Binance order-book depth/spread | **Not usable** | Snapshot-only API — no historical endpoint exists; would need to start live-collecting today for a future retrain. |
| Binance open interest | **Not usable** | Free `openInterestHist` only returns ~30 days; a confirmed real call returned just the last 5. |
| Tick-level microstructure (already-collected `state/btc_ticks_real.parquet` etc.) | **Not usable** | Corrects my own earlier claim that this was "already sitting there" — verified it only covers 2026-07-26→07-30 (5 days), nowhere near the training window. |
| Cross-asset (ETH/DXY via the LSE vault already used for BTC) | **Blocked** | The account's LSE key returned HTTP 429 on 3 separate attempts across this session (even for BTC/USD itself), almost certainly the dashboard's own continuous live polling holding the rate budget. Not fetched — skipped honestly rather than faked. |
| Fear & Greed Index (alternative.me) | **Used** | Confirmed full daily history since 2018-02-01, one keyless call. |
| Binance funding rate | **Used** | Confirmed full history via `startTime`/`endTime` back through 2025-01-01, keyless, paginated. |
| CryptoPanic | **Not attempted** | Needs your own free-tier `auth_token` — didn't ask you to paste a credential into chat. Add `CRYPTOPANIC_API_KEY` to `.env` yourself if you want this wired in later. |

Fetched both usable sources for real (`scripts/fetch_external_signals.py`,
new) — 3,120 daily Fear & Greed rows, 1,762 funding-rate rows — and merged
them onto the candle timeline via `pd.merge_asof(..., direction="backward")`
(the most recent known value at or before each timestamp, never a future
one) in `fill_and_test_arb_v3.py::merge_external_signals`. Retrained on the
same real 845,516-row span, same methodology, same baseline hyperparameters:

| | mean holdout accuracy | holdout_a / b / c |
|---|---|---|
| Without (current baseline) | 67.51% | 72.51% / 62.99% / 67.03% |
| With Fear & Greed + funding rate | 67.45% | 72.48% / 62.90% / 66.97% |

**A 0.06pp difference — standard error at this holdout size (n≈105,690) is
≈0.15pp, so this is noise, not a real effect.** `fear_greed_value` shows up
in permutation importance (0.0011) at roughly the same marginal level as
`volume`/`rolling_vol_60m_bps` (already known to be weak); `funding_rate_bps`
didn't even make the top-5. Precision@0.7 confidence went slightly the wrong
way too (90.48% → 88.85%). Third negative result in a row (data volume was
the one real win; hyperparameters and now two new real signals have both
landed at "no meaningful change").

**Deployed model was not switched.** Per the same discipline as the
hyperparameter search — no swap without a genuine improvement — the
with-signals model was saved separately
(`state/lightgbm_volatility_model_v6_with_signals.pkl` /
`..._training_metrics_v6_with_signals.json`) rather than left as the live
default. `fear_greed_value`/`funding_rate_bps` stay in `FEATURE_COLUMNS` for
future retrains (no cost to keeping them), but the actually-deployed
`state/lightgbm_volatility_model_v3.pkl` was regenerated to the original
25-feature version and reverified to reproduce the exact original numbers
(72.51/62.99/67.03).

One real bug this surfaced and fixed along the way:
`kronos_session.py::_try_lightgbm` used to derive its required feature list
from the training script's current global `FEATURE_COLUMNS` rather than
from the loaded model itself — meaning swapping in an older or differently-
featured model.pkl would silently hand it columns it never trained on and
LightGBM would reject the input outright. Fixed to read `model.feature_name_`
(scikit-learn's LightGBM wrapper always sets this after fitting on a named
DataFrame), so `_try_lightgbm` now self-adapts to whichever model is
actually on disk — verified with a real (non-mocked) inference call against
the restored model, and covered by two new tests
(`test_try_lightgbm_older_model_without_new_features_skips_live_fetches`,
`test_try_lightgbm_external_signal_fetch_failure_uses_neutral_defaults`).

## Follow-up 3: order-book depth, open interest, cross-asset (2026-08-21/22, also a negative result)

Follow-up 2 closed with two "not usable" verdicts (order-book depth, open
interest) and one "blocked" verdict (cross-asset via the rate-limited LSE
vault). You pushed back with specific sources that overturn two of those
three — this follow-up re-checked each one for real before building
anything:

| Source | Verdict | Why (corrects Follow-up 2 where relevant) |
|---|---|---|
| **Binance Vision bulk archive** (`data.binance.vision/data/futures/um/daily/bookDepth/BTCUSDT/`) | **Used** — overturns Follow-up 2's verdict | That verdict was about Binance's *live* REST depth snapshot (no history). This is a separate, free, keyless, static daily-zip archive with one file per day back through at least 2025-01-01: `timestamp,percentage,depth,notional` at ±1–5% from mid, sampled ~every 35s. Real historical order-book depth exists; my earlier check just hadn't found it. |
| Same archive, `data/futures/um/daily/metrics/BTCUSDT/` | **Used** — overturns Follow-up 2's verdict | That verdict was about the free live `openInterestHist` REST endpoint (~30 days only, confirmed by a real call returning just 5 rows). This archive has real open interest + top-trader/taker long-short ratios at 5-minute granularity back to 2020-09-01 — no 30-day limit. |
| `data.binance.vision` full tick trades (`data/futures/um/daily/trades/BTCUSDT/`) | **Deferred** | Confirmed present, ~15MB/day zipped (~8.8GB for the full window). Real, but a 1-minute resample of tick prints mostly re-derives what a 1-min OHLCV candle already has; the genuinely new information (order flow imbalance, depth) is already captured by the bookDepth archive above. Not built for a hunch — can revisit if there's still appetite after this round's result. |
| `yfinance` (already-installed project dependency, v1.6.0) | **Used** — solves Follow-up 2's "blocked" cross-asset verdict | Bypasses the rate-limited LSE vault entirely. Confirmed live: pulls real daily ETH-USD OHLCV instantly, no auth, no rate limit hit. |
| CryptoPanic | **Still not attempted** | Unchanged from Follow-up 2 — needs your own free-tier `auth_token`; add `CRYPTOPANIC_API_KEY` to `.env` yourself if you want this wired in later. |

Bulk-downloaded both Binance Vision datasets and the cross-asset daily bars
(`scripts/fetch_binance_vision_signals.py`, new) — 17,508,458 book-depth
rows, 169,341 OI/long-short rows, 587 cross-asset daily rows, all real, all
cached to parquet. One real bug found and fixed along the way:
`load_binance_bookdepth`'s initial pivot-lookup used string-formatted column
keys (e.g. `"depth_-1"`), but the real pivoted columns are float-keyed tuples
(`("depth", -1.0)`) and some days carry unexpected extra levels (`-0.2`,
`0.2`) — fixed by indexing the pivoted MultiIndex directly; verified 0% NaN
afterward (was 100% NaN before the fix).

Engineered 9 new features onto the same `merge_asof(direction="backward")`
no-lookahead pattern as every prior signal: `depth_imbalance_1pct` /
`depth_imbalance_5pct` (bid/ask notional imbalance at ±1%/±5% from mid),
`depth_total_notional_1pct`, `open_interest`, `oi_change_1h_pct` (the rate of
change, not just the level — a static OI level is mostly a proxy for
price×size), `toptrader_long_short_ratio`, `taker_long_short_vol_ratio`,
`eth_return_1d_lag`, `eth_realized_vol_5d_lag` (both lagged one full day past
the source's own daily close to rule out same-day lookahead). Retrained on
the same real 2025-01-01→2026-08-11 span (836,843 eligible rows here, down
from 845,516 — the cross-asset signal's ~7-day warmup trims the front of the
window), same methodology, same baseline hyperparameters:

| | mean holdout accuracy | holdout_a / b / c |
|---|---|---|
| Without (current baseline) | 67.51% | 72.51% / 62.99% / 67.03% |
| With depth + OI/long-short + cross-asset | 67.25% | 72.55% / 62.70% / 66.50% |

**A 0.26pp regression — within the ≈0.15pp standard error band's near
neighborhood but consistently on the wrong side across 2 of 3 holdouts (B and
C both down, A marginally up), so more likely mild noise than a real effect,
but not an improvement by any reading.** None of the 9 new features reached
the permutation-importance top 5 (`atr_60m_bps`, `atr_14m_bps`, `hour_sin`,
`volume`, `fear_greed_value` still dominate, unchanged from Follow-up 2) —
the best-placed new feature, `eth_realized_vol_5d_lag`, ranked 10th
(0.00151, vs `atr_60m_bps`'s 0.0833). Precision@0.7 on holdout_c also went
the wrong way: 88.45% precision at 24.09% fired, vs the baseline's 90.48% at
20.18% fired — the model got *less* selective, not more, for roughly the
same or worse hit rate. Fourth negative result in a row now (data volume
remains the one real win; hyperparameters, Fear & Greed + funding rate, and
now book depth/OI/cross-asset have all landed at "no meaningful change or
mild regression").

**Deployed model was not switched.** Backed up the live-deployed model
*before* retraining this time (a lesson from Follow-up 2, where the retrain's
hardcoded output path briefly overwrote the live model before I'd decided
whether to keep it) — no repeat of that near-miss here. The full-signals
model was saved separately (`state/lightgbm_volatility_model_v7_full_signals.pkl`
/ `..._training_metrics_v7_full_signals.json`) and the actually-deployed
`state/lightgbm_volatility_model_v3.pkl` was restored byte-for-byte from the
pre-retrain backup, reverified: 25 features, no Follow-up 2/3 columns
present, matching the exact original 67.51%-mean model. The 9 new columns
stay in `FEATURE_COLUMNS` for future retrains (no cost to keeping them
defined even though this model doesn't use them).

Live inference (`kronos_session.py::_try_lightgbm`) was wired for all 9 new
features regardless of whether this particular retrain paid off, since
`model.feature_name_`-driven column selection (fixed in Follow-up 2) means a
future model swap can pick them up with no code change: live book-depth via
`GET /fapi/v1/depth`, live OI/long-short via `/fapi/v1/openInterest` and
`/futures/data/topLongShortPositionRatio`, live cross-asset via one
`yfinance` call — each independently wrapped so a single failed fetch falls
back to a neutral default (`0.0` for imbalances/changes, `1.0` for ratios)
rather than veto an otherwise-good prediction, same discipline as every
external signal added so far. Confirmed via the currently-deployed
25-feature model that none of these 5 new fetch functions get called at all
(the required columns aren't in `model.feature_name_`), so this round adds
zero live-latency or live-API-failure risk to the model actually running in
production today.

## Follow-up 4: shorter forecast horizon (2026-08-22, negative — horizon is not the lever)

Tried a different axis after four rounds of feature engineering all landed
flat or slightly negative: is 15 minutes even the right horizon to be
classifying? Hypothesis was that a *shorter* horizon (5 min) would be
mechanically easier to predict — less time for regime to drift mid-window —
and could raise the ceiling independent of any feature changes.

Made `horizon_minutes` and the output-filename tag configurable on
`run_pipeline`/the CLI (`--horizon`, `--model-tag`, both new, defaulting to
`15`/`v3` — fully backward compatible) so experiments like this can never
again risk silently overwriting the live-deployed artifact the way Follow-up
2 nearly did. Retrained on the identical 845,516-row real span, identical 25
features (no external signals — isolates horizon as the only changed
variable), identical hyperparameters, `horizon_minutes=5`:

| | mean holdout accuracy | holdout_a / b / c | precision@0.7 (fired% / prec%) |
|---|---|---|---|
| 15-min (current baseline) | 67.51% | 72.51% / 62.99% / 67.03% | ~20% / ~90% |
| 5-min | **61.29%** | 65.58% / 57.22% / 61.06% | 25-41% / 80-86% |

**Wrong direction, and by a wide margin (−6.2pp) — the opposite of the
hypothesis.** A shorter horizon didn't make the classification easier; it
made it noisier. The likely mechanism: `forward_realized_vol_bps` over 5
one-minute returns has far more relative noise than over 15 (fewer squared
terms summed, so idiosyncratic single-minute moves dominate the label more),
and the train-only tercile cutoffs on that noisier target are themselves
less stable. Permutation importance also concentrated even harder onto just
`atr_14m_bps`/`atr_60m_bps` (0.097/0.033, vs the 15-min model's spread across
more features) — the model is leaning on the two ATR features alone to
compensate for a noisier target, and it isn't enough.

**Deployed model unchanged** (never at risk this time — new artifacts saved
under `model_tag="v8_horizon5"`, isolated from `_v3` from the start).
15 minutes turns out not to be an arbitrary legacy choice — it's close to
where this feature set's signal-to-noise ratio actually works. Longer
horizons (30min, 60min) are the logical next probe if horizon is still worth
pursuing, but the direction of this result (shorter = worse) makes that a
speculative follow-on, not a natural next step from here.

## Reproduce

```bash
# LightGBM
.venv/bin/python3 scripts/fill_and_test_arb_v3.py \
  --candles state/btc_1m_candles_real_2025_2026.parquet \
  --output state/lightgbm_training_metrics_v4_real2025.json

# Fetch the two new external signals (full real history, both keyless)
.venv/bin/python3 scripts/fetch_external_signals.py --start 2025-01-01 --end 2026-08-11

# LightGBM with Fear & Greed + funding rate (negative result, see Follow-up 2 above;
# writes to the same default output paths as the plain run above)
.venv/bin/python3 scripts/fill_and_test_arb_v3.py \
  --candles state/btc_1m_candles_real_2025_2026.parquet \
  --fear-greed state/fear_greed_index.json \
  --funding-rate state/binance_funding_rate_btc.json

# LightGBM hyperparameter search (validation-only selection; negative result, see above)
.venv/bin/python3 scripts/tune_lightgbm_hyperparams.py \
  --candles state/btc_1m_candles_real_2025_2026.parquet

# Fetch Binance Vision book-depth/OI/long-short archives + yfinance cross-asset
# (real bulk data, keyless; negative result, see Follow-up 3 above)
.venv/bin/python3 scripts/fetch_binance_vision_signals.py \
  --start 2025-01-01 --end 2026-08-11

# LightGBM with all 5 external signal sources (negative result, see Follow-up 3 above;
# writes to the same default output paths as every run above)
.venv/bin/python3 scripts/fill_and_test_arb_v3.py \
  --candles state/btc_1m_candles_real_2025_2026.parquet \
  --fear-greed state/fear_greed_index.json \
  --funding-rate state/binance_funding_rate_btc.json \
  --bookdepth state/binance_bookdepth_btc.parquet \
  --oi-longshort state/binance_oi_longshort_btc.parquet \
  --cross-asset state/cross_asset_daily.parquet

# 5-min horizon experiment (negative result, see Follow-up 4 above; isolated
# to model_tag=v8_horizon5, never touches the deployed _v3 artifacts)
.venv/bin/python3 scripts/fill_and_test_arb_v3.py \
  --candles state/btc_1m_candles_real_2025_2026.parquet \
  --horizon 5 --model-tag v8_horizon5 \
  --fear-greed state/_none.json --funding-rate state/_none.json \
  --bookdepth state/_none.parquet --oi-longshort state/_none.parquet \
  --cross-asset state/_none.parquet
# (needs FEATURE_COLUMNS temporarily restricted to the original 25 to avoid
# all-NaN placeholder columns for the 11 follow-up 2/3 signals zeroing out
# the eligible frame — see the one-off script used for this run)

# Kronos-style classifier
.venv/bin/python3 scripts/train_kronos_comprehensive.py \
  --candles state/btc_1m_candles_kronos_180d.parquet \
  --model-name kronos_volatility_classifier_v2.pth \
  --epochs 60 --output-dir state
```

`state/btc_1m_candles_real_2025_2026.parquet` and
`state/btc_1m_candles_kronos_180d.parquet` are slices of
`state/cache/btc_usd_1m_candles.parquet` (2025-01-01→2026-08-11 full span,
and its most recent 180 days respectively) — regenerate by filtering on
`timestamp` if the cache has moved on.
