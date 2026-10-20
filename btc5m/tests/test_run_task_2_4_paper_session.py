from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from scripts.run_task_2_4_paper_session import (
    atomic_publish,
    select_post_halt_probe_state,
)


def test_atomic_publish_replaces_dashboard_copy_without_mutating_source(tmp_path: Path):
    source = tmp_path / "archive" / "bot.jsonl"
    destination = tmp_path / "state" / "bot.jsonl"
    source.parent.mkdir()
    destination.parent.mkdir(exist_ok=True)
    source.write_text("new evidence\n", encoding="utf-8")
    destination.write_text("old evidence\n", encoding="utf-8")

    atomic_publish(source, destination)

    assert source.read_text(encoding="utf-8") == "new evidence\n"
    assert destination.read_text(encoding="utf-8") == "new evidence\n"
    assert not list(destination.parent.glob("*.tmp"))


def test_post_halt_probe_state_is_frozen_before_window_teardown():
    live_state = SimpleNamespace(
        market_id="btc-window", best_yes_ask=0.41, best_no_ask=0.56,
    )
    feed = SimpleNamespace(states={"btc-window": live_state})
    execution = SimpleNamespace(_templates={"btc-window": object()})

    frozen = select_post_halt_probe_state(feed, execution)
    feed.states.clear()
    execution._templates.clear()
    live_state.best_yes_ask = 0.99

    assert frozen is not live_state
    assert frozen.market_id == "btc-window"
    assert frozen.best_yes_ask == 0.41
    assert frozen.best_no_ask == 0.56
