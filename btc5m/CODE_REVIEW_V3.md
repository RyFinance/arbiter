# Task 1 — Independent Code Review: `scripts/fill_and_test_arb_v3.py` vs `state/lightgbm_training_metrics_v3.json`

Reviewed end-to-end. Findings reported with line numbers, then a severity-ranked summary at the bottom. All line numbers are from the post-fix version that produced the JSON on 2026-08-06.

## (a) JSON-first save ordering — VERIFIED CORRECT

- Line **736**: `output_path = out_dir / "lightgbm_training_metrics_v3.json"`
- Line **737**: `output_path.write_text(json.dumps(artifact, indent=2, sort_keys=True))` — JSON written to disk here
- Line **738**: `print(f"\n[SAVED] {output_path}")`
- Line **742-747**: `joblib.dump(model_a, out_dir / "lightgbm_volatility_model_v3.pkl")` wrapped in try/except
- Line **749-754**: `joblib.dump(models_c, out_dir / "lightgbm_two_stage_v3.pkl")` wrapped in try/except

**Verdict: JSON is genuinely written first, before either pickle.** Each pickle is independently guarded so a single pickle failure does not destroy the JSON. The log output order therefore matches the code order. No defect.

Cross-check with the JSON artifact (`state/lightgbm_training_metrics_v3.json`): it does exist on disk with `generated_at_utc: 2026-08-06T07:31:21.199624+00:00`, matching the log line `[SAVED] .../lightgbm_training_metrics_v3.json` printed before `[SAVED] .../lightgbm_volatility_model_v3.pkl`. Confirmed.

## (b) `cutoffs_fit_on_train_only` — VERIFIED PURE

- Line **236**: `def label_from_training_quantiles(parts: ...)`
- Line **237**: `train_target = parts["train"][TARGET_COLUMN]` — explicitly the `"train"` key only
- Line **238**: `low_high_cutoff, high_cutoff = train_target.quantile([1/3, 2/3]).tolist()` — quantiles computed only from `train_target`
- Lines 241-248: `for name, part in parts.items()` then `np.select([lp[TARGET_COLUMN] <= low_high_cutoff, ...], ...)` — the cutoffs are *applied* to all partitions but *computed* from train only.

Confirmed upstream: line 221 `"train": df.iloc[:boundaries[0]].copy()` (the first 50% of the chronologically-sorted rows). Line 219 `boundaries = [int(len(df) * f) for f in (0.50, ...)]`. These are positional splits of a timestamp-sorted frame, so `parts["train"]` is genuinely the chronological first 50% with no leakage into validation/holdout.

The `target.cutoffs_match_recomputed_train_quantiles: true` field in the JSON is the runtime self-check (line 463-465 of `run_pipeline`): `np.allclose([...], recomputed.to_numpy(), rtol=0.0, atol=0.01)`. Confirmed the cutoffs in the JSON (`11.362024347271765` / `19.237681579132012`) are the train-only terciles and the recomputation matched within 0.01 bps.

**Verdict: no leakage.** The cutoffs trace only to `parts["train"]` rows.

## (c) Two-stage joint-probability confidence proxy — VERIFIED CORRECT

The `TwoStagePredictionModel.predict_proba_proxy` method (lines 468-479):

```
470: s1_proba = self.s1.predict_proba(part[self.feature_cols], num_iteration=self.s1_best)
471: p_notlow = s1_proba[:, 1]                       # P(notLOW | features)
472: joint_probs = np.zeros((len(part), 3), dtype=float)
473: joint_probs[:, 0] = 1.0 - p_notlow              # P(LOW) = 1 - P(notLOW)
474: notlow_idx = s1_proba.argmax(axis=1) == 1
475: if notlow_idx.any():
476:     s2_proba = self.s2.predict_proba(part.loc[notlow_idx, self.feature_cols], num_iteration=self.s2_best)
477:     joint_probs[notlow_idx, 1] = p_notlow[notlow_idx] * s2_proba[:, 0]     # P(MED) = P(notLOW) * P(MED|notLOW)
478:     joint_probs[notlow_idx, 2] = p_notlow[notlow_idx] * s2_proba[:, 1]      # P(HIGH) = P(notLOW) * P(HIGH|notLOW)
479: return joint_probs
```

**Math check** — by conditional probability:
- P(LOW) = 1 − P(notLOW) ✓
- P(MED) = P(notLOW ∩ MED) = P(notLOW) × P(MED | notLOW) ✓
- P(HIGH) = P(notLOW ∩ HIGH) = P(notLOW) × P(HIGH | notLOW) ✓

These three sum to 1.0 when the argmax of stage 1 is "notLOW": `P(LOW) + P(MED) + P(HIGH) = (1 − p) + (p × s2[0]) + (p × s2[1])`. Since `s2_proba[:, 0] + s2_proba[:, 1] = 1.0` (LightGBM binary classifier), this equals `(1 − p) + p × (s2[0] + s2[1]) = (1 − p) + p = 1.0` ✓.

When the argmax of stage 1 is "LOW" (i.e. `notlow_idx == False`), `joint_probs[:, 0] = 1 - p_notlow = 1.0` and MED/HIGH stay 0, summing to 1.0 ✓.

**Comparison with the precision-at-confidence sweep**: the sweep at line 354-365 calls `_precision_at_confidence_sweep(part["target"].to_numpy(), joint_probs)` where `joint_probs` is exactly the output of `predict_proba_proxy`. `_precision_at_confidence` line 313: `max_probs = probs.max(axis=1)`, `fired_mask = max_probs >= threshold`, `preds = probs.argmax(axis=1)`. The sweep thus uses argmax of `joint_probs` as the predicted class and the max of `joint_probs` as the confidence — which are both well-founded given the joint-sums-to-1 property.

**Verdict: the proxy is mathematically sound and is what the precision-at-confidence sweep assumes.**

## (d) Stage 2 row selection — INTENTIONAL DESIGN DIVERGENCE FROM USER'S HYPOTHESIS, NOT A BUG

The user's hypothesis was: *"stage 2's training rows must exclude anything stage 1 would have abstained on, if that's the design — confirm what the actual row-selection logic is."*

Actual code (lines 514-519):
```
514: # Stage 2: MED vs HIGH on notLOW rows (target in {1,2} -> {0,1})
515: notlow_train = parts["train"]["target"].isin([1, 2])
516: notlow_val = parts["validation"]["target"].isin([1, 2])
517: if notlow_train.sum() < 50 or notlow_val.sum() < 10: ...
518: ...
519: s2_train_y = (parts["train"].loc[notlow_train, "target"] == 2).astype(int)
```

Stage 2 trains on rows where the **ground truth** `target ∈ {1, 2}` (true MED or true HIGH), NOT on rows that stage 1 predicted as notLOW. The mask is on the literal true-label column (`parts["train"]["target"].isin([1, 2])`).

**This is a deliberate design choice, not a leak.** The standard two-stage cascade can be trained two ways:
1. **Truth-gated training** (what this code does): s2 sees ground-truth-notLOW rows. No training-time error propagation from s1. Standard practice — this is the textbook cascade.
2. **Prediction-gated training** (the user's hypothesis): s2 sees rows s1 *predicted* as notLOW. Would propagate s1's training-set predictions into s2's training data; can amplify s1's mistakes through s2.

Choosing (1) is the better ML practice. It does NOT introduce leakage from validation/holdout because `notlow_train` only ever indexes into `parts["train"]` — never `parts["validation"]` or any holdout. Stage 2's `eval_set` (line 524) likewise only uses `parts["validation"].loc[notlow_val, ...]` (the validation partition's true-notLOW rows).

**However, there is a subtle semantic gap to flag**: at INFERENCE time (line 474), the conditional pipeline uses `notlow_idx = s1_proba.argmax(axis=1) == 1` (stage 1's *prediction*), not the ground truth. So at inference, stage 2 sees rows where stage 1 was confident enough to predict notLOW; at training, stage 2 saw all true-notLOW rows. This is the standard cascade train/inference skew — the training distribution for s2 is cleaner than what s2 sees at inference (where s1's false-positives leak in). This is *expected*, not a bug, but it does explain why Holdout C precision varies non-trivially with confidence threshold: at low thresholds, s1 sends many uncertain rows into s2, including s1's false positives.

**Verdict: this is the standard cascade and not a bug, but the training/inference row-selection asymmetry should be documented as known because it explains the confidence-threshold sensitivity reported in Task 2b.**

## Other findings (lower severity)

### F1 — Cosmetic: stale `note` field in JSON contradicts the JSON's own `non_nan_pct`

In `scripts/fill_and_test_arb_v3.py` line 565-571 (`run_pipeline`), the artifact dict hard-codes:
```python
"note": "Historical HIGH-impact macro events are NOT available for the May-Aug 2026 training window via the LSE calendar or Forex Factory public feed. Feature is NaN for the training period."
```

This note is *factually wrong* — the very same artifact reports `macro_feature_coverage.non_nan_pct: 100.0`. The reason it's wrong was explained in Task 4 of the prior report: `searchsorted` returns a finite positive value for every candle because the 35 calendar events all lie *after* every candle in the dataset, so the "next event" exists for every row. The note should read: *"Feature is finite (100.0% non-NaN) but predictively inert — permutation importance = 0.0 — because it is a near-monotonic countdown to a small cluster of out-of-distribution future calendar events, not a per-row predictive signal. Recommend dropping from FEATURE_COLUMNS."* **Severity: low (cosmetic, but misleadingly inaccurate).**

### F2 — Dead helper `_two_stage_confidence_sweep`

Line 367 defines `_two_stage_confidence_sweep` but it is never called anywhere (grep confirmed only the definition). Its functionality was folded into `TwoStagePredictionModel.predict_proba_proxy`. **Severity: trivial. Safe to leave or delete for cleanliness.**

### F3 — Variant B `_two_stage_confidence_sweep` "no probability output" reported in stdout is wrong artifact key

The stdout summary printer loop (around line 794) iterates tag values `("A_balanced", "B_explicit_weights", "C_two_stage")` but the `holdout_summary` dict uses key `"B_explicit"` (not `"B_explicit_weights"`). This is why the v3 artifact's stdout block printed "B_explicit_weights no probability output / no rows fired at 0.7" even though the JSON actually has B's precision-at-0.7 numbers (`fired=1082, precision=87.15%`, per `holdout_summary.B_explicit.precision_at_0_7.holdout_c`).

**Severity: low — only affects terminal stdout, NOT the canonical JSON.** But the user's session would have seen a misleading line. Fix: align the key names — either change line ~565 area to write `summary_variants["B_explicit_weights"]` or change the printer loop to use `"B_explicit"`. Recommend the first (rename to `B_explicit_weights` everywhere for consistency with the variant key).

### F4 — `recall` filtering on `precision_at_0_7` summary

In lines 643-650 (per-variant 0.7 lookup), if `sweep.get("available")` is False or threshold 0.7 is not in the list, `t07` stays None and is silently dropped from the summary. This is correct behavior but if a future variant's confidence sweep genuinely has no rows at 0.7 (e.g. flat variant A's 0.9 sweep shown in JSON has `fired_rows=0`), the field becomes None silently. **Severity: trivial** — visible from the JSON read in the prior task's report.

### F5 — `prediction_test_results_v2.json` "signal_rate" still named inconsistently with v2 fix

Not a v3 issue but the codebase still has `state/prediction_test_results_v2.json` with a `prediction_rate_percent` field — this is the v2 fix preserved (renamed from "signal_rate"), is accurate, no action needed. Noted for completeness only.

## Severity-ranked summary

| ID | Severity | One-liner | Code location |
|---|---|---|---|
| (a) | — | JSON write happens before pickles, each guarded | lines 736-754 — correct |
| (b) | — | Cutoffs computed from train partition only | lines 236-249 — correct |
| (c) | — | Joint-probability proxy math correct, sums to 1 | lines 468-479 — correct |
| (d) | INFO | Stage 2 trains on truth-notLOW rows (not s1 predictions); this is the standard cascade | lines 514-526 — correct, but train/inference asymmetry should be documented |
| F1 | LOW | Stale misleading `note` in macro_feature_coverage | line ~570 — should be reworded |
| F3 | LOW | stdout summary uses wrong key `B_explicit_weights` vs JSON's `B_explicit` | around line 794 — printer loop |
| F2 | TRIVIAL | Dead helper `_two_stage_confidence_sweep` | line 367 |
| F4 | TRIVIAL | Silent None handling on threshold lookup | lines 643-650 |

**No correctness bugs found.** The four (a)-(d) structural invariants the user asked for all hold. The only real fixes worth doing are F1 (reword the misleading macro-feature note) and F3 (rename key for consistency). Neither invalidates any number in the JSON.
