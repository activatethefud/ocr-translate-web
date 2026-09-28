"""API tests. The vision provider is faked, so no network / no API key."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app.main import app
from tests.conftest import FakeProvider


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr("ocrtran.pipeline.build_provider", lambda cfg, key=None: FakeProvider())
    with TestClient(app) as c:
        yield c


def _upload(client, path):
    with open(path, "rb") as fh:
        return client.post("/api/documents", files={"file": ("tiny.pdf", fh, "application/pdf")})


def test_health(client):
    assert client.get("/api/health").json() == {"ok": True}


def test_upload_and_list(client, tiny_pdf):
    r = _upload(client, tiny_pdf)
    assert r.status_code == 200, r.text
    doc = r.json()
    assert doc["n_pages"] == 2
    assert client.get("/api/documents").json()[0]["id"] == doc["id"]
    assert client.get(f"/api/documents/{doc['id']}").status_code == 200


def test_upload_rejects_garbage(client, tmp_path):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"not a pdf at all")
    r = _upload(client, bad)
    assert r.status_code == 400


def test_models_without_key(client):
    r = client.get("/api/models")
    assert r.status_code == 200
    assert r.json()["models"][0]["id"]


def _wait(client, job_id, timeout=90):
    deadline = time.time() + timeout
    job = {}
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("done", "failed", "canceled"):
            return job
        time.sleep(0.2)
    return job


@pytest.mark.integration
def test_full_job_flow(client, tiny_pdf, xelatex_available):
    if not xelatex_available:
        pytest.skip("xelatex not installed")
    doc = _upload(client, tiny_pdf).json()
    r = client.post(
        f"/api/documents/{doc['id']}/jobs",
        json={
            "source_lang": "Serbian",
            "target_lang": "French",
            "font_main": "Noto Serif",
            "combine": "interleave",
            "bilingual": True,
        },
    )
    assert r.status_code == 200, r.text
    job_id = r.json()["id"]

    job = _wait(client, job_id)
    assert job["status"] == "done", job
    assert job["progress"] == 1.0
    assert job["artifacts"], job

    dl = client.get(job["artifacts"][0]["download_url"])
    assert dl.status_code == 200 and dl.content[:4] == b"%PDF"

    pages = client.get(f"/api/jobs/{job_id}/pages").json()
    assert len(pages) == 2
    assert pages[0]["blocks"]

    # simple block editor
    r = client.patch(f"/api/jobs/{job_id}/pages/1/blocks/0", json={"target": "Titre"})
    assert r.status_code == 200 and r.json()["target"] == "Titre"

    # reorder blocks (order editor)
    n = len(pages[0]["blocks"])
    r = client.post(f"/api/jobs/{job_id}/pages/1/reorder", json={"order": list(reversed(range(n)))})
    assert r.status_code == 200, r.text
    r = client.post(f"/api/jobs/{job_id}/pages/1/reorder", json={"order": [0] * n})
    assert r.status_code == 422

    # rebuild the edited page
    r = client.post(f"/api/jobs/{job_id}/pages/1/rebuild")
    assert r.status_code == 200, r.text


@pytest.mark.integration
def test_page_image(client, tiny_pdf, xelatex_available):
    if not xelatex_available:
        pytest.skip("xelatex not installed")
    doc = _upload(client, tiny_pdf).json()
    job_id = client.post(f"/api/documents/{doc['id']}/jobs", json={"target_lang": "French"}).json()["id"]
    _wait(client, job_id)
    r = client.get(f"/api/jobs/{job_id}/pages/1/image")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
