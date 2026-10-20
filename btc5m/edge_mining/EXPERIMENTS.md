# EXPERIMENTAL EDGES REPORT — Round 2

**Date:** 2026-08-25 | Experiments E1–E9 on the same 13-day dataset (8.66M snapshots, 1,445 resolved windows)

---

## Champion edges (all fee-inclusive, Polymarket crypto taker fee 7%·p·(1−p))

| Rank | Edge | Trades/day | Net ¢/sh | Net $/day | Pos days |
|---|---|---|---|---|---|
| 1 | **D**: up-side always + dn-side on up-days, early window (τ 150–270s), θ=0.08, EWMA15m σ | 141 | +3.5¢ | **$303** | 9/13 |
| 2 | **A**: up-side only, early window, θ=0.12, EWMA15m σ | 71 | +4.2¢ | **$229** | 10/13 |
| 3 | Original E0 baseline (both sides, all τ, rv60 σ) | 188 | +1.0¢ | $121 | 8/13 |

Max drawdown: A −$207, D −$197 (≈ 1 day of mean). Sharpe/trade ~0.15–0.2.

**Both are ~2–2.5× the original edge, with half the trades.** A is simpler
(no trend filter); D adds ~$75/day using daily BTC direction as a dn-side gate.

## What the experiments taught us

### E1 — Sniping latency: DEAD
Polymarket reprices BTC 1-second moves within **1–2 seconds** (corr(r, dP): 0.22 @0s → 0.15 @1s → ~0 @2s). Zero % of our fires occur within 3s of a big move — the edge is NOT about speed or stale quotes after jumps.

### E2 — Vol model: EWMA15m wins (+20%)
| σ model | net/day @θ=0.12 |
|---|---|
| EWMA 15m | **$283** |
| GARCH(1,1) | $266 |
| rv 60m (baseline) | $237 |
| 1s-realized 15m | $230 |

The market prices with *fresh* vol; our model must adapt faster than 60m rolling.

### E3 — Early exit: DEAD as taker
Round-trip taker fees (≈3.4¢) eat every 1.5–3¢ exit gain. Hold-to-expiry is optimal
for takers. Early exit only viable as MAKER (limit exits) — needs queue simulation (phase 3).

### E4 — Book-imbalance signal: USELESS as taker
Real but tiny (+0.085 corr, ~0.15¢ gross over 5s) — far below fee+spread costs.
Could still feed a maker's quote placement; dead as a standalone taker edge.

### E5 — Where the edge lives (the big one)
| Condition | Net ¢/sh |
|---|---|
| LOW vol windows | **−0.5¢ (loser)** |
| MED vol windows | +0.8¢ |
| HIGH vol windows | **+2.4¢** |
| UP side | **+2.6¢** |
| DN side | **−0.7¢ (loser)** |
| Fire persists ≥3s | +0.0¢ |
| Fire persists ≥5s | **−0.9¢ (loser)** |

Three filters found: **vol regime, side, and fire freshness**. Persistent fires are
the market "knowing" our model is wrong; transient fires are real divergences.

### E6 — Polymarket → Binance lead: DEAD
dP leads BTC next-second return at corr 0.05, but gross ~0.00 bps — untradeable
after Binance's 4bps round trip. No cross-venue export.

### E8 — Trend dependence (why D exists)
- UP side: +2.2¢ on up-days, +5.6¢ on down-days — **robust in both trends**
- DN side: +0.9¢ on up-days, **−7.6¢ on down-days** — the poison leg
- Edge concentrates in τ 150–270s: +3.6¢ early vs −2 to −3¢ late

## Final recipe (edge A — simplest robust form)

1. σ = EWMA of 1m squared returns, half-life 15 min
2. fair_up = Φ( ln(S/S_open) / (σ√τ) ) each second per window
3. Enter ONLY when: τ ∈ (150, 270) · side = UP · fair_up − ask_up > 0.12 · FIRST fire per window
4. Size = min(top-of-book ask size, bankroll-appropriate cap)
5. Hold to expiry; never exit early as taker

At 3 shares/trade (≈$68 bankroll): ~$11/day backtested, recovers the $68 stake in ~1 week *before* the fill-realism haircut (expect 30–60% in live → real number closer to $4–7/day, ~2–3 weeks to payback).

## Caveats (unchanged + new)
1. Fill realism still #1 risk — paper-pilot before real money.
2. Up-side dominance measured during a BTC rally — A's up-side is positive on both
   day types (encouraging), D's dn-gate explicitly hedges this.
3. CA geoblock still applies — Polymarket order placement blocked; Kalshi port remains the live path.
4. All results one 13-day sample; regenerate on new captures weekly.

## Reproduction
```bash
cd ~/ccp/polymarket_arb/edge_mining
../.venv/bin/python exp1_lag.py exp2_volvariants.py exp3_earlyexit.py \
  exp4_imbalance.py exp5_regimes.py exp6_lead.py exp7_combo.py \
  exp8_trend.py exp9_final.py
```
