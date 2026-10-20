"""Static structural checks for the JS dashboard frontend.

Verifies the frontend files exist and wire every panel to its API endpoint.
These are structural tests only; the live end-to-end check is a manual run
of the backend plus a browser fetch (see dashboard_js/README integration).
"""
from __future__ import annotations

from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parent.parent / "dashboard_js" / "static"

REQUIRED_IDS = (
    "ticker-tape",
    "command-bar",
    "panel-status",
    "panel-heatmap",
    "panel-pnl",
    "panel-depth",
    "panel-alerts",
    "panel-models",
    "panel-latency",
    "panel-checklist",
    "panel-ticker-stream",
)

REQUIRED_ENDPOINTS = (
    "/api/ticker",
    "/api/heatmap",
    "/api/pnl",
    "/api/depth",
    "/api/alerts",
    "/api/models",
    "/api/latency",
    "/api/checklist",
    "/api/command",
)


def test_static_files_exist() -> None:
    assert (STATIC_DIR / "index.html").is_file()
    assert (STATIC_DIR / "style.css").is_file()
    assert (STATIC_DIR / "app.js").is_file()


def test_index_html_contains_all_panel_ids() -> None:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    for element_id in REQUIRED_IDS:
        assert f'id="{element_id}"' in html, f"missing element id={element_id}"


def test_app_js_wires_every_endpoint() -> None:
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    for endpoint in REQUIRED_ENDPOINTS:
        assert endpoint in js, f"missing fetch for {endpoint}"


def test_style_has_terminal_theme_tokens() -> None:
    css = (STATIC_DIR / "style.css").read_text(encoding="utf-8")
    assert "monospace" in css
    assert "#000" in css
    amber_or_orange = any(token in css for token in ("#ffb000", "#ff7f00", "#ffa500"))
    assert amber_or_orange, "missing amber/orange command-bar color"
    assert "#00ff41" in css  # green
    assert "#ff4136" in css  # red


def test_html_references_js_and_css() -> None:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert "/static/app.js" in html
    assert "/static/style.css" in html


def test_no_external_cdn_dependencies() -> None:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    for content in (html, js):
        assert "cdn." not in content.lower()
        assert "unpkg" not in content.lower()
        assert "jsdelivr" not in content.lower()
