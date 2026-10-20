"""Market cache persistence — COLD PATH.

Save/load the discovered market metadata to state/market_cache.json so the hot loop
never re-discovers or re-parses. CACHE_REFRESH cadence is the only time discovery
repeats, and it runs in the background — never inside the trading loop.
"""
from __future__ import annotations

import json
import os
import time
from typing import List

from .config import SETTINGS
from .models import MarketCache, MarketMeta


def cache_path() -> str:
    """Return the filesystem path to the market cache JSON file.
    
    COLD PATH constant. Kept as a function so callers don't hardcode the path.
    """
    return os.path.join(os.path.dirname(os.path.dirname(__file__)), "state", "market_cache.json")


def save(markets: List[MarketMeta], source: str = "gamma", path: str = None) -> str:
    """Persist a list of MarketMeta to the cache file.
    
    Args:
        markets: List of MarketMeta objects to cache
        source: Data source identifier (default: "gamma")
        path: Override cache file path (default: cache_path())
        
    Returns:
        The absolute path where the cache was written
    """
    path = path or cache_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    cache = MarketCache(generated_at=time.time(), source=source, markets=markets)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cache.to_dict(), f, separators=(",", ":"))
    return path


def load(path: str = None) -> MarketCache:
    """Load MarketCache from the cache file.
    
    Args:
        path: Override cache file path (default: cache_path())
        
    Returns:
        MarketCache object, or empty MarketCache if file doesn't exist
    """
    path = path or cache_path()
    if not os.path.exists(path):
        return MarketCache()
    with open(path, "r", encoding="utf-8") as f:
        return MarketCache.from_dict(json.load(f))


def is_fresh(cache: MarketCache, max_age_s: float = None) -> bool:
    """Check if cache is still fresh based on generation timestamp.
    
    Args:
        cache: MarketCache to check
        max_age_s: Maximum age in seconds (default: SETTINGS.cache_refresh_interval_s)
        
    Returns:
        True if cache has markets and is within max_age_s, False otherwise
    """
    max_age_s = max_age_s if max_age_s is not None else SETTINGS.cache_refresh_interval_s
    if not cache.markets:
        return False
    return (time.time() - cache.generated_at) <= max_age_s
