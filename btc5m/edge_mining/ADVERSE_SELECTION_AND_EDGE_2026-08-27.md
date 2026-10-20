# Is there a real, exploitable edge on Polymarket's BTC up/down markets? (2026-08-27)

Full research pass: external research on documented exploits + Polymarket's own fee
mechanics, plus a real-order-book-data test of the one candidate edge this project has
actually validated (the reversal signal). Answers the standing question from the prior
session turn: does maker-order execution survive real adverse selection?

## 1. There WAS a real, documented exploit here. It has been patched.

Wallet `0x8dxd` turned $313 into ~$414-438k between Dec 2025 and Jan 2026 trading
Polymarket's 15-minute BTC/ETH/SOL up/down markets, with a **98% win rate across 6,615
trades**. The mechanism: Polymarket's own displayed price lagged real exchange spot
price by enough that a bot watching Binance/Coinbase directly could buy the
already-decided winning side while Polymarket's book was still showing near-50/50 odds.
This validates the mechanism this project's `arb_engine.py` was originally built
around — the repricing lag is real and was, for a period, large enough to exploit
directly and repeatedly.

Polymarket's response: a **dynamic taker fee specifically on the 15-minute crypto
markets**, introduced without a formal announcement (quietly rolled into their fee
docs), explicitly to neutralize this exact strategy by making entries near 50/50
expensive. Combined with faster internal repricing, this appears to have closed the
original gap — consistent with everything measured earlier this session (live
mispricings now decay in 1-3ms, not the multi-second lag `0x8dxd` exploited).

**Confirmed via official docs** (docs.polymarket.com/trading/fees, not secondary
aggregator sites, several of which quote an inconsistent "3.15%" figure not found in
the primary source): `fee = shares × 0.07 × p × (1-p)` for crypto, taker-only, maker
fee is **exactly zero**, with a 20% maker rebate share funded by collected taker fees.
No time-to-expiry term in the documented formula. This matches what the live Gamma API
returns for both the 5m and 15m markets today.

## 2. The zero-maker-fee structure is a real, currently-open lever

Every directional signal found this session (Phase 0 ML classifier, the reversal
signal) failed cost-adjusted profitability **as a taker** — the 0.07×p×(1-p) fee alone
eats most of any edge this small. But makers pay nothing. `execution.py` has never
built a resting/GTC order; `py_clob_client_v2` supports it and it's unused. This was
the open question at the end of the last turn.

## 3. Real-data adverse-selection test (this turn's new work)

Used actual captured Polymarket order-book ticks — `edge_mining/out/snapshots.parquet`,
8.66M real bb/ba/bq/qa snapshots across 2,602 real 5-minute BTC windows, Aug 12-25 2026
— not a spot-price proxy. Only the 5-min market has this real book-tick history
captured (the validated 15-min signal has no matching real order-book capture yet, see
caveats). Method (`edge_mining/adverse_selection_test.py`): at window open, the
reversal signal (same fixed rule validated on 19 months of spot data, no re-fitting)
picks a side; simulate resting a maker bid at the then-current best bid on that token;
scan real subsequent ticks for whether the book ever crosses through that price
(idealized fill rule — see caveats).

| Variant | n windows | unconditional acc. | fill rate | **acc. \| FILLED** | acc. \| not filled | net EV/$1 (filled) |
|---|---|---|---|---|---|---|
| N1 reversal | 1,443 | 53.43% | 94.7% | **51.35%** | 90.79% | +2.72¢ |
| N2 reversal | 1,443 | 53.15% | 95.3% | **51.27%** | 91.18% | +2.18¢ |
| N3 reversal | 1,442 | 53.61% | 95.4% | **51.71%** | 92.54% | +2.44¢ |
| decay reversal | 1,441 | 52.46% | 95.1% | **50.44%** | 91.55% | +1.42¢ |

**Adverse selection is real and large.** Accuracy conditional on your resting order
actually filling craters from ~53% to ~51%, while the (rare, ~5%) unfilled windows show
90%+ accuracy. The mechanism is exactly textbook: when your directional call is
correct, the token's price runs *away* from your resting bid (never crosses it — you
sit there unfilled and miss the win); when you're wrong, price collapses *through* your
bid on the way to zero, and that's when you get filled. Passive maker orders on a
directional signal get systematically filled on the losing side.

**But a small edge survives anyway.** Average fill price is modestly below 50¢
(48.6-49.3¢), so the asymmetric payout (win pays `1-p`, loss costs `p`) plus the zero
maker fee leaves real, positive, if unglamorous EV: **+1.4¢ to +2.7¢ per $1 of filled
notional**, ~$19-37 total PnL across ~1,370 filled $1-notional trades over the 13-day
sample. This is not the 67%, not even the 53% headline accuracy — after the real cost
that actually applies to a maker strategy, it's closer to a 51-52% coin flip with
favorable odds.

## 4. Sizing reality check

Real captured book depth: median best-bid/ask size ≈ $139, mean ≈ $272; median depth
across the top 5 price levels ≈ $2,646. A strategy sized much above the low hundreds of
dollars per window would walk its own resting order down through multiple price levels,
degrading the already-thin average edge further — this has not been modeled here and
would shrink the effective edge at any real trading size.

## 5. Cross-venue (Kalshi) angle: revisited, likely a modeling artifact, not confirmed

This project already has an unused Kalshi/Polymarket cross-venue observer
(`cross_venue_arb.py`, `kalshi_client.py`) with 828 real "fully paired" observations
from a single day (Aug 3 2026): mean gap 7.6¢, median 5.7¢, max 33¢ — large numbers,
if real. But inspecting an actual record shows the gap is computed by averaging three
**sequential, independently-anchored** Polymarket 5-min window prices against one
Kalshi 15-min price — comparing bets with different reference points, not equivalent
claims. The project's own design doc (`docs/kalshi_cross_venue_design.md`) already
flagged this precisely: "not a deterministic sum-to-one arbitrage," composition "must
be a model output, not deterministic composition" — and that modeling was never done.
Kalshi also settles on CF Benchmarks BRTI (multi-exchange index) vs. Polymarket's
Chainlink BTC/USD stream — a second, real, unquantified oracle-basis risk. **This
captured "edge" should be treated as very likely mostly a measurement artifact until
someone builds the real path-dependent composition model** — not re-examined further
this pass given the reversal+maker angle above is already validated and quantifiable.

## Bottom line

A real, small, adverse-selection-tested, positive-EV edge exists: **the reversal
signal, executed as a resting maker order to collect Polymarket's zero maker fee**,
nets roughly +1.4 to +2.7¢ per $1 of filled notional on the 5-min market (the only
horizon with real order-book validation so far), materially reduced from the naive
53%-accuracy headline by real, measured adverse selection, and further constrained by
thin real book depth (~$100-300 per window before you move your own price). The
stronger, already-validated 15-min signal (holdout accuracy 52.7%, larger edge) has not
had this same real-book adverse-selection test run against it yet — no historical
15-min order-book capture exists to test it on; the qualitative adverse-selection
mechanism should transfer, but the exact magnitude is unverified there.
