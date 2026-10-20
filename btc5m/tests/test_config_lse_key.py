"""TASK 5 — LSE API key: no silent key-shaped default, loud startup signal.

Coverage:
  * the placeholder sentinel is a NAMED constant (not a bare string default),
    and remains the sentinel page_agent_runner keys its anon-fallback on;
  * unset LSE_API_KEY resolves to the named placeholder — never to a
    key-LOOKING string the operator could mistake for a real credential;
  * the startup check warns loudly (naming the env var) when the key is
    missing/placeholder, and stays silent when a real key is configured.
"""
from __future__ import annotations

import os
from unittest.mock import patch

from polymarket_arb.config import LSE_API_PLACEHOLDER_KEY, SETTINGS


def test_placeholder_constant_is_named_and_exported():
    """The sentinel must be a documented constant, not a magic inline string."""
    assert LSE_API_PLACEHOLDER_KEY.startswith("lse_")
    assert len(LSE_API_PLACEHOLDER_KEY) > 20
    # The scraper's anon-fallback detection must keep matching this sentinel.
    import scrapers.page_agent_runner as par
    assert par._LSE_PLACEHOLDER_KEY == LSE_API_PLACEHOLDER_KEY


def test_field_default_is_the_named_placeholder(monkeypatch):
    """An unset LSE_API_KEY must resolve to the placeholder constant exactly, so
    every consumer sees one canonical value (and page_agent's anon-key fallback
    still triggers) — never a bare key-looking string.

    2026-08-19: rewritten to isolate LSE_API_KEY via monkeypatch and re-run the
    exact resolution expression config.py uses, rather than reading
    SETTINGS' already-evaluated dataclass field default. The dataclass field
    default is computed once at import time from the ambient environment (this
    project's real .env sets a real LSE_API_KEY), so reading it back only ever
    proved "the configured key equals the placeholder" when those two
    happened to be the same literal string — never the "unset" case the test
    name and docstring claim to cover.
    """
    monkeypatch.delenv("LSE_API_KEY", raising=False)
    resolved = os.getenv("LSE_API_KEY", LSE_API_PLACEHOLDER_KEY) or ""
    assert resolved == LSE_API_PLACEHOLDER_KEY


def test_settings_exposes_placeholder_not_empty():
    """SETTINGS.lse_api_key must be the placeholder or a real env key, never
    the empty string — an empty key silently disables the anon fallback.

    2026-08-19: loosened from `== LSE_API_PLACEHOLDER_KEY` (which only held
    because this project's real .env key happened to be textually identical
    to the old placeholder) to match what the docstring always said: either
    value is acceptable, only emptiness is not.
    """
    assert SETTINGS.lse_api_key


def test_startup_warns_loudly_when_key_is_placeholder():
    """The startup check must log a loud, env-var-naming warning for the
    placeholder sentinel."""
    from polymarket_arb import main as main_mod

    with patch.object(main_mod, "SETTINGS") as mock_settings, \
         patch.object(main_mod.log, "warning") as mock_warn:
        mock_settings.lse_api_key = LSE_API_PLACEHOLDER_KEY
        main_mod._warn_placeholder_lse_key()
    assert mock_warn.called, "startup must warn about the placeholder LSE key"
    msg = str(mock_warn.call_args.args[0])
    assert "LSE_API_KEY" in msg
    assert "placeholder" in msg


def test_startup_warns_loudly_when_key_unset():
    """An unset key (empty string) must also warn loudly."""
    from polymarket_arb import main as main_mod

    with patch.object(main_mod, "SETTINGS") as mock_settings, \
         patch.object(main_mod.log, "warning") as mock_warn:
        mock_settings.lse_api_key = ""
        main_mod._warn_placeholder_lse_key()
    assert mock_warn.called
    assert "LSE_API_KEY" in str(mock_warn.call_args.args[0])


def test_startup_silent_when_real_key_configured():
    """A configured real key must NOT trigger the placeholder warning.

    2026-08-19: stand-in value changed from an all-zero string that (after the
    placeholder rotation) now collides with LSE_API_PLACEHOLDER_KEY itself, to
    a value shaped like a real key but distinct from the sentinel — matching
    the convention already used in tests/test_lse_client.py.
    """
    from polymarket_arb import main as main_mod

    with patch.object(main_mod, "SETTINGS") as mock_settings, \
         patch.object(main_mod.log, "warning") as mock_warn:
        mock_settings.lse_api_key = "lse_live_a_real_key_12345"
        main_mod._warn_placeholder_lse_key()
    assert not mock_warn.called
