"""Regression coverage for the isolated validation-backtest configuration."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import polymarket_arb.config as config
import polymarket_arb.backtest_engine as backtest_engine
import polymarket_arb.risk as risk
from polymarket_arb.config import Settings
import pytest
from scripts import validate_backtest


def test_extract_paper_trading_pnl_matches_fill_fidelity_archive():
    artifact = Path(__file__).parents[1] / "state" / "fill_fidelity_validation_1.jsonl"

    metrics = validate_backtest.extract_paper_trading_pnl(str(artifact))

    assert metrics["total_sessions"] == 1
    assert metrics["total_opps"] == 8
    assert metrics["total_fills"] == 8
    assert metrics["total_missed"] == 221
    assert metrics["total_locked_pnl"] == pytest.approx(1.90)
    assert metrics["total_session_pnl"] == pytest.approx(1.90)
    assert metrics["fills_by_band"] == {"fresh": 8, "0.5-2s": 0, "2-10s": 0, "stale": 0}
    assert metrics["fresh_attempts"] == {"fresh": 8, "0.5-2s": 49, "2-10s": 113, "stale": 33}
    assert metrics["total_sent"] == 16
    assert metrics["total_loops"] == 14_013


def test_validation_backtest_lifts_default_total_capital(monkeypatch, tmp_path):
    """The replay must not inherit the $1,000 live-risk capital cap."""
    default_capital = 1_000.0
    original_settings = Settings(max_total_capital=default_capital)
    monkeypatch.setattr(config, "SETTINGS", original_settings)
    monkeypatch.setattr(backtest_engine, "SETTINGS", original_settings)
    monkeypatch.setattr(risk, "SETTINGS", original_settings)
    monkeypatch.setattr(validate_backtest, "STATE_DIR", tmp_path)

    captured = {}

    class StubBacktestEngine:
        def __init__(self, backtest_config):
            captured["settings"] = config.SETTINGS

        async def run(self):
            return object()

        async def aclose(self):
            pass

    monkeypatch.setattr(validate_backtest, "BacktestEngine", StubBacktestEngine)

    result = asyncio.run(validate_backtest.run_backtest(ticks=[]))

    assert result is not None
    assert captured["settings"].max_total_capital == 10_000_000.0
    assert captured["settings"].max_total_capital != default_capital
    assert config.SETTINGS is original_settings
    assert backtest_engine.SETTINGS is original_settings
    assert risk.SETTINGS is original_settings
    assert not (tmp_path / "_validate_backtest_ticks.csv").exists()


def test_run_backtest_restores_globals_closes_engine_and_deletes_csv_on_failure(monkeypatch, tmp_path):
    original_settings = Settings(max_total_capital=1_000.0)
    monkeypatch.setattr(config, "SETTINGS", original_settings)
    monkeypatch.setattr(backtest_engine, "SETTINGS", original_settings)
    monkeypatch.setattr(risk, "SETTINGS", original_settings)
    monkeypatch.setattr(validate_backtest, "STATE_DIR", tmp_path)
    events = []

    class FailingBacktestEngine:
        def __init__(self, backtest_config):
            events.append("initialized")
            assert config.SETTINGS.max_total_capital == 10_000_000.0

        async def run(self):
            events.append("run")
            raise RuntimeError("replay failed")

        async def aclose(self):
            events.append("closed")

    monkeypatch.setattr(validate_backtest, "BacktestEngine", FailingBacktestEngine)

    with pytest.raises(RuntimeError, match="replay failed"):
        asyncio.run(validate_backtest.run_backtest(ticks=[]))

    assert events == ["initialized", "run", "closed"]
    assert config.SETTINGS is original_settings
    assert backtest_engine.SETTINGS is original_settings
    assert risk.SETTINGS is original_settings
    assert not (tmp_path / "_validate_backtest_ticks.csv").exists()


class _UnavailableLSE:
    async def start_tick_export(self, **kwargs):
        raise RuntimeError("LSE unavailable")

    async def fetch_candles(self, **kwargs):
        return []

    async def aclose(self):
        pass


class _CandleOnlyLSE:
    candle_fetches = 0

    async def start_tick_export(self, **kwargs):
        raise RuntimeError("tick export unavailable")

    async def fetch_candles(self, **kwargs):
        type(self).candle_fetches += 1
        return [
            {"timestamp": 1.0, "close": 100.0, "volume": 2.0},
            {"timestamp": 61.0, "close": 101.0, "volume": 3.0},
        ]

    async def aclose(self):
        pass


def test_main_requires_explicit_opt_in_before_synthetic_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(validate_backtest, "STATE_DIR", tmp_path)
    monkeypatch.setattr(validate_backtest, "LSEClient", _UnavailableLSE)

    async def unexpected_backtest(**kwargs):
        raise AssertionError("synthetic fallback must not run without opt-in")

    monkeypatch.setattr(validate_backtest, "run_backtest", unexpected_backtest)

    assert asyncio.run(validate_backtest.main()) == 2


def test_main_default_refuses_candle_derived_books(monkeypatch, tmp_path):
    _CandleOnlyLSE.candle_fetches = 0
    monkeypatch.setattr(validate_backtest, "STATE_DIR", tmp_path)
    monkeypatch.setattr(validate_backtest, "LSEClient", _CandleOnlyLSE)

    async def unexpected_backtest(**kwargs):
        raise AssertionError("candle-derived replay must not run by default")

    monkeypatch.setattr(validate_backtest, "run_backtest", unexpected_backtest)

    assert asyncio.run(validate_backtest.main()) == 2
    assert _CandleOnlyLSE.candle_fetches == 0


def test_candle_converter_requires_explicit_structural_opt_in():
    with pytest.raises(
        validate_backtest.CandleDerivedStructuralOptInRequired,
        match="structural opt-in",
    ):
        validate_backtest.candles_to_prob_ticks(
            [{"timestamp": 1.0, "close": 100.0, "volume": 2.0}]
        )


def test_main_labels_candle_opt_in_noncomparable(monkeypatch, tmp_path):
    _CandleOnlyLSE.candle_fetches = 0
    monkeypatch.setattr(validate_backtest, "STATE_DIR", tmp_path)
    monkeypatch.setattr(validate_backtest, "LSEClient", _CandleOnlyLSE)
    captured = {}

    async def stub_backtest(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(validate_backtest, "run_backtest", stub_backtest)
    monkeypatch.setattr(validate_backtest, "extract_paper_trading_pnl", lambda path: {})
    monkeypatch.setattr(
        validate_backtest,
        "compare_and_report",
        lambda *args: {
            "consistency_check": {
                "flagged_for_review": False,
                "directionally_consistent": True,
            }
        },
    )
    monkeypatch.setattr(validate_backtest, "print_report", lambda report: None)

    assert asyncio.run(validate_backtest.main(allow_synthetic=True)) == 0
    assert _CandleOnlyLSE.candle_fetches == 1
    assert captured["source_label"] == "candle_derived_structural_opt_in"
    assert len(captured["ticks"]) == 2


def test_main_allows_explicit_synthetic_structural_test(monkeypatch, tmp_path):
    monkeypatch.setattr(validate_backtest, "STATE_DIR", tmp_path)
    monkeypatch.setattr(validate_backtest, "LSEClient", _UnavailableLSE)
    captured = {}

    async def stub_backtest(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(validate_backtest, "run_backtest", stub_backtest)
    monkeypatch.setattr(validate_backtest, "extract_paper_trading_pnl", lambda path: {})
    monkeypatch.setattr(
        validate_backtest,
        "compare_and_report",
        lambda *args: {"consistency_check": {"flagged_for_review": False, "directionally_consistent": True}},
    )
    monkeypatch.setattr(validate_backtest, "print_report", lambda report: None)

    assert asyncio.run(validate_backtest.main(allow_synthetic=True)) == 0
    assert captured["source_label"] == "synthetic_opt_in"
    assert len(captured["ticks"]) == 5_000


def test_backtest_only_misses_are_reported_as_unknown_without_reason_evidence():
    metrics = SimpleNamespace(
        opportunities=10,
        fills=8,
        partial_fills=0,
        partial_resolved=0,
        missed=2,
        locked_pnl=1.0,
        residual_pnl=0.0,
        session_pnl=1.0,
        would_lock_pnl=0.0,
        partial_rate=0.0,
        fills_by_band={},
        fresh_attempts={},
        avg_opp_to_order_ms=lambda: 0.0,
        p50_opp_to_order_ms=lambda: 0.0,
    )
    result = SimpleNamespace(metrics=metrics, total_ticks=10, duration_s=1.0)

    report = validate_backtest.compare_and_report(
        result, {}, "polymarket_outcome_book", 10
    )

    miss_finding = next(finding for finding in report["findings"] if "Backtest-only misses" in finding)
    assert "UNKNOWN" in miss_finding
    assert "Root cause:" not in miss_finding
    assert report["consistency_check"]["validated_against_real_data"] is True

    lse_spot_report = validate_backtest.compare_and_report(
        result, {}, "lse_tick_export", 10
    )
    assert lse_spot_report["validation_status"] == "NOT VALIDATED / NON-COMPARABLE"
    assert lse_spot_report["comparable_to_outcome_books"] is False
    assert lse_spot_report["consistency_check"]["validated_against_real_data"] is False
    assert any("BTC/USD spot" in finding for finding in lse_spot_report["findings"])

    converted_spot_report = validate_backtest.compare_and_report(
        result, {}, "lse_tick_export_converted", 10
    )
    assert converted_spot_report["validation_status"] == "NOT VALIDATED / NON-COMPARABLE"
    assert converted_spot_report["comparable_to_outcome_books"] is False
    assert converted_spot_report["consistency_check"]["validated_against_real_data"] is False
    assert any("synthetic YES/NO books" in finding for finding in converted_spot_report["findings"])
    assert converted_spot_report["backtest"]["go_live_pnl_metric"] is None
    assert converted_spot_report["normalized_economics"]["comparison_permitted"] is False
    assert converted_spot_report["normalized_economics"]["consistent_within_3x"] is None
    assert converted_spot_report["consistency_check"]["directionally_consistent"] is None
    assert converted_spot_report["consistency_check"]["discrepancy_ratio"] is None
    assert not any("live bot missed" in finding for finding in converted_spot_report["findings"])
    assert any("comparison suppressed" in finding.lower() for finding in converted_spot_report["findings"])

    synthetic_report = validate_backtest.compare_and_report(result, {}, "synthetic_opt_in", 10)
    assert synthetic_report["consistency_check"]["validated_against_real_data"] is False
    assert any("NOT VALIDATED" in finding for finding in synthetic_report["findings"])

    candle_report = validate_backtest.compare_and_report(
        result, {}, "candle_derived_structural_opt_in", 10
    )
    assert candle_report["validation_status"] == "NOT VALIDATED / NON-COMPARABLE"
    assert candle_report["comparable_to_outcome_books"] is False
    assert candle_report["consistency_check"]["validated_against_real_data"] is False
    assert any(
        "NOT VALIDATED / NON-COMPARABLE" in finding
        for finding in candle_report["findings"]
    )

    contextual_report = validate_backtest.compare_and_report(
        result,
        {"total_session_pnl": 0.1, "total_opps": 1, "total_fills": 1, "total_sessions": 1},
        "polymarket_outcome_book",
        10,
    )
    context_finding = next(finding for finding in contextual_report["findings"] if finding.startswith("  Context:"))
    assert "random non-fills" not in context_finding
    assert "8 fills vs 1 fills" in context_finding


def test_report_distinguishes_capped_actual_pnl_from_requested_quantity_equivalent():
    metrics = SimpleNamespace(
        opportunities=391,
        fills=391,
        partial_fills=0,
        partial_resolved=0,
        missed=0,
        locked_pnl=34.4875,
        residual_pnl=0.0,
        session_pnl=34.4875,
        would_lock_pnl=0.0,
        partial_rate=0.0,
        fills_by_band={},
        fresh_attempts={},
        avg_opp_to_order_ms=lambda: 0.0,
        p50_opp_to_order_ms=lambda: 0.0,
    )
    result = SimpleNamespace(
        metrics=metrics,
        total_ticks=500_000,
        duration_s=1.0,
        fill_summary={
            "requested_buy_quantity": 7820.0,
            "filled_buy_quantity": 3910.0,
            "requested_quantity_fill_rate": 0.5,
        },
    )

    report = validate_backtest.compare_and_report(
        result, {}, "polymarket_outcome_book", 500_000
    )

    assert report["backtest"]["locked_pnl"] == pytest.approx(34.4875)
    assert report["backtest"]["uncapped_requested_quantity_equivalent_pnl"] == pytest.approx(68.975)
    assert report["backtest"]["go_live_pnl_metric"] == "locked_pnl"
    assert any("capped actual" in finding for finding in report["findings"])
