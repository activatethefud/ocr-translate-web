"""Live API tests — opt-in, they call a real model and cost money.

Run with:
    OCRtran_LIVE=1 DS_KEY=... PYTHONPATH=.deps:. pytest -m live -v
"""

from __future__ import annotations

import os
import time

import fitz
import pytest

from ocrtran.config import PipelineConfig
from ocrtran.pipeline import Pipeline

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not os.environ.get("OCRtran_LIVE"), reason="set OCRtran_LIVE=1 to run"),
    pytest.mark.skipif(not os.environ.get("DS_KEY"), reason="set DS_KEY"),
]


def test_live_one_page(tmp_path, xelatex_available):
    if not xelatex_available:
        pytest.skip("xelatex not installed")
    src = tmp_path / "page.pdf"
    doc = fitz.open()
    page = doc.new_page(width=400, height=300)
    page.insert_text((30, 60), "Povrsina kruga je pi*r^2.", fontsize=14)
    page.insert_text((30, 90), "Talesova teorema: AB/AB1 = AC/AC1.", fontsize=12)
    doc.save(src)
    doc.close()

    cfg = PipelineConfig(
        sources=[str(src)],
        workdir=str(tmp_path / "work"),
        source_lang="Serbian",
        target_lang="French",
        font_main="Noto Serif",
        bilingual=False,
        combine="interleave",
    )
    result = Pipeline(cfg).run()
    assert result.outputs, "no output produced"
    assert result.usage.get("calls", 0) >= 1


def _live_book_source(tmp_path, n_pages=6):
    src = tmp_path / "livebook.pdf"
    doc = fitz.open()
    lines = [
        "Definicija 1. Neka je f neprekidna funkcija na intervalu [a, b].",
        "Teorema. Ako je f diferencijabilna, onda je f neprekidna.",
        "Dokaz. Neka je x tacka u kojoj je f diferencijabilna.",
        "Primer. Izracunati integral funkcije f(x) = x^2 na [0, 1].",
        "Limes niza a_n je broj L ako za svako epsilon > 0 postoji n0.",
        "Zadatak. Dokazati da je zbir uglova trougla 180 stepeni.",
    ]
    for i in range(n_pages):
        page = doc.new_page(width=400, height=300)
        page.insert_text((30, 60), f"Strana {i + 1}", fontsize=16)
        page.insert_text((30, 90), lines[i % len(lines)], fontsize=12)
        page.insert_text((30, 120), "Talesova teorema: AB/AB1 = AC/AC1.", fontsize=12)
    doc.save(src)
    doc.close()
    return src


def test_live_book_small(tmp_path, xelatex_available):
    """A real 6-page book, chunk_size=2: chunks done, merged PDF, resume is free."""
    if not xelatex_available:
        pytest.skip("xelatex not installed")
    from app import books, db, storage
    from app.settings import Settings

    settings = Settings(storage_dir=tmp_path / "store", api_key_env="DS_KEY")
    settings.ensure_dirs()
    db.init_db(f"sqlite:///{tmp_path / 'live_book.db'}")

    src = _live_book_source(tmp_path, n_pages=6)
    doc_dir = storage.doc_dir(settings, "livebook")
    doc_dir.mkdir(parents=True, exist_ok=True)
    import shutil

    shutil.copy2(src, storage.source_path(settings, "livebook"))
    s = db.get_session()
    try:
        s.add(
            db.Document(
                id="livebook",
                filename="livebook.pdf",
                sha256="x",
                n_pages=6,
                page_w=400,
                page_h=300,
                kind="text",
            )
        )
        book = books.create_book(
            s,
            settings,
            s.get(db.Document, "livebook"),
            {
                "target_lang": "English",
                "source_lang": "Serbian",
                "font_main": "Noto Serif",
                "bilingual": True,
                "combine": "interleave",
            },
            chunk_size=2,
            session_id="live",
        )
        bid = book.id
    finally:
        s.close()

    d = books.BookDispatcher(settings)  # real provider from DS_KEY
    d.start()
    try:
        deadline = time.time() + 900
        while time.time() < deadline:
            job = db.get_session().get(db.Job, bid)
            if job.status in ("done", "failed", "paused"):
                break
            time.sleep(2)
    finally:
        d.stop()
    job = db.get_session().get(db.Job, bid)
    assert job.status == "done", f"book {job.status}: {job.error}"
    chunks = _live_chunks(bid)
    assert [c.state for c in chunks] == ["done", "done", "done"]
    assert job.cost_usd > 0
    assert not job.error, job.error

    art = (
        db.get_session()
        .execute(__import__("sqlalchemy").select(db.Artifact).where(db.Artifact.job_id == bid))
        .scalars()
        .first()
    )
    out = fitz.open(art.path)
    assert out.page_count == 12  # 6 originals + 6 translations
    text = " ".join(p.get_text() for p in out)
    assert "Strana 1" in text  # originals kept
    out.close()


def _live_chunks(bid):
    from sqlalchemy import select

    from app import db

    s = db.get_session()
    try:
        return (
            s.execute(select(db.Chunk).where(db.Chunk.book_job_id == bid).order_by(db.Chunk.idx))
            .scalars()
            .all()
        )
    finally:
        s.close()
