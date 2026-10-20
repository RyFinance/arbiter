# Kalshi Cross-Venue BTC Arbitrage Design

Status: design only. Do not implement live trading from this document without a separate review.

## Scope

This document designs an additive cross-venue research path for BTC Up/Down markets between Kalshi and Polymarket. It does not change the existing Polymarket-only arbitrage engine, execution layer, risk layer, or trading client.

Future code should be isolated in new modules:

- `polymarket_arb/clients/kalshi_client.py`
- `polymarket_arb/cross_venue_arb.py`

Geoblock remains an external blocker for Polymarket live trading from restricted locations. This design must not include or imply bypassing venue restrictions.

## Source Findings

### Kalshi BTC 15-minute markets

Kalshi exposes BTC 15-minute events under the `KXBTC15M` series. A current public API query to `GET /trade-api/v2/events?series_ticker=KXBTC15M` returned events titled `BTC Up or Down - 15 minutes` with `product_metadata.cadence` set to `15MIN`. The event metadata listed the settlement source as CF Benchmarks.

A current event detail query returned a binary market with:

- `open_time` at the 15-minute window start.
- `close_time` at the 15-minute window end.
- `expected_expiration_time` five minutes after close.
- `rules_primary` stating the market resolves Yes if the simple average of the sixty seconds of CF Benchmarks' BRTI before the window end is at least the simple average of the sixty seconds of CF Benchmarks' BRTI before the window start.
- `rules_secondary` stating the official value is based on CF Benchmarks' corresponding Real Time Index, using 60 RTI prices averaged and rounded to the nearest 2 decimals.

Primary references:

- Kalshi API docs, Get Market: https://docs.kalshi.com/api-reference/market/get-market
- Kalshi API changelog, live data examples mention `KXBTC15M`: https://docs.kalshi.com/changelog
- Kalshi BTC category page showing 15 Minute BTC markets: https://kalshi.com/category/crypto/btc
- Current Kalshi API examples queried during design:
  - `https://api.elections.kalshi.com/trade-api/v2/events?series_ticker=KXBTC15M&limit=5`
  - `https://api.elections.kalshi.com/trade-api/v2/events/{event_ticker}`

### Polymarket BTC Up/Down 5-minute markets

Polymarket BTC Up/Down 5-minute markets are individual 5-minute windows. A current market page states that the market resolves Up when Bitcoin's price at the end of the 5-minute window is greater than or equal to its price at the start; otherwise it resolves Down. The cited resolution source is the Chainlink BTC/USD data stream.

Polymarket docs also expose Chainlink crypto/TWAP market data references, and the fee docs call out 5-minute and 15-minute crypto fee curves. Polymarket's general resolution docs describe UMA Optimistic Oracle resolution for markets broadly, but the BTC 5-minute market page is the more specific source for this product's Chainlink BTC/USD rule.

Primary references:

- Polymarket BTC Up/Down 5-minute example page: https://polymarket.com/event/btc-updown-5m-1785657300
- Polymarket real-time market data docs: https://docs.polymarket.com/market-data/realtime-data
- Polymarket market-data overview, Chainlink TWAP prices: https://docs.polymarket.com/market-data/overview
- Polymarket trading fees, 5-minute and 15-minute crypto fee curves: https://docs.polymarket.com/trading/fees
- Polymarket resolution overview: https://docs.polymarket.com/concepts/resolution

## Core Constraint

These markets are directionally similar, but they are not automatically fungible:

- Kalshi uses CF Benchmarks BRTI 60-second averages at the 15-minute window start and end.
- Polymarket BTC 5-minute pages cite Chainlink BTC/USD at the 5-minute window start and end.

The first implementation must treat oracle/source mismatch as a hard pairing risk. Cross-venue opportunities should be paper-only until logged data proves that the two settlement references agree closely enough around window boundaries after rounding, latency, and outage edge cases.

## Window Mapping

One Kalshi 15-minute window maps to three Polymarket 5-minute windows:

```text
Kalshi window:     [T, T+15m)
Polymarket leg A:  [T, T+5m)
Polymarket leg B:  [T+5m, T+10m)
Polymarket leg C:  [T+10m, T+15m)
```

The mapping is valid only when all of these are true:

- The Kalshi event and all three Polymarket markets refer to BTC/USD direction.
- Window boundaries match in UTC after converting from venue display time.
- The Polymarket windows are contiguous and non-overlapping.
- Each Polymarket market's start and end timestamps match the expected five-minute grid.
- The Kalshi start equals the first Polymarket start, and the Kalshi end equals the third Polymarket end.
- All four markets are open and tradable for the intended entry time.
- Settlement sources and rule text are known and stored with the opportunity record.

Implementation detail:

```python
kalshi_start = kalshi_market.open_time
kalshi_end = kalshi_market.close_time
poly_windows = [
    (kalshi_start, kalshi_start + 5 minutes),
    (kalshi_start + 5 minutes, kalshi_start + 10 minutes),
    (kalshi_start + 10 minutes, kalshi_end),
]
```

Do not infer window boundaries from display titles alone. Use API timestamps where available, then keep the original title/rules as audit metadata.

## Payoff Relationship

Kalshi Yes pays if:

```text
avg_cf_brti(last 60s before T+15m) >= avg_cf_brti(last 60s before T)
```

Polymarket Up for each 5-minute segment pays if:

```text
chainlink_btc_usd(segment_end) >= chainlink_btc_usd(segment_start)
```

The three Polymarket outcomes do not compose perfectly into the Kalshi outcome. Example: BTC can go Up in two 5-minute segments and still end lower over the full 15-minute window depending on path and magnitude. Therefore this is not a deterministic sum-to-one arbitrage between three 5-minute legs and one 15-minute leg.

The future engine should use the 5-minute legs as a high-frequency proxy for the 15-minute direction, not as a guaranteed replication portfolio.

## Edge Calculation

The cross-venue engine should buy the underpriced side on each venue only when both venues express the same event hypothesis and the expected value clears fees, slippage, latency, and oracle-basis buffers.

Define normalized prices in dollars:

```text
K_yes = best executable Kalshi Yes ask
K_no = best executable Kalshi No ask
P_up_i = best executable Polymarket Up ask for segment i
P_down_i = best executable Polymarket Down ask for segment i
```

For Polymarket, derive an aggregate 15-minute direction probability from the three 5-minute windows. This must be a model output, not a deterministic composition:

```text
poly_implied_prob_15m_up = f(P_up_1, P_up_2, P_up_3, spreads, BTC path, time_remaining)
poly_implied_prob_15m_down = 1 - poly_implied_prob_15m_up
```

The simplest paper-trading baseline can use a calibrated logistic model or historical lookup table:

```text
poly_implied_prob_15m_up = calibrated_probability(
    p_up_1,
    p_up_2,
    p_up_3,
    current_btc_vs_kalshi_start,
    minutes_remaining,
)
```

Cross-venue edge candidates:

```text
buy_kalshi_yes_edge =
    poly_implied_prob_15m_up
    - K_yes
    - kalshi_fee
    - expected_slippage
    - oracle_basis_buffer
    - latency_buffer

buy_kalshi_no_edge =
    poly_implied_prob_15m_down
    - K_no
    - kalshi_fee
    - expected_slippage
    - oracle_basis_buffer
    - latency_buffer
```

For buying Polymarket 5-minute sides using Kalshi as the anchor, estimate segment probabilities conditioned on the Kalshi 15-minute book and current BTC path:

```text
buy_poly_up_i_edge =
    estimated_prob_segment_i_up_from_kalshi_context
    - P_up_i
    - polymarket_fee
    - expected_slippage
    - oracle_basis_buffer
    - latency_buffer

buy_poly_down_i_edge =
    estimated_prob_segment_i_down_from_kalshi_context
    - P_down_i
    - polymarket_fee
    - expected_slippage
    - oracle_basis_buffer
    - latency_buffer
```

The execution rule is intentionally conservative:

- Buy only the underpriced side.
- Do not short.
- Do not assume a simultaneous hedge exists.
- Require positive edge after all costs and buffers.
- Require a minimum confidence score from a future cross-venue match-confidence gate.
- Paper trade first until realized settlement comparisons are logged.

## Data Needed

Market metadata:

- Kalshi event ticker, market ticker, title, open time, close time, expected expiration, rules, settlement source, price tick structure.
- Polymarket event/market identifiers, token IDs, title, start/end timestamps, rules, resolution source, fee flags.

Order book data:

- Best executable bid/ask for each side on both venues.
- Depth at target size.
- Timestamp from receipt and, where provided, exchange/server timestamp.
- Spread and estimated slippage by size.

Reference price data:

- CF Benchmarks BRTI data around Kalshi start/end averaging windows.
- Chainlink BTC/USD stream or TWAP data around Polymarket segment boundaries.
- External BTC spot feed only as diagnostic context, not as settlement truth.

Outcome data:

- Final settlement value/outcome for Kalshi.
- Final settlement value/outcome for each Polymarket 5-minute segment.
- Dispute, outage, correction, or delayed settlement flags.

Environment and compliance:

- Geoblock status per venue.
- Account/trading eligibility.
- Fee tier and maker/taker classification.

## Future Module Boundaries

### `kalshi_client.py`

Responsibilities:

- Fetch Kalshi series/events/markets for `KXBTC15M`.
- Fetch Kalshi order books.
- Normalize prices to dollars.
- Parse and expose event windows from API timestamps.
- Persist raw settlement source and rules fields.
- Expose read-only/public methods first; authenticated order methods can be added later behind explicit review.

Non-responsibilities:

- No Polymarket access.
- No arbitrage decision logic.
- No live trading in the first design implementation.

Suggested interface:

```python
class KalshiClient:
    async def list_btc_15m_events(self, start: datetime, end: datetime) -> list[KalshiEvent]: ...
    async def get_event(self, event_ticker: str) -> KalshiEventDetail: ...
    async def get_orderbook(self, market_ticker: str) -> KalshiOrderBook: ...
```

### `cross_venue_arb.py`

Responsibilities:

- Map one Kalshi 15-minute market to three Polymarket 5-minute markets.
- Validate timestamp alignment and settlement-source metadata.
- Compute paper-trade-only edge candidates.
- Apply risk gates, including oracle-basis buffers and match-confidence thresholds.
- Emit structured opportunity logs for later ML calibration.

Non-responsibilities:

- No direct live order placement initially.
- No changes to existing Polymarket-only `arb_engine.py`, `execution.py`, `risk.py`, or `trading_client.py`.

Suggested interface:

```python
class CrossVenueArbScanner:
    async def find_window_pairs(self, now: datetime) -> list[WindowPair]: ...
    async def score_pair(self, pair: WindowPair) -> list[CrossVenueOpportunity]: ...
```

## Risk Gates

Required gates before any future live implementation:

- `settlement_source_known`: both venues expose explicit rule/source metadata.
- `oracle_basis_acceptable`: historical CF Benchmarks BRTI vs Chainlink BTC/USD differences are below configured limits near settlement boundaries.
- `window_alignment_exact`: all four windows align on expected UTC boundaries.
- `liquidity_sufficient`: executable depth exists after fees at the intended size.
- `latency_budget_met`: quote age and order round-trip estimates fit the remaining window.
- `geoblock_clear`: venue APIs permit trading from the current jurisdiction.
- `paper_trade_validated`: paper fills and realized settlements show positive edge after costs.
- `manual_reviewed`: design and implementation reviewed before enabling live trading.

## Main Risks

- Oracle mismatch: CF Benchmarks BRTI and Chainlink BTC/USD can diverge, especially around fast moves and boundary seconds.
- Temporal mismatch: Kalshi averages the 60 seconds before start/end; Polymarket 5-minute wording references start/end prices from Chainlink.
- Non-replication: three Up/Down 5-minute outcomes do not mechanically replicate a 15-minute start-to-end outcome.
- Fee drag: both venues may have fees and different tick sizes; the apparent edge can disappear after costs.
- Spread and depth: crypto micro-markets can move faster than resting depth can be consumed.
- Latency: stale quotes are especially dangerous near expiry.
- Settlement delay/dispute: payout timing and finality can differ by venue.
- Compliance: geoblock and account eligibility can prevent live execution regardless of technical readiness.

## Recommended First Build

Build a paper-only scanner:

1. Fetch Kalshi `KXBTC15M` events and corresponding market details.
2. Fetch active Polymarket BTC 5-minute markets.
3. Pair windows only when UTC boundaries match exactly.
4. Store raw rule and settlement-source metadata for every pair.
5. Compute conservative underpriced-side edges using current books and large buffers.
6. Log all candidates, rejected pairs, market states, and final settlements.
7. After enough data is collected, measure CF Benchmarks/Chainlink basis and train or calibrate the cross-venue probability model.

Do not enable live order placement until the oracle-basis study and paper-trade replay prove that the relationship is tradable after fees, slippage, and latency.
