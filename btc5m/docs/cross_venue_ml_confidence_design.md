# Cross-Venue Match-Confidence Model Design

## Purpose

This model scores whether a Polymarket market and a Kalshi market appear to track the same underlying event closely enough for a future `cross_venue_arb.py` module to consider firing.

It is a gate, not a directional predictor. It does not forecast price movement, estimate fair value, or decide which side to buy. Its only job is to answer:

> Are these two venue markets sufficiently matched that cross-venue edge calculations are allowed to run?

No live trading code should depend on this until a real logged cross-venue dataset exists and the model has been validated out of sample.

## Current Data Status

There is no real logged cross-venue training dataset yet. Training is pending data collection.

The first production step is therefore instrumentation, not modeling:

- Log candidate Polymarket/Kalshi market pairs.
- Capture market metadata snapshots from both venues.
- Capture synchronized order book and mid-price snapshots.
- Record settlement source, window start/end, market status, and final resolution.
- Label pairs after settlement as true match, partial/ambiguous match, or non-match.

Until that dataset exists, any model thresholds should be treated as design placeholders only.

## Model Choice

Use one of two small, interpretable classifiers:

- Preferred: LightGBM binary classifier.
- Fallback: shallow decision tree or calibrated random forest if LightGBM adds deployment friction.

LightGBM is a good first choice because it handles mixed numeric/categorical-style features, missing values, and nonlinear feature interactions without a large model. A shallow decision tree is easier to audit but likely less stable once features become noisy.

The model output should be a calibrated probability:

```text
P(pair_tracks_same_underlying_event)
```

The output should be used only as a gating score.

## Candidate Labels

Suggested labels after enough historical data is collected:

- `1.0`: Same event and same settlement semantics.
- `0.5`: Same broad event, but materially different settlement window, oracle, status, or payout condition.
- `0.0`: Different event, incompatible terms, or insufficient evidence of equivalence.

For the first production classifier, either drop ambiguous `0.5` examples or use them only for calibration analysis. Do not train a binary model that treats known ambiguous pairs as clean positives.

## Feature Set

### Window Alignment

`window_overlap_pct`

Intersection-over-union of the two market windows:

```text
overlap_seconds / union_seconds
```

For BTC examples, a 15-minute Kalshi window mapped to three 5-minute Polymarket windows may produce high overlap for the full bundle and lower overlap for individual child windows. The model should receive the actual overlap, not a hardcoded assumption.

Additional window features:

- `start_delta_seconds`
- `end_delta_seconds`
- `duration_ratio`
- `same_calendar_day`
- `venue_window_granularity_match`

### Settlement Semantics

`settlement_source_match`

Categorical or binary feature comparing the stated settlement source or oracle. Examples:

- exact same source
- compatible source family
- different source
- unknown

Additional settlement features:

- `settlement_rule_text_similarity`
- `price_reference_match`
- `timezone_match`
- `inclusive_boundary_match`
- `resolution_delay_delta_seconds`

This is a high-importance feature. A high price correlation should not override mismatched settlement rules.

### Historical Price Behavior

`historical_price_correlation`

Correlation of synchronized mid-price returns or mid-price levels over a shared observation window.

Recommended variants:

- `mid_return_corr_5m`
- `mid_return_corr_30m`
- `mid_level_corr_30m`
- `price_delta_mean_abs`
- `price_delta_p95_abs`

Use time-aligned snapshots only. Missing data and stale books should be explicit features rather than silently forward-filled without limits.

### Liquidity And Spread

`spread_tightness`

A normalized measure of whether both markets have reliable executable prices.

Possible features:

- `poly_best_bid_ask_spread`
- `kalshi_best_bid_ask_spread`
- `max_spread`
- `mean_spread`
- `poly_depth_usd_1pct`
- `kalshi_depth_usd_1pct`
- `min_depth_usd_1pct`
- `book_staleness_seconds_max`

The confidence gate should be stricter when either venue has a wide spread or stale book, because apparent agreement can be an artifact of bad quotes.

### Metadata Similarity

`token_market_metadata_similarity`

Similarity between the market titles, slugs, descriptions, tickers, outcome labels, and relevant token metadata.

Suggested components:

- `title_embedding_cosine`
- `description_embedding_cosine`
- `normalized_keyword_jaccard`
- `asset_symbol_match`
- `outcome_label_match`
- `market_category_match`
- `contract_ticker_similarity`
- `negation_or_direction_mismatch`

For binary event markets, explicitly detect reversed semantics such as one market asking "above X" and another asking "below X". These may be related but are not the same side.

### Status And Lifecycle

Useful guardrail features:

- `both_open`
- `time_to_close_delta_seconds`
- `accepting_orders_both`
- `halted_or_paused_either`
- `resolved_either`
- `minimum_tick_size_compatible`
- `fee_model_compatible`

Some of these can also be hard filters before model scoring.

## Calibration And Thresholds

Raw LightGBM probabilities should be calibrated before use. Use Platt scaling or isotonic regression on a validation set that is separated by event family and date.

Initial threshold policy:

- `score >= 0.95`: allow cross-venue arb evaluation.
- `0.80 <= score < 0.95`: monitor only, log for review.
- `score < 0.80`: reject pair.

These are starting points, not final trading thresholds. Thresholds should be tuned against false-positive cost, not only accuracy. A false positive can allow trades between markets that settle differently, so precision near the firing threshold matters more than recall.

Recommended operating metrics:

- Precision at the fire threshold.
- False-positive rate by event family.
- Calibration error by score bucket.
- Coverage rate: percent of candidate pairs allowed through.
- Post-settlement mismatch rate among allowed pairs.

## Training Plan

1. Add passive logging for candidate pairs and synchronized snapshots.
2. Build a labeled dataset after enough markets resolve.
3. Split by time and event family to avoid leakage.
4. Train a simple baseline decision tree.
5. Train LightGBM and compare against the baseline.
6. Calibrate probabilities on a held-out validation set.
7. Freeze a threshold policy and evaluate on a final test period.
8. Deploy in monitor-only mode before allowing it to gate live arb evaluation.

## Runtime Contract

Future `cross_venue_arb.py` should call a small scorer interface:

```python
score = match_confidence_model.score(polymarket_market, kalshi_market, aligned_market_data)
```

Expected response:

```python
MatchConfidenceResult(
    score=0.972,
    decision="allow",
    threshold=0.95,
    reasons=[
        "window_overlap_pct=1.00",
        "settlement_source_match=exact",
        "historical_price_correlation=0.91",
    ],
)
```

The arb engine should treat any model error, missing required feature, stale book, or unknown settlement source as `decision="reject"` unless explicitly running in monitor-only mode.

## Guardrails

- Never use this model to choose trade direction.
- Never allow metadata similarity alone to approve a pair.
- Require settlement-source compatibility as either a hard filter or a heavily weighted model feature.
- Reject or monitor-only when either market has stale books, paused trading, unknown resolution rules, or unresolved semantic mismatch.
- Keep every score explainable through logged feature values.

## Open Questions

- Which exact Kalshi BTC settlement index/source maps to each Polymarket BTC market type?
- Are boundary conditions identical at window start and end?
- How often do Polymarket 5-minute markets line up cleanly with Kalshi 15-minute windows?
- What minimum liquidity and maximum spread should be required before a pair is eligible?
- How many labeled resolved pairs are needed before the first calibrated model is trustworthy?

## Recommendation

Start with passive data collection and a rules-first baseline. Once enough labeled pairs exist, train a shallow decision tree and LightGBM side by side, calibrate the better model, and deploy it as a monitor-only gate. Promote it to an active gate only after it shows very high precision at the chosen threshold on unseen event families and dates.
