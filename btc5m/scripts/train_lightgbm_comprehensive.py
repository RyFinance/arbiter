#!/usr/bin/env python3
"""Comprehensive LightGBM training for BTC volatility regime classification.

This script trains a LightGBM classifier to predict volatility regimes (LOW/MED/HIGH)
based on BTC/USD price data from LSE. It uses ALL available data and optimizes
hyperparameters for maximum accuracy.

Usage:
    python scripts/train_lightgbm_comprehensive.py
    python scripts/train_lightgbm_comprehensive.py --use-parquet-only
    python scripts/train_lightgbm_comprehensive.py --days-back 365 --optimize
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import joblib
import lightgbm as lgb
import numpy as np
import pandas as pd
from lightgbm import LGBMClassifier
from sklearn.inspection import permutation_importance
from sklearn.metrics import (accuracy_score, confusion_matrix, f1_score, 
                              log_loss, precision_score, recall_score)
from sklearn.model_selection import train_test_split, TimeSeriesSplit
from sklearn.preprocessing import StandardScaler

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from polymarket_arb.clients.lse_client import LSEClient


# =============================================================================
# Configuration
# =============================================================================

# Feature engineering configuration
HORIZON_MINUTES = 15  # Predict next 15 minutes of volatility
FEATURE_COLUMNS = [
    "open", "high", "low", "close", "volume",
    "log_volume", "return_1m_bps", "rolling_vol_5m_bps",
    "rolling_vol_15m_bps", "rolling_vol_60m_bps", "atr_14m_bps",
    "atr_60m_bps", "minutes_to_next_high_macro", "session_bucket",
    "hour_sin", "hour_cos", "dayofweek_sin", "dayofweek_cos",
    "bollinger_band_width", "rsi_14", "macd_line", "macd_signal",
    "price_trend_5m", "price_trend_15m", "volume_trend_5m",
]

CLASS_NAMES = ["LOW", "MED", "HIGH"]
TARGET_COLUMN = "forward_realized_vol_bps"

# Default hyperparameters (will be optimized)
DEFAULT_PARAMS = {
    "objective": "multiclass",
    "num_class": 3,
    "boosting_type": "gbdt",
    "metric": ["multi_logloss", "multi_error"],
    "n_estimators": 1000,
    "learning_rate": 0.05,
    "num_leaves": 31,
    "max_depth": -1,
    "min_child_samples": 20,
    "subsample": 0.8,
    "colsample_bytree": 0.8,
    "reg_alpha": 0.0,
    "reg_lambda": 0.0,
    "class_weight": "balanced",
    "random_state": 20260804,
    "n_jobs": -1,
    "verbosity": -1,
}

# Hyperparameter optimization space
PARAM_GRID = {
    "learning_rate": [0.01, 0.05, 0.1],
    "num_leaves": [15, 31, 63],
    "max_depth": [3, 5, 7, -1],
    "min_child_samples": [10, 20, 50],
    "subsample": [0.6, 0.8, 1.0],
    "colsample_bytree": [0.6, 0.8, 1.0],
    "reg_alpha": [0.0, 0.1, 0.5],
    "reg_lambda": [0.0, 0.1, 0.5],
}


@dataclass
class TrainingConfig:
    use_parquet: bool = True
    days_back: int = 90
    fetch_lse: bool = True
    optimize_hyperparams: bool = True
    test_size: float = 0.2
    val_size: float = 0.1
    random_state: int = 20260804
    n_splits: int = 5
    early_stopping_rounds: int = 100
    output_dir: Path = Path("state")


# =============================================================================
# Data Loading
# =============================================================================

def load_all_btc_parquet_data(parquet_dir: Path = None) -> pd.DataFrame:
    """Load all BTC candle data from parquet files."""
    if parquet_dir is None:
        parquet_dir = PROJECT_ROOT / "state"
    
    # First, try to load pre-converted candle data
    candle_files = list(parquet_dir.glob("*candle*.parquet"))
    
    if candle_files:
        print(f"Loading {len(candle_files)} candle parquet files...")
        all_data = []
        for pf in candle_files:
            try:
                df = pd.read_parquet(pf)
                print(f"  Loaded {len(df)} rows from {pf.name}")
                all_data.append(df)
            except Exception as e:
                print(f"  Error loading {pf.name}: {e}")
        
        if all_data:
            combined = pd.concat(all_data, ignore_index=True)
            if "timestamp" in combined.columns:
                combined = combined.drop_duplicates(subset=["timestamp"]).sort_values("timestamp")
            elif "ts" in combined.columns:
                combined = combined.drop_duplicates(subset=["ts"]).sort_values("ts")
            print(f"Total candle rows from parquet: {len(combined)}")
            return combined
    
    # If no candle files, try to load and convert tick data
    tick_files = list(parquet_dir.glob("*btc*.parquet"))
    
    if not tick_files:
        print(f"No BTC parquet files found in {parquet_dir}")
        return pd.DataFrame()
    
    print(f"Loading {len(tick_files)} BTC tick parquet files...")
    all_data = []
    for pf in tick_files:
        try:
            df = pd.read_parquet(pf)
            print(f"  Loaded {len(df)} rows from {pf.name}")
            all_data.append(df)
        except Exception as e:
            print(f"  Error loading {pf.name}: {e}")
    
    if not all_data:
        return pd.DataFrame()
    
    # Combine all tick data
    combined_ticks = pd.concat(all_data, ignore_index=True)
    
    # Check if we have tick data (with 'price', 'volume', 'ts') or candle data
    if 'price' in combined_ticks.columns and 'volume' in combined_ticks.columns:
        print(f"Converting {len(combined_ticks)} ticks to OHLCV candles...")
        # Convert tick data to 1-minute OHLCV candles
        candles = convert_ticks_to_candles(combined_ticks)
        print(f"Generated {len(candles)} 1-minute candles from tick data")
        return candles
    elif 'open' in combined_ticks.columns and 'close' in combined_ticks.columns:
        # Already candle data
        print(f"Total rows from parquet: {len(combined_ticks)}")
        if "timestamp" in combined_ticks.columns:
            combined_ticks = combined_ticks.drop_duplicates(subset=["timestamp"]).sort_values("timestamp")
        elif "ts" in combined_ticks.columns:
            combined_ticks = combined_ticks.drop_duplicates(subset=["ts"]).sort_values("ts")
        return combined_ticks
    else:
        print(f"Unknown data format in parquet files")
        return pd.DataFrame()


def convert_ticks_to_candles(df: pd.DataFrame, timeframe_min: int = 1) -> pd.DataFrame:
    """Convert tick data to OHLCV candles."""
    df = df.copy()
    
    # Ensure we have the right columns
    ts_col = 'ts' if 'ts' in df.columns else 'timestamp'
    price_col = 'price' if 'price' in df.columns else ('close' if 'close' in df.columns else None)
    volume_col = 'volume' if 'volume' in df.columns else ('vol' if 'vol' in df.columns else None)
    
    if price_col is None or volume_col is None:
        print(f"Error: Cannot find price and volume columns. Available: {list(df.columns)}")
        return pd.DataFrame()
    
    # Convert timestamp
    print(f"Converting timestamps from {ts_col}...")
    
    # Check if ts is in milliseconds (common for crypto data)
    if df[ts_col].dtype in ['int64', 'float64']:
        # Try to determine if it's milliseconds or seconds
        sample_val = df[ts_col].iloc[0]
        if sample_val > 1e12:  # Probably nanoseconds
            df['timestamp'] = pd.to_datetime(df[ts_col], unit='ns', utc=True, errors='coerce')
        elif sample_val > 1e9:  # Probably milliseconds
            df['timestamp'] = pd.to_datetime(df[ts_col], unit='ms', utc=True, errors='coerce')
        else:
            df['timestamp'] = pd.to_datetime(df[ts_col], unit='s', utc=True, errors='coerce')
    else:
        df['timestamp'] = pd.to_datetime(df[ts_col], utc=True, errors='coerce')
    
    df = df.dropna(subset=['timestamp'])
    
    # Filter to reasonable date range (2025 onwards)
    df = df[df['timestamp'] >= pd.Timestamp('2025-01-01', tz='UTC')]
    
    # Set timestamp as index for resampling
    df = df.set_index('timestamp').sort_index()
    
    print(f"Loaded {len(df)} valid ticks")
    
    # Calculate dollar volume (price * volume)
    df['dollar_volume'] = df[price_col] * df[volume_col]
    
    # Resample to 1-minute candles
    print(f"Resampling to {timeframe_min}-minute candles...")
    
    # Create a DataFrame with all needed columns
    resample_df = df[[price_col, volume_col, 'dollar_volume']].copy()
    
    # Resample and aggregate
    ohlcv = resample_df.resample(f'{timeframe_min}min').agg({
        price_col: ['first', 'max', 'min', 'last'],
        volume_col: 'sum',
        'dollar_volume': 'sum',
    })
    
    # Flatten multi-index columns
    ohlcv.columns = ['open', 'high', 'low', 'close', 'volume', 'dollar_volume']
    ohlcv = ohlcv.reset_index()
    
    # Clean up
    ohlcv = ohlcv.dropna()
    ohlcv = ohlcv[(ohlcv['high'] > 0) & (ohlcv['low'] > 0) & (ohlcv['close'] > 0)]
    
    print(f"Generated {len(ohlcv)} {timeframe_min}-minute candles")
    
    return ohlcv


async def fetch_lse_candles_days_back(days: int = 90) -> pd.DataFrame:
    """Fetch LSE BTC/USD 1m candles for the last N days."""
    client = LSEClient()
    try:
        end = datetime.now(UTC)
        start = end - timedelta(days=days)
        
        print(f"Fetching LSE candles from {start.isoformat()} to {end.isoformat()}...")
        
        rows = await client.fetch_candles(
            symbol="BTC/USD",
            timeframe="1m",
            start=start.strftime("%Y-%m-%d"),
            end=end.strftime("%Y-%m-%d"),
            limit=50000,
        )
        return normalize_lse_candles(rows)
    finally:
        await client.aclose()


def normalize_lse_candles(rows: List[Dict[str, Any]]) -> pd.DataFrame:
    """Normalize LSE candle data."""
    if not rows:
        return pd.DataFrame()
    
    frame = pd.DataFrame(rows)
    timestamp_column = "ts" if "ts" in frame.columns else "timestamp"
    
    required = {timestamp_column, "open", "high", "low", "close", "volume"}
    missing = sorted(required.difference(frame.columns))
    if missing:
        print(f"Warning: Missing columns {missing}")
        return pd.DataFrame()
    
    result = frame[[timestamp_column, "open", "high", "low", "close", "volume"]].rename(
        columns={timestamp_column: "timestamp"}
    )
    result["timestamp"] = pd.to_datetime(result["timestamp"], utc=True, errors="coerce")
    for column in ("open", "high", "low", "close", "volume"):
        result[column] = pd.to_numeric(result[column], errors="coerce")
    
    result = result.dropna().drop_duplicates(subset="timestamp").sort_values("timestamp")
    result = result[(result["close"] > 0) & (result["high"] > 0) & (result["low"] > 0)]
    return result.reset_index(drop=True)


# =============================================================================
# Feature Engineering
# =============================================================================

def add_temporal_features(df: pd.DataFrame) -> pd.DataFrame:
    """Add temporal features to the dataframe."""
    df = df.copy()
    
    # Hour features (cyclical)
    df["hour"] = df["timestamp"].dt.hour
    df["hour_sin"] = np.sin(2 * np.pi * df["hour"] / 24)
    df["hour_cos"] = np.cos(2 * np.pi * df["hour"] / 24)
    
    # Day of week features (cyclical)
    df["dayofweek"] = df["timestamp"].dt.dayofweek
    df["dayofweek_sin"] = np.sin(2 * np.pi * df["dayofweek"] / 7)
    df["dayofweek_cos"] = np.cos(2 * np.pi * df["dayofweek"] / 7)
    
    # Session bucket (Asia, Europe, US, Pacific)
    df["session_bucket"] = pd.cut(
        df["hour"],
        bins=[-1, 6, 12, 20, 24],
        labels=False,
        include_lowest=True,
        right=True,
    ).astype("int8")
    
    return df


def add_technical_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Add technical indicators as features."""
    df = df.copy()
    
    # Log volume
    df["log_volume"] = np.log1p(df["volume"])
    
    # Returns (log returns for stability)
    df["return_1m_bps"] = np.log(df["close"] / df["close"].shift(1)) * 10000
    
    # Rolling volatility (standard deviation of returns)
    df["rolling_vol_5m_bps"] = df["return_1m_bps"].rolling(5).std().values * 1  # Already in bps
    df["rolling_vol_15m_bps"] = df["return_1m_bps"].rolling(15).std().values * 1
    df["rolling_vol_60m_bps"] = df["return_1m_bps"].rolling(60).std().values * 1
    
    # Average True Range (ATR)
    high_low = df["high"] - df["low"]
    high_close = np.abs(df["high"] - df["close"].shift(1))
    low_close = np.abs(df["low"] - df["close"].shift(1))
    true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    
    df["atr_14m_bps"] = (true_range.rolling(14).mean() / df["close"] * 10000).values
    df["atr_60m_bps"] = (true_range.rolling(60).mean() / df["close"] * 10000).values
    
    # Bollinger Bands
    df["bb_middle"] = df["close"].rolling(20).mean()
    df["bb_std"] = df["close"].rolling(20).std()
    df["bollinger_band_width"] = (df["bb_std"] / df["bb_middle"] * 100).values
    
    # RSI
    delta = df["close"].diff()
    gain = (delta.where(delta > 0, 0)).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / loss
    df["rsi_14"] = 100 - (100 / (1 + rs)).values
    
    # MACD
    exp12 = df["close"].ewm(span=12, adjust=False).mean()
    exp26 = df["close"].ewm(span=26, adjust=False).mean()
    df["macd_line"] = (exp12 - exp26).values
    df["macd_signal"] = df["macd_line"].ewm(span=9, adjust=False).mean().values
    
    # Price trends (linear regression slope over windows)
    for window in [5, 15]:
        cols = df["close"].rolling(window).apply(
            lambda x: np.polyfit(np.arange(len(x)), x, 1)[0] if len(x) == window else np.nan, raw=False
        )
        df[f"price_trend_{window}m"] = cols.values
    
    # Volume trend
    df["volume_trend_5m"] = df["volume"].rolling(5).apply(
        lambda x: np.polyfit(np.arange(len(x)), x, 1)[0] if len(x) == 5 else np.nan, raw=False
    ).values
    
    return df


def add_macro_features(df: pd.DataFrame, high_events: List[pd.Timestamp]) -> pd.DataFrame:
    """Add macro event proximity features.
    
    Returns minutes to next high-impact macro event, or NaN if no upcoming event.
    """
    df = df.copy()
    
    if not high_events:
        df["minutes_to_next_high_macro"] = 0.0  # Use 0 if no events
        return df
    
    # Filter events to those that are after our first timestamp
    df_min_time = df["timestamp"].min()
    df_max_time = df["timestamp"].max()
    
    # Filter to events within a reasonable range
    relevant_events = [
        event for event in high_events 
        if df_min_time - pd.Timedelta(days=1) <= event <= df_max_time + pd.Timedelta(days=30)
    ]
    
    if not relevant_events:
        df["minutes_to_next_high_macro"] = 0.0
        return df
    
    event_ns = np.array([event.value for event in relevant_events], dtype=np.int64)
    candle_ns = df["timestamp"].astype("int64").to_numpy()
    
    positions = np.searchsorted(event_ns, candle_ns, side="left")
    result = np.full(len(df), 0.0, dtype=float)  # Default to 0
    valid = positions < len(event_ns)
    result[valid] = (event_ns[positions[valid]] - candle_ns[valid]) / 60_000_000_000
    
    df["minutes_to_next_high_macro"] = result
    
    # Also add: is near macro event (< 30 minutes)
    df["near_macro_event"] = (df["minutes_to_next_high_macro"].abs() < 30).astype(int)
    
    return df


def add_target_feature(df: pd.DataFrame, horizon_minutes: int = 15) -> pd.DataFrame:
    """Add forward realized volatility target feature."""
    df = df.copy()
    
    log_return = np.log(df["close"])
    future_squared_returns = sum(
        log_return.diff().shift(-step).pow(2) 
        for step in range(1, horizon_minutes + 1)
    )
    
    contiguous_future = df["timestamp"].shift(-horizon_minutes).sub(df["timestamp"]).eq(
        pd.Timedelta(minutes=horizon_minutes)
    )
    
    forward_vol = np.sqrt(future_squared_returns) * 10000  # Convert to bps
    forward_vol[~contiguous_future] = np.nan
    
    df[TARGET_COLUMN] = forward_vol.values
    
    return df


def create_label_from_target(df: pd.DataFrame, target_col: str = TARGET_COLUMN) -> pd.DataFrame:
    """Create LOW/MED/HIGH labels from forward realized volatility."""
    df = df.copy()
    
    # Use training data to determine quantile cutoffs
    train_target = df[target_col].dropna()
    if len(train_target) == 0:
        raise ValueError("No valid target values for labeling")
    
    low_high_cutoff, high_cutoff = train_target.quantile([1/3, 2/3]).tolist()
    
    if not np.isfinite([low_high_cutoff, high_cutoff]).all() or low_high_cutoff >= high_cutoff:
        raise ValueError(f"Invalid quantile cutoffs: {low_high_cutoff}, {high_cutoff}")
    
    # Create labels: 0=LOW, 1=MED, 2=HIGH
    df["target"] = np.select(
        [
            df[target_col] <= low_high_cutoff,
            df[target_col] <= high_cutoff,
        ],
        [0, 1],
        default=2,
    ).astype(int)
    
    return df, {"low_to_med_bps": float(low_high_cutoff), "med_to_high_bps": float(high_cutoff)}


# =============================================================================
# Data Preparation Pipeline
# =============================================================================

def prepare_data_pipeline(
    parquet_data: pd.DataFrame,
    lse_data: pd.DataFrame,
    high_events: List[pd.Timestamp],
    horizon_minutes: int = HORIZON_MINUTES,
) -> pd.DataFrame:
    """Complete data preparation pipeline."""
    
    # Combine all data
    if not parquet_data.empty and not lse_data.empty:
        # Check for overlapping timestamps
        if "timestamp" in parquet_data.columns and "timestamp" in lse_data.columns:
            parquet_data = parquet_data.set_index("timestamp")
            lse_data = lse_data.set_index("timestamp")
            combined = pd.concat([parquet_data, lse_data])
            combined = combined[~combined.index.duplicated(keep="first")].sort_index()
            combined = combined.reset_index()
        else:
            combined = pd.concat([parquet_data, lse_data], ignore_index=True)
            combined = combined.drop_duplicates().sort_values("timestamp")
    elif not parquet_data.empty:
        combined = parquet_data
    elif not lse_data.empty:
        combined = lse_data
    else:
        raise ValueError("No data available from either source")
    
    print(f"Combined dataset: {len(combined)} rows")
    
    # Add temporal features
    combined = add_temporal_features(combined)
    
    # Add technical indicators
    combined = add_technical_indicators(combined)
    
    # Add macro features (optional - will have NaN if events are outside data range)
    try:
        combined = add_macro_features(combined, high_events)
    except Exception as e:
        print(f"Warning: Could not add macro features: {e}")
        # Add a placeholder column to avoid errors
        combined['minutes_to_next_high_macro'] = 0.0
    
    # Add target feature (forward realized volatility)
    combined = add_target_feature(combined, horizon_minutes)
    
    # Drop rows with missing target
    combined = combined.dropna(subset=[TARGET_COLUMN])
    print(f"After adding target: {len(combined)} rows")
    
    # Create labels
    combined, cutoffs = create_label_from_target(combined, TARGET_COLUMN)
    
    # Drop rows with missing features
    feature_cols = [c for c in FEATURE_COLUMNS if c in combined.columns]
    
    # For features with NaN (due to rolling windows), we have two options:
    # 1. Fill NaN with 0 or mean
    # 2. Drop rows with NaN (which removes the warmup period)
    # We'll use option 2: drop rows with any NaN in feature columns
    # But first, check if we have any features with NaN
    nan_counts = combined[feature_cols].isna().sum()
    print(f"NaN counts per feature: {dict(nan_counts[nan_counts > 0])}")
    
    # Drop rows with NaN in feature columns
    combined = combined.dropna(subset=feature_cols)
    print(f"Final dataset: {len(combined)} rows, {len(feature_cols)} features")
    
    return combined, cutoffs


# =============================================================================
# Model Training
# =============================================================================

def train_lightgbm(
    X_train: pd.DataFrame,
    y_train: pd.Series,
    X_val: pd.DataFrame,
    y_val: pd.Series,
    params: Dict[str, Any] = None,
    early_stopping_rounds: int = 100,
    eval_set: List[Tuple] = None,
) -> LGBMClassifier:
    """Train LightGBM classifier with early stopping."""
    
    if params is None:
        params = DEFAULT_PARAMS.copy()
    
    if eval_set is None:
        eval_set = [(X_val, y_val)]
    
    print(f"Training LightGBM with params: {params}")
    print(f"Training set: {len(X_train)} samples")
    print(f"Validation set: {len(X_val)} samples")
    
    model = LGBMClassifier(**params)
    
    model.fit(
        X_train, y_train,
        eval_set=eval_set,
        eval_metric=params.get("metric", "multi_logloss"),
        callbacks=[
            lgb.early_stopping(stopping_rounds=early_stopping_rounds, verbose=True),
            lgb.log_evaluation(period=10),
        ],
    )
    
    print(f"Best iteration: {model.best_iteration_}")
    print(f"Feature importances:")
    for feature, importance in sorted(
        zip(X_train.columns, model.feature_importances_),
        key=lambda x: x[1], reverse=True
    ):
        print(f"  {feature}: {importance:.4f}")
    
    return model


def hyperparameter_optimization(
    X_train: pd.DataFrame,
    y_train: pd.Series,
    X_val: pd.DataFrame,
    y_val: pd.Series,
    param_grid: Dict[str, List] = None,
    n_iter: int = 20,
) -> Tuple[Dict[str, Any], float]:
    """Simple hyperparameter optimization via random search."""
    
    if param_grid is None:
        param_grid = PARAM_GRID
    
    import random
    from itertools import product
    
    best_params = DEFAULT_PARAMS.copy()
    best_accuracy = 0.0
    
    # Generate random parameter combinations
    param_names = list(param_grid.keys())
    param_values = [param_grid[name] for name in param_names]
    
    # For efficiency, use a smaller grid if too large
    total_combinations = 1
    for values in param_values:
        total_combinations *= len(values)
    
    if total_combinations > n_iter:
        # Random sampling
        combinations = []
        for _ in range(n_iter):
            combo = {name: random.choice(values) for name, values in param_grid.items()}
            combinations.append(combo)
    else:
        # Full grid search
        combinations = [dict(zip(param_names, combo)) for combo in product(*param_values)]
    
    print(f"Testing {len(combinations)} parameter combinations...")
    
    for i, combo in enumerate(combinations):
        params = DEFAULT_PARAMS.copy()
        params.update(combo)
        
        try:
            model = train_lightgbm(
                X_train, y_train, X_val, y_val,
                params=params,
                early_stopping_rounds=50,
            )
            
            y_pred = model.predict(X_val)
            accuracy = accuracy_score(y_val, y_pred)
            
            if accuracy > best_accuracy:
                best_accuracy = accuracy
                best_params = params.copy()
                print(f"  New best: {accuracy:.4f} with params: {combo}")
        except Exception as e:
            print(f"  Failed combo {i}: {e}")
    
    return best_params, best_accuracy


# =============================================================================
# Evaluation
# =============================================================================

def evaluate_model(
    model: LGBMClassifier,
    X: pd.DataFrame,
    y: pd.Series,
    dataset_name: str = "test",
) -> Dict[str, Any]:
    """Comprehensive model evaluation."""
    
    y_pred = model.predict(X)
    y_proba = model.predict_proba(X)
    
    accuracy = accuracy_score(y, y_pred)
    f1_macro = f1_score(y, y_pred, labels=[0, 1, 2], average="macro", zero_division=0)
    f1_weighted = f1_score(y, y_pred, labels=[0, 1, 2], average="weighted", zero_division=0)
    
    precision_macro = precision_score(y, y_pred, labels=[0, 1, 2], average="macro", zero_division=0)
    recall_macro = recall_score(y, y_pred, labels=[0, 1, 2], average="macro", zero_division=0)
    
    logloss = log_loss(y, y_proba, labels=[0, 1, 2])
    
    cm = confusion_matrix(y, y_pred, labels=[0, 1, 2])
    
    class_counts = {CLASS_NAMES[i]: int((y == i).sum()) for i in range(3)}
    class_accuracy = {
        CLASS_NAMES[i]: accuracy_score(y[y == i], y_pred[y == i]) if (y == i).any() else 0.0
        for i in range(3)
    }
    
    metrics = {
        "dataset": dataset_name,
        "accuracy": round(accuracy, 6),
        "accuracy_pct": round(accuracy * 100, 4),
        "f1_macro": round(f1_macro, 6),
        "f1_weighted": round(f1_weighted, 6),
        "precision_macro": round(precision_macro, 6),
        "recall_macro": round(recall_macro, 6),
        "log_loss": round(logloss, 6),
        "class_counts": class_counts,
        "class_accuracy": {k: round(v, 4) for k, v in class_accuracy.items()},
        "confusion_matrix": cm.tolist(),
        "n_samples": len(y),
    }
    
    print(f"\n--- {dataset_name.upper()} Metrics ---")
    print(f"Accuracy: {accuracy:.4f} ({accuracy*100:.2f}%)")
    print(f"F1 Macro: {f1_macro:.4f}")
    print(f"F1 Weighted: {f1_weighted:.4f}")
    print(f"Precision Macro: {precision_macro:.4f}")
    print(f"Recall Macro: {recall_macro:.4f}")
    print(f"Log Loss: {logloss:.4f}")
    print(f"Class counts: {class_counts}")
    print(f"Class accuracy: {class_accuracy}")
    print(f"Confusion Matrix:\n{cm}")
    
    return metrics


def permutation_feature_importance(
    model: LGBMClassifier,
    X: pd.DataFrame,
    y: pd.Series,
    n_repeats: int = 20,
) -> pd.DataFrame:
    """Compute permutation feature importance."""
    
    result = permutation_importance(
        model, X, y,
        scoring="accuracy",
        n_repeats=n_repeats,
        random_state=DEFAULT_PARAMS["random_state"],
        n_jobs=-1,
    )
    
    importance_df = pd.DataFrame({
        "feature": X.columns,
        "importance": result.importances_mean,
        "std": result.importances_std,
    }).sort_values("importance", ascending=False)
    
    print(f"\n--- Permutation Feature Importance ---")
    for _, row in importance_df.head(15).iterrows():
        print(f"  {row['feature']}: {row['importance']:.4f} ± {row['std']:.4f}")
    
    return importance_df


# =============================================================================
# Main Pipeline
# =============================================================================

def load_macro_events(calendar_path: Path = None) -> Tuple[List[pd.Timestamp], Dict[str, Any]]:
    """Load macro events from calendar file.
    
    The macro_events.json file contains timestamps as epoch seconds (float).
    """
    if calendar_path is None:
        calendar_path = PROJECT_ROOT / "state" / "macro_events.json"
    
    if not calendar_path.exists():
        print(f"Warning: Macro calendar not found at {calendar_path}")
        return [], {}
    
    try:
        payload = json.loads(calendar_path.read_text())
        events = payload.get("events", []) if isinstance(payload, dict) else payload
        
        high_times = []
        impact_counts = {}
        
        for event in events:
            if not isinstance(event, dict):
                continue
            impact = str(event.get("impact", "")).upper()
            impact_counts[impact] = impact_counts.get(impact, 0) + 1
            
            if impact != "HIGH" or event.get("timestamp") is None:
                continue
            
            try:
                # Try to parse timestamp - it could be epoch seconds, ISO string, or other
                ts_value = event["timestamp"]
                if isinstance(ts_value, (int, float)):
                    # Epoch timestamp - try seconds first
                    ts = pd.to_datetime(ts_value, unit='s', utc=True)
                else:
                    # Try as ISO string
                    ts = pd.to_datetime(str(ts_value), utc=True)
                high_times.append(ts)
            except (TypeError, ValueError, OverflowError):
                continue
        
        high_times = sorted(set(high_times))
        
        return high_times, {
            "path": str(calendar_path),
            "event_count": len(events),
            "high_event_count": len(high_times),
            "impact_counts": impact_counts,
        }
    except Exception as e:
        print(f"Error loading macro calendar: {e}")
        return [], {}


async def main():
    parser = argparse.ArgumentParser(description="Train comprehensive LightGBM volatility regime classifier")
    parser.add_argument("--use-parquet-only", action="store_true", help="Use only parquet data, no LSE fetch")
    parser.add_argument("--days-back", type=int, default=90, help="Number of days of LSE data to fetch")
    parser.add_argument("--optimize", action="store_true", help="Optimize hyperparameters")
    parser.add_argument("--output-dir", type=Path, default=Path("state"), help="Output directory")
    parser.add_argument("--test-size", type=float, default=0.2, help="Test set size")
    parser.add_argument("--val-size", type=float, default=0.1, help="Validation set size")
    
    args = parser.parse_args()
    
    # --use-parquet-only means only use parquet, no LSE fetch
    # So use_parquet=True, fetch_lse=False when use_parquet_only=True
    # Otherwise use_parquet=True (default), fetch_lse=True
    config = TrainingConfig(
        use_parquet=True,  # Always try to use parquet
        days_back=args.days_back,
        fetch_lse=not args.use_parquet_only,  # Only fetch LSE if not use_parquet_only
        optimize_hyperparams=args.optimize,
        test_size=args.test_size,
        val_size=args.val_size,
        output_dir=args.output_dir,
    )
    
    print("=" * 80)
    print("LightGBM Comprehensive Training Pipeline")
    print("=" * 80)
    
    # Step 1: Load data
    print("\n[Step 1] Loading data...")
    
    parquet_data = pd.DataFrame()
    if config.use_parquet:
        parquet_data = load_all_btc_parquet_data()
        if not parquet_data.empty:
            print(f"Loaded {len(parquet_data)} rows from parquet files")
    
    lse_data = pd.DataFrame()
    if config.fetch_lse:
        lse_data = await fetch_lse_candles_days_back(config.days_back)
        if not lse_data.empty:
            print(f"Fetched {len(lse_data)} rows from LSE API")
    
    # Load macro events
    high_events, macro_metadata = load_macro_events()
    print(f"Loaded {len(high_events)} high-impact macro events")
    
    if parquet_data.empty and lse_data.empty:
        print("ERROR: No data available from any source!")
        return 1
    
    # Step 2: Prepare data
    print("\n[Step 2] Preparing features and targets...")
    
    try:
        df, cutoffs = prepare_data_pipeline(parquet_data, lse_data, high_events)
        print(f"Final dataset: {len(df)} rows")
        print(f"Target cutoffs: {cutoffs}")
        
        # Check class distribution
        class_dist = df["target"].value_counts().sort_index()
        print(f"Class distribution: {dict(class_dist)}")
        
        if len(df) < 1000:
            print("WARNING: Dataset is small. Consider more data.")
    except Exception as e:
        print(f"ERROR in data preparation: {e}")
        import traceback
        traceback.print_exc()
        return 1
    
    # Step 3: Split data
    print("\n[Step 3] Splitting data...")
    
    # Time-based split (important for financial data)
    df = df.sort_values("timestamp")
    
    # Use time series split
    tscv = TimeSeriesSplit(n_splits=config.n_splits)
    
    # For hyperparameter optimization, use a simpler train/val/test split
    train_size = int(len(df) * (1 - config.test_size - config.val_size))
    val_size = int(len(df) * config.val_size)
    
    train_df = df.iloc[:train_size].copy()
    val_df = df.iloc[train_size:train_size + val_size].copy()
    test_df = df.iloc[train_size + val_size:].copy()
    
    print(f"Train: {len(train_df)} samples")
    print(f"Val: {len(val_df)} samples")
    print(f"Test: {len(test_df)} samples")
    
    feature_cols = [c for c in FEATURE_COLUMNS if c in df.columns]
    
    X_train = train_df[feature_cols]
    y_train = train_df["target"]
    
    X_val = val_df[feature_cols]
    y_val = val_df["target"]
    
    X_test = test_df[feature_cols]
    y_test = test_df["target"]
    
    # Step 4: Hyperparameter optimization (optional)
    best_params = DEFAULT_PARAMS.copy()
    if config.optimize_hyperparams:
        print("\n[Step 4] Optimizing hyperparameters...")
        try:
            best_params, best_val_accuracy = hyperparameter_optimization(
                X_train, y_train, X_val, y_val,
                param_grid=PARAM_GRID,
                n_iter=15,  # Limit for speed
            )
            print(f"Best validation accuracy: {best_val_accuracy:.4f}")
            print(f"Best params: {best_params}")
        except Exception as e:
            print(f"Hyperparameter optimization failed: {e}")
            print("Using default parameters")
            best_params = DEFAULT_PARAMS.copy()
    
    # Step 5: Train final model
    print("\n[Step 5] Training final model...")
    
    try:
        # Combine train and val for final training
        X_train_full = pd.concat([X_train, X_val], ignore_index=True)
        y_train_full = pd.concat([y_train, y_val], ignore_index=True)
        
        final_model = train_lightgbm(
            X_train_full, y_train_full,
            X_test, y_test,
            params=best_params,
            early_stopping_rounds=config.early_stopping_rounds,
        )
    except Exception as e:
        print(f"Final model training failed: {e}")
        import traceback
        traceback.print_exc()
        return 1
    
    # Step 6: Evaluate
    print("\n[Step 6] Evaluating model...")
    
    train_metrics = evaluate_model(final_model, X_train_full, y_train_full, "train")
    test_metrics = evaluate_model(final_model, X_test, y_test, "test")
    
    # Permutation importance
    perm_importance = permutation_feature_importance(final_model, X_test, y_test, n_repeats=10)
    
    # Step 7: Save artifacts
    print("\n[Step 7] Saving artifacts...")
    
    config.output_dir.mkdir(parents=True, exist_ok=True)
    
    # Save model
    model_path = config.output_dir / "lightgbm_volatility_regime_model.pkl"
    joblib.dump(final_model, model_path)
    print(f"Model saved to {model_path}")
    
    # Save metrics
    metrics = {
        "timestamp": datetime.now(UTC).isoformat(),
        "config": {k: str(v) if isinstance(v, Path) else v for k, v in asdict(config).items()},
        "best_params": best_params,
        "feature_columns": feature_cols,
        "class_names": CLASS_NAMES,
        "target_cutoffs": cutoffs,
        "data_stats": {
            "total_samples": int(len(df)),
            "train_samples": int(len(X_train_full)),
            "test_samples": int(len(X_test)),
            "features": len(feature_cols),
        },
        "metrics": {
            "train": train_metrics,
            "test": test_metrics,
        },
        "feature_importances": {
            "gain": {str(k): float(v) for k, v in zip(feature_cols, final_model.feature_importances_)},
            "split": {str(k): float(v) for k, v in zip(feature_cols, final_model.booster_.feature_importance(importance_type="split"))},
        },
        "permutation_importance": perm_importance.to_dict(orient="records"),
        "macro_calendar": macro_metadata,
    }
    
    metrics_path = config.output_dir / "lightgbm_training_metrics.json"
    with open(metrics_path, "w") as f:
        json.dump(metrics, f, indent=2)
    print(f"Metrics saved to {metrics_path}")
    
    # Print summary
    print("\n" + "=" * 80)
    print("TRAINING COMPLETE")
    print("=" * 80)
    print(f"Test Accuracy: {test_metrics['accuracy_pct']}%")
    print(f"Test F1 Macro: {test_metrics['f1_macro']:.4f}")
    print(f"Model saved to: {model_path}")
    print(f"Metrics saved to: {metrics_path}")
    
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
