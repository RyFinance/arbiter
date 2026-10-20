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
- Range: 2026-06-30T00:00:00+00:00 to 2026-07-19T09:19:00+00:00
- Candles: 8000
- Checkpoints: 12
- Status: ok
- Selected checkpoint dates: `{'2026-06-30': 2, '2026-07-01': 2, '2026-07-07': 2, '2026-07-08': 2, '2026-07-14': 1, '2026-07-15': 1, '2026-07-18': 1, '2026-07-19': 1}`
- Selected checkpoint sources: `{'lse:2026-06-30:2026-07-02': 4, 'lse:2026-07-07:2026-07-09': 4, 'lse:2026-07-14:2026-07-16': 2, 'lse:2026-07-18:2026-07-20': 2}`
- Selected checkpoint UTC hours: `{'0': 3, '16': 3, '20': 3, '4': 3}`
- Required target UTC hours: `[0, 4, 16, 20]`; present: `[0, 4, 16, 20]`
- Realized regimes: `{'LOW': 8, 'MED': 4}`
- Old match rate: 5/12 = 41.67%
- New match rate: 6/12 = 50.00%
- Retuning: `{'status': 'evaluated_on_untouched_holdout', 'trigger': 'production_threshold_match_rate_below_80_percent', 'training_source': '~/polymarket_arb/state/kronos_threshold_calibration.json', 'training_checkpoints': 27, 'candidate_thresholds': {'low_threshold': 0.03, 'high_threshold': 0.05, 'match_rate': 0.8888888888888888, 'matches': 24, 'total': 27}, 'untouched_holdout_validation': {'low_threshold': 0.03, 'high_threshold': 0.05, 'matches': 6, 'total': 12, 'match_rate': 0.5}, 'changes_production_thresholds': False}`
