"""Compact console logger + structured JSONL writer.

All logging is non-blocking from the trading loop's perspective: console is stdout
and JSONL is buffered via a single shared file handle written synchronously.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from typing import Optional

_LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50, "TRADE": 20}


class BotLogger:
    """Compact console logger with optional structured JSONL output."""

    def __init__(self, jsonl_path: str = "", level: str = "INFO"):
        self.level = _LEVELS.get(level.upper(), 20)
        self._jsonl_path = jsonl_path
        self._fh = None
        self._lock = threading.Lock()
        if jsonl_path:
            self._open(jsonl_path)

    def _open(self, jsonl_path: str) -> None:
        os.makedirs(os.path.dirname(jsonl_path) or ".", exist_ok=True)
        self._fh = open(jsonl_path, "a", buffering=1, encoding="utf-8")

    def configure(self, jsonl_path: str = "", level: str = "INFO") -> None:
        """Reconfigure this logger's sink/level in place.

        polymarket_arb's main.py replaced the module-level `log` object wholesale
        and had to patch every importing module's local reference via a hardcoded
        module-name list -- modules absent from that list silently kept logging to
        the pre-configured (JSONL-disabled) instance. `log` here is a mutable
        singleton every module imports once; reconfiguring it in place means every
        importer sees the change with no list to keep in sync.
        """
        with self._lock:
            if self._fh is not None:
                self._fh.close()
                self._fh = None
            self.level = _LEVELS.get(level.upper(), 20)
            self._jsonl_path = jsonl_path
        if jsonl_path:
            self._open(jsonl_path)

    def _clock(self) -> str:
        now = datetime.now()
        return now.strftime("%H:%M:%S.") + f"{now.microsecond // 1000:03d}"

    def _emit(self, event: str, msg: str, data: Optional[dict] = None):
        if _LEVELS.get(event.upper(), 20) < self.level:
            return
        print(f"[{self._clock()}] {msg}", flush=False)
        if self._fh is not None:
            rec = {"t": self._clock(), "event": event, "msg": msg}
            if data:
                rec["data"] = data
            with self._lock:
                self._fh.write(json.dumps(rec, separators=(",", ":"), default=str) + "\n")

    def debug(self, msg, data=None):
        self._emit("DEBUG", msg, data)

    def info(self, msg, data=None):
        self._emit("INFO", msg, data)

    def warning(self, msg, data=None):
        self._emit("WARNING", msg, data)

    def error(self, msg, data=None):
        self._emit("ERROR", msg, data)

    def critical(self, msg, data=None):
        self._emit("CRITICAL", msg, data)

    def trade(self, outcome: str, data: dict):
        """Structured trade-attempt event for post-hoc latency/PnL bucketing.

        `outcome` is one of: 'filled' | 'missed' | 'partial' | 'partial_resolved'.
        Emitted as a JSONL line with event="TRADE".
        """
        self._emit("TRADE", outcome, data)

    def close(self):
        if self._fh is not None:
            self._fh.close()
            self._fh = None


# Module-level singleton. main.py calls log.configure(...) at startup instead of
# rebinding this name, so every module that did `from .logger import log` sees the
# reconfigured sink without needing to be enumerated anywhere.
log = BotLogger()
