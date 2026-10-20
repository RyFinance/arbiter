# Out-of-sample validation of edge_mining's champion edges (2026-08-26)

**This is the check that was missing from both FINDINGS.md and
EXPERIMENTS.md: every filter in both reports (side, vol regime, τ window,
vol-model choice, trend gate) was discovered by exploring the SAME 13-day
sample (2026-08-12 → 2026-08-25T08:07 UTC) it was then backtested on. No
result in either report was ever tested on data that had zero influence on
choosing the filters. That's the same look-ahead-via-repeated-testing risk
this project's own walk-forward policy
(`docs/chunk_e_walk_forward_retraining_policy_2026-08-11.md`) and every ML
round this session have been built to avoid — it just hadn't been applied
here yet.

## Method

- Confirmed `out/snapshots.parquet` (used by both FINDINGS.md and
  EXPERIMENTS.md/E1-E9) ends at exactly 2026-08-25T08:07:02Z — it was never
  regenerated for Round 2's experiments, meaning **all filter discovery,
  in both reports, happened on the identical 13-day window.**
- Raw depth capture continued running past that point (confirmed:
  `state/cache/polymarket_depth_capture.2026-08-2[5-9]*.jsonl` cover
  through 2026-08-27T00:14Z). Mined ONLY the period strictly after
  2026-08-25T09:00 UTC (a 53-minute buffer past the training cutoff, to be
  safe) — genuinely untouched data (`mine_oos.py`).
- Fetched fresh Binance klines for the same window (`fetch_ground_truth.py`
  with adjusted date range).
- Re-derived the token map and both ground-truth labels (naive 1m
  close≥open, and the TWAP-corrected version from
  `verify_resolution_and_edge.py`) independently on this new period —
  no reuse of in-sample labels.
- Ran the EXACT fixed rules from both reports — **no threshold or filter
  was re-fit**: Edge A (up-only, τ∈(150,270), θ=0.12, EWMA15m σ), Edge D
  (up-always + dn-on-up-days, same τ/θ), and the E0 baseline (both sides,
  τ∈(20,270), θ=0.03).

## Result: every variant is negative out-of-sample

| Variant | In-sample net $/day | OOS net $/day | In-sample hit% | OOS hit% |
|---|---|---|---|---|
| Edge A | +$229 to +$285 | **-$48** | 41-43% | 37% |
| Edge D | +$303 to +$414 | **-$57 to -$107** | 44-45% | 36-40% |
| E0 baseline | +$77 to +$259 | **-$36 to -$75** | 45-47% | 41-43% |

Sample size caveat, stated plainly: the OOS window is only ~3 days / 200
raw windows, of which just **52 pass the same decisive-resolution quality
gate** used in-sample (200 → 52 is itself informative — most windows don't
resolve cleanly enough to grade at all). 30-82 trades per variant is not
enough to declare the edge dead with confidence either way. What it DOES
show: the specific numbers in both prior reports do not hold up on the
first genuinely fresh data checked, and every single tested variant
flipped sign. That is a real, not cosmetic, warning sign given how many
filters (5+) were chosen by looking at the training sample.

## Honest verdict

Not "the edge is fake" — the sample is too small to say that. But **not
"the edge is real and ready for a paper pilot" either**, which is what
both reports concluded. The responsible read: this needs a larger,
genuinely out-of-sample test (let capture keep running, re-check again
once there are e.g. 10+ OOS days) before trusting any of the specific
filter combinations in FINDINGS.md or EXPERIMENTS.md. The core fair-value
model concept (compare a parametric Brownian-bridge probability to the
market's quote) may still have something to it — the in-sample markout
convergence check is independent of resolution labels and did hold up —
but the SPECIFIC discovered filters (up-side-only, τ 150-270s, EWMA15m,
θ=0.12/0.08) look, on this evidence, like they were tuned to one 13-day
regime rather than a persistent inefficiency.

## Reproduce

```bash
cd edge_mining
../.venv/bin/python mine_oos.py           # mines post-2026-08-25T09:00 capture only
../.venv/bin/python oos_full_validate.py  # derives labels + runs fixed filters, no refitting
```
