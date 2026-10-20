# PAPER_TRADE SDK Divergence Audit

Scope: `PAPER_TRADE=true`, `DRY_RUN=false`. This audit covers every branch in the
order-book, submission, fill-ratio, and latency path that differs from the live CLOB SDK path.
It is intentionally descriptive, not an equivalence claim.

| Surface | PAPER_TRADE behavior | Live SDK behavior | Risk / control |
| --- | --- | --- | --- |
| Approval cold path | Signs approval payloads locally with a placeholder nonce and does not fetch RPC state or broadcast. | Fetches nonce and gas, signs, then calls Polygon `eth_sendRawTransaction`. | No chain transaction can originate from paper mode. See `polymarket_arb/clients/trading_client.py:162-171`, `:253-325`. |
| Books | Uses the decision snapshot when supplied; otherwise performs SDK `get_order_book(token_id)` and reads the first ask/bid. | Does not fetch a book inside `submit_order`; it posts the supplied limit order. | The snapshot bypasses another REST read. Neither path models queue priority, depth consumption, matching delay, or an exchange rejection. See `polymarket_arb/clients/trading_client.py:407-497`. |
| Submit | `submit_paper_order` executes the simulation in `asyncio.to_thread` and returns a synthetic id only for a marketable synthetic fill. It cannot invoke `create_and_post_order`. | Constructs `OrderArgs` / `PartialCreateOrderOptions`, then synchronously invokes the SDK's `create_and_post_order`. | Paper exercises the paired-order failure, hedge, and unwind state machine without an exchange order. See `polymarket_arb/clients/trading_client.py:517-601`. |
| `get_fill_ratio` | `None` is `0.0`; every returned simulated id is treated as `1.0`. | A returned SDK acceptance id is also currently treated as `1.0`; true fill quantity is not reconciled here. | This is a shared modeling limitation, not a paper-only promise of actual fills. The controlled stress runner forces one filled/missing-leg event through the real unwind logic. See `polymarket_arb/clients/trading_client.py:499-514`. |
| Latency | The paper branch yields through `asyncio.to_thread`, so both legs dispatched by `asyncio.gather` can overlap while simulating a fill. | The SDK call is synchronous within the async coroutine; it can block the event loop and therefore does not provide equivalent wire or acknowledgement timing. | `opp2first_ms`, `dual_submit_ms`, and `paper_leg_max_ms` are instrumentation, not live-latency equivalence. See `polymarket_arb/execution.py:80-92`, `:250-260`, `:315-335` and `polymarket_arb/clients/trading_client.py:517-530`, `:563-601`. |
| Fill confirmation | A simulated id confirms immediately. | Polls `get_order` once in non-blocking mode or until the leg-gap deadline. | Paper does not model resting/partial order-status transitions. See `polymarket_arb/clients/trading_client.py:603-661`. |
| Cancellation | No-op success because no order was posted. | Cancels an explicit id, or queries open SDK orders by token before cancellation. | Paper cancellation verifies the caller's state transition only; it cannot remove exchange liquidity. See `polymarket_arb/clients/trading_client.py:663-705`. |

`build_order` is data-only and has no PAPER_TRADE branch (`polymarket_arb/clients/trading_client.py:395-403`).

The evidence runner is `scripts/run_execution_stress_session.py`. It is guarded by
`PAPER_TRADE=true`, does not instantiate `TradingClient`, and records zero live,
cross-venue, Kronos, and chain calls in its archived `evidence.json`.
