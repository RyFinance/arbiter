from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo
import altair as alt
import pandas as pd
import streamlit as st

# Streamlit's AppTest executes this file as ``__main__`` and leaves that module
# installed for later tests.  The sealed legacy v3 joblib artifact records its
# wrapper as ``__main__.TwoStagePredictionModel``; expose the compatibility
# class here so an AppTest run cannot make the artifact unpicklable.
from polymarket_arb.two_stage_model_compat import (  # noqa: F401, E402
    TwoStagePredictionModel,
)

from polymarket_arb.demo_replay import (
    discover_demo_sessions,
    load_demo_events,
    replay_snapshot,
)
from polymarket_arb.dashboard_status import (
    btc_model_status,
    depth_capture_status,
    discover_pytest_status,
    kill_switch_status,
    latest_sealed_task_session,
    macro_state_status,
    paper_session_status,
    task_bot_liveness_status,
)
from polymarket_arb.live_control import LiveTradingControl
from polymarket_arb.product_status import build_product_status
from polymarket_arb.wallet_status import refresh_wallet_status

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
EVIDENCE_DATE = os.getenv(
    "PRODUCT_EVIDENCE_DATE",
    datetime.now(ZoneInfo("America/Vancouver")).date().isoformat(),
)
BOT_LOG = STATE_DIR / "bot.jsonl"
ALERTS_LOG = STATE_DIR / "alerts.jsonl"
BACKTEST_VAL = STATE_DIR / "backtest_validation.json"
BACKTEST_REPORT = STATE_DIR / "backtest_validation_report.json"
MACRO_EVENTS = STATE_DIR / "macro_events.json"
MACRO_EVENTS_PATH = MACRO_EVENTS
LATENCY_PROFILE = STATE_DIR / "latency_profile.json"
FRESHNESS_PNL = STATE_DIR / "freshness_pnl_report.json"
PAPER_MONITOR = STATE_DIR / "paper_session_monitor.json"
LIGHTGBM_METRICS = STATE_DIR / "lightgbm_training_metrics_v3.json"
KRONOS_METRICS = STATE_DIR / "kronos_training_metrics.json"
MARKET_CACHE = STATE_DIR / "market_cache.json"
CROSS_VENUE_LOG = STATE_DIR / "cross_venue_log.jsonl"
CROSS_VENUE_LONGRUN = STATE_DIR / "cross_venue_longrun.jsonl"
LSE_FEED = STATE_DIR / "lse_feed.jsonl"
CLOB_FEED = STATE_DIR / "clob_feed.jsonl"
DEPTH_LADDERS = STATE_DIR / "depth_ladders"
DEPTH_REPLAY_VAL = next(
    (
        path
        for path in (
            STATE_DIR / f"depth_replay_validation_combined_{EVIDENCE_DATE}.json",
            STATE_DIR / f"depth_replay_validation_postfix_{EVIDENCE_DATE}.json",
            STATE_DIR / f"depth_replay_validation_supervised_{EVIDENCE_DATE}.json",
            STATE_DIR / f"chunk_b_depth_replay_validation_{EVIDENCE_DATE}.json",
        )
        if path.is_file()
    ),
    STATE_DIR / f"depth_replay_validation_postfix_{EVIDENCE_DATE}.json",
)
DEPTH_CONSOLIDATED = STATE_DIR / "cache" / "polymarket_depth_consolidated.jsonl"
GROUND_TRUTH = STATE_DIR / f"ground_truth_{EVIDENCE_DATE}.json"
TASK_SESSION_ARCHIVES = STATE_DIR / "paper_session_archives"
TASK_HEARTBEAT = Path("/tmp/polymarket-arb-task-2-4-HEARTBEAT")
BOT_HEARTBEAT = STATE_DIR / "HEARTBEAT"
BTC_SIZING_EVIDENCE = (
    ROOT
    / "docs"
    / "research"
    / f"btc_offline_confidence_weighted_sizing_{EVIDENCE_DATE}.json"
)
EMERGENCY_STOP = ROOT / "emergency.stop"
LIVE_CONTROL_STATE = STATE_DIR / "live_control.json"
LIVE_CONTROL_AUDIT = STATE_DIR / "live_control_audit.jsonl"
WALLET_STATUS = STATE_DIR / "wallet_status.json"
PRODUCT_STATUS = STATE_DIR / f"product_status_{EVIDENCE_DATE}.json"

st.set_page_config(
    page_title="ARB TERMINAL",
    page_icon="◆",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.html("""
<style>
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;500;700&display=swap');
:root {
    --bg: #000000; --panel-bg: #060608; --border: #1a1a1e; --border-bright: #333338;
    --amber: #FFB900; --amber-dim: #996600; --green: #00FF66; --green-dim: #009933;
    --cyan: #00FFFF; --cyan-dim: #009999; --red: #FF3333; --red-dim: #991111;
    --magenta: #FF00FF; --white: #E0E0E0; --dim: #555555; --faint: #333333;
}
html, body, [class*="css"] { font-family: 'JetBrains Mono', monospace !important; font-size: 12px !important; color: var(--white) !important; }
.stApp { background-color: var(--bg) !important; }
.block-container { padding-top: 0.5rem !important; padding-bottom: 0.5rem !important; padding-left: 1rem !important; padding-right: 1rem !important; max-width: 100% !important; }
[data-testid="stVerticalBlockBorderWrapper"] > div { border-color: var(--border) !important; border-radius: 0px !important; background-color: var(--panel-bg) !important; }
[data-testid="stMetric"] { background-color: transparent !important; border: 1px solid var(--border) !important; padding: 6px 10px !important; border-radius: 0px !important; }
[data-testid="stMetricLabel"] { color: var(--dim) !important; font-size: 0.65rem !important; text-transform: uppercase !important; letter-spacing: 1px !important; }
[data-testid="stMetricValue"] { color: var(--amber) !important; font-size: 1.3rem !important; font-weight: 700 !important; }
[data-testid="stMetricDelta"] svg { display: none !important; }
h1, h2, h3 { color: var(--amber) !important; text-transform: uppercase; letter-spacing: 2px; font-weight: 700 !important; border-bottom: 1px solid var(--border) !important; padding-bottom: 4px !important; margin-bottom: 8px !important; font-size: 0.85rem !important; }
@keyframes blink { 0%,100% { opacity: 1; } 50% { opacity: 0.3; } }
.status-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; animation: blink 1.5s ease-in-out infinite; vertical-align: middle; }
.dot-green { background: var(--green); box-shadow: 0 0 6px var(--green); }
.dot-amber { background: var(--amber); box-shadow: 0 0 6px var(--amber); }
.dot-red { background: var(--red); box-shadow: 0 0 6px var(--red); }
.dot-off { background: var(--dim); }
@keyframes countUp { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: translateY(0); } }
.anim-num { animation: countUp 0.6s ease-out forwards; display: inline-block; }
.alert-feed { max-height: 220px; overflow-y: auto; font-size: 0.7rem; line-height: 1.4; }
.alert-row { padding: 2px 0; border-bottom: 1px solid var(--border); display: flex; gap: 8px; }
.alert-ts { color: var(--dim); min-width: 70px; }
.alert-sev { min-width: 50px; font-weight: 700; }
.sev-CRIT { color: var(--red); } .sev-WARN { color: var(--amber); } .sev-INFO { color: var(--cyan); } .sev-TEST { color: var(--magenta); }
::-webkit-scrollbar { width: 6px; height: 6px; }
::-webkit-scrollbar-track { background: var(--bg); }
::-webkit-scrollbar-thumb { background: var(--faint); border-radius: 0; }
::-webkit-scrollbar-thumb:hover { background: var(--amber); }
.ci-badge { display: inline-block; padding: 2px 8px; font-size: 0.65rem; font-weight: 700; letter-spacing: 1px; border: 1px solid; }
.ci-pass { color: var(--green); border-color: var(--green-dim); }
.ci-fail { color: var(--red); border-color: var(--red-dim); }
#MainMenu { visibility: hidden; } footer { visibility: hidden; } header { visibility: hidden; }
[data-testid="stSidebar"] { background-color: var(--panel-bg) !important; border-right: 1px solid var(--border-bright) !important; }
.streamlit-expanderHeader { font-size: 0.7rem !important; color: var(--cyan) !important; text-transform: uppercase !important; letter-spacing: 1px !important; }
[data-testid="stDataFrame"] { border: 1px solid var(--border) !important; font-size: 0.7rem !important; }
.terminal-tile { border: 1px solid var(--border); padding: 8px 10px; background: var(--panel-bg); height: 100%; }
.terminal-tile .label { font-size: 0.6rem; color: var(--dim); text-transform: uppercase; letter-spacing: 1px; margin-bottom: 2px; }
.terminal-tile .value { font-size: 1.4rem; font-weight: 700; line-height: 1.2; }
.terminal-tile .sub { font-size: 0.6rem; color: var(--dim); margin-top: 2px; }
.stale-banner { background: #1a0000; border: 1px solid #550000; color: #FF6666; font-size: 0.6rem; padding: 2px 6px; text-align: center; margin-bottom: 4px; letter-spacing: 1px; }
.go-live-row { display: flex; gap: 4px; align-items: stretch; margin-bottom: 6px; }
.go-live-item { flex: 1; border: 1px solid var(--border); padding: 4px 8px; text-align: center; font-size: 0.65rem; }
.go-live-item .gl-status { font-size: 0.9rem; font-weight: 700; }
.go-live-item .gl-label { color: var(--dim); text-transform: uppercase; letter-spacing: 0.5px; font-size: 0.55rem; }
.verdict-item { text-align: center; flex: 1; }
.verdict-item .v-model { font-size: 0.6rem; color: var(--dim); text-transform: uppercase; letter-spacing: 0.5px; }
.verdict-item .v-status { font-size: 0.85rem; font-weight: 700; }
.verdict-item .v-detail { font-size: 0.55rem; color: var(--dim); }
.tw-label { font-size: 0.55rem; color: var(--dim); letter-spacing: 0.5px; }
</style>
""")


@st.cache_data(ttl=5, max_entries=1)
def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


@st.cache_data(ttl=10, max_entries=1)
def load_product_status() -> dict[str, Any]:
    """Rebuild the authoritative gates from current evidence without mutating it."""
    try:
        return build_product_status(ROOT, evidence_date=EVIDENCE_DATE)
    except Exception:
        return load_json(PRODUCT_STATUS)


@st.cache_data(ttl=5, max_entries=1)
def load_macro_events() -> dict[str, Any]:
    """Load macro-calendar state with a stable fail-open shape for the dashboard."""
    data = load_json(MACRO_EVENTS_PATH)
    return {
        "events": data.get("events", [])
        if isinstance(data.get("events", []), list)
        else [],
        "last_update": data.get("last_update", 0.0),
        "high_risk_windows": (
            data.get("high_risk_windows", [])
            if isinstance(data.get("high_risk_windows", []), list)
            else []
        ),
    }


def get_high_macro_risk() -> bool:
    """Read the shared risk flag, failing open when unavailable."""
    try:
        from polymarket_arb import risk

        return bool(risk._high_macro_risk())
    except Exception:
        return False


def get_vol_regime() -> str:
    try:
        from polymarket_arb import kronos_session

        value = str(getattr(kronos_session, "SESSION_VOL_REGIME", "MED")).upper()
        return value if value in {"HIGH", "MED", "LOW"} else "MED"
    except Exception:
        return "MED"


def _parse_session_time(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        parts = value.split(":")
        if len(parts) != 3:
            return 0.0
        seconds = parts[2].split(".")
        whole = float(seconds[0])
        millis = int(seconds[1]) / 1000.0 if len(seconds) == 2 else 0.0
        return float(parts[0]) * 3600.0 + float(parts[1]) * 60.0 + whole + millis
    except (TypeError, ValueError):
        return 0.0


def parse_pnl_curve(events: list[dict[str, Any]]) -> pd.DataFrame:
    pattern = re.compile(
        r"session metrics:.*?locked_pnl=(-?[\d.]+).*?residual_pnl=(-?[\d.]+)"
    )
    rows = []
    for event in events:
        match = pattern.search(str(event.get("msg", "")))
        if match:
            rows.append(
                {
                    "t_sec": _parse_session_time(event.get("t")),
                    "locked_pnl": float(match.group(1)),
                    "residual_pnl": float(match.group(2)),
                }
            )
    return pd.DataFrame(rows, columns=["t_sec", "locked_pnl", "residual_pnl"])


@st.cache_data(ttl=5, max_entries=1)
def load_jsonl(path: Path, max_rows: int = 5000) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i >= max_rows:
                    break
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    except OSError:
        pass
    return rows


@st.cache_data(ttl=5, max_entries=1)
def file_age_s(path: Path) -> float:
    if not path.exists():
        return float("inf")
    try:
        return time.time() - path.stat().st_mtime
    except OSError:
        return float("inf")


def age_label(path: Path) -> str:
    age = file_age_s(path)
    if age == float("inf"):
        return "MISSING"
    if age < 60:
        return f"{age:.0f}s ago"
    if age < 3600:
        return f"{age / 60:.0f}m ago"
    if age < 86400:
        return f"{age / 3600:.1f}h ago"
    return f"{age / 86400:.1f}d ago"


def ts_to_str(ts: float) -> str:
    if not ts or ts <= 0:
        return "—"
    try:
        return datetime.fromtimestamp(ts, timezone.utc).strftime("%m-%d %H:%M")
    except (OSError, ValueError):
        return "—"


def ts_to_hms(ts: float) -> str:
    if not ts or ts <= 0:
        return "—"
    try:
        return datetime.fromtimestamp(ts, timezone.utc).strftime("%H:%M:%S")
    except (OSError, ValueError):
        return "—"


def countdown_str(ts: float, now: float) -> str:
    remaining = ts - now
    if remaining <= 0:
        return "active/expired"
    if remaining < 3600:
        return f"in {remaining / 60:.0f}m"
    if remaining < 86400:
        return f"in {remaining / 3600:.1f}h"
    return f"in {remaining / 86400:.1f}d"


demo_sessions = discover_demo_sessions(TASK_SESSION_ARCHIVES)
valid_demo_sessions = sorted(
    (session for session in demo_sessions if session.seal_valid),
    key=lambda session: (
        session.opportunity_count > 0,
        session.trade_event_count > 0,
        session.opportunity_count,
        session.trade_event_count,
        session.event_count,
        session.started_at,
    ),
    reverse=True,
)
demo_mode = st.sidebar.toggle(
    "Demo mode (sealed paper replay)",
    value=False,
    help="Replays an immutable real paper-session archive without a live connection.",
)
demo_session = None
demo_snapshot: dict[str, Any] = {}
if demo_mode and valid_demo_sessions:
    demo_by_id = {session.session_id: session for session in valid_demo_sessions}
    selected_demo_id = st.sidebar.selectbox(
        "Historical session",
        options=list(demo_by_id),
        index=0,
    )
    demo_progress_pct = st.sidebar.slider(
        "Replay position",
        min_value=0,
        max_value=100,
        value=100,
        step=1,
    )
    demo_session = demo_by_id[selected_demo_id]
    demo_snapshot = replay_snapshot(demo_session, demo_progress_pct / 100.0)
    all_demo_events = load_demo_events(demo_session)
    bot_events = [
        event
        for event in all_demo_events
        if float(event.get("_elapsed_s", 0.0)) <= demo_snapshot["cutoff_seconds"]
    ]
    alerts_events = []
    for event in bot_events:
        message = str(event.get("msg", ""))
        match = re.search(r"ALERT\[([^]]+)\]:\s*(.*)", message)
        if match:
            alerts_events.append(
                {
                    "kind": match.group(1),
                    "message": match.group(2),
                    "ts": 0,
                }
            )
else:
    bot_events = load_jsonl(BOT_LOG)
    alerts_events = load_jsonl(ALERTS_LOG)
    if demo_mode and not valid_demo_sessions:
        st.sidebar.error(
            "No hash-verified paper archives are available for demo replay."
        )
backtest_val = load_json(BACKTEST_VAL)
backtest_report = load_json(BACKTEST_REPORT)
backtest_comparable = backtest_report.get("comparable_to_outcome_books") is True
backtest_source = str(backtest_report.get("data_source") or "unknown")
latency_profile = load_json(LATENCY_PROFILE)
freshness_pnl = load_json(FRESHNESS_PNL)
kronos_metrics = load_json(KRONOS_METRICS)
market_cache = load_json(MARKET_CACHE)
cross_venue = load_jsonl(CROSS_VENUE_LOG, 100)
cross_venue_lr = load_jsonl(CROSS_VENUE_LONGRUN, 100)
lse_feed = load_jsonl(LSE_FEED, 100)
clob_feed = load_jsonl(CLOB_FEED, 100)

now = time.time()
utc_now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

latest_session_report = (
    load_json(demo_session.report_path)
    if demo_mode and demo_session is not None
    else latest_sealed_task_session(TASK_SESSION_ARCHIVES)
)
latest_session = paper_session_status(latest_session_report)
latest_session_path = (
    Path(latest_session["report_path"])
    if latest_session.get("report_path")
    else TASK_SESSION_ARCHIVES
)
bot_liveness = task_bot_liveness_status(
    (BOT_HEARTBEAT, TASK_HEARTBEAT), TASK_SESSION_ARCHIVES, timeout_s=300.0, now=now
)
bot_state = bot_liveness["state"]
if demo_mode and demo_session is not None:
    bot_state = "demo replay"
bot_dot = {
    "running": "dot-green",
    "completed/offline": "dot-off",
    "stale/disconnected": "dot-amber",
    "demo replay": "dot-amber",
}.get(bot_state, "dot-red")
bot_stale = bot_state not in {"running", "demo replay"}

kill_status = kill_switch_status(EMERGENCY_STOP, latest_session_report)
kill_switch_active = bool(kill_status["armed"])
kill_events = [e for e in alerts_events if e.get("kind") == "kill_switch"]
last_kill = kill_events[-1] if kill_events else None

macro_status = macro_state_status(MACRO_EVENTS, now=now, max_age_s=900.0)
macro_events_list = macro_status["events"]
high_risk_windows = macro_status["high_risk_windows"]
active_windows = macro_status["active_windows"]
upcoming_windows = [
    w for w in high_risk_windows if isinstance(w, dict) and w.get("start", 0) > now
]
upcoming_windows.sort(key=lambda w: w.get("start", 0))

bt_metrics = backtest_val.get("metrics", {})
bt_locked = bt_metrics.get("locked_pnl", 0.0)
bt_opps = bt_metrics.get("opportunities", 0)
bt_fills = bt_metrics.get("fills", 0)
bt_orders = bt_metrics.get("orders_sent", 0)

bt_report_bt = backtest_report.get("backtest", {})
uncapped_pnl = bt_report_bt.get("uncapped_requested_quantity_equivalent_pnl", 0.0)
fill_rate = bt_report_bt.get("requested_quantity_fill_rate", 0.0)

paper_locked = latest_session.get("locked_pnl", 0.0)
paper_session_pnl = latest_session.get("session_pnl", 0.0)
paper_would_lock = latest_session.get("would_lock", 0.0)
paper_fills = latest_session.get("fills", 0)
paper_opps = latest_session.get("opportunities", 0)
paper_missed = latest_session.get("missed", 0)
paper_avg_lat = latest_session.get("avg_latency_ms", 0.0)
paper_p50_lat = latest_session.get("p50_latency_ms", 0.0)

lat_stages = latency_profile.get("stages", {})
lat_opp = lat_stages.get("opp_to_order", {})

fresh_data = freshness_pnl.get("freshness", {})
fresh_pct = fresh_data.get("freshness_pct", 0.0)
fresh_bands = fresh_data.get("bands", {})

btc_status = btc_model_status(BTC_SIZING_EVIDENCE)

kronos_test = kronos_metrics.get("training_results", {}).get("final_test_metrics", {})
kronos_best_val = kronos_metrics.get("training_results", {}).get(
    "best_val_accuracy", 0.0
)

cv_count = len(cross_venue_lr) if cross_venue_lr else len(cross_venue)
cv_latest = (
    cross_venue[-1] if cross_venue else (cross_venue_lr[-1] if cross_venue_lr else None)
)
lse_count = len(lse_feed)
clob_count = len(clob_feed)
market_count = len(market_cache.get("markets", []))

depth_status = depth_capture_status(DEPTH_CONSOLIDATED, GROUND_TRUTH, DEPTH_REPLAY_VAL)
depth_replay_status = depth_status["strict_gate_status"]
depth_replay_reason = depth_status["strict_gate_reason"]
test_status = discover_pytest_status(STATE_DIR, GROUND_TRUTH)
test_source_path = (
    Path(test_status["source"]) if test_status.get("source") else STATE_DIR
)
live_control = LiveTradingControl(LIVE_CONTROL_STATE, LIVE_CONTROL_AUDIT)
live_control_state = live_control.read_state()
live_control_audit = live_control.read_audit(limit=200)
wallet_status = load_json(WALLET_STATUS)
product_status = load_product_status()

TEST_MARKERS = (
    "test message",
    "test crash",
    "test halt",
    "m-test",
    "synth-ks",
    "synth-rej",
)


def is_test_alert(entry: dict) -> bool:
    msg = entry.get("message", "")
    market = entry.get("market_id", "") or ""
    return any(m in msg or m in market for m in TEST_MARKERS)


alerts_prod = [e for e in alerts_events if not is_test_alert(e)]
alerts_test = [e for e in alerts_events if is_test_alert(e)]

METRIC_RE = re.compile(
    r"loops=(?P<loops>\d+) opps=(?P<opps>\d+) sent=(?P<sent>\d+) "
    r"fills=(?P<fills>\d+) partial=(?P<partial>\d+) resolved=(?P<resolved>\d+) "
    r"missed=(?P<missed>\d+) opp2order_avg_ms=(?P<avg_ms>[-.\d]+) "
    r"p50_ms=(?P<p50_ms>[-.\d]+) ack_avg_ms=(?P<ack_ms>[-.\d]+) "
    r"locked_pnl=(?P<locked>[-.\d]+) residual_pnl=(?P<residual>[-.\d]+) "
    r"session_pnl=(?P<session>[-.\d]+) would_lock=(?P<would>[-.\d]+)"
)

pnl_rows = []
for ev in bot_events:
    msg = str(ev.get("msg", ""))
    if "session metrics:" not in msg:
        continue
    m = METRIC_RE.search(msg)
    if not m:
        continue
    gd = m.groupdict()
    pnl_rows.append(
        {
            "idx": len(pnl_rows),
            "locked": float(gd["locked"]),
            "residual": float(gd["residual"]),
            "session": float(gd["session"]),
            "would_lock": float(gd["would"]),
        }
    )
pnl_df = (
    pd.DataFrame(pnl_rows)
    if pnl_rows
    else pd.DataFrame(columns=["idx", "locked", "residual", "session", "would_lock"])
)


def stale_banner(source_path: Path, is_stale: bool) -> None:
    if is_stale:
        age = age_label(source_path)
        st.html(
            f'<div class="stale-banner">STALE OR OFFLINE SOURCE \u2014 file updated {age}</div>'
        )
    else:
        age = age_label(source_path)
        st.html(
            f'<div style="font-size:0.55rem;color:#555555;text-align:right;margin-bottom:2px;">updated {age}</div>'
        )


def panel_header(
    title: str, source_path: Path, is_stale: bool, time_window: str = ""
) -> None:
    stale_banner(source_path, is_stale)
    tw = f'<span class="tw-label">[{time_window}]</span>' if time_window else ""
    st.subheader(f"&#9672; {title} {tw}")


header_cols = st.columns([2, 1, 1, 1, 1, 1])
with header_cols[0]:
    st.html(
        f'<div style="font-size:1.1rem;font-weight:700;color:#FFB900;letter-spacing:3px;">'
        f"&#9670; ARB TERMINAL"
        f'<span style="color:#555555;font-size:0.7rem;margin-left:12px;">{utc_now_str}</span></div>'
    )
with header_cols[1]:
    st.html(
        f'<div style="text-align:center;"><span class="status-dot {bot_dot}"></span>'
        f'<br><span style="font-size:0.7rem;color:#888;">BOT</span> '
        f'<span style="font-size:0.85rem;font-weight:700;">{bot_state.upper()}</span></div>'
    )
with header_cols[2]:
    if kill_switch_active:
        ks_text, ks_color, ks_dot = "ARMED", "var(--red)", "dot-red"
    elif kill_status["verified"]:
        ks_text, ks_color, ks_dot = "VERIFIED", "var(--green)", "dot-green"
    else:
        ks_text, ks_color, ks_dot = "UNVERIFIED", "var(--amber)", "dot-amber"
    st.html(
        f'<div style="text-align:center;"><span class="status-dot {ks_dot}"></span>'
        f'<br><span style="font-size:0.7rem;color:#888;">KILL SW</span> '
        f'<span style="font-size:0.85rem;font-weight:700;color:{ks_color};">{ks_text}</span></div>'
    )
with header_cols[3]:
    if not macro_status["available"]:
        macro_dot, macro_header = "dot-red", "FAIL-SAFE"
    elif active_windows:
        macro_dot, macro_header = "dot-red", f"{len(active_windows)} ACTIVE"
    else:
        macro_dot, macro_header = "dot-green", "AVAILABLE"
    st.html(
        f'<div style="text-align:center;"><span class="status-dot {macro_dot}"></span>'
        f'<br><span style="font-size:0.7rem;color:#888;">MACRO</span> '
        f'<span style="font-size:0.85rem;font-weight:700;">{macro_header}</span></div>'
    )
with header_cols[4]:
    st.html(
        f'<div style="text-align:center;">'
        f'<span style="font-size:0.7rem;color:#888;">LATENCY</span><br>'
        f'<span style="font-size:0.85rem;font-weight:700;">{lat_opp.get("avg_ms", 0):.2f}ms</span>'
        f'<br><span class="tw-label">profile snapshot</span></div>'
    )
with header_cols[5]:
    st.html(
        f'<div style="text-align:center;">'
        f'<span style="font-size:0.7rem;color:#888;">FRESHNESS</span><br>'
        f'<span style="font-size:0.85rem;font-weight:700;">{fresh_pct * 100:.1f}%</span>'
        f'<br><span class="tw-label">session_2026_08_05</span></div>'
    )
st.markdown("---")

if demo_mode and demo_session is not None:
    st.warning(
        f"DEMO MODE — sealed historical PAPER_TRADE replay {demo_session.session_id}; "
        f"network_used=false; SHA256 seal valid; position={demo_snapshot['progress'] * 100:.0f}%."
    )
    demo_cols = st.columns(5)
    demo_cols[0].metric("Replay seconds", f"{demo_snapshot['cutoff_seconds']:.3f}")
    demo_cols[1].metric("Visible events", demo_snapshot["visible_event_count"])
    demo_cols[2].metric("Fires", demo_snapshot["fires"])
    demo_cols[3].metric("Trade events", demo_snapshot["trade_events"])
    demo_cols[4].metric("Alerts", demo_snapshot["alerts"])
    with st.expander("Demo replay event tape", expanded=False):
        event_rows = [
            {
                "elapsed_s": event.get("_elapsed_s"),
                "clock": event.get("t"),
                "event": event.get("event"),
                "message": event.get("msg"),
            }
            for event in demo_snapshot.get("recent_events", [])
        ]
        st.dataframe(pd.DataFrame(event_rows), hide_index=True, width="stretch")

with st.container(border=True):
    st.subheader("&#9672; Live-Trading Control / Pre-flight")
    st.caption(
        "The operator flag is audited, but effective live submission remains hard-blocked: "
        "the exchange order endpoint is intentionally unbuilt. Paper and dry-run simulations remain available."
    )
    control_cols = st.columns([1, 1, 1, 2])
    with control_cols[0]:
        operator_color = "#FFB900" if live_control_state.operator_enabled else "#00FF66"
        st.html(
            f'<div class="terminal-tile"><div class="label">Master operator flag</div>'
            f'<div class="value" style="color:{operator_color};font-size:1rem;">'
            f"{'ENABLED / ARMED' if live_control_state.operator_enabled else 'DISABLED'}</div>"
            f'<div class="sub">revision {live_control_state.revision}</div></div>'
        )
    with control_cols[1]:
        effective_color = (
            "#00FF66"
            if live_control_state.effective_live_submission_enabled
            else "#FF3333"
        )
        st.html(
            f'<div class="terminal-tile"><div class="label">Effective live submission</div>'
            f'<div class="value" style="color:{effective_color};font-size:1rem;">'
            f"{'ON' if live_control_state.effective_live_submission_enabled else 'OFF'}</div>"
            f'<div class="sub">{live_control_state.hard_block_reason}</div></div>'
        )
    wallet_balances = wallet_status.get("balances", {})
    jurisdiction = wallet_status.get("jurisdiction", {})
    with control_cols[2]:
        st.html(
            f'<div class="terminal-tile"><div class="label">Funding status</div>'
            f'<div class="value" style="color:{"#00FF66" if wallet_status.get("funding_ready") else "#FF3333"};font-size:1rem;">'
            f"{'READY' if wallet_status.get('funding_ready') else 'NOT READY'}</div>"
            f'<div class="sub">native={wallet_balances.get("polygon_native")} | '
            f"pUSD={wallet_balances.get('pusd')} / required={wallet_balances.get('required_pusd')} | "
            f"approvals_ready={wallet_status.get('approvals_ready')}</div></div>"
        )
    with control_cols[3]:
        jurisdiction_status = str(jurisdiction.get("status", "not checked")).upper()
        jurisdiction_color = "#00FF66" if jurisdiction_status == "CLEAR" else "#FF3333"
        st.html(
            f'<div class="terminal-tile"><div class="label">Jurisdiction / compliance</div>'
            f'<div class="value" style="color:{jurisdiction_color};font-size:1rem;">{jurisdiction_status}</div>'
            f'<div class="sub">country={jurisdiction.get("country")} region={jurisdiction.get("region")} | '
            f"checked={wallet_status.get('checked_at', 'never')}</div></div>"
        )

    action_col, wallet_col = st.columns([2, 1])
    with action_col:
        desired_label = st.selectbox(
            "Master flag target",
            options=("DISABLED", "ENABLED / ARMED (still hard-blocked)"),
            index=1 if live_control_state.operator_enabled else 0,
            key="live_control_target",
        )
        actor = st.text_input(
            "Operator / actor", value="dashboard_operator", key="live_control_actor"
        )
        reason = st.text_input(
            "Audit reason", value="dashboard control update", key="live_control_reason"
        )
        apply_control = st.button(
            "Apply audited state change", key="apply_live_control_state"
        )
        if apply_control:
            try:
                live_control.set_operator_enabled(
                    desired_label.startswith("ENABLED"), actor=actor, reason=reason
                )
                st.success("State persisted and audit record fsynced.")
                st.rerun()
            except (TypeError, ValueError, OSError) as exc:
                st.error(f"State change rejected: {exc}")
    with wallet_col:
        st.write("Wallet queries are display-only (`eth_getBalance` + `eth_call`).")
        if st.button("Refresh read-only wallet / jurisdiction", key="refresh_wallet"):
            try:
                wallet_status = refresh_wallet_status(output_path=WALLET_STATUS)
                st.success(
                    f"Updated: funding_ready={wallet_status['funding_ready']}, "
                    f"jurisdiction={wallet_status['jurisdiction']['status']}"
                )
                st.rerun()
            except Exception as exc:
                st.error(f"Read-only refresh failed: {exc}")

    preflight_rows = [
        {
            "Gate": "Master flag / endpoint",
            "Status": (
                "verified-pass"
                if live_control_state.effective_live_submission_enabled
                else "verified-fail"
            ),
            "Current value": (
                f"operator={live_control_state.operator_enabled}; "
                f"effective={live_control_state.effective_live_submission_enabled}; "
                f"{live_control_state.hard_block_reason}"
            ),
            "Evidence": str(LIVE_CONTROL_STATE),
        },
        {
            "Gate": "Wallet funding",
            "Status": "verified-pass"
            if wallet_status.get("trading_ready")
            else "verified-fail",
            "Current value": (
                f"native={wallet_balances.get('polygon_native')}; pUSD={wallet_balances.get('pusd')}; "
                f"required={wallet_balances.get('required_pusd')}; "
                f"funding_ready={wallet_status.get('funding_ready')}; "
                f"approvals_ready={wallet_status.get('approvals_ready')}; "
                f"trading_ready={wallet_status.get('trading_ready')}"
            ),
            "Evidence": str(WALLET_STATUS),
        },
        {
            "Gate": "Jurisdiction",
            "Status": (
                "verified-pass"
                if jurisdiction.get("status") == "clear"
                else "blocked-external"
                if jurisdiction.get("status") == "blocked"
                else "not-yet-tested"
            ),
            "Current value": (
                f"status={jurisdiction.get('status')}; country={jurisdiction.get('country')}; "
                f"region={jurisdiction.get('region')}"
            ),
            "Evidence": str(WALLET_STATUS),
        },
        {
            "Gate": "Kill switch + alert",
            "Status": "verified-pass"
            if kill_status.get("verified")
            else "verified-fail",
            "Current value": f"verified={kill_status.get('verified')}; armed={kill_status.get('armed')}",
            "Evidence": str(
                latest_session_path if latest_session.get("available") else ALERTS_LOG
            ),
        },
        {
            "Gate": "Paper session",
            "Status": "verified-pass"
            if latest_session.get("available")
            else "not-yet-tested",
            "Current value": (
                f"session={latest_session.get('session_id')}; opportunities={paper_opps}; fills={paper_fills}"
            ),
            "Evidence": str(latest_session_path),
        },
        {
            "Gate": "Depth continuity",
            "Status": depth_replay_status,
            "Current value": (
                f"{depth_status.get('complete_windows', 0)}/"
                f"{depth_status.get('required_complete_windows', 30)} complete windows"
            ),
            "Evidence": str(DEPTH_REPLAY_VAL),
        },
        {
            "Gate": "Regression suite",
            "Status": "verified-pass"
            if test_status.get("status") == "pass"
            else "verified-fail",
            "Current value": (
                f"passed={test_status.get('passed', 0)}; failures={test_status.get('failures', 0)}; "
                f"errors={test_status.get('errors', 0)}"
            ),
            "Evidence": str(test_source_path),
        },
    ]
    authoritative_gates = product_status.get("gates", [])
    if isinstance(authoritative_gates, list) and authoritative_gates:
        preflight_rows = [
            {
                "Gate": gate.get("Gate"),
                "Status": gate.get("Status"),
                "Current value": gate.get("Current value"),
                "Criterion": gate.get("Criterion"),
                "Evidence": "; ".join(str(item) for item in gate.get("Evidence", [])),
            }
            for gate in authoritative_gates
            if isinstance(gate, dict)
        ]
    st.dataframe(pd.DataFrame(preflight_rows), hide_index=True, width="stretch")
    with st.expander("Live-control audit log", expanded=False):
        audit_rows = list(reversed(live_control_audit[-50:]))
        if audit_rows:
            st.dataframe(pd.DataFrame(audit_rows), hide_index=True, width="stretch")
        else:
            st.info("No state changes or submission checks recorded yet.")

st.html(
    '<div style="font-size:0.65rem;color:#888;text-transform:uppercase;letter-spacing:1px;margin-bottom:4px;">Model Verdict</div>'
)
verdict_cols = st.columns(3)
with verdict_cols[0]:
    best_btc = btc_status.get("best_policy", {})
    btc_verdict = (
        "NOT DEPLOYABLE"
        if btc_status.get("status") == "not deployable"
        else "UNAVAILABLE"
    )
    best_btc_mean = best_btc.get("mean_net_pnl_bps")
    best_btc_text = (
        f"{best_btc_mean:.6f} bps" if isinstance(best_btc_mean, (int, float)) else "n/a"
    )
    st.html(
        f'<div class="verdict-item"><div class="v-model">BTC offline sizing</div>'
        f'<div class="v-status" style="color:#FF3333;">{btc_verdict}</div>'
        f'<div class="v-detail">{btc_status.get("negative_policy_count", 0)}/'
        f"{btc_status.get('policy_count', 0)} policies negative | best mean {best_btc_text}</div></div>"
    )
with verdict_cols[1]:
    st.html(
        '<div class="verdict-item"><div class="v-model">Mistral BTC</div>'
        '<div class="v-status" style="color:#555555;">N/A</div>'
        '<div class="v-detail">no separate model | &#34;Mistral Vibe&#34; is the report generator</div></div>'
    )
with verdict_cols[2]:
    kronos_acc = kronos_test.get("accuracy", 0) * 100
    st.html(
        f'<div class="verdict-item"><div class="v-model">Kronos</div>'
        f'<div class="v-status" style="color:#FF3333;">NOT DEPLOYABLE</div>'
        f'<div class="v-detail">test acc {kronos_acc:.2f}% | severe overfit | val {kronos_best_val * 100:.1f}%</div></div>'
    )
st.markdown("---")

st.html(
    '<div style="font-size:0.65rem;color:#888;text-transform:uppercase;letter-spacing:1px;margin-bottom:4px;">Go-Live Checklist</div>'
)
gl_cols = st.columns(5)
with gl_cols[0]:
    st.html(
        '<div class="go-live-item"><div class="gl-label">Geoblock</div>'
        '<div class="gl-status" style="color:#FF3333;">FAIL</div>'
        '<div style="font-size:0.5rem;color:#555555;">CA blocked</div></div>'
    )
with gl_cols[1]:
    if kill_switch_active:
        ks_gl, ks_gl_color = "ARMED", "#FF3333"
    elif kill_status["verified"]:
        ks_gl, ks_gl_color = "VERIFIED PASS", "#00FF66"
    else:
        ks_gl, ks_gl_color = "NOT VERIFIED", "#FFB900"
    st.html(
        f'<div class="go-live-item"><div class="gl-label">Kill Switch</div>'
        f'<div class="gl-status" style="color:{ks_gl_color};">{ks_gl}</div>'
        f'<div style="font-size:0.5rem;color:#555555;">armed={str(kill_switch_active).lower()} | '
        f"verified session {kill_status.get('session_id', 'unknown')}</div></div>"
    )
with gl_cols[2]:
    strict_fill_rate = depth_status.get("requested_quantity_fill_rate", 0.0) * 100.0
    st.html(
        '<div class="go-live-item"><div class="gl-label">Fill Ratio</div>'
        '<div class="gl-status" style="color:#FF3333;">SIM ONLY</div>'
        f'<div style="font-size:0.5rem;color:#555555;">{strict_fill_rate:.14f}% requested quantity | '
        f"{depth_status.get('replay_halted_windows', 0)} halted windows</div></div>"
    )
with gl_cols[3]:
    depth_color = "#00FF66" if depth_replay_status == "verified-pass" else "#FF3333"
    depth_rows_text = (
        f"{depth_status['rows']:,}"
        if isinstance(depth_status.get("rows"), int)
        else "unknown"
    )
    st.html(
        f'<div class="go-live-item"><div class="gl-label">Depth Gate</div>'
        f'<div class="gl-status" style="color:{depth_color};">{depth_replay_status.upper()}</div>'
        f'<div style="font-size:0.5rem;color:#555555;">{depth_rows_text} consolidated rows | '
        f"{depth_status.get('complete_windows', 0)}/{depth_status.get('required_complete_windows', 0)} complete windows</div></div>"
    )
with gl_cols[4]:
    test_fail_total = int(test_status.get("failures", 0)) + int(
        test_status.get("errors", 0)
    )
    test_color = "#00FF66" if test_status.get("status") == "pass" else "#FF3333"
    st.html(
        '<div class="go-live-item"><div class="gl-label">Test Suite</div>'
        f'<div class="gl-status" style="color:{test_color};">{test_status.get("passed", 0)} PASS</div>'
        f'<div style="font-size:0.5rem;color:#555555;">{test_fail_total} failed/error | '
        f"{test_status.get('matching_runs', 0)} matching dated run(s)</div></div>"
    )
st.markdown("---")

col_left, col_mid, col_right = st.columns([1.2, 1, 1])

with col_left:
    with st.container(border=True):
        panel_header(
            "PnL / Performance",
            latest_session_path,
            False,
            "legacy structural backtest + latest sealed PAPER_TRADE",
        )
        pnl_tcols = st.columns(4)
        with pnl_tcols[0]:
            locked_display = f"USD {bt_locked:.4f}" if backtest_comparable else "N/A"
            locked_color = "#00FF66" if backtest_comparable else "#FF3333"
            st.html(
                f'<div class="terminal-tile"><div class="label">Executable BT PnL</div>'
                f'<div class="value" style="color:{locked_color};">{locked_display}</div>'
                f'<div class="sub">comparable={str(backtest_comparable).lower()}</div></div>'
            )
        with pnl_tcols[1]:
            st.html(
                f'<div class="terminal-tile"><div class="label">Session</div>'
                f'<div class="value" style="color:#00FFFF;">${paper_session_pnl:.4f}</div>'
                f'<div class="sub">{paper_fills} fills / {paper_opps} opportunities</div></div>'
            )
        with pnl_tcols[2]:
            st.html(
                f'<div class="terminal-tile"><div class="label">Would-Lock</div>'
                f'<div class="value" style="color:#FFB900;">${paper_would_lock:.2f}</div>'
                f'<div class="sub">latest sealed session</div></div>'
            )
        with pnl_tcols[3]:
            uncapped_display = (
                f"USD {uncapped_pnl:.4f}" if backtest_comparable else "N/A"
            )
            uncapped_color = "#FF00FF" if backtest_comparable else "#FF3333"
            st.html(
                f'<div class="terminal-tile"><div class="label">Executable Uncapped</div>'
                f'<div class="value" style="color:{uncapped_color};">{uncapped_display}</div>'
                f'<div class="sub">outcome-book equivalent</div></div>'
            )
        if not backtest_comparable:
            st.error(
                f"NON-COMPARABLE: source={backtest_source}. The legacy structural result "
                f"(USD {bt_locked:.4f} capped / USD {uncapped_pnl:.4f} requested-quantity equivalent) "
                "is not executable Polymarket outcome-book PnL."
            )
        if latest_session.get("available") and not latest_session.get(
            "sample_available"
        ):
            st.info(
                f"PnL convergence unavailable: sample_available=false; latest sealed session "
                f"{latest_session.get('session_id')} completed with {paper_opps} opportunities and {paper_fills} fills."
            )
        elif not pnl_df.empty:
            chart_data = pnl_df[
                ["idx", "locked", "residual", "session", "would_lock"]
            ].melt("idx", var_name="bucket", value_name="usd")
            chart = (
                alt.Chart(chart_data)
                .mark_line(point=False)
                .encode(
                    x=alt.X("idx:Q", title="Sample"),
                    y=alt.Y("usd:Q", title="USD"),
                    color=alt.Color(
                        "bucket:N",
                        scale=alt.Scale(
                            range=["#00FF66", "#00FFFF", "#FFB900", "#FF00FF"]
                        ),
                    ),
                    tooltip=["bucket", "usd"],
                )
                .properties(height=160)
            )
            st.altair_chart(chart, width="stretch")
        else:
            st.info("No session metrics samples in bot log yet.")
        st.html(
            f'<div style="font-size:0.6rem;color:#555555;margin-top:4px;">'
            f"Legacy structural BT: capped USD {bt_locked:.4f} | uncapped USD {uncapped_pnl:.4f} | "
            f"fill rate {fill_rate * 100:.1f}% | opps {bt_opps} | "
            f"comparable={str(backtest_comparable).lower()} | "
            f"Paper: {paper_fills} fills / {paper_opps} opportunities | "
            f"sample_available={str(bool(latest_session.get('sample_available'))).lower()}</div>"
        )

    with st.container(border=True):
        panel_header("Latency Profile", LATENCY_PROFILE, bot_stale, "13-fire profile")
        if lat_stages:
            lat_cols = st.columns(4)
            with lat_cols[0]:
                avg_ms = lat_opp.get("avg_ms", 0)
                color = "#00FF66" if avg_ms < 500 else "#FF3333"
                st.html(
                    f'<div class="terminal-tile"><div class="label">Avg opp-to-order</div>'
                    f'<div class="value" style="color:{color};">{avg_ms:.2f}ms</div>'
                    f'<div class="sub">&lt;500ms target</div></div>'
                )
            with lat_cols[1]:
                p50 = lat_opp.get("p50_ms", 0)
                st.html(
                    f'<div class="terminal-tile"><div class="label">p50</div>'
                    f'<div class="value" style="color:#00FFFF;">{p50:.2f}ms</div>'
                    f'<div class="sub">median</div></div>'
                )
            with lat_cols[2]:
                p95 = lat_opp.get("p95_ms", 0)
                st.html(
                    f'<div class="terminal-tile"><div class="label">p95</div>'
                    f'<div class="value" style="color:#FFB900;">{p95:.2f}ms</div>'
                    f'<div class="sub">tail</div></div>'
                )
            with lat_cols[3]:
                mx = lat_opp.get("max_ms", 0)
                st.html(
                    f'<div class="terminal-tile"><div class="label">max</div>'
                    f'<div class="value" style="color:#FF3333;">{mx:.2f}ms</div>'
                    f'<div class="sub">worst</div></div>'
                )
            stage_rows = []
            for name, data in lat_stages.items():
                stage_rows.append(
                    {
                        "Stage": name,
                        "Avg": data.get("avg_ms", 0),
                        "p50": data.get("p50_ms", 0),
                        "p95": data.get("p95_ms", 0),
                        "Max": data.get("max_ms", 0),
                        "N": data.get("n", 0),
                    }
                )
            st.dataframe(pd.DataFrame(stage_rows), hide_index=True, width="stretch")
            if paper_avg_lat > 0:
                st.html(
                    f'<div style="font-size:0.6rem;color:#555555;margin-top:4px;">'
                    f"Paper session: avg={paper_avg_lat:.1f}ms p50={paper_p50_lat:.1f}ms | Profile n={lat_opp.get('n', 0)} fires</div>"
                )
        else:
            st.info("No latency profile data.")

with col_mid:
    with st.container(border=True):
        panel_header(
            "Alerts Feed",
            ALERTS_LOG,
            bot_stale,
            f"{len(alerts_prod)} prod + {len(alerts_test)} test filtered",
        )
        if alerts_prod:
            kind_counts: dict[str, int] = {}
            for e in alerts_prod:
                k = e.get("kind", "unknown")
                kind_counts[k] = kind_counts.get(k, 0) + 1
            sev_cols = st.columns(len(kind_counts))
            for i, (kind, count) in enumerate(sorted(kind_counts.items())):
                sev_color = (
                    "#FF3333"
                    if "kill" in kind
                    else (
                        "#FFB900"
                        if "disconnect" in kind or "rejection" in kind
                        else "#00FFFF"
                    )
                )
                with sev_cols[i]:
                    st.html(
                        f'<div class="terminal-tile" style="text-align:center;">'
                        f'<div class="value" style="color:{sev_color};font-size:1.1rem;">{count}</div>'
                        f'<div class="label">{kind}</div></div>'
                    )
            recent = alerts_prod[-15:][::-1]
            alert_html = '<div class="alert-feed">'
            for e in recent:
                ts = e.get("ts", 0)
                kind = e.get("kind", "?")
                msg = e.get("message", "")
                sev = (
                    "CRIT"
                    if "kill" in kind
                    else (
                        "WARN"
                        if "disconnect" in kind or "rejection" in kind
                        else "INFO"
                    )
                )
                t_str = ts_to_hms(ts)
                alert_html += (
                    f'<div class="alert-row"><span class="alert-ts">{t_str}</span>'
                    f'<span class="alert-sev sev-{sev}">{sev}</span>'
                    f'<span style="color:#AAA;flex:1;">{msg[:80]}</span></div>'
                )
            alert_html += "</div>"
            st.html(alert_html)
        else:
            st.info("No production alerts.")
        if alerts_test:
            st.html(
                f'<div style="font-size:0.6rem;color:#FF00FF;margin-top:4px;">'
                f"\u25c6 {len(alerts_test)} test entries filtered (not shown) \u2014 conftest isolate_alerts_log protects test runs</div>"
            )

    with st.container(border=True):
        panel_header(
            "Model Status",
            BTC_SIZING_EVIDENCE,
            False,
            "2026-08-15 chronological holdouts",
        )
        if btc_status.get("available"):
            st.html(
                f'<div style="font-size:0.75rem;color:#FF3333;font-weight:700;margin:4px 0;">'
                f"BTC OFFLINE SIZING — NOT DEPLOYABLE</div>"
                f'<div style="font-size:0.6rem;color:#888;margin-bottom:5px;">'
                f"{btc_status['negative_policy_count']}/{btc_status['policy_count']} model-policy results "
                f"are negative across every holdout range.</div>"
            )
            policies_by_model: dict[str, list[dict[str, Any]]] = {}
            for policy in btc_status["policies"]:
                policies_by_model.setdefault(policy["model"], []).append(policy)
            model_cols = st.columns(max(1, len(policies_by_model)))
            model_labels = {
                "gradient_boosted_trees": "Gradient-boosted trees",
                "logistic_regression": "Logistic regression",
            }
            policy_labels = {
                "binary_f1": "binary F1",
                "top_decile": "top decile",
                "confidence_weighted": "confidence weighted",
            }
            for column, (model_name, policies) in zip(
                model_cols, policies_by_model.items()
            ):
                rows_html = "".join(
                    f'<div class="sub">{policy_labels.get(policy["policy"], policy["policy"])}: '
                    f"{policy['mean_net_pnl_bps']:.6f} bps mean "
                    f"[{policy['range_low_bps']:.6f}, {policy['range_high_bps']:.6f}]</div>"
                    for policy in policies
                )
                with column:
                    st.html(
                        f'<div class="terminal-tile"><div class="label">'
                        f"{model_labels.get(model_name, model_name)}</div>"
                        f'<div class="value" style="color:#FF3333;font-size:1rem;">ALL NEGATIVE</div>'
                        f"{rows_html}</div>"
                    )
            best_policy = btc_status["best_policy"]
            st.html(
                f'<div style="font-size:0.6rem;color:#555555;margin-top:4px;">'
                f"Least-negative mean: {best_policy['model']}/{best_policy['policy']} "
                f"{best_policy['mean_net_pnl_bps']:.6f} bps | "
                f"Executable Polymarket PnL: {str(btc_status['executable_status']).upper()} | BTC proxy only</div>"
            )
        else:
            st.warning(
                "BTC offline sizing evidence is unavailable; no deployability claim is made."
            )

with col_right:
    with st.container(border=True):
        panel_header(
            "Macro Risk",
            MACRO_EVENTS,
            not macro_status["available"],
            f"checked @ {utc_now_str}",
        )
        if not macro_status["available"]:
            gauge_color = "#FF3333"
            gauge_label = macro_status["state"].upper()
        elif active_windows:
            gauge_color = "#FF3333"
            gauge_label = "HIGH RISK ACTIVE"
        else:
            gauge_color = "#00FF66"
            gauge_label = "FRESH / NO ACTIVE WINDOW"
        gauge_value = str(len(active_windows)) if macro_status["available"] else "—"
        st.html(
            f'<div style="text-align:center;padding:6px 0;">'
            f'<div style="font-size:1.8rem;font-weight:700;color:{gauge_color};">{gauge_value}</div>'
            f'<div style="font-size:0.6rem;color:#555555;text-transform:uppercase;letter-spacing:1px;">{gauge_label}</div>'
            f"</div>"
        )
        st.html(
            f'<div style="font-size:0.6rem;color:#555555;text-align:center;margin:4px 0;">'
            f"Current time: {utc_now_str}<br>"
            f"{macro_status['detail']} | "
            f"{len(high_risk_windows)} total windows in feed</div>"
        )
        if macro_status["available"] and upcoming_windows:
            st.html(
                '<div style="font-size:0.65rem;color:#888;text-transform:uppercase;letter-spacing:1px;margin:4px 0;">Next event</div>'
            )
            next_w = upcoming_windows[0]
            evt = next_w.get("event", "unknown")
            start_str = ts_to_hms(next_w.get("start", 0))
            cd = countdown_str(next_w.get("start", 0), now)
            st.html(
                f'<div style="font-size:0.7rem;padding:4px 0;border-bottom:1px solid #1a1a1e;">'
                f'<span style="color:#FFB900;">{start_str}</span> '
                f'<span style="color:#00FFFF;">({cd})</span><br>'
                f'<span style="color:#AAA;font-size:0.65rem;">{evt[:50]}</span></div>'
            )
        elif macro_status["available"] and high_risk_windows:
            st.html(
                '<div style="font-size:0.65rem;color:#555555;">No upcoming windows. All past.</div>'
            )
        elif macro_status["available"]:
            st.html(
                '<div style="font-size:0.65rem;color:#555555;">No macro event windows loaded.</div>'
            )
        else:
            st.html(
                '<div style="font-size:0.65rem;color:#FF3333;">Macro decisions unavailable; fail-safe blocking state is active.</div>'
            )
        st.html(
            f'<div style="font-size:0.6rem;color:#555555;margin-top:6px;">'
            f"{len(macro_events_list)} events | {len(high_risk_windows)} windows | "
            f"last_update age: {macro_status['age_s']:.1f}s | freshness limit: 900.0s</div>"
        )

    with st.container(border=True):
        panel_header(
            "Depth Capture",
            DEPTH_CONSOLIDATED,
            not depth_status["available"],
            "consolidated real order books",
        )
        if depth_status["available"]:
            depth_rows = depth_status.get("rows")
            depth_rows_display = (
                f"{depth_rows:,}"
                if isinstance(depth_rows, int)
                else "ROW COUNT UNVERIFIED"
            )
            st.html(
                f'<div class="terminal-tile"><div class="label">Snapshots</div>'
                f'<div class="value" style="color:#00FF66;">{depth_rows_display}</div>'
                f'<div class="sub">{depth_status["bytes"]:,} bytes | ground-truth bytes match: '
                f"{str(depth_status['evidence_matches_file']).lower()}</div></div>"
            )
            st.html(
                f'<div style="font-size:0.6rem;color:#555555;margin-top:4px;">'
                f"Consolidated duplicate keys: {depth_status.get('duplicate_keys')} | "
                f"chronology violations: {depth_status.get('chronology_violations')}<br>"
                f'Strict historical replay: <span style="color:#FF3333;">{depth_replay_status.upper()}</span> '
                f"({depth_status['replay_complete_ticks']}/{depth_status['replay_total_ticks']} paired ticks; "
                f"{depth_status.get('complete_windows', 0)}/{depth_status.get('required_complete_windows', 0)} complete windows; "
                f"{depth_status.get('replay_fills', 0)} fills; {depth_status.get('replay_partials', 0)} partials; "
                f"{depth_status.get('replay_halted_windows', 0)} halted windows) \u2014 "
                f"{depth_replay_reason[:100]}</div>"
            )
        else:
            st.html(
                '<div class="terminal-tile"><div class="label">Status</div>'
                '<div class="value" style="color:#FFB900;font-size:1rem;">NO DATA YET</div>'
                '<div class="sub">polymarket_depth_consolidated.jsonl not found</div></div>'
            )
            st.html(
                f'<div style="font-size:0.6rem;color:#555555;margin-top:4px;">'
                f'Replay validation: <span style="color:#FF3333;">{depth_replay_status.upper()}</span> \u2014 {depth_replay_reason[:60]}</div>'
            )

    with st.container(border=True):
        panel_header(
            "CI / Tests", test_source_path, False, "latest dated JUnit / ground truth"
        )
        ci_failed = int(test_status.get("failures", 0)) + int(
            test_status.get("errors", 0)
        )
        ci_pass_color = "#00FF66" if test_status.get("status") == "pass" else "#FFB900"
        ci_cols = st.columns(2)
        with ci_cols[0]:
            st.html(
                f'<div class="terminal-tile" style="text-align:center;">'
                f'<div class="value" style="color:{ci_pass_color};font-size:1.2rem;">{test_status.get("passed", 0)}</div>'
                f'<div class="label">PASSED</div></div>'
            )
        with ci_cols[1]:
            st.html(
                f'<div class="terminal-tile" style="text-align:center;">'
                f'<div class="value" style="color:#FF3333;font-size:1.2rem;">{ci_failed}</div>'
                f'<div class="label">FAILED / ERROR</div></div>'
            )
        ci_badge = "ci-pass" if test_status.get("status") == "pass" else "ci-fail"
        st.html(
            f'<div style="font-size:0.6rem;color:#555555;margin-top:4px;">'
            f"pytest {test_status.get('seconds', 0.0):.3f}s | "
            f'<span class="ci-badge {ci_badge}">{str(test_status.get("status", "unavailable")).upper()}</span> | '
            f"{test_status.get('matching_runs', 0)} matching dated run(s) | "
            f"{test_source_path.name} | skipped: {test_status.get('skipped', 0)}</div>"
        )
        if fresh_bands:
            band_cols = st.columns(len(fresh_bands))
            band_colors = {
                "fresh": "#00FF66",
                "0.5-2s": "#FFB900",
                "2-10s": "#FF3333",
                "stale": "#FF00FF",
            }
            for i, (band, count) in enumerate(fresh_bands.items()):
                with band_cols[i]:
                    color = band_colors.get(band, "#888")
                    st.html(
                        f'<div class="terminal-tile" style="text-align:center;">'
                        f'<div class="value" style="color:{color};font-size:1rem;">{count}</div>'
                        f'<div class="label">{band}</div></div>'
                    )

st.markdown("---")
footer_cols = st.columns(4)
with footer_cols[0]:
    st.html(
        f'<div style="font-size:0.6rem;color:#555555;">Bot: {bot_state} | '
        f"latest session {latest_session.get('session_id', 'unavailable')} | "
        f"finished {latest_session.get('finished_at', 'unavailable')}</div>"
    )
with footer_cols[1]:
    st.html(
        f'<div style="font-size:0.6rem;color:#555555;">Cross-venue: {cv_count} obs | {"kalshi vs polymarket" if cv_latest else "no data"}</div>'
    )
with footer_cols[2]:
    last_kill_str = ts_to_str(last_kill.get("ts", 0)) if last_kill else "never"
    st.html(
        f'<div style="font-size:0.6rem;color:#555555;">Emergency stop armed: '
        f"{str(kill_status['armed']).lower()} | verified event: "
        f"{str(kill_status['verified']).lower()} | alert log last: {last_kill_str}</div>"
    )
with footer_cols[3]:
    st.html(
        '<div style="font-size:0.6rem;color:#555555;text-align:right;">All data from state/ | Read-only | No fabrication</div>'
    )


def main() -> None:
    pass


if __name__ == "__main__":
    main()
