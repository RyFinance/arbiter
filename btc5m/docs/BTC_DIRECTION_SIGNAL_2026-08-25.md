# BTC direction signal — Phase 0 validation (2026-08-25)

**Verdict: no-go on all three timeframes.** This was the pre-registered
gate for the arb→directional-bot pivot (see the approved plan from
2026-08-22): don't rebuild the execution layer until a genuinely new signal
beats a majority baseline AND clears a realistic transaction cost, on real
holdouts, per timeframe. None of 5min/60min/1440min clear that bar. This
report documents the real numbers honestly, same discipline as every ML
round this session.

## Method

- Real 1-minute BTC/USD candles, same source used all session
  (`state/btc_1m_candles_real_2025_2026.parquet`, 845,763 rows,
  2025-01-01 → 2026-08-11).
- **New features, not a rerun of the prior failed directional study**
  (`docs/btc_offline_chronological_evaluation_2026-08-11.md`,
  `docs/btc_offline_sizing_sharpe_2026-08-15.md`, which used only
  price/technical-indicator features and lost money everywhere):
  - Multi-window momentum (`mom_{1,5,15,60,240,1440}m_bps`) and
    mean-reversion z-scores (`mrev_{5,15,60}m_z`), time-indexed so gaps
    can't blend stale pre-gap prices into a post-gap value.
  - **Order-flow imbalance** (`order_flow_imbalance_{1,5,15}m`) from real
    Binance Vision tick-trades data — 830,862 1-minute taker buy/sell
    volume rows, bulk-downloaded and aggregated for this round
    (`scripts/fetch_binance_tick_trades.py`, new; 588-day window, 11 days
    missing/577 present = 98.1% coverage). This is the genuinely new signal
    class the plan called for: aggressor-side volume as a direct
    expression of buying/selling pressure, distinct from anything tried in
    the prior directional study or the volatility-regime bake-off.
  - Calendar features (hour/day-of-week sin/cos), `log_volume`, `rsi_14`.
- Target: `sign(close[t+horizon] - close[t])`, computed independently for
  horizon ∈ {5, 60, 1440} minutes — one model each, not pooled (the vol-
  regime horizon experiment found a 6.2pp accuracy swing from horizon alone
  on an easier 3-class target; direction was expected to be at least as
  sensitive).
- Same leak-free methodology as every round this session:
  `split_time_disjoint` (train 50% / val 12.5% / holdout A/B/C 12.5% each,
  chronological, reused directly from `fill_and_test_arb_v3.py`), majority-
  baseline comparison per holdout, LightGBM with the same baseline
  hyperparameters used throughout.
- PnL is an explicitly-labeled **proxy** (mirrors the prior failed study's
  own methodology for direct comparability): a correct/incorrect call
  captures/loses the underlying's own forward return, net of a flat 20bps
  round-trip cost. This is not a simulation of Polymarket's actual binary
  $0/$1 settlement against real historical order-book prices — that data
  isn't available for this window, same caveat the prior study carried.

New script: `scripts/fill_and_test_direction_v1.py` — deliberately kept
separate from `fill_and_test_arb_v3.py` (the volatility-regime pipeline);
different target, different feature philosophy, different downstream use.

## Results

| Horizon | Mean holdout acc. | vs majority baseline | Net PnL @ threshold (bps, 3 holdouts) |
|---|---|---|---|
| 5min | 51.06% | +0.77pp | **Negative in all 3 holdouts, all thresholds** (-17.6 to -20.1 bps) |
| 60min | 50.89% | +0.42pp | **Negative in all 3 holdouts, all thresholds** (-17.9 to -20.7 bps) |
| 1440min | 50.81% | -1.48pp | **Positive in holdout A only** (+8.4 to +28.7 bps); negative in B and C (-3.0 to -23.1 bps) |

Full per-holdout, per-threshold numbers are in
`state/btc_direction_training_metrics_v1.json`.

**5min and 60min: accuracy edges nominally above 50%, but the edge is too
small relative to cost.** Accuracy does climb at higher confidence
thresholds (up to 56-60% on some holdouts at threshold 0.65), which is a
real, non-trivial signal — but at these horizons the underlying's typical
forward move is only a few bps, so a flat 20bps round-trip cost swamps it
almost exactly regardless of threshold. Every single accuracy/threshold
combination at these two horizons has negative mean PnL and negative
Sharpe (-0.2 to -1.6 per trade). This is the same failure shape as the
prior study, for a related but distinct reason: there it was low accuracy;
here it's real (if modest) accuracy that a flat transaction cost still eats
entirely.

**1440min: one holdout looks genuinely promising, but it's 1 of 3, which
fails this project's own bar.** Holdout A shows a real, monotonically
improving edge with confidence (thr 0.52→0.65: accuracy 52.5%→55.0%, mean
PnL +8.4→+28.7 bps, Sharpe 0.03→0.09) — the only combination anywhere in
this run with a positive Sharpe. Holdout B and C are negative across every
threshold, though both narrow toward breakeven at higher thresholds
(holdout B: -23.1→-9.1 bps; holdout C: -13.7→-3.0 bps, thr 0.52→0.65).
One-of-three-positive is exactly the pattern this project's own walk-
forward policy (`docs/chunk_e_walk_forward_retraining_policy_2026-08-11.md`)
and every ML round this session have treated as noise, not a real effect —
a real edge should show up consistently, not in one arbitrary chronological
slice.

**Order-flow imbalance is a real, non-zero contributor — just not a big
enough one.** It appears in the permutation-importance top-8 for both 5min
and 60min horizons (`order_flow_imbalance_5m`/`15m`), confirming the
literature-grounded hypothesis that aggressor-side volume carries some
genuine short-horizon directional information distinct from price-only
features. But its marginal importance (0.0008–0.0027) is small next to the
dominant momentum features (`mom_1m_bps` 0.0067, `mom_1440m_bps` 0.0187),
and even with it included, no horizon clears the cost bar. This is a
different, more encouraging negative result than the prior study's (a real
new signal that measurably helps, just not enough) — but it's still a
negative result for the question Phase 0 was gating: is there a tradeable
edge here.

## What this means for the pivot plan

Per the approved plan's own explicit gate: **do not proceed to Phase 1
(execution-layer rewrite)** on any of the three timeframes. Building a
directional bot on top of a signal that loses money net of cost in 5 of 6
tested horizon/majority combinations (and is unconfirmed in the 6th) would
repeat the prior study's mistake at a much higher cost — that one never
touched live code either, and for the same reason.

This is not the same as "the pivot is dead." It's a real, now twice-
replicated finding (once with pure price/technical features, once with
price/technical + genuine order-flow) that short-horizon BTC direction —
at least from public market data alone — doesn't carry enough signal to
clear even a modest transaction cost on this specific dataset and
methodology. What would be a genuinely different next attempt, not more of
the same:

- **The full tick-trade archive is now cached and aggregated** (`state/
  binance_taker_flow_1m_btc.parquet`) — cheap to extend with more
  microstructure features (e.g. trade-size distribution, aggressor-volume
  acceleration) if there's appetite, though the marginal-importance numbers
  above suggest this is a smaller lever than a new data source, not zero.
- **The 1440min holdout-A result, while not a green light on its own, is
  the one data point in this whole session's ML work that isn't flat or
  negative when confidence-gated.** Worth a dedicated follow-up (more
  daily-horizon history, a wider holdout sample) before writing off the
  daily timeframe specifically — it's the one thread that isn't closed.
- **A genuinely different lever**: real Polymarket order-book prices,
  not a BTC-spot proxy, for however much history is actually capturable
  going forward — the proxy-PnL caveat has now bitten two studies in a row.

I'd recommend pausing the arb→directional pivot here rather than pushing
into the execution-layer rewrite on an unconfirmed signal, and deciding
explicitly whether to chase the daily-horizon thread, gather real order-
book history first, or reconsider the pivot's shape.
