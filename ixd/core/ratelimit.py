"""Token-bucket bandwidth limiting and speed measurement.

Limits compose hierarchically: a worker asks its download's bucket and the
global bucket for permission before every read, so a per-download cap and a
global cap can be active at the same time.  A rate of ``0`` means unlimited and
costs nothing at runtime.
"""

from __future__ import annotations

import threading
import time
from collections import deque


class TokenBucket:
    """Classic token bucket. ``rate`` is bytes/second; 0 disables limiting."""

    def __init__(self, rate: int = 0, burst_seconds: float = 1.0) -> None:
        self._lock = threading.Lock()
        self._rate = max(0, int(rate))
        self._burst_seconds = burst_seconds
        self._capacity = max(1, int(self._rate * burst_seconds)) if self._rate else 0
        self._tokens = float(self._capacity)
        self._last = time.monotonic()

    @property
    def rate(self) -> int:
        with self._lock:
            return self._rate

    def set_rate(self, rate: int) -> None:
        with self._lock:
            self._rate = max(0, int(rate))
            self._capacity = max(1, int(self._rate * self._burst_seconds)) if self._rate else 0
            self._tokens = min(self._tokens, float(self._capacity))
            self._last = time.monotonic()

    def _refill_unlocked(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last
        self._last = now
        if self._rate:
            self._tokens = min(float(self._capacity), self._tokens + elapsed * self._rate)

    def take(self, amount: int) -> float:
        """Try to take ``amount`` tokens.

        Returns 0.0 when granted, otherwise the number of seconds the caller
        should wait before retrying.
        """
        if amount <= 0:
            return 0.0
        with self._lock:
            if not self._rate:
                return 0.0
            self._refill_unlocked()
            if self._tokens >= amount:
                self._tokens -= amount
                return 0.0
            deficit = amount - self._tokens
            return deficit / self._rate

    def consume(self, amount: int, stop_event: threading.Event | None = None,
                max_wait: float = 5.0) -> bool:
        """Block until ``amount`` tokens are available.

        Returns ``False`` if ``stop_event`` fired while waiting.
        """
        while True:
            delay = self.take(amount)
            if delay <= 0:
                return True
            delay = min(delay, max_wait)
            if stop_event is not None:
                if stop_event.wait(delay):
                    return False
            else:
                time.sleep(delay)

    def suggested_read_size(self, default: int) -> int:
        """Keep reads small enough that a low cap still feels responsive."""
        with self._lock:
            if not self._rate:
                return default
            return max(4096, min(default, self._rate // 8 or default))


class CompositeLimiter:
    """Applies a per-download bucket and the global bucket together."""

    def __init__(self, *buckets: TokenBucket | None) -> None:
        self.buckets = [b for b in buckets if b is not None]

    def consume(self, amount: int, stop_event: threading.Event | None = None) -> bool:
        for bucket in self.buckets:
            if not bucket.consume(amount, stop_event):
                return False
        return True

    def suggested_read_size(self, default: int) -> int:
        size = default
        for bucket in self.buckets:
            size = min(size, bucket.suggested_read_size(default))
        return max(4096, size)

    @property
    def limited(self) -> bool:
        return any(bucket.rate for bucket in self.buckets)


class SpeedMeter:
    """Sliding-window throughput estimate, resilient to bursty reads."""

    def __init__(self, window: float = 5.0) -> None:
        self._window = window
        self._samples: deque[tuple[float, int]] = deque()
        self._lock = threading.Lock()
        self._total = 0

    def record(self, byte_count: int) -> None:
        if byte_count <= 0:
            return
        now = time.monotonic()
        with self._lock:
            self._samples.append((now, byte_count))
            self._total += byte_count
            cutoff = now - self._window
            while self._samples and self._samples[0][0] < cutoff:
                _, dropped = self._samples.popleft()
                self._total -= dropped

    @property
    def speed(self) -> float:
        now = time.monotonic()
        with self._lock:
            cutoff = now - self._window
            while self._samples and self._samples[0][0] < cutoff:
                _, dropped = self._samples.popleft()
                self._total -= dropped
            if not self._samples:
                return 0.0
            span = max(0.25, now - self._samples[0][0])
            return self._total / span

    def reset(self) -> None:
        with self._lock:
            self._samples.clear()
            self._total = 0

    def eta(self, remaining_bytes: int) -> float:
        speed = self.speed
        if speed <= 0 or remaining_bytes <= 0:
            return 0.0
        return remaining_bytes / speed
