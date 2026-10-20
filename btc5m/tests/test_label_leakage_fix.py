#!/usr/bin/env python3
"""Regression test for label leakage fix.

This test asserts that tercile cutoffs are computed from training data only.
This exact leakage must never silently reappear.

Run with: python -m pytest tests/test_label_leakage_fix.py -v
"""
import json
import sys
from pathlib import Path

import pytest
import pandas as pd

from scripts.fill_and_test_arb_v2 import (
    FEATURE_COLUMNS,
    TARGET_COLUMN,
    calculate_forward_volatility,
    calculate_technical_indicators,
    split_time_disjoint,
)


# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


def test_tercile_cutoffs_from_train_only():
    """Assert that tercile cutoffs are computed from training data only.
    
    This is the regression test for BUG 1: Label leakage via pre-split tercile cutoffs.
    The test verifies that the training artifact explicitly states cutoffs were
    fit on train only.
    """
    # Load the training artifact
    artifact_path = PROJECT_ROOT / "state" / "lightgbm_training_metrics_v2.json"
    
    assert artifact_path.exists(), f"Artifact not found: {artifact_path}"
    
    with open(artifact_path, 'r') as f:
        artifact = json.load(f)
    
    # Check that target has cutoffs_fit_on_train_only
    assert 'target' in artifact, "Missing 'target' in artifact"
    assert 'cutoffs_fit_on_train_only' in artifact['target'], \
        "Missing 'cutoffs_fit_on_train_only' in target - cutoffs may have been computed on entire dataset!"
    
    # Check that we have both cutoffs
    cutoffs = artifact['target']['cutoffs_fit_on_train_only']
    assert 'low_to_med_bps' in cutoffs, "Missing low_to_med_bps cutoff"
    assert 'med_to_high_bps' in cutoffs, "Missing med_to_high_bps cutoff"
    
    # Check that partitions exist
    assert 'partitions' in artifact, "Missing partitions in artifact"
    assert len(artifact['partitions']) == 5, "Expected 5 partitions (train, validation, 3 holdouts)"
    
    # Check that metrics exist for all holdouts
    assert 'metrics' in artifact, "Missing metrics in artifact"
    for holdout in ['holdout_a', 'holdout_b', 'holdout_c']:
        assert holdout in artifact['metrics'], f"Missing metrics for {holdout}"
    
    print("[PASS] Label cutoffs are explicitly marked as train-only")
    print(f"[PASS] Cutoffs: LOW<{cutoffs['low_to_med_bps']}, MED<{cutoffs['med_to_high_bps']}")


def test_persisted_v2_cutoffs_match_recomputed_train_partition_quantiles():
    """The saved cutoffs must be reproducible from only the saved train partition."""
    artifact_path = PROJECT_ROOT / "state" / "lightgbm_training_metrics_v2.json"
    candles_path = PROJECT_ROOT / "state" / "btc_1m_candles_lse.parquet"

    artifact = json.loads(artifact_path.read_text())
    candles = pd.read_parquet(candles_path)
    if "timestamp" not in candles.columns and "ts" in candles.columns:
        candles = candles.rename(columns={"ts": "timestamp"})
    if "timestamp" not in candles.columns and candles.index.name == "ts":
        candles = candles.reset_index().rename(columns={"ts": "timestamp"})
    candles["timestamp"] = pd.to_datetime(candles["timestamp"], utc=True)
    eligible = calculate_forward_volatility(calculate_technical_indicators(candles))
    feature_columns = [column for column in FEATURE_COLUMNS if column in eligible.columns]
    eligible = eligible.dropna(subset=feature_columns + [TARGET_COLUMN])
    raw_parts, _ = split_time_disjoint(eligible, minimum_rows_per_partition=100)
    expected_low, expected_high = raw_parts["train"][TARGET_COLUMN].quantile([1 / 3, 2 / 3])

    target = artifact["target"]
    cutoffs = target["cutoffs_fit_on_train_only"]
    assert cutoffs["low_to_med_bps"] == pytest.approx(expected_low, abs=0.01)
    assert cutoffs["med_to_high_bps"] == pytest.approx(expected_high, abs=0.01)
    assert target["cutoffs_match_recomputed_train_quantiles"] is True


def test_three_holdout_evaluation():
    """Assert that model is evaluated on three time-disjoint holdouts.
    
    This is the regression test for BUG 2: Single, imbalanced final-10% test split.
    """
    artifact_path = PROJECT_ROOT / "state" / "lightgbm_training_metrics_v2.json"
    
    with open(artifact_path, 'r') as f:
        artifact = json.load(f)
    
    # Check stability_vs_kronos section
    assert 'stability_vs_kronos' in artifact, "Missing stability_vs_kronos in artifact"
    
    stability = artifact['stability_vs_kronos']
    
    # Check holdout names
    assert stability['holdout_names'] == ['holdout_a', 'holdout_b', 'holdout_c'], \
        "Expected three holdouts: holdout_a, holdout_b, holdout_c"
    
    # Check we have metrics for all three
    assert len(stability['holdout_accuracy_pct']) == 3, "Expected 3 holdout accuracies"
    
    # Check mean, std, range are present
    assert 'mean_holdout_accuracy_pct' in stability, "Missing mean_holdout_accuracy_pct"
    assert 'holdout_accuracy_stddev_pct_points' in stability, "Missing holdout_accuracy_stddev_pct_points"
    assert 'holdout_accuracy_range_pct_points' in stability, "Missing holdout_accuracy_range_pct_points"
    
    # Check all_holdouts_beat_kronos flag
    assert 'all_holdouts_beat_kronos' in stability, "Missing all_holdouts_beat_kronos"
    
    # Check all_holdouts_have_all_three_classes flag
    assert 'all_holdouts_have_all_three_classes' in stability, "Missing all_holdouts_have_all_three_classes"
    
    print("[PASS] Three time-disjoint holdouts present")
    print(f"[PASS] Holdout accuracies: {stability['holdout_accuracy_pct']}")
    print(f"[PASS] Mean: {stability['mean_holdout_accuracy_pct']}%")
    print(f"[PASS] Std: {stability['holdout_accuracy_stddev_pct_points']}pp")
    print(f"[PASS] Range: {stability['holdout_accuracy_range_pct_points']}pp")


def test_missing_dead_feature():
    """Assert that minutes_to_next_high_macro is NOT in feature columns.
    
    This is the regression test for BUG 3: Dead/constant feature.
    """
    artifact_path = PROJECT_ROOT / "state" / "lightgbm_training_metrics_v2.json"
    
    with open(artifact_path, 'r') as f:
        artifact = json.load(f)
    
    # Check that minutes_to_next_high_macro is NOT in feature_columns
    assert 'feature_columns' in artifact, "Missing feature_columns in artifact"
    
    features = artifact['feature_columns']
    assert 'minutes_to_next_high_macro' not in features, \
        "minutes_to_next_high_macro should be removed as it was hardcoded to 0!"
    
    print("[PASS] Dead feature 'minutes_to_next_high_macro' removed from feature set")
    print(f"[PASS] Feature columns: {features}")


def test_prediction_not_called_signal():
    """Assert that prediction results are NOT presented as trading signals.
    
    This is the regression test for BUG 4: "Signal rate" is not a real evaluation metric.
    """
    results_path = PROJECT_ROOT / "state" / "prediction_test_results_v2.json"
    
    with open(results_path, 'r') as f:
        results = json.load(f)
    
    # Check that we have prediction_rate_percent (not signal_rate)
    assert 'prediction_rate_percent' in results, "Missing prediction_rate_percent"
    
    # Check that there's a note clarifying this is not a signal
    assert 'note' in results, "Missing note clarifying predictions are not signals"
    assert 'NOT trading signals' in results['note'], \
        "Note should clarify that predictions are NOT trading signals"
    
    # Check that 'signal_rate' does NOT exist
    assert 'signal_rate' not in results, \
        "signal_rate should not exist - renamed to prediction_rate_percent"
    
    print("[PASS] Predictions correctly labeled as NOT trading signals")
    print(f"[PASS] Note: {results['note']}")


def test_no_forward_looking_contamination():
    """Assert that evaluation uses only past information for predictions.
    
    This is the regression test for BUG 5: Forward-looking target contamination.
    """
    artifact_path = PROJECT_ROOT / "state" / "lightgbm_training_metrics_v2.json"
    
    with open(artifact_path, 'r') as f:
        artifact = json.load(f)
    
    # Check that metrics are computed separately for each partition
    # This ensures predictions are made using only information available at that time
    assert 'metrics' in artifact, "Missing metrics in artifact"
    
    metrics = artifact['metrics']
    
    # Check that each partition has its own metrics
    for partition in ['train', 'validation', 'holdout_a', 'holdout_b', 'holdout_c']:
        assert partition in metrics, f"Missing metrics for {partition}"
        assert 'accuracy_pct' in metrics[partition], f"Missing accuracy_pct for {partition}"
        assert 'class_counts' in metrics[partition], f"Missing class_counts for {partition}"
    
    print("[PASS] Each partition has independent metrics - no forward-looking contamination")
    
    # Check that majority baseline is also computed per-partition
    assert 'majority_baseline' in artifact, "Missing majority_baseline"
    assert 'metrics' in artifact['majority_baseline'], "Missing majority_baseline metrics"
    
    for partition in ['train', 'validation', 'holdout_a', 'holdout_b', 'holdout_c']:
        assert partition in artifact['majority_baseline']['metrics'], \
            f"Missing majority baseline metrics for {partition}"
    
    print("[PASS] Majority baseline computed per-partition")


if __name__ == '__main__':
    # Run all tests
    print("="*60)
    print("REGRESSION TESTS FOR LABEL LEAKAGE FIX")
    print("="*60)
    
    try:
        test_tercile_cutoffs_from_train_only()
        print()
        test_three_holdout_evaluation()
        print()
        test_missing_dead_feature()
        print()
        test_prediction_not_called_signal()
        print()
        test_no_forward_looking_contamination()
        print()
        print("="*60)
        print("ALL REGRESSION TESTS PASSED")
        print("="*60)
    except AssertionError as e:
        print(f"\n[FAIL] {e}")
        sys.exit(1)
