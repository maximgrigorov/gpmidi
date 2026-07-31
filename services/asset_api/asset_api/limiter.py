"""In-process rate and concurrency limiter for Phase 1 (single replica)."""

from __future__ import annotations

import threading
import time
from collections import deque


class RateLimiter:
    """Token-bucket-style rate limiter per window."""

    def __init__(self, max_per_minute: int):
        self.max_per_minute = max_per_minute
        self._timestamps: deque[float] = deque()
        self._lock = threading.Lock()

    def allow(self) -> bool:
        now = time.monotonic()
        with self._lock:
            while self._timestamps and now - self._timestamps[0] > 60.0:
                self._timestamps.popleft()
            if len(self._timestamps) >= self.max_per_minute:
                return False
            self._timestamps.append(now)
            return True

    def retry_after(self) -> int:
        """Seconds until next slot opens."""
        with self._lock:
            if not self._timestamps:
                return 0
            oldest = self._timestamps[0]
            return max(1, int(60.0 - (time.monotonic() - oldest)) + 1)

    def reset(self) -> None:
        with self._lock:
            self._timestamps.clear()


class ConcurrencyLimiter:
    """Limits active concurrent operations."""

    def __init__(self, max_concurrent: int):
        self.max_concurrent = max_concurrent
        self._semaphore = threading.Semaphore(max_concurrent)
        self._active = 0
        self._lock = threading.Lock()

    def try_acquire(self) -> bool:
        acquired = self._semaphore.acquire(blocking=False)
        if acquired:
            with self._lock:
                self._active += 1
        return acquired

    def release(self) -> None:
        with self._lock:
            self._active -= 1
        self._semaphore.release()

    @property
    def active(self) -> int:
        with self._lock:
            return self._active

    def reset(self) -> None:
        with self._lock:
            while self._active > 0:
                self._semaphore.release()
                self._active -= 1
