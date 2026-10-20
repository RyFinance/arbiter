# BTC offline feature report

Rows: 139,523; label horizon: 15 bars; usable labels: 139,508

## Causal realized-volatility regime distribution
- low: 41,421 (29.69%)
- mid: 48,023 (34.42%)
- high: 50,004 (35.84%)
- nan: 60 (0.04%)

## Feature/label correlations (Pearson, complete pairs)
- `momentum_60`: -0.020587 (n=139,448)
- `bb_width`: 0.019096 (n=139,489)
- `day_of_week`: 0.016489 (n=139,508)
- `realized_vol_30`: 0.013661 (n=139,478)
- `zscore_20`: -0.013346 (n=139,489)
- `vol_squeeze`: 0.013135 (n=139,478)
- `bb_lower`: -0.012669 (n=139,489)
- `close`: -0.012592 (n=139,508)
- `sma_5`: -0.012534 (n=139,504)
- `bb_mid`: -0.012486 (n=139,489)
- `sma_20`: -0.012486 (n=139,489)
- `sma_50`: -0.012351 (n=139,459)
- `bb_upper`: -0.012302 (n=139,489)
- `sma_200`: -0.012276 (n=139,309)
- `rsi_14`: -0.011148 (n=139,494)
- `vol_regime`: -0.011118 (n=139,448)
- `atr_14`: 0.010848 (n=139,495)
- `hour_utc`: 0.010121 (n=139,508)
- `macd`: -0.008999 (n=139,483)
- `macd_signal`: -0.007968 (n=139,475)
- `macd_hist`: -0.004967 (n=139,475)
- `zscore_60`: -0.003987 (n=139,449)
- `momentum_20`: -0.003895 (n=139,488)
- `vwap_distance`: 0.001330 (n=139,479)
- `half_life_60`: -0.001222 (n=138,761)

## Leakage audit
- Feature columns containing future/label tokens: none
- All rolling/EMA/VWAP features are causal (current bar and prior bars only).
- `vol_regime` uses expanding tertile thresholds from realized-volatility observations strictly before the current bar; it never uses a full-sample rank or quantile.
- The forward-return label uses `close.shift(-horizon)` and is excluded from feature input.
