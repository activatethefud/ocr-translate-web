from __future__ import annotations

import time

import fitz
import pytest
from sqlalchemy import select

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


# ==========================================================================
# Thorough book-machinery tests
# ==========================================================================
class _RecExec:
    """Executor that records submissions instead of running them."""

    def __init__(self):
        self.submitted: list[tuple[str, tuple]] = []

    def submit(self, fn, *a, **k):
        self.submitted.append((getattr(fn, "__name__", str(fn)), a))

    def shutdown(self, **k):
        pass


def _chunks(bid: str):
    s = db.get_session()
    try:
        return (
            s.execute(select(db.Chunk).where(db.Chunk.book_job_id == bid).order_by(db.Chunk.idx))
            .scalars()
            .all()
        )
    finally:
        s.close()


def _new_book(env, n_pages=5, chunk_size=1, *, page_from=1, page_to=None, **cfg) -> str:
    _make_doc(env, n_pages)
    s = db.get_session()
    try:
        doc = s.get(db.Document, "docbook")
        book = books.create_book(
            s,
            env,
            doc,
            {"target_lang": "French", **cfg},
            chunk_size=chunk_size,
            page_from=page_from,
            page_to=page_to,
        )
        return book.id
    finally:
        s.close()


def _run_all(d, bid):
    for c in _chunks(bid):
        d._run_chunk(bid, c.id)


# -- scheduler -------------------------------------------------------------
def test_tick_respects_concurrency_cap(env):
    env.book_chunk_concurrency = 1
    bid = _new_book(env, n_pages=3, chunk_size=1)
    d = _dispatcher(env)
    d._executor = _RecExec()
    d.tick()
    assert len(d._executor.submitted) == 1
    assert [c.state for c in _chunks(bid)] == ["running", "queued", "queued"]


def test_tick_no_double_dispatch(env):
    env.book_chunk_concurrency = 1
    bid = _new_book(env, n_pages=3, chunk_size=1)
    s = db.get_session()
    try:
        first = s.execute(select(db.Chunk).where(db.Chunk.book_job_id == bid)).scalars().first()
        first.state = "running"
        s.commit()
    finally:
        s.close()
    d = _dispatcher(env)
    d._executor = _RecExec()
    d.tick()
    assert d._executor.submitted == []
    assert [c.state for c in _chunks(bid)] == ["running", "queued", "queued"]


def test_budget_pause_then_resume(env):
    env.max_book_usd = 0.001
    bid = _new_book(env, n_pages=2, chunk_size=1)
    s = db.get_session()
    try:
        c = s.execute(select(db.Chunk).where(db.Chunk.book_job_id == bid)).scalars().first()
        c.cost_usd = 1.0
        s.commit()
    finally:
        s.close()
    d = _dispatcher(env)
    d._executor = _RecExec()
    d.tick()
    assert db.get_session().get(db.Job, bid).status == "paused"

    env.max_book_usd = 100.0
    s = db.get_session()
    try:
        s.get(db.Job, bid).status = "running"
        s.commit()
    finally:
        s.close()
    d.tick()
    assert len(d._executor.submitted) >= 1  # dispatching resumed


def test_fail_chunk_retries_until_max(env):
    bid = _new_book(env, n_pages=1, chunk_size=1)
    cid = _chunks(bid)[0].id
    d = _dispatcher(env)
    d._fail_chunk(cid, "boom")
    assert _chunks(bid)[0].state == "queued"  # attempts 0 < 3 -> retried
    s = db.get_session()
    try:
        s.get(db.Chunk, cid).attempts = 3  # exhausted
        s.commit()
    finally:
        s.close()
    d._fail_chunk(cid, "boom again")
    c = _chunks(bid)[0]
    assert c.state == "failed"
    assert "boom again" in (c.error or "")


def test_progress_tracks_done_pages(env):
    bid = _new_book(env, n_pages=4, chunk_size=2)  # 2 chunks x 2 pages
    s = db.get_session()
    try:
        zero = (
            s.execute(select(db.Chunk).where(db.Chunk.book_job_id == bid).order_by(db.Chunk.idx))
            .scalars()
            .first()
        )
        zero.state = "done"
        s.commit()
    finally:
        s.close()
    d = _dispatcher(env)
    d._executor = _RecExec()
    d.tick()
    assert abs(db.get_session().get(db.Job, bid).progress - 0.5) < 1e-6


def test_start_recovers_orphans_and_finalizes(env):
    bid = _new_book(env, n_pages=2, chunk_size=1)
    # orphan a running chunk (as if the server had died mid-chunk)
    s = db.get_session()
    try:
        s.execute(select(db.Chunk).where(db.Chunk.book_job_id == bid)).scalars().first().state = "running"
        s.commit()
    finally:
        s.close()

    d = _dispatcher(env)
    seen: list[str] = []
    d._run_chunk = lambda b, c: (seen.append(c), d._finish_chunk(c, 0.0))
    d._finalize = lambda b: seen.append("final:" + b)
    d.start()
    try:
        for _ in range(120):
            if "final:" + bid in seen:
                break
            time.sleep(0.05)
    finally:
        d.stop()
    assert "final:" + bid in seen
    assert all(c.state == "done" for c in _chunks(bid))


def test_tick_paused_not_dispatched(env):
    bid = _new_book(env, n_pages=1, chunk_size=1)
    s = db.get_session()
    try:
        s.get(db.Job, bid).status = "paused"
        s.commit()
    finally:
        s.close()
    d = _dispatcher(env)
    d._executor = _RecExec()
    d.tick()
    assert d._executor.submitted == []
    assert _chunks(bid)[0].state == "queued"


# -- rolling glossary ------------------------------------------------------
class _GlossaryTextProvider:
    def __init__(self, payload):
        self.payload = payload

    def text(self, prompt, max_tokens=None):
        return self.payload

    def vision(self, *a, **k):
        return "{}"


def _results(src="a", tgt="b"):
    return {"d": [{"page": 1, "blocks": [{"type": "prose", "source": src, "target": tgt}]}]}


def test_glossary_accumulates_and_dedupes(env):
    bid = _new_book(env, n_pages=2, chunk_size=1)
    d = _dispatcher(env)
    d._update_glossary(bid, {}, _GlossaryTextProvider('[{"source":"a","target":"b"}]'), _results())
    d._update_glossary(
        bid,
        {},
        _GlossaryTextProvider('[{"source":"a","target":"b"},{"source":"c","target":"d"}]'),
        _results(),
    )
    s = db.get_session()
    try:
        glossary = s.get(db.Job, bid).config["auto_glossary"]
    finally:
        s.close()
    assert [g["source"] for g in glossary] == ["a", "c"]  # deduped, order kept


def test_glossary_empty_is_noop(env):
    bid = _new_book(env, n_pages=1, chunk_size=1)
    d = _dispatcher(env)
    d._update_glossary(bid, {}, _GlossaryTextProvider("[]"), _results())
    s = db.get_session()
    try:
        assert not (s.get(db.Job, bid).config or {}).get("auto_glossary")
    finally:
        s.close()


def test_create_book_translated_only_mode(env):
    bid = _new_book(env, n_pages=2, chunk_size=2, combine="translated_only")
    assert db.get_session().get(db.Job, bid).mode == "translated_only"


def test_collect_pages_copies_pages_and_figures(env, tmp_path):
    from ocrtran import paths
    from ocrtran.config import PipelineConfig

    bid = _new_book(env, n_pages=1, chunk_size=1)
    cfg = PipelineConfig(sources=["dummy.pdf"], workdir=str(tmp_path / "chunk"))
    p = paths.page_pdf(cfg.workdir, "source", 1)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"%PDF-1.4")
    (p.parent / "fig_1_0.png").write_bytes(b"png")
    _dispatcher(env)._collect_pages(cfg, {}, "docbook", bid, (1, 1))
    book_tex = storage.work_dir(env, "docbook", bid) / "source" / "tex"
    assert (book_tex / "p01.pdf").exists()
    assert (book_tex / "fig_1_0.png").exists()


# -- finalize: naming, cost, subset range ----------------------------------
@pytest.mark.integration
def test_finalize_names_output_and_sums_cost(env, xelatex_available):
    if not xelatex_available:
        pytest.skip("xelatex not installed")
    bid = _new_book(
        env,
        n_pages=2,
        chunk_size=1,
        font_main="Noto Serif",
        bilingual=True,
        combine="interleave",
        output_name="My Great Book",
    )
    d = _dispatcher(env)
    _run_all(d, bid)
    s = db.get_session()
    try:
        for c in s.execute(select(db.Chunk).where(db.Chunk.book_job_id == bid)).scalars():
            c.cost_usd = 0.25
        s.commit()
    finally:
        s.close()
    d._finalize(bid)
    s = db.get_session()
    try:
        job = s.get(db.Job, bid)
        assert job.status == "done"
        assert abs(job.cost_usd - 0.5) < 1e-9  # 2 chunks x 0.25
        art = s.execute(select(db.Artifact).where(db.Artifact.job_id == bid)).scalars().first()
        assert art.path.rsplit("/", 1)[-1].startswith("My Great Book")
    finally:
        s.close()


@pytest.mark.integration
def test_finalize_subset_range_only(env, xelatex_available):
    if not xelatex_available:
        pytest.skip("xelatex not installed")
    bid = _new_book(
        env,
        n_pages=5,
        chunk_size=1,
        page_from=2,
        page_to=3,
        font_main="Noto Serif",
        bilingual=True,
        combine="interleave",
    )
    d = _dispatcher(env)
    _run_all(d, bid)
    d._finalize(bid)
    s = db.get_session()
    try:
        job = s.get(db.Job, bid)
        art = s.execute(select(db.Artifact).where(db.Artifact.job_id == bid)).scalars().first()
        out = fitz.open(art.path)
        assert out.page_count == 4  # pages 2 and 3 only: original + translation each
        out.close()
        assert not job.error  # pages outside the book range are not "missing"
    finally:
        s.close()


# -- live progress: per-page done_pages ------------------------------------
def test_finish_chunk_sets_done_pages(env):
    bid = _new_book(env, n_pages=3, chunk_size=2)  # chunk 0 = pages 1-2
    cid = _chunks(bid)[0].id
    _dispatcher(env)._finish_chunk(cid, 0.0)
    c = _chunks(bid)[0]
    assert c.state == "done" and c.done_pages == 2


def test_set_chunk_pages_updates_progress(env):
    bid = _new_book(env, n_pages=4, chunk_size=2)  # 2 chunks of 2 pages
    cid = _chunks(bid)[0].id
    d = _dispatcher(env)
    d._executor = _RecExec()
    d._set_chunk_pages(cid, 1)  # one page of the first chunk is done
    d.tick()
    job = db.get_session().get(db.Job, bid)
    assert job.done_pages == 1
    assert abs(job.progress - 0.25) < 1e-6


def test_tick_progress_uses_partial_chunks(env):
    bid = _new_book(env, n_pages=4, chunk_size=2)
    s = db.get_session()
    try:
        for c in s.execute(select(db.Chunk).where(db.Chunk.book_job_id == bid)).scalars():
            c.done_pages = 1
        s.commit()
    finally:
        s.close()
    d = _dispatcher(env)
    d._executor = _RecExec()
    d.tick()
    job = db.get_session().get(db.Job, bid)
    assert job.done_pages == 2
    assert abs(job.progress - 0.5) < 1e-6
