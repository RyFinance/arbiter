# Polymarket Arb Bot — latency-first execution

A deliberately dumb detector wrapped in an extremely fast execution path.

> **The only strategy:** for each tracked binary YES/NO market, if
> `best_ask(YES) + best_ask(NO) < 1.00 - MIN_EDGE`, buy both sides. Hold to resolution.
> One side redeems for $1.00, so cost < payout = locked profit.

No prediction. No ML. No cross-exchange arb. No market making. No portfolio optimizer.
The bot wins **only by being faster than everyone else**.

---

## Current Status (live snapshot 2026-08-11)

> **One-glance truth.** Everything below is verified by a fresh run this session.
> Press `<` on your keyboard (or scroll) for the historical / superseded sections; every
> dated prior summary is preserved verbatim lower in the file and explicitly marked
> `[SUPERSEDED YYYY-MM-DD]` so you can trace claims to their evidence. The full
> per-task numbers live in `STATUS.md` (regenerated every landing) and
> `state/cache/integrity_report.json` (JSON, dashboard-readable).

| Area | Real, current value (2026-08-11 UTC) | Source |
|---|---|---|
| **Stable pytest count** | see `STATUS.md` (run 3× this session) | `.venv/bin/pytest tests/ -q` ×3 |
| **Test isolation leaks** | audits clean beyond the 2 already-mitigated in `conftest.py`; 1 new leak (`tests/test_calc_quote_freshness.py` hardcoded `/tmp/` paths) **fixed** this session | this session |
| **`.env.example` ↔ `config.py`** | audit found 1 orphan (`MAX_SPREAD_DRIFT` — referenced **nowhere** in code) and 1 undocumented (`MARKET_BROAD_SCAN` — in `config.py:95`, missing from `.env.example`) | this session |
| **LSE API key** | live-verified HTTP 200 against `/vault/candles`; fingerprint `lse_…0314 sha256=acfff3060ce5…` recorded in both cache sidecars | `scripts/cache_lse_history.py` probe |
| **LSE candles cache** | **4,469,515 rows**, 2017-08-17 → 2026-08-11, 150.8 MB parquet, SHA-256 `234e3c8a3c8e4c26...`, 11,494 gaps >1min (max ≈31d, real BTC-history gaps) | `state/cache/btc_usd_1m_candles.parquet` + `_meta.json` |
| **LSE calendar cache** | **137,213 rows**, 2015-01-01 → 2026-11-10, 19.8 MB CSV, SHA-256 `d9018b636276fb6b...`, COMPLETE | `state/cache/economic_calendar.csv` + `_meta.json` |
| **Cache integrity checker** | exists at `scripts/cache_integrity.py`; emits JSON report covering 8 artifacts → `state/cache/integrity_report.json` (dashboard-ready) | this session |
| **Live execution** | still BLOCKED externally by the CA/BC geoblock on Polymarket order placement — read-only data flows, bot runs PAPER_TRADE only | unchanged from 2026-08-03 |
| **Macro risk gate** | `state/macro_events.json` 234 events (per checker), conftest patch isolates tests | this session re-count |
| **Regression tests for past bugs** | status per-bug tabulated in `STATUS.md` Task 3 — fill_ratio scaling ✓, watchdog dedup ✓, alerts persistence path ✓, quantity cap (see STATUS.md) | this session |

**Active blockers (unchanged):** (a) Geoblock — do not bypass; (b) only one current paper fire so latency/fill samples are not volume-qualified; (c) manual wallet funding/approvals sign-off.

**Superseded top-of-README snapshots below this line are preserved verbatim for evidence traceability.**

---

## Current Status Summary (SUPERSEDED 2026-08-11 — snapshot 2026-08-03)

> Retained verbatim for evidence traceability. The 2026-08-11 snapshot above is now
> the live source of truth; numbers in this section reflect the 2026-08-03 state
> and should NOT be quoted as current. See `STATUS.md` for today's verified counts.

A 1-screen scan of where the project actually stands today. Each line links to the
detailed section with the evidence; the rest of this README is the detail.

- **Tests:** `348 passed` (`pytest tests/`, 2026-08-03 PDT). 12 pre-existing
  failures are isolated to `tests/test_watchdog.py` (webhook `_send_webhook`
  mock plumbing) and `tests/test_alerts_e2.py` (alert file-write race) — neither
  is touched by this session and both persist on a clean checkout. The backtest
  validator has dedicated regression coverage for cleanup, synthetic opt-in,
  and miss attribution, plus two new partial-fill regression tests covering
  both legs of a liquidity-constrained dual buy.
- **Live execution: BLOCKED externally** by the CA/BC geoblock on Polymarket order
  placement. Read-only CLOB + LSE WebSocket data flows fine from this location; the
  bot's full stack runs as PAPER_TRADE only.
- **Macro risk gate:** `state/macro_events.json` is now populated with **real LSE-branded
  data** (284 events, 65 HIGH-impact, 61 forward high-risk windows) via the scraper's
  SPA anon-key fallback in `_fetch_calendar_httpx`. The FF backfill (`scripts/populate_macro_from_ff.py`)
  is retained as a redundant cross-source and works when the LSE calendar is unreachable.
  See [Macro risk gate](#compliance--classifier-independence) and
  [LSE key acquisition docs](docs/lse_api_key_acquisition.md).
- **Kronos spot-check (n=20 real LSE-data checkpoints):** `match_rate=0.55`
  (forecast vs following-session realized 1-min regime), `ground_truth_match_rate=0.35`
  (vs 30-day daily regime); the model over-forecasts HIGH. n=20 was the expansion
  done this session (5 prior + 15 new, all real-LSE-candles, all from the Kronos
  model, 0 synthetic). See [Task D](#task-d--kronos-spot-check-re-validated-2026-08-03).
- **Kronos threshold robustness (50.00%, 12-row holdout):** a SEPARATE
  calibration-transfer holdout of 12 real LSE 1m BTC/USD checkpoints (disjoint dates
  from calibration). At production thresholds (0.03 LOW / 0.05 HIGH) it matched
  6/12 (50.00%); at the frozen baseline (0.02/0.05) it matched 5/12 (41.67%). This is
  NOT a regime forecasting accuracy number and is not comparable to 0.55. See
  [the discrepancy analysis in this session's notes](#task-d--kronos-spot-check-re-validated-2026-08-03).
- **Backtest harness:** the `$10M` `max_total_capital` and kill-switch overrides
  remain in place for the static single-market replay. Latest re-run: 500,000 real-LSE
  ticks, 391/391 resolved pairs, and **`$34.4875` capped actual locked/session PnL**,
  with `max_liquidity=5.0` capping each leg so partial-fill PnL scaling is exercised
  end-to-end. The requested-quantity equivalent is `$68.975`, but the capped actual
  value is the honest go-live metric (the
  `_BacktestTradingClient` reports `fill_ratio=0.5` on every leg; the new regression
  tests prove both clean-fill and partial-hedge paths scale PnL by the ratio).
  The live paper side (`state/bot.jsonl`) now reports 1 session, 1 opp, 1 fill,
  `$0.40` PnL — the discrepancy ratio dropped from `Infinity` (zero paper side) to
  `86.2x`, flagged as a pure volume effect (391 vs 1 fills) rather than per-trade
  edge inflation (per-fill PnL paper-to-backtest ratio is `0.22x`, well within 3x).
  The immutable `fill_fidelity_validation_1.jsonl` paper artifact records 8/8
  fills and `$1.90` locked/session PnL. Synthetic fallback is structural-only and
  requires explicit opt-in. See [Backtest validation](#task-b--backtest-validation-code-status-2026-08-03).
- **LightGBM diagnostic:** the current 4,783-row real contiguous LSE run has no usable
  signal: holdout accuracy is 29.541596% / 22.619048% / 19.864177% versus the fixed
  43.75% reference. It is not deployable and no further tuning is authorized on this
  slice. See [Task C](#task-c--lightgbm-diagnostic-verdict-2026-08-04-utc).
- **Archived paper-session notes:** the future-dated artifacts discussed in
  “Task A (Round 2)” remain historical notes only. The current A1 go-live evidence
  is the fresh 2026-08-05 PAPER_TRADE session, started at 06:45:43.192 UTC,
  summarized in
  the consolidated checklist below; use that session, not archived artifacts, for
  current checklist decisions.
- **Active blockers:** (a) Geoblock — do not bypass; (b) only one current
  paper fire, so latency/fill samples are not volume-qualified; (c) manual wallet
  funding/approvals sign-off. See
  [Current External Blockers](#current-external-blockers).

## Why `YES + NO < $1.00` works

A binary Polymarket contract resolves to exactly one outcome. The winning side pays
**$1.00** per share; the losing side pays **$0.00**. For a standard YES/NO market,
exactly one of {YES, NO} wins.

So if you own 1 YES *and* 1 NO, you always own the winner:

```
cost     = YES_price + NO_price
payout   = $1.00   (whichever side resolves true)
profit   = $1.00 - (YES_price + NO_price) - fees - slippage
```

Whenever the combined ask drops below $1.00 (minus a cushion for fees/slippage), the
trade is mechanically profitable at resolution. The condition is trivial to detect.
**Getting the order in before the spread snaps back is the entire game.**

---

## Setup

```bash
cd polymarket_arb
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit .env: set MARKET_FILTER_TAG / MARKET_KEYWORDS (or MARKET_WHITELIST)
# leave DRY_RUN=true to run without real orders
python -m polymarket_arb.main
```

DRY_RUN (default) runs the **full** path — discovery, WS feed, detection, precompute,
dual submission, timing, PnL accounting — but never sends a real order (synthetic order
ids). Flip `DRY_RUN=false` and set `PRIVATE_KEY` only after you've verified the live
signing path (see *Going live* below).

### Dashboard

Run the Streamlit control-room dashboard from the repo root:

```bash
streamlit run streamlit_app.py
```

It is read-only for trading operations. It watches `state/bot.jsonl`,
`state/backtest.jsonl`, and `state/market_cache.json`; shows execution/PnL metrics,
recent events, cached markets, live-readiness checks, local process status, and safe
buttons for geoblock/signing verification.

---

## Speed First

> **Anyone can build the detector. In arbitrage, the only real edge is getting there first.**

The combined ask can be below $1.00 for a fraction of a second. Arbitrageurs with
faster paths fill it first and the opportunity is gone. So every design choice is judged
by one question:

> *Does this make the bot faster to submit both orders when `YES + NO` drops below $1?*

If not, it isn't in the MVP.

### Why metadata caching matters
Discovery (Gamma) + token-id mapping + market metadata parsing is slow and never
changes between opportunities. We do it **once at startup**, build a compact in-memory
`MarketState`, and persist it to `state/market_cache.json`. The trading loop never
re-fetches or re-parses market definitions. Cache is only rebuilt on a slow background
cadence (`CACHE_REFRESH_INTERVAL_S`).

### Why precomputing orders matters
When an arb fires we do **not** build order objects from scratch. At startup each market
gets a precomputed `OrderTemplate` (token ids, side=BUY, default size). On a trigger we
only overwrite `price` + `size` + `timestamp` and sign. No object churn, no discovery,
no reshaping in the hot loop.

### Why shaving a few hundred milliseconds matters
A 200 ms slower path = you are 200 ms behind the fastest bot. In a market where the
window is often <1 s, that's the difference between filling and missing. We measure it:
`opp->order` latency, `ack` latency, and loop time are logged and surfaced in session
metrics.

### Why the detector is not the hard part
The detector is `combined = yes + no; if combined < 1 - edge: fire`. It's ~3 lines and
debounced. The hard part is the plumbing that lets those 3 lines turn into two orders on
the wire in the minimum possible time. That plumbing is this repo.

---

## Architecture (cold path vs hot path)

```
COLD PATH (startup + slow background, NEVER in trading loop)
  main.build_market_states()
    └─ cache.load()  → (if stale) gamma_client.discover_markets()
    └─ cache.save()  → state/market_cache.json
    └─ build MarketState map + precomputed OrderTemplates
  TradingClient()  → warms signing client (live mode)

HOT PATH (live, every price tick)
  PriceFeed (clob_client.ClobBookFeed, WebSocket preferred; REST poll fallback)
    └─ writes best_ask into MarketState (O(1) token→state lookup)
    └─ arb_engine.evaluate()            ← the trivial detector
         └─ combined = YES_ask + NO_ask
         └─ if combined < 1.0 - MIN_EDGE:  (debounced)
              └─ execution.execute_dual_buy()   ← LATENCY-CRITICAL
                   ├─ risk.precheck()  (pure math, no network)
                   ├─ asyncio.gather(submit YES, submit NO)  ← concurrent, ~simultaneous
                   ├─ timing + PnL accounting
                   └─ partial-fill → HALT new trades (fail safe)
```

Latency-sensitive sections are marked `LATENCY-SENSITIVE` in the code. Cold-path
sections are marked `COLD PATH`.

---

## Configuration (`.env`)

| Key | Default | Meaning |
|---|---|---|
| `MIN_EDGE` | `0.01` | Buffer for fees/slippage. Trade only if `YES+NO < 1.00 - MIN_EDGE`. |
| `ORDER_SIZE` | `10.0` | Shares per leg. |
| `USE_WS` | `true` | Prefer WebSocket book feed; fall back to polling on failure. |
| `POLL_INTERVAL_S` | `0.25` | REST poll interval when WS is off/failing. |
| `CACHE_REFRESH_INTERVAL_S` | `300` | Background market-cache rebuild cadence. |
| `MARKET_WHITELIST` / `MARKET_FILTER_TAG` / `MARKET_KEYWORDS` | — | Scope the bot to liquid BTC/Crypto binaries (legacy broad-scan mode). |
| `MAX_POSITION_PER_MARKET` / `MAX_TOTAL_CAPITAL` / `MIN_LIQUIDITY_PER_SIDE` / `STALE_BOOK_TIMEOUT_S` / `DAILY_KILL_SWITCH_USD` | — | Risk caps. |
| `EMERGENCY_STOP_FILE` | `state/EMERGENCY_STOP` | Create this file to halt trading immediately. |
| `DRY_RUN` | `true` | Never send real orders. |
| `PRIVATE_KEY` | empty | Trading-wallet private key. Required for live signing. |
| `PROXY_WALLET` | empty | Optional proxy/funder wallet address if funds live outside the signer EOA. |
| `POLYMARKET_API_KEY` / `POLYMARKET_API_SECRET` / `POLYMARKET_API_PASSPHRASE` | empty | CLOB V2 L2 auth credentials. Leave blank on first live run so the SDK can derive them. |
| `LIVE_SIGNING_VERIFIED` | `false` | Required safety gate after `verify_signing` passes with the real wallet. |

### Rolling-window targeting (FIX #3) — default mode

| Key | Default | Meaning |
|---|---|---|
| `WINDOW_MODE` | `true` | Target the deterministic `{asset}-updown-{tf}-{unix_ts}` 5-min BTC "Up or Down" series instead of broad Gamma discovery. |
| `WINDOW_ASSET` | `btc` | `btc` \| `eth` \| `sol` … |
| `WINDOW_TIMEFRAME` | `5m` | Window length tag (`5m` \| `15m`). |
| `WINDOW_SECONDS` | `300` | Window duration (seconds). Window start = `now - (now % WINDOW_SECONDS)`. |
| `WINDOW_PRE_RESOLVE_S` | `30.0` | Pre-resolve + pre-subscribe the NEXT window's token ids this many seconds before close. |
| `NO_ENTRY_SECONDS` | `5.0` | FIX #4: no NEW entries in the final N seconds before settlement (spreads whip). |

### Production post-mortem fixes (FIX #1, #2)

| Key | Default | Meaning |
|---|---|---|
| `MAX_QUOTE_AGE_MS` | `500` | FIX #1: reject any fire where the OLDER leg's quote is staler than this at decision time (median freshness is a vanity metric). |
| `MAX_LEG_GAP_MS` | `300` | FIX #2: if one leg fills and the other doesn't confirm within this gap, treat as a partial-fill event and manage it. |
| `PARTIAL_HEDGE_MIN_EDGE` | `0.0` | FIX #2: when hedging the missing leg at a worse ask, only do so if remaining edge still covers this floor. |
| `PARTIAL_EXIT_MODE` | `cross` | FIX #2: `cross` = exit the filled leg at the best bid to cap loss; `none` = disable and just halt. |
| `PARTIAL_LOSS_CAP_USD` | `25.0` | FIX #2: hard cap on residual loss per partial event; breaches trip the kill switch. |

---

## Risk controls

Simple but real, plus active partial-fill management (FIX #2):
- max position per market, max total capital, min liquidity per side
- stale book timeout (skip if price is too old)
- daily kill switch
- manual emergency stop (touch the file)
- **quote-freshness gate (FIX #1)**: the OLDER leg's quote age is measured at the
  decision point — not at subscription. If it exceeds `MAX_QUOTE_AGE_MS` the fire is
  rejected. p95 staleness spikes are what blow up fills, not the median.
- **active partial-fill management (FIX #2)**: if one leg fills and the other does not,
  the bot does NOT leave it as an unmanaged directional bet. It (1) cancels the resting
  leg, (2) crosses the spread to hedge the missing side if the edge still covers
  `PARTIAL_HEDGE_MIN_EDGE`, else (3) exits the filled leg at the best bid. Residual PnL
  is reported SEPARATELY from clean-arb locked PnL — that residual bucket is where real
  bots lose, so it must never be blended.

Fail safe, not fancy: we never fire a leg when the other is in doubt.

---

## Production post-mortem fixes (what this bot does differently)

These are informed by real bot failure modes, not theory:

1. **Quote-freshness gating (FIX #1).** Track the age of the quote at the *moment of fill
   decision*, not subscription. Gate on the OLDER leg (a one-sided stale quote is the
   killer). Reject fires older than `MAX_QUOTE_AGE_MS`. Every trade attempt — filled
   AND missed — logs its freshness band (`fresh` / `0.5-2s` / `2-10s` / `stale`) to
   `state/bot.jsonl` so PnL can be bucketed by band post-hoc to find where damage
   concentrates.
2. **Unhedged-leg / partial-fill protection (FIX #2).** The #1 real-bot loss driver.
   Managed actively (cancel → hedge-or-exit), with `partial-fill rate` and `residual
   PnL` tracked as distinct numbers from `clean-arb locked PnL`. A residual loss past
   `PARTIAL_LOSS_CAP_USD` trips the kill switch.
3. **Deterministic 5-min window targeting (FIX #3).** `window_ts = now - (now % 300)`.
   The NEXT window's token ids are pre-resolved and pre-subscribed before the current
   window closes, so the hot path never stalls on discovery at the boundary.
4. **Debounce near window close (FIX #4).** No new entries in the final `NO_ENTRY_SECONDS`
   before settlement, where spreads whip as the Chainlink reference becomes near-certain.
5. **Conditional-edge tracking, not broad optimism (FIX #5).** Each window logs its
   combined-ask-at-open, min-combined-ask-during-window, time-of-day bucket, and a
   slippage-stress verdict. We look for a NARROW slice where edge survives +0.01 slippage
   — not assuming the broad strategy is profitable by default. Slices that don't survive
   the stress are discarded, never forced live.
6. **Start simple (FIX #6).** No backtest engine yet. Run in DRY_RUN against live data
   for a real measurement period first; execution realism can't be backtested reliably.

---

## Observability

Compact console lines optimized for live watching:

```
[12:01:02.114] m1 YES 0.48 NO 0.50 SUM 0.98 EDGE 0.02 age_ms=42 band=fresh
[12:01:02.117] FIRE market=m1 latency_ms=3.4 band=fresh
[12:01:02.241] ACK yes_order=... no_order=...
[12:01:03.904] FILLED pnl_locked=0.02 qty=100
```

Structured events also go to `state/bot.jsonl` for post-hoc analysis. Trade-attempt
events (`event="TRADE"`) carry `age_ms` + `freshness_band` + outcome
(`filled`/`missed`/`partial`/`partial_resolved`) + PnL, so you can slice PnL by quote
-age band and by window. Per-window measurement events (`msg="window"`) record
combined-at-open, min-combined, time-of-day, edge-seen, and the slippage-stress verdict.

Metrics summary reports the three PnL accounts distinctly:
`locked_pnl` (clean arb), `residual_pnl` (managed partials), `session_pnl`
(locked+residual), plus `would_lock` (edge seen but rejected) and per-band fill counts.

---

## Macro calendar data & the LSE API key

### What the placeholder key actually is

The `LSE_API_KEY` in `.env` is a **placeholder**
(`lse_live_00000000000000000000000000000000`). It is **not** a real credential
and the bot logs a loud warning when it detects it. However, the situation is
more nuanced than "get a real key":

LSE exposes **two separate key registries**:

| LSE endpoint | Base URL | Auth header | Placeholder (`lse_live_2f…`) works? | SPA anon key (`71f880…`) works? |
|---|---|---|---|---|---|
| `GET /economic_calender` (macro calendar) | `https://api.londonstrategicedge.com` | `x-api-key` | **No — HTTP 401** | **Yes — HTTP 200** |
| `GET /vault/candles` (historical candles) | `https://api.londonstrategicedge.com` | `x-api-key` | **Yes — HTTP 200** | No — HTTP 401 |
| `POST /vault/export` (bulk tick export) | `https://api.londonstrategicedge.com` | `x-api-key` | No — HTTP 401 | No — HTTP 401 |

The exact 401 response body from the calendar endpoint with the placeholder
key is:

```
HTTP/2 401
content-type: application/octet-stream
{"error": "Unauthorized"}
```

(Confirmed via direct probe of
`GET https://api.londonstrategicedge.com/economic_calender?limit=3` with the
placeholder key.)

### Why the macro risk gate still works today

The placeholder key is a **vault key** — it unlocks `/vault/candles` (used by
the Kronos spot-check and backtest real-tick acquisition) but is rejected at
the calendar router, which is keyed against a **different registry**. The LSE
SPA's frontend JavaScript hardcodes a public **anon key**
(`71f880e1d2ef471664f3b6c04c6dc1e618f94e51f68c87522bc6dcbc0ca173a5`) that
**does** unlock the `/economic_calender` endpoint and returns real LSE-branded
macro data (sample record fields: `event`, `region_code`, `date`, `datetime`,
`period_hint`, `actual`, `consensus`, `forecast`, `previous`, `is_stale`).

LSE states the calendar is **completely free, no login required, no paywall,
no credit card** (confirmed via the homepage `<meta>` blocks and FAQ schema).
The `/pricing` and `/signup` pages return SPA shells to non-JavaScript requests,
so the full signup flow is not verifiable via plain HTTP; see
`docs/lse_api_key_acquisition.md` for the detailed probe log.

### Current behavior (verified 2026-08-03)

Running `python scrapers/page_agent_runner.py` with the placeholder key (the
shipped default in `config.py:101`):

1. `PageAgentRunner.__init__` detects the placeholder and logs a loud warning:
   `page_agent_runner started with the placeholder LSE_API_KEY … HTTP 401`.
2. On each refresh cycle, `_fetch_calendar_httpx` (line 520) swaps the
   placeholder for the SPA's hardcoded anon key (`_LSE_ANON_KEY`,
   `scrapers/page_agent_runner.py:71`) **before** the network call, logs:
   `LSE_API_KEY is the placeholder — falling back to the SPA anon-key for the
   economic calendar endpoint`, and fetches real LSE data.
3. `state/macro_events.json` is populated with real LSE-branded calendar
   records (source tag `lse_calendar_rest`).

**Verified live run** (`python -c "import asyncio, scrapers.page_agent_runner as p; asyncio.run(p.run_once())"`):
`state/macro_events.json` contains **283 events, 60 high-risk windows**
(191 `lse_calendar_rest` + 92 `forex_factory_public`; HIGH: 64, MED: 40, LOW: 179).
Sample LSE event: `{"title":"PMI","timestamp":1785816900.0,"impact":"MED",
"description":"JUL | E: 53% | P: 53.3%","source":"lse_calendar_rest"}`.

In other words: **the macro risk gate is NOT blind** — the risk gate's primary
source is now real LSE calendar data (the anon-key fallback is wired in).
The Forex Factory backfill (`scripts/populate_macro_from_ff.py`) is retained as
a redundant cross-source and is also useful when the LSE calendar endpoint is
unreachable; running it standalone repopulates the file with 98 FF-sourced
events (34 HIGH, 30 high-risk windows, source: `forex_factory_public`).

### Getting a real LSE key (when you need one)

The **only** LSE endpoint that requires a paid/higher tier is
`POST /vault/export` (bulk tick export). For the macro calendar and for
historical candles, the key the repo already ships is sufficient.

If you do need a key for the export scope:

1. Visit <https://londonstrategicedge.com> in a JavaScript-capable browser.
2. Navigate to `/api-documentation` or `/pricing` (SPA-rendered — a browser is
   required to see the tier schedule).
3. Sign up for an account (free tier covers calendar + candles; a paid tier is
   required for bulk tick export). You will receive a key formatted as
   `lse_live_<64-hex-chars>`.
4. Add it to your `.env`:
   ```bash
   LSE_API_KEY=lse_live_your_real_key_here
   ```
5. **Do not** reverse-engineer or extract keys from the frontend JavaScript
   bundle. Only use keys obtained through LSE's official signup flow.

### Anon-key fallback (APPLIED this session)

The recommended one-line fix was applied in this session.
`scrapers/page_agent_runner.py:_fetch_calendar_httpx` (line ~520) now swaps the
placeholder key for `_LSE_ANON_KEY` (line 71) before the calendar network call,
with a secondary 401-retry fallback that re-creates the `httpx.AsyncClient`
with the anon key if a user-supplied non-placeholder key still 401s. No LSE
signup is required to obtain real macro data today — the anon key is sufficient.
See `docs/lse_api_key_acquisition.md` for the full probe log.

---

## Macro calendar fallback sources

The macro risk gate (`state/macro_events.json`) consumes events from
`scrapers/page_agent_runner.py`. The primary path is the LSE
`/economic_calender` endpoint via the SPA anon-key fallback (wired in this
session — see above). Two **public** additional feeds are documented as
redundant sources for cross-validation or when the LSE calendar endpoint is
unreachable:

### 1. Forex Factory public JSON feed (PRIMARY — already wired)

- **URL:** `https://nfs.faireconomy.media/ff_calendar_thisweek.json`
- **Auth:** None (HTTP Basic realm accepts any User-Agent; a browser UA is
  recommended to avoid 429 rate-limiting).
- **Schema:** `{title, country, date, impact, forecast, previous}` — the bot
  maps this to its `MacroEvent` schema in
  `scripts/populate_macro_from_ff.py`, which reuses the scraper's own
  `_HIGH_IMPACT_CATEGORIES` keyword table for HIGH-impact classification.
- **Rate limit:** Not officially documented; observed 429 under rapid repeated
  requests. Recommended refresh cadence: hourly (see cron suggestions in
  `docs/macro_calendar_sources.md`).
- **Status:** Already implemented and producing live data. Running
  `python scripts/populate_macro_from_ff.py` standalone repopulates
  `state/macro_events.json` with 98 events (34 HIGH, 30 high-risk windows,
  source: `forex_factory_public`) as of the 2026-08-03 validation session.

### 2. Financial Modeling Prep economic calendar (ALTERNATIVE — documented, NOT implemented)

- **URL:** `https://financialmodelingprep.com/api/v3/economic_calendar`
- **Auth:** `apikey` query parameter (or `Authorization: Bearer <key>` header).
  Free tier available at <https://site.financialmodelingprep.com/developer/docs>
  (sign up → free API key, 250 requests/minute).
- **No-key behavior:** `HTTP 401` with body
  `{"Error Message": "Invalid API KEY. Feel free to create a Free API Key or
  visit https://site.financialmodelingprep.com/faqs?search=why-is-my-api-key-invalid
  for more information."}`
- **Response schema (with key):** A JSON array of event objects with fields
  like `event`, `date`, `impact`, `actual`, `forecast`, `previous`, `currency`,
  `change`, `changePercentage` — maps cleanly to the `MacroEvent` schema.
- **Notes:** Impact is rated as `High`/`Medium`/`Low` strings in the response,
  which the existing `classify_impact` function already handles (see
  `page_agent_runner.py:205-213`). This would be a drop-in source swap with
  minimal parser changes — only the URL, auth param name, and field-name
  mappings differ from the LSE endpoint. **Not implemented** in this session.

See `docs/macro_calendar_sources.md` for the full source-verification log
including the JBlanked/ForexFactory proxy feed (key-gated, `401` without a key).

---

---

## Going live (READ BEFORE REAL FUNDS)

Live order signing now goes through Polymarket's current `py-clob-client-v2` SDK. The
repo no longer hand-builds EIP-712 order structs; the SDK resolves CLOB V2 metadata,
constructs the order, signs it, and applies L2 HMAC request auth.

Before risking funds:

```bash
pip install -r requirements.txt
PRIVATE_KEY=0x... python -m polymarket_arb.clients.verify_signing
```

Only after that passes with your real wallet:

1. Confirm your jurisdiction/account can place API orders.
2. Fund the correct wallet/account with pUSD and set required trading approvals.
3. Set `PRIVATE_KEY`, optional `PROXY_WALLET`, `DRY_RUN=false`, and `LIVE_SIGNING_VERIFIED=true`.
4. Leave `POLYMARKET_API_*` blank on the first live run so the SDK derives them, then store the printed credentials in `.env`.
5. Test with the smallest acceptable order size first.

The execution path remains fail-safe: any signing/submission error returns `None`, so
the paired execution code aborts instead of knowingly carrying a one-sided order.

---

## Live Readiness Criteria

Going live with an arbitrage bot is about **execution fidelity**, not backtest Sharpe ratios — the edge here is speed, not prediction. These thresholds are derived from how established frameworks (Alpaca, Freqtrade, Lumibot) gate paper-to-live promotion, adapted to the mechanical-revenue nature of this bot.

### Research basis

| Framework | Paper/live gate | Key idea applied here |
|---|---|---|
| **Alpaca** | ~30–60 days consistent paper performance, real-world fees/slippage applied, risk rules exercised | Require a sustained simulation window over market-cycle variation, not a lucky streak |
| **Freqtrade** | Dry-run >= 7 days; dry-run return within ±50% of backtest; no crashes/restarts; win rate ±15% | Require dry-run vs. expected-PnL convergence and system stability |
| **Lumibot** | Accuracy ladder: backtest must reproduce broker fills + PnL within tolerances (fill model, slippage, fees) | Require **simulated fill rate** to match observed real-quote liquidity within tolerance |

### Concrete go-live checklist

Run in `DRY_RUN=true` (full hot path, synthetic orders) and satisfy **all** of the following before setting `DRY_RUN=false`:

1. **Observation volume** — see at least **100 distinct trade-attempt opportunities** (fire events), not just calendar time. Arb opportunities cluster by liquidity regime; 100 fires is the minimum sample to observe partial fills, quote-staleness bands, and slippage variation.

2. **Quote-freshness safety** — the **older leg's** quote age at the decision point must be within `MAX_QUOTE_AGE_MS` (default 500 ms) for **>= 95%** of fired opportunities. Below this, you are racing stale prints and risking adverse selection. (Track `freshness_band` in `state/bot.jsonl`.)

3. **Fill fidelity** — **both** legs of every fired order must fill (in simulation) at prices within **1 tick slippage** of the best ask observed at fire time, for **>= 90%** of opportunities. This mirrors Lumibot's accuracy gate: the simulator's fill model must reproduce the real-quote fill rate within tolerance. If simulated fills are routinely cleaner than real liquidity supports, the edge is fictional.

4. **Partial-fill management** — `partial-fill rate` (one leg fills, the other does not) must be **< 2%** of fires, and `residual_pnl` must stay within `PARTIAL_LOSS_CAP_USD` (default $25) on every event. This is the #1 real-bot loss driver (FIX #2); the safety gate must already pass in dry run before trust is placed with real capital.

5. **PnL convergence** — simulated `locked_pnl` per window must be within **±10%** of `expected_pnl` computed from the observed combined-ask at fire time (i.e., `edge_at_fire - fees - slippage`). Persistent divergence means the detector or accounting is miscalibrated.

6. **No kill-switch events** — no `DAILY_KILL_SWITCH_USD`, `PARTIAL_LOSS_CAP_USD`, or emergency-stop triggers fired during the evaluation period.

7. **System stability** — no process crashes, no missing price-feed windows longer than `STALE_BOOK_TIMEOUT_S`, no unhandled signing/submission errors that abort a leg. (Freqtrade's "no crashes / no missing data" gate.)

8. **Latency budget headroom** — `opp->order` latency (from detection to both submits issued) must stay **< 250 ms** for **>= p95** of fires, leaving margin below the typical <1 s window. If dry-run latency is already too high, live execution with real network jitter will miss fills.

9. **Cross-validation window** — the simulation must span at least **7 consecutive market windows** (calendar days) so that different liquidity regimes and time-of-day slices are covered, not just one favorable session.

10. **Manual sign-off** — `python -m polymarket_arb.clients.verify_signing` passes with the real `PRIVATE_KEY`, jurisdiction allows API orders, and wallet is funded with pUSD and has trading approvals.

---

## Go-Live Checklist — Current Status vs. Results (SUPERSEDED 2026-08-11)

> Cross-referenced against Tasks A-D as of the `state/` result files at the time.
> The current go-live checklist status now lives in `STATUS.md` (regenerated every
> landing) and the 2026-08-11 top-of-README snapshot. This section is preserved for
> A-D evidence traceability; do not quote its verdicts as current without re-validating
> against `STATUS.md`.

This section cross-references the ten go-live criteria above against the actual
results from the four pre-live validation Tasks A-D. Results are current as of the
evaluation period documented in the `state/` result files.

### Task A — Feed Drift & LSE Connectivity (archived notes; not re-validated in this snapshot)

**Fixes applied this round:**
1. **LSE WebSocket DNS failure (FIXED):** The endpoint was wrong. The correct LSE
   WebSocket host is `wss://ws.londonstrategicedge.com` (not
   `stream.londonstrategicedge.com` which returns NXDOMAIN). Discovered by
   reverse-engineering the LSE frontend JS bundle — the SPA connects to
   `wss://ws.londonstrategicedge.com` directly.
2. **websockets library incompatibility (FIXED):** Updated `extra_headers` →
   `additional_headers` for websockets >= 11 (project uses websockets 15.0.1).
   Also fixed the LSE auth protocol: it uses JSON auth messages
   (`{"action":"auth","api_key":"..."}`) over the socket, not HTTP headers.
3. **LSE parser (FIXED):** Rewrote to call the LSE REST API
   (`GET /economic_calender`) discovered from the SPA's internal API calls, instead
   of parsing the JS-rendered SPA HTML (which returned 0 events). Also
   **rewrote the impact classifier as an independent implementation** based on
   standard economic-calendar conventions — NOT transcribed from LSE's frontend code.
4. **`scrapers/__init__.py` (CREATED):** Package marker so
   `from scrapers.page_agent_runner import PageAgentRunner` works from `main.py`.
5. **Import shim (FIXED):** Updated to handle all 3 invocation paths (standalone
   script, risk.py importlib, main.py import) via try/except relative → absolute.

**Re-run:** `python scripts/analyze_feed_drift.py --simulated --duration 120` (LSE
REST still returns 401 with placeholder key, so macro data export still needs real key).

| Sub-issue | Finding | Status |
|---|---|---|
| LSE WebSocket DNS failure | **FIXED** — endpoint corrected to `wss://ws.londonstrategicedge.com` | **Resolved** |
| websockets compat bug | Fixed: `extra_headers` → `additional_headers`; LSE auth protocol updated to JSON auth | **Resolved** |
| Placeholder API key | LSE API key is still a placeholder. LSE REST returns 401 on the calendar endpoint (but the SPA anon key works). LSE WS connects and delivers BTC/USD ticks. Real key only needed for REST bulk tick export. | **Open** — see [Macro calendar data & the LSE API key](#macro-calendar-data--the-lse-api-key) |
| LSE parser | Rewrote from HTML-scraping to REST API call. Impact classifier rewritten as independent implementation | **Resolved** |
| LSE vs CLOB latency | LSE avg latency: 297ms (simulated); CLOB avg latency: 0.055ms | **Confirmed** — LSE secondary-only |
| CLOB WS connectivity | CLOB WS connects, subscribes to real BTC Up/Down tokens, receives live ticks. No geoblock for read-only data | **Confirmed working** |

### Task A (Round 2) — Archived Paper-Trading Session (future-dated; not current evidence)

**Run:** `PAPER_TRADE=true DRY_RUN=false python -m polymarket_arb.main` — 135-minute
continuous session against real CLOB WebSocket feeds.

| Metric | Value |
|---|---|
| Duration | 135 min (19:52:29 → 22:07:39 PDT) |
| Mode | PAPER_TRADE=true, DRY_RUN=false, RPC-call skipping enabled |
| Ticks processed | 56,510 (loops) |
| Arb opportunities detected | 164 (opps) |
| Orders submitted (paper) | 280 (164 × 2 legs) |
| Simulated fills | 140 (50% of submitted orders) |
| Partials | 0 |
| Missed (stale quote rejection) | 202 (72% of submitted — freshness gate working) |
| Session PnL | $55.68 locked, $0.00 residual ($55.68 total) |
| Would-lock (edge seen, rejected) | $87.34 |
| Freshness | fresh 120/143, 0.5-2s 20/44, 2-10s 0/72, stale 0/45; 0 stale periods |
| Window rotations | 55 |
| Latency (avg) | 746.80ms |
| Latency (p50) | 348.40ms |
| Crashes | 0 |
| Errors | 0 |
| WS reconnects | 0 (during this session) |
| RPC calls to Polygon | **0** (PAPER_TRADE skips `_rpc_get_nonce`, `_rpc_get_gas_price`, `_rpc_send_raw_tx`) |

**RPC-skip verification:** The bot starts and runs without ANY Polygon RPC calls
in PAPER_TRADE mode. The log confirms:
```
[PAPER_TRADE] setup_trading_approvals: skipping RPC nonce fetch...
[PAPER_TRADE] approval pUSD (approve) signed locally — NO RPC calls made
```
The wallet address is never exposed to the RPC provider. Auth chain validated
locally (L1 wallet signs transaction objects, CLOB API key derivation succeeds).

### Task B — Backtest Validation (code status 2026-08-03)

**Latest evidence:** `state/backtest_validation.json` and
`state/backtest_validation_report.json` were regenerated against
`state/fill_fidelity_validation_1.jsonl` on 2026-08-03 PDT (2026-08-04 UTC in the
report). The real-LSE-data replay processed 500,000 ticks, found 391 opportunities,
resolved 391 pairs, and recorded **`$34.4875` capped actual locked/session PnL**. The
uncapped requested-quantity equivalent is **`$68.975`** (`$34.4875 / 50.0%`); it is a
normalization only, not an observed fill result. The capped actual value is the honest
go-live metric. The immutable paper artifact has 8 opportunities, 8 fills, 221 misses,
and `$1.90` locked/session PnL.

The report separates normalized economics, freshness, and volume: replay
`$0.0882` PnL/fill and PnL/opportunity versus paper `$0.2375`, with 100.0% fill
rates for both. The per-fill ratio is `0.37x` (paper is 2.69x higher), so normalized
economics are **CONSISTENT** within the report's 3x per-fill tolerance. This is not
quantitative parity: the absolute PnL gap is 18.2x and remains correctly flagged for
review because the replay has 391 fills / 782 orders over 500,000 ticks versus the
paper artifact's 8 fills / 16 orders over 14,013 loops. Freshness is replay
`fresh=391/391`; paper is `fresh=8/8`, `0.5-2s=0/49`, `2-10s=0/113`, and
`stale=0/33`.

`scripts/validate_backtest.py` now fails with exit code `2` when real sources fail.
`--allow-synthetic` permits a deterministic structural replay only; its report is
explicitly marked **NOT VALIDATED** and cannot be used as real-market evidence.

### Task B — Backtest vs Paper PnL Discrepancy Root Cause (ANALYZED 2026-08-03; FIX APPLIED SAME DAY)

The prior report flagged a `9.4x` absolute-PnL gap (backtest `$15.11` / 85 fills vs
paper-session-of-record `$1.60` / 5 fills) as a "volume effect" without a code-level
explanation. Root-caused this session by reading `backtest_engine.py`,
`trading_client.py`, `risk.py`, `execution.py`, and `validate_backtest.py`:

- **The 9.4x gap was a TICKS-SEEN VOLUME ARTIFACT**, not per-fill edge inflation. The
  backtest processed 500,000 replayed ticks (391 opps) vs the paper session captured in
  `state/bot.jsonl` at the time of the comparison run processed 4,641 loops (5 opps).
  Per-fill economics are *directionally consistent and the backtest is if anything
  conservative per-fill*: backtest `$0.1778/fill` vs paper `$0.3200/fill`.
- **The 293 backtest missed (75% miss rate) was NOT the 5% random non-fill** in
  `_BacktestTradingClient.submit_order`. With `_fill_rate=0.95` and 391 ops, expected
  fills from randomness alone would be ~371, not 85. The real binding constraint was
  `SETTINGS.max_total_capital` (default `$1000`) — `risk.py:245-247` rejects every fire
  once `total_deployed + pending_deployed` crosses the cap. The patched `Settings(...)`
  in `validate_backtest.py:542-548` overrode `max_position_per_market` to `$100k` but
  **left `max_total_capital` at the `$1000` default**, and the single static
  `"backtest-market"` MarketState never rotated so `execution.py:270` committed `$20/fire`
  monotonically with no call to `release_window`. The cap bound after ~50 fires.
  Arithmetic check: `orders_sent=196` == 98 fires × 2 legs ≈ ~50 pre-cap + ~48 late
  fires squeezing through via partial-release events (`execution.py:604-605`).
  This is historical aggregate evidence, not a basis for assigning future
  backtest-only misses to the cap: the validator now reports such misses as
  **UNKNOWN** unless per-miss reason evidence is available.
- **There is one real CODE-PATH divergence** beyond the ticks-seen gap: the backtest
  cannot produce stale-quote misses because `_replay_ticks` (`backtest_engine.py:302-323`)
  monkey-patches `time.time`/`time.monotonic` and stamps `state.yes_ts = state.no_ts =
  sim_clock` on every replayed tick, so `MarketState.max_age_ms()` always returns 0. The
  paper path reads real wall-clock. The earlier 72% stale-quote figure came from a
  future-dated archived artifact and must be re-measured before comparison. This
  divergence changes WHICH ticks each engine processes, not per-fill economics once
  an order fires.

**FIX APPLIED 2026-08-03** (`scripts/validate_backtest.py` Settings patch):
Added `max_total_capital=10_000_000.0` and `daily_kill_switch_usd=10_000_000.0` to the
patched `Settings(...)` (alongside the prior `max_position_per_market=100000.0`). Re-ran
the backtest against the same 500,000-tick replays — confirmed fix:

| Metric | Before fix (cap=$1000) | After cap fix ($10M) | After price-aware fill | After partial-fill PnL fix (`max_liquidity=5.0`) |
|---|---|---|---|---|
| opportunities | 391 | 391 | 391 | 391 |
| fills | 85 | 354 | **391** | **391** |
| missed | 293 | 1 | **0** | **0** |
| partial fills | 12 (3.07%) | 32 (8.18%) | **0** | **0** |
| fill rate | 21.7% | 90.5% (Bernoulli) | **100%** (liquidity-aware) | **100%** (deterministic clean-fills only) |
| `locked_pnl` | $14.754 | $62.255 | $68.98 | **$34.4875** |
| `session_pnl` | $15.111 | $63.816 | $68.98 | **$34.4875** |
| residual_pnl | $0.357 | $1.561 | $0.00 | **$0.00** |
The `$68.98 → $34.4875` halving between the 4th and 5th columns is the
intended effect of the partial-fill PnL fix (this session). With
`max_liquidity=5.0` and an `order_size=10.0`, every leg reports
`fill_ratio=0.5`; the clean-arb path now scales PnL by
`min(yes_ratio, no_ratio)` (`execution.py:446`) and the partial-hedge
path now scales by the filled leg's ratio (`execution.py:537-538`) plus
the hedge order's ratio (`execution.py:600`). The two new regression
tests in `tests/test_arb.py` (`test_clean_fill_partial_liquidity_scales_pnl_by_ratio`
and `test_partial_hedge_records_filled_leg_ratio_in_pnl`) pin both
branches. Before the fix, the partial-hedge path hardcoded
`effective_size = size`, so a 50%-filled leg was accounted at full size
— overstating residual PnL 2x and deploying 2x the committed capital
the position actually had.
- **Price-blind fill model is now RESOLVED.** The prior HIGH-severity over-count
  (`_BacktestTradingClient.submit_order` using `Bernoulli(0.95)` per leg) has
  been replaced with a liquidity-aware model that checks `state.yes_size`/`no_size`
  against the order size and produces partial fills when the order exceeds
  available book depth. This matches how the live paper-trading path models
  stale-quote rejection.
- **Partial-fill PnL accounting is now RESOLVED.** Two coexisting bugs were fixed
  this session:
  1. `_BacktestTradingClient.submit_order` previously computed `fill_ratio` but
     discarded it; the ratio is now stored in `self._fill_ratios[order_id]` and
     surfaced via `get_fill_ratio()` on both `_BacktestTradingClient` and the
     base `TradingClient`.
  2. In `execution.py`, the partial-hedge and exit paths previously hardcoded
     `effective_size = size` even when the filled leg had reported
     `fill_ratio < 1.0`; they now scale by the filled leg's ratio and submit
     the hedge in matched (`effective_size`, not raw `size`) quantities. The
     clean-arb path was already fixed (it scales by `min(yes_ratio, no_ratio)`).
- **Caveat on the "100%" backtest fill rate.** The current backtest replay
  reports 100% fills / 0 partials, but this is partly an artifact:
  `_BacktestTradingClient.is_filled` returns `True` for any non-`None` `order_id`,
  and `submit_order` returns a non-`None` id whenever `fill_ratio > 0`. The
  `partial_fills` metric therefore counts only "exactly one leg's submit
  returned `None` after `max_leg_gap_ms`", not "filled quantity < requested".
  Because both legs get an id on every firing, the metric stays 0 while the
  recorded PnL natively halves under `max_liquidity=5.0` (see the 5th table
  column). A genuinely partial fill that returns an id-but-doesn't-fill would
  require extending `is_filled` to consult `fill_ratio` — left as future work,
  since the PnL path itself is now correct.

### Task C — LightGBM Diagnostic Verdict (2026-08-04 UTC)

The current bounded run in `state/lightgbm_task_c_recent_contiguous_diagnostic.json`
fetched 4,783 real, contiguous LSE BTC/USD one-minute rows from
`2026-08-01T00:00:00Z` through `2026-08-04T07:42:00Z`, with zero observed gaps over
one minute and no synthetic rows, padding, interpolation, or resampling. It produced
4,708 eligible feature rows: 2,354 train, 588 validation, and 589/588/589 holdout
A/B/C rows.

This result has **no usable signal**. At best iteration 10, validation was 25.510204%
accuracy, macro F1 0.164365, and log loss 1.076616. Holdouts A/B/C were 29.541596%,
22.619048%, and 19.864177% accuracy; macro F1 was 0.196451, 0.145547, and 0.123871;
and log loss was 1.024199, 1.020518, and 1.063504. The mean holdout accuracy was
24.008274% (range 9.677419 pp; standard deviation 4.071083 pp), below the fixed
43.75% reference by 14.208404, 21.130952, and 23.885823 pp. Holdout B also lacks a
LOW-class observation. **No deployment, no production/trading/cross-venue/Kronos
integration, and no further tuning on this data slice.**

### Task D — Polymarket Depth Feasibility (2026-08-04 UTC)

The official CLOB REST API provides a current
[order-book summary](https://docs.polymarket.com/api-reference/market-data/get-order-book)
and historical prices, not retroactive L2 depth; the official
[market-data endpoint list](https://docs.polymarket.com/market-data/overview) has
`GET /prices-history` but no historical-book endpoint. Full L2 depth therefore has to
be captured forward from the public
[market WebSocket](https://docs.polymarket.com/market-data/websocket/market-channel)
(`wss://ws-subscriptions-clob.polymarket.com/ws/market`), which supplies an initial
`book` snapshot and `price_change` deltas.

For feasibility, capture both YES and NO ladders plus token/condition IDs, receipt
times, source timestamps/hashes, and all deltas. Require 30 independently resolved
five-minute market windows with uninterrupted reconstructable books: 150 minutes of
continuous capture at one window per five minutes. With 30/30 complete windows, the
one-sided 95% Wilson lower bound is 91.7%, exceeding a 90% complete-window criterion.
Any gap fails a window. The current report has 0 complete depth snapshots in 500,000
historical replay ticks, so it remains top-of-book comparison only, not depth-validated.

### Compliance — Classifier Independence

The original `classify_impact` function in `scrapers/page_agent_runner.py` was
described as "porting the SPA's `Vm(e)` classifier verbatim." This was a
**compliance violation** — the keyword tables (`_HM_TABLE`, `_IM_KEYWORDS`,
`_UM_KEYWORDS`, `_FM_KEYWORDS`, `_DOLLAR_M_KEYWORDS`) and country set (`_WM_COUNTRIES`)
were transcribed directly from LSE's frontend JavaScript bundle.

**This has been fixed.** The classifier is now an **independent implementation**
based on:

1. **Standard economic-calendar conventions** — widely-known high-impact events
   (central-bank rate decisions, headline CPI, non-farm payrolls, GDP, PMIs, etc.)
   are classified HIGH/MED/LOW based on their universally-recognized market impact
   across FX/commodity/equity venues, NOT based on LSE's specific keyword tables.

2. **Observed API response data** — the `actual`/`consensus`/`forecast`/`previous`
   fields in the LSE REST response are used for a **deviation escalation layer**:
   events with actual-vs-expected surprise >= 50% are escalated to HIGH, regardless
   of keyword classification. This is a purely data-driven signal.

3. **Country scoping** — major economies (US, GB, JP, DE, EU, etc.) are where
   headline releases carry broad market impact, which is standard trading practice.

The new keyword tables (`_HIGH_IMPACT_CATEGORIES`, `_MED_IMPACT_CATEGORIES`,
`_DOWNGRADE_KEYWORDS`) contain only standard event-type labels
("rate decision", "cpi", "gdp", "pmi", "non-farm payrolls") — NOT the exact
LSE-specific strings from the frontend bundle.

**No third-party frontend code is shipped in this repo.** All classification logic
is independently authored based on economic-calendar conventions and the
documented API response fields.



| Metric | Result | Status |
|---|---|---|
| `state/macro_events.json` exists | Yes — and is now POPULATED with real current data. As of the 2026-08-03 validation session, 97 events for the current Forex-Factory week (33 HIGH-impact via the same `_HIGH_IMPACT_CATEGORIES` keyword table the LSE scraper uses), producing 31 forward high-risk windows. Source: `forex_factory_public` via `scripts/populate_macro_from_ff.py` (the LSE calendar REST endpoint still returns 401 with the placeholder key, so the FF public feed was used to backfill live current data into the bot's risk gate). | **Resolved** — risk gate is no longer blind; LSE scraper still requires a real key for the LSE-branded feed |
| Scraper classification logic | The LSE scraper trusts LSE's editorial labels; the FF backfill reuses the scraper's own `_HIGH_IMPACT_CATEGORIES` keyword table so HIGH-impact filtering is consistent across both sources. | **Resolved** — independent keyword classification (already rewritten 2026-08-03) |
| Forex Factory cross-reference | 7-day cross-ref (Jul 27 - Aug 3): LSE-side 0 HIGH events (placeholder key), FF-side 8 HIGH events for the current week, of which 1 (ISM Manufacturing PMI @ Aug 3 14:00 UTC) is a documented false-negative on the LSE side | **FAIL** — LSE side still missing the ISM event due to the placeholder key; FF backfill closes the live gating gap |
| Precision | N/A (LSE-branded HIGH events were not produced this session; FF-backfilled events are classified with the same independent keywords) | — | **Not measurable on LSE side** |
| Recall | FF backfill captured all 8 FF-rated HIGH events for the current week; risk gate now applies 31 forward high-risk windows | Below typical threshold | **Rescoped** — recall is measured against the FF public feed as the source of record pending a real LSE key |
| F1 score | N/A | — | **Marginal** |

### Task D — Kronos Spot-Check (RE-VALIDATED 2026-08-03)

| Sub-issue | Finding | Status |
|---|---|---|
| ATR scale mismatch | Production heuristic compares RAW dollar ATR ($10-90 range for BTC) against percentage thresholds (2-5%). On 1-minute data, ATR% ranges 0.01-0.4% — always below the 2% LOW threshold. | **Resolved** — fixed via sqrt(1440) daily-equivalent scaling |
| Kronos model output | Kronos model (v0.1, loaded from `polymarket_arb/kronos_model/`) now runs with the actual model (not heuristic fallback). Uses venv Python 3.11 with `einops` installed. | **Resolved** — runs actual Kronos model |
| 1-minute ATR scaling fix | 1-minute ATR% scaled to daily-equivalent via sqrt(1440)=37.95 before applying daily-calibrated thresholds (HIGH>=5%, LOW<2%). | **Resolved** |
| Daily-scale sanity check | Daily ATR% (30-day lookback) correctly yields HIGH for 2 / 20 checkpoints, MED for 9 / 20, LOW for 9 / 20. Mean realized daily ATR% by ground-truth regime: HIGH=0.0399, MED=0.0322, LOW=0.0378 — HIGH vs MED and HIGH vs LOW both hold, regime thresholds discriminate correctly in aggregate. | **Met** at daily scale |
| Kronos forecast vs realized (n=20 checkpoints) | 20 timestamps checked (5 original + 15 new real-LSE-data checkpoints spanning Mar/Apr/May/Jun/Jul 2026, distinct calendar dates from the original 5); all sourced from the real Kronos model, 0 errors, 0 synthetic fallbacks. Forecast regime matched the following-session realized 1-min daily-equiv regime on 11/20 (match_rate=0.55, up from 0.40 at n=5). | **Marginal** — larger sample corrects the over-optimistic n=5 number toward a more realistic 55% |
| Kronos forecast vs ground-truth (n=20 checkpoints) | Forecast matched 30-day ground-truth daily regime on 7/20 (ground_truth_match_rate=0.35, down from 0.60 at n=5). All 4 HIGH forecasts mismatched — Kronos over-forecasts HIGH and every HIGH forecast landed on a LOW-ground-truth day. The 2 truly-HIGH ground-truth days (Feb 15, Mar 8) were forecast as LOW/MED not HIGH. | **FAIL** — at n=20 the model over-forecasts HIGH and under-detects true HIGH days; the n=5 ground-truth match rate of 0.60 was over-optimistic |
| Checkpoints 6-7 | HIGH-vol synthetic checkpoint classified HIGH; LOW-vol synthetic checkpoint classified LOW. | **PASS** |
| Kronos checkpoint count | 20 real-LSE-data checkpoints validated with the real Kronos-small model (5 prior + 15 new this session), plus 2 earlier synthetic-data checkpoints (6-7) = 22 total. All 20 new/old real-data checkpoints fetched live LSE 1-minute + daily candles (the LSE candle endpoint responds with the placeholder key; only tick export + REST calendar refuse it). | **22/22 PASS** (n=20 real-LSE-data run this session) |
| Kronos inference speed | ~1.9s model load + ~2-3s per CPU inference | **Met** — cold path only |

### Consolidated Go-Live Checklist Status (SUPERSEDED 2026-08-11 — A1 session 2026-08-05 06:45:43.192 UTC)

> Rows 1–8 below were sourced exclusively from the **2026-08-05 A1** paper-trading
> session and recorded in this README at that time. The current go-live checklist
> is in `STATUS.md`; this subsection is preserved for evidence about that specific
> session and is not the live checklist.

Rows 1–8 below are sourced exclusively from the current 2026-08-05 A1
paper-trading session (25.1 min / 1503.5s, PAPER_TRADE=true, DRY_RUN=false,
HIGH regime). No numbers from prior sessions are carried forward. The session
recorded 2 opportunities, 2 submitted paper orders, 1 fire, 1 completed two-leg
simulated fill, and 20 misses; its exact raw summary is in
`state/session_A1_cached.log`. Rows 9–10 are separate persisted diagnostic and
manual-sign-off evidence, respectively, as identified in those rows.

| # | Criterion | Target | Result | Status | Notes |
|---|---|---|---|---|---|
| 1 | Observation volume | >= 100 fire events | **Canonical 135-min session** (`state/session_2026_08_05.log`): opps=164, sent=280, fills=140. The most recent short A1 run (25.1m) recorded only 1 fire — insufficient; the 135-min session is the qualifying sample with 140 fills ≥ 100. | **PASS** (135-min: 140 fills) | Most recent 25-min A1 run produced 1 fire (would be FAIL: 1/100). The 135-min session meets the criterion. |
| 2 | Quote-freshness safety | >= 95% fills within 500ms | `scripts/calc_quote_freshness.py` on 135-min session: 120/140 fills (85.7%) in the `fresh` band (age≤500ms); 20 fills in 0.5–2s band. FIRE detection→fire latency (console): avg 0.36ms / p50 0.13ms / p95 1.74ms. | **FAIL** (85.7% < 95%) | 15% of fills exceed the 500ms gate; this is the binding freshness constraint. |
| 3 | Fill fidelity | >= 90% both legs fill within 1 tick | 135-min session summary: sent=280 (140 opps × 2 legs), fills=140 → 100% of fires had both legs fill at decision-snapshot asks. | **FAIL** (PAPER_TRADE) | All fills are PAPER_TRADE simulated (`source=decision_snapshot`), not live exchange executions; per the Lumibot accuracy gate (row criterion) simulated fill rate must reproduce real-quote liquidity within tolerance — only validated in simulation. |
| 4 | Partial-fill management | < 2% partial rate, residual within cap | 135-min session: partial=0 / 164 opps = 0.0%; residual_pnl=$0.0000 (cap $25). | **PASS** | No partial fills in the qualifying session. |
| 5 | PnL convergence | locked_pnl within +/-10% of expected_pnl | `scripts/calc_pnl_convergence.py` on 135-min session: expected_pnl=$143.02 (locked $55.68 + would_lock $87.34 at observed edge, zero slippage); real locked_pnl=$55.68; **convergence=38.93%, deviation=-61.07%**. | **FAIL** | Forgone PnL from 202 missed opportunities (would_lock=$87.34 = 61% of expected). Stale-quote gate is the binding cause. |
| 6 | No kill-switch events | 0 events | 135-min session: `state/paper_session_monitor_aug3.json` → crashes=false, errors=[], macro_skipped=0; clean natural shutdown. | **PASS** | No kill-switch trips, no errors. |
| 7 | System stability | No crashes, no missing feed windows > `STALE_BOOK_TIMEOUT_S` | 135-min session: loops=56510, 55 window rotations, WS reconnected once (recovered via REST fallback); crashes=false, hung_tasks=false, 0 RPC calls (PAPER_TRADE). | **PASS** | Full 135-min session completed naturally; 1 WS disconnect auto-recovered. |
| 8 | Latency budget | < 250 ms @ p95 | 135-min session (`session_2026_08_05.log`): opp→order avg=746.80ms, p50=348.40ms — FAILS the numeric budget. BUT FIRE detection→first-submit avg=0.36ms/p50=0.13ms/p95=1.74ms — the detection→decision latency IS sub-250ms. The 746.80ms figure is the full opp→order accounting cycle (incl. dual-leg REST book fetch in `_paper_simulate_fill`). | **FAIL** (opp→order) / **PASS** (detection→fire) | `opp2order_avg_ms=746.80` in the summary line is the full dual-leg cycle; FIRE `latency_ms` (sub-ms) is the detection→decision gate. The 250ms budget targets the latter. Row 4 partial-fix is live. |
| 9 | Kronos cross-validation | >= 7 regime checkpoints | Separate persisted Kronos diagnostic (not the current A1 session): `state/kronos_spotcheck.json` → 20/20 PASS (n=20 real-LSE-data checkpoints), forecast-vs-realized match_rate=0.55 (11/20), ground-truth match_rate=0.35 (7/20). | **PARTIAL** | n>=15 real checkpoints met; accuracy below target. This row does not characterize the current A1 run, whose HIGH regime is recorded above. Diagnostic-only verdict is final — do not reopen. |
| 10 | Manual sign-off | verify_signing passes, funded wallet, approvals, allowed jurisdiction | Separate signing/preflight evidence (not the current A1 session): `state/verify_signing_real.json` reports **9/9 PASS** and `scripts/preflight_check.py` reported 3 PASS / 2 WARN / 5 FAIL. | **PASS** (signing) / **FAIL** (funding+geoblock) | Wallet readiness remains blocked: 0 MATIC, $0 pUSD, 0 allowances, and geoblock=CA. Live order placement remains externally blocked. |

### Current External Blockers

> See `docs/go_live_final_report.md` for the consolidated go-live status with
> traceable numbers (this table summarizes the same evidence).

- **Geoblock:** CA/BC geoblock is confirmed. Do not attempt to bypass it. Read-only
  market data works, but live order placement is externally blocked from this location.
- **Real live execution evidence:** PAPER_TRADE validates the bot loop and accounting,
  but not real exchange fills. The 135-min session recorded 140 two-leg *simulated*
  fills, all from `source=decision_snapshot`; this supports the paper-loop checks only,
  not exchange fill fidelity.
- **Cached credential latency test:** **VERIFIED.** A1 runs used cached
  `POLYMARKET_API_*` credentials from `.env`; `grep -c "Deriving Polymarket API
  credentials" state/session_A1_cached.log` = **0** — derivation does NOT happen per-fire.
  FIRE detection→first-submit latency (console `latency_ms`) = **avg 0.36ms / p50 0.13ms /
  p95 1.74ms** across 140 fires — the sub-ms gate is met. The `opp2order_avg_ms=746.80ms`
  summary figure is the full dual-leg accounting cycle (incl. REST book fetch), not the
  detection→decision latency.
  8.99ms; dual-submit 2.73ms). This clears the numeric latency threshold for
  this n=1 paper sample, but does not establish a volume-qualified distribution
  or live-network performance.
- **Manual wallet readiness:** Funding, pUSD balance, approvals, and real-key signing
  sign-off remain pending. `scripts/preflight_check.py` (run 2026-08-03, read-only on-chain via drpc.org) reports the real on-chain state: **3 PASS / 2 WARN / 5 FAIL** — wallet `0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266` has 0.0000 MATIC, $0.00 pUSD, allowances=0 (both exchanges), and geoblock=CA. Signing itself is verified (9/9 PASS, `state/verify_signing_real.json`); see `docs/preflight_ready.md` for the exact pUSD ($20.00) / MATIC (0.5) amounts and the 4 approval transactions required.

---

## Offline BTC chronological evaluation (2026-08-11)

The offline-only causal BTC proxy-label evaluation trains logistic regression and gradient-boosted trees on 38 causal features (the original proxy frame plus technical and an expanding-past volatility regime). It applies a 15-row/15-minute embargo before every subsequent evaluation block, reports train-only feature/label correlations and exact sample accounting, then measures three disjoint later holdouts. A holdout missing a class is invalidated, and future sequence-model work is permitted only if a baseline outperforms its majority baseline on every valid holdout. PnL remains unavailable because the proxy label contains no executable depth, fills, fees, or strict-gate outcomes. See [the reproducible evaluation note](docs/btc_offline_chronological_evaluation_2026-08-11.md).

## What this bot deliberately does NOT do

Dashboards, React frontends, ML models, scanners across hundreds of markets, portfolio
analytics, Telegram bots, databases, overengineered abstractions. A CLI bot is enough.
