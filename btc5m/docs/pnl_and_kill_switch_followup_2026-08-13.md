# PnL convergence and kill-switch follow-up — 2026-08-13

## Recalculation of the 2026-08-11 15-minute session

The archived dashboard's `$2.5000 expected_pnl` was **raw detected edge**, not
the expected PnL for the seven submitted pairs. Its formula was
`locked_pnl + would_lock = $1.8000 + $0.7000`. The `$0.7000` is from three
`stale_quote` rejections and was never submitted, filled, or exposed to price
movement.

The convergence denominator is now `expected_committed_pnl`: the sum of the
decision-snapshot edge on submitted pairs, plus any explicitly submitted
`no_fill_in_window` pair. It excludes pre-submit rejections while retaining
them in `expected_raw_pnl` for opportunity-capture accounting.

| Filled at | Decision/fill combined | Expected edge, 10 shares | Realized locked PnL | Difference |
| --- | ---: | ---: | ---: | ---: |
| 08:43:19.848 | 0.9800 | $0.2000 | $0.2000 | $0.0000 |
| 08:44:19.738 | 0.9600 | $0.4000 | $0.4000 | $0.0000 |
| 08:48:53.371 | 0.9700 | $0.3000 | $0.3000 | $0.0000 |
| 08:49:07.468 | 0.9800 | $0.2000 | $0.2000 | $0.0000 |
| 08:49:39.040 | 0.9700 | $0.3000 | $0.3000 | $0.0000 |
| 08:49:52.226 | 0.9800 | $0.2000 | $0.2000 | $0.0000 |
| 08:52:45.379 | 0.9800 | $0.2000 | $0.2000 | $0.0000 |

Totals: expected committed PnL `$1.8000`; realized locked PnL `$1.8000`;
convergence `100.00%`; deviation `0.00%`.

This paper client deliberately fills at the decision snapshot (`source=decision_snapshot`), so it cannot measure real fill-time price movement or market-impact slippage. The sample contains seven pairs and is therefore too small to establish live-fill convergence. It does establish that the reported `-28.00%` was a denominator error, not observed paper slippage.

## Audit-path repairs

- Candle fallback now raises `CandleFallbackUnavailable` instead of inventing
  `no_ask = 1 - close`; no fallback result can be reported as a comparable
  outcome-book backtest.
- `Execution.execute_dual_buy` has a live rotation/release path where a
  template can be absent. It records `template_unavailable` in metrics and a
  structured missed TRADE event. The 2026-08-11 zero-order archive contained
  zero such events; its cause was the separately repaired macro snapshot bug.
