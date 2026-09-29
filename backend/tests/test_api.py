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
    monkeypatch.setattr("app.books._default_provider", lambda cfg, key, st: FakeProvider())
    with TestClient(app) as c:
        # seed a session key so job creation succeeds (value is irrelevant: provider is faked)
        c.put("/api/session", json={"api_key": "test-key"})
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
    # a session with no stored key returns the default model without a network call
    r = client.get("/api/models", headers={"X-Session-Id": "models-nokey"})
    assert r.status_code == 200
    assert r.json()["models"][0]["id"]


def test_estimate(client, tiny_pdf):
    doc = _upload(client, tiny_pdf).json()
    r = client.get(f"/api/documents/{doc['id']}/estimate?model=deepseek-flash")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pages"] == 2 and body["est_cost_usd"] > 0


def test_usage_ok(client):
    r = client.get("/api/usage")
    assert r.status_code == 200
    assert "cost_usd" in r.json() and "calls" in r.json()


def test_session_stores_key_encrypted(client):
    r = client.get("/api/session")
    assert r.status_code == 200
    assert r.json()["has_key"] is True
    assert r.json()["hint"].startswith("••••")
    # clearing and re-setting
    assert client.delete("/api/session/key").json()["has_key"] is False
    assert client.get("/api/session").json()["has_key"] is False
    r = client.put("/api/session", json={"api_key": "sk-abcdef123456"})
    assert r.json()["has_key"] is True and r.json()["hint"].endswith("3456")


def test_job_create_requires_key(monkeypatch, tiny_pdf):
    # a session with no saved key (and no env key) must reject job creation
    monkeypatch.delenv("DS_KEY", raising=False)
    monkeypatch.setattr("ocrtran.pipeline.build_provider", lambda cfg, key=None: FakeProvider())
    with TestClient(app) as c:
        with open(tiny_pdf, "rb") as fh:
            did = c.post("/api/documents", files={"file": ("t.pdf", fh, "application/pdf")}).json()["id"]
        r = c.post(
            f"/api/documents/{did}/jobs",
            json={"target_lang": "French"},
            headers={"X-Session-Id": "nokey-session"},
        )
        assert r.status_code == 400


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
            "verify_math": True,
            "glossary": [{"source": "Prava", "target": "droite"}],
            "llm_instructions": "Keep the tone concise.",
        },
    )
    assert r.status_code == 200, r.text
    job_id = r.json()["id"]

    job = _wait(client, job_id)
    assert job["status"] == "done", job
    assert job["progress"] == 1.0
    assert job["artifacts"], job

    report = client.get(f"/api/jobs/{job_id}/report")
    assert report.status_code == 200
    assert "source" in report.json()
    assert report.json()["source"]["math"]  # verify_math produced per-page checks

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


@pytest.mark.integration
def test_job_output_name(client, tiny_pdf, xelatex_available):
    if not xelatex_available:
        pytest.skip("xelatex not installed")
    doc = _upload(client, tiny_pdf).json()

    named = client.post(
        f"/api/documents/{doc['id']}/jobs", json={"target_lang": "French", "output_name": "My Thesis"}
    ).json()["id"]
    job = _wait(client, named)
    assert job["status"] == "done", job
    assert job["artifacts"][0]["filename"] == "My Thesis.pdf"

    # blank -> default derived from the ORIGINAL file name + target language
    default = client.post(f"/api/documents/{doc['id']}/jobs", json={"target_lang": "French"}).json()["id"]
    job2 = _wait(client, default)
    assert job2["status"] == "done"
    assert job2["artifacts"][0]["filename"] == "tiny (French).pdf"

    # a traversal-y name is neutralised
    sneaky = client.post(
        f"/api/documents/{doc['id']}/jobs",
        json={"target_lang": "French", "output_name": "../../etc/passwd"},
    ).json()["id"]
    job3 = _wait(client, sneaky)
    assert job3["status"] == "done"
    assert job3["artifacts"][0]["filename"] == "passwd.pdf"


@pytest.mark.integration
def test_book_api_flow(client, tiny_pdf, xelatex_available):
    if not xelatex_available:
        pytest.skip("xelatex not installed")
    doc = _upload(client, tiny_pdf).json()
    r = client.post(
        f"/api/documents/{doc['id']}/book",
        json={"target_lang": "French", "font_main": "Noto Serif", "combine": "interleave", "chunk_size": 1},
    )
    assert r.status_code == 200, r.text
    assert r.json()["kind"] == "book"
    job_id = r.json()["id"]

    job = _wait(client, job_id, timeout=180)
    assert job["status"] == "done", job
    chunks = client.get(f"/api/jobs/{job_id}/chunks").json()
    assert [c["state"] for c in chunks] == ["done", "done"]
    assert job["artifacts"]
    assert client.get(job["artifacts"][0]["download_url"]).status_code == 200


@pytest.mark.integration
def test_book_pause_resume_and_status(client, tiny_pdf, xelatex_available):
    if not xelatex_available:
        pytest.skip("xelatex not installed")
    doc = _upload(client, tiny_pdf).json()
    job_id = client.post(
        f"/api/documents/{doc['id']}/book", json={"target_lang": "French", "chunk_size": 1}
    ).json()["id"]

    assert client.post(f"/api/jobs/{job_id}/pause").json()["ok"] is True
    assert client.post(f"/api/jobs/{job_id}/resume").json()["ok"] is True
    job = _wait(client, job_id, timeout=180)
    assert job["status"] == "done", job

    # retrying a finished chunk is a no-op
    chunks = client.get(f"/api/jobs/{job_id}/chunks").json()
    r = client.post(f"/api/jobs/{job_id}/chunks/{chunks[0]['id']}/retry")
    assert r.status_code == 200


def test_estimate_breakdown_and_options(client, tiny_pdf):
    doc = _upload(client, tiny_pdf).json()
    base = client.get(f"/api/documents/{doc['id']}/estimate?model=deepseek-flash").json()
    assert base["pages"] == 2
    assert base["est_cost_usd"] > 0
    assert "ocr_translation" in base["breakdown"]
    assert base["est_cost_low"] <= base["est_cost_usd"] <= base["est_cost_high"]
    assert base["assumptions"]["image_tokens_per_page"] > 0

    # options that add calls/tokens must raise the estimate
    richer = client.get(
        f"/api/documents/{doc['id']}/estimate?model=deepseek-flash"
        "&verify_math=true&figure_mode=judge&glossary_terms=30&target_lang=Chinese"
    ).json()
    assert richer["est_cost_usd"] > base["est_cost_usd"]


def test_pricing_endpoint(client):
    r = client.get("/api/pricing")
    assert r.status_code == 200
    assert any(p["model"] == "deepseek-flash" for p in r.json()["prices"])
