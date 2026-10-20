# Polymarket Arb — paper-trading product

Polymarket Arb watches the real Polymarket BTC five-minute YES/NO order books and
detects moments when buying both outcomes costs less than their eventual $1 payout.
It currently runs as a research and paper-trading product: prices and books are real,
but fills are simulated and no exchange order is posted.

## Safety status

Real order submission is intentionally unavailable in this build.

- `PAPER_TRADE` uses real market data and simulated fills.
- The dashboard has an audited master enable/disable flag, but a separate code-level
  block keeps effective live submission off even when the operator flag is armed.
- Every production order-submission boundary checks and logs that control state.
- Wallet and funding checks are read-only. Only `eth_getBalance` and `eth_call` are
  permitted; the checker has no transaction method.
- The current CA/BC jurisdiction result is blocked. The product does not bypass it.

The authoritative current evidence is generated from the same gate rows used by the
dashboard in [the current product status](docs/product_status_2026-08-15.md) and
[the historical checklist pointer](docs/go_live_checklist_2026-08-11.md). Older engineering notes
were preserved in
[the legacy README](docs/legacy_readme_pre_product_2026-08-15.md), but they are not a
current operating guide.

## Install

Python 3.11 or newer is recommended.

```bash
cd /path/to/polymarket_arb
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

## Configure with the wizard

The wizard supports only `dry-run` and `paper` profiles. It validates URLs, numeric
risk settings, dependencies, credential shape, and safe execution-mode flags before
writing anything. Secrets are entered with hidden prompts, and the generated `.env`
has mode `0600`.

For a first local run with no wallet:

```bash
.venv/bin/python scripts/setup_wizard.py --profile dry-run
```

For authenticated paper trading against real market data:

```bash
.venv/bin/python scripts/setup_wizard.py --profile paper
```

Validate an existing configuration without changing it:

```bash
.venv/bin/python scripts/setup_wizard.py --check \
  --report-output state/setup_wizard_verification_2026-08-15.json
```

Add real, read-only wallet and jurisdiction queries:

```bash
.venv/bin/python scripts/setup_wizard.py --check --online
```

An online check exits non-zero when funding, approvals, or jurisdiction gates fail.
That is expected and useful: it does not fund a wallet, set an approval, or weaken a
gate. Bot startup repeats the safety-critical mode, URL, credential-completeness,
numeric, and capital-cap checks and refuses an invalid configuration with a pointer
back to the wizard. The online report tests the configured Polygon RPC separately;
if a public read-only fallback is used, that is reported rather than silently masking
a dead configured endpoint.

## Run the bot

Dry-run or paper mode is selected by `.env`:

```bash
.venv/bin/python -m polymarket_arb.main
```

For a sealed, time-bounded paper evidence run using fresh REST books for fill
simulation:

```bash
.venv/bin/python scripts/run_task_2_4_paper_session.py \
  --duration-minutes 15 \
  --paper-fill-source rest_book
```

The harness refuses durations below 15 minutes. Each completed run is archived under
`state/paper_session_archives/` with a `SHA256SUMS` seal, session report, bot log,
alerts, and PnL-convergence report.

Stop the normal bot with `Ctrl-C`. It writes operational state under `state/`.

## Run the dashboard

```bash
.venv/bin/streamlit run streamlit_app.py
```

The dashboard selects the current `America/Vancouver` evidence date. For an
intentional historical review, set `PRODUCT_EVIDENCE_DATE=YYYY-MM-DD`; missing
current-day evidence fails closed rather than silently borrowing an older pass.

The dashboard includes:

- current bot, kill-switch, latency, PnL, model, depth, and test status;
- the master operator flag, effective hard-block state, and append-only audit log;
- exact read-only signer/funding-wallet balances, separate funding/approval/trading
  readiness, and jurisdiction;
- a live pre-flight table with evidence paths for every gate;
- a refresh button that performs only read-only wallet/jurisdiction calls; and
- demo mode, which replays a hash-verified historical paper session without a live
  connection.

To present offline, open the dashboard sidebar, enable **Demo mode**, choose a sealed
session, and move the replay-position slider. The event tape and metrics are generated
from the archived real paper log; archives with a bad or missing hash seal are rejected.

## How the strategy works

For a binary market, one outcome settles at `$1` and the other at `$0`. A matched pair
therefore has a fixed gross payout of `$1`:

```text
combined cost = YES ask + NO ask
gross edge    = 1.00 - combined cost
```

Adjacent paired YES/NO WebSocket updates pass through a 10 ms pair barrier: the
detector receives an update only after both distinct token books arrive, while a
one-sided timeout is suppressed. This prevents a fresh leg from being combined with
the prior frame's opposite leg. The detector fires only when the resulting combined
cost clears `MIN_EDGE`, quote-freshness, liquidity, macro-risk, position, and capital
gates. REST paper mode then fetches both
books concurrently and revalidates full-size executable depth against the unchanged
edge threshold before reserving capital. The execution layer submits the two simulated
legs concurrently and walks visible depth to a full-size VWAP; insufficient depth is
not promoted to a full fill. If only one leg fills after revalidation, it cancels the
other and either hedges or crosses the current REST bid to exit. The model deliberately
retains movement between detection, preflight, and submission.

## Tests

Run the full regression suite:

```bash
.venv/bin/pytest -q
```

Focused product tests cover the live-control hard block, audit persistence, read-only
RPC allowlist, setup wizard, sealed demo replay, execution accounting, and dashboard
status. Current dated test evidence and the exact stable count are linked from the
go-live checklist rather than hard-coded here.

## Current capabilities and limitations

Verified capabilities:

- real Polymarket CLOB WebSocket and REST market-data ingestion;
- five-minute window rotation and singleton-supervised depth capture;
- dry-run and authenticated paper execution with exact per-fill audit data;
- risk reservations, partial-fill recovery, kill-switch alerting, and sealed sessions;
- read-only wallet/funding/jurisdiction status; and
- historical dashboard demo replay with seal verification.

Hard limitations:

- no live exchange-order endpoint exists;
- the current jurisdiction check is blocked in CA/BC and is never bypassed;
- balances and approvals are display-only; there is no funding transaction flow;
- paper fills are simulations, not exchange confirmations;
- raw one-leg WebSocket dislocations are not treated as executable opportunities: the
  pair barrier and fresh REST depth preflight may correctly reduce a session to zero
  orders when no coherent full-size pair exists;
- clean-arbitrage convergence and strict depth gates must remain evidence-backed before
  any future live-order implementation is considered; and
- the offline BTC/LightGBM work is not deployable and does not control execution.

Do not treat the dashboard flag as authorization to trade real money. In this release,
arming it records operator intent only; effective live submission remains off.
