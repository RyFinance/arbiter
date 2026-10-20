# LSE Backfill Feasibility

Generated: `2026-08-03T08:05:21.719740+00:00`

## Finding

LSE historical BTC/USD prices are not venue settlement labels. The cross-venue confidence gate needs settled Kalshi/Polymarket outcomes or verified official oracle boundary values, not a proxy spot series.

No labels or model artifact were created. BTC/USD price history, even if accessible, is a proxy series and not proof of settled Kalshi/Polymarket equivalence.

## LSE Capability

- REST historical BTC/USD available: `True`
- Candle timeframes confirmed: `15m, 1h, 1m, 5m`
- Max candle lookback tested successfully: `365` days
- Max candle rows returned in a bounded request: `50`
- Candle lookbacks tested: `7, 30, 365` days
- Candle limits tested: `10, 20, 50`
- Candle pagination discovered: `False`
- Tick export accepted: `True`
- Tick export status code: `202`
- Tick export terminal status: `ready`
- Tick export polls attempted: `2`
- Tick export rows reported: `1285632`
- Tick export bytes reported: `3957631`
- Tick export download attempted: `True`
- Tick export download status: `401`
- Tick export result retrievable during bounded poll: `False`
- WebSocket real-time BTC/USD ticker available: `True`
- Venue settlement labels available from LSE: `False`

## Bounded Candle Probes

- `1m` lookback `7` days, limit `10`: ok=`True`, status=`200`, rows=`10`, range=`2026-07-27 00:00:00.000000` to `2026-07-27 00:09:00.000000`
- `1m` lookback `30` days, limit `50`: ok=`True`, status=`200`, rows=`50`, range=`2026-07-04 00:00:00.000000` to `2026-07-04 00:49:00.000000`
- `1m` lookback `365` days, limit `10`: ok=`True`, status=`200`, rows=`10`, range=`2025-08-03 00:00:00.000000` to `2025-08-03 00:09:00.000000`
- `5m` lookback `30` days, limit `20`: ok=`True`, status=`200`, rows=`20`, range=`2026-07-04 00:00:00.000000` to `2026-07-04 01:35:00.000000`
- `15m` lookback `30` days, limit `20`: ok=`True`, status=`200`, rows=`20`, range=`2026-07-04 00:00:00.000000` to `2026-07-04 04:45:00.000000`
- `1h` lookback `365` days, limit `20`: ok=`True`, status=`200`, rows=`20`, range=`2025-08-03 00:00:00.000000` to `2025-08-03 19:00:00.000000`

## Fastest Honest Live Path

- Current observed rate: `2.0` Kalshi windows per 15 minutes
- Already observed but unlabeled: `2` windows
- Additional time to 30 labeled windows: `210.0` minutes
- Additional time to 100 labeled windows: `735.0` minutes
- From scratch to 30 windows: `225.0` minutes
- From scratch to 100 windows: `750.0` minutes

The observer should keep writing real records and a separate settlement-labeling pass should only add labels after venue outcomes or official oracle boundary values are verifiably available.
