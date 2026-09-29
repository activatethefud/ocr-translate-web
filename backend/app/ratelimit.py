"""Weighted rate limiting + SSE connection slots (in-memory, light).

A token bucket per key (session id, or client IP behind a trusted proxy). Requests
are charged a *weight* so an expensive action (starting a job/book) costs far more
than a `GET`. Disabled in ``APP_MODE=local``.

This is a **pure ASGI** middleware (not ``BaseHTTPMiddleware``) so it never interferes
with SSE streaming.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict

from starlette.requests import Request
from starlette.responses import JSONResponse

# method-agnostic path -> weight (first matching prefix wins)
_WEIGHTED = [
    ("/api/health", 1),
    ("/api/pricing", 1),
    ("/api/session", 2),
    ("/api/models", 3),
    ("/api/documents", 10),  # uploads (POST) and metadata (GET) share the prefix
    ("/api/jobs", 2),  # polling / SSE connect
]
log = logging.getLogger("app.audit")

_JOB_CREATE = 20
_BOOK_CREATE = 50


def endpoint_weight(method: str, path: str) -> int:
    if method == "POST" and path.endswith("/jobs"):
        return _JOB_CREATE
    if method == "POST" and path.endswith("/book"):
        return _BOOK_CREATE
    for prefix, weight in _WEIGHTED:
        if path.startswith(prefix):
            return weight
    return 1


class RateLimiter:
    def __init__(self, per_min: int, burst: int, max_sse: int) -> None:
        self.rate = max(0.01, per_min / 60.0)  # tokens per second
        self.capacity = float(max(1, burst))
        self.max_sse = max(0, max_sse)
        self._tokens: dict[str, float] = defaultdict(lambda: self.capacity)
        self._seen: dict[str, float] = {}
        self._slots: dict[str, int] = defaultdict(int)
        self._lock = threading.Lock()

    # -- request budget ------------------------------------------------
    def check(self, key: str, cost: int = 1) -> tuple[bool, int]:
        now = time.time()
        with self._lock:
            tokens = self._tokens[key]
            elapsed = now - self._seen.get(key, now)
            tokens = min(self.capacity, tokens + elapsed * self.rate)
            self._seen[key] = now
            if tokens >= cost:
                self._tokens[key] = tokens - cost
                self._sweep(now)
                return True, 0
            self._tokens[key] = tokens
            retry = int((cost - tokens) / self.rate) + 1
            return False, retry

    def _sweep(self, now: float, max_idle: float = 600.0) -> None:
        if len(self._seen) < 5000:
            return
        for k, t in list(self._seen.items()):
            if now - t > max_idle and k not in self._slots:
                self._seen.pop(k, None)
                self._tokens.pop(k, None)

    # -- SSE slots -----------------------------------------------------
    def acquire_slot(self, key: str) -> bool:
        with self._lock:
            if self.max_sse and self._slots[key] >= self.max_sse:
                return False
            self._slots[key] += 1
            return True

    def release_slot(self, key: str) -> None:
        with self._lock:
            if self._slots.get(key):
                self._slots[key] -= 1

    def slots_used(self, key: str) -> int:
        return self._slots.get(key, 0)

    def stats(self) -> dict:
        with self._lock:
            return {
                "keys_tracked": len(self._seen),
                "active_sse_slots": sum(self._slots.values()),
            }


def client_key(request: Request, trust_proxy: bool) -> str:
    if trust_proxy:
        fwd = request.headers.get("x-forwarded-for", "")
        if fwd:
            return fwd.split(",")[0].strip()
    sid = request.headers.get("x-session-id")
    if sid:
        return f"sid:{sid[:64]}"
    host = request.client.host if request.client else "?"
    return f"ip:{host}"


class RateLimitMiddleware:
    """Charge each request against a weighted token bucket; return 429 when empty."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        state = scope["app"].state
        settings = getattr(state, "settings", None)
        limiter: RateLimiter | None = getattr(state, "limiter", None)
        if settings is not None and limiter is not None and settings.limits_enabled:
            request = Request(scope)
            key = client_key(request, settings.trust_proxy)
            allowed, retry = limiter.check(key, endpoint_weight(request.method, request.url.path))
            if not allowed:
                log.warning("ratelimit 429 key=%s path=%s retry=%s", key, request.url.path, retry)
                response = JSONResponse(
                    {"detail": "rate limit exceeded, slow down"},
                    status_code=429,
                    headers={"Retry-After": str(retry)},
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
