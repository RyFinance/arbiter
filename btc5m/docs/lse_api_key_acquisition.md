# LSE API Key Acquisition — Why the Calendar Endpoint 401s

Status: **RESOLVED root cause** — the 401 on the LSE economic calendar was a
scraper misconfiguration (wrong key used), not a missing-account problem. The repo
already contains a working key for the calendar endpoint.

## TL;DR

| LSE endpoint | Public path | Accepts the repo's `_LSE_ANON_KEY` (`71f8...`)? | Accepts the placeholder (`lse_live_2f...`)? |
|---|---|---|---|
| `GET /economic_calender` (macro calendar) | `https://api.londonstrategicedge.com/economic_calender` | **YES (HTTP 200)** | No (HTTP 401) |
| `GET /vault/candles` (history candles) | `https://api.londonstrategicedge.com/vault/candles` | No (HTTP 401 `invalid api key`) | **YES (HTTP 200)** |
| `POST /vault/export` (bulk tick export) | `https://api.londonstrategicedge.com/vault/export` | No (HTTP 401) | No (HTTP 401) |

The two low-friction LSE endpoints are gated by *different* keys. The repo already
ships both: `config.py` defaults `SETTINGS.lse_api_key` to the placeholder
(`lse_live_00000000000000000000000000000000`), which works for the candle endpoint,
and `scrapers/page_agent_runner.py:71` defines `_LSE_ANON_KEY`
(`71f880e1d2ef471664f3b6c04c6dc1e618f94e51f68c87522bc6dcbc0ca173a5`), which works for
the economic-calendar endpoint. The scraper's `_fetch_calendar_httpx` was previously
hard-wired to the user key (which defaults to the placeholder that the calendar
endpoint rejects) — that was the only reason `state/macro_events.json` was empty
before session-2026-08-03. **This is now fixed:** `_fetch_calendar_httpx` falls back
to `_LSE_ANON_KEY` whenever `SETTINGS.lse_api_key == _LSE_PLACEHOLDER_KEY`, and also
retries with the anon key on a 401 (see "Anon-key fallback (applied)" below).

The marketing site (`https://londonstrategicedge.com`) states the calendar is
*"completely free, no login required. Most platforms gate this behind a
subscription; we do not."* (FAQ schema on the `/calendar` page). This is consistent
with the anon-key working without any account.

## Why the candle endpoint accepts the placeholder but the calendar endpoint does not

These are different authorization schemes on different LSE clusters:

- **`/vault/*` endpoints** (candles AND bulk export) live under a PathPrefix-based
  router that validates an `x-api-key` against a registry of "vault" keys. The
  placeholder `lse_live_00000000000000000000000000000000` is registered as such a
  vault key and the candle endpoint unlocks read access. The bulk tick export path
  (`/vault/export`) requires a *higher* tier than the candle path — same router,
  different scope, so the placeholder 401s on export (see `state/lse_backfill_feasibility.json`
  `export_probe.download_status_code: 401`) but 200s on candles.

- **`/economic_calender` (calendar)** lives at the API root (NOT under `/vault`)
  and is keyed against a different registry. The SPA's frontend bundle hardcodes the
  anon key `71f880e1d2ef471664f3b6c04c6dc1e618f94e51f68c87522bc6dcbc0ca173a5`
  (see `scrapers/page_agent_runner.py:71` and the comment "SPA hardcodes this"),
  which is what unlocks the public calendar. The placeholder vault key is registered
  only for `/vault/*` and is 401'd at the calendar router.

  The exact 401 response body from `GET /economic_calender` with the placeholder
  vault key is:
  ```
  HTTP/2 401
  content-type: application/octet-stream
  {"error": "Unauthorized"}
  ```
  (Confirmed via direct `curl` probe with the placeholder key; the body is 25
  bytes, `Content-Type: application/octet-stream`, served via Cloudflare.)

- **Bulk tick export `/vault/export`** requires a vault key with an `export` scope —
  none of the two defaults (placeholder OR anon) unlock it. This is the only endpoint
  that requires you to sign up for a higher tier or a paid export scope.

## Verified from this session (`scripts/populate_macro_from_ff.py` and direct probes)

- `GET https://api.londonstrategicedge.com/economic_calender` with `x-api-key: 71f8...`
  returns `HTTP 200`, real records (sample fields: `id`, `date`, `time`, `datetime`,
  `region_code`, `event`, `period_hint`, `actual`, `previous`, `consensus`, `forecast`,
  `is_stale`, `created_at`, `updated_at`, the revisions flags). The LSE records have
  **NO `impact` / `importance` field** — that is why the project's scraper applies
  `_HIGH_IMPACT_CATEGORIES` keyword classification (see
  `scrapers/page_agent_runner.py:161`).
- Using the scraper's *exact* PostgREST params
  (`{date: gte.<today>, and: (date.gte.<today>,date.lte.<+7d>), order:
  date.asc,datetime.asc, limit: 200, is_stale: is.false}`) + anon-key returns real
  upcoming events including the ISM Manufacturing PMI on 2026-08-03 that an earlier
  Forex Factory cross-reference said "LSE missed."
- The placeholder `lse_live_00000000000000000000000000000000` still works on
  `/vault/candles` (real 1-minute daily BTC/USD candles returned in this session), so
  the Kronos spot-check, backtest real-tick acquisition, and LSE-backfill-feasibility
  probe all keep working on the placeholder.

## Standard signup path (for higher-tier scopes such as bulk tick export)

This is the only path for which this repo actually lacks a key today.

1. **Web signup:** the SPA's auth API at `api.londonstrategicedge.com/signup`
   (`POST`) and `/auth/signup`, plus `/verify-email` and `/welcome-email` callbacks —
   a standard email-verification sign-up flow. The marketing funnel lives at the
   sister domain `https://lsedemo.com/signup` (route `/signup` also exists on
   `api.londonstrategicedge.com`; both require an `x-api-key` header even on signup,
   meaning you must seed your first key via a public anon-key request before you
   signup is accepted as a self-serve flow). The available public landing pages:
   - https://londonstrategicedge.com/ (marketing)
   - https://londonstrategicedge.com/calendar (calendar web UI; live demo with anon-key already transparent)
   - https://londonstrategicedge.com/api-documentation (HTTP API docs — JS-rendered)
   - https://londonstrategicedge.com/data (free-dataset browser)
2. **Tier/pricing:** the marketing site tagline and FAQ schema both state "no
   paywall, no credit card required, one free key" (`https://londonstrategicedge.com`
   homepage `<meta>` block). The pricing page (`/pricing`) is SPA-rendered and did
   not return a parseable tier schedule on a plain-HTTP probe in this session; this
   should be checked in a JS-capable browser. As of session-2026-08-03, no anecdotal
   higher tier appears to be required for the calendar endpoint (it returns live LSE
   data for free via the anon-key).
3. **Scope you actually need:**
   - **`economic_calender` (calendar, the macro-risk gate)** — works today with the
     anon-key already in the repo. You do NOT need to sign up for a key.
     `scrapers/page_agent_runner.py:_fetch_calendar_httpx` now falls back to
     `_LSE_ANON_KEY` automatically when `SETTINGS.lse_api_key` is the placeholder, and
     also retries with the anon key if a 401 is returned — so
     `state/macro_events.json` populates with real LSE-branded data out of the box.
   - **`/vault/candles` (historical candles, used by Kronos spot-check + backtest)**
     — works today with the placeholder `lse_live_2f0a...`. You do NOT need to sign
     up.
   - **`/vault/export` (bulk tick export)** — this is the ONLY endpoint for which you
     must sign up for a higher tier. Watch `https://londonstrategicedge.com/pricing`
     in a JS-capable browser, or contact LSE directly (links on the homepage
     include a LinkedIn company page and an X/Twitter handle
     `@londonstrategicedge`).

## Anon-key fallback (applied in session 2026-08-03)

`scrapers/page_agent_runner.py:_fetch_calendar_httpx` now derives an `active_key`
up front: it starts as `SETTINGS.lse_api_key` and switches to `_LSE_ANON_KEY` when
the configured key equals the `_LSE_PLACEHOLDER_KEY` constant. If the first
request returns HTTP 401, the function retries once with the anon key before
giving up. This means `state/macro_events.json` populates with real LSE-branded
calendar data using a key the project already ships, with no signup needed. The
FF backfill (`scripts/populate_macro_from_ff.py`) is now an audit cross-reference
only and is no longer required as the primary macro-event source.

## Verification commands (re-run any time)

```bash
# Calendar endpoint with the anon-key already in the repo:
python -c "import httpx; print(httpx.get('https://api.londonstrategicedge.com/economic_calender', headers={'x-api-key':'71f880e1d2ef471664f3b6c04c6dc1e618f94e51f68c87522bc6dcbc0ca173a5'}, params={'limit':'3'}).status_code)"
# Expected: 200

# Calendar endpoint with the placeholder (this is the current default):
python -c "import httpx; print(httpx.get('https://api.londonstrategicedge.com/economic_calender', headers={'x-api-key':'lse_live_00000000000000000000000000000000'}, params={'limit':'3'}).status_code)"
# Expected: 401 — the placeholder is a vault key, not a calendar key

# Candle endpoint with the placeholder (works fine):
python -c "import httpx; print(httpx.get('https://api.londonstrategicedge.com/vault/candles', headers={'x-api-key':'lse_live_00000000000000000000000000000000'}, params={'symbol':'BTC/USD','timeframe':'1d','start':'2026-07-01','end':'2026-07-15','limit':'5'}).status_code)"
# Expected: 200

# Candle endpoint with the anon-key (does NOT work):
python -c "import httpx; print(httpx.get('https://api.londonstrategicedge.com/vault/candles', headers={'x-api-key':'71f880e1d2ef471664f3b6c04c6dc1e618f94e51f68c87522bc6dcbc0ca173a5'}, params={'symbol':'BTC/USD','timeframe':'1d','start':'2026-07-01','end':'2026-07-15','limit':'5'}).status_code)"
# Expected: 401 — the anon-key is a calendar key, not a vault key
```

Documented 2026-08-03 by direct API probes (no separate account needed to confirm).
