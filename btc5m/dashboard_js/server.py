"""FastAPI server for the JS dashboard.

Serves ``dashboard_js/static/`` at ``/`` plus a JSON API under ``/api/`` that
reads the bot's live state files through ``dashboard_js.readers``.

Endpoints never raise: every read is wrapped and returns a JSON error field
with HTTP 200. If ``dashboard_js.parsers`` (written by a parallel workstream)
is missing, the models/checklist endpoints degrade gracefully instead of
crashing.

The data root defaults to the project root but can be overridden with the
``DASHBOARD_JS_DATA_ROOT`` env var (used by tests so they never touch real
state).
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from dashboard_js import readers

PROJECT_ROOT = Path(__file__).resolve().parent.parent

STATIC_MISSING_HTML = (
    "<html><head><title>dashboard_js</title></head>"
    "<body><h1>static dir not yet written</h1>"
    "<p>dashboard_js/static/ does not exist yet.</p></body></html>"
)

LIVE_FRESH_S = 30.0
HEATMAP_KIND = {"filled": "fill", "partial_resolved": "partial", "missed": "reject", "no_fill": "reject"}
TICKER_OUTCOME = {"filled": "filled", "partial_resolved": "partial", "missed": "missed", "no_fill": "rejected"}


def _now_iso() -> str:
    return readers.to_iso(readers.utc_now()) or ""


def _trade_pnl(data: Dict[str, Any]) -> Optional[float]:
    for key in ("realized_pnl", "pnl_locked", "residual_pnl"):
        val = data.get(key)
        if isinstance(val, (int, float)):
            return float(val)
    return None


def _trade_size(data: Dict[str, Any]) -> Optional[float]:
    val = data.get("size")
    return float(val) if isinstance(val, (int, float)) else None


def _trade_age(data: Dict[str, Any]) -> Optional[float]:
    val = data.get("age_ms")
    return float(val) if isinstance(val, (int, float)) else None


def _trade_combined(data: Dict[str, Any]) -> Optional[float]:
    val = data.get("combined")
    return float(val) if isinstance(val, (int, float)) else None


def _last_valid_ts_and_mtime(bot_path: Path, now: float):
    anchor = None
    try:
        anchor = bot_path.stat().st_mtime
    except OSError:
        pass
    last_ts: Optional[float] = None
    for row in readers.iter_jsonl(bot_path):
        ts = readers.parse_bot_ts(row.get("t"), anchor)
        if ts is not None:
            last_ts = ts
    age = None
    if anchor is not None:
        age = max(0.0, now - anchor)
    return last_ts, anchor, age


def create_app(root: Optional[Path] = None, static_dir: Optional[Path] = None) -> FastAPI:
    if root is None:
        root = Path(os.environ.get("DASHBOARD_JS_DATA_ROOT", PROJECT_ROOT))
    else:
        root = Path(root)
    if static_dir is None:
        static_dir = Path(__file__).resolve().parent / "static"
    else:
        static_dir = Path(static_dir)

    app = FastAPI(title="polymarket_arb dashboard js")

    # ---------------------------------------------------------------- health

    @app.get("/api/health")
    def api_health() -> Dict[str, Any]:
        resp: Dict[str, Any] = {
            "fetched_at_utc": _now_iso(),
            "data_sources": {},
            "session_archives": {"count": 0, "latest": None},
        }
        try:
            now = time.time()
            live_files = [
                ("bot.jsonl", root / "state" / "bot.jsonl"),
                ("alerts.jsonl", root / "state" / "alerts.jsonl"),
                ("order_ledger.jsonl", root / "state" / "order_ledger.jsonl"),
                ("HEARTBEAT", root / "state" / "HEARTBEAT"),
            ]
            for name, path in live_files:
                entry: Dict[str, Any] = {"exists": False, "bytes": None, "mtime_utc": None, "age_s": None, "fresh": False}
                if path.exists():
                    st = path.stat()
                    age = max(0.0, now - st.st_mtime)
                    entry.update(
                        {
                            "exists": True,
                            "bytes": st.st_size,
                            "mtime_utc": readers.to_iso(st.st_mtime),
                            "age_s": age,
                            "fresh": age < LIVE_FRESH_S,
                        }
                    )
                resp["data_sources"][name] = entry
            shards: List[Dict[str, Any]] = []
            for path in readers.depth_shard_paths(root):
                entry = {"name": path.name, "exists": False, "bytes": None, "mtime_utc": None, "age_s": None, "fresh": False}
                try:
                    st = path.stat()
                    age = max(0.0, now - st.st_mtime)
                    entry.update(
                        {
                            "exists": True,
                            "bytes": st.st_size,
                            "mtime_utc": readers.to_iso(st.st_mtime),
                            "age_s": age,
                            "fresh": age < LIVE_FRESH_S,
                        }
                    )
                except OSError:
                    pass
                shards.append(entry)
            resp["data_sources"]["depth_shards"] = shards
            archives = readers.list_session_archives(root)
            resp["session_archives"] = {
                "count": len(archives),
                "latest": archives[-1].name if archives else None,
            }
        except Exception as exc:  # noqa: BLE001
            resp["error"] = f"{type(exc).__name__}: {exc}"
        return resp

    # --------------------------------------------------------------- command

    @app.get("/api/command")
    def api_command() -> Dict[str, Any]:
        now = readers.utc_now()
        resp: Dict[str, Any] = {
            "fetched_at_utc": readers.to_iso(now),
            "mode": "PAPER_TRADE",
            "paper_trade": True,
            "dry_run": False,
            "bot_log_age_s": None,
            "bot_log_fresh": False,
            "last_log_ts_utc": None,
            "kill_switch": {"state": "unknown", "last_alert_ts_utc": None, "last_message": None, "session_id": None},
            "session": {"start_ts_utc": None, "loops": None, "opps": None, "fills": None, "elapsed_s": None},
            "utc_now": readers.to_iso(now),
        }
        try:
            bot_path = root / "state" / "bot.jsonl"
            env_paper = os.environ.get("PAPER_TRADE")
            if env_paper is not None and env_paper.strip() != "":
                paper = env_paper.strip().lower() in ("1", "true", "yes", "on")
                resp["mode"] = "PAPER_TRADE" if paper else "LIVE"
                resp["paper_trade"] = paper
            else:
                flags = readers.detect_paper_mode(bot_path)
                resp["mode"] = "PAPER_TRADE" if flags["paper"] else "LIVE"
                resp["paper_trade"] = flags["paper"]
                resp["dry_run"] = flags["dry_run"]

            last_ts, mtime, age = _last_valid_ts_and_mtime(bot_path, now)
            resp["bot_log_age_s"] = age
            resp["bot_log_fresh"] = age is not None and age < LIVE_FRESH_S
            resp["last_log_ts_utc"] = readers.to_iso(last_ts)

            start = readers.find_session_start(bot_path)
            session = resp["session"]
            if start and start["ts_epoch"] is not None:
                session["start_ts_utc"] = start["ts_utc"]
                end_ts = None
                metrics = readers.extract_session_metrics_rows(bot_path)
                if metrics:
                    latest_metrics = metrics[-1]
                    session["loops"] = latest_metrics.get("loops")
                    session["opps"] = latest_metrics.get("opps")
                    session["fills"] = latest_metrics.get("fills")
                    end_ts = latest_metrics.get("ts_epoch")
                if end_ts is None:
                    end_ts = last_ts
                if end_ts is not None:
                    session["elapsed_s"] = max(0.0, end_ts - start["ts_epoch"])

            for alert in readers.read_alerts_rows(root):
                if alert.get("kind") == "kill_switch":
                    resp["kill_switch"] = {
                        "state": "halted",
                        "last_alert_ts_utc": readers.to_iso(alert.get("ts")),
                        "last_message": alert.get("message"),
                        "session_id": alert.get("session_id"),
                    }
        except Exception as exc:  # noqa: BLE001
            resp["error"] = f"{type(exc).__name__}: {exc}"
        return resp

    # ---------------------------------------------------------------- ticker

    def _live_or_archive_trades() -> tuple[List[Dict[str, Any]], Optional[str]]:
        """Trade rows from the live bot.jsonl, or the newest archive that has any.

        The current session is frequently thin (zero opportunities).  When the
        live file holds no trade events, the newest sealed archive that does
        is used instead and every item is tagged with its source so the
        dashboard can show exactly where the data came from.
        """
        live_rows = readers.extract_trade_rows(root / "state" / "bot.jsonl")
        if live_rows:
            return live_rows, "live"
        for archive in readers.list_session_archives(root)[:3]:
            rows = readers.extract_trade_rows(archive / "bot.jsonl")
            if rows:
                return rows, "archive:" + archive.name
        return [], None

    @app.get("/api/ticker")
    def api_ticker(limit: int = 60) -> Dict[str, Any]:
        resp: Dict[str, Any] = {"fetched_at_utc": _now_iso(), "items": [], "source": None}
        try:
            trades, source = _live_or_archive_trades()
            resp["source"] = source
            items: List[Dict[str, Any]] = []
            for trade in trades:
                data = trade["data"]
                combined = _trade_combined(data)
                edge = (1.0 - combined) if combined is not None else None
                outcome = TICKER_OUTCOME.get(trade["msg"])
                reason = data.get("reason")
                items.append(
                    {
                        "ts_utc": trade["ts_utc"],
                        "event": trade["msg"],
                        "market_id": data.get("market_id"),
                        "combined": combined,
                        "edge": edge,
                        "outcome": outcome,
                        "reason": reason if isinstance(reason, str) else None,
                        "pnl": _trade_pnl(data) if trade["msg"] != "missed" else None,
                        "size": _trade_size(data),
                        "age_ms": _trade_age(data),
                        "source": source,
                    }
                )
            items.sort(key=lambda it: it["ts_utc"] or "")
            resp["items"] = items[-max(0, limit):]
        except Exception as exc:  # noqa: BLE001
            resp["error"] = f"{type(exc).__name__}: {exc}"
        return resp

    # --------------------------------------------------------------- heatmap

    @app.get("/api/heatmap")
    def api_heatmap(limit: int = 48) -> Dict[str, Any]:
        resp: Dict[str, Any] = {"fetched_at_utc": _now_iso(), "items": [], "source": None}
        try:
            trades, source = _live_or_archive_trades()
            resp["source"] = source
            items: List[Dict[str, Any]] = []
            for trade in trades:
                data = trade["data"]
                msg = trade["msg"]
                combined = _trade_combined(data)
                edge = (1.0 - combined) if combined is not None else None
                reason = data.get("reason")
                if msg == "no_fill" and not isinstance(reason, str):
                    reason = "no_fill"
                items.append(
                    {
                        "ts_utc": trade["ts_utc"],
                        "kind": HEATMAP_KIND.get(msg, "reject"),
                        "market_id": data.get("market_id"),
                        "edge": edge,
                        "pnl": _trade_pnl(data) if msg in ("filled", "partial_resolved") else None,
                        "size": _trade_size(data),
                        "reason": reason if isinstance(reason, str) else None,
                        "source": source,
                    }
                )
            if source == "live":
                for row in readers.read_order_ledger_rows(root):
                    status = row.get("status")
                    if isinstance(status, str) and "reject" in status.lower():
                        ts = row.get("timestamp")
                        size = row.get("reserved")
                        items.append(
                            {
                                "ts_utc": readers.to_iso(ts if isinstance(ts, (int, float)) else None),
                                "kind": "reject",
                                "market_id": row.get("market_id"),
                                "edge": None,
                                "pnl": None,
                                "size": float(size) if isinstance(size, (int, float)) else None,
                                "reason": status,
                                "source": "live",
                            }
                        )
            items.sort(key=lambda it: it["ts_utc"] or "")
            resp["items"] = items[-max(0, limit):]
        except Exception as exc:  # noqa: BLE001
            resp["error"] = f"{type(exc).__name__}: {exc}"
        return resp

    # ------------------------------------------------------------------- pnl

    @app.get("/api/pnl")
    def api_pnl() -> Dict[str, Any]:
        resp: Dict[str, Any] = {
            "fetched_at_utc": _now_iso(),
            "latest": {"locked": None, "residual": None, "session": None, "would_lock": None, "ts_utc": None},
            "series": [],
            "notes": "series merges current bot.jsonl plus the most recent 3 session archives (chronological, oldest first)",
        }
        try:
            series: List[Dict[str, Any]] = []
            seen: set = set()

            def _append(metrics: Dict[str, Any], source: str) -> None:
                ts = metrics.get("ts_epoch")
                if ts is None or ts in seen:
                    return
                seen.add(ts)
                series.append(
                    {
                        "ts_utc": metrics.get("ts_utc"),
                        "locked": metrics.get("locked_pnl"),
                        "residual": metrics.get("residual_pnl"),
                        "session": metrics.get("session_pnl"),
                        "source": source,
                        "_ts": ts,
                        "_would_lock": metrics.get("would_lock"),
                    }
                )

            for metrics in readers.extract_session_metrics_rows(root / "state" / "bot.jsonl"):
                _append(metrics, "live")
            for archive in readers.list_session_archives(root)[-3:]:
                for metrics in readers.extract_session_metrics_rows(archive / "bot.jsonl"):
                    _append(metrics, f"archive:{archive.name}")
            series.sort(key=lambda it: it["_ts"])
            if series:
                last = series[-1]
                resp["latest"] = {
                    "locked": last["locked"],
                    "residual": last["residual"],
                    "session": last["session"],
                    "would_lock": last["_would_lock"],
                    "ts_utc": last["ts_utc"],
                }
            for it in series:
                it.pop("_ts", None)
                it.pop("_would_lock", None)
            resp["series"] = series
        except Exception as exc:  # noqa: BLE001
            resp["error"] = f"{type(exc).__name__}: {exc}"
        return resp

    # ----------------------------------------------------------------- alerts

    @app.get("/api/alerts")
    def api_alerts(limit: int = 50) -> Dict[str, Any]:
        resp: Dict[str, Any] = {"fetched_at_utc": _now_iso(), "items": []}
        try:
            items: List[Dict[str, Any]] = []
            for row in readers.read_alerts_rows(root):
                ts = row.get("ts")
                if not isinstance(ts, (int, float)):
                    continue
                items.append(
                    {
                        "ts_utc": readers.to_iso(float(ts)),
                        "kind": row.get("kind"),
                        "message": row.get("message"),
                        "session_id": row.get("session_id"),
                        "_ts": float(ts),
                    }
                )
            items.sort(key=lambda it: it["_ts"], reverse=True)
            for it in items:
                it.pop("_ts", None)
            resp["items"] = items[: max(0, limit)]
        except Exception as exc:  # noqa: BLE001
            resp["error"] = f"{type(exc).__name__}: {exc}"
        return resp

    # --------------------------------------------------------------- latency

    @app.get("/api/latency")
    def api_latency() -> Dict[str, Any]:
        resp: Dict[str, Any] = {
            "fetched_at_utc": _now_iso(),
            "latest": {"opp2order_avg_ms": None, "p50_ms": None, "p95_ms": None, "p99_ms": None, "ack_avg_ms": None, "n": None, "ts_utc": None},
            "series": [],
            "profile": {},
        }
        try:
            metrics = readers.extract_session_metrics_rows(root / "state" / "bot.jsonl")
            series: List[Dict[str, Any]] = []
            for m in metrics:
                series.append(
                    {
                        "ts_utc": m.get("ts_utc"),
                        "p50_ms": m.get("p50_ms"),
                        "p95_ms": m.get("p95_ms"),
                        "p99_ms": m.get("p99_ms"),
                    }
                )
            if metrics:
                last = metrics[-1]
                resp["latest"] = {
                    "opp2order_avg_ms": last.get("opp2order_avg_ms"),
                    "p50_ms": last.get("p50_ms"),
                    "p95_ms": last.get("p95_ms"),
                    "p99_ms": last.get("p99_ms"),
                    "ack_avg_ms": last.get("ack_avg_ms"),
                    "n": last.get("loops"),
                    "ts_utc": last.get("ts_utc"),
                }
            resp["series"] = series
            resp["profile"] = readers.read_latency_profile(root)
        except Exception as exc:  # noqa: BLE001
            resp["error"] = f"{type(exc).__name__}: {exc}"
        return resp

    # ----------------------------------------------------------------- depth

    @app.get("/api/depth")
    def api_depth() -> Dict[str, Any]:
        now = readers.utc_now()
        resp: Dict[str, Any] = {
            "fetched_at_utc": readers.to_iso(now),
            "process": {"pid": None, "alive": False, "lock_held": False},
            "shards": [],
            "snapshot_count": 0,
            "snapshot_count_scope": "none",
            "last_capture_age_s": None,
            "continuity": {"windows": 0, "complete": 0, "broken": 0, "ratio": 0.0, "criterion": "both tokens: first<=5s, last>=295s, max gap<=60s"},
            "last_30_windows": {"complete": 0, "windows": 0},
            "history": None,
            "scanning": False,
        }
        try:
            pid = readers.find_capture_pid(root)
            alive = readers.pid_alive(pid)
            resp["process"] = {
                "pid": pid,
                "alive": alive,
                "lock_held": alive and readers.read_depth_lock(root) is not None,
            }
            metadata = readers.depth_shard_metadata(root, now=now)
            resp["shards"] = [
                {
                    "name": item["name"],
                    "bytes": item["bytes"],
                    "rows": None,
                    "first_ts_utc": None,
                    "last_ts_utc": None,
                    "age_s": item["age_s"],
                }
                for item in metadata
            ]
            fast = readers.scan_newest_shard(root, now=now)
            resp["continuity"] = fast["continuity"]
            resp["last_30_windows"] = fast["last_30_windows"]
            resp["fast_shard"] = fast.get("shard")
            if fast.get("last_capture_ts") is not None:
                resp["last_capture_age_s"] = max(0.0, now - fast["last_capture_ts"])
            sidecar = readers.load_depth_sidecar(root, now=now)
            started = readers.start_full_scan_if_needed(root, now=now)
            resp["scanning"] = bool(started) or readers.full_scan_running()
            if sidecar is not None:
                resp["snapshot_count"] = sidecar.get("snapshot_count", 0)
                resp["snapshot_count_scope"] = "all shards (sidecar)"
                resp["history"] = {
                    "continuity": sidecar.get("continuity"),
                    "last_30_windows": sidecar.get("last_30_windows"),
                    "written_at_utc": readers.to_iso(sidecar.get("written_at")),
                }
                resp["shards"] = [
                    {
                        "name": item["name"],
                        "bytes": item["bytes"],
                        "rows": item.get("rows"),
                        "first_ts_utc": readers.to_iso(item.get("first_ts")),
                        "last_ts_utc": readers.to_iso(item.get("last_ts")),
                        "age_s": item.get("age_s"),
                    }
                    for item in sidecar.get("shards", resp["shards"])
                ]
            else:
                resp["snapshot_count"] = fast.get("snapshot_count", 0)
                resp["snapshot_count_scope"] = "newest shard only (sidecar pending)"
        except Exception as exc:  # noqa: BLE001
            resp["error"] = f"{type(exc).__name__}: {exc}"
        return resp

    # ------------------------------------------------------- external providers

    def _provider_endpoint(fn_name: str) -> Dict[str, Any]:
        resp: Dict[str, Any] = {"fetched_at_utc": _now_iso()}
        try:
            from dashboard_js import providers  # lazy import

            result = getattr(providers, fn_name)()
            if isinstance(result, dict):
                resp.update(result)
            else:
                resp["items"] = result
        except Exception as exc:  # noqa: BLE001
            resp["error"] = f"{type(exc).__name__}: {exc}"
        return resp

    @app.get("/api/lse_cross_asset")
    def api_lse_cross_asset() -> Dict[str, Any]:
        return _provider_endpoint("lse_cross_asset")

    @app.get("/api/indices")
    def api_indices() -> Dict[str, Any]:
        return _provider_endpoint("massive_index_proxies")

    @app.get("/api/sector_heatmap")
    def api_sector_heatmap() -> Dict[str, Any]:
        return _provider_endpoint("lse_heatmap")

    @app.get("/api/stock_chart")
    def api_stock_chart(symbol: str = "") -> Dict[str, Any]:
        resp: Dict[str, Any] = {"fetched_at_utc": _now_iso(), "symbol": symbol}
        try:
            from dashboard_js import providers  # lazy import

            resp.update(providers.stock_chart(symbol))
        except Exception as exc:  # noqa: BLE001
            resp["error"] = f"{type(exc).__name__}: {exc}"
        return resp

    @app.get("/api/ohlcv")
    def api_ohlcv(symbol: str = "", resolution: str = "1d") -> Dict[str, Any]:
        resp: Dict[str, Any] = {"fetched_at_utc": _now_iso(), "symbol": symbol, "resolution": resolution}
        try:
            from dashboard_js import providers  # lazy import

            resp.update(providers.yf_ohlcv(symbol, resolution=resolution))
        except Exception as exc:  # noqa: BLE001
            resp["error"] = f"{type(exc).__name__}: {exc}"
        return resp

    @app.get("/api/company_news")
    def api_company_news(symbol: str = "", days: int = 3) -> Dict[str, Any]:
        resp: Dict[str, Any] = {"fetched_at_utc": _now_iso(), "symbol": symbol}
        try:
            from dashboard_js import providers  # lazy import

            resp.update(providers.finnhub_company_news(symbol, days=max(1, min(days, 7))))
        except Exception as exc:  # noqa: BLE001
            resp["error"] = f"{type(exc).__name__}: {exc}"
        return resp

    @app.get("/api/btc_sentiment")
    def api_btc_sentiment() -> Dict[str, Any]:
        resp: Dict[str, Any] = {"fetched_at_utc": _now_iso()}
        try:
            from dashboard_js import providers  # lazy import

            resp.update(providers.btc_sentiment_proxy())
        except Exception as exc:  # noqa: BLE001
            resp["error"] = f"{type(exc).__name__}: {exc}"
        return resp

    @app.get("/api/news_wire")
    def api_news_wire() -> Dict[str, Any]:
        return _provider_endpoint("news_wire")

    @app.get("/api/social")
    def api_social() -> Dict[str, Any]:
        return _provider_endpoint("social_feed")

    @app.get("/api/finnhub_search")
    def api_finnhub_search(q: str = "") -> Dict[str, Any]:
        resp: Dict[str, Any] = {"fetched_at_utc": _now_iso(), "query": q}
        try:
            from dashboard_js import providers  # lazy import

            resp.update(providers.finnhub_search(q))
        except Exception as exc:  # noqa: BLE001
            resp["error"] = f"{type(exc).__name__}: {exc}"
        return resp

    @app.get("/api/reddit")
    def api_reddit() -> Dict[str, Any]:
        return _provider_endpoint("read_reddit_feed")

    # ------------------------------------------------------- bot control (paper only)

    def _botctl_endpoint(fn_name: str, **kwargs) -> Dict[str, Any]:
        resp: Dict[str, Any] = {"fetched_at_utc": _now_iso()}
        try:
            from dashboard_js import botctl  # lazy import

            result = getattr(botctl, fn_name)(**kwargs)
            if isinstance(result, dict):
                resp.update(result)
        except Exception as exc:  # noqa: BLE001
            resp["error"] = f"{type(exc).__name__}: {exc}"
        return resp

    @app.get("/api/botctl/status")
    def api_botctl_status() -> Dict[str, Any]:
        return _botctl_endpoint("status")

    @app.get("/api/botctl/risk")
    def api_botctl_risk_get() -> Dict[str, Any]:
        try:
            from dashboard_js import botctl
            return {"fetched_at_utc": _now_iso(), "risk": botctl.load_risk()}
        except Exception as exc:  # noqa: BLE001
            return {"fetched_at_utc": _now_iso(), "error": f"{type(exc).__name__}: {exc}"}

    @app.post("/api/botctl/risk")
    def api_botctl_risk_set(payload: Dict[str, Any]) -> Dict[str, Any]:
        resp: Dict[str, Any] = {"fetched_at_utc": _now_iso()}
        try:
            from dashboard_js import botctl
            saved, error = botctl.save_risk(payload or {})
            if error:
                resp["error"] = error
            else:
                resp["risk"] = saved
        except Exception as exc:  # noqa: BLE001
            resp["error"] = f"{type(exc).__name__}: {exc}"
        return resp

    @app.post("/api/botctl/start")
    def api_botctl_start(payload: Dict[str, Any]) -> Dict[str, Any]:
        resp: Dict[str, Any] = {"fetched_at_utc": _now_iso()}
        try:
            from dashboard_js import botctl
            result = botctl.start_bot(
                duration_minutes=float((payload or {}).get("duration_minutes", 15)),
                fill_source=str((payload or {}).get("fill_source", "rest_book")),
                risk=(payload or {}).get("risk"),
            )
            resp.update(result)
        except Exception as exc:  # noqa: BLE001
            resp["error"] = f"{type(exc).__name__}: {exc}"
        return resp

    @app.post("/api/botctl/stop")
    def api_botctl_stop() -> Dict[str, Any]:
        return _botctl_endpoint("stop_bot")

    @app.get("/api/botctl/log")
    def api_botctl_log(lines: int = 60) -> Dict[str, Any]:
        return _botctl_endpoint("log_tail", lines=max(1, min(lines, 300)))

    @app.get("/api/botmind")
    def api_botmind() -> Dict[str, Any]:
        resp: Dict[str, Any] = {"fetched_at_utc": _now_iso()}
        try:
            from dashboard_js import botmind  # lazy import

            resp.update(botmind.mind())
        except Exception as exc:  # noqa: BLE001
            resp["error"] = f"{type(exc).__name__}: {exc}"
        return resp

    @app.get("/api/windows")
    def api_windows() -> Dict[str, Any]:
        resp: Dict[str, Any] = {"fetched_at_utc": _now_iso()}
        try:
            from dashboard_js import windowgrid  # lazy import

            resp.update(windowgrid.snapshot())
        except Exception as exc:  # noqa: BLE001
            resp["error"] = f"{type(exc).__name__}: {exc}"
        return resp

    # ------------------------------------------------ isolated paper loop bot

    def _loopbot_endpoint(fn_name: str, **kwargs) -> Dict[str, Any]:
        resp: Dict[str, Any] = {"fetched_at_utc": _now_iso()}
        try:
            from dashboard_js import loopbot  # lazy import

            result = getattr(loopbot, fn_name)(**kwargs)
            if isinstance(result, dict):
                resp.update(result)
        except Exception as exc:  # noqa: BLE001
            resp["error"] = f"{type(exc).__name__}: {exc}"
        return resp

    @app.get("/api/loopbot/status")
    def api_loopbot_status() -> Dict[str, Any]:
        return _loopbot_endpoint("status")

    @app.get("/api/loopbot/events")
    def api_loopbot_events(limit: int = 80) -> Dict[str, Any]:
        return _loopbot_endpoint("events", limit=max(1, min(limit, 500)))

    @app.get("/api/loopbot/chart")
    def api_loopbot_chart() -> Dict[str, Any]:
        return _loopbot_endpoint("chart")

    @app.get("/api/loopbot/log")
    def api_loopbot_log(lines: int = 40) -> Dict[str, Any]:
        return _loopbot_endpoint("log_tail", lines=max(1, min(lines, 200)))

    @app.get("/api/loopbot/config")
    def api_loopbot_config() -> Dict[str, Any]:
        resp: Dict[str, Any] = {"fetched_at_utc": _now_iso()}
        try:
            from dashboard_js import loopbot
            resp["config"] = loopbot.load_config()
        except Exception as exc:  # noqa: BLE001
            resp["error"] = f"{type(exc).__name__}: {exc}"
        return resp

    @app.post("/api/loopbot/config")
    def api_loopbot_config_set(payload: Dict[str, Any]) -> Dict[str, Any]:
        resp: Dict[str, Any] = {"fetched_at_utc": _now_iso()}
        try:
            from dashboard_js import loopbot
            saved, error = loopbot.save_config(payload or {})
            if error:
                resp["error"] = error
            else:
                resp["config"] = saved
        except Exception as exc:  # noqa: BLE001
            resp["error"] = f"{type(exc).__name__}: {exc}"
        return resp

    @app.post("/api/loopbot/start")
    def api_loopbot_start(payload: Dict[str, Any]) -> Dict[str, Any]:
        resp: Dict[str, Any] = {"fetched_at_utc": _now_iso()}
        try:
            from dashboard_js import loopbot
            result = loopbot.start_bot((payload or {}).get("config"))
            resp.update(result)
        except Exception as exc:  # noqa: BLE001
            resp["error"] = f"{type(exc).__name__}: {exc}"
        return resp

    @app.post("/api/loopbot/stop")
    def api_loopbot_stop() -> Dict[str, Any]:
        return _loopbot_endpoint("stop_bot")

    # ------------------------------------------------------- models/checklist

    def _parsers_endpoint(attr: str, fallback_key: str) -> Dict[str, Any]:
        resp: Dict[str, Any] = {"fetched_at_utc": _now_iso()}
        try:
            from dashboard_js import parsers  # noqa: F401  (lazy: may not exist yet)

            fn = getattr(parsers, attr)
            if attr == "parse_checklist":
                checklist_doc = PROJECT_ROOT / "docs" / "product_status_2026-08-15.md"
                if not checklist_doc.is_file():
                    checklist_doc = PROJECT_ROOT / "docs" / "go_live_checklist_2026-08-11.md"
                result = fn(str(checklist_doc))
            else:
                result = fn()
            if isinstance(result, dict):
                resp.update(result)
            else:
                resp[fallback_key] = result
            resp.setdefault("fetched_at_utc", _now_iso())
        except ImportError:
            resp.update({"error": "parsers module unavailable", fallback_key: []})
        except Exception as exc:  # noqa: BLE001
            resp.update({"error": f"{type(exc).__name__}: {exc}", fallback_key: []})
        return resp

    @app.get("/api/models")
    def api_models() -> Dict[str, Any]:
        return _parsers_endpoint("parse_model_status", "models")

    @app.get("/api/checklist")
    def api_checklist() -> Dict[str, Any]:
        return _parsers_endpoint("parse_checklist", "checklist")

    # --------------------------------------------------------------- static

    static_exists = static_dir.is_dir()
    canvas_dir = Path(__file__).resolve().parent.parent / "dashboard_canvas" / "static"
    if canvas_dir.is_dir():
        app.mount("/canvas", StaticFiles(directory=str(canvas_dir), html=True), name="canvas")
    if static_exists:
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static-prefix")
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")
    else:
        @app.get("/")
        @app.get("/static/{path:path}")
        def static_missing() -> HTMLResponse:
            return HTMLResponse(STATIC_MISSING_HTML, status_code=404)

    return app


app = create_app()


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8088)


if __name__ == "__main__":
    main()
