"""Compact console logger + structured JSONL writer.

Two goals:
  * Human line fast enough to watch live (the [12:01:02.114] BTC-... format from the spec).
  * One JSONL line per event for post-hoc latency analysis.

All logging is non-blocking from the trading loop's perspective: console is stdout
(no disk flush per line unless needed) and JSONL is buffered via a single shared
file handle written synchronously (it's cheap, and we keep it as one append call).
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from typing import Optional

_LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}


class BotLogger:
    """Compact console logger with optional structured JSONL output.
    
    All logging is non-blocking: console uses stdout, JSONL uses a buffered
    file handle with a lock. Designed to be fast enough for live trading loops.
    """
    def __init__(self, jsonl_path: str = "", level: str = "INFO"):
        """Initialize the logger.
        
        Args:
            jsonl_path: Path for JSONL output file (empty string disables JSONL)
            level: Minimum log level: DEBUG, INFO, WARNING, ERROR, CRITICAL
        """
        self.level = _LEVELS.get(level.upper(), 20)
        self._jsonl_path = jsonl_path
        self._fh = None
        self._lock = threading.Lock()
        if jsonl_path:
            os.makedirs(os.path.dirname(jsonl_path) or ".", exist_ok=True)
            self._fh = open(jsonl_path, "a", buffering=1, encoding="utf-8")

    def _clock(self) -> str:
        # HOT PATH: strftime is cheap and good enough for human + JSONL timestamps.
        return datetime.now().strftime("%H:%M:%S.") + f"{datetime.now().microsecond // 1000:03d}"

    def _emit(self, event: str, msg: str, data: Optional[dict] = None):
        """Internal method to emit a log event.
        
        Args:
            event: Log level/event type (DEBUG, INFO, WARNING, ERROR, CRITICAL, TRADE)
            msg: Human-readable message
            data: Optional structured data for JSONL output
        """
        if _LEVELS.get(event.upper(), 20) < self.level:
            return
        # Console line — compact, fixed shape.
        print(f"[{self._clock()}] {msg}", flush=False)
        if self._fh is not None:
            rec = {"t": self._clock(), "event": event, "msg": msg}
            if data:
                rec["data"] = data
            with self._lock:
                self._fh.write(json.dumps(rec, separators=(",", ":"), default=str) + "\n")

    def debug(self, msg, data=None):
        """Log a debug message."""
        self._emit("DEBUG", msg, data)
    
    def info(self, msg, data=None):
        """Log an info message."""
        self._emit("INFO", msg, data)
    
    def warning(self, msg, data=None):
        """Log a warning message."""
        self._emit("WARNING", msg, data)
    
    def error(self, msg, data=None):
        """Log an error message."""
        self._emit("ERROR", msg, data)
    
    def critical(self, msg, data=None):
        """Log a critical message."""
        self._emit("CRITICAL", msg, data)

    def trade(self, outcome: str, data: dict):
        """Structured trade-attempt event for post-hoc freshness/PnL bucketing (FIX #1/#2).

        `outcome` is one of: 'filled' | 'missed' | 'partial' | 'partial_resolved'.
        `data` MUST include `age_ms` and `freshness_band` so we can bucket PnL by quote
        age band later. Emitted as a JSONL line with event="TRADE".
        """
        self._emit("TRADE", outcome, data)

    def close(self):
        """Close the JSONL file handle if open."""
        if self._fh is not None:
            self._fh.close()
            self._fh = None


# Module-level default; main.py replaces it with the configured instance.
log = BotLogger()
