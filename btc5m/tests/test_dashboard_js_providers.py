"""Unit tests for the external data providers (network fully mocked).

No test in this module touches the real network: every HTTP layer is
monkeypatched.  Provider payloads are tested against synthetic fixtures in
the exact shapes the real APIs return.
"""
from __future__ import annotations

import json

import pytest

from dashboard_js import envs, providers


@pytest.fixture(autouse=True)
def clear_provider_state(monkeypatch):
    monkeypatch.setattr(providers, "_CACHE", {})
    monkeypatch.setattr(providers, "_BUCKET", {"lock": providers._BUCKET["lock"], "stamps": []})
    monkeypatch.setattr(providers, "_NEWS_ROTATION", {"lock": providers._NEWS_ROTATION["lock"], "index": 0})
    monkeypatch.setattr(providers, "_REDDIT_STATE", {"lock": providers._REDDIT_STATE["lock"], "fail_until": 0.0, "consecutive": 0})
    # hermetic rdt credential: a fake reddit_session cookie so reddit tests
    # never depend on the operator's real ~/.config/rdt-cli/credential.json
    monkeypatch.setattr(
        providers,
        "RDT_CREDENTIAL_PATH",
        type("FakePath", (), {"read_text": lambda self, encoding="utf-8": json.dumps(
            {"cookies": {"reddit_session": "test-session-cookie"}}
        )})(),
    )
    monkeypatch.setattr(envs, "_VALUES", {})
    yield


def test_env_loader_reads_dotenv_keys(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("LSE_API_KEY=lse_test_123\nMASSIVE_API_KEY=massive_test_456\n# comment\n")
    monkeypatch.setattr(envs, "_ENV_FILE", env_file)
    monkeypatch.setattr(envs, "_VALUES", {})
    monkeypatch.delenv("LSE_API_KEY", raising=False)
    monkeypatch.delenv("MASSIVE_API_KEY", raising=False)

    assert envs.lse_api_key() == "lse_test_123"
    assert envs.massive_api_key() == "massive_test_456"
    assert envs.get("MISSING") is None


def test_missing_key_yields_clear_error(tmp_path, monkeypatch):
    monkeypatch.setattr(envs, "_ENV_FILE", tmp_path / "no-such.env")
    monkeypatch.setattr(envs, "_VALUES", {})
    monkeypatch.delenv("MASSIVE_API_KEY", raising=False)
    data, error, quota = providers.massive_get("/v2/reference/news", {"ticker": "NVDA"}, "k", 60.0)

    assert data is None
    assert error == "MASSIVE_API_KEY not set in .env"
    assert quota["limit_per_min"] == 5


def test_pearson_hand_calculated():
    xs = [1.0, 2.0, 3.0, 4.0, 5.0]
    ys = [2.0, 4.0, 6.0, 8.0, 10.0]

    assert providers._pearson(xs, ys) == pytest.approx(1.0)

    assert providers._pearson([1.0, 2.0], [2.0, 4.0]) is None  # <3 pairs
    assert providers._pearson([1.0, 1.0, 1.0], [1.0, 2.0, 3.0]) is None  # zero variance


def test_bucket_enforces_five_per_minute(monkeypatch):
    monkeypatch.setattr(providers, "_BUCKET", {"lock": providers._BUCKET["lock"], "stamps": []})

    allowed = [providers._bucket_allow() for _ in range(providers.MASSIVE_CALLS_PER_MIN)]
    sixth = providers._bucket_allow()

    assert all(allowed)
    assert sixth is False
    state = providers.bucket_state()
    assert state["remaining_this_minute"] == 0
    assert state["next_free_in_s"] is not None


def test_lse_candles_parse_and_cache(monkeypatch):
    monkeypatch.setattr(envs, "_VALUES", {"LSE_API_KEY": "test"})
    fake_rows = [
        {"ts": "2026-08-12 00:00:00.000000", "symbol": "BTC/USD", "close": 100.0},
        {"ts": "2026-08-13 00:00:00.000000", "symbol": "BTC/USD", "close": 105.0},
    ]
    calls = []

    def fake_http(url, headers=None):
        calls.append(url)
        return json.loads(json.dumps(fake_rows)), None

    monkeypatch.setattr(providers, "_http_get_json", fake_http)
    rows, error = providers.lse_daily_candles("BTC/USD", days=60)
    assert error is None
    assert [row["close"] for row in rows] == [100.0, 105.0]
    assert "start=" in calls[0] and "end=" in calls[0] and "timeframe=1d" in calls[0]

    rows_again, _ = providers.lse_daily_candles("BTC/USD", days=60)
    assert rows_again[0]["close"] == 100.0
    assert len(calls) == 1  # cached


def test_lse_cross_asset_correlation_payload(monkeypatch):
    monkeypatch.setattr(envs, "_VALUES", {"LSE_API_KEY": "test"})

    def fake_candles(symbol, days=60):
        days = 40
        base = {"BTC/USD": [10 + i for i in range(days)]}
        if symbol == "BTC/USD":
            closes = base[symbol]
        elif symbol == "ETH/USD":
            closes = [20 + 2 * i for i in range(days)]  # perfect positive corr
        elif symbol == "EUR/USD":
            closes = [20 - 2 * i for i in range(days)]  # perfect negative corr
        else:
            return None, "no such symbol"
        return [{"ts": f"2026-07-{1 + i % 28:02d}", "ts_epoch": None, "close": float(c)} for i, c in enumerate(closes)], None

    monkeypatch.setattr(providers, "lse_daily_candles", fake_candles)
    payload = providers.lse_cross_asset()

    items = {item["symbol"]: item for item in payload["items"]}
    assert items["ETH/USD"]["corr_vs_btc_30d"] == pytest.approx(1.0)
    assert items["EUR/USD"]["corr_vs_btc_30d"] == pytest.approx(-1.0)
    assert items["BTC/USD"]["corr_vs_btc_30d"] == 1.0
    assert payload["matrix"]["window"]


def test_massive_news_rotates_and_merges(monkeypatch):
    monkeypatch.setattr(envs, "_VALUES", {"MASSIVE_API_KEY": "test"})
    fetched = []

    def fake_massive_get(path, params, cache_key, ttl):
        fetched.append((path, params["ticker"]))
        data = {"results": [{"id": f"id-{params['ticker']}", "title": f"Headline {params['ticker']}",
                             "published_utc": "2026-08-15T12:00:00Z",
                             "publisher": {"name": "Test Wire"}, "article_url": "https://x"}]}
        providers._cache_put(cache_key, data, ttl)
        return (
            data,
            None,
            {"limit_per_min": 5, "remaining_this_minute": 4, "next_free_in_s": None},
        )

    monkeypatch.setattr(providers, "massive_get", fake_massive_get)
    first = providers.massive_news()
    second = providers.massive_news()

    assert first["items"][0]["publisher"] == "Test Wire"
    assert first["last_ticker_fetched"] != second["last_ticker_fetched"]  # rotation
    assert fetched[0][0] == "/v2/reference/news"
    # merged cache grows
    assert len(second["items"]) >= len(first["items"])


def test_reddit_feed_parses_real_format(tmp_path, monkeypatch):
    monkeypatch.setattr(providers, "SENTIMENT_DIR", tmp_path)
    monkeypatch.setattr(providers, "OLD_SENTIMENT_DIR", tmp_path / "missing")
    raw = {
        "scraped_at": "2026-07-22T04:51:21.961455+00:00",
        "total_posts": 3,
        "sources": {"reddit": 3},
        "posts": [
            {"id": "a", "title": "Top post", "score": 500, "num_comments": 40, "subreddit": "wallstreetbets", "created_utc": 1784311045.0},
            {"id": "b", "title": "Low post", "score": 3, "num_comments": 1, "subreddit": "stocks", "created_utc": 1784311045.0},
            {"id": "c", "title": "Mid post", "score": 50, "num_comments": 5, "subreddit": "investing", "created_utc": 1784311045.0},
        ],
    }
    (tmp_path / "raw_data.json").write_text(json.dumps(raw))
    payload = providers.read_reddit_feed()

    assert payload["stale"] is True
    assert payload["last_run_utc"].startswith("2026-07-22")
    assert [item["score"] for item in payload["items"]] == [500, 50, 3]
    assert payload["items"][0]["subreddit"] == "wallstreetbets"
    assert payload["total_posts"] == 3


def test_reddit_feed_missing_files_is_honest(tmp_path, monkeypatch):
    monkeypatch.setattr(providers, "SENTIMENT_DIR", tmp_path)
    monkeypatch.setattr(providers, "OLD_SENTIMENT_DIR", tmp_path / "missing")
    payload = providers.read_reddit_feed()

    assert payload["items"] == []
    assert "no reddit scraper output" in payload["error"]


def test_deitaone_feed_live_scrape_is_primary(tmp_path, monkeypatch):
    """The live x.com profile scrape wins; the scraper file is never read when it works."""
    monkeypatch.setattr(providers, "SENTIMENT_DIR", tmp_path)
    monkeypatch.setattr(providers, "OLD_SENTIMENT_DIR", tmp_path / "missing")
    (tmp_path / "news_feed.json").write_text(json.dumps({"status": "ok", "items": []}))
    html = (
        '<article data-tweet-id="2089099642537443673" itemType="https://schema.org/SocialMediaPosting">'
        "*Walter Bloomberg <path d=\"M20.396 11c-.018\"></path>"
        "<time datetime=\"2026-08-16T21:19\"></time>"
        "<div>TRUMP ORDERS REDUCTION IN SOUTH KOREA DRILLS</div>"
        "<div>President Trump says he has instructed Defense Secretary</div>"
        "</article>"
        '<article data-tweet-id="2088900782032617818">'
        "<time datetime=\"2026-08-16T08:08\"></time>"
        "<div>NATO MILITARY HEADQUARTERS SPOKESPERSON ON DRONE</div>"
        "<div>Drone appears to be Russian</div>"
        "</article>"
    )
    monkeypatch.setattr(providers, "_http_get_text", lambda url, headers=None: (html, None))
    payload = providers.read_deitaone_feed()

    assert payload["status"] == "ok"
    assert payload["live"] is True
    assert payload["items"][0]["text"].startswith("TRUMP ORDERS REDUCTION")
    assert payload["items"][0]["ts_utc"] == "2026-08-16T21:19"
    assert payload["items"][0]["id"] == "2089099642537443673"
    assert payload["items"][1]["text"].startswith("NATO MILITARY")
    assert "profile" in payload["source"].lower()


def test_deitaone_feed_falls_back_to_file_when_live_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(providers, "SENTIMENT_DIR", tmp_path)
    monkeypatch.setattr(providers, "OLD_SENTIMENT_DIR", tmp_path / "missing")
    feed = {
        "scraped_at_iso": "2026-07-16T07:57:57.847847+00:00",
        "status": "ok",
        "reason": "15 headlines",
        "items": [{"id": "1", "text": "HEADLINE ONE\nbody", "time_iso": "2026-07-15T16:06:07.000Z", "url": "https://x.com/DeItaone/1"}],
    }
    (tmp_path / "news_feed.json").write_text(json.dumps(feed))
    monkeypatch.setattr(providers, "_http_get_text", lambda url, headers=None: (None, "HTTP 403"))
    payload = providers.read_deitaone_feed()

    assert payload["live"] is False
    assert payload["items"][0]["text"].startswith("HEADLINE ONE")
    assert payload["scraped_at_utc"].startswith("2026-07-16")
    assert "stale" in payload["reason"].lower() or "fallback" in payload["reason"].lower()


def test_lse_heatmap_groups_sectors(monkeypatch):
    monkeypatch.setattr(envs, "_VALUES", {"LSE_API_KEY": "test"})

    def fake_candles(symbol, days=60):
        base = abs(hash(symbol)) % 100
        rows = []
        for i in range(30):
            rows.append({"ts": f"2026-07-{1 + i % 28:02d}", "ts_epoch": None, "close": float(base + i * 0.5)})
        return rows, None

    monkeypatch.setattr(providers, "lse_daily_candles", fake_candles)
    payload = providers.lse_heatmap()

    sector_names = [sector["sector"] for sector in payload["sectors"]]
    assert "SEMICONDUCTORS" in sector_names
    assert "MINERALS / MATERIALS" in sector_names
    semis = next(s for s in payload["sectors"] if s["sector"] == "SEMICONDUCTORS")
    assert {"NVDA", "AMD", "AVGO", "QCOM", "INTC", "MU", "MRVL", "LRCX", "KLAC", "AMAT", "TXN"} <= {item["symbol"] for item in semis["items"]}
    assert semis["change_pct"] is not None
    assert all("close" in item and "change_pct" in item for item in semis["items"])


def test_stock_chart_merges_lse_series_and_finnhub_quote(monkeypatch):
    monkeypatch.setattr(envs, "_VALUES", {"LSE_API_KEY": "test", "FINNHUB_API_KEY": "fkey"})
    rows = [{"ts": f"2026-08-{1 + i:02d}", "ts_epoch": None, "close": 100.0 + i} for i in range(10)]
    monkeypatch.setattr(providers, "lse_daily_candles", lambda symbol, days=60: (rows, None))
    captured = {}

    def fake_http(url, headers=None):
        captured["url"] = url
        return {"c": 110.5, "d": 1.2, "dp": 1.098, "h": 111.0, "l": 108.0, "o": 109.0, "pc": 109.3, "t": 1786737600}, None

    monkeypatch.setattr(providers, "_http_get_json", fake_http)
    payload = providers.stock_chart("NVDA")

    assert len(payload["items"]) == 10
    assert payload["quote"]["c"] == 110.5
    assert "symbol=NVDA" in captured["url"], "finnhub symbol must be the full ticker, not a prefix"
    assert "finnhub.io" in captured["url"]
    assert len(payload["sources"]) == 2


def test_stock_chart_finnhub_candles_not_entitled_path_uses_lse_only(monkeypatch):
    monkeypatch.setattr(envs, "_VALUES", {"LSE_API_KEY": "test", "FINNHUB_API_KEY": "fkey"})
    rows = [{"ts": f"2026-08-{1 + i:02d}", "ts_epoch": None, "close": 50.0 + i} for i in range(5)]
    monkeypatch.setattr(providers, "lse_daily_candles", lambda symbol, days=60: (rows, None))

    def fake_http(url, headers=None):
        return None, "HTTP 403 You don't have access to this resource."

    monkeypatch.setattr(providers, "_http_get_json", fake_http)
    payload = providers.stock_chart("NVDA")

    assert len(payload["items"]) == 5
    assert payload["quote"] is not None
    assert payload["quote"]["_source"] == "lse_series"  # falls back to the series close
    assert "403" in payload["quote_error"]


def test_finnhub_quote_missing_key(monkeypatch, tmp_path):
    monkeypatch.setattr(envs, "_ENV_FILE", tmp_path / "no.env")
    monkeypatch.setattr(envs, "_VALUES", {})
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    payload = providers.finnhub_quote("NVDA")

    assert payload["quote"] is None
    assert payload["error"] == "FINNHUB_API_KEY not set in .env"


def test_finnhub_search_parses_results_and_filters_symbols(monkeypatch):
    monkeypatch.setattr(envs, "_VALUES", {"FINNHUB_API_KEY": "fkey"})
    captured = {}

    def fake_http(url, headers=None):
        captured["url"] = url
        return {
            "count": 2,
            "result": [
                {"symbol": "AAPL", "description": "Apple Inc", "type": "Common Stock"},
                {"symbol": "BRK.B", "description": "Berkshire", "type": "Common Stock"},
            ],
        }, None

    monkeypatch.setattr(providers, "_http_get_json", fake_http)
    payload = providers.finnhub_search("appl")

    assert [item["symbol"] for item in payload["items"]] == ["AAPL"], "BRK.B must be filtered out"
    assert "token=" in captured["url"]
    assert "q=appl" in captured["url"]


def test_finnhub_search_missing_key(monkeypatch, tmp_path):
    env_file = tmp_path / "no.env"
    env_file.write_text("MASSIVE_API_KEY=x\n")
    monkeypatch.setattr(envs, "_ENV_FILE", env_file)
    monkeypatch.setattr(envs, "_VALUES", {})
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    payload = providers.finnhub_search("NVDA")

    assert payload["items"] == []
    assert "FINNHUB_API_KEY" in payload["error"]


def test_finnhub_news_builds_items(monkeypatch):
    monkeypatch.setattr(envs, "_VALUES", {"FINNHUB_API_KEY": "fkey"})

    def fake_http(url, headers=None):
        return [{"headline": "World markets rally", "source": "Reuters", "url": "https://x", "datetime": 1786737600}], None

    monkeypatch.setattr(providers, "_http_get_json", fake_http)
    payload = providers.finnhub_news()

    assert payload["items"][0]["title"] == "World markets rally"
    assert payload["items"][0]["publisher"] == "Reuters"
    assert payload["items"][0]["ts_utc"].startswith("2026")


def test_news_wire_includes_finnhub_section(monkeypatch):
    monkeypatch.setattr(envs, "_VALUES", {"FINNHUB_API_KEY": "fkey"})
    monkeypatch.setattr(providers, "massive_news", lambda: {"items": [], "error": "quota"})
    monkeypatch.setattr(providers, "read_deitaone_feed", lambda: {"items": [], "status": "missing", "reason": "none"})

    def fake_http(url, headers=None):
        return [{"headline": "h", "source": "s", "url": "u", "datetime": 1786737600}], None

    monkeypatch.setattr(providers, "_http_get_json", fake_http)
    payload = providers.news_wire()

    assert "finnhub" in payload
    assert payload["finnhub"]["items"]


def test_social_feed_uses_live_reddit_and_deitaone(monkeypatch):
    monkeypatch.setattr(envs, "_VALUES", {"FINNHUB_API_KEY": "fkey"})

    def fake_http(url, headers=None):
        if "reddit.com" in url:
            return {
                "data": {
                    "children": [
                        {"data": {"id": "p1", "title": "Hot post", "subreddit": "wallstreetbets", "score": 5000, "num_comments": 100}},
                    ]
                }
            }, None
        return None, "unexpected url"

    monkeypatch.setattr(providers, "_http_get_json", fake_http)
    monkeypatch.setattr(providers, "read_deitaone_feed", lambda: {"items": [], "status": "missing"})
    payload = providers.social_feed()

    assert payload["reddit"]["live"] is True
    assert payload["reddit"]["items"][0]["title"] == "Hot post"
    assert "x" in payload


def test_social_feed_reddit_failure_serves_last_good_snapshot(monkeypatch):
    monkeypatch.setattr(envs, "_VALUES", {"FINNHUB_API_KEY": "fkey"})
    calls = {"n": 0}

    def fake_http(url, headers=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return {
                "data": {"children": [{"data": {"id": "p1", "title": "First fetch", "subreddit": "stocks", "score": 10}}]}
            }, None
        return None, "HTTP 429 Too Many Requests"

    monkeypatch.setattr(providers, "_http_get_json", fake_http)
    monkeypatch.setattr(providers, "read_deitaone_feed", lambda: {"items": [], "status": "missing"})
    first = providers.social_feed()
    assert first["reddit"]["live"] is True

    # simulate TTL expiry while the last payload stays in the cache:
    monkeypatch.setattr(providers, "_cache_fresh", lambda key: False)
    second = providers.social_feed()

    assert second["reddit"]["degraded"] is True
    assert second["reddit"]["items"][0]["title"] == "First fetch"


def test_finnhub_candles_returns_none_when_not_entitled(monkeypatch):
    monkeypatch.setattr(envs, "_VALUES", {"FINNHUB_API_KEY": "fkey"})

    def fake_http(url, headers=None):
        return None, "HTTP 403 You don't have access to this resource."

    monkeypatch.setattr(providers, "_http_get_json", fake_http)
    assert providers.finnhub_candles("NVDA") is None


def test_finnhub_candles_builds_daily_series(monkeypatch):
    monkeypatch.setattr(envs, "_VALUES", {"FINNHUB_API_KEY": "fkey"})

    def fake_http(url, headers=None):
        return {"s": "ok", "t": [1786737600, 1786824000], "c": [100.0, 102.5]}, None

    monkeypatch.setattr(providers, "_http_get_json", fake_http)
    series = providers.finnhub_candles("NVDA")

    assert len(series) == 2
    assert series[1]["close"] == 102.5
    assert series[1]["ts_utc"] == "2026-08-15"


def test_stock_chart_prefers_finnhub_candles_when_entitled(monkeypatch):
    monkeypatch.setattr(envs, "_VALUES", {"LSE_API_KEY": "test", "FINNHUB_API_KEY": "fkey"})
    monkeypatch.setattr(
        providers,
        "finnhub_candles",
        lambda symbol, days=60: [{"ts_utc": "2026-08-14", "close": 99.0}, {"ts_utc": "2026-08-15", "close": 101.0}],
    )
    monkeypatch.setattr(providers, "lse_daily_candles", lambda symbol, days=60: (None, "lse down"))
    monkeypatch.setattr(providers, "finnhub_quote", lambda symbol: {"quote": {"c": 101.5, "dp": 0.5}, "source": "finnhub"})

    payload = providers.stock_chart("NVDA")

    assert len(payload["items"]) == 2
    assert payload["sources"] == ["finnhub.io candles (1d)", "finnhub"]
    assert payload["quote"]["c"] == 101.5


def test_reddit_live_uses_session_cookie_header(monkeypatch):
    """The live reddit fetch must send the rdt reddit_session cookie."""
    captured = {}

    def fake_http(url, headers=None):
        captured["headers"] = headers or {}
        return {"data": {"children": []}}, None

    monkeypatch.setattr(providers, "_http_get_json", fake_http)
    providers._live_reddit()

    assert "reddit_session=test-session-cookie" in captured["headers"]["Cookie"]
    assert captured["headers"]["Cookie"].startswith("reddit_session=")
    assert captured["headers"]["User-Agent"].startswith("Mozilla/5.0")


def test_reddit_live_missing_cookie_is_honest(monkeypatch, tmp_path):
    monkeypatch.setattr(providers, "RDT_CREDENTIAL_PATH", tmp_path / "no-credential.json")
    payload = providers._live_reddit()
    assert payload["live"] is False
    assert "reddit_session cookie" in payload["error"]


def test_x_profile_parse_filters_svg_and_boilerplate():
    html = (
        '<article data-tweet-id="111"><path d="M20.396 11c-.018"></path>'
        "<time datetime=\"2026-08-16T10:00\"></time>"
        "<div>BREAKING HEADLINE ONE ABOUT THE GLOBAL MARKETS TODAY</div>"
        "<div>Full body text continues with more details on the story</div></article>"
        '<article data-tweet-id="222">'
        "<div>See what's happening and join the conversation</div>"
        "<div>Still a real headline about market movements overnight</div></article>"
    )
    items = providers._x_profile_parse_tweets(html)
    assert len(items) == 2
    assert items[0]["id"] == "111"
    assert items[0]["text"].startswith("BREAKING HEADLINE ONE")
    assert items[0]["ts_utc"] == "2026-08-16T10:00"
    assert "See what" not in items[1]["text"]


def test_x_profile_empty_page_is_honest(monkeypatch):
    monkeypatch.setattr(providers, "_http_get_text", lambda url, headers=None: ("<html><body></body></html>", None))
    payload = providers.x_profile_timeline("DeItaone")
    assert payload["ok"] is False
    assert "0 tweets parsed" in payload["error"]


def test_x_profile_http_failure_is_honest(monkeypatch):
    monkeypatch.setattr(providers, "_http_get_text", lambda url, headers=None: (None, "HTTP 429"))
    payload = providers.x_profile_timeline("DeItaone")
    assert payload["ok"] is False
    assert "429" in payload["error"]


def test_yf_ohlcv_parses_multilevel_columns(monkeypatch):
    """yfinance returns MultiIndex columns; the provider must flatten them."""
    import pandas as pd

    index = pd.to_datetime(["2026-08-14 10:00", "2026-08-14 10:15"], utc=True)
    df = pd.DataFrame(
        {
            ("Open", "AAPL"): [100.0, 101.0],
            ("High", "AAPL"): [102.0, 103.0],
            ("Low", "AAPL"): [99.0, 100.5],
            ("Close", "AAPL"): [101.5, 102.5],
            ("Volume", "AAPL"): [1000, 2000],
        },
        index=index,
    )
    monkeypatch.setattr("yfinance.download", lambda *a, **k: df)
    payload = providers.yf_ohlcv("AAPL", "15m")
    assert payload["error"] is None
    assert len(payload["items"]) == 2
    assert payload["items"][0]["c"] == 101.5
    assert payload["items"][1]["h"] == 103.0
    assert payload["items"][1]["v"] == 2000
    assert payload["source"] == "yfinance (no API key)"


def test_yf_ohlcv_unsupported_resolution(monkeypatch):
    payload = providers.yf_ohlcv("AAPL", "13m")
    assert "unsupported resolution" in payload["error"]


def test_finnhub_company_news_builds_items(monkeypatch):
    monkeypatch.setattr(envs, "_VALUES", {"FINNHUB_API_KEY": "fkey"})

    def fake_http(url, headers=None):
        return [
            {"datetime": 1786737600, "headline": "Apple hits record", "source": "Reuters",
             "url": "https://r", "summary": "sum", "image": "https://i"},
        ], None

    monkeypatch.setattr(providers, "_http_get_json", fake_http)
    payload = providers.finnhub_company_news("AAPL")
    assert payload["items"][0]["headline"] == "Apple hits record"
    assert payload["items"][0]["ts_utc"].startswith("2026")
    assert payload["source"] == "finnhub.io company news"


def test_finnhub_company_news_missing_key(monkeypatch, tmp_path):
    env_file = tmp_path / "no.env"
    env_file.write_text("MASSIVE_API_KEY=x\n")
    monkeypatch.setattr(envs, "_ENV_FILE", env_file)
    monkeypatch.setattr(envs, "_VALUES", {})
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    payload = providers.finnhub_company_news("AAPL")
    assert payload["items"] == []
    assert "FINNHUB_API_KEY" in payload["error"]
