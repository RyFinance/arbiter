"""Phase 0 smoke tests for config.py."""
from __future__ import annotations

from polyuni.config import SETTINGS


def test_settings_singleton_has_sane_defaults():
    assert SETTINGS.min_edge == 0.01
    assert SETTINGS.max_concurrent_markets == 50
    assert SETTINGS.slippage_stress_buffer > 0
    assert SETTINGS.min_liquidity_per_side > 0
    assert SETTINGS.no_entry_seconds > 0


def test_dry_run_and_paper_trade_default_to_safe_local_mode():
    assert SETTINGS.dry_run is True
    assert SETTINGS.paper_trade is False
    assert SETTINGS.live is False


def test_max_time_to_resolution_defaults_to_uncapped(monkeypatch):
    monkeypatch.delenv("MAX_TIME_TO_RESOLUTION_S", raising=False)
    import polyuni.config as config_mod
    assert config_mod._optional_float("MAX_TIME_TO_RESOLUTION_S") is None


def test_max_time_to_resolution_parses_when_set(monkeypatch):
    monkeypatch.setenv("MAX_TIME_TO_RESOLUTION_S", "86400")
    import polyuni.config as config_mod
    assert config_mod._optional_float("MAX_TIME_TO_RESOLUTION_S") == 86400.0
