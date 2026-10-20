"""Environment loader for the dashboard's external data providers.

Keys live in the project `.env` file (gitignored) and are never hardcoded
or logged.  This module is stdlib-only and fails open: a missing key simply
means the corresponding provider endpoint reports "not set in .env".
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent

_ENV_FILE = PROJECT_ROOT / ".env"

_VALUES: Dict[str, str] = {}


def _load() -> Dict[str, str]:
    if _VALUES:
        return _VALUES
    if not _ENV_FILE.is_file():
        return _VALUES
    try:
        for line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            _VALUES[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    return _VALUES


def get(key: str) -> Optional[str]:
    """Return the .env value for ``key`` (process environment wins)."""
    env_value = os.environ.get(key)
    if env_value:
        return env_value
    return _load().get(key) or None


def lse_api_key() -> Optional[str]:
    return get("LSE_API_KEY")


def massive_api_key() -> Optional[str]:
    return get("MASSIVE_API_KEY")


def finnhub_api_key() -> Optional[str]:
    return get("FINNHUB_API_KEY")
