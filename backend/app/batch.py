"""Sequential multi-document batches.

A *batch job* (``kind="batch"``) owns child single jobs (``parent_id`` = batch id).
A background dispatcher runs the children **one at a time, in order**, so a list of
documents is translated sequentially. State lives in the DB, so a restart resumes;
child pages reuse the per-document cache, so already-finished documents are free.
"""

from __future__ import annotations

import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields
from pathlib import Path

from sqlalchemy import select

from ocrtran.config import PipelineConfig
from ocrtran.pages import parse_page_spec

from . import books, db, storage
from .runner import JobRunner
from .secrets import get_cipher
from .settings import Settings

log = logging.getLogger("app.batch")

_ENGINE_KEYS = {f.name for f in fields(PipelineConfig)}


def create_batch(
    s,
    settings: Settings,
    docs: list[db.Document],
    config: dict,
    *,
    session_id: str = "default",
    book: bool = False,
    chunk_size: int = 25,
) -> db.Job:
    """Create a batch job + one queued child per document (a single or a book job)."""
    cfgs = [dict(config) for _ in docs]
    for cfg in cfgs:
        cfg["session_id"] = session_id
    # pages covered: whole document for a book child, else the requested range
    if book:
        ranges = [d.n_pages for d in docs]
    else:
        ranges = [len(parse_page_spec(cfgs[i].get("pages"), d.n_pages)) for i, d in enumerate(docs)]
    total = sum(ranges)
    config = {**config, "book": book, "chunk_size": chunk_size}

    parent = db.Job(
        document_id=docs[0].id,
        config={
            **config,
            "session_id": session_id,
            "doc_ids": [d.id for d in docs],
            "output_names": [Path(d.filename).stem for d in docs],
        },
        status="running",
        kind="batch",
        model=config.get("model") or settings.default_model,
        source_lang=config.get("source_lang", "auto"),
        target_lang=config.get("target_lang", ""),
        mode=config.get("mode")
        or ("translated_only" if not config.get("bilingual", True) else config.get("combine", "interleave")),
        total_pages=total,
        progress=0.0,
    )
    s.add(parent)
    s.flush()

    base_name = (config.get("output_name") or "").strip()
    target = config.get("target_lang", "")
    for i, doc in enumerate(docs):
        cfg = cfgs[i]
        cfg["_batch_index"] = i
        stem = Path(doc.filename).stem or f"document-{i + 1}"
        if len(docs) == 1:
            cfg["output_name"] = base_name or f"{stem} ({target})"
        elif base_name:
            cfg["output_name"] = f"{base_name} - {i + 1:02d} {stem}"
        else:
            cfg["output_name"] = f"{i + 1:02d} {stem} ({target})"
        if book:
            cfg["pages"] = "all"
            books.create_book(
                s,
                settings,
                doc,
                cfg,
                chunk_size=chunk_size,
                session_id=session_id,
                parent_id=parent.id,
                status="queued",
            )
        else:
            s.add(
                db.Job(
                    document_id=doc.id,
                    config=cfg,
                    status="queued",
                    kind="single",
                    parent_id=parent.id,
                    model=parent.model,
                    source_lang=parent.source_lang,
                    target_lang=parent.target_lang,
                    mode=parent.mode,
                    total_pages=ranges[i],
                )
            )
    s.commit()
    return parent


class BatchDispatcher:
    """Run a batch's child jobs sequentially, one at a time."""

    def __init__(self, settings: Settings, runner: JobRunner | None = None) -> None:
        self.settings = settings
        self.runner = runner or JobRunner(settings)
        self._own_runner = runner is None
        self._executor = ThreadPoolExecutor(max_workers=max(1, settings.batch_concurrency))
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._inflight: set[str] = set()
        self._thread: threading.Thread | None = None

    # -- lifecycle -----------------------------------------------------
    def start(self) -> None:
        self.recover()
        self._thread = threading.Thread(target=self._loop, name="batch-dispatcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self._executor.shutdown(wait=False, cancel_futures=True)
        if self._own_runner:
            self.runner.shutdown()

    def wake(self) -> None:
        self._wake.set()

    def recover(self) -> None:
        """A restart orphaned running children -> re-queue them (pages come from cache)."""
        s = db.get_session()
        try:
            books = (
                s.execute(select(db.Job).where(db.Job.kind == "batch", db.Job.status == "running"))
                .scalars()
                .all()
            )
            for batch in books:
                for child in self._children(s, batch.id):
                    if child.kind == "book" or child.status == "canceled":
                        continue  # books recover their own chunks
                    if child.status in ("running", "finalizing"):
                        child.status = "queued"
            s.commit()
        finally:
            s.close()

    # -- helpers -------------------------------------------------------
    @staticmethod
    def _children(s, batch_id: str) -> list[db.Job]:
        rows = s.execute(select(db.Job).where(db.Job.parent_id == batch_id)).scalars().all()
        return sorted(rows, key=lambda j: (j.config or {}).get("_batch_index", 0))

    @staticmethod
    def _terminal(job: db.Job) -> bool:
        return job.status in ("done", "failed", "canceled")

    def _api_key(self, child: db.Job) -> str | None:
        sid = (child.config or {}).get("session_id") or "default"
        s = db.get_session()
        try:
            row = s.get(db.Session, sid)
            if row and row.api_key_enc:
                return get_cipher(self.settings).decrypt(row.api_key_enc)
        finally:
            s.close()
        return os.environ.get(self.settings.api_key_env)

    # -- scheduler -----------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - keep the dispatcher alive
                log.exception("batch tick failed")
            self._wake.wait(timeout=2.0)
            self._wake.clear()

    def tick(self) -> None:
        s = db.get_session()
        try:
            free = storage.free_mb(self.settings.storage_dir)
            if free < self.settings.min_free_mb:
                for b in (
                    s.execute(select(db.Job).where(db.Job.kind == "batch", db.Job.status == "running"))
                    .scalars()
                    .all()
                ):
                    b.status = "paused"
                    b.error = books.LOW_DISK_MARK
                    s.commit()
                    _emit(b.id, "paused", f"low disk ({free} MB free)", {"free_mb": free})
                return
            for b in (
                s.execute(
                    select(db.Job).where(
                        db.Job.kind == "batch", db.Job.status == "paused", db.Job.error == books.LOW_DISK_MARK
                    )
                )
                .scalars()
                .all()
            ):
                b.status = "running"
                b.error = None
                s.commit()
                _emit(b.id, "resumed", "disk space recovered", {"free_mb": free})
            batches = (
                s.execute(select(db.Job).where(db.Job.kind == "batch", db.Job.status == "running"))
                .scalars()
                .all()
            )
            for batch in batches:
                children = self._children(s, batch.id)
                if not children:
                    continue
                done = sum(1 for c in children if self._terminal(c))
                running = next((c for c in children if c.status in ("running", "finalizing")), None)
                pages = sum(c.done_pages or 0 for c in children)
                if running is not None:
                    batch.done_pages = min(batch.total_pages, pages)
                    batch.progress = min(0.99, pages / max(1, batch.total_pages))
                    s.commit()
                    continue
                if done >= len(children):
                    s.commit()
                    self._executor.submit(self._finalize, batch.id)
                    continue
                with self._lock:
                    capacity = self.settings.batch_concurrency - len(self._inflight)
                    nxt = next((c for c in children if c.status == "queued"), None)
                    if nxt is not None and capacity > 0:
                        nxt.status = "running"
                        nxt.started_at = db.utcnow()
                        self._inflight.add(nxt.id)
                        s.commit()
                        self._executor.submit(self._run_child, batch.id, nxt.id)
                        done += 1
                batch.done_pages = min(batch.total_pages, pages)
                batch.progress = min(0.99, pages / max(1, batch.total_pages))
                s.commit()
        finally:
            s.close()

    def _run_child(self, batch_id: str, child_id: str) -> None:
        s = db.get_session()
        try:
            child = s.get(db.Job, child_id)
            kind = child.kind if child else ""
            key = None if kind == "book" else self._api_key(child)
        except Exception:  # noqa: BLE001
            child, key, kind = None, None, ""
        finally:
            s.close()
        if child is None:
            self._finish_inflight(child_id)
            return
        if kind == "book":
            # the book dispatcher owns the chunks; it is already marked running
            self._finish_inflight(child_id)
            self.wake()
            return
        try:
            if not key:
                raise RuntimeError("no API key for batch (save one for this session or set the env key)")
            self.runner.run_now(child_id, key)
        except Exception as exc:  # noqa: BLE001 - recorded on the child
            log.exception("batch child %s failed", child_id)
            self._fail(child_id, str(exc))
        finally:
            self._finish_inflight(child_id)
            self.wake()

    def _finish_inflight(self, child_id: str) -> None:
        with self._lock:
            self._inflight.discard(child_id)

    def _fail(self, child_id: str, error: str) -> None:
        s = db.get_session()
        try:
            child = s.get(db.Job, child_id)
            if child and not self._terminal(child):
                child.status = "failed"
                child.error = error[:1000]
                child.finished_at = db.utcnow()
                s.commit()
        finally:
            s.close()

    # -- finalize / cancel --------------------------------------------
    def _finalize(self, batch_id: str) -> None:
        s = db.get_session()
        try:
            batch = s.get(db.Job, batch_id)
            if batch is None or batch.status == "canceled":
                return
            children = self._children(s, batch_id)
            if not all(self._terminal(c) for c in children):
                return
            failed = [c for c in children if c.status == "failed"]
            batch.status = "failed" if failed and len(failed) == len(children) else "done"
            batch.progress = 1.0
            batch.cost_usd = sum(c.cost_usd or 0.0 for c in children)
            batch.done_pages = sum(c.done_pages or 0 for c in children)
            batch.finished_at = db.utcnow()
            if failed:
                names = []
                for c in failed:
                    doc = s.get(db.Document, c.document_id)
                    names.append(doc.filename if doc else c.document_id)
                batch.error = "failed documents: " + ", ".join(names)
            else:
                batch.error = None
            # surface every child output at the batch level (download links)
            for a in s.execute(select(db.Artifact).where(db.Artifact.job_id == batch_id)).scalars().all():
                s.delete(a)
            for c in children:
                for a in s.execute(select(db.Artifact).where(db.Artifact.job_id == c.id)).scalars():
                    s.add(db.Artifact(job_id=batch_id, kind=a.kind, path=a.path, bytes=a.bytes))
            s.commit()
        finally:
            s.close()

    def cancel(self, batch_id: str) -> None:
        s = db.get_session()
        try:
            batch = s.get(db.Job, batch_id)
            if batch is None:
                return
            for child in self._children(s, batch_id):
                if child.status == "queued":
                    child.status = "canceled"
                    child.finished_at = db.utcnow()
                elif child.status in ("running", "finalizing"):
                    self.runner.cancel(child.id)
                if child.kind == "book":
                    for ch in s.execute(select(db.Chunk).where(db.Chunk.book_job_id == child.id)).scalars():
                        if ch.state == "queued":
                            ch.state = "canceled"
            batch.status = "canceled"
            batch.finished_at = db.utcnow()
            s.commit()
        finally:
            s.close()
        self.wake()


def _emit(batch_id: str, kind: str, message: str, data: dict | None = None) -> None:
    s = db.get_session()
    try:
        s.add(db.EventRecord(job_id=batch_id, stage="batch", status=kind, message=message, data=data or {}))
        s.commit()
    finally:
        s.close()
