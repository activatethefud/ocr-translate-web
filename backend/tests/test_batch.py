from __future__ import annotations

import time

import fitz
import pytest
from sqlalchemy import select

from app import batch, db, guards, storage
from app.batch import BatchDispatcher
from app.settings import Settings


@pytest.fixture
def env(tmp_path):
    settings = Settings(storage_dir=tmp_path / "store", api_key_env="DS_KEY", batch_concurrency=1)
    settings.ensure_dirs()
    db.init_db(f"sqlite:///{tmp_path / 'batch.db'}")
    return settings


def _make_doc(settings: Settings, doc_id: str, n_pages: int) -> db.Document:
    doc_dir = storage.doc_dir(settings, doc_id)
    doc_dir.mkdir(parents=True, exist_ok=True)
    src = storage.source_path(settings, doc_id)
    d = fitz.open()
    for i in range(n_pages):
        d.new_page(width=200, height=200).insert_text((20, 40), f"page {i + 1}")
    d.save(src)
    d.close()
    s = db.get_session()
    try:
        doc = db.Document(
            id=doc_id,
            filename=f"{doc_id}.pdf",
            sha256=doc_id,
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


def _docs(settings, specs) -> list[db.Document]:
    return [_make_doc(settings, did, n) for did, n in specs]


class _FakeRunner:
    """Runs children immediately; records order + max concurrency; can fail some."""

    def __init__(self, fail_indexes=()):
        self.order: list[int] = []
        self.active = 0
        self.max_active = 0
        self.fail = set(fail_indexes)
        self.cancelled: list[str] = []

    def run_now(self, job_id: str, api_key: str | None) -> None:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        s = db.get_session()
        try:
            child = s.get(db.Job, job_id)
            idx = (child.config or {}).get("_batch_index")
            total = child.total_pages
        finally:
            s.close()
        self.order.append(idx)
        time.sleep(0.03)
        s = db.get_session()
        try:
            child = s.get(db.Job, job_id)
            if idx in self.fail:
                child.status = "failed"
                child.error = "boom"
            else:
                child.status = "done"
                child.cost_usd = 0.1
                child.done_pages = total
                child.progress = 1.0
            child.finished_at = db.utcnow()
            s.commit()
        finally:
            s.close()
        self.active -= 1

    def cancel(self, job_id: str) -> bool:
        self.cancelled.append(job_id)
        return True

    def shutdown(self) -> None:
        pass


def _children(batch_id):
    s = db.get_session()
    try:
        rows = s.execute(select(db.Job).where(db.Job.parent_id == batch_id)).scalars().all()
        return sorted(rows, key=lambda j: (j.config or {}).get("_batch_index", 0))
    finally:
        s.close()


def _create(env, specs, pages="all", **cfg):
    docs = _docs(env, specs)
    s = db.get_session()
    try:
        return batch.create_batch(
            s, env, docs, {"target_lang": "French", "pages": pages, **cfg}, session_id="s"
        ).id
    finally:
        s.close()


def _run_until(disp, bid, timeout=30):
    t0 = time.time()
    while time.time() - t0 < timeout:
        job = db.get_session().get(db.Job, bid)
        if job.status in ("done", "failed", "canceled"):
            return job
        time.sleep(0.03)
    return db.get_session().get(db.Job, bid)


# -- creation --------------------------------------------------------------
def test_create_batch_children_in_order(env):
    bid = _create(env, [("d1", 3), ("d2", 2), ("d3", 4)], pages="1-2")
    s = db.get_session()
    try:
        parent = s.get(db.Job, bid)
        assert parent.kind == "batch"
        assert parent.status == "running"
        assert parent.total_pages == 6  # 2 + 2 + 2 pages selected
        assert parent.config["doc_ids"] == ["d1", "d2", "d3"]
        assert parent.document_id == "d1"
    finally:
        s.close()
    kids = _children(bid)
    assert [k.document_id for k in kids] == ["d1", "d2", "d3"]
    assert [k.kind for k in kids] == ["single", "single", "single"]
    assert [k.parent_id for k in kids] == [bid, bid, bid]
    assert all(k.status == "queued" for k in kids)
    assert [k.total_pages for k in kids] == [2, 2, 2]
    assert [k.config["_batch_index"] for k in kids] == [0, 1, 2]
    # output names are unique and ordered
    assert [k.config["output_name"] for k in kids] == [
        "01 d1 (French)",
        "02 d2 (French)",
        "03 d3 (French)",
    ]


def test_create_batch_single_doc_name(env):
    bid = _create(env, [("solo", 2)], pages="all")
    assert _children(bid)[0].config["output_name"] == "solo (French)"


def test_create_batch_dedupes_and_keeps_order(env):
    docs = _docs(env, [("a", 1), ("b", 1)])
    s = db.get_session()
    try:
        parent = batch.create_batch(
            s, env, [docs[0], docs[1], docs[0]], {"target_lang": "French"}, session_id="s"
        )
        bid = parent.id
    finally:
        s.close()
    # create_batch itself does not dedupe (the endpoint does); this documents that
    assert [k.document_id for k in _children(bid)] == ["a", "b", "a"]


# -- sequential dispatch ---------------------------------------------------
def test_dispatcher_runs_children_sequentially(env, monkeypatch):
    monkeypatch.setenv("DS_KEY", "test")
    bid = _create(env, [("d1", 1), ("d2", 1), ("d3", 1)])
    fake = _FakeRunner()
    disp = BatchDispatcher(env, runner=fake)
    disp.start()
    try:
        job = _run_until(disp, bid)
    finally:
        disp.stop()
    assert job.status == "done"
    assert fake.order == [0, 1, 2]  # strict order
    assert fake.max_active == 1  # never two at once
    assert job.progress == 1.0
    assert abs(job.cost_usd - 0.3) < 1e-9
    assert [c.status for c in _children(bid)] == ["done", "done", "done"]


def test_partial_failure_marks_batch_done_with_error(env, monkeypatch):
    monkeypatch.setenv("DS_KEY", "test")
    bid = _create(env, [("d1", 1), ("d2", 1), ("d3", 1)])
    fake = _FakeRunner(fail_indexes={1})
    disp = BatchDispatcher(env, runner=fake)
    disp.start()
    try:
        job = _run_until(disp, bid)
    finally:
        disp.stop()
    assert job.status == "done"
    assert "d2" in (job.error or "")
    assert [c.status for c in _children(bid)] == ["done", "failed", "done"]


def test_all_failed_marks_batch_failed(env, monkeypatch):
    monkeypatch.setenv("DS_KEY", "test")
    bid = _create(env, [("d1", 1), ("d2", 1)])
    fake = _FakeRunner(fail_indexes={0, 1})
    disp = BatchDispatcher(env, runner=fake)
    disp.start()
    try:
        job = _run_until(disp, bid)
    finally:
        disp.stop()
    assert job.status == "failed"


# -- recovery / cancel -----------------------------------------------------
def test_recover_requeues_running_children(env):
    bid = _create(env, [("d1", 1), ("d2", 1)])
    s = db.get_session()
    try:
        s.execute(select(db.Job).where(db.Job.parent_id == bid)).scalars().first().status = "running"
        s.commit()
    finally:
        s.close()
    disp = BatchDispatcher(env, runner=_FakeRunner())
    disp.recover()
    assert [c.status for c in _children(bid)] == ["queued", "queued"]


def test_cancel_marks_queued_children_canceled(env, monkeypatch):
    monkeypatch.setenv("DS_KEY", "test")
    bid = _create(env, [("d1", 1), ("d2", 1), ("d3", 1)])
    disp = BatchDispatcher(env, runner=_FakeRunner())
    disp.cancel(bid)
    s = db.get_session()
    try:
        assert s.get(db.Job, bid).status == "canceled"
    finally:
        s.close()
    assert [c.status for c in _children(bid)] == ["canceled", "canceled", "canceled"]


# -- guards integration ----------------------------------------------------
def test_children_do_not_count_against_session_limits(env):
    bid = _create(env, [("d1", 1), ("d2", 1)])
    s = db.get_session()
    try:
        jobs = guards.session_jobs(s, "s")
        ids = {j.id for j in jobs}
        assert bid in ids  # the batch parent counts
        assert all(c.id not in ids for c in _children(bid))  # children do not
    finally:
        s.close()
