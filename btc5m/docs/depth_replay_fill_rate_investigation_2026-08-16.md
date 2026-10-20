# Depth-replay clean-fill-rate investigation — 2026-08-16

## Verdict (no softening)

The published **0.27% clean-fill rate is wrong as a measure of the market**,
and it is also **not fully wrong as a measure of this strategy**.

Decomposition of the published headline (77 clean fills / 28,407
opportunities, from `state/depth_replay_validation_combined_2026-08-15.json`,
reproduced exactly by the instrumented rerun — all seven headline counters
matched, see §0):

1. **84.3% of opportunities never fire.** 23,955/28,407 opportunities are
   rejected by pre-submit risk gates (halted 18,223, max-position 5,167,
   low-liquidity 565, no-entry 5, no-fill accounting 280). They are not
   fill failures.
2. **Of the 4,452 pairs that did fire, the fill semantics themselves
   produced 258 clean two-leg fills — 5.8% of fired pairs, not 0.27%.**
   The engine only *recorded* 77 because of a replay-only defect:
   **3,405 of the 4,452 fired pairs (76.5%) were silently discarded by a
   pending-key collision** (execution.py:609,627 — see §3). Among the
   discarded pairs were **181 clean two-leg fills** and **1,913 one-leg
   pairs** (3,003 fired pairs were flagged as colliding at fire start;
   173 of the 241 full-size clean pairs and 12 of the 17 ratio-clean pairs
   carried that flag — 8 flagged pairs still slipped in because the prior
   entry resolved between the flag and the insert).
3. The remaining gap — 5.8% clean instead of ~100% — **is real**:
   94.2% of fired pairs lose at least one leg within a single book update
   (~1–3 ms) after detection. The missing leg's ask moves a median
   **+3.0 cents** (p90 **+11.0 cents**) above the decision limit in that
   one tick. Liquidity at the decision tick is NOT the constraint (median
   top-of-book size 49 shares, median depth-at-limit 137 shares vs a
   10-share order) — **price movement is**.

So: the low clean-fill rate is **both** — a genuine property of how thin
and fast this market's arb edge is (dominant, real), **and** a fixable
simulation defect that makes the headline rate ~21× lower than the fill
model actually produced (77 vs 258 clean fills; the collision also hid
1,913 of the 2,603 one-leg pairs from partial management).

Fixing the collision will NOT make the strategy profitable: it will surface
~258 clean fills and ~2,603 partials into accounting, and the partial
recovery path (protective exits) already loses money in this market (§4).
It makes the accounting truthful; it does not make the edge executable.

## 0. Method and reproduction check

New tool: `scripts/analyze_depth_fill_rate.py`. It replays the sealed
consolidated capture (SHA-256
`b615af838729b7d754bd20a72f9b7a476c694058c6aa3073610d684aba3ef98b`,
1,028,338 source rows, 445 two-token markets) through the **same**
`BacktestEngine(fill_model="depth_snapshot")` path as
`scripts/replay_captured_depth.py` (identical pairing guards, identical
config: min_edge 0.01, order_size 10, max_quote_age 500 ms,
no_entry 5 s, replay_speed 1e9), with four instrumentation hooks:
decision capture, per-leg submit capture (including the exact tick the
submit coroutine resolved against), `_resolve_one` branch capture, and
`RiskState.halt` capture.

| Counter | Published | Instrumented rerun |
| --- | ---: | ---: |
| loops | 975,259 | 975,259 |
| opportunities | 28,407 | 28,407 |
| orders_sent | 8,904 | 8,904 |
| fills (clean) | 77 | 77 |
| partial_fills | 690 | 690 |
| partial_resolved | 590 | 590 |
| halted windows | 86 | 86 |

Full artifact: `state/depth_fill_rate_investigation_2026-08-16.json`.

## 1. The 8,904 orders broken down by outcome

8,904 leg orders = 4,452 fired pairs. Distribution of the 4,452 pairs by
what the submit step actually produced (instrumented, complete — not
limited to what the engine resolved):

| Submit-level outcome | Pairs | Share of fired |
| --- | ---: | ---: |
| Both legs filled at submit | 258 | 5.8% |
| Exactly one leg filled | 2,603 | 58.5% |
| Both legs rejected at resolve tick | 1,591 | 35.7% |

(Within the 258: 241 full-size pairs + 17 ratio-scaled pairs.)

The engine's own resolution accounting only ever saw 1,047 of these pairs:
clean 77, partial 690 (yes-only 474 / no-only 216), no-fill 280. The
missing 3,405 pairs are the collision losses (§3).

**Clustering across conditions:**

- **By window offset** (seconds since window open, decision tick):
  clean fills cluster mid-window (p50 **111.0 s**, p25 73 s, p90 224 s);
  non-clean fires skew late (p50 **180.3 s**, p90 267 s). Fills are rare
  both early (first ~20 s of a window produced zero clean fills) and in the
  final ~5 s (no-entry zone). Window rollover itself does not cluster
  clean fills.
- **By window:** 279 of 445 windows fired at least once; 78 windows had at
  least one clean fill; 266 had one-leg fires. The busiest window
  (`btc-updown-5m-1786631700`) fired 103 pairs: 4 both-filled, 52 one-leg,
  64 collided. No window produced a clean-fill rate materially above the
  aggregate; clean fills are scattered, not concentrated.
- **By liquidity at decision tick:** no separation. Median min-top-of-book
  size was 72.0 for clean pairs vs 49.2 for one-leg vs 45.7 for
  both-rejected; median depth-at-limit (YES leg) was 196.0 vs 136.9 vs
  107.5 (min observed top-of-book 5 shares across all outcomes). Median
  depth is ≥5× the 10-share order. Liquidity level does
  not predict the outcome — the market moves between decision and resolve.
- **By missing leg:** symmetric (NO missing 1,419, YES missing 1,184).
- **By submit-resolve lag:** every order resolved exactly **1 tick** after
  the decision tick (n=8,904, p25=p50=p75=p90=1, max 1). The fill ladder
  is the very next book update, ~1–3 ms of market time later (median
  inter-tick spacing on this capture is ~2.7 ms). This latency is
  realistic (live paper sessions measured 4.34 ms opportunity→first-order)
  and is NOT the driver of the low rate.

## 2. Why the 690 partials + 86 halted windows didn't clean-fill

Mechanism (code path): both legs submit at the decision-tick snapshot
(execution.py:473–488). `_BacktestTradingClient.submit_order`
(backtest_engine.py:260–334) fills instantly against the ladder of the
**current** market state — which, because the submit coroutine yields once
(`await asyncio.sleep(0.001/replay_speed)`), is the **next tick's** book.
A leg is rejected (returns `None`) when that next book has no ask level ≤
the decision limit (BUY) or no bid ≥ limit (SELL exit). The surviving pair
then waits out `max_leg_gap_ms=300 ms` of simulated clock
(execution.py:791–822) before the one-leg pair is declared partial and
handed to protective-exit/hedge management.

Representative partial cases (exact instrumented values; `dYES/dNO` =
decision asks; "missing ask @ resolve" = the missing leg's best ask one
tick later; lag is always 1 tick):

| seq | window (…5m-) | off s | edge | dYES | dNO | missing | missing ask @resolve | Δ ask |
| ---: | --- | ---: | ---: | ---: | ---: | --- | ---: | ---: |
| 1 | 1786529400 | 239.3 | 0.020 | 0.17 | 0.81 | yes | 0.20 | +0.03 |
| 2 | 1786529700 | 34.9 | 0.020 | 0.46 | 0.52 | no | 0.55 | +0.03 |
| 3 | 1786530000 | 227.4 | 0.020 | 0.86 | 0.12 | no | 0.147 | +0.027 |
| 4 | 1786530300 | 133.1 | 0.030 | 0.23 | 0.74 | no | 0.81 | +0.07 |
| 5 | 1786531500 | 184.8 | 0.030 | 0.52 | 0.45 | yes | 0.56 | +0.04 |
| 6 | 1786531500 | 239.9 | 0.020 | 0.10 | 0.88 | yes | 0.34 | +0.24 |
| 7 | 1786532100 | 179.2 | 0.020 | 0.08 | 0.90 | no | 0.93 | +0.03 |
| 8 | 1786532400 | 204.0 | 0.200 | 0.40 | 0.40 | no | 0.61 | +0.21 |

Full 25-case table in the artifact (`partial_sample`). The failure mode is
uniform across all 2,603 one-leg submits: **the missing leg's ask rose
above the decision limit within one book update — price movement, not
depth exhaustion** (median +3.0¢, p90 +11.0¢, max +47¢; median depth-at-limit
at the decision tick was ≥5× order size). The filled leg filled at its
decision ask (VWAP = decision price in every case — no price improvement).

**The 86 halted windows** (100 halt events: 87 "partial exit not
confirmed", 13 "partial hedge not fully filled"; plus 81 exit-submit
rejections and 6 exit-no-bid errors): after a one-leg fill, the protective
exit SELL is submitted against the *next tick's* bid ladder; in a moving
market the bids have already dropped below the exit limit, the exit is
rejected, exposure stays, and `RiskState.halt()` freezes the window. So
halts are a downstream cascade of the same one-tick price movement — not a
separate market condition.

## 3. Market property vs simulation artifact

### 3a. Real market property (dominant)

The arb signal (`YES_ask + NO_ask < 0.99`) is transient on these
Polymarket BTC 5-minute books: within one update (~1–3 ms) one side's ask
snaps back a median 3 cents, killing the second leg. 58.5% of fired pairs
became one-leg, 35.7% lost both legs, 5.8% kept both. Liquidity is NOT
thin at the decision tick — it is **fleeting**. This matches the live
paper evidence (`docs/pnl_and_kill_switch_followup_2026-08-15.md`: 2/2
REST-book opportunities became one-leg fills with protective-exit losses)
and is consistent with the freshness gate passing while the edge still
decays.

### 3b. Simulation artifacts (fixable, and decisive for the headline number)

1. **Pending-key collision — the big one.** In replay every window shares
   the synthetic market id `backtest-market` and synthetic token ids
   `0xBACKTESTYES/NO` (backtest_engine.py:409–410), so every order id is
   the constant `bt-buy-CKTESTYES` / `bt-buy-CKTESTNO` and every fire's
   pending key `(market_id, yes_id)` (execution.py:609) is **identical**.
   The insert guard at execution.py:627 (`if pending_key not in
   self._pending`) therefore silently discards the tracking record of any
   fire that arrives while an earlier pair is still pending (~300 ms of
   simulated clock ≈ 300 ticks). Measured: **3,003 of 4,452 fired pairs
   (67.4%) collided**; with the 280 no-fill resolutions, 3,405 pairs
   (76.5%) never reached `_resolve_one`. Those pairs' fills and capital
   commits already happened (execution.py:579) but were never resolved:
   no PnL, no partial management, no metric.    This buried **181 clean fills** (77 + 181 = 258) and
   **1,913 one-leg pairs** (690 + 1,913 = 2,603).
   Live trading cannot hit this: real token ids and exchange order ids are
   unique per order, so the key never collides. It is a replay-only
   defect.
2. **One-tick resolve lag** (submit fills against the next tick's ladder).
   Small (~1–3 ms) and realistic; not the driver.
3. **Exit-path rejects**: the protective SELL exit in the backtest client
   also resolves one tick later with no resting-order persistence, so in a
   moving book it is rejected and the window halts (86/445 windows). The
   live client would rest the exit at the market and could fill on the
   next touch; the replay cannot model that. This inflates halts and
   residual losses relative to what a live protective exit would do — but
   both live paper sessions and the REST-book sample also lost money on
   protective exits, so this inflates, but does not invent, the problem.

### Corrected clean-fill rates

| Basis | Clean-fill rate |
| --- | ---: |
| Published headline (77 / 28,407 opportunities) | **0.27%** |
| Engine-resolved (77 / 4,452 fired pairs) | **1.73%** |
| Collision-free engine (258 / 4,452) | **5.79%** |
| Submit-level truth (258 / 4,452) | **5.79%** |

## 4. Clean-fill rate by edge bucket

Clean fills as a share of fired pairs, by decision-time edge:

| Edge bucket | Fired pairs | Clean | One-leg | Both rejected | Clean % |
| --- | ---: | ---: | ---: | ---: | ---: |
| 0.010–0.020 | 3 | 0 | 1 | 1 | 0.00% |
| 0.020–0.030 | 4,513 | 52 | 635 | 263 | **1.15%** |
| 0.030–0.050 | 7,098 | 76 | 789 | 465 | **1.07%** |
| 0.050–0.100 | 9,118 | 76 | 718 | 523 | **0.83%** |
| ≥0.100 | 7,675 | 37 | 460 | 339 | **0.48%** |

**Larger edges clean-fill WORSE, monotonically.** Bigger mispricings revert
faster (the missing-leg ask jump at ≥0.10 edge is the largest in the
sample set). Tightening `MIN_EDGE` would therefore *reduce* the clean-fill
rate, not improve it. The problem is structural (one-tick decay of the
second leg), not a threshold problem. (This says nothing about expected
PnL per fire — a bigger edge on the leg that does fill is worth more — but
the protective-exit losses on the missing leg have dominated so far.)

## 5. Verdict

**Both, with precise attribution.**

- As a measure of the market, the 0.27% headline is false: the fill model
  itself produced 5.8% clean fills on fired pairs; the engine lost 76.5%
  of all fired pairs to a replay-only pending-key collision before
  accounting for them.
- As a strategy conclusion ("clean two-leg fills are rare and the arb
  decays in milliseconds"), the finding survives: even with the collision
  fixed, ~94% of fired pairs lose at least one leg within one book update
  because the mispricing reverts in a single tick, and the protective-exit
  path loses money. This is real thin-and-fast market behavior, confirmed
  by independent live REST-book paper sessions.
- Fix the collision first if this replay is used again: give the backtest
  client per-fire unique order ids (e.g. a counter suffix in
  `submit_order`, backtest_engine.py:325) so `pending_key` never collides;
  optionally cap the sim-clock gap floor (backtest_engine.py:575) at a
  realistic sub-millisecond value. After the fix, expect the honest
  replay to report ~250–258 clean fills and ~2,600 partials over the same
  445 windows — a much lower headline rate than a healthy venue, but one
  that finally measures the market instead of the harness.

## Evidence

- [Machine artifact](../state/depth_fill_rate_investigation_2026-08-16.json)
- [Instrumented analysis driver](../scripts/analyze_depth_fill_rate.py)
- [Combined replay artifact](../state/depth_replay_validation_combined_2026-08-15.json)
- Code inspected: `polymarket_arb/backtest_engine.py` (fill model 260–334,
  replay clock 516–600, synthetic ids 405–417), `polymarket_arb/execution.py`
  (dual submit 466–528, pending key 609–629, partial branch 786–824, no-fill
  branch 826–856, tick-driven probe 1200–1213), `scripts/replay_captured_depth.py`
  (per-window replay 279–314, drain 262–276).
