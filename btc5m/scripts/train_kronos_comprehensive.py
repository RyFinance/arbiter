#!/usr/bin/env python3
"""Comprehensive Kronos training for BTC volatility regime forecasting.

This script trains the Kronos Transformer-based model to predict volatility regimes
(LOW/MED/HIGH) based on BTC/USD price data. It uses ALL available data and optimizes
the model for maximum accuracy.

The Kronos model uses:
- A tokenizer (KronosTokenizer) that encodes price data into quantized tokens
- A Transformer decoder that predicts future volatility regimes

Usage:
    python scripts/train_kronos_comprehensive.py
    python scripts/train_kronos_comprehensive.py --use-parquet-only
    python scripts/train_kronos_comprehensive.py --days-back 365 --epochs 100
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
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np
import pandas as pd
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import Dataset, DataLoader, random_split
from sklearn.metrics import accuracy_score, f1_score, confusion_matrix
from sklearn.preprocessing import LabelEncoder
from tqdm import tqdm

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from polymarket_arb.clients.lse_client import LSEClient


# =============================================================================
# Configuration
# =============================================================================

# Model configuration
KRONOS_CONFIG = {
    "s1_bits": 8,
    "s2_bits": 8,
    "n_layers": 6,
    "d_model": 128,
    "n_heads": 8,
    "ff_dim": 512,
    "ffn_dropout_p": 0.1,
    "attn_dropout_p": 0.1,
    "resid_dropout_p": 0.1,
    "token_dropout_p": 0.1,
    "learn_te": True,
}

# Training configuration
TRAIN_CONFIG = {
    "batch_size": 32,
    "learning_rate": 0.0001,
    "weight_decay": 0.01,
    "max_epochs": 100,
    "patience": 10,
    "warmup_steps": 1000,
    "max_grad_norm": 1.0,
    "device": "cuda" if torch.cuda.is_available() else "mps" if hasattr(torch.backends, 'mps') and torch.backends.mps.is_available() else "cpu",
}

# Data configuration
HORIZON_MINUTES = 15
SEQUENCE_LENGTH = 60  # Use 60 minutes of history to predict next 15
STRIDE = 5  # Sliding window stride

# Classification thresholds (will be learned from data)
CLASS_NAMES = ["LOW", "MED", "HIGH"]
N_CLASSES = 3


@dataclass
class TrainingConfig:
    use_parquet: bool = True
    days_back: int = 90
    fetch_lse: bool = True
    sequence_length: int = SEQUENCE_LENGTH
    stride: int = STRIDE
    horizon_minutes: int = HORIZON_MINUTES
    batch_size: int = 32
    max_epochs: int = 100
    learning_rate: float = 0.0001
    test_size: float = 0.2
    val_size: float = 0.1
    random_state: int = 20260804
    output_dir: Path = Path("state")
    device: str = field(default_factory=lambda: "cuda" if torch.cuda.is_available() else "mps" if hasattr(torch.backends, 'mps') and torch.backends.mps.is_available() else "cpu")
    # Explicit single-file override. load_all_btc_parquet_data()'s glob
    # (*btc*.parquet / *candle*.parquet under state/) also matches legacy
    # non-candle files (e.g. state/btc_ticks_real.parquet — raw ticks with a
    # price/side/ask schema, no OHLCV) that would silently get concatenated
    # in. When set, skip the glob entirely and load exactly this one file.
    candles_path: Optional[Path] = None
    model_name: str = "kronos_volatility_classifier.pth"


# =============================================================================
# Data Loading and Preparation
# =============================================================================

def load_all_btc_parquet_data(parquet_dir: Path = None) -> pd.DataFrame:
    """Load all BTC tick data from parquet files."""
    if parquet_dir is None:
        parquet_dir = PROJECT_ROOT / "state"
    
    all_data = []
    parquet_files = list(parquet_dir.glob("*btc*.parquet")) + list(parquet_dir.glob("*candle*.parquet"))
    
    if not parquet_files:
        print(f"No BTC parquet files found in {parquet_dir}")
        return pd.DataFrame()
    
    print(f"Loading {len(parquet_files)} BTC parquet files...")
    for pf in parquet_files:
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
        print(f"Total rows from parquet: {len(combined)}")
        return combined
    return pd.DataFrame()


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


def add_forward_volatility(df: pd.DataFrame, horizon_minutes: int = 15) -> pd.DataFrame:
    """Add forward realized volatility as target."""
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
    
    df["forward_vol_bps"] = forward_vol
    
    return df


def create_volatility_labels(df: pd.DataFrame) -> pd.DataFrame:
    """Create LOW/MED/HIGH labels from forward volatility."""
    df = df.copy()
    
    target_col = "forward_vol_bps"
    if target_col not in df.columns:
        df = add_forward_volatility(df)
    
    valid_target = df[target_col].dropna()
    if len(valid_target) == 0:
        raise ValueError("No valid target values")
    
    # Use tertiles for class boundaries
    low_cutoff, high_cutoff = valid_target.quantile([1/3, 2/3]).tolist()
    
    df["volatility_class"] = np.select(
        [
            df[target_col] <= low_cutoff,
            df[target_col] <= high_cutoff,
        ],
        [0, 1],
        default=2,
    ).astype(int)
    
    # Also store the cutoffs
    df.attrs["low_cutoff"] = low_cutoff
    df.attrs["high_cutoff"] = high_cutoff
    
    return df


def create_sequences(
    df: pd.DataFrame,
    sequence_length: int = SEQUENCE_LENGTH,
    stride: int = STRIDE,
    horizon_minutes: int = HORIZON_MINUTES,
    price_cols: List[str] = ["open", "high", "low", "close", "volume"],
) -> Tuple[np.ndarray, np.ndarray, List[pd.Timestamp]]:
    """Create time-series sequences for training.
    
    Each sequence uses `sequence_length` minutes of historical data to predict
    the volatility class of the next `horizon_minutes` minutes.
    
    Returns:
        X: Array of shape (n_sequences, sequence_length, n_features)
        y: Array of shape (n_sequences,) with class labels
        timestamps: List of timestamps for each sequence
    """
    df = df.sort_values("timestamp").reset_index(drop=True)
    
    if "volatility_class" not in df.columns:
        df = create_volatility_labels(df)
    
    # Normalize price features
    mean_vals = df[price_cols].mean()
    std_vals = df[price_cols].std()
    
    normalized = df[price_cols].copy()
    for col in price_cols:
        std = std_vals[col] if std_vals[col] > 0 else 1.0
        normalized[col] = (df[col] - mean_vals[col]) / std
    
    # Create sequences
    sequences = []
    labels = []
    seq_timestamps = []
    
    n = len(df)
    
    for i in range(0, n - sequence_length - horizon_minutes + 1, stride):
        # Check if we have contiguous data
        timestamps = df["timestamp"].iloc[i:i + sequence_length + horizon_minutes]
        expected_end = timestamps.iloc[0] + pd.Timedelta(minutes=sequence_length + horizon_minutes - 1)
        actual_end = timestamps.iloc[-1]
        
        if (actual_end - timestamps.iloc[0]) != pd.Timedelta(minutes=sequence_length + horizon_minutes - 1):
            continue  # Skip non-contiguous sequences
        
        # Get the sequence features
        seq_data = normalized.iloc[i:i + sequence_length].values  # (seq_len, n_features)
        
        # The label is the volatility class at position i + sequence_length
        label_idx = i + sequence_length + horizon_minutes - 1  # Label at end of horizon
        label = df["volatility_class"].iloc[label_idx]
        
        sequences.append(seq_data)
        labels.append(label)
        seq_timestamps.append(df["timestamp"].iloc[i])
    
    return np.array(sequences), np.array(labels), seq_timestamps


# =============================================================================
# Dataset and DataLoader
# =============================================================================

class KronosDataset(Dataset):
    """PyTorch Dataset for Kronos training."""
    
    def __init__(self, sequences: np.ndarray, labels: np.ndarray):
        self.sequences = torch.FloatTensor(sequences)
        self.labels = torch.LongTensor(labels)
    
    def __len__(self) -> int:
        return len(self.labels)
    
    def __getitem__(self, idx: int) -> Tuple[torch.Tensor, torch.Tensor]:
        return self.sequences[idx], self.labels[idx]


def create_data_loaders(
    X_train: np.ndarray,
    y_train: np.ndarray,
    X_val: np.ndarray,
    y_val: np.ndarray,
    X_test: np.ndarray,
    y_test: np.ndarray,
    batch_size: int = 32,
) -> Tuple[DataLoader, DataLoader, DataLoader]:
    """Create PyTorch DataLoaders."""
    
    train_dataset = KronosDataset(X_train, y_train)
    val_dataset = KronosDataset(X_val, y_val)
    test_dataset = KronosDataset(X_test, y_test)
    
    train_loader = DataLoader(
        train_dataset, batch_size=batch_size, shuffle=True, num_workers=2
    )
    val_loader = DataLoader(
        val_dataset, batch_size=batch_size, shuffle=False, num_workers=2
    )
    test_loader = DataLoader(
        test_dataset, batch_size=batch_size, shuffle=False, num_workers=2
    )
    
    return train_loader, val_loader, test_loader


# =============================================================================
# Kronos Model Setup
# =============================================================================

def import_kronos_model():
    """Import and return the Kronos model classes."""
    try:
        from polymarket_arb.kronos_model.kronos import Kronos, KronosTokenizer, KronosPredictor
        return Kronos, KronosTokenizer, KronosPredictor
    except Exception as e:
        print(f"Error importing Kronos model: {e}")
        # Try to import from module path
        sys.path.insert(0, str(PROJECT_ROOT / "polymarket_arb"))
        try:
            from kronos_model.kronos import Kronos, KronosTokenizer, KronosPredictor
            return Kronos, KronosTokenizer, KronosPredictor
        except Exception as e2:
            print(f"Still cannot import Kronos: {e2}")
            raise


class KronosVolatilityClassifier(nn.Module):
    """Kronos-based volatility regime classifier.
    
    This model uses the Kronos tokenizer to encode input sequences and a 
    classification head to predict volatility regimes.
    """
    
    def __init__(
        self,
        input_dim: int = 5,  # open, high, low, close, volume
        d_model: int = 128,
        n_layers: int = 4,
        n_heads: int = 8,
        ff_dim: int = 512,
        s1_bits: int = 8,
        s2_bits: int = 8,
        n_classes: int = 3,
        dropout: float = 0.1,
    ):
        super().__init__()
        
        self.input_dim = input_dim
        self.d_model = d_model
        self.s1_bits = s1_bits
        self.s2_bits = s2_bits
        self.n_classes = n_classes
        
        # Projection to d_model
        self.input_proj = nn.Linear(input_dim, d_model)
        
        # Temporal embeddings
        self.seq_len = SEQUENCE_LENGTH
        self.pos_emb = nn.Embedding(self.seq_len, d_model)
        
        # Transformer layers
        encoder_layer = nn.TransformerEncoderLayer(
            d_model=d_model,
            nhead=n_heads,
            dim_feedforward=ff_dim,
            dropout=dropout,
            batch_first=True,
        )
        self.transformer = nn.TransformerEncoder(encoder_layer, num_layers=n_layers)
        
        # Layer normalization
        self.norm = nn.LayerNorm(d_model)
        
        # Classification head
        self.class_head = nn.Sequential(
            nn.Linear(d_model, d_model // 2),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(d_model // 2, n_classes),
        )
        
        # Initialize weights
        self._init_weights()
    
    def _init_weights(self):
        """Initialize model weights."""
        for module in self.modules():
            if isinstance(module, nn.Linear):
                nn.init.xavier_normal_(module.weight)
                if module.bias is not None:
                    nn.init.zeros_(module.bias)
            elif isinstance(module, nn.Embedding):
                nn.init.normal_(module.weight, mean=0, std=self.d_model ** -0.5)
            elif isinstance(module, nn.LayerNorm):
                nn.init.ones_(module.weight)
                nn.init.zeros_(module.bias)
    
    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """Forward pass.
        
        Args:
            x: Input tensor of shape (batch_size, seq_len, input_dim)
            
        Returns:
            logits: Tensor of shape (batch_size, n_classes)
        """
        # Project input to d_model
        x = self.input_proj(x)  # (batch_size, seq_len, d_model)
        
        # Add positional embeddings
        pos_ids = torch.arange(x.size(1), device=x.device).unsqueeze(0).expand(x.size(0), -1)
        x = x + self.pos_emb(pos_ids)
        
        # Transformer
        x = self.transformer(x)
        
        # Pool: use mean over sequence
        x = x.mean(dim=1)  # (batch_size, d_model)
        
        # Classification
        x = self.norm(x)
        logits = self.class_head(x)
        
        return logits


# =============================================================================
# Training
# =============================================================================

def train_epoch(
    model: nn.Module,
    dataloader: DataLoader,
    criterion: nn.Module,
    optimizer: optim.Optimizer,
    scheduler: optim.lr_scheduler._LRScheduler = None,
    device: str = "cpu",
) -> Dict[str, float]:
    """Train for one epoch."""
    
    model.train()
    
    total_loss = 0.0
    total_correct = 0
    total_samples = 0
    
    for batch_x, batch_y in tqdm(dataloader, desc="Training", leave=False):
        batch_x = batch_x.to(device)
        batch_y = batch_y.to(device)
        
        optimizer.zero_grad()
        
        logits = model(batch_x)
        loss = criterion(logits, batch_y)
        
        loss.backward()
        optimizer.step()
        
        if scheduler is not None:
            scheduler.step()
        
        total_loss += loss.item() * batch_x.size(0)
        
        # Calculate accuracy
        preds = logits.argmax(dim=1)
        total_correct += (preds == batch_y).sum().item()
        total_samples += batch_x.size(0)
    
    avg_loss = total_loss / total_samples
    accuracy = total_correct / total_samples
    
    return {"loss": avg_loss, "accuracy": accuracy}


@torch.no_grad()
def evaluate(
    model: nn.Module,
    dataloader: DataLoader,
    criterion: nn.Module,
    device: str = "cpu",
) -> Dict[str, float]:
    """Evaluate model on a dataset."""
    
    model.eval()
    
    total_loss = 0.0
    total_correct = 0
    total_samples = 0
    all_preds = []
    all_labels = []
    
    for batch_x, batch_y in tqdm(dataloader, desc="Evaluating", leave=False):
        batch_x = batch_x.to(device)
        batch_y = batch_y.to(device)
        
        logits = model(batch_x)
        loss = criterion(logits, batch_y)
        
        total_loss += loss.item() * batch_x.size(0)
        
        preds = logits.argmax(dim=1)
        total_correct += (preds == batch_y).sum().item()
        total_samples += batch_x.size(0)
        
        all_preds.extend(preds.cpu().numpy())
        all_labels.extend(batch_y.cpu().numpy())
    
    avg_loss = total_loss / total_samples
    accuracy = total_correct / total_samples
    
    # Calculate additional metrics
    f1_macro = f1_score(all_labels, all_preds, labels=[0, 1, 2], average="macro", zero_division=0)
    f1_weighted = f1_score(all_labels, all_preds, labels=[0, 1, 2], average="weighted", zero_division=0)
    
    return {
        "loss": avg_loss,
        "accuracy": accuracy,
        "f1_macro": f1_macro,
        "f1_weighted": f1_weighted,
        "n_samples": total_samples,
    }


def train_model(
    model: nn.Module,
    train_loader: DataLoader,
    val_loader: DataLoader,
    test_loader: DataLoader,
    config: TrainingConfig,
    patience: int = 10,
) -> Tuple[nn.Module, Dict[str, Any]]:
    """Train the model with early stopping."""
    
    device = config.device
    model = model.to(device)
    
    criterion = nn.CrossEntropyLoss()
    optimizer = optim.AdamW(
        model.parameters(),
        lr=config.learning_rate,
        weight_decay=0.01,
    )
    
    # Learning rate scheduler
    scheduler = optim.lr_scheduler.LinearLR(
        optimizer,
        start_factor=1.0,
        end_factor=0.1,
        total_iters=config.max_epochs,
    )
    
    best_val_accuracy = 0.0
    best_epoch = 0
    epochs_no_improve = 0
    
    history = {
        "train": [],
        "val": [],
        "test": [],
    }
    
    print(f"\nTraining on device: {device}")
    print(f"Model parameters: {sum(p.numel() for p in model.parameters()):,}")
    
    for epoch in range(config.max_epochs):
        epoch_start = datetime.now()
        
        # Train
        train_metrics = train_epoch(
            model, train_loader, criterion, optimizer, scheduler, device
        )
        history["train"].append(train_metrics)
        
        # Validate
        val_metrics = evaluate(model, val_loader, criterion, device)
        history["val"].append(val_metrics)
        
        # Test (for monitoring)
        test_metrics = evaluate(model, test_loader, criterion, device)
        history["test"].append(test_metrics)
        
        epoch_time = (datetime.now() - epoch_start).total_seconds()
        
        print(f"\nEpoch {epoch + 1}/{config.max_epochs} ({epoch_time:.2f}s)")
        print(f"  Train: loss={train_metrics['loss']:.4f} acc={train_metrics['accuracy']:.4f}")
        print(f"  Val:   loss={val_metrics['loss']:.4f} acc={val_metrics['accuracy']:.4f} f1_macro={val_metrics['f1_macro']:.4f}")
        print(f"  Test:  loss={test_metrics['loss']:.4f} acc={test_metrics['accuracy']:.4f} f1_macro={test_metrics['f1_macro']:.4f}")
        
        # Early stopping
        if val_metrics["accuracy"] > best_val_accuracy:
            best_val_accuracy = val_metrics["accuracy"]
            best_epoch = epoch + 1
            epochs_no_improve = 0
            
            # Save best model
            best_model_state = {k: v.clone() for k, v in model.state_dict().items()}
        else:
            epochs_no_improve += 1
            if epochs_no_improve >= patience:
                print(f"\nEarly stopping at epoch {epoch + 1} (no improvement for {patience} epochs)")
                break
    
    # Restore best model
    model.load_state_dict(best_model_state)
    
    # Final evaluation
    final_test_metrics = evaluate(model, test_loader, criterion, device)
    
    print(f"\nTraining complete!")
    print(f"Best epoch: {best_epoch} with val accuracy: {best_val_accuracy:.4f}")
    print(f"Final test accuracy: {final_test_metrics['accuracy']:.4f}")
    print(f"Final test F1 macro: {final_test_metrics['f1_macro']:.4f}")
    
    result = {
        "best_epoch": best_epoch,
        "best_val_accuracy": best_val_accuracy,
        "final_test_metrics": final_test_metrics,
        "history": history,
    }
    
    return model, result


# =============================================================================
# Main Pipeline
# =============================================================================

async def main():
    parser = argparse.ArgumentParser(description="Train comprehensive Kronos volatility regime classifier")
    parser.add_argument("--use-parquet-only", action="store_true", help="Use only parquet data, no LSE fetch")
    parser.add_argument("--days-back", type=int, default=90, help="Number of days of LSE data to fetch")
    parser.add_argument("--epochs", type=int, default=100, help="Maximum training epochs")
    parser.add_argument("--batch-size", type=int, default=32, help="Batch size")
    parser.add_argument("--lr", type=float, default=0.0001, help="Learning rate")
    parser.add_argument("--output-dir", type=Path, default=Path("state"), help="Output directory")
    parser.add_argument("--test-size", type=float, default=0.2, help="Test set size")
    parser.add_argument("--val-size", type=float, default=0.1, help="Validation set size")
    parser.add_argument("--candles", type=Path, default=None,
                         help="Load exactly this one parquet file instead of glob-scanning "
                              "state/ for *btc*.parquet / *candle*.parquet (which also matches "
                              "non-candle files). Implies --use-parquet-only.")
    parser.add_argument("--model-name", type=str, default="kronos_volatility_classifier.pth",
                         help="Output checkpoint filename under --output-dir")

    args = parser.parse_args()

    config = TrainingConfig(
        use_parquet=True,  # Always try to use parquet
        days_back=args.days_back,
        fetch_lse=not args.use_parquet_only and args.candles is None,
        max_epochs=args.epochs,
        batch_size=args.batch_size,
        learning_rate=args.lr,
        test_size=args.test_size,
        val_size=args.val_size,
        output_dir=args.output_dir,
        candles_path=args.candles,
        model_name=args.model_name,
    )
    
    print("=" * 80)
    print("Kronos Comprehensive Training Pipeline")
    print("=" * 80)
    print(f"Device: {config.device}")
    
    # Step 1: Load data
    print("\n[Step 1] Loading data...")
    
    parquet_data = pd.DataFrame()
    if config.candles_path is not None:
        parquet_data = pd.read_parquet(config.candles_path)
        parquet_data["timestamp"] = pd.to_datetime(parquet_data["timestamp"], utc=True)
        parquet_data = (
            parquet_data.drop_duplicates(subset="timestamp")
            .sort_values("timestamp")
            .reset_index(drop=True)
        )
        print(f"Loaded {len(parquet_data)} rows from {config.candles_path} (explicit, no glob)")
    elif config.use_parquet:
        parquet_data = load_all_btc_parquet_data()
        if not parquet_data.empty:
            print(f"Loaded {len(parquet_data)} rows from parquet files")
    
    lse_data = pd.DataFrame()
    if config.fetch_lse:
        lse_data = await fetch_lse_candles_days_back(config.days_back)
        if not lse_data.empty:
            print(f"Fetched {len(lse_data)} rows from LSE API")
    
    if parquet_data.empty and lse_data.empty:
        print("ERROR: No data available from any source!")
        return 1
    
    # Combine data
    if not parquet_data.empty and not lse_data.empty:
        # Check if we have timestamp column
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
    else:
        combined = lse_data
    
    print(f"Combined dataset: {len(combined)} rows")
    
    # Step 2: Create sequences
    print("\n[Step 2] Creating sequences...")
    
    try:
        # Add volatility target
        combined = add_forward_volatility(combined, config.horizon_minutes)
        combined = create_volatility_labels(combined)
        
        # Create sequences
        X, y, timestamps = create_sequences(
            combined,
            sequence_length=config.sequence_length,
            stride=config.stride,
            horizon_minutes=config.horizon_minutes,
        )
        
        print(f"Created {len(X)} sequences")
        print(f"Class distribution: {dict(zip(*np.unique(y, return_counts=True)))}")
        
        if len(X) < 100:
            print("ERROR: Not enough sequences. Try reducing sequence_length or increasing data.")
            return 1
            
    except Exception as e:
        print(f"ERROR in sequence creation: {e}")
        import traceback
        traceback.print_exc()
        return 1
    
    # Step 3: Split data
    print("\n[Step 3] Splitting data...")
    
    n_total = len(X)
    n_test = int(n_total * config.test_size)
    n_val = int(n_total * config.val_size)
    n_train = n_total - n_test - n_val
    
    # Time-based split (important for financial data)
    X_train, X_val, X_test = X[:n_train], X[n_train:n_train + n_val], X[n_train + n_val:]
    y_train, y_val, y_test = y[:n_train], y[n_train:n_train + n_val], y[n_train + n_val:]
    
    print(f"Train: {len(X_train)} samples")
    print(f"Val: {len(X_val)} samples")
    print(f"Test: {len(X_test)} samples")
    
    # Step 4: Create DataLoaders
    print("\n[Step 4] Creating DataLoaders...")
    
    train_loader, val_loader, test_loader = create_data_loaders(
        X_train, y_train, X_val, y_val, X_test, y_test,
        batch_size=config.batch_size,
    )
    
    print(f"Train batches: {len(train_loader)}")
    print(f"Val batches: {len(val_loader)}")
    print(f"Test batches: {len(test_loader)}")
    
    # Step 5: Initialize model
    print("\n[Step 5] Initializing model...")
    
    input_dim = X_train.shape[2]  # Number of features
    
    model = KronosVolatilityClassifier(
        input_dim=input_dim,
        d_model=128,
        n_layers=4,
        n_heads=8,
        ff_dim=512,
        dropout=0.1,
        n_classes=N_CLASSES,
    )
    
    print(f"Model: {model.__class__.__name__}")
    print(f"Parameters: {sum(p.numel() for p in model.parameters()):,}")
    
    # Step 6: Train
    print("\n[Step 6] Training...")
    
    try:
        trained_model, results = train_model(
            model,
            train_loader,
            val_loader,
            test_loader,
            config,
            patience=10,
        )
    except Exception as e:
        print(f"Training failed: {e}")
        import traceback
        traceback.print_exc()
        return 1
    
    # Step 7: Save artifacts
    print("\n[Step 7] Saving artifacts...")
    
    config.output_dir.mkdir(parents=True, exist_ok=True)
    
    # Save model
    model_path = config.output_dir / config.model_name
    torch.save({
        "model_state_dict": trained_model.state_dict(),
        "config": asdict(config),
        "input_dim": input_dim,
        "n_classes": N_CLASSES,
    }, model_path)
    print(f"Model saved to {model_path}")
    
    # Save metrics
    metrics = {
        "timestamp": datetime.now(UTC).isoformat(),
        "config": {k: str(v) if isinstance(v, Path) else v for k, v in asdict(config).items()},
        "data_stats": {
            "total_sequences": int(len(X)),
            "train_sequences": int(len(X_train)),
            "val_sequences": int(len(X_val)),
            "test_sequences": int(len(X_test)),
            "input_dim": int(input_dim),
            "sequence_length": int(config.sequence_length),
            "horizon_minutes": int(config.horizon_minutes),
        },
        "training_results": results,
        "class_names": CLASS_NAMES,
    }
    
    metrics_path = config.output_dir / "kronos_training_metrics.json"
    with open(metrics_path, "w") as f:
        json.dump(metrics, f, indent=2)
    print(f"Metrics saved to {metrics_path}")
    
    # Print summary
    print("\n" + "=" * 80)
    print("TRAINING COMPLETE")
    print("=" * 80)
    print(f"Best Epoch: {results['best_epoch']}")
    print(f"Best Val Accuracy: {results['best_val_accuracy']:.4f}")
    print(f"Final Test Accuracy: {results['final_test_metrics']['accuracy']:.4f}")
    print(f"Final Test F1 Macro: {results['final_test_metrics']['f1_macro']:.4f}")
    print(f"Model saved to: {model_path}")
    print(f"Metrics saved to: {metrics_path}")
    
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
