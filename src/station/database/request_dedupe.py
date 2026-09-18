from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import time
from collections.abc import Callable
from typing import Any

class RequestDedupeCache:
    def __init__(self) -> None:
        self._entries: dict[str, float] = {}
        self._lock = threading.Lock()
        self._ops_since_cleanup = 0

    def _cleanup_unlocked(self, now: float) -> None:
        stale = [k for k, exp in self._entries.items() if exp <= now]
        for k in stale:
            self._entries.pop(k, None)

    def contains(self, key: str) -> bool:
        if not key:
            return False
        now = time.time()
        with self._lock:
            exp = self._entries.get(key)
            if exp is None:
                return False
            if exp <= now:
                self._entries.pop(key, None)
                return False
            return True

    def remember(self, key: str, ttl: float = 300.0) -> bool:

        if not key:
            return True
        now = time.time()
        expires = now + max(ttl, 1.0)
        with self._lock:
            self._ops_since_cleanup += 1
            if self._ops_since_cleanup >= 1024:
                self._cleanup_unlocked(now)
                self._ops_since_cleanup = 0
            prev = self._entries.get(key)
            if prev is not None and prev > now:
                return False
            self._entries[key] = expires
            return True

    def cleanup(self, ttl: float = 3600.0) -> None:
        threshold = time.time() - max(ttl, 1.0)
        with self._lock:
            stale = [k for k, exp in self._entries.items() if exp <= threshold]
            for k in stale:
                self._entries.pop(k, None)

_SHARED = RequestDedupeCache()
_DB_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=64, thread_name_prefix="station-db")

def shared_dedupe_cache() -> RequestDedupeCache:
    return _SHARED

async def run_in_db_executor(fn: Callable[..., Any], *args: Any) -> Any:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_DB_EXECUTOR, fn, *args)
