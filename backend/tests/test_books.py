from __future__ import annotations

import fitz
import pytest

from app import books, db, storage
from app.books import BookDispatcher, plan_chunks
from app.settings import Settings
from tests.conftest import FakeProvider


# -- planner ---------------------------------------------------------------
def test_plan_chunks_basic():
    assert plan_chunks(60, 25) == [(0, 1, 25), (1, 26, 50), (2, 51, 60)]


def test_plan_chunks_exact():
    assert plan_chunks(50, 25) == [(0, 1, 25), (1, 26, 50)]


def test_plan_chunks_single():
    assert plan_chunks(1, 25) == [(0, 1, 1)]
    assert plan_chunks(3, 99) == [(0, 1, 3)]


def test_plan_chunks_range():
    assert plan_chunks(100, 10, page_from=31, page_to=45) == [(0, 31, 40), (1, 41, 45)]


# -- fixtures --------------------------------------------------------------
@pytest.fixture
def env(tmp_path):
    settings = Settings(storage_dir=tmp_path / "store")
    settings.ensure_dirs()
    db.init_db(f"sqlite:///{tmp_path / 'book.db'}")
    return settings


def _make_doc(settings: Settings, n_pages: int = 3) -> db.Document:
    doc_dir = storage.doc_dir(settings, "docbook")
    doc_dir.mkdir(parents=True, exist_ok=True)
    src = storage.source_path(settings, "docbook")
    d = fitz.open()
    for i in range(n_pages):
        d.new_page(width=200, height=200).insert_text((20, 40), f"page {i + 1}")
    d.save(src)
    d.close()
    s = db.get_session()
    try:
        doc = db.Document(
            id="docbook",
            filename="mybook.pdf",
            sha256="x",
            n_pages=n_pages,
            page_w=200,
            page_h=200,
            kind="text",
        )
        s.add(doc)
        s.commit()
        return doc
    finally:
        s.close()


def _dispatcher(settings):
    return BookDispatcher(settings, provider_factory=lambda cfg, key, st: FakeProvider())


# -- create book -----------------------------------------------------------
def test_create_book_makes_chunks(env):
    _make_doc(env, 3)
    s = db.get_session()
    try:
        doc = s.get(db.Document, "docbook")
        book = books.create_book(s, env, doc, {"target_lang": "French", "bilingual": True}, chunk_size=2)
        chunks = (
            s.execute(__import__("sqlalchemy").select(db.Chunk).where(db.Chunk.book_job_id == book.id))
            .scalars()
            .all()
        )
    finally:
        s.close()
    assert book.kind == "book"
    assert book.total_pages == 3
    assert [(c.page_from, c.page_to) for c in chunks] == [(1, 2), (3, 3)]
    assert all(c.state == "queued" for c in chunks)


def test_recover_resets_running_chunks(env):
    _make_doc(env, 2)
    s = db.get_session()
    try:
        doc = s.get(db.Document, "docbook")
        book = books.create_book(s, env, doc, {"target_lang": "French"}, chunk_size=2)
        chunk = (
            s.execute(__import__("sqlalchemy").select(db.Chunk).where(db.Chunk.book_job_id == book.id))
            .scalars()
            .first()
        )
        chunk.state = "running"
        s.commit()
        cid = chunk.id
    finally:
        s.close()
    _dispatcher(env).recover()
    s = db.get_session()
    try:
        assert s.get(db.Chunk, cid).state == "queued"
    finally:
        s.close()


def test_tick_pauses_on_budget(env):
    _make_doc(env, 2)
    env.max_book_usd = 0.001
    s = db.get_session()
    try:
        doc = s.get(db.Document, "docbook")
        book = books.create_book(s, env, doc, {"target_lang": "French"}, chunk_size=2)
        chunk = (
            s.execute(__import__("sqlalchemy").select(db.Chunk).where(db.Chunk.book_job_id == book.id))
            .scalars()
            .first()
        )
        chunk.cost_usd = 1.0  # already over budget
        s.commit()
        bid = book.id
    finally:
        s.close()
    d = _dispatcher(env)
    d.tick()
    s = db.get_session()
    try:
        assert s.get(db.Job, bid).status == "paused"
    finally:
        s.close()


# -- glossary extraction ---------------------------------------------------
class _GlossaryProvider:
    def __init__(self, payload):
        self.payload = payload

    def text(self, prompt, max_tokens=None):
        return self.payload

    def vision(self, *a, **k):
        return "{}"


def test_extract_glossary_ok():
    payload = '[{"source":"trougao","target":"triangle"},{"source":"ugao","target":"angle"}]'
    results = {
        "d": [
            {
                "page": 1,
                "blocks": [{"type": "prose", "source": "trougao i ugao", "target": "triangle and angle"}],
            }
        ]
    }
    pairs = books.extract_glossary(_GlossaryProvider(payload), results, "Serbian", "English")
    assert pairs == [{"source": "trougao", "target": "triangle"}, {"source": "ugao", "target": "angle"}]


def test_extract_glossary_bad_json():
    results = {"d": [{"page": 1, "blocks": [{"type": "prose", "source": "a", "target": "b"}]}]}
    assert books.extract_glossary(_GlossaryProvider("nope"), results, "S", "E") == []


def test_extract_glossary_no_text():
    assert books.extract_glossary(_GlossaryProvider("[]"), {"d": []}, "S", "E") == []


# -- end to end (chunks + merge) ------------------------------------------
@pytest.mark.integration
def test_book_chunks_and_merge(env, xelatex_available):
    if not xelatex_available:
        pytest.skip("xelatex not installed")
    _make_doc(env, 3)
    s = db.get_session()
    try:
        doc = s.get(db.Document, "docbook")
        book = books.create_book(
            s,
            env,
            doc,
            {"target_lang": "French", "font_main": "Noto Serif", "bilingual": True, "combine": "interleave"},
            chunk_size=2,
        )
        bid = book.id
        chunk_ids = [
            c.id
            for c in s.execute(
                __import__("sqlalchemy")
                .select(db.Chunk)
                .where(db.Chunk.book_job_id == bid)
                .order_by(db.Chunk.idx)
            )
            .scalars()
            .all()
        ]
    finally:
        s.close()

    d = _dispatcher(env)
    for cid in chunk_ids:
        d._run_chunk(bid, cid)
    s = db.get_session()
    try:
        states = [
            c.state
            for c in s.execute(__import__("sqlalchemy").select(db.Chunk).where(db.Chunk.book_job_id == bid))
            .scalars()
            .all()
        ]
        assert states == ["done", "done"]
    finally:
        s.close()

    d._finalize(bid)
    s = db.get_session()
    try:
        job = s.get(db.Job, bid)
        assert job.status == "done"
        arts = (
            s.execute(__import__("sqlalchemy").select(db.Artifact).where(db.Artifact.job_id == bid))
            .scalars()
            .all()
        )
        assert len(arts) == 1
        out = fitz.open(arts[0].path)
        assert out.page_count == 6  # 3 originals + 3 translations (interleave)
        out.close()
    finally:
        s.close()


@pytest.mark.integration
def test_book_missing_page_keeps_original_with_warning(env, xelatex_available):
    if not xelatex_available:
        pytest.skip("xelatex not installed")
    _make_doc(env, 2)
    s = db.get_session()
    try:
        doc = s.get(db.Document, "docbook")
        book = books.create_book(
            s,
            env,
            doc,
            {"target_lang": "French", "font_main": "Noto Serif", "bilingual": True, "combine": "interleave"},
            chunk_size=1,
        )
        bid = book.id
        chunk_ids = [
            c.id
            for c in s.execute(
                __import__("sqlalchemy")
                .select(db.Chunk)
                .where(db.Chunk.book_job_id == bid)
                .order_by(db.Chunk.idx)
            )
            .scalars()
            .all()
        ]
    finally:
        s.close()

    d = _dispatcher(env)
    for cid in chunk_ids:
        d._run_chunk(bid, cid)
    # simulate a missing translation for page 2
    book_tex = storage.work_dir(env, "docbook", bid) / "source" / "tex"
    (book_tex / "p02.pdf").unlink(missing_ok=True)

    d._finalize(bid)
    s = db.get_session()
    try:
        job = s.get(db.Job, bid)
        assert "missing" in (job.error or "")
        assert "2" in (job.error or "")
        art = (
            s.execute(__import__("sqlalchemy").select(db.Artifact).where(db.Artifact.job_id == bid))
            .scalars()
            .first()
        )
        out = fitz.open(art.path)
        assert out.page_count == 3  # p1 orig+tx, p2 original only (kept once)
        out.close()
    finally:
        s.close()


def test_tick_skips_paused_book(env):
    _make_doc(env, 2)
    s = db.get_session()
    try:
        doc = s.get(db.Document, "docbook")
        book = books.create_book(s, env, doc, {"target_lang": "French"}, chunk_size=1)
        book.status = "paused"
        s.commit()
        bid = book.id
    finally:
        s.close()
    d = _dispatcher(env)
    d.tick()
    s = db.get_session()
    try:
        chunk = (
            s.execute(__import__("sqlalchemy").select(db.Chunk).where(db.Chunk.book_job_id == bid))
            .scalars()
            .first()
        )
        assert chunk.state == "queued"  # not dispatched while paused
    finally:
        s.close()


def test_tick_finalizes_when_chunks_terminal(env):
    _make_doc(env, 2)
    s = db.get_session()
    try:
        doc = s.get(db.Document, "docbook")
        book = books.create_book(s, env, doc, {"target_lang": "French"}, chunk_size=1)
        for c in (
            s.execute(__import__("sqlalchemy").select(db.Chunk).where(db.Chunk.book_job_id == book.id))
            .scalars()
            .all()
        ):
            c.state = "failed"  # permanently failed chunk must not stall the book
        s.commit()
        bid = book.id
    finally:
        s.close()
    d = _dispatcher(env)
    calls = []
    d._finalize = lambda b: calls.append(b)  # do not actually assemble
    d.tick()
    import time

    for _ in range(60):
        if calls:
            break
        time.sleep(0.05)
    assert calls == [bid]


@pytest.mark.integration
def test_book_failed_chunk_keeps_original(env, xelatex_available):
    if not xelatex_available:
        pytest.skip("xelatex not installed")
    _make_doc(env, 2)
    s = db.get_session()
    try:
        doc = s.get(db.Document, "docbook")
        book = books.create_book(
            s,
            env,
            doc,
            {"target_lang": "French", "font_main": "Noto Serif", "bilingual": True, "combine": "interleave"},
            chunk_size=1,
        )
        bid = book.id
        ids = [
            c.id
            for c in s.execute(
                __import__("sqlalchemy")
                .select(db.Chunk)
                .where(db.Chunk.book_job_id == bid)
                .order_by(db.Chunk.idx)
            )
            .scalars()
            .all()
        ]
    finally:
        s.close()
    d = _dispatcher(env)
    d._run_chunk(bid, ids[0])  # page 1 translated
    s = db.get_session()
    try:
        c2 = s.get(db.Chunk, ids[1])
        c2.state = "failed"
        s.commit()
    finally:
        s.close()
    d._finalize(bid)
    s = db.get_session()
    try:
        job = s.get(db.Job, bid)
        art = (
            s.execute(__import__("sqlalchemy").select(db.Artifact).where(db.Artifact.job_id == bid))
            .scalars()
            .first()
        )
        out = fitz.open(art.path)
        assert out.page_count == 3  # p1 orig+tx, p2 original only (kept)
        out.close()
        assert "2" in (job.error or "")
    finally:
        s.close()
