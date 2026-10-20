"""Grid invariants for the canvas dashboard default layout.

The LOOP BOT tab is the UI standard: 16px margins + gutters, aligned
rows/columns, zero overlaps. Every tab group (BOT, LOOP, MARKETS, NEWS,
SOCIAL, CHARTS) must obey the same invariants so no panel is ever left
"floating" or hidden under another panel.
"""
from __future__ import annotations

import re
from pathlib import Path

from dataclasses import dataclass

STATIC_DIR = Path(__file__).resolve().parent.parent / "dashboard_canvas" / "static"
GUTTER = 16


@dataclass(frozen=True)
class Box:
    id: str
    x: int
    y: int
    w: int
    h: int

    @property
    def left(self) -> int:
        return self.x

    @property
    def top(self) -> int:
        return self.y

    @property
    def right(self) -> int:
        return self.x + self.w

    @property
    def bottom(self) -> int:
        return self.y + self.h


def _canvas_size() -> tuple[int, int]:
    css = (STATIC_DIR / "style.css").read_text(encoding="utf-8")
    match = re.search(r"#canvas\s*\{[^}]*width:\s*(\d+)px;[^}]*height:\s*(\d+)px;", css, re.S)
    assert match, "canvas size not found in style.css"
    return int(match.group(1)), int(match.group(2))


def _default_layout() -> dict[str, Box]:
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    section = js.split("function defaultLayout()", 1)[1].split("function clampLayout", 1)[0]
    consts: dict[str, int] = {}
    for name, value in re.findall(r"\b(x\d|w)\s*=\s*(\d+)", section):
        consts[name] = int(value)
    boxes: dict[str, Box] = {}
    pattern = re.compile(
        r'"panel-([\w-]+)"\s*:\s*\{\s*x:\s*([\w]+),\s*y:\s*(\d+),\s*w:\s*([\w]+),\s*h:\s*(\d+),\s*z:\s*(\d+)'
    )
    for match in pattern.finditer(section):
        pid = "panel-" + match.group(1)
        x_ref = match.group(2)
        w_ref = match.group(4)
        x = consts[x_ref] if x_ref in consts else int(x_ref)
        w = consts[w_ref] if w_ref in consts else int(w_ref)
        boxes[pid] = Box(pid, x, int(match.group(3)), w, int(match.group(5)))
    return boxes


def _group_of(panel_id: str) -> str:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    match = re.search(r'<section[^>]*id="' + re.escape(panel_id) + r'"[^>]*data-group="([^"]+)"', html)
    assert match, f"panel {panel_id} missing from index.html"
    return match.group(1)


def test_default_layout_has_every_panel() -> None:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    panels = re.findall(r'<section[^>]*id="(panel-[\w-]+)"[^>]*data-panel', html)
    assert panels, "no draggable panels found in index.html"
    layout = _default_layout()
    for pid in panels:
        assert pid in layout, f"panel {pid} missing from defaultLayout()"


def test_no_tab_group_has_overlapping_panels() -> None:
    """The sentiment/botctl stacking bug: grids must be overlap-free per tab."""
    layout = _default_layout()
    groups: dict[str, list[Box]] = {}
    for pid, box in layout.items():
        groups.setdefault(_group_of(pid), []).append(box)
    for group, boxes in groups.items():
        for i, a in enumerate(boxes):
            for b in boxes[i + 1:]:
                overlap = a.left < b.right and b.left < a.right and a.top < b.bottom and b.top < a.bottom
                assert not overlap, f"{group}: {a.id} overlaps {b.id} ({a} vs {b})"


def test_all_panels_inside_canvas() -> None:
    canvas_w, canvas_h = _canvas_size()
    for box in _default_layout().values():
        assert box.left >= 0 and box.top >= 0, f"{box.id} off canvas top-left"
        assert box.right <= canvas_w, f"{box.id} exceeds canvas width ({box.right} > {canvas_w})"
        assert box.bottom <= canvas_h, f"{box.id} exceeds canvas height ({box.bottom} > {canvas_h})"


def test_bot_tab_uses_loop_style_grid_with_16px_gutters() -> None:
    """BOT must read like LOOP BOT: even rows/columns, exact 16px gutters."""
    layout = _default_layout()
    mind = layout["panel-mind"]
    pnl = layout["panel-pnl"]
    botctl = layout["panel-botctl"]
    sentiment = layout["panel-sentiment"]

    # full-width top / bottom rows hug both margins
    assert mind.left == GUTTER and mind.right == 1780 - GUTTER
    assert sentiment.left == GUTTER and sentiment.right == 1780 - GUTTER
    # paired row: equal height, aligned bottom edge, exact gutter between
    assert pnl.h == botctl.h
    assert pnl.bottom == botctl.bottom
    assert botctl.left - pnl.right == GUTTER
    # full-width rows and paired row are separated by the same 16px gutter
    assert pnl.top - mind.bottom == GUTTER
    cards_top = layout["panel-exec"].top
    assert cards_top - botctl.bottom == GUTTER
    cards_bottom = layout["panel-exec"].bottom
    assert sentiment.top - cards_bottom == GUTTER

    # card row: six equal cards, exact 16px gutters, aligned on one line
    cards = [layout[pid] for pid in ("panel-exec", "panel-depth", "panel-alerts",
                                     "panel-models", "panel-latency", "panel-gates")]
    assert len(cards) == 6
    widths = {c.w for c in cards}
    assert len(widths) == 1, f"card widths not equal: {widths}"
    heights = {c.h for c in cards}
    assert len(heights) == 1, f"card heights not equal: {heights}"
    tops = {c.top for c in cards}
    assert len(tops) == 1, f"card tops not aligned: {tops}"
    sorted_cards = sorted(cards, key=lambda c: c.left)
    for prev, cur in zip(sorted_cards, sorted_cards[1:]):
        assert cur.left - prev.right == GUTTER, f"{prev.id} → {cur.id} gutter != {GUTTER}"
    assert sorted_cards[0].left == GUTTER and sorted_cards[-1].right == 1780 - GUTTER
    assert sorted_cards[0].top - botctl.bottom == GUTTER


def test_loop_tab_standard_unchanged() -> None:
    """LOOP BOT stays the reference grid: 16 gaps on every edge."""
    layout = _default_layout()
    ctl = layout["panel-loopctl"]
    chart = layout["panel-loopchart"]
    events = layout["panel-loopevents"]
    assert ctl.left == GUTTER and ctl.top == GUTTER
    assert chart.right == 1780 - GUTTER and events.right == 1780 - GUTTER
    assert chart.left - ctl.right == GUTTER
    assert events.left == chart.left
    assert events.top - chart.bottom == GUTTER