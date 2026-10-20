# polyuni — universe-wide Polymarket arb bot

Scans eligible Polymarket binary markets (including YES/NO and Up/Down), filters to
books with tradeable liquidity/spread/resolution clarity, and runs a paper-only
sequential pair strategy by default.

Forked from [btc5m](../btc5m), generalizing its BTC-only window
rotation into N-concurrent-market tracking, splitting its execution god-object into
focused collaborators, and rebuilding the dashboard as one fast, real-time (WebSocket)
UI instead of three separate poll-based ones.

## Safety status

Real order submission does not exist in this codebase, full stop — not gated, not
feature-flagged, simply not implemented. `PAPER_TRADE` uses real market data and
simulated fills. LIVE mode is a real, audited operator-intent state machine
(`live_control.py`) with an unambiguous dashboard indicator, but arming it changes
what is displayed and logged, never whether an order can reach an exchange. See
`polyuni/live_control.py`'s module docstring.

## Status

All 6 planned phases are complete and validated against live Polymarket data (not
just unit tests).

- **Universe discovery** (`market_universe.py`, `clients/gamma_client.py`): scans the
  full active Polymarket market set (2000+ markets in practice), filters by
  liquidity/volume/spread/resolution-clarity/neg-risk, and promotes the top
  `MAX_CONCURRENT_MARKETS` by volume to active tracking. Because Gamma's bulk
  pagination omits rotating crypto windows, current+next BTC 5m/15m slugs are also
  fetched explicitly and prioritized while sequential mode is active. Lifecycle:
  discovered → qualified → active → closing → released.
- **Execution engine** (`execution/`): split into `order_router.py` (submission/
  cancellation), `fill_tracker.py` (deferred fill resolution), `partial_fill_recovery.py`
  (hedge-or-exit), `pnl_ledger.py` (accounting), coordinated by a thin `execution.py`.
  Verified behaviorally equivalent to the original monolithic module by cross-checking
  every risk/metrics call site, plus the full capital-accounting regression suite.
- **Sequential strategy** (`sequential_engine.py`): watches raw one-side book updates,
  buys one side near 50/50 only during the configured early window and after measured
  local volatility, then seeks the opposite side later. The second fill must preserve
  a positive net edge after actual depth-walked paper fills and Polymarket's taker-fee
  curve. A wall-clock timeout forces a protective exit even if no further tick arrives.
  Set `STRATEGY_MODE=dual` to retain the legacy simultaneous detector.
- **LightGBM regime input** (`regime_signal.py`): reads the sibling
  `polymarket_arb/state/regime_active.json` signal with strict freshness checks. It
  modifies only the BTC Up/Down first-entry volatility threshold; missing/stale data
  or confidence below `POLYUNI_LIGHTGBM_MIN_CONFIDENCE` falls back safely, and an
  open hedge/exit is never blocked by the model.
- **PnL** (`execution/pnl_ledger.py`): realized (locked/residual/session), forgone
  edge (would-lock), per-market breakdown, capital currently at risk. Published as an
  atomic JSON snapshot every `PNL_PUBLISH_INTERVAL_S` (default 5s), not just at
  shutdown.
- **Dashboard** (`dashboard/`): one FastAPI app, one page. LIVE/PAPER banner sourced
  directly from `live_control.py` state, start/stop with a real
  STOPPED/STARTING/RUNNING/STOPPING state machine (not just "process alive"), PnL
  panel, live market universe table, and sequential phase/exposure/rejection diagnostics.
  WebSocket push (`/ws`) is the primary channel, with REST endpoints including
  `/api/strategy` as the reconnect fallback.

### Validation evidence

- Live discovery run: 2070 markets discovered, 73 qualified, 50 promoted to ACTIVE
  under the capacity cap.
- Live end-to-end trading-loop run: 50 markets WS-subscribed, 100/100 pair-coalesced
  correctly, edge gate correctly evaluated (and correctly found no arbitrage in that
  window — the honest answer, not a bug).
- Live dashboard run (real browser, real WebSocket client, real bot subprocess):
  start → STARTING → RUNNING transition observed both via the API and visually;
  universe/PnL data streamed live over the socket; stop returned in <0.03s
  (non-blocking) with a clean process exit.
- **Multi-hour stability run**: the bot ran continuously for 2+ hours against live
  Polymarket data with **zero ERROR/CRITICAL log lines**, stable memory (~67MB RSS),
  the WebSocket feed actually disconnected and recovered twice on its own (real
  network conditions, not simulated), and the universe lifecycle correctly promoted
  and demoted markets in response to real volume changes over the run. A dashboard
  stop issued mid-run (after 2+ hours) returned in ~0.03s and exited cleanly.

### Full test suite

The test suite (`.venv/bin/pytest -q`) covers config, models, live_control,
order_ledger, alerting, market_universe, risk, trading_client, capital accounting,
execution split, official fee math, the sequential state machine, LightGBM fallback,
PnL ledger, dashboard server, botctl, and the WebSocket push layer.

## Install

```bash
cd polyuni
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.example .env  # then edit — never commit a populated .env
```

## Run the bot

```bash
.venv/bin/python -m polyuni.main
```

Requires `DRY_RUN=false`, `PAPER_TRADE=true`, `LIVE_SIGNING_VERIFIED=true`, and a
valid `PRIVATE_KEY` in `.env` for authenticated (but still simulated-fill) paper
trading. `DRY_RUN=true` (the default) needs no key at all.

## Run the dashboard

```bash
.venv/bin/uvicorn dashboard.server:app --port 8811
```

Open `http://127.0.0.1:8811`. Start/stop the bot from the banner; it launches
`python -m polyuni.main` as a managed subprocess (paper-only — see Safety status).

## Tests

```bash
.venv/bin/pytest -q
```
