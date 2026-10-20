from __future__ import annotations

import json

import pytest

from scripts.setup_wizard import (
    DEFAULTS,
    profile_values,
    save_validation_report,
    validate_config,
    write_env,
)


VALID_PRIVATE_KEY = "ac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"


def _statuses(values):
    return {result.name: result.status for result in validate_config(values)}


def test_profiles_only_emit_safe_execution_modes():
    dry = profile_values("dry-run", {})
    paper = profile_values("paper", {"PRIVATE_KEY": VALID_PRIVATE_KEY})
    assert (dry["DRY_RUN"], dry["PAPER_TRADE"]) == ("true", "false")
    assert (paper["DRY_RUN"], paper["PAPER_TRADE"]) == ("false", "true")
    with pytest.raises(ValueError, match="unsupported profile"):
        profile_values("live", {})


def test_paper_profile_requires_valid_private_key():
    missing = profile_values("paper", {})
    invalid = profile_values("paper", {"PRIVATE_KEY": "not-a-key"})
    valid = profile_values("paper", {"PRIVATE_KEY": VALID_PRIVATE_KEY})
    assert _statuses(missing)["private_key"] == "fail"
    assert _statuses(invalid)["private_key"] == "fail"
    assert _statuses(valid)["private_key"] == "pass"


def test_unsafe_no_simulation_mode_is_rejected():
    values = profile_values("dry-run", {})
    values["DRY_RUN"] = "false"
    values["PAPER_TRADE"] = "false"
    assert _statuses(values)["execution_mode"] == "fail"


def test_partial_l2_credentials_are_rejected():
    values = profile_values(
        "paper",
        {"PRIVATE_KEY": VALID_PRIVATE_KEY, "POLYMARKET_API_KEY": "only-one"},
    )
    assert _statuses(values)["polymarket_l2_credentials"] == "fail"


def test_proxy_wallet_must_be_blank_or_a_valid_address():
    invalid = profile_values(
        "paper", {"PRIVATE_KEY": VALID_PRIVATE_KEY, "PROXY_WALLET": "not-an-address"}
    )
    valid = profile_values(
        "paper",
        {
            "PRIVATE_KEY": VALID_PRIVATE_KEY,
            "PROXY_WALLET": "0x1111111111111111111111111111111111111111",
        },
    )
    assert _statuses(invalid)["proxy_wallet"] == "fail"
    assert _statuses(valid)["proxy_wallet"] == "pass"


def test_alert_webhook_is_blank_or_https_without_echoing_secret_url():
    blank = profile_values("dry-run", {})
    invalid = profile_values("dry-run", {"ALERT_WEBHOOK_URL": "http://example.test/hook"})
    valid = profile_values("dry-run", {"ALERT_WEBHOOK_URL": "https://example.test/secret"})
    loopback = profile_values("dry-run", {"ALERT_WEBHOOK_URL": "http://127.0.0.1:9876/hook"})
    assert _statuses(blank)["alert_webhook_url"] == "pass"
    assert _statuses(invalid)["alert_webhook_url"] == "fail"
    result = next(row for row in validate_config(valid) if row.name == "alert_webhook_url")
    assert result.status == "pass"
    assert "secret" not in result.detail
    assert _statuses(loopback)["alert_webhook_url"] == "pass"


def test_env_write_is_atomic_private_and_requires_force(tmp_path):
    values = profile_values("paper", {"PRIVATE_KEY": VALID_PRIVATE_KEY})
    path = write_env(values, tmp_path / ".env")
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    text = path.read_text(encoding="utf-8")
    assert 'PAPER_TRADE="true"' in text
    assert 'DRY_RUN="false"' in text
    with pytest.raises(FileExistsError):
        write_env(values, path)
    write_env(values, path, force=True)
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_newlines_in_values_are_rejected(tmp_path):
    values = profile_values("dry-run", {"ALERT_WEBHOOK_URL": "safe\ninjected"})
    with pytest.raises(ValueError, match="newlines"):
        write_env(values, tmp_path / ".env")


def test_defaults_cover_optional_numeric_values_for_existing_configs():
    values = dict(DEFAULTS)
    values.update(
        {
            "DRY_RUN": "false",
            "PAPER_TRADE": "true",
            "PRIVATE_KEY": VALID_PRIVATE_KEY,
        }
    )
    statuses = _statuses(values)
    assert statuses["max_quote_age_ms"] == "pass"
    assert statuses["cross_leg_settle_ms"] == "pass"
    assert statuses["max_leg_gap_ms"] == "pass"


def test_validation_report_is_atomic_and_excludes_secret_values(tmp_path):
    values = profile_values("paper", {"PRIVATE_KEY": VALID_PRIVATE_KEY})
    results = validate_config(values)
    output = tmp_path / "setup.json"
    report = save_validation_report(results, output, env_file=tmp_path / ".env")
    persisted = json.loads(output.read_text(encoding="utf-8"))
    assert report["validation"]["status"] == "verified-pass"
    assert persisted["validation"]["fail_count"] == 0
    assert persisted["secrets_included"] is False
    assert VALID_PRIVATE_KEY not in output.read_text(encoding="utf-8")
