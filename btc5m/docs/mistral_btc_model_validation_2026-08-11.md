# Mistral BTC Model — Independent Adversarial Validation (2026-08-11)

**Status: 66.13% headline is a leakage/methodology artifact. Corrected full-frame evaluation shows modest real ranking signal but NO stable threshold. NOT ready for live testing.**

## Claim under test

The "Mistral BTC model" — reported 66.13% accuracy (up from 54.08%) on FEWER candles (5,000 vs ~9,200), "signal rate 32.20%", declared "ready for live testing" (`LSE_ARB_FILL_REPORT.md`).

**Identity clarification:** there is no separate "Mistral" model. "Mistral Vibe" is the AI that authored the reports. The model behind the claim is the **LightGBM volatility-regime classifier** trained by `scripts/fill_and_test_arb.py` (v1, self-labeled SUPERSEDED/INVALID):

| Artifact | Path |
|---|---|
| Training script (v1, 66.13% producer) | `scripts/fill_and_test_arb.py` (27,575 B) |
| Training script (54.08% era) | `scripts/train_lightgbm_comprehensive.py` |
| Data (66.13% run, 5,000 rows) | `state/btc_1m_candles_lse.parquet` |
| Data (auditable full frame, 139,523 rows) | `state/btc_1m_candles_lse_v3.parquet` |
| Saved model (66.13%) | `state/lightgbm_volatility_model.pkl` (8.2 MB, joblib LGBMClassifier, n_features_in_=25, feature names `Column_0..24`) |
| Metrics artifacts | `state/lightgbm_training_metrics.json`, `state/arbitrage_test_results.json` |

**Task defined by the v1 code:** 15-minute-forward volatility regime (LOW/MED/HIGH terciles of forward realized vol in bps). This is a volatility-regime classifier, NOT a price-direction model. Codex's offline evaluation (same 15-min horizon) uses a price-direction profitability target — different task, same horizon and methodology discipline.

**Validation script (new, created for this audit):** `scripts/adversarial_validate_mistral_v1.py`
**Run:** `.venv/bin/python scripts/adversarial_validate_mistral_v1.py` — EXIT 0, rerun reproducible. Full stdout in `/tmp/adv_validation_rerun.log`, JSON in `/tmp/adversarial_validation_results.json`.

## Methodology (identical discipline to Codex's `evaluate_btc_offline_profitability.py`)

- Chronological splits: 55% train + 3 disjoint holdouts
- **15-row embargo purge** before every boundary (mirrors `_purge_before_boundary`, EMBARGO_ROWS=15)
- Label cutoffs computed **train-only** (the documented fix; v1 computed them on the full frame — the leak)
- Same hyperparams as v1: multiclass 3, n_estimators=1000, lr=0.05, num_leaves=63, max_depth=5, class_weight=balanced, random_state=20260804
- Per-holdout: accuracy, majority-class baseline, per-class PR-AUC (one-vs-rest) vs prevalence baseline, signal rate

## Part 1 — Faithful reproduction of the claimed 66.13% run (5,000-row frame)

Reproduced EXACTLY: **66.13%** (stored 0.6612576; f1-macro 0.3375 matches stored 0.3375; test slice 493 rows, train 4,446 — matches stored).

| Metric | Value |
|---|---|
| Test slice class distribution | LOW 442 (89.66%), MED 36 (7.30%), HIGH 15 (3.04%) |
| **Model test accuracy** | **66.13%** |
| **Majority-class baseline on test** | **89.66%** |
| **Model vs majority baseline** | **−23.53 pp (BELOW baseline)** |
| Per-class PR-AUC vs prevalence | LOW 0.8932 vs 0.8966 (−0.0034) · MED 0.0671 vs 0.0730 (−0.0060) · HIGH 0.0891 vs 0.0304 (+0.0586) |
| Saved model check | `lightgbm_volatility_model.pkl` scores the same 66.13% on the same slice; full-frame pred dist LOW=4317/MED=549/HIGH=73 |

**Signal rate "32.20%" defined** (`fill_and_test_arb.py:687-688`):
```python
signal_rate = results['lightgbm_signals'] / results['total_candles'] * 100
```
where `results['lightgbm_signals']` = count of rows the model predicted **HIGH** (lines 640-643), `total_candles` = **entire frame (5,000) including training rows**. It is a raw HIGH-prediction frequency, not a tradeable-signal metric and not restricted to the test set. Honest redefinition on valid (non-NaN) rows: 1610/4939 = 32.60%. The "ready for live testing" print fires whenever `signal_rate > 0` (line 691).

## Part 2 — Corrected chronological evaluation, 5,000-row frame

Train-only cutoffs, 55/3-holdout split, 15-row embargo. **The 66.13% collapses.**

| Holdout | Rows | Model acc | Majority baseline | Delta vs headline | HIGH PR-AUC vs prevalence |
|---|---|---|---|---|---|
| A | 726 | 25.07% | 55.23% | −41.06 pp | 0.1856 vs 0.1749 (+1.07 pp) |
| B | 726 | 49.59% | 33.88% | −16.54 pp | 0.6577 vs 0.3389 (+31.88 pp) |
| C | 741 | 39.41% | 95.95% | −26.72 pp | 0.0166 vs 0.0202 (−0.36 pp) |
| **Mean** | | **38.02%** | **61.69%** | | |

Beats majority on **1 of 3** holdouts. Thresholded binary HIGH (p≥0.5→HIGH): beats baseline on 1 of 3 (B: 73.55% vs 66.12%; C: 97.71% vs 97.98% baseline).

## Part 3 — Corrected chronological evaluation, 139,523-row v3 full frame

Same discipline on the auditable full frame (same data Codex used).

| Holdout | Rows | Model acc | Majority baseline | Delta vs headline | HIGH PR-AUC vs prevalence |
|---|---|---|---|---|---|
| A (06-23→07-07) | 20,905 | 55.15% | 43.04% | −10.97 pp | 0.7983 vs 0.4304 (+36.79 pp) |
| B (07-07→07-22) | 20,904 | 60.52% | 38.32% | −5.61 pp | 0.6874 vs 0.2408 (+44.67 pp) |
| C (07-22→08-05) | 20,919 | 56.26% | 49.67% | −9.87 pp | 0.6157 vs 0.1678 (+44.79 pp) |
| **Mean** | | **57.31%** | **43.67%** | | |

Beats majority on **3 of 3** holdouts. HIGH PR-AUC above prevalence on **3 of 3** (+36.8 to +44.8 pp).

**BUT the tradeable threshold does not hold up:**
| Holdout | Thresholded binary acc | Binary majority baseline | Delta |
|---|---|---|---|
| A | 67.06% | 56.96% | +10.10 pp |
| B | 76.47% | 75.92% | +0.55 pp |
| C | 77.80% | 83.22% | **−5.42 pp** |

No stable binary threshold across all holdouts. LOW-class PR-AUC is also strong on B/C (0.81/0.83) — the model mostly learned "quiet vs active" volatility regimes, which vary by epoch.

## Part 4 — Leakage audit

1. **Label tercile cutoffs on the full frame** (`fill_and_test_arb.py:189`): `quantile([0.333, 0.667])` computed on ALL 5,000 rows **before** splitting → distribution information leaks into the labels.
2. **Single final-10% test slice** (`fill_and_test_arb.py:315-316`): test set is 89.66% one class (LOW) — majority baseline alone (89.66%) beats the reported 66.13% by 23.5 pp.
3. **Feature diff 54% → 66% runs: IDENTICAL.** Both runs use the same 25 `FEATURE_COLUMNS` in the same order (programmatically verified from both scripts). **No features were added between the runs** — the +12.05 pp jump comes from the broken split/labels, not new features.
4. **No post-cutoff features found in the v1 builder** (no negative-shift/future-looking usage detected programmatically); all features are causal rolling/OHLCV indicators. However `minutes_to_next_high_macro` is **hardcoded to 0** (`fill_and_test_arb.py:166`) — a dead constant feature.
5. **Signal rate definition** is prediction frequency over the full frame including train rows (Part 1).

## Part 5 — Verdict

**66.13% is a leakage/methodology artifact, not a real result.** Three independent facts:
1. On the claim's own test slice, the majority-class baseline (89.66%) beats the model (66.13%) by 23.53 pp.
2. Under corrected chronological splits + embargo on the same 5,000-row frame, mean accuracy is 38.02% vs 61.69% baseline.
3. The +12.05 pp vs the 54% run came from a broken split and full-frame label cutoffs — the feature list is byte-identical between runs.

**Fair comparison vs Codex's BTC findings (same data, same discipline):** Codex (docs/btc_offline_chronological_evaluation_2026-08-11.md) found majority baseline ~90%, PR-AUC ranking signal present but **no stable threshold**, verdict "verified-fail" for go-live. This audit reproduces the same shape on the v1 model: on the full 139k frame it beats majority on all holdouts and HIGH PR-AUC is consistently above prevalence (+37 to +45 pp), but no binary threshold beats baseline on all holdouts (fails on C). **Both results agree: ranking signal exists, no stable threshold, not go-live ready.**

**Bottom line:** the 66.13% / "ready for live testing" claim is invalid as stated. The underlying model contains genuine but modest volatility-regime ranking signal on the full frame (consistent with Codex's finding), and any future use must be gated by a threshold validated out-of-sample with this embargoed chronological methodology. v2/v3 pipelines (`fill_and_test_arb_v2.py`/`_v3.py`) already fix the cutoff leak but still lack embargo purging — their holdout numbers should be re-audited with a 15-row embargo before reuse.

## Artifacts

- `scripts/adversarial_validate_mistral_v1.py` — reproducible validation script
- `/tmp/adversarial_validation_results.json` — machine-readable results
- `/tmp/adv_validation_rerun.log` — full stdout of the rerun (EXIT 0)
- Every number above was produced by `.venv/bin/python scripts/adversarial_validate_mistral_v1.py` on 2026-08-11 and is reproducible.
