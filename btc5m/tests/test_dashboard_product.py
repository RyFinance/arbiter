from __future__ import annotations

from pathlib import Path

from streamlit.testing.v1 import AppTest


APP = Path(__file__).resolve().parents[1] / "streamlit_app.py"


def test_dashboard_control_panel_runs_without_exception():
    app = AppTest.from_file(str(APP), default_timeout=30).run()
    assert not app.exception
    assert "Demo mode (sealed paper replay)" in [item.label for item in app.toggle]
    assert "Apply audited state change" in [item.label for item in app.button]
    assert "Refresh read-only wallet / jurisdiction" in [item.label for item in app.button]


def test_dashboard_demo_mode_replays_sealed_archive_offline():
    app = AppTest.from_file(str(APP), default_timeout=30).run()
    app.toggle[0].set_value(True).run()
    assert not app.exception
    warnings = [item.value for item in app.warning]
    assert any("network_used=false" in value for value in warnings)
    metrics = {item.label: item.value for item in app.metric}
    assert int(metrics["Visible events"]) > 0
    assert "Fires" in metrics
    assert "Trade events" in metrics
