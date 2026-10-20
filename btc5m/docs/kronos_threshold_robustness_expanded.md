# Kronos Threshold Robustness

## Calibration Skew
- Checkpoints: 27
- Time span: 2026-07-26T08:32:00Z to 2026-07-30T12:41:00Z
- Realized regimes: `{'LOW': 22, 'MED': 5}`
- Volatility sample buckets: `{'HIGH_SAMPLE': 9, 'LOW_SAMPLE': 9, 'MED_SAMPLE': 9}`
- Source files: `{'btc_ticks_jul26-28.parquet': 17, 'btc_ticks_jul29.parquet': 6, 'btc_ticks_jul30-31.parquet': 4}`
- UTC hours: `{'0': 3, '10': 2, '11': 2, '12': 5, '16': 3, '2': 1, '20': 2, '4': 2, '8': 5, '9': 2}`
- Skew flags: `{'realized_regime_dominated': True, 'single_source_dominated': True, 'single_date_dominated': False, 'single_hour_dominated': False}`

## Holdout
- Source: LSE BTC/USD 1m candles
- Range: 2026-07-04T00:00:00+00:00 to 2026-07-21T16:39:00+00:00
- Candles: 6000
- Checkpoints: 18
- Status: ok
- Selected checkpoint dates: `{'2026-07-04': 3, '2026-07-06': 3, '2026-07-10': 3, '2026-07-12': 3, '2026-07-16': 3, '2026-07-21': 3}`
- Selected checkpoint sources: `{'lse:2026-07-04:2026-07-05': 3, 'lse:2026-07-06:2026-07-07': 3, 'lse:2026-07-10:2026-07-11': 3, 'lse:2026-07-12:2026-07-13': 3, 'lse:2026-07-16:2026-07-17': 3, 'lse:2026-07-21:2026-07-22': 3}`
- Selected checkpoint UTC hours: `{'12': 6, '13': 6, '8': 6}`
- Realized regimes: `{'LOW': 12, 'MED': 6}`
- Old match rate: 9/18 = 50.00%
- New match rate: 11/18 = 61.11%
