"""Adaptive concurrency + backoff for provider rate limits.

One process-wide limiter caps how many model calls may run at once. Calls are
wrapped with ``with THROTTLE:``; when a request is rate-limited (HTTP 429 / 503)
the provider reports it, the limit is cut and a cooldown is enforced. After a quiet
period the limit ramps back up one slot at a time, so throughput recovers on its
own. Normal operation is unaffected (the limit only bites when the provider pushes
back), and it is shared across pages, book chunks and batches.
"""

from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger("ocrtran.throttle")

_DEFAULT_CEILING = 16
_MIN_LIMIT = 1


class Throttle:
    def __init__(
        self,
        ceiling: int = _DEFAULT_CEILING,
        min_limit: int = _MIN_LIMIT,
        quiet: float = 5.0,
        ramp_interval: float = 3.0,
        default_cooldown: float = 2.0,
    ) -> None:
        self._lock = threading.Lock()
        self._ceiling = max(1, int(ceiling))
        self._min = max(1, int(min_limit))
        self._limit = self._ceiling
        self._inflight = 0
        self._cooldown_until = 0.0
        self._last_down = 0.0
        self._last_up = 0.0
        self._events = 0
        self._quiet = quiet
        self._ramp_interval = ramp_interval
        self._default_cooldown = default_cooldown

    # -- introspection -------------------------------------------------
    @property
    def limit(self) -> int:
        with self._lock:
            return self._limit

    @property
    def ceiling(self) -> int:
        with self._lock:
            return self._ceiling

    @property
    def inflight(self) -> int:
        with self._lock:
            return self._inflight

    @property
    def events(self) -> int:
        with self._lock:
            return self._events

    def stats(self) -> dict:
        with self._lock:
            return {
                "limit": self._limit,
                "ceiling": self._ceiling,
                "inflight": self._inflight,
                "rate_limit_events": self._events,
            }

    # -- configuration -------------------------------------------------
    def ensure_ceiling(self, n: int) -> None:
        """Raise the ceiling to at least ``n`` (never lowers it)."""
        n = max(1, int(n or 1))
        with self._lock:
            if n > self._ceiling:
                at_ceiling = self._limit >= self._ceiling
                self._ceiling = n
                if at_ceiling:
                    self._limit = n

    def set_ceiling(self, n: int) -> int:
        """Set the ceiling exactly (e.g. from configuration at startup)."""
        with self._lock:
            self._ceiling = max(self._min, int(n))
            if self._limit > self._ceiling:
                self._limit = self._ceiling
            return self._ceiling

    def set_limit(self, n: int) -> int:
        """Force the current limit (used by tests / an admin override)."""
        with self._lock:
            self._limit = max(self._min, min(int(n), self._ceiling))
            return self._limit

    def reset(self) -> None:
        with self._lock:
            self._limit = self._ceiling
            self._inflight = 0
            self._cooldown_until = 0.0
            self._last_down = 0.0
            self._last_up = 0.0
            self._events = 0

    # -- gate ----------------------------------------------------------
    def acquire(self, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._lock:
                now = time.monotonic()
                if now < self._cooldown_until:
                    wait = min(self._cooldown_until - now, 0.5)
                elif self._inflight < self._limit:
                    self._inflight += 1
                    return True
                else:
                    wait = 0.05
                if deadline is not None and now > deadline:
                    return False
            time.sleep(wait)

    def release(self) -> None:
        with self._lock:
            if self._inflight > 0:
                self._inflight -= 1

    def __enter__(self) -> Throttle:
        self.acquire()
        return self

    def __exit__(self, *_exc) -> None:
        self.release()

    # -- feedback ------------------------------------------------------
    def note_rate_limited(self, retry_after: float | None = None) -> int:
        """A request was rate-limited: cut the limit and set a cooldown."""
        with self._lock:
            self._events += 1
            target = min(self._limit // 2, max(1, self._inflight // 2))
            self._limit = max(self._min, target)
            delay = retry_after if retry_after else self._default_cooldown
            self._cooldown_until = max(self._cooldown_until, time.monotonic() + max(0.5, delay))
            self._last_down = time.monotonic()
            limit = self._limit
        log.warning("rate limited -> concurrency %d (cooldown %.1fs)", limit, max(0.5, delay))
        return limit

    def note_success(self) -> None:
        """A request succeeded: gradually restore the limit after a quiet period."""
        with self._lock:
            now = time.monotonic()
            if now - self._last_down < self._quiet:
                return
            if now - self._last_up < self._ramp_interval:
                return
            if self._limit < self._ceiling:
                self._limit += 1
                self._last_up = now


THROTTLE = Throttle()
