"""Static structural checks for the canvas dashboard frontend.

Structural only; the interactive verification (zero console errors,
drag-and-drop, localStorage persistence) is exercised by the Playwright
end-to-end script run manually against the live server.
"""
from __future__ import annotations

from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parent.parent / "dashboard_canvas" / "static"

REQUIRED_IDS = (
    "command-bar",
    "tab-bar",
    "canvas",
    "globe-canvas",
    "panel-mind",
    "panel-exec",
    "panel-pnl",
    "panel-depth",
    "panel-alerts",
    "panel-models",
    "panel-latency",
    "panel-gates",
    "panel-sentiment",
    "detail-modal",
    "panel-cross",
    "panel-indices",
    "panel-sectorheat",
    "panel-news",
    "panel-social",
    "panel-loopctl",
    "panel-loopchart",
    "panel-loopevents",
    "panel-chart",
    "panel-botctl",
)

REQUIRED_ENDPOINTS = (
    "/api/heatmap",
    "/api/pnl",
    "/api/depth",
    "/api/alerts",
    "/api/models",
    "/api/latency",
    "/api/checklist",
    "/api/command",
    "/api/lse_cross_asset",
    "/api/indices",
    "/api/sector_heatmap",
    "/api/news_wire",
    "/api/social",
    "/api/finnhub_search",
    "/api/ohlcv",
    "/api/company_news",
    "/api/botctl/status",
    "/api/botctl/start",
    "/api/botctl/stop",
    "/api/botctl/risk",
    "/api/botctl/log",
    "/api/botmind",
    "/api/windows",
    "/api/btc_sentiment",
    "/api/loopbot/status",
    "/api/loopbot/start",
    "/api/loopbot/stop",
    "/api/loopbot/config",
    "/api/loopbot/events",
    "/api/loopbot/chart",
)


def test_static_files_exist() -> None:
    assert (STATIC_DIR / "index.html").is_file()
    assert (STATIC_DIR / "style.css").is_file()
    assert (STATIC_DIR / "app.js").is_file()
    assert (STATIC_DIR / "landmask.js").is_file()


def test_landmask_is_embedded_and_parseable() -> None:
    landmask = (STATIC_DIR / "landmask.js").read_text(encoding="utf-8")
    assert "window.LANDMASK" in landmask
    assert "width: 180" in landmask
    assert "height: 90" in landmask
    assert len(landmask) > 4000  # the hex payload is ~4 KB
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert "landmask.js" in html


def test_reset_button_guard_exists() -> None:
    """The RESET LAYOUT bug fix: persist must skip when reset was requested."""
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "resetRequested" in js
    assert "if (resetRequested) return" in js


def test_panel_buttons_are_functional() -> None:
    """Min/max/close buttons must carry data-act and have click handlers."""
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    for action in ("min", "max", "close"):
        assert f'data-act="{action}"' in html, f"missing data-act={action}"
    assert 'data-act === "min"' in js or 'action === "min"' in js
    assert 'action === "max"' in js
    assert 'action === "close"' in js
    assert "cmd-restore" in html and "cmd-restore" in js
    assert "stopPropagation" in js  # buttons must not start a panel drag


def test_stock_chip_click_flow_exists() -> None:
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "stock-chip" in js
    assert "data-symbol" in js
    assert "onChipClick" in js
    assert "renderStockChart" in js
    assert "stock-canvas" in js


def test_all_tickers_clickable_and_botctl_wired() -> None:
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "openChart" in js
    assert "clickable-row" in js
    assert "wireBotctl" in js
    assert "bot-start" in js and "bot-stop" in js and "bot-save-risk" in js
    assert "postJson" in js
    assert "PAPER TRADE ONLY" in js
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert "panel-botctl" in html


def test_bot_mind_wired() -> None:
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert "renderMind" in js
    assert "mind-thoughts" in js
    assert "mind-verdict" in js
    assert "BOT MIND" in html
    assert "/api/botmind" in js


def test_readme_exists_and_documents_run() -> None:
    readme = (STATIC_DIR.parent / "README.md").read_text(encoding="utf-8")
    assert "uvicorn" in readme
    assert "/canvas/" in readme
    assert "localStorage" in readme


def test_index_html_contains_canvas_structure() -> None:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    for element_id in REQUIRED_IDS:
        assert f'id="{element_id}"' in html, f"missing element id={element_id}"
    assert 'data-drag' in html
    assert 'data-resize' in html
    assert 'data-panel' in html


def test_app_js_wires_every_endpoint() -> None:
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    for endpoint in REQUIRED_ENDPOINTS:
        assert endpoint in js, f"missing fetch for {endpoint}"


def test_layout_persistence_mechanisms_present() -> None:
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "localStorage" in js
    assert "pm-canvas-layout-v5" in js
    assert "pm-canvas-tab-v4" in js
    assert "pointerdown" in js
    assert "pointermove" in js
    assert "pointerup" in js
    assert "beforeunload" in js


def test_tab_groups_present() -> None:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    for tab in ("bot", "loop", "markets", "news", "social", "charts"):
        assert f'data-tab="{tab}"' in html, f"missing tab {tab}"
    assert "data-group" in html
    assert "tab-hidden" in js
    assert "setTab" in js
    assert "applyTab" in js
    for panel in ("panel-mind", "panel-cross", "panel-news", "panel-social", "panel-chart", "panel-loopctl"):
        assert f'id="{panel}"' in html


def test_social_toggle_and_chart_search_present() -> None:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "social-src-reddit" in html and "social-src-x" in html
    assert "social-refresh" in html
    assert "activeSocialSource" in js
    assert "chart-search" in html and "chart-go" in html
    assert "wireChartSearch" in js
    assert "chart-suggest" in html


def test_loop_bot_wired() -> None:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "LOOP BOT" in html
    assert "loop-start" in js and "loop-stop" in js and "loop-save" in js
    assert "renderLoopctl" in js
    assert "renderLoopEvents" in js
    assert "drawLoopChart" in js
    assert "loop-chart-canvas" in html
    assert "5m" in js and "15m" in js and "30m" in js


def test_loop_bot_panel_matches_sequential_v2_state_contract() -> None:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    assert "SEQUENTIAL PAPER" in html
    assert "SEQUENTIAL_V2" in js
    for field in (
        "entry_window_seconds",
        "first_price_min",
        "first_price_max",
        "hedge_timeout_seconds",
        "local_vol_threshold",
        "second_preview",
        "open_exposure",
        "data.rejections",
        "data.regime",
    ):
        assert field in js
    assert "volatility-gated near-even first leg" in js
    assert "YES+NO < 1" not in js


def test_style_has_terminal_theme_tokens() -> None:
    css = (STATIC_DIR / "style.css").read_text(encoding="utf-8")
    assert "monospace" in css
    assert "#000" in css
    assert any(token in css for token in ("#ffb000", "#ff7f00", "#ffa500"))
    assert "#00ff41" in css
    assert "#ff4136" in css
    assert "cursor: move" in css
    assert "nwse-resize" in css


def test_no_external_cdn_dependencies() -> None:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    for content in (html, js):
        assert "cdn." not in content.lower()
        assert "unpkg" not in content.lower()
        assert "jsdelivr" not in content.lower()
