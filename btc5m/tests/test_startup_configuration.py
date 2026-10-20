from __future__ import annotations

from types import SimpleNamespace

from polymarket_arb.main import validate_startup_configuration


VALID_PRIVATE_KEY = "ac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"


def _settings(**overrides):
    values = {
        "dry_run": True,
        "paper_trade": False,
        "live": False,
        "gamma_base_url": "https://gamma-api.polymarket.com",
        "clob_base_url": "https://clob.polymarket.com",
        "clob_ws_url": "wss://ws-subscriptions-clob.polymarket.com",
        "polygon_rpc_url": "https://polygon-rpc.com",
        "alert_webhook_url": "",
        "private_key": "",
        "proxy_wallet": "",
        "order_size": 10.0,
        "max_quote_age_ms": 500,
        "cross_leg_settle_ms": 10.0,
        "max_leg_gap_ms": 300.0,
        "max_position_per_market": 200.0,
        "max_total_capital": 1000.0,
        "min_edge": 0.01,
        "polymarket_api_key": "",
        "polymarket_api_secret": "",
        "polymarket_api_passphrase": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_startup_accepts_safe_dry_run_and_authenticated_paper_modes():
    assert validate_startup_configuration(_settings()) == []
    assert validate_startup_configuration(
        _settings(
            dry_run=False,
            paper_trade=True,
            live=True,
            private_key=VALID_PRIVATE_KEY,
        )
    ) == []


def test_startup_rejects_unsupported_live_or_ambiguous_mode():
    unsupported = validate_startup_configuration(
        _settings(dry_run=False, paper_trade=False, live=True)
    )
    ambiguous = validate_startup_configuration(
        _settings(dry_run=True, paper_trade=True, live=False)
    )
    assert any("exactly one supported mode" in error for error in unsupported)
    assert any("exactly one supported mode" in error for error in ambiguous)


def test_startup_rejects_unusable_paper_urls_caps_and_partial_credentials():
    errors = validate_startup_configuration(
        _settings(
            dry_run=False,
            paper_trade=True,
            live=False,
            clob_base_url="http://insecure.example",
            max_position_per_market=2000.0,
            polymarket_api_key="only-one",
        )
    )
    assert any("paper mode requires PRIVATE_KEY" in error for error in errors)
    assert any("clob_base_url" in error for error in errors)
    assert any("cannot exceed" in error for error in errors)
    assert any("all present or all blank" in error for error in errors)


def test_startup_rejects_invalid_signer_proxy_and_leg_gap():
    errors = validate_startup_configuration(
        _settings(
            dry_run=False,
            paper_trade=True,
            live=True,
            private_key="not-a-key",
            proxy_wallet="not-an-address",
            max_leg_gap_ms=-1.0,
            alert_webhook_url="http://insecure.example/hook",
        )
    )
    assert any("PRIVATE_KEY" in error for error in errors)
    assert any("PROXY_WALLET" in error for error in errors)
    assert any("max_leg_gap_ms" in error for error in errors)
    assert any("alert_webhook_url" in error for error in errors)


def test_startup_allows_http_only_for_local_paper_alert_sink():
    local = validate_startup_configuration(
        _settings(alert_webhook_url="http://127.0.0.1:9876/hook")
    )
    remote = validate_startup_configuration(
        _settings(alert_webhook_url="http://example.test/hook")
    )
    assert not any("alert_webhook_url" in error for error in local)
    assert any("alert_webhook_url" in error for error in remote)
