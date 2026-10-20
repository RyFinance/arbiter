# PM CANVAS — tabbed terminal dashboard

Black-background, terminal-style canvas dashboard for the polymarket_arb
project. The 15 panels are organised into **five top-level tabs** so the
dashboard stays uncluttered — each tab shows only its own group:

| Tab | Panels |
|---|---|
| **BOT** | 00 bot mind (live thoughts + **all-timeframe window grid**) · 01-07 **compact summary cards** (execution, depth, alerts, models, latency, gates — click any card for the full detail in a modal) · 08 BTC sentiment gauge · 02 PnL chart · 14 bot control |
| **LOOP BOT** | 15 isolated paper loop bot (**AUTO/MANUAL mode**, live-config hot-reload) · 16 decision chart (BTC + enter/exit markers) · 17 loop events wire |
| **MARKETS** | 08 cross-asset regime · 09 global indices · 10 sector heatmap |
| **NEWS** | 11 headlines wire — Massive v2 news + Finnhub world news + X/DeItaone feed |
| **SOCIAL** | 12 social feed — Reddit and X combined into one panel with a REDDIT/X source toggle and a REFRESH NOW button |
| **CHARTS** | 13 chart workspace with a **Finnhub symbol search box** (type-ahead suggestions) |

The active tab persists in `localStorage` (key `pm-canvas-tab-v4`).
Panels are **draggable** (grab the header) and **resizable** (bottom-right
corner); positions and stacking persist in `localStorage` (key
`pm-canvas-layout-v4`). `RESET LAYOUT` in the command bar clears the saved
layout + tab and restores the default grid (a reset guard prevents the
beforeunload handler from resurrecting the old layout during reload).

A low-opacity rotating dotted globe renders behind the panels
(`landmask.js` — an embedded 180×90 land-mask derived once from the
public-domain world-atlas land-110m dataset; no runtime CDN).

**No demo data anywhere.** Every panel is a live adapter over the
project's real state files and the external provider APIs, all served by
the backend. Empty, stale, or quota-blocked sources are shown honestly in
the terminal style — never fabricated.

## Panels

| # | Panel | Backend source | Real data shown |
|---|---|---|---|
| — | Command bar (fixed) | `/api/command` | connection freshness, `PAPER_TRADE` mode, kill-switch state, live UTC clock, session elapsed + loops |
| 00 | **BOT MIND** (centerpiece) | `/api/botmind` + `/api/windows` | **live narrative of the bot**: status/verdict/funnel + the **multi-timeframe window grid** — one row per timeframe (5m/15m/30m) with the live YES/NO best asks, combined, best-combined, and loop-bot position, refreshed every 5 s from the public CLOB books (read-only) |
| 01-07 | **Summary cards** (execution · depth · alerts · models · latency · gates) | `/api/heatmap` `/api/depth` `/api/alerts` `/api/models` `/api/latency` `/api/checklist` | each card shows just the headline number (fills, windows complete %, alert count + latest kind, model pass count, opp→order p50, gates passed) — **click a card to open the full detail table/log in a modal** (ESC or × closes; live-updating while open). Same data as before, decluttered |
| 08 | BTC sentiment gauge | `/api/btc_sentiment` | fear/greed-style gauge (0-100) **derived from real market data** — yfinance BTC-USD daily closes: 30d momentum (45%), distance from 50d SMA (35%), annualized realized vol (20%) — with the BTC price and each component labeled. It is a market proxy, labeled as such, never survey sentiment |
| 08 | Cross-asset regime (LSE) | `/api/lse_cross_asset` | BTC/USD vs ETH/XAU/EUR/GBP/AAPL/TSLA/NVDA: last close, day %, **30-day Pearson correlation vs BTC** from LSE vault daily candles |
| 09 | Global indices (Massive) | `/api/indices` | SPY/DIA/QQQ/IWM day change via Massive v2 aggs — free-tier key is NOT entitled to the real v3 index snapshot, so the panel shows ETF proxies and says so |
| 10 | Sector heatmap (LSE) | `/api/sector_heatmap` | **10 real sectors, 83 tickers** (Semiconductors, Tech, EV/Auto, Financials, Energy, Healthcare, Industrials, Minerals/Materials, Consumer, Macro/Cross-asset incl. USD/JPY, AUD/USD, USD/CAD); sector-average and per-stock day-change % as green/red intensity chips |
| 11 | Headlines wire | `/api/news_wire` | live Massive v2 news + **Finnhub world news** (category=general, cached 120 s) + **@DeItaone live** (x.com profile-page scrape, see below) |
| 12 | Social feed (Reddit ⇄ X) | `/api/social` | **one panel, two sources** — toggle REDDIT/X in the toolbar. Reddit: hot posts from reddit.com's **public JSON with the saved `reddit_session` cookie** (rdt-cli method, see below), cached 90 s, throttled-with-backoff, scraper file only as last resort. X: **live x.com profile-page scrape** — plain browser-UA GET of `https://x.com/DeItaone`, logged-out server-rendered HTML with the newest tweets, no API key, no session (see below). REFRESH NOW forces an immediate re-poll |
| 13 | Chart workspace | `/api/ohlcv` + `/api/company_news` + `/api/finnhub_search` | **full chart workspace** — candlesticks/bars/line/area across 1m–1W (yfinance, no API key), SMA/EMA/Bollinger/VWAP overlays, volume/RSI/MACD sub-panels, crosshair OHLC readout, wheel-zoom, watchlist, LIGHT/DARK theme, 60 s auto-refresh, and a **company/ETF news feed** under the chart (Finnhub, user's key is entitled). Search any ticker or click any ticker anywhere on the dashboard |
| 14 | Bot control | `/api/botctl/*` | **full paper-session lifecycle**: START/STOP the sealed paper session runner with a saved risk profile (order size, min edge, max position/market, max total capital, daily kill switch, no-entry window, quote age), live session metrics, session log tail, and an emergency-stop file. **PAPER TRADE ONLY** — real orders impossible; refuses to start while another paper bot runs |
| 15 | **Loop bot control** (isolated paper) | `/api/loopbot/*` | **closed-loop paper test bot, fully isolated from the main bot**: watches ALL updown timeframes at once (5m/15m/30m), one paper entry per window when YES+NO < 1 − edge, both legs held to close. **AUTO/MANUAL mode switch**: AUTO uses pre-tuned defaults with the fields locked; MANUAL unlocks order size / min edge / no-entry seconds / timeframes, and APPLY CONFIG **hot-reloads the running bot** (no restart). **ROI + risk-adjusted ROI counters** (settled trades): plain ROI = realized ÷ capital deployed, risk-adjusted = Sharpe-style per-trade return ÷ per-trade std, with trade count, mean return and total cost shown. Per-timeframe live table + isolated PnL in `state/loop_bot/` |
| 16 | Decision chart | `/api/loopbot/chart` | **live chart of every decision**: BTC/USD spot line (Finnhub, 30 s samples) with ▲ enter markers colored per timeframe (5m amber / 15m blue / 30m purple) and □ exit markers labeled with the realized PnL; per-timeframe entry/exit/PnL legend |
| 17 | Loop events wire | `/api/loopbot/events` | every loop-bot event: btc samples, watches, edges, enters (combined, size, locked PnL), exits (realized PnL), errors |

**Note on timeframes:** Polymarket only publishes `btc-updown-5m` and
`btc-updown-15m` rolling windows (verified against Gamma 2026-08-17).
The 30m checkbox is kept because it's part of the requested watchlist —
the row honestly shows `resolving…` while no 30m market exists, and would
start trading automatically the moment Polymarket publishes one.

**Global min-edge:** the whole system now requires **YES+NO < 0.97**
(a ≥3% gross edge to cover fees). This is applied consistently across the
`.env` default, `polymarket_arb/config.py`, the main-bot dashboard risk
profile, the loop bot defaults, the runner, and the BOT MIND verdict text
— not just one panel. (Core-bot test fixtures were updated to the same
threshold; 788 tests pass.)

Panel title-bar buttons are functional: **–** minimizes to the header
(click again to restore), **□** maximizes to the full canvas (again to
restore), **×** closes the panel (persisted; `RESTORE PANELS` in the
command bar re-opens closed panels, `RESET LAYOUT` restores the default
grid). Button state (minimized/closed/maximized) survives reloads via the
same localStorage layout.

Each panel header carries a live data-age badge: `LIVE` (green, <10 s),
`STALE` (amber), `OFFLINE` (red).

## Keys and rate limits

All external keys live in the project `.env` (gitignored — never
hardcoded, never committed):

```
LSE_API_KEY=...
MASSIVE_API_KEY=...
FINNHUB_API_KEY=...
```

- **Massive free tier: 5 API calls/minute** — the backend enforces a
  shared token bucket; news fetches one ticker per call on rotation,
  aggs are cached 300 s. On exhaustion, panels serve the stale cache and
  show the quota state (`quota: n/5 left this minute`).
- **LSE vault** candles are cached 300 s per symbol (multi-asset: crypto,
  FX, commodities, US equities all verified working with this key).
- **Finnhub** (search, quote, company + general news — free tier entitled):
  news cached 120 s; search is called only while typing in the chart search
  box. Daily candles are used when the key is entitled and fall back to
  the LSE series otherwise.
- **yfinance** (chart candles, no API key): powers the chart workspace's
  OHLCV across all resolutions (1m–1W); company/ETF news comes from
  Finnhub's `/company-news` (the dashboard key is entitled).
- **Reddit** (via the `reddit_session` cookie from `~/.config/rdt-cli/credential.json`, rdt-cli): Reddit's anonymous `.json` endpoints are 403 anti-bot-blocked, but the same **public JSON endpoints return 200 with a browser User-Agent + a session cookie captured once from a browser login** — no API key, no OAuth app registration (this is exactly how sentiment_oldft's Agent-Reach `rdt sub` works). Cached 90 s with exponential backoff on 429/403 — the panel keeps serving the last live snapshot while throttled and recovers automatically.
- **X/DeItaone** (live profile-page scrape): X's **logged-out profile HTML** embeds the newest tweets server-side (`article[data-tweet-id]` with text + ISO timestamps). A plain browser-UA GET of `https://x.com/DeItaone` parses them — no API key, no session cookie, no rate limit. This is the mechanism the sentiment project's launchd monitor (`monitor_deItaone.sh`, `com.user.monitordeItaone`) uses to push an iMessage on every new DeItaone post. Cached 120 s; the old `news_feed.json` scraper file is only a labeled fallback when x.com is unreachable.

## Run

```bash
cd ~/polymarket_arb
.venv/bin/python -m uvicorn dashboard_js.server:app --host 127.0.0.1 --port 8765
```

Open http://127.0.0.1:8765/canvas/ (the grid dashboard remains at `/`).

Backend contract, readers, parsers, and providers live in `dashboard_js/`
(`server.py`, `readers.py`, `parsers.py`, `providers.py`, `envs.py`).
All static assets here are local — **zero CDN dependencies**.

## Notes

- `/api/depth` computes live continuity from the newest shard (cached
  120 s); the full-history scan runs in a background thread and is served
  from a sidecar (`state/cache/depth_health_sidecar.json`, 10-min TTL).
- The DeItaone/X and Reddit panels were rebuilt on the **key-free scraper
  mechanisms proven in the sibling sentiment projects**: X via logged-out
  profile-page scraping (what the sentiment project's DeItaone monitor
  uses), Reddit via the public JSON + rdt-cli session cookie (what
  sentiment_oldft's `rdt sub` uses). Both stream live and show their real
  last-fetch timestamps; neither needs an X or Reddit API key, so they
  don't hit official-API rate limits.
- The loop bot can run **simultaneously** with the main paper session bot:
  both are paper-only simulations with fully separate state, PnL, and stop
  files (`state/loop_bot/STOP` vs `state/EMERGENCY_STOP`).
- Tests: `.venv/bin/python -m pytest tests/test_dashboard_canvas_static.py tests/test_dashboard_js_providers.py tests/test_dashboard_js_api.py tests/test_dashboard_js_readers.py tests/test_dashboard_js_parsers.py tests/test_loop_bot.py -q`
