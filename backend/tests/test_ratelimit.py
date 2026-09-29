from __future__ import annotations

import pytest

from app.ratelimit import RateLimiter, client_key, endpoint_weight


def test_endpoint_weights():
    assert endpoint_weight("POST", "/api/documents/x/jobs") == 20
    assert endpoint_weight("POST", "/api/documents/x/book") == 50
    assert endpoint_weight("POST", "/api/documents") == 10
    assert endpoint_weight("GET", "/api/health") == 1
    assert endpoint_weight("GET", "/api/jobs/x/events") == 2


def test_token_bucket_blocks_then_refills():
    lim = RateLimiter(per_min=60, burst=2, max_sse=0)
    assert lim.check("k", 1)[0] is True
    assert lim.check("k", 1)[0] is True
    ok, retry = lim.check("k", 1)
    assert ok is False and retry >= 1


def test_token_bucket_weighted_cost():
    lim = RateLimiter(per_min=60, burst=25, max_sse=0)
    assert lim.check("k", 20)[0] is True  # a job submission
    ok, _ = lim.check("k", 20)
    assert ok is False  # only 5 tokens left


def test_keys_are_isolated():
    lim = RateLimiter(per_min=60, burst=1, max_sse=0)
    assert lim.check("a", 1)[0] is True
    assert lim.check("b", 1)[0] is True  # different key
    assert lim.check("a", 1)[0] is False


def test_sse_slots():
    lim = RateLimiter(per_min=60, burst=10, max_sse=2)
    assert lim.acquire_slot("k") is True
    assert lim.acquire_slot("k") is True
    assert lim.acquire_slot("k") is False  # cap reached
    lim.release_slot("k")
    assert lim.acquire_slot("k") is True
    assert lim.stats()["active_sse_slots"] == 2


class _Req:
    def __init__(self, headers=None, host="1.2.3.4"):
        self.headers = headers or {}
        self.client = type("C", (), {"host": host})()


def test_client_key_forwarded_for():
    assert client_key(_Req({"x-forwarded-for": "9.9.9.9, 10.0.0.1"}), True) == "9.9.9.9"
    assert client_key(_Req({"x-forwarded-for": "9.9.9.9"}), False) == "ip:1.2.3.4"
    assert client_key(_Req({"x-session-id": "abc"}), False) == "sid:abc"


# -- API integration (rate limiting active only when APP_MODE != local) -----
@pytest.fixture
def client_rl(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app
    from app.settings import get_settings

    monkeypatch.setenv("APP_MODE", "team")
    monkeypatch.setenv("RATE_LIMIT_PER_MIN", "1")
    monkeypatch.setenv("RATE_BURST", "3")
    monkeypatch.setenv("ADMIN_TOKEN", "secret")
    get_settings.cache_clear()
    with TestClient(app) as c:
        yield c
    monkeypatch.undo()
    get_settings.cache_clear()


def test_api_rate_limit_429(client_rl):
    for _ in range(3):
        assert client_rl.get("/api/health").status_code == 200
    r = client_rl.get("/api/health")
    assert r.status_code == 429
    assert "Retry-After" in r.headers


def test_admin_stats_requires_token(client_rl):
    assert client_rl.get("/api/admin/stats").status_code == 403
    assert client_rl.get("/api/admin/stats", headers={"X-Admin-Token": "nope"}).status_code == 403
    r = client_rl.get("/api/admin/stats", headers={"X-Admin-Token": "secret"})
    assert r.status_code == 200
    body = r.json()
    assert "jobs" in body and "usage" in body and "ratelimit" in body
