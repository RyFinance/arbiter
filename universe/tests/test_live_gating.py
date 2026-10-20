"""Live trading is opt-in at three layers and capped at the last step."""
from types import SimpleNamespace

import pytest

from polyuni import venues
from polyuni.clients import trading_client as tc
from polyuni.live_control import LiveTradingControl, live_endpoint_enabled


@pytest.fixture
def live_env(monkeypatch):
    monkeypatch.setenv("ARBITER_LIVE_TRADING", "enabled")
    monkeypatch.setenv("LIVE_EXECUTION", "true")


def _client(sdk):
    client = tc.TradingClient.__new__(tc.TradingClient)
    client.s = SimpleNamespace(live_execution=True, live_max_order_usd=5.0)
    client._clob_client = sdk
    client._creds = object()
    client._paper_fill_details = {}
    return client


class _Sdk:
    def __init__(self, resp):
        self.resp, self.calls = resp, 0

    def create_and_post_order(self, args, order_type=None):
        self.calls += 1
        self.args, self.order_type = args, order_type
        return self.resp


def test_live_is_off_without_env(monkeypatch):
    monkeypatch.delenv("ARBITER_LIVE_TRADING", raising=False)
    monkeypatch.delenv("LIVE_EXECUTION", raising=False)
    assert not live_endpoint_enabled()


def test_operator_flag_alone_does_not_allow_live(tmp_path, monkeypatch):
    monkeypatch.delenv("ARBITER_LIVE_TRADING", raising=False)
    control = LiveTradingControl(tmp_path / "s.json", tmp_path / "a.jsonl")
    control.set_operator_enabled(True, actor="test", reason="test")
    assert not control.check_submission(mode="live", call_site="t").allowed


def test_all_three_switches_allow_live(tmp_path, live_env):
    control = LiveTradingControl(tmp_path / "s.json", tmp_path / "a.jsonl")
    control.set_operator_enabled(True, actor="test", reason="test")
    assert control.check_submission(mode="live", call_site="t").allowed


def test_order_over_cap_never_reaches_exchange(live_env):
    sdk = _Sdk({"success": True, "orderID": "x", "status": "matched"})
    assert _client(sdk)._post_live_fok("tok", 0.5, 20, "BUY") is None  # $10 > $5 cap
    assert sdk.calls == 0


def test_matched_fok_records_real_fill_price(live_env):
    sdk = _Sdk({"success": True, "orderID": "0xabc", "status": "matched",
                "makingAmount": "4.8", "takingAmount": "10"})
    client = _client(sdk)
    oid = client._post_live_fok("tok", 0.49, 10, "BUY")
    assert oid == "0xabc"
    assert sdk.order_type == "FOK"
    assert client._paper_fill_details[oid]["fill_price"] == pytest.approx(0.48)


def test_unmatched_fok_is_not_a_fill(live_env):
    sdk = _Sdk({"success": True, "orderID": "0xdef", "status": "unmatched"})
    assert _client(sdk)._post_live_fok("tok", 0.49, 10, "BUY") is None


def test_live_mode_needs_env_opt_in(tmp_path, monkeypatch):
    monkeypatch.delenv("ARBITER_LIVE_TRADING", raising=False)
    monkeypatch.setattr(venues, "MODES_PATH", tmp_path / "modes.json")
    monkeypatch.setattr(venues, "MODE_AUDIT_PATH", tmp_path / "audit.jsonl")
    with pytest.raises(ValueError, match="ARBITER_LIVE_TRADING"):
        venues.set_mode("polymarket", "live", "LIVE")
    with pytest.raises(ValueError, match="no Kalshi engine"):
        venues.set_mode("kalshi", "paper")


def test_live_setup_lists_missing_steps_in_order(tmp_path, monkeypatch):
    monkeypatch.delenv("ARBITER_LIVE_TRADING", raising=False)
    monkeypatch.delenv("POLYMARKET_ADDRESS", raising=False)
    monkeypatch.delenv("PROXY_WALLET", raising=False)
    monkeypatch.setattr(venues, "CREDENTIALS_PATH", tmp_path / "creds.json")
    monkeypatch.setattr(venues, "MODES_PATH", tmp_path / "modes.json")
    setup = venues.live_setup_status()
    assert [step["id"] for step in setup["steps"]] == ["link", "key", "opt_in", "mode"]
    assert not setup["ready"] and not setup["live"]
    assert not next(step for step in setup["steps"] if step["id"] == "opt_in")["done"]
