from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException

from app import db, guards
from app.settings import Settings


def _nb(**kw) -> datetime:
    return datetime.now(UTC).replace(tzinfo=None) - timedelta(**kw)


@pytest.fixture
def env(tmp_path):
    settings = Settings(
        storage_dir=tmp_path / "store",
        app_mode="team",
        job_cooldown_seconds=5,
        max_active_jobs_per_session=1,
        max_jobs_per_hour=3,
        max_daily_usd=1.0,
    )
    settings.ensure_dirs()
    db.init_db(f"sqlite:///{tmp_path / 'g.db'}")
    s = db.get_session()
    try:
        s.add(db.Document(id="d", filename="d.pdf", sha256="abcd1234deadbeef", n_pages=1))
        s.commit()
    finally:
        s.close()
    return settings


def _add_job(session_id="s1", status="done", cost=0.0, created=None, config=None) -> str:
    s = db.get_session()
    try:
        job = db.Job(
            document_id="d", status=status, cost_usd=cost, config={"session_id": session_id, **(config or {})}
        )
        if created is not None:
            job.created_at = created
        s.add(job)
        s.commit()
        return job.id
    finally:
        s.close()


# -- fingerprint / dedupe --------------------------------------------------
def test_config_fingerprint_ignores_irrelevant_keys():
    a = guards.config_fingerprint({"target_lang": "French", "pages": "all"})
    b = guards.config_fingerprint(
        {"pages": "all", "target_lang": "French", "api_key": "secret", "session_id": "x"}
    )
    assert a == b
    assert a != guards.config_fingerprint({"target_lang": "German", "pages": "all"})


def test_find_duplicate_within_window(env):
    cfg = {"target_lang": "French"}
    fp = guards.config_fingerprint(cfg)
    jid = _add_job(config={**cfg, "_fingerprint": f"abcd1234deadbeef:{fp}"}, created=_nb(seconds=5))
    s = db.get_session()
    try:
        dup = guards.find_duplicate(s, "s1", "abcd1234deadbeef", cfg, window=60)
        assert dup is not None and dup.id == jid
    finally:
        s.close()


def test_find_duplicate_outside_window(env):
    cfg = {"target_lang": "French"}
    fp = guards.config_fingerprint(cfg)
    _add_job(config={**cfg, "_fingerprint": f"abcd1234deadbeef:{fp}"}, created=_nb(minutes=5))
    s = db.get_session()
    try:
        assert guards.find_duplicate(s, "s1", "abcd1234deadbeef", cfg, window=60) is None
    finally:
        s.close()


def test_find_duplicate_is_per_session(env):
    cfg = {"target_lang": "French"}
    fp = guards.config_fingerprint(cfg)
    _add_job(session_id="other", config={**cfg, "_fingerprint": f"abcd1234deadbeef:{fp}"})
    s = db.get_session()
    try:
        assert guards.find_duplicate(s, "mine", "abcd1234deadbeef", cfg, window=60) is None
    finally:
        s.close()


# -- enforce_submit --------------------------------------------------------
def test_enforce_disabled_in_local():
    s = db.get_session()
    try:
        _add_job(status="running")
        guards.enforce_submit(s, Settings(storage_dir="x", app_mode="local"), "s1")  # no raise
    finally:
        s.close()


def test_enforce_active_job_cap(env):
    s = db.get_session()
    try:
        _add_job(status="running", created=_nb(minutes=1))
        with pytest.raises(HTTPException) as e:
            guards.enforce_submit(s, env, "s1")
        assert e.value.status_code == 429
    finally:
        s.close()


def test_enforce_cooldown(env):
    s = db.get_session()
    try:
        _add_job(status="done", created=_nb(seconds=1))  # no active, but too recent
        with pytest.raises(HTTPException) as e:
            guards.enforce_submit(s, env, "s1")
        assert e.value.status_code == 429
        assert "Retry-After" in e.value.headers
    finally:
        s.close()


def test_enforce_hourly_quota(env):
    s = db.get_session()
    try:
        for i in range(3):  # max_jobs_per_hour=3
            _add_job(status="done", created=_nb(minutes=10 + i))
        with pytest.raises(HTTPException) as e:
            guards.enforce_submit(s, env, "s1")
        assert e.value.status_code == 429
    finally:
        s.close()


def test_enforce_daily_budget(env):
    s = db.get_session()
    try:
        _add_job(status="done", cost=1.0, created=_nb(minutes=30))  # == max_daily_usd
        with pytest.raises(HTTPException) as e:
            guards.enforce_submit(s, env, "s1")
        assert e.value.status_code == 402
    finally:
        s.close()


# -- API integration (team mode) ------------------------------------------
@pytest.fixture
def client_team(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app
    from app.settings import get_settings

    monkeypatch.setenv("APP_MODE", "team")
    monkeypatch.setenv("JOB_COOLDOWN_SECONDS", "5")
    get_settings.cache_clear()

    from tests.conftest import FakeProvider

    monkeypatch.setattr("ocrtran.pipeline.build_provider", lambda cfg, key=None: FakeProvider())
    monkeypatch.setattr("app.books._default_provider", lambda cfg, key, st: FakeProvider())
    with TestClient(app) as c:
        c.put("/api/session", json={"api_key": "k"})
        yield c
    monkeypatch.undo()
    get_settings.cache_clear()


def _upload(c, path):
    with open(path, "rb") as fh:
        return c.post("/api/documents", files={"file": ("tiny.pdf", fh, "application/pdf")}).json()


def test_api_dedupe_returns_same_job(client_team, tiny_pdf):
    doc = _upload(client_team, tiny_pdf)
    body = {"target_lang": "French"}
    a = client_team.post(f"/api/documents/{doc['id']}/jobs", json=body).json()
    b = client_team.post(f"/api/documents/{doc['id']}/jobs", json=body).json()
    assert a["id"] == b["id"]  # identical request within the window -> same job


def test_api_cooldown_rejects_different_job(client_team, tiny_pdf):
    doc = _upload(client_team, tiny_pdf)
    client_team.post(f"/api/documents/{doc['id']}/jobs", json={"target_lang": "French"})
    r = client_team.post(f"/api/documents/{doc['id']}/jobs", json={"target_lang": "German"})
    assert r.status_code == 429
    assert "Retry-After" in r.headers


# -- public mode: BYOK mandatory ------------------------------------------
@pytest.fixture
def client_public(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app
    from app.settings import get_settings

    monkeypatch.setenv("APP_MODE", "public")
    monkeypatch.delenv("DS_KEY", raising=False)
    get_settings.cache_clear()

    from tests.conftest import FakeProvider

    monkeypatch.setattr("ocrtran.pipeline.build_provider", lambda cfg, key=None: FakeProvider())
    monkeypatch.setattr("app.books._default_provider", lambda cfg, key, st: FakeProvider())
    with TestClient(app) as c:
        yield c
    monkeypatch.undo()
    get_settings.cache_clear()


def test_public_requires_byok(client_public, tiny_pdf):
    doc = _upload(client_public, tiny_pdf)
    H = {"X-Session-Id": "public-fresh"}  # a session with no stored key
    # no key anywhere -> rejected (no server-key fallback in public mode)
    r = client_public.post(f"/api/documents/{doc['id']}/jobs", json={"target_lang": "French"}, headers=H)
    assert r.status_code == 400, r.text
    assert "BYOK" in r.json()["detail"]
    # providing the key works
    r = client_public.post(
        f"/api/documents/{doc['id']}/jobs",
        json={"target_lang": "French", "api_key": "sk-user"},
        headers=H,
    )
    assert r.status_code == 200, r.text


def test_public_book_requires_byok(client_public, tiny_pdf):
    doc = _upload(client_public, tiny_pdf)
    r = client_public.post(
        f"/api/documents/{doc['id']}/book",
        json={"target_lang": "French", "chunk_size": 1},
        headers={"X-Session-Id": "public-book-fresh"},
    )
    assert r.status_code == 400, r.text
