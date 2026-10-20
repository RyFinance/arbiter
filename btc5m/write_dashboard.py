#!/usr/bin/env python3
"""Write the complete streamlit_app.py dashboard."""
import textwrap

OUT = "~/polymarket_arb/streamlit_app.py"

with open(OUT, "w") as f:
    f.write(textwrap.dedent('''\
        from __future__ import annotations

        import json
        import os
        import re
        import time
        from datetime import datetime, timezone
        from pathlib import Path
        from typing import Any

        import altair as alt
        import pandas as pd
        import streamlit as st

        # Paths
        ROOT = Path(__file__).resolve().parent
        STATE_DIR = ROOT / "state"
        BOT_LOG = STATE_DIR / "bot.jsonl"
        ALERTS_LOG = STATE_DIR / "alerts.jsonl"
        BACKTEST_VAL = STATE_DIR / "backtest_validation.json"
        BACKTEST_REPORT = STATE_DIR / "backtest_validation_report.json"
        MACRO_EVENTS = STATE_DIR / "macro_events.json"
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
        DEPTH_REPLAY_VAL = STATE_DIR / "chunk_b_depth_replay_validation.json"
        EMERGENCY_STOP = ROOT / "emergency.stop"

        st.set_page_config(
            page_title="ARB TERMINAL",
            page_icon="\\u25c6",
            layout="wide",
            initial_sidebar_state="collapsed",
        )
    ''').replace('    ', '    '))

print("Part 1 skeleton written")
