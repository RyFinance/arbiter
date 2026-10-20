# Macro Calendar Source Verification

Verified on 2026-08-04 using unauthenticated `GET` requests where possible. This is
an operational-source note, not permission to trade or a claim that either vendor is
fit for an unattended production dependency.

## JBlanked News Calendar

- Documentation: <https://www.jblanked.com/news/api/docs/calendar/>.
- Endpoint tested: `GET https://www.jblanked.com/news/api/forex-factory/calendar/week/`.
- Unauthenticated result: `401 Unauthorized`, `WWW-Authenticate: Bearer`, with
  `{"message":"Either no API key was provided or the API key does not match any in our database."}`.
- Authentication: `Authorization: Api-Key YOUR_API_KEY`; keys are created in the
  user profile: <https://www.jblanked.com/api/key/>.
- Documented limit/pricing: one request per second and one free request per day.
  The Calendar page says one credit per premium use; the changelog says credits or
  membership are required beyond the free daily allowance. Confirm account-specific
  entitlement in billing: <https://www.jblanked.com/api/billing/>.
- Reliability: its FAQ says Forex Factory week data is fetched day by day, so
  `/calendar/week/` only includes data through the current date. It documents GMT+3
  as the default event timezone and refreshes at 19:01 and 00:00 EST, plus on
  pre-processed events: <https://www.jblanked.com/news/api/docs/faqs/>.
- Terms: the Calendar docs exposed no API terms, redistribution, caching, or Forex
  Factory sublicensing policy during this verification. Do not assume those rights.

`scripts/populate_macro_from_jblanked.py` is therefore key-gated and does not
overwrite `state/macro_events.json` unless `--write-state PATH` is explicit.

```bash
JBLANKED_API_KEY=... python scripts/populate_macro_from_jblanked.py \
  --compare-ff --report-json state/jblanked_ff_comparison.json
```

The report compares the actual current UTC week by normalized HIGH-event title and
UTC date, printing agreement plus FF-only and JBlanked-only events. A live comparison
was not possible here: no JBlanked key was configured and the endpoint returned 401.

## LSE Access Paths

| Path | Header used in verification | Result |
| --- | --- | --- |
| `GET https://api.londonstrategicedge.com/economic_calender?limit=3` | SPA anonymous calendar key | `200`; sample records were dated 2026-06-17 |
| Same calendar path | repository vault placeholder key | `401`, `{"error":"Unauthorized"}` |
| `GET https://api.londonstrategicedge.com/vault/candles?...` | repository vault placeholder key | `200`; returned BTC/USD candles |
| Same candle path | SPA anonymous calendar key | `401`, `{"detail":"invalid api key"}` |

Calendar and candle access are distinct. The calendar `200` response was materially
behind the verification date, so reachability alone does not establish freshness.

The public pages <https://londonstrategicedge.com/calendar/>,
<https://londonstrategicedge.com/pricing>, and
<https://londonstrategicedge.com/signup> each returned `200`. The calendar page says
it is free with no login required. Pricing and signup returned SPA shell/home metadata
to a non-JavaScript request, so no authoritative tier or endpoint-scope schedule was
verifiable. Homepage metadata advertises one free key and no paywall, but that does
not establish `/vault/export` or other restricted scopes.

## Forex Factory Refresh Recommendation

Keep the public FF feed primary. Refresh hourly and run a separate staleness check;
the writer stores top-level `source: forex_factory_public`, and `--check-stale` emits
a warning when `last_update` exceeds 24 hours.

```cron
7 * * * * cd ~/polymarket_arb && /usr/bin/env python scripts/populate_macro_from_ff.py >> state/macro_ff_refresh.log 2>&1
20 */6 * * * cd ~/polymarket_arb && /usr/bin/env python scripts/populate_macro_from_ff.py --check-stale >> state/macro_ff_refresh.log 2>&1
```

The staleness command is informational and returns zero even when stale; route its
`WARNING` output through the host's normal monitoring mechanism.

## Alternative macro calendar sources (documented, NOT implemented)

### Financial Modeling Prep economic calendar

- **URL:** `https://financialmodelingprep.com/api/v3/economic_calendar`
- **Auth:** `apikey` query parameter (or `Authorization: Bearer <key>` header).
  Free tier at <https://site.financialmodelingprep.com/developer/docs> (250 req/min).
- **No-key behavior:** `HTTP 401` with
  `{"Error Message": "Invalid API KEY. Feel free to create a Free API Key or
  visit https://site.financialmodelingprep.com/faqs?search=why-is-my-api-key-invalid
  for more information."}`.
- **Schema (with key):** JSON array of event objects with `event`, `date`,
  `impact` (High/Medium/Low strings), `actual`, `forecast`, `previous`,
  `currency`, `change`, `changePercentage` — maps cleanly to the `MacroEvent`
  schema. The existing `classify_impact` function already handles High/Medium/Low
  string ratings (see `page_agent_runner.py:205-213`), so this would be a
  drop-in source swap with minimal parser changes.
- **Status:** Documented in README "Macro calendar fallback sources"; NOT
  implemented.
