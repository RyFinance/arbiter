"""TASK A — Macro-risk integration into risk.py.

Verifies:
  * HIGH_MACRO_RISK is false when no HIGH window is active in valid fresh state.
  * HIGH_MACRO_RISK is true while inside a 2-hour HIGH-impact window.
  * risk.precheck() TIGHTENS during the window: a market that would normally pass
    (combined ask just under the plain MIN_EDGE threshold) is REJECTED because the
    effective MIN_EDGE is widened by macro_edge_widen (default +0.005).
  * After the window closes, precheck returns to normal and the trade is allowed.
  * Metrics tracks trades skipped due to HIGH_MACRO_RISK (macro_skipped / macro_skipped_pnl).
  * Missing/import-failed/unreadable/invalid/stale macro state fails closed with
    transition-deduplicated durable alerts and recovers only on fresh valid state.

The HIGH-impact 2-hour window math is REUSED from scrapers/page_agent_runner.py —
this test drives that real logic by injecting a synthetic MacroEventState into
risk.py's module-level cache (no scraper/network needed).
"""
from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from polymarket_arb.alerting import ALERT_MACRO_STATE_FAILURE, WebhookAlerter
from polymarket_arb.config import SETTINGS, Settings
from polymarket_arb.metrics import Metrics
from polymarket_arb.models import MarketState
from polymarket_arb.risk import RiskState


def _state(yes, no, ysz=None, nsz=None, ts=None, window_close=0.0, yes_bid=None, no_bid=None):
    now = ts if ts is not None else time.time()
    return MarketState(
        market_id="m1", question="BTC up?", yes_token_id="0xYES", no_token_id="0xNO",
        best_yes_ask=yes, best_no_ask=no, yes_size=ysz, no_size=nsz,
        last_update_ts=now, yes_ts=now, no_ts=now,
        window_close=window_close, best_yes_bid=yes_bid, best_no_bid=no_bid,
    )


def _install_macro_state(state):
    """Inject a pre-built MacroEventState into risk.py's module cache, bypassing the
    file read. This exercises the REAL is_high_risk_window window membership logic
    from scrapers/page_agent_runner.py against our synthetic windows."""
    import polymarket_arb.risk as risk_mod
    risk_mod._macro_cache = state
    risk_mod._macro_cache_epoch = time.time() + 1_000_000  # far-future epoch => cache hit


def _clear_macro_cache():
    import polymarket_arb.risk as risk_mod
    risk_mod._macro_cache = None
    risk_mod._macro_cache_epoch = 0.0
    risk_mod._macro_state_error = None
    risk_mod._macro_state_error_code = None


def _macro_alert_records(path):
    if not path.exists():
        return []
    return [
        record
        for record in (json.loads(line) for line in path.read_text().splitlines() if line.strip())
        if record.get("kind") == ALERT_MACRO_STATE_FAILURE
    ]




def _macro_state_cls():
    """Resolve the scraper's MacroEventState once, for direct construction."""
    import polymarket_arb.risk as risk_mod
    module = risk_mod._maybe_page_agent()[0]
    return module.MacroEventState, module.MacroEvent


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def _build_state_with_windows(windows, last_update=None):
    MacroEventState, MacroEvent = _macro_state_cls()
    now = time.time()
    events = []
    win_list = []
    for i, (start, end) in enumerate(windows):
        title = f"HIGH-EVENT-{i}"
        events.append(MacroEvent(title=title, timestamp=start, impact="HIGH", description="synthetic"))
        win_list.append({"start": start, "end": end, "event": title})
    return MacroEventState(
        events=events,
        last_update=now if last_update is None else last_update,
        high_risk_windows=win_list,
    )


# ----------------------------------------------------------------------------
# HIGH_MACRO_RISK detection (reuses scraper is_high_risk_window)
# ----------------------------------------------------------------------------
def test_high_macro_risk_false_without_window():
    """With no macro windows active, HIGH_MACRO_RISK is False (fail-open)."""
    # Clear the cache so we start fresh, then install an empty-window state.
    import polymarket_arb.risk as risk_mod
    risk_mod._macro_cache = None
    risk_mod._macro_cache_epoch = 0.0
    state = _build_state_with_windows([])
    _install_macro_state(state)

    risk = RiskState(balance=1000.0)
    assert risk.HIGH_MACRO_RISK is False


def test_high_macro_risk_true_inside_window():
    """Now falls inside the 2-hour HIGH window -> HIGH_MACRO_RISK is True."""
    now = time.time()
    state = _build_state_with_windows([(now - 100, now + 500)])  # active window
    _install_macro_state(state)
    risk = RiskState(balance=1000.0)
    assert risk.HIGH_MACRO_RISK is True


def test_high_macro_risk_false_after_window_closes():
    """Window ended >1 window ago -> HIGH_MACRO_RISK is False again."""
    now = time.time()
    state = _build_state_with_windows([(now - 4000, now - 1000)])  # ended
    _install_macro_state(state)
    risk = RiskState(balance=1000.0)
    assert risk.HIGH_MACRO_RISK is False


# ----------------------------------------------------------------------------
# precheck tightens during window, normal after
# ----------------------------------------------------------------------------
def test_precheck_rejects_during_macro_window():
    """A market with combined ask just under the plain MIN_EDGE (would normally pass)
    is REJECTED during an active HIGH macro window because precheck widens the
    effective MIN_EDGE by macro_edge_widen (default +0.005)."""
    min_edge = SETTINGS.min_edge            # e.g. 0.01
    widen = 0.005                            # default macro_edge_widen
    # combined = 1 - min_edge - tiny  -> normally an arb (combined < 1 - min_edge)
    # but >= 1 - (min_edge + widen)  -> rejected under macro risk.
    combined_ok = 1.0 - min_edge - 1e-4      # clears plain edge
    assert combined_ok < 1.0 - min_edge
    assert combined_ok >= 1.0 - (min_edge + widen), "fixture must sit in the widened-reject band"

    yes = combined_ok / 2.0
    no = combined_ok - yes                  # yes + no == combined_ok
    st = _state(yes, no, ysz=100.0, nsz=100.0)

    # Install an ACTIVE macro window.
    now = time.time()
    state = _build_state_with_windows([(now - 100, now + 500)])
    _install_macro_state(state)

    risk = RiskState(balance=1000.0)
    assert risk.HIGH_MACRO_RISK is True
    # cost = combined * size ; size = order_size (default 10) in tests, but precheck
    # only needs cost_yes + cost_no to stay within balance. Use small costs.
    cost_yes, cost_no = yes * 1.0, no * 1.0
    reason = risk.precheck(st, cost_yes, cost_no)
    assert reason is not None, "precheck must reject a thin-edge trade during macro risk"
    assert "macro" in reason.lower()


def test_precheck_uses_detector_snapshot_for_macro_edge() -> None:
    """A later book mutation cannot invalidate a detector-approved snapshot."""
    now = time.time()
    _install_macro_state(_build_state_with_windows([(now - 100, now + 500)]))
    macro_threshold = 1.0 - (SETTINGS.min_edge + 0.005)
    snapshot_combined = macro_threshold - 0.01
    st = _state(0.51, 0.50, ysz=100.0, nsz=100.0)  # later book is 1.01
    risk = RiskState(balance=1000.0)

    assert risk.precheck(
        st, snapshot_combined / 2.0, snapshot_combined / 2.0, snapshot_combined
    ) is None


def test_precheck_normal_after_window():
    """Same market that was rejected during the window is ALLOWED once the window closes."""
    min_edge = SETTINGS.min_edge
    widen = 0.005
    combined_ok = 1.0 - min_edge - 1e-4
    yes = combined_ok / 2.0
    no = combined_ok - yes
    st = _state(yes, no, ysz=100.0, nsz=100.0)

    # Install an EXPIRED macro window (ended in the past).
    now = time.time()
    state = _build_state_with_windows([(now - 4000, now - 1000)])
    _install_macro_state(state)

    risk = RiskState(balance=1000.0)
    assert risk.HIGH_MACRO_RISK is False
    cost_yes, cost_no = yes * 1.0, no * 1.0
    reason = risk.precheck(st, cost_yes, cost_no)
    assert reason is None, f"precheck should allow the trade after window closes, got: {reason!r}"


# ----------------------------------------------------------------------------
# Metrics tracking
# ----------------------------------------------------------------------------
def test_metrics_tracks_macro_skipped():
    """Metrics records trades skipped due to HIGH_MACRO_RISK, mirroring would_lock."""
    m = Metrics()
    assert m.macro_skipped == 0
    assert m.macro_skipped_pnl == 0.0
    m.record_macro_skipped(0.05)
    m.record_macro_skipped(0.03)
    assert m.macro_skipped == 2
    assert abs(m.macro_skipped_pnl - 0.08) < 1e-9
    # Surfaced in summary().
    assert "macro_skipped" in m.summary()


# ----------------------------------------------------------------------------
# Configurable widen (default +0.005 honored; custom value respected)
# ----------------------------------------------------------------------------
def test_macro_edge_widen_is_configurable_default():
    """Default macro_edge_widen is +0.005 (per spec)."""
    risk = RiskState(balance=1000.0)
    assert risk.macro_edge_widen == 0.005


def test_custom_edge_widen_rejects_thicker_band():
    """A larger macro_edge_widen rejects trades with thicker edges than the default."""
    min_edge = SETTINGS.min_edge
    widen = 0.02  # custom, larger than the default 0.005
    # Allow-under-default-widen, reject-under-custom-widen band.
    # Rejection happens when combined >= threshold = 1 - (min_edge + widen).
    #   plain edge threshold   : 1-min_edge      = 0.990
    #   default-widen threshold: 1-(.min_edge+0.005) = 0.965  (allow when < 0.965)
    #   custom-widen threshold : 1-(.min_edge+0.02)  = 0.970  (reject when >= 0.970)
    # So pick combined in [0.970, 0.965): allowed by default, rejected by custom.
    combined = 1.0 - (min_edge + 0.005) - 0.005  # 0.960 -> < plain & < default-thresh, >= custom-thresh
    assert combined < 1.0 - min_edge, "must be a valid arb (combined < plain edge)"
    assert combined < 1.0 - (min_edge + 0.005), "must clear the DEFAULT widen (allowed normally)"
    assert combined >= 1.0 - (min_edge + widen), "must FAIL the custom widen (rejected)"
    yes = combined / 2.0
    no = combined - yes
    st = _state(yes, no, ysz=100.0, nsz=100.0)

    now = time.time()
    _install_macro_state(_build_state_with_windows([(now - 100, now + 500)]))

    risk = RiskState(balance=1000.0, macro_edge_widen=widen)
    assert risk.HIGH_MACRO_RISK is True
    reason = risk.precheck(st, yes * 1.0, no * 1.0)
    assert reason is not None, "custom widen should reject this band"
    assert "macro" in reason.lower()

    # Same market with the DEFAULT widen should be allowed (combined < default threshold).
    risk_default = RiskState(balance=1000.0)
    assert risk_default.macro_edge_widen == 0.005
    reason_default = risk_default.precheck(st, yes * 1.0, no * 1.0)
    assert reason_default is None, f"default widen should allow, got: {reason_default!r}"


# ----------------------------------------------------------------------------
# No-entry widening under macro risk
# ----------------------------------------------------------------------------
def test_macro_no_entry_widens_window_close_gate():
    """With macro_no_entry_extra_s>0, a windowed market near close that would normally
    be allowed (outside the plain no-entry zone) is rejected during a macro window."""
    now = time.time()
    base = SETTINGS.no_entry_seconds
    # window_close is `base + 1` seconds out: beyond the plain no-entry zone
    # (>= base is inside, so base+1 is OUTSIDE / not-yet no-entry).
    st = _state(0.47, 0.49, ysz=100.0, nsz=100.0, window_close=now + base + 1.0)

    _install_macro_state(_build_state_with_windows([(now - 100, now + 500)]))

    risk = RiskState(balance=1000.0, macro_no_entry_extra_s=30.0)
    assert risk.HIGH_MACRO_RISK is True
    # combined = 0.96 < 1 - (min_edge+widen) so the edge check passes; the no-entry
    # widening is what triggers the rejection here.
    assert (0.47 + 0.49) < 1.0 - (SETTINGS.min_edge + risk.macro_edge_widen)
    reason = risk.precheck(st, 0.47, 0.49)
    assert reason is not None
    assert "no-entry" in reason.lower() or "macro" in reason.lower()


# ----------------------------------------------------------------------------
# Macro state HARD FAIL-SAFE
# ----------------------------------------------------------------------------
def test_macro_state_max_age_default_and_env_override(monkeypatch):
    monkeypatch.delenv("MACRO_STATE_MAX_AGE_S", raising=False)
    assert Settings().macro_state_max_age_s == 900.0
    monkeypatch.setenv("MACRO_STATE_MAX_AGE_S", "123.5")
    assert Settings().macro_state_max_age_s == 123.5


def test_missing_macro_state_fails_precheck_and_reserve(
    tmp_path, monkeypatch, isolate_alerts_log, capsys
):
    import polymarket_arb.risk as risk_mod

    module, ok = risk_mod._maybe_page_agent()
    assert ok and module is not None
    missing = tmp_path / "missing-macro-events.json"
    monkeypatch.setattr(module, "MACRO_EVENTS_PATH", str(missing))
    _clear_macro_cache()

    alerter = WebhookAlerter(webhook_url="", alerts_path=str(isolate_alerts_log))
    risk = RiskState(balance=1000.0, alerter=alerter)
    st = _state(0.47, 0.49, ysz=100.0, nsz=100.0)

    assert risk.precheck(st, 0.47, 0.49) == "macro state missing"
    assert asyncio.run(risk.reserve(st, 0.47, 0.49)) == "macro state missing"
    assert risk.pending_deployed == 0.0
    records = _macro_alert_records(isolate_alerts_log)
    assert [record["failure"] for record in records] == ["missing"]
    assert str(missing) in records[0]["error"]
    assert "MACRO STATE FAIL-SAFE" in capsys.readouterr().out


def test_deleted_macro_file_is_detected_even_with_a_fresh_cached_state(
    tmp_path, monkeypatch, isolate_alerts_log
):
    import polymarket_arb.risk as risk_mod

    module, ok = risk_mod._maybe_page_agent()
    assert ok and module is not None
    macro_path = tmp_path / "macro-events.json"
    macro_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(module, "MACRO_EVENTS_PATH", str(macro_path))
    _install_macro_state(_build_state_with_windows([], last_update=time.time()))
    risk = RiskState(
        balance=1000.0,
        alerter=WebhookAlerter(webhook_url="", alerts_path=str(isolate_alerts_log)),
    )
    st = _state(0.47, 0.49, 100.0, 100.0)

    assert risk.precheck(st, 0.47, 0.49) is None
    macro_path.unlink()

    assert risk.precheck(st, 0.47, 0.49) == "macro state missing"
    assert [record["failure"] for record in _macro_alert_records(isolate_alerts_log)] == [
        "missing"
    ]


def test_importer_failure_fails_closed(monkeypatch, isolate_alerts_log):
    import polymarket_arb.risk as risk_mod

    monkeypatch.setattr(risk_mod, "_PAGE_AGENT", "SENTINEL")
    monkeypatch.setattr(risk_mod, "_PAGE_AGENT_IMPORT_OK", False)
    monkeypatch.setattr(risk_mod, "_PAGE_AGENT_IMPORT_ERROR", "synthetic importer failure")
    _clear_macro_cache()

    risk = RiskState(
        balance=1000.0,
        alerter=WebhookAlerter(webhook_url="", alerts_path=str(isolate_alerts_log)),
    )
    reason = risk.precheck(_state(0.47, 0.49, 100.0, 100.0), 0.47, 0.49)

    assert reason == "macro state importer unavailable"
    records = _macro_alert_records(isolate_alerts_log)
    assert [record["failure"] for record in records] == ["importer_unavailable"]
    assert "synthetic importer failure" in records[0]["error"]


@pytest.mark.parametrize("reader_result", [None, object()])
def test_unreadable_or_structurally_invalid_state_fails_closed(
    reader_result, monkeypatch, isolate_alerts_log
):
    import polymarket_arb.risk as risk_mod

    monkeypatch.setattr(risk_mod, "_read_macro_state", lambda: reader_result)
    _clear_macro_cache()
    risk = RiskState(
        balance=1000.0,
        alerter=WebhookAlerter(webhook_url="", alerts_path=str(isolate_alerts_log)),
    )

    reason = risk.precheck(_state(0.47, 0.49, 100.0, 100.0), 0.47, 0.49)

    assert reason == "macro state unreadable invalid"
    records = _macro_alert_records(isolate_alerts_log)
    assert [record["failure"] for record in records] == ["unreadable_invalid"]


@pytest.mark.parametrize("last_update", [0.0, -1.0, float("nan")])
def test_nonpositive_or_nonfinite_last_update_fails_closed(
    last_update, isolate_alerts_log
):
    state = _build_state_with_windows([], last_update=last_update)
    _install_macro_state(state)
    risk = RiskState(
        balance=1000.0,
        alerter=WebhookAlerter(webhook_url="", alerts_path=str(isolate_alerts_log)),
    )

    reason = risk.precheck(_state(0.47, 0.49, 100.0, 100.0), 0.47, 0.49)

    assert reason == "macro state invalid last update"
    records = _macro_alert_records(isolate_alerts_log)
    assert [record["failure"] for record in records] == ["invalid_last_update"]


def test_stale_state_fails_closed_with_exact_age(monkeypatch, isolate_alerts_log):
    import polymarket_arb.risk as risk_mod

    assert SETTINGS.macro_state_max_age_s == 900.0
    clock = SimpleNamespace(now=10_000.0)
    monkeypatch.setattr(risk_mod, "time", SimpleNamespace(time=lambda: clock.now))
    _install_macro_state(_build_state_with_windows([], last_update=9_099.0))
    risk = RiskState(
        balance=1000.0,
        alerter=WebhookAlerter(webhook_url="", alerts_path=str(isolate_alerts_log)),
    )

    reason = risk.precheck(_state(0.47, 0.49, 100.0, 100.0, ts=clock.now), 0.47, 0.49)

    assert reason == "macro state stale"
    record = _macro_alert_records(isolate_alerts_log)[0]
    assert record["failure"] == "stale"
    assert record["age_seconds"] == 901.0
    assert record["max_age_seconds"] == 900.0


def test_age_is_recomputed_every_precheck_and_blocks_after_crossing(monkeypatch):
    import polymarket_arb.risk as risk_mod

    clock = SimpleNamespace(now=10_899.0)
    monkeypatch.setattr(risk_mod, "time", SimpleNamespace(time=lambda: clock.now))
    _install_macro_state(_build_state_with_windows([], last_update=10_000.0))
    risk = RiskState(balance=1000.0)
    st = _state(0.47, 0.49, 100.0, 100.0, ts=clock.now)

    assert risk.precheck(st, 0.47, 0.49) is None
    clock.now = 10_901.0
    assert risk.precheck(st, 0.47, 0.49) == "macro state stale"


def test_failure_alert_is_deduplicated_then_rearmed_after_recovery(
    monkeypatch, isolate_alerts_log
):
    import polymarket_arb.risk as risk_mod

    clock = SimpleNamespace(now=20_000.0)
    monkeypatch.setattr(risk_mod, "time", SimpleNamespace(time=lambda: clock.now))
    state = _build_state_with_windows([], last_update=19_099.0)
    _install_macro_state(state)
    risk = RiskState(
        balance=1000.0,
        alerter=WebhookAlerter(webhook_url="", alerts_path=str(isolate_alerts_log)),
    )
    st = _state(0.47, 0.49, 100.0, 100.0, ts=clock.now)

    assert risk.precheck(st, 0.47, 0.49) == "macro state stale"
    assert risk.precheck(st, 0.47, 0.49) == "macro state stale"
    assert len(_macro_alert_records(isolate_alerts_log)) == 1

    state.last_update = clock.now
    assert risk.precheck(st, 0.47, 0.49) is None
    assert risk._active_macro_failure is None

    clock.now += 901.0
    assert risk.precheck(st, 0.47, 0.49) == "macro state stale"
    records = _macro_alert_records(isolate_alerts_log)
    assert [record["failure"] for record in records] == ["stale", "stale"]
    assert [record["age_seconds"] for record in records] == [901.0, 901.0]
