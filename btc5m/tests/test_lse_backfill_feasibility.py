from __future__ import annotations

import json

import httpx
import pytest

from models.lse_backfill_feasibility import (
    ExportProbe,
    _absolute_url,
    classify_backfill_feasibility,
    live_observation_path,
    load_observed_window_count,
    poll_export_result,
    probe_candle_series,
)


def test_classification_refuses_historical_btc_proxy_as_labels():
    rest = {
        "candle_series_probes": [
            {
                "timeframe": "1m",
                "lookback_days": 365,
                "ok": True,
                "status_code": 200,
                "item_count": 10,
                "keys": ["close", "timestamp"],
            },
            {
                "timeframe": "5m",
                "lookback_days": 30,
                "ok": True,
                "status_code": 200,
                "item_count": 20,
                "keys": ["close", "timestamp"],
            },
        ],
        "export_probe": {
            "accepted": True,
            "result_retrievable": True,
            "item_count": 100,
        },
    }
    websocket = {"ok": True, "messages": [{"type": "tick", "has_price": True}]}

    result = classify_backfill_feasibility(rest, websocket)

    assert result["historical_btc_price_available"] is True
    assert result["historical_candle_timeframes_available"] == ["1m", "5m"]
    assert result["max_candle_lookback_days_tested"] == 365
    assert result["max_candle_items_returned"] == 20
    assert result["historical_tick_export_accepted"] is True
    assert result["historical_tick_export_result_retrievable"] is True
    assert result["realtime_btc_ticker_available"] is True
    assert result["venue_settlement_labels_available"] is False
    assert result["can_create_ml_labels"] is False
    assert result["can_train_model"] is False
    assert "not venue settlement labels" in result["reason"]


def test_classification_reports_no_historical_depth_when_rest_unavailable():
    rest = {
        "candle_series_probes": [
            {
                "timeframe": "1m",
                "lookback_days": 7,
                "ok": False,
                "status_code": 401,
                "item_count": 0,
            }
        ],
        "export_probe": {
            "accepted": True,
            "result_retrievable": False,
        },
    }

    result = classify_backfill_feasibility(rest, {"ok": False, "messages": []})

    assert result["historical_btc_price_available"] is False
    assert result["historical_tick_export_accepted"] is True
    assert result["historical_tick_export_result_retrievable"] is False
    assert result["realtime_btc_ticker_available"] is False
    assert result["can_create_ml_labels"] is False
    assert "No usable LSE historical BTC/USD series" in result["reason"]


def test_live_observation_path_uses_current_two_windows_per_fifteen_rate():
    result = live_observation_path(observed_windows=2)

    assert result["additional_windows_for_minimum"] == 28
    assert result["additional_minutes_for_minimum"] == 210.0
    assert result["additional_windows_for_preferred"] == 98
    assert result["additional_minutes_for_preferred"] == 735.0
    assert result["from_scratch_minutes_for_minimum"] == 225.0
    assert result["from_scratch_minutes_for_preferred"] == 750.0


def test_load_observed_window_count_deduplicates_tickers(tmp_path):
    log_path = tmp_path / "cross_venue_log.jsonl"
    records = [
        {"kalshi": {"ticker": "KXBTC15M-A"}},
        {"kalshi": {"ticker": "KXBTC15M-A"}},
        {"kalshi": {"ticker": "KXBTC15M-B"}},
        {"kalshi": {"ticker": ""}},
    ]
    log_path.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )

    assert load_observed_window_count(log_path) == 2


def test_absolute_url_handles_export_relative_paths():
    assert (
        _absolute_url("https://api.londonstrategicedge.com/vault", "download/file.csv")
        == "https://api.londonstrategicedge.com/vault/download/file.csv"
    )
    assert (
        _absolute_url("https://api.londonstrategicedge.com/vault", "/export/job-1")
        == "https://api.londonstrategicedge.com/vault/export/job-1"
    )
    assert (
        _absolute_url("https://api.londonstrategicedge.com/vault", "/vault/export/job-1/download")
        == "https://api.londonstrategicedge.com/vault/export/job-1/download"
    )


@pytest.mark.asyncio
async def test_export_poll_downloads_ready_relative_result(monkeypatch):
    captured: dict[str, str] = {}

    async def fake_download(url: str, *, base_url: str):
        captured["url"] = url
        captured["base_url"] = base_url
        return {
            "result_retrievable": True,
            "item_count": 2,
            "status_code": 200,
            "content_type": "application/json",
            "downloaded_bytes": 128,
            "keys": ["price", "ts"],
            "sample_time_min": "2026-08-03T00:00:00Z",
            "sample_time_max": "2026-08-03T00:00:01Z",
        }

    monkeypatch.setattr("models.lse_backfill_feasibility.download_export_url", fake_download)

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://api.londonstrategicedge.com/vault/export/job-1"
        return httpx.Response(
            200,
            json={
                "status": "ready",
                "download_url": "/vault/export/job-1/download",
                "rows": 2,
                "bytes": 128,
            },
        )

    async with httpx.AsyncClient(
        base_url="https://api.londonstrategicedge.com/vault",
        transport=httpx.MockTransport(handler),
    ) as client:
        result = await poll_export_result(
            client,
            ExportProbe(
                name="vault_export_btcusd_tick",
                url="https://api.londonstrategicedge.com/vault/export",
                accepted=True,
                job_id="job-1",
            ),
            max_polls=1,
            poll_interval_seconds=0,
        )

    assert result.result_retrievable is True
    assert result.download_attempted is True
    assert result.rows_reported == 2


@pytest.mark.asyncio
async def test_candle_probe_records_pagination_metadata():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [{"ts": "2026-08-03T00:00:00Z", "close": 65000}],
                "next": "/candles?cursor=abc",
                "meta": {"cursor": "abc"},
            },
        )

    async with httpx.AsyncClient(
        base_url="https://api.londonstrategicedge.com/vault",
        transport=httpx.MockTransport(handler),
    ) as client:
        result = await probe_candle_series(
            client, timeframe="1m", lookback_days=7, limit=10
        )

    assert result.ok is True
    assert set(result.pagination_keys) == {"next", "meta.cursor"}
    assert result.item_count == 1
