#!/usr/bin/env python3
"""Convert BTC tick data from parquet files to OHLCV candles."""

import pandas as pd
from pathlib import Path
from datetime import datetime
import numpy as np

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
    
    # Convert timestamp and resample
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
    
    # Ensure we have all required columns
    required = ['timestamp', 'open', 'high', 'low', 'close', 'volume']
    for col in required:
        if col not in ohlcv.columns:
            print(f"Warning: Missing column {col} in generated candles")
    
    print(f"Generated {len(ohlcv)} {timeframe_min}-minute candles")
    
    return ohlcv


def main():
    parquet_dir = Path("~/polymarket_arb/state")
    output_file = parquet_dir / "btc_1m_candles.parquet"
    
    # Load all BTC tick data
    all_data = []
    parquet_files = list(parquet_dir.glob("*btc*.parquet"))
    
    print(f"Found {len(parquet_files)} BTC parquet files")
    
    for pf in parquet_files:
        try:
            df = pd.read_parquet(pf)
            print(f"  {pf.name}: {len(df)} rows, columns: {list(df.columns)}")
            all_data.append(df)
        except Exception as e:
            print(f"  Error loading {pf.name}: {e}")
    
    if not all_data:
        print("No data loaded")
        return 1
    
    # Combine all tick data
    combined_ticks = pd.concat(all_data, ignore_index=True)
    print(f"Total ticks: {len(combined_ticks)}")
    
    # Convert to candles
    candles = convert_ticks_to_candles(combined_ticks, timeframe_min=1)
    
    if not candles.empty:
        # Save to parquet
        candles.to_parquet(output_file)
        print(f"Saved candles to {output_file}")
        
        # Print summary
        print(f"\nCandle Summary:")
        print(f"Start: {candles['timestamp'].min()}")
        print(f"End: {candles['timestamp'].max()}")
        print(f"Duration: {(candles['timestamp'].max() - candles['timestamp'].min()).total_seconds() / 3600:.2f} hours")
        
        # Check for gaps
        time_diffs = candles['timestamp'].diff()
        gaps = time_diffs[time_diffs > pd.Timedelta(minutes=2)]
        print(f"Gaps > 2min: {len(gaps)}")
        
        return 0
    else:
        print("No candles generated")
        return 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
