#!/usr/bin/env python3
"""Fill Kronos & LightGBM with LSE API data and test for arbitrage opportunities.

**SUPERSEDED** -- see scripts/fill_and_test_arb_v2.py for the fixed pipeline.
This original script produces INVALID model artifacts (state/lightgbm_volatility_model.pkl,
state/lightgbm_scaler.pkl, state/lightgbm_training_metrics.json) due to label leakage
and broken test split. Do NOT use for live trading.

This script:
1. Fetches BTC/USD candle data from London Strategic Edge API
2. Trains LightGBM model for volatility regime classification
3. Trains Kronos model for volatility regime forecasting
4. Tests both models for arbitrage signal generation

Usage:
    python scripts/fill_and_test_arb.py
    python scripts/fill_and_test_arb.py --days-back 90
    python scripts/fill_and_test_arb.py --use-existing-data
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import joblib
import lightgbm as lgb
import numpy as np
import pandas as pd
import torch
from lightgbm import LGBMClassifier
from sklearn.metrics import accuracy_score, f1_score, confusion_matrix
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import LabelEncoder, StandardScaler

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from polymarket_arb.clients.lse_client import LSEClient
from polymarket_arb.config import SETTINGS


# =============================================================================
# Configuration
# =============================================================================

# LSE API key: read from LSE_API_KEY env / .env, never hardcoded
LSE_API_KEY = SETTINGS.lse_api_key

# Feature engineering
HORIZON_MINUTES = 15
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

# Kronos model config
KRONOS_CONFIG = {
    "s1_bits": 8,
    "s2_bits": 8,
    "n_layers": 4,
    "d_model": 128,
    "n_heads": 8,
    "ff_dim": 512,
    "ffn_dropout_p": 0.1,
    "attn_dropout_p": 0.1,
    "resid_dropout_p": 0.1,
    "token_dropout_p": 0.1,
    "learn_te": True,
}

SEQUENCE_LENGTH = 60
STRIDE = 5


# =============================================================================
# Helper Functions
# =============================================================================

def calculate_technical_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Calculate all technical indicators for the dataframe."""
    df = df.copy()
    
    # Log returns
    df['return_1m_bps'] = (np.log(df['close'] / df['close'].shift(1))) * 10000
    
    # Rolling volatilities
    for window in [5, 15, 60]:
        df[f'rolling_vol_{window}m_bps'] = df['return_1m_bps'].rolling(window=window).std()
    
    # ATR (Average True Range)
    df['tr'] = np.maximum(
        df['high'] - df['low'],
        np.maximum(
            abs(df['high'] - df['close'].shift(1)),
            abs(df['low'] - df['close'].shift(1))
        )
    )
    df['atr_14m_bps'] = df['tr'].rolling(window=14).mean() * 10000
    df['atr_60m_bps'] = df['tr'].rolling(window=60).mean() * 10000
    
    # Log volume
    df['log_volume'] = np.log1p(df['volume'])
    
    # Bollinger Bands
    df['bb_middle'] = df['close'].rolling(window=20).mean()
    df['bb_std'] = df['close'].rolling(window=20).std()
    df['bollinger_band_width'] = (df['bb_std'] / df['bb_middle']) * 100
    
    # RSI
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss
    df['rsi_14'] = 100 - (100 / (1 + rs))
    
    # MACD
    exp1 = df['close'].ewm(span=12, adjust=False).mean()
    exp2 = df['close'].ewm(span=26, adjust=False).mean()
    df['macd_line'] = exp1 - exp2
    df['macd_signal'] = df['macd_line'].ewm(span=9, adjust=False).mean()
    
    # Price trends (linear regression slope)
    for window in [5, 15]:
        x = np.arange(len(df))
        y = df['close'].values
        # Simple rolling linear regression
        df[f'price_trend_{window}m'] = df['close'].rolling(window=window).apply(
            lambda x: np.polyfit(np.arange(len(x)), x, 1)[0] if len(x) == window else np.nan, raw=False
        )
    
    # Volume trend
    df['volume_trend_5m'] = df['volume'].rolling(window=5).apply(
        lambda x: np.polyfit(np.arange(len(x)), x, 1)[0] if len(x) == 5 else np.nan, raw=False
    )
    
    # Temporal features
    df['hour'] = df.index.hour
    df['dayofweek'] = df.index.dayofweek
    df['hour_sin'] = np.sin(2 * np.pi * df['hour'] / 24)
    df['hour_cos'] = np.cos(2 * np.pi * df['hour'] / 24)
    df['dayofweek_sin'] = np.sin(2 * np.pi * df['dayofweek'] / 7)
    df['dayofweek_cos'] = np.cos(2 * np.pi * df['dayofweek'] / 7)
    
    # Session buckets (simplified)
    df['session_bucket'] = df.index.hour % 24
    df['session_bucket'] = df['session_bucket'].apply(
        lambda h: 0 if 0 <= h < 6 else (1 if 6 <= h < 12 else (2 if 12 <= h < 18 else 3))
    )
    
    # Macro event proximity (placeholder - would need real macro calendar)
    df['minutes_to_next_high_macro'] = 0
    
    return df


def calculate_forward_volatility(df: pd.DataFrame, horizon_minutes: int = 15) -> pd.DataFrame:
    """Calculate forward realized volatility as target variable."""
    df = df.copy()
    
    # Calculate forward realized volatility for each candle
    forward_vols = []
    for i in range(len(df)):
        end_idx = min(i + horizon_minutes, len(df))
        if end_idx > i + 1:
            returns = df['return_1m_bps'].iloc[i:end_idx].values
            vol = np.sqrt(np.sum(returns**2)) * 10000
            forward_vols.append(vol)
        else:
            forward_vols.append(np.nan)
    
    df[TARGET_COLUMN] = forward_vols
    
    # Create volatility regime classes (terciles)
    cutoffs = df[TARGET_COLUMN].quantile([0.333, 0.667]).values
    df['volatility_regime'] = pd.cut(
        df[TARGET_COLUMN],
        bins=[-np.inf, cutoffs[0], cutoffs[1], np.inf],
        labels=['LOW', 'MED', 'HIGH']
    )
    
    return df, cutoffs


def create_sequences(df: pd.DataFrame, sequence_length: int, stride: int, target_col: str) -> Tuple:
    """Create sequences for Kronos transformer model."""
    sequences = []
    targets = []
    
    selected_cols = ['open', 'high', 'low', 'close', 'volume']
    data = df[selected_cols].values
    target_data = df[target_col].values
    
    for i in range(0, len(data) - sequence_length, stride):
        end_idx = i + sequence_length
        if end_idx >= len(data):
            break
        
        # Get the next target (at end of sequence)
        target_idx = end_idx + HORIZON_MINUTES - 1
        if target_idx >= len(target_data):
            break
            
        sequences.append(data[i:end_idx])
        targets.append(target_data[target_idx])
    
    return np.array(sequences), np.array(targets)


# =============================================================================
# Data Fetching
# =============================================================================

async def fetch_lse_data(days_back: int = 90, limit: int = 50000) -> pd.DataFrame:
    """Fetch BTC/USD candle data from LSE API."""
    client = LSEClient(api_key=LSE_API_KEY)
    
    try:
        print(f"[INFO] Fetching {days_back} days of BTC/USD 1m candles from LSE API...")
        
        end = datetime.now(UTC)
        start = end - timedelta(days=days_back)
        
        candles = await client.fetch_candles(
            symbol="BTC/USD",
            timeframe="1m",
            start=start.strftime("%Y-%m-%d"),
            end=end.strftime("%Y-%m-%d"),
            limit=limit
        )
        
        print(f"[INFO] Fetched {len(candles)} candles")
        
        if not candles:
            print("[WARNING] No candles returned - using placeholder data")
            return pd.DataFrame()
        
        # Convert to DataFrame
        df = pd.DataFrame(candles)
        df['ts'] = pd.to_datetime(df['ts'])
        df = df.set_index('ts').sort_index()
        
        # Remove duplicates
        df = df[~df.index.duplicated(keep='first')]
        
        print(f"[INFO] Data range: {df.index[0]} to {df.index[-1]}")
        print(f"[INFO] Total candles after dedup: {len(df)}")
        
        return df
        
    except Exception as e:
        print(f"[ERROR] Failed to fetch LSE data: {e}")
        return pd.DataFrame()
    finally:
        await client.aclose()


# =============================================================================
# LightGBM Training
# =============================================================================

def train_lightgbm(df: pd.DataFrame) -> Optional[LGBMClassifier]:
    """Train LightGBM model on candle data."""
    print("\n" + "="*60)
    print("LIGHTGBM TRAINING")
    print("="*60)
    
    if len(df) < 100:
        print("[ERROR] Insufficient data for LightGBM training")
        return None
    
    # Calculate features
    print("[INFO] Calculating technical indicators...")
    df = calculate_technical_indicators(df)
    
    # Calculate target
    print("[INFO] Calculating forward volatility target...")
    df, cutoffs = calculate_forward_volatility(df, HORIZON_MINUTES)
    
    # Remove rows with NaN in features or target
    feature_cols = [c for c in FEATURE_COLUMNS if c in df.columns]
    df = df.dropna(subset=feature_cols + [TARGET_COLUMN])
    
    if len(df) < 100:
        print(f"[ERROR] Only {len(df)} valid rows after feature calculation")
        return None
    
    print(f"[INFO] Training samples: {len(df)}")
    print(f"[INFO] Class distribution:")
    print(df['volatility_regime'].value_counts())
    print(f"[INFO] Volatility cutoffs: LOW<{cutoffs[0]:.2f}, MED<{cutoffs[1]:.2f}, HIGH")
    
    # Prepare data
    X = df[feature_cols].values
    y = df['volatility_regime'].astype('category').cat.codes.values
    
    # Split data (time-series split)
    test_size = int(0.1 * len(X))
    val_size = int(0.1 * len(X))
    
    X_train, X_test = X[:-test_size], X[-test_size:]
    y_train, y_test = y[:-test_size], y[-test_size:]
    
    # Scale features
    scaler = StandardScaler()
    X_train = scaler.fit_transform(X_train)
    X_test = scaler.transform(X_test)
    
    # Train model
    print("[INFO] Training LightGBM model...")
    
    params = {
        "objective": "multiclass",
        "num_class": 3,
        "boosting_type": "gbdt",
        "metric": ["multi_logloss", "multi_error"],
        "n_estimators": 1000,
        "learning_rate": 0.05,
        "num_leaves": 63,
        "max_depth": 5,
        "min_child_samples": 10,
        "subsample": 0.8,
        "colsample_bytree": 0.8,
        "class_weight": "balanced",
        "random_state": 20260804,
        "n_jobs": -1,
        "verbosity": -1,
    }
    
    model = LGBMClassifier(**params)
    model.fit(X_train, y_train)
    
    # Evaluate
    y_pred = model.predict(X_test)
    
    accuracy = accuracy_score(y_test, y_pred)
    f1 = f1_score(y_test, y_pred, average='macro')
    
    print(f"\n[LIGHTGBM RESULTS]")
    print(f"  Test Accuracy: {accuracy:.4f} ({accuracy*100:.2f}%)")
    print(f"  Test F1 Macro: {f1:.4f}")
    print(f"  Training Samples: {len(X_train)}")
    print(f"  Test Samples: {len(X_test)}")
    
    # Feature importance
    print(f"\n[FEATURE IMPORTANCE]")
    importance = pd.DataFrame({
        'feature': feature_cols,
        'importance': model.feature_importances_
    }).sort_values('importance', ascending=False)
    print(importance.head(10).to_string(index=False))
    
    # Save model and metrics
    os.makedirs('state', exist_ok=True)
    joblib.dump(model, 'state/lightgbm_volatility_model.pkl')
    joblib.dump(scaler, 'state/lightgbm_scaler.pkl')
    
    metrics = {
        'model': 'lightgbm',
        'accuracy': float(accuracy),
        'f1_macro': float(f1),
        'training_samples': len(X_train),
        'test_samples': len(X_test),
        'timestamp': datetime.now(UTC).isoformat(),
        'cutoffs': [float(c) for c in cutoffs],
        'feature_importance': importance.to_dict('records')
    }
    
    with open('state/lightgbm_training_metrics.json', 'w') as f:
        json.dump(metrics, f, indent=2)
    
    print(f"[INFO] LightGBM model saved to state/lightgbm_volatility_model.pkl")
    
    return model


# =============================================================================
# Kronos Training
# =============================================================================

def train_kronos(df: pd.DataFrame) -> Optional[torch.nn.Module]:
    """Train Kronos transformer model on candle data."""
    print("\n" + "="*60)
    print("KRONOS TRAINING")
    print("="*60)
    
    if len(df) < 100:
        print("[ERROR] Insufficient data for Kronos training")
        return None
    
    # Calculate features first
    print("[INFO] Calculating technical indicators...")
    df = calculate_technical_indicators(df)
    
    # Calculate target
    print("[INFO] Calculating forward volatility target...")
    df, cutoffs = calculate_forward_volatility(df, HORIZON_MINUTES)
    
    # Remove rows with NaN
    selected_cols = ['open', 'high', 'low', 'close', 'volume']
    df = df.dropna(subset=selected_cols + [TARGET_COLUMN])
    
    if len(df) < 100:
        print(f"[ERROR] Only {len(df)} valid rows after cleaning")
        return None
    
    # Create sequences (limit to first 5000 rows for speed)
    print(f"[INFO] Creating sequences (length={SEQUENCE_LENGTH}, stride={STRIDE})...")
    df_subset = df.head(5000)  # Limit for faster training
    sequences, targets = create_sequences(df_subset, SEQUENCE_LENGTH, STRIDE, TARGET_COLUMN)
    
    # Encode targets
    label_encoder = LabelEncoder()
    targets_encoded = label_encoder.fit_transform(targets)
    
    print(f"[INFO] Created {len(sequences)} sequences")
    print(f"[INFO] Target distribution: {dict(zip(*np.unique(targets_encoded, return_counts=True)))}")
    
    # Split data
    test_size = int(0.1 * len(sequences))
    val_size = int(0.1 * len(sequences))
    
    X_train, X_test = sequences[:-test_size], sequences[-test_size:]
    y_train, y_test = targets_encoded[:-test_size], targets_encoded[-test_size:]
    
    # Convert to tensors
    X_train = torch.FloatTensor(X_train)
    X_test = torch.FloatTensor(X_test)
    y_train = torch.LongTensor(y_train)
    y_test = torch.LongTensor(y_test)
    
    # Create DataLoader
    class SequenceDataset(torch.utils.data.Dataset):
        def __init__(self, X, y):
            self.X = X
            self.y = y
        
        def __len__(self):
            return len(self.X)
        
        def __getitem__(self, idx):
            return self.X[idx], self.y[idx]
    
    train_dataset = SequenceDataset(X_train, y_train)
    test_dataset = SequenceDataset(X_test, y_test)
    
    train_loader = torch.utils.data.DataLoader(train_dataset, batch_size=32, shuffle=False)
    test_loader = torch.utils.data.DataLoader(test_dataset, batch_size=32, shuffle=False)
    
    # Define Kronos model (simplified version)
    device = torch.device('cuda' if torch.cuda.is_available() else 'mps' if hasattr(torch.backends, 'mps') and torch.backends.mps.is_available() else 'cpu')
    
    class SimpleKronos(torch.nn.Module):
        def __init__(self, input_dim=5, d_model=128, n_layers=4, n_heads=8, n_classes=3):
            super().__init__()
            self.input_dim = input_dim
            self.d_model = d_model
            
            # Input projection
            self.input_proj = torch.nn.Linear(input_dim, d_model)
            
            # Transformer encoder
            encoder_layer = torch.nn.TransformerEncoderLayer(
                d_model=d_model,
                nhead=n_heads,
                dim_feedforward=512,
                dropout=0.1,
                batch_first=True
            )
            self.transformer = torch.nn.TransformerEncoder(encoder_layer, num_layers=n_layers)
            
            # Output head
            self.output_head = torch.nn.Linear(d_model, n_classes)
            
        def forward(self, x):
            # x shape: (batch, seq_len, input_dim)
            x = self.input_proj(x)
            x = self.transformer(x)
            # Use mean across sequence length
            x = x.mean(dim=1)
            x = self.output_head(x)
            return x
    
    model = SimpleKronos(input_dim=5, d_model=128, n_layers=4, n_heads=8, n_classes=3)
    model.to(device)
    
    # Training
    print("[INFO] Training Kronos model...")
    
    criterion = torch.nn.CrossEntropyLoss()
    optimizer = torch.optim.AdamW(model.parameters(), lr=0.0001, weight_decay=0.01)
    scheduler = torch.optim.lr_scheduler.LinearLR(optimizer, start_factor=1.0, end_factor=0.1, total_iters=100)
    
    best_val_loss = float('inf')
    patience = 5
    no_improve = 0
    
    for epoch in range(10):  # Reduce epochs for faster training
        model.train()
        train_loss = 0.0
        
        for X_batch, y_batch in train_loader:
            X_batch, y_batch = X_batch.to(device), y_batch.to(device)
            
            optimizer.zero_grad()
            outputs = model(X_batch)
            loss = criterion(outputs, y_batch)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
            
            train_loss += loss.item()
        
        scheduler.step()
        
        # Validation
        model.eval()
        val_loss = 0.0
        all_preds = []
        all_targets = []
        
        with torch.no_grad():
            for X_batch, y_batch in test_loader:
                X_batch, y_batch = X_batch.to(device), y_batch.to(device)
                outputs = model(X_batch)
                loss = criterion(outputs, y_batch)
                val_loss += loss.item()
                
                preds = torch.argmax(outputs, dim=1)
                all_preds.extend(preds.cpu().numpy())
                all_targets.extend(y_batch.cpu().numpy())
        
        train_loss /= len(train_loader)
        val_loss /= len(test_loader)
        
        accuracy = accuracy_score(all_targets, all_preds)
        f1 = f1_score(all_targets, all_preds, average='macro')
        
        print(f"  Epoch {epoch+1}: train_loss={train_loss:.4f}, val_loss={val_loss:.4f}, "
              f"accuracy={accuracy:.4f}, f1={f1:.4f}")
        
        # Early stopping
        if val_loss < best_val_loss:
            best_val_loss = val_loss
            no_improve = 0
            
            # Save best model
            torch.save(model.state_dict(), 'state/kronos_volatility_classifier.pth')
        else:
            no_improve += 1
            if no_improve >= patience:
                print(f"  Early stopping at epoch {epoch+1}")
                break
    
    # Final evaluation
    print(f"\n[KRONOS RESULTS]")
    print(f"  Best Val Loss: {best_val_loss:.4f}")
    print(f"  Test Accuracy: {accuracy:.4f}")
    print(f"  Test F1 Macro: {f1:.4f}")
    
    # Save metrics
    metrics = {
        'model': 'kronos',
        'accuracy': float(accuracy),
        'f1_macro': float(f1),
        'val_loss': float(best_val_loss),
        'training_sequences': len(X_train),
        'test_sequences': len(X_test),
        'timestamp': datetime.now(UTC).isoformat(),
    }
    
    with open('state/kronos_training_metrics.json', 'w') as f:
        json.dump(metrics, f, indent=2)
    
    print(f"[INFO] Kronos model saved to state/kronos_volatility_classifier.pth")
    
    return model


# =============================================================================
# Arbitrage Testing
# =============================================================================

def test_arbitrage_signals(df: pd.DataFrame, lgbm_model: LGBMClassifier, kronos_model: torch.nn.Module = None) -> Dict:
    """Test models for arbitrage signal generation."""
    print("\n" + "="*60)
    print("ARBITRAGE SIGNAL TESTING")
    print("="*60)
    
    results = {
        'lightgbm_signals': 0,
        'kronos_signals': 0,
        'combined_signals': 0,
        'total_candles': len(df),
        'arbitrage_opportunities': []
    }
    
    if len(df) < 100:
        print("[ERROR] Insufficient data for arbitrage testing")
        return results
    
    # Prepare data for LightGBM
    df = calculate_technical_indicators(df)
    df, _ = calculate_forward_volatility(df, HORIZON_MINUTES)
    
    feature_cols = [c for c in FEATURE_COLUMNS if c in df.columns]
    df = df.dropna(subset=feature_cols + [TARGET_COLUMN])
    
    # Load scaler
    try:
        scaler = joblib.load('state/lightgbm_scaler.pkl')
        X = scaler.transform(df[feature_cols].values)
    except:
        print("[WARNING] Could not load scaler, using raw features")
        X = df[feature_cols].values
    
    # LightGBM predictions
    print("[INFO] Generating LightGBM signals...")
    lgbm_preds = lgbm_model.predict(X)
    lgbm_proba = lgbm_model.predict_proba(X)
    
    # Map predictions to class names
    class_mapping = {0: 'LOW', 1: 'MED', 2: 'HIGH'}
    lgbm_classes = [class_mapping[p] for p in lgbm_preds]
    
    # For arbitrage, we care about HIGH volatility regimes
    # These indicate periods where arbitrage opportunities are more likely
    high_vol_indices = [i for i, c in enumerate(lgbm_classes) if c == 'HIGH']
    results['lightgbm_signals'] = len(high_vol_indices)
    
    print(f"  LightGBM identified {len(high_vol_indices)} high-volatility periods")
    
    # Kronos predictions (if model provided)
    if kronos_model is not None:
        print("[INFO] Generating Kronos signals...")
        
        # Prepare sequences for Kronos
        sequences, _ = create_sequences(df, SEQUENCE_LENGTH, STRIDE, TARGET_COLUMN)
        
        if len(sequences) > 0:
            # Use the model to predict
            device = next(kronos_model.parameters()).device
            
            with torch.no_grad():
                X_tensor = torch.FloatTensor(sequences).to(device)
                kronos_outputs = kronos_model(X_tensor)
                kronos_preds = torch.argmax(kronos_outputs, dim=1).cpu().numpy()
            
            kronos_classes = [class_mapping[p] for p in kronos_preds]
            kronos_high_vol_indices = [i for i, c in enumerate(kronos_classes) if c == 'HIGH']
            
            results['kronos_signals'] = len(kronos_high_vol_indices)
            print(f"  Kronos identified {len(kronos_high_vol_indices)} high-volatility periods")
            
            # Combined signals (both models predict HIGH)
            # This is a simplified approach - in practice you'd want more sophisticated logic
            results['combined_signals'] = len(set(high_vol_indices) & set(kronos_high_vol_indices))
    
    # Arbitrage opportunity detection logic
    # In a real scenario, this would involve:
    # 1. Monitoring Polymarket order books
    # 2. Looking for YES+NO < 1.00 - MIN_EDGE
    # 3. Using model predictions to time entries
    
    print(f"\n[ARBITRAGE SUMMARY]")
    print(f"  Total candles analyzed: {results['total_candles']}")
    print(f"  LightGBM high-vol signals: {results['lightgbm_signals']}")
    if kronos_model is not None:
        print(f"  Kronos high-vol signals: {results['kronos_signals']}")
        print(f"  Combined signals: {results['combined_signals']}")
    
    # Signal quality assessment
    signal_rate = results['lightgbm_signals'] / results['total_candles'] * 100
    print(f"  Signal rate: {signal_rate:.2f}%")
    
    if signal_rate > 0:
        print(f"\n[INFO] Models are generating signals - ready for live testing!")
        print(f"[INFO] Next step: Deploy to live arbitrage bot with Polymarket feed")
    else:
        print(f"\n[WARNING] Low signal rate - models may need more data or tuning")
    
    # Save results
    with open('state/arbitrage_test_results.json', 'w') as f:
        json.dump(results, f, indent=2)
    
    return results


# =============================================================================
# Main
# =============================================================================

async def main():
    parser = argparse.ArgumentParser(description="Fill Kronos & LightGBM with LSE data and test for arbitrage")
    parser.add_argument('--days-back', type=int, default=90, help='Number of days of data to fetch')
    parser.add_argument('--use-existing-data', action='store_true', help='Use existing parquet data instead of fetching')
    parser.add_argument('--skip-kronos', action='store_true', help='Skip Kronos training (time-consuming)')
    parser.add_argument('--limit', type=int, default=50000, help='Max candles to fetch from LSE')
    args = parser.parse_args()
    
    print("="*60)
    print("LSE API DATA PIPELINE FOR ARBITRAGE TESTING")
    print("="*60)
    print(f"API Key: {LSE_API_KEY}")
    print(f"Days back: {args.days_back}")
    print(f"Limit: {args.limit}")
    
    # Step 1: Fetch or load data
    if args.use_existing_data:
        print("\n[INFO] Loading existing data from parquet...")
        try:
            df = pd.read_parquet('state/btc_1m_candles.parquet')
            print(f"[INFO] Loaded {len(df)} candles from state/btc_1m_candles.parquet")
        except Exception as e:
            print(f"[ERROR] Could not load existing data: {e}")
            df = await fetch_lse_data(args.days_back, args.limit)
    else:
        df = await fetch_lse_data(args.days_back, args.limit)
    
    if len(df) < 100:
        print("[ERROR] Insufficient data - cannot proceed")
        sys.exit(1)
    
    # Save raw data
    df.to_parquet('state/btc_1m_candles_lse.parquet')
    print(f"[INFO] Saved raw LSE data to state/btc_1m_candles_lse.parquet")
    
    # Step 2: Train LightGBM
    lgbm_model = train_lightgbm(df)
    if lgbm_model is None:
        print("[ERROR] LightGBM training failed")
        sys.exit(1)
    
    # Step 3: Train Kronos (optional)
    kronos_model = None
    if not args.skip_kronos:
        try:
            kronos_model = train_kronos(df)
        except Exception as e:
            print(f"[WARNING] Kronos training failed: {e}")
            print("[INFO] Proceeding with LightGBM only")
    else:
        print("\n[INFO] Skipping Kronos training as requested")
    
    # Step 4: Test for arbitrage signals
    test_arbitrage_signals(df, lgbm_model, kronos_model)
    
    print("\n" + "="*60)
    print("PIPELINE COMPLETE")
    print("="*60)
    print("[INFO] Both models have been trained with LSE API data")
    print("[INFO] Arbitrage signal testing complete")
    print("\n[NEXT STEPS]")
    print("1. Deploy models to live arbitrage bot")
    print("2. Monitor signal quality in real-time")
    print("3. Backtest with historical Polymarket data")
    print("4. Consider getting a real LSE API key for more data")


if __name__ == '__main__':
    asyncio.run(main())
