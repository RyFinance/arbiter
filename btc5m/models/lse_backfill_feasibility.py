"""Probe LSE backfill capability for cross-venue ML labels.

This utility is deliberately conservative.  Historical BTC/USD candles can
help describe market regimes, but they are not settled labels for whether a
Kalshi CF Benchmarks BRTI contract and a Polymarket Chainlink BTC/USD contract
resolved equivalently.  The report only marks labels as available when a real
source of venue settlement outcomes is present.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

import httpx

from polymarket_arb.clients.lse_client import (
    LSE_BASE_URL,
    LSE_VAULT_URL,
    LSE_WS_URL,
    _LSE_PLACEHOLDER_KEY,
)
from polymarket_arb.config import SETTINGS


DEFAULT_OBSERVER_LOG = Path("state/cross_venue_log.jsonl")
DEFAULT_REPORT_PATH = Path("state/lse_backfill_feasibility.json")
DEFAULT_DOC_PATH = Path("docs/lse_backfill_feasibility.md")
CURRENT_OBSERVED_WINDOWS_PER_15_MIN = 2.0
MIN_LABELED_WINDOWS = 30
PREFERRED_LABELED_WINDOWS = 100


@dataclass(frozen=True)
class EndpointProbe:
    name: str
    url: str
    ok: bool
    status_code: Optional[int] = None
    item_count: int = 0
    keys: tuple[str, ...] = ()
    sample_time_min: Optional[str] = None
    sample_time_max: Optional[str] = None
    error: Optional[str] = None


@dataclass(frozen=True)
class CandleSeriesProbe:
    timeframe: str
    lookback_days: int
    limit_requested: int
    ok: bool
    status_code: Optional[int] = None
    item_count: int = 0
    keys: tuple[str, ...] = ()
    pagination_keys: tuple[str, ...] = ()
    sample_time_min: Optional[str] = None
    sample_time_max: Optional[str] = None
    error: Optional[str] = None


@dataclass(frozen=True)
class ExportProbe:
    name: str
    url: str
    accepted: bool
    status_code: Optional[int] = None
    job_id: Optional[str] = None
    poll_url: Optional[str] = None
    terminal_status: Optional[str] = None
    result_retrievable: bool = False
    item_count: int = 0
    rows_reported: Optional[int] = None
    bytes_reported: Optional[int] = None
    download_attempted: bool = False
    download_status_code: Optional[int] = None
    download_content_type: Optional[str] = None
    downloaded_bytes: int = 0
    download_error: Optional[str] = None
    keys: tuple[str, ...] = ()
    sample_time_min: Optional[str] = None
    sample_time_max: Optional[str] = None
    polls_attempted: int = 0
    error: Optional[str] = None


def _safe_error(exc: Exception) -> str:
    return f"{exc.__class__.__name__}: {exc}"


def _extract_items(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "candles", "results", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


def _reported_url(client: httpx.AsyncClient, path: str) -> str:
    return f"{str(client.base_url).rstrip('/')}/{path.lstrip('/')}"


def _absolute_url(base_url: str, url: str) -> str:
    if url.startswith("http://") or url.startswith("https://"):
        return url
    relative = url.lstrip("/")
    base = base_url.rstrip("/")
    base_path = urlparse(base).path.strip("/")
    if base_path and relative.startswith(f"{base_path}/"):
        root = base.removesuffix(f"/{base_path}")
        return urljoin(f"{root}/", relative)
    return urljoin(f"{base}/", relative)


def _item_keys_and_times(items: list[Any]) -> tuple[tuple[str, ...], Optional[str], Optional[str]]:
    keys: tuple[str, ...] = ()
    if items and isinstance(items[0], dict):
        keys = tuple(sorted(str(k) for k in items[0].keys()))
    sample_times = sorted(
        str(item.get("ts") or item.get("timestamp") or item.get("time"))
        for item in items
        if isinstance(item, dict) and (item.get("ts") or item.get("timestamp") or item.get("time"))
    )
    return keys, sample_times[0] if sample_times else None, sample_times[-1] if sample_times else None


def _pagination_keys(payload: Any) -> tuple[str, ...]:
    if not isinstance(payload, dict):
        return ()
    keys = []
    for key in ("next", "next_url", "cursor", "next_cursor", "page", "pages", "has_more"):
        if payload.get(key) not in (None, "", False):
            keys.append(key)
    meta = payload.get("meta") or payload.get("pagination")
    if isinstance(meta, dict):
        for key in ("next", "next_url", "cursor", "next_cursor", "page", "pages", "has_more"):
            if meta.get(key) not in (None, "", False):
                keys.append(f"meta.{key}" if payload.get("meta") is meta else f"pagination.{key}")
    return tuple(sorted(set(keys)))


async def probe_rest_endpoint(
    client: httpx.AsyncClient,
    name: str,
    path: str,
    *,
    method: str = "GET",
    params: Optional[dict[str, str]] = None,
    json_body: Optional[dict[str, Any]] = None,
) -> EndpointProbe:
    try:
        response = await client.request(method, path, params=params, json=json_body)
        payload: Any
        try:
            payload = response.json()
        except ValueError:
            payload = None
        items = _extract_items(payload)
        keys, sample_time_min, sample_time_max = _item_keys_and_times(items)
        if not keys and isinstance(payload, dict):
            keys = tuple(sorted(str(k) for k in payload.keys()))
        return EndpointProbe(
            name=name,
            url=_reported_url(client, path),
            ok=200 <= response.status_code < 300,
            status_code=response.status_code,
            item_count=len(items),
            keys=keys,
            sample_time_min=sample_time_min,
            sample_time_max=sample_time_max,
        )
    except Exception as exc:
        return EndpointProbe(
            name=name,
            url=_reported_url(client, path),
            ok=False,
            error=_safe_error(exc),
        )


async def probe_candle_series(
    client: httpx.AsyncClient,
    *,
    timeframe: str,
    lookback_days: int,
    limit: int,
) -> CandleSeriesProbe:
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=lookback_days)
    params = {
        "symbol": "BTC/USD",
        "timeframe": timeframe,
        "start": start.strftime("%Y-%m-%d"),
        "end": end.strftime("%Y-%m-%d"),
        "limit": str(limit),
    }
    try:
        response = await client.get("/candles", params=params)
        try:
            payload = response.json()
        except ValueError:
            payload = None
        items = _extract_items(payload)
        keys, sample_time_min, sample_time_max = _item_keys_and_times(items)
        pagination_keys = _pagination_keys(payload)
        if not keys and isinstance(payload, dict):
            keys = tuple(sorted(str(k) for k in payload.keys()))
        return CandleSeriesProbe(
            timeframe=timeframe,
            lookback_days=lookback_days,
            limit_requested=limit,
            ok=200 <= response.status_code < 300 and bool(items),
            status_code=response.status_code,
            item_count=len(items),
            keys=keys,
            pagination_keys=pagination_keys,
            sample_time_min=sample_time_min,
            sample_time_max=sample_time_max,
        )
    except Exception as exc:
        return CandleSeriesProbe(
            timeframe=timeframe,
            lookback_days=lookback_days,
            limit_requested=limit,
            ok=False,
            error=_safe_error(exc),
        )


async def probe_export_job(
    client: httpx.AsyncClient,
    *,
    max_polls: int = 4,
    poll_interval_seconds: float = 1.0,
) -> ExportProbe:
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=1)
    try:
        response = await client.post(
            "/export",
            json={
                "dataset": "crypto",
                "symbol": "BTC/USD",
                "timeframe": "tick",
                "start": start.strftime("%Y-%m-%d"),
                "end": end.strftime("%Y-%m-%d"),
            },
        )
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        job_id = payload.get("job_id") or payload.get("jobId")
        poll_url = payload.get("poll") or payload.get("poll_url") or payload.get("pollUrl")
        accepted = 200 <= response.status_code < 300 and bool(job_id or poll_url)
        probe = ExportProbe(
            name="vault_export_btcusd_tick",
            url=_reported_url(client, "/export"),
            accepted=accepted,
            status_code=response.status_code,
            job_id=str(job_id) if job_id else None,
            poll_url=str(poll_url) if poll_url else None,
            keys=tuple(sorted(str(k) for k in payload.keys())),
        )
        if not accepted:
            return probe
        return await poll_export_result(client, probe, max_polls=max_polls, poll_interval_seconds=poll_interval_seconds)
    except Exception as exc:
        return ExportProbe(
            name="vault_export_btcusd_tick",
            url=_reported_url(client, "/export"),
            accepted=False,
            error=_safe_error(exc),
        )


async def _fetch_export_poll(client: httpx.AsyncClient, probe: ExportProbe) -> httpx.Response:
    if probe.poll_url:
        if probe.poll_url.startswith("http://") or probe.poll_url.startswith("https://"):
            return await client.get(probe.poll_url)
        return await client.get(probe.poll_url)
    return await client.get(f"/export/{probe.job_id}")


async def poll_export_result(
    client: httpx.AsyncClient,
    probe: ExportProbe,
    *,
    max_polls: int,
    poll_interval_seconds: float,
) -> ExportProbe:
    polls_attempted = 0
    last_status = None
    last_keys = probe.keys
    for attempt in range(max_polls):
        polls_attempted = attempt + 1
        try:
            response = await _fetch_export_poll(client, probe)
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            if not isinstance(payload, dict):
                payload = {"data": payload}
            last_keys = tuple(sorted(str(k) for k in payload.keys()))
            status = str(payload.get("status") or "").lower()
            last_status = status or f"http_{response.status_code}"
            result = payload.get("result") or payload.get("data")
            download_url = payload.get("download_url") or payload.get("downloadUrl") or payload.get("url")
            rows_reported = _optional_int(payload.get("rows"))
            bytes_reported = _optional_int(payload.get("bytes"))
            items = _extract_items(result)
            if not items and isinstance(result, list):
                items = result
            if items:
                keys, sample_time_min, sample_time_max = _item_keys_and_times(items)
                return ExportProbe(
                    **{
                        **probe.__dict__,
                        "terminal_status": status or "data_inline",
                        "result_retrievable": True,
                        "item_count": len(items),
                        "keys": keys,
                        "sample_time_min": sample_time_min,
                        "sample_time_max": sample_time_max,
                        "polls_attempted": polls_attempted,
                    }
                )
            if download_url:
                downloaded = await download_export_url(download_url, base_url=str(client.base_url))
                if downloaded["result_retrievable"]:
                    return ExportProbe(
                        **{
                            **probe.__dict__,
                            "terminal_status": status or "download_url",
                            "result_retrievable": True,
                            "item_count": downloaded["item_count"],
                            "rows_reported": rows_reported,
                            "bytes_reported": bytes_reported,
                            "download_attempted": True,
                            "download_status_code": downloaded["status_code"],
                            "download_content_type": downloaded["content_type"],
                            "downloaded_bytes": downloaded["downloaded_bytes"],
                            "download_error": downloaded.get("error"),
                            "keys": tuple(downloaded["keys"]),
                            "sample_time_min": downloaded["sample_time_min"],
                            "sample_time_max": downloaded["sample_time_max"],
                            "polls_attempted": polls_attempted,
                        }
                    )
                if status in {"ready", "completed", "complete"}:
                    return ExportProbe(
                        **{
                            **probe.__dict__,
                            "terminal_status": status,
                            "result_retrievable": False,
                            "item_count": downloaded["item_count"],
                            "rows_reported": rows_reported,
                            "bytes_reported": bytes_reported,
                            "download_attempted": True,
                            "download_status_code": downloaded["status_code"],
                            "download_content_type": downloaded["content_type"],
                            "downloaded_bytes": downloaded["downloaded_bytes"],
                            "download_error": downloaded.get("error"),
                            "keys": tuple(downloaded["keys"]) or last_keys,
                            "sample_time_min": downloaded["sample_time_min"],
                            "sample_time_max": downloaded["sample_time_max"],
                            "polls_attempted": polls_attempted,
                        }
                    )
            if status in {"failed", "error"}:
                return ExportProbe(
                    **{
                        **probe.__dict__,
                        "terminal_status": status,
                        "rows_reported": rows_reported,
                        "bytes_reported": bytes_reported,
                        "download_attempted": bool(download_url),
                        "keys": last_keys,
                        "polls_attempted": polls_attempted,
                    }
                )
        except Exception as exc:
            return ExportProbe(
                **{
                    **probe.__dict__,
                    "terminal_status": last_status,
                    "polls_attempted": polls_attempted,
                    "error": _safe_error(exc),
                }
            )
        await asyncio.sleep(poll_interval_seconds)
    return ExportProbe(
        **{
            **probe.__dict__,
            "terminal_status": last_status or "poll_timeout",
            "keys": last_keys,
            "polls_attempted": polls_attempted,
        }
    )


def _optional_int(value: Any) -> Optional[int]:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


async def download_export_url(url: str, *, base_url: str = LSE_VAULT_URL) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(_absolute_url(base_url, url))
            content_type = response.headers.get("content-type")
            content = response.content
            try:
                payload = response.json()
            except ValueError:
                payload = None
        items = _extract_items(payload)
        keys, sample_time_min, sample_time_max = _item_keys_and_times(items)
        non_json_data = payload is None and bool(content.strip())
        return {
            "result_retrievable": 200 <= response.status_code < 300 and (bool(items) or non_json_data),
            "item_count": len(items),
            "status_code": response.status_code,
            "content_type": content_type,
            "downloaded_bytes": len(content),
            "keys": list(keys),
            "sample_time_min": sample_time_min,
            "sample_time_max": sample_time_max,
        }
    except Exception as exc:
        return {
            "result_retrievable": False,
            "item_count": 0,
            "status_code": None,
            "content_type": None,
            "downloaded_bytes": 0,
            "error": _safe_error(exc),
            "keys": [],
            "sample_time_min": None,
            "sample_time_max": None,
        }


async def probe_lse_rest(api_key: str) -> dict[str, Any]:
    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
    }
    async with httpx.AsyncClient(
        base_url=LSE_VAULT_URL,
        headers=headers,
        timeout=20.0,
    ) as client:
        candle_probes = [
            await probe_candle_series(client, timeframe="1m", lookback_days=7, limit=10),
            await probe_candle_series(client, timeframe="1m", lookback_days=30, limit=50),
            await probe_candle_series(client, timeframe="1m", lookback_days=365, limit=10),
            await probe_candle_series(client, timeframe="5m", lookback_days=30, limit=20),
            await probe_candle_series(client, timeframe="15m", lookback_days=30, limit=20),
            await probe_candle_series(client, timeframe="1h", lookback_days=365, limit=20),
        ]
        export_probe = await probe_export_job(client)
        successful_candles = [probe for probe in candle_probes if probe.ok and probe.item_count > 0]
        pagination_keys = sorted({key for probe in candle_probes for key in probe.pagination_keys})
        return {
            "candle_series_probes": [candle_series_probe_to_dict(probe) for probe in candle_probes],
            "candle_series_summary": {
                "tested_timeframes": sorted({probe.timeframe for probe in candle_probes}),
                "confirmed_timeframes": sorted({probe.timeframe for probe in successful_candles}),
                "tested_lookback_days": sorted({probe.lookback_days for probe in candle_probes}),
                "confirmed_lookback_days": sorted({probe.lookback_days for probe in successful_candles}),
                "tested_limits": sorted({probe.limit_requested for probe in candle_probes}),
                "max_limit_requested": max((probe.limit_requested for probe in candle_probes), default=0),
                "max_items_returned": max((probe.item_count for probe in candle_probes), default=0),
                "pagination_discovered": bool(pagination_keys),
                "pagination_keys_observed": pagination_keys,
                "pagination_note": (
                    f"Observed pagination metadata keys: {', '.join(pagination_keys)}"
                    if pagination_keys
                    else "No next/page cursor metadata was observed in bounded candle responses."
                ),
            },
            "export_probe": export_probe_to_dict(export_probe),
        }


async def probe_lse_websocket(api_key: str, timeout_seconds: float = 8.0) -> dict[str, Any]:
    try:
        import websockets
    except Exception as exc:
        return {"ok": False, "url": LSE_WS_URL, "error": _safe_error(exc), "messages": []}

    messages: list[dict[str, Any]] = []
    try:
        async with websockets.connect(
            LSE_WS_URL,
            additional_headers={},
            ping_interval=None,
            close_timeout=3,
        ) as ws:
            if api_key and api_key != _LSE_PLACEHOLDER_KEY:
                await ws.send(json.dumps({"action": "auth", "api_key": api_key}))
            await ws.send(json.dumps({"action": "subscribe", "symbol": "BTC/USD"}))

            deadline = asyncio.get_running_loop().time() + timeout_seconds
            while asyncio.get_running_loop().time() < deadline:
                remaining = max(0.1, deadline - asyncio.get_running_loop().time())
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                except asyncio.TimeoutError:
                    break
                try:
                    payload = json.loads(raw)
                except (TypeError, ValueError):
                    payload = {"type": "unparseable"}
                sanitized = {
                    "type": payload.get("type"),
                    "symbol": payload.get("symbol"),
                    "has_price": payload.get("price") is not None,
                    "keys": sorted(str(k) for k in payload.keys()),
                }
                messages.append(sanitized)
                if payload.get("type") == "tick" and payload.get("price") is not None:
                    break
        return {
            "ok": any(msg.get("type") == "tick" and msg.get("has_price") for msg in messages),
            "url": LSE_WS_URL,
            "messages": messages,
        }
    except Exception as exc:
        return {"ok": False, "url": LSE_WS_URL, "error": _safe_error(exc), "messages": messages}


def load_observed_window_count(path: Path = DEFAULT_OBSERVER_LOG) -> int:
    if not path.exists():
        return 0
    tickers: set[str] = set()
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            ticker = ((record.get("kalshi") or {}).get("ticker") or "")
            if ticker:
                tickers.add(str(ticker))
    return len(tickers)


def live_observation_path(
    observed_windows: int,
    *,
    windows_per_15_min: float = CURRENT_OBSERVED_WINDOWS_PER_15_MIN,
) -> dict[str, Any]:
    def minutes_for(target: int) -> float:
        remaining = max(0, target - observed_windows)
        return (remaining / windows_per_15_min) * 15.0

    return {
        "observed_unlabeled_windows": observed_windows,
        "observed_window_rate_per_15_minutes": windows_per_15_min,
        "minimum_labeled_windows_required": MIN_LABELED_WINDOWS,
        "preferred_labeled_windows": PREFERRED_LABELED_WINDOWS,
        "additional_windows_for_minimum": max(0, MIN_LABELED_WINDOWS - observed_windows),
        "additional_minutes_for_minimum": minutes_for(MIN_LABELED_WINDOWS),
        "additional_windows_for_preferred": max(0, PREFERRED_LABELED_WINDOWS - observed_windows),
        "additional_minutes_for_preferred": minutes_for(PREFERRED_LABELED_WINDOWS),
        "from_scratch_minutes_for_minimum": (MIN_LABELED_WINDOWS / windows_per_15_min) * 15.0,
        "from_scratch_minutes_for_preferred": (PREFERRED_LABELED_WINDOWS / windows_per_15_min) * 15.0,
    }


def classify_backfill_feasibility(rest: dict[str, Any], websocket: dict[str, Any]) -> dict[str, Any]:
    candle_probes = rest.get("candle_series_probes") or []
    export_probe = rest.get("export_probe") or {}
    candle_timeframes = sorted({
        str(probe.get("timeframe"))
        for probe in candle_probes
        if probe.get("ok") and probe.get("item_count", 0) > 0
    })
    max_lookback_days_tested = max(
        (int(probe.get("lookback_days") or 0) for probe in candle_probes if probe.get("ok")),
        default=0,
    )
    max_candle_items_returned = max(
        (int(probe.get("item_count") or 0) for probe in candle_probes),
        default=0,
    )
    historical_btc_price_available = bool(candle_timeframes or export_probe.get("result_retrievable"))
    realtime_btc_ticker_available = bool(websocket.get("ok"))
    return {
        "historical_btc_price_available": historical_btc_price_available,
        "historical_candle_timeframes_available": candle_timeframes,
        "max_candle_lookback_days_tested": max_lookback_days_tested,
        "max_candle_items_returned": max_candle_items_returned,
        "historical_tick_export_accepted": bool(export_probe.get("accepted")),
        "historical_tick_export_result_retrievable": bool(export_probe.get("result_retrievable")),
        "realtime_btc_ticker_available": realtime_btc_ticker_available,
        "venue_settlement_labels_available": False,
        "can_create_ml_labels": False,
        "can_train_model": False,
        "reason": (
            "LSE historical BTC/USD prices are not venue settlement labels. "
            "The cross-venue confidence gate needs settled Kalshi/Polymarket "
            "outcomes or verified official oracle boundary values, not a proxy "
            "spot series."
            if historical_btc_price_available else
            "No usable LSE historical BTC/USD series was accessible with the configured key; "
            "the WebSocket probe, if available, is real-time only and cannot backfill past settled windows."
        ),
    }


def endpoint_probe_to_dict(probe: EndpointProbe) -> dict[str, Any]:
    return {
        "name": probe.name,
        "url": probe.url,
        "ok": probe.ok,
        "status_code": probe.status_code,
        "item_count": probe.item_count,
        "keys": list(probe.keys),
        "sample_time_min": probe.sample_time_min,
        "sample_time_max": probe.sample_time_max,
        "error": probe.error,
    }


def candle_series_probe_to_dict(probe: CandleSeriesProbe) -> dict[str, Any]:
    return {
        "timeframe": probe.timeframe,
        "lookback_days": probe.lookback_days,
        "limit_requested": probe.limit_requested,
        "ok": probe.ok,
        "status_code": probe.status_code,
        "item_count": probe.item_count,
        "keys": list(probe.keys),
        "pagination_keys": list(probe.pagination_keys),
        "sample_time_min": probe.sample_time_min,
        "sample_time_max": probe.sample_time_max,
        "error": probe.error,
    }


def export_probe_to_dict(probe: ExportProbe) -> dict[str, Any]:
    return {
        "name": probe.name,
        "url": probe.url,
        "accepted": probe.accepted,
        "status_code": probe.status_code,
        "job_id_present": bool(probe.job_id),
        "poll_url_present": bool(probe.poll_url),
        "terminal_status": probe.terminal_status,
        "result_retrievable": probe.result_retrievable,
        "item_count": probe.item_count,
        "rows_reported": probe.rows_reported,
        "bytes_reported": probe.bytes_reported,
        "download_attempted": probe.download_attempted,
        "download_status_code": probe.download_status_code,
        "download_content_type": probe.download_content_type,
        "downloaded_bytes": probe.downloaded_bytes,
        "download_error": probe.download_error,
        "keys": list(probe.keys),
        "sample_time_min": probe.sample_time_min,
        "sample_time_max": probe.sample_time_max,
        "polls_attempted": probe.polls_attempted,
        "error": probe.error,
    }


async def build_report(
    *,
    api_key: str,
    observer_log: Path = DEFAULT_OBSERVER_LOG,
) -> dict[str, Any]:
    rest = await probe_lse_rest(api_key)
    websocket = await probe_lse_websocket(api_key)
    observed_windows = load_observed_window_count(observer_log)
    feasibility = classify_backfill_feasibility(rest, websocket)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "api": {
            "base_url": LSE_BASE_URL,
            "vault_url": LSE_VAULT_URL,
            "websocket_url": LSE_WS_URL,
            "api_key_configured": bool(api_key),
            "api_key_is_placeholder": api_key == _LSE_PLACEHOLDER_KEY,
        },
        "rest_probes": rest,
        "websocket_probe": websocket,
        "feasibility": feasibility,
        "live_observation_path": live_observation_path(observed_windows),
        "labels_created": 0,
        "model_artifact_created": False,
    }


def write_doc(report: dict[str, Any], path: Path = DEFAULT_DOC_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    candle_summary = report["rest_probes"].get("candle_series_summary") or {}
    export_probe = report["rest_probes"].get("export_probe") or {}
    candle_lines = [
        (
            f"- `{probe['timeframe']}` lookback `{probe['lookback_days']}` days, "
            f"limit `{probe['limit_requested']}`: ok=`{probe['ok']}`, "
            f"status=`{probe['status_code']}`, rows=`{probe['item_count']}`, "
            f"range=`{probe['sample_time_min']}` to `{probe['sample_time_max']}`"
        )
        for probe in report["rest_probes"].get("candle_series_probes", [])
    ]
    path.write_text(
        "\n".join(
            [
                "# LSE Backfill Feasibility",
                "",
                f"Generated: `{report['generated_at']}`",
                "",
                "## Finding",
                "",
                report["feasibility"]["reason"],
                "",
                "No labels or model artifact were created. BTC/USD price history, even if accessible, is a proxy series and not proof of settled Kalshi/Polymarket equivalence.",
                "",
                "## LSE Capability",
                "",
                f"- REST historical BTC/USD available: `{report['feasibility']['historical_btc_price_available']}`",
                f"- Candle timeframes confirmed: `{', '.join(report['feasibility']['historical_candle_timeframes_available']) or 'none'}`",
                f"- Max candle lookback tested successfully: `{report['feasibility']['max_candle_lookback_days_tested']}` days",
                f"- Max candle rows returned in a bounded request: `{report['feasibility']['max_candle_items_returned']}`",
                f"- Candle lookbacks tested: `{', '.join(str(day) for day in candle_summary.get('tested_lookback_days', [])) or 'none'}` days",
                f"- Candle limits tested: `{', '.join(str(limit) for limit in candle_summary.get('tested_limits', [])) or 'none'}`",
                f"- Candle pagination discovered: `{candle_summary.get('pagination_discovered', False)}`",
                f"- Tick export accepted: `{report['feasibility']['historical_tick_export_accepted']}`",
                f"- Tick export status code: `{export_probe.get('status_code')}`",
                f"- Tick export terminal status: `{export_probe.get('terminal_status')}`",
                f"- Tick export polls attempted: `{export_probe.get('polls_attempted')}`",
                f"- Tick export rows reported: `{export_probe.get('rows_reported')}`",
                f"- Tick export bytes reported: `{export_probe.get('bytes_reported')}`",
                f"- Tick export download attempted: `{export_probe.get('download_attempted')}`",
                f"- Tick export download status: `{export_probe.get('download_status_code')}`",
                f"- Tick export result retrievable during bounded poll: `{report['feasibility']['historical_tick_export_result_retrievable']}`",
                f"- WebSocket real-time BTC/USD ticker available: `{report['feasibility']['realtime_btc_ticker_available']}`",
                f"- Venue settlement labels available from LSE: `{report['feasibility']['venue_settlement_labels_available']}`",
                "",
                "## Bounded Candle Probes",
                "",
                *(candle_lines or ["- none"]),
                "",
                "## Fastest Honest Live Path",
                "",
                f"- Current observed rate: `{report['live_observation_path']['observed_window_rate_per_15_minutes']}` Kalshi windows per 15 minutes",
                f"- Already observed but unlabeled: `{report['live_observation_path']['observed_unlabeled_windows']}` windows",
                f"- Additional time to 30 labeled windows: `{report['live_observation_path']['additional_minutes_for_minimum']}` minutes",
                f"- Additional time to 100 labeled windows: `{report['live_observation_path']['additional_minutes_for_preferred']}` minutes",
                f"- From scratch to 30 windows: `{report['live_observation_path']['from_scratch_minutes_for_minimum']}` minutes",
                f"- From scratch to 100 windows: `{report['live_observation_path']['from_scratch_minutes_for_preferred']}` minutes",
                "",
                "The observer should keep writing real records and a separate settlement-labeling pass should only add labels after venue outcomes or official oracle boundary values are verifiably available.",
                "",
            ]
        ),
        encoding="utf-8",
    )


async def async_main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Probe LSE backfill feasibility for cross-venue ML labels.")
    parser.add_argument("--observer-log", default=str(DEFAULT_OBSERVER_LOG))
    parser.add_argument("--report-path", default=str(DEFAULT_REPORT_PATH))
    parser.add_argument("--doc-path", default=str(DEFAULT_DOC_PATH))
    args = parser.parse_args(argv)

    report = await build_report(
        api_key=SETTINGS.lse_api_key,
        observer_log=Path(args.observer_log),
    )
    report_path = Path(args.report_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    write_doc(report, Path(args.doc_path))
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    return asyncio.run(async_main(argv))


if __name__ == "__main__":
    raise SystemExit(main())
