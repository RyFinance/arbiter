# Independent re-verification: REST-book sign-flip and depth continuity — 2026-08-15

## Verdict

Both published findings are **confirmed** on fresh, never-before-analyzed data
by an independent re-implementation:

1. **REST-book sign-flip — confirmed, at much larger sample.**
   On 115–131 fresh decision-time opportunities, the decision-snapshot baseline
   is mechanically profitable (100% convergence by construction), while the
   rest_book execution semantics realize **negative** PnL on the same
   opportunities: expected `+$33.60`, realized `-$27.00`
   (`-80.36%` convergence) with the immediate-fill horizon, and expected
   `+$39.00`, realized `-$17.20` (`-44.10%`) with the 200 ms acknowledgement
   horizon. The published `n=2` result (`-$0.5000 / +$0.5000`, `-100.00%`)
   has the same sign, and its failure mechanism (one leg fills, the other
   leg's ask moves away, protective exit realizes the loss) is reproduced
   exactly in 99/115 events.

2. **Depth continuity — confirmed as failed.**
   On the sealed consolidated snapshot, this independent implementation
   reproduces the published pairing counts exactly (672,906 paired ticks;
   694,944 candidates; 12,800 skew-rejected; 9,238 invalid-ladder; 254
   awaiting-other-token; 0 continuity-complete windows). On fresh live
   capture (256,027 paired ticks from 184 windows written today), **1/184**
   windows satisfies the continuity criterion — the operational gate remains
   failed by a margin of 29+ windows.

## Method

A new self-contained script, `scripts/verify_rest_book_sign_flip.py`,
re-implements both analyses from scratch (no imports from
`replay_captured_depth.py`), then:

1. validates the implementation against the sealed consolidated snapshot
   (SHA-256 `410bfb9c3e83a391b63d073a93fc604a1df6876aaf7b446671bcc236fbc7d6a7`
   in `state/cache/polymarket_depth_consolidated.jsonl`) by requiring the
   published pairing and continuity counts to reproduce; and
2. applies the same analysis to fresh snapshot copies of the live capture
   shards written today:
   - `state/cache/polymarket_depth_capture.2026-08-15.1.jsonl`
     (SHA-256 `36d922c9e4471a91…` of the snapshot copy), and
   - `state/cache/polymarket_depth_capture.2026-08-16.jsonl`
     (SHA-256 `40251646bbf9a089…` of the snapshot copy).

Replay semantics mirror the production paper path
(`trading_client._paper_simulate_fill` with `PAPER_FILL_SOURCE=rest_book`)
and the published accounting (`calc_pnl_convergence.compute_pnl_convergence`):

- detection: `combined ask < 1.0 - 0.01`, pair skew ≤ 500 ms (which subsumes
  the live 500 ms freshness gate because the older leg is at most the pair
  skew old), no entry inside the final 5 s before window close, per-market
  debounce on the exact combined price, one open position per market until
  resolution;
- leg fill: a BUY fills at the submission-time best ask iff that ask is ≤ the
  decision ask. Two horizons are reported: the next captured observation
  after the decision (`immediate`) and the next observation after
  decision + 200 ms (`ack_200ms`), bracketing the ~210 ms average
  acknowledgement recorded in the original session;
- one-leg fill: protective exit at the next captured bid for the filled token
  (first observation ≥ fill + 100 ms);
- accounting: `expected = (1 - decision_combined) * 10` over committed
  opportunities; `realized = clean two-leg PnL + protective-exit PnL`, via
  `compute_pnl_convergence` (the same calculator used for the original
  finding).

During development, one token-orientation bug was found and fixed in this
script (the fill lookup must use the pairing's first-observed token order,
not a sorted token-ID order). The numbers below are from the corrected run.

## Implementation validation on the sealed input

| Measure | Published | This implementation |
| --- | ---: | ---: |
| Pair candidates | 694,944 | 694,944 |
| Complete paired ticks | 672,906 | 672,906 |
| Skew rejected | 12,800 | 12,800 |
| Missing or invalid ask ladder | 9,238 | 9,238 |
| Awaiting other token at end | 254 | 254 |
| Windows with both tokens | 249 | 249 |
| Continuity-complete windows | 0 | 0 |

Artifact:
`state/rest_book_verification_implementation_check_2026-08-15.json`.

## Fresh-data results

### Depth continuity

| Measure | Sealed snapshot | Fresh capture (today) |
| --- | ---: | ---: |
| Windows with both tokens | 249 | 184 |
| Continuity-complete windows | 0 | 1 |
| Gap to the 30-window gate | 30 | 29 |
| Paired ticks | 672,906 | 256,027 |

Inter-snapshot spacing on the fresh shards: median of window medians
`2.69 ms`, p90 `4.24 ms` — the capture is high-frequency enough that the
"next observation" fill proxy is meaningful. **Agreement:** the continuity
criterion fails on both the sealed and the fresh data (0/249 and 1/184,
against a requirement of 30 complete windows).

### REST-book sign-flip

| Horizon | Opps | Clean | One-leg | No-fill | Expected | Realized | Convergence | Deviation |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| immediate | 115 | 0 | 99 | 16 | `+$33.60` | `-$27.00` | `-80.36%` | `-180.36%` |
| ack_200ms | 131 | 0 | 94 | 37 | `+$39.00` | `-$17.20` | `-44.10%` | `-144.10%` |
| decision_snapshot baseline | 115 | 115 | 0 | 0 | `+$33.60` | `+$33.60` | `100.00%` | `0.00%` |

`decision_snapshot` is shown to confirm the mechanical-baseline contrast
(the published finding's first claim). Fill-observation latency on the fresh
data (immediate horizon): p50 `132.3 ms`, p90 `387.5 ms`, max `709.6 ms`;
exit-observation latency p50 `271.8 ms`. Zero fills used the sparse
fallback, so every fill is a real captured post-decision book observation.

Example event (immediate horizon, exactly the published failure mode):

| Field | Value |
| --- | --- |
| Decision | YES 0.05 / NO 0.92, combined 0.97, expected `+$0.30` |
| YES leg | filled at 0.05 after 166.9 ms |
| NO leg | next ask above 0.92 after 29.4 ms → no fill |
| Protective exit | YES sold at bid 0.03 (331.6 ms later) |
| Realized | `-$0.20` |

This is the same sequence as the published
`docs/pnl_and_kill_switch_followup_2026-08-15.md` events (YES bought, NO ask
moved away, YES exited worse), confirming the sign-flip is systematic on
today's data, not a two-event fluke.

## Disagreement note

The published sample reported convergence `-100.00%` on `n=2`. The fresh
sample does not reproduce the exact magnitude (`-80.36%` / `-44.10%`) — the
magnitude depends on horizon and on how fast the losing leg's exit bid moves.
The **sign** of the effect (positive decision-time expectation realized
negative under rest_book execution) is reproduced in every variant.
No variant produced a positive realized PnL.

## Evidence

- [Machine artifact, fresh run](../state/rest_book_verification_fresh_2026-08-15.json)
- [Machine artifact, implementation check](../state/rest_book_verification_implementation_check_2026-08-15.json)
- [Verification script](../scripts/verify_rest_book_sign_flip.py)
- Original findings: [depth replay](depth_replay_2026-08-15.md) and
  [PnL follow-up](pnl_and_kill_switch_followup_2026-08-15.md)

No live order path, signing path, cross-venue component, or model artifact
was used. This analysis is offline replay of captured real data plus the
published accounting formulas.
