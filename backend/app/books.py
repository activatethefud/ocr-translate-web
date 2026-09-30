"""Durable book translation: plan chunks, dispatch them, merge the result.

A *book job* owns contiguous *chunks* (page ranges). A background dispatcher
(thread) pulls queued chunks from SQLite, runs the normal page pipeline for each
chunk into a shared book workdir, and finally assembles the whole document once.
State lives in the DB, so a restart resumes: orphaned ``running`` chunks are reset
to ``queued`` and finished pages are reused from the document cache.
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields
from pathlib import Path

from sqlalchemy import select

from ocrtran import annotate, cache, latex, ocr, paths, pricing
from ocrtran import assemble as assemble_mod
from ocrtran.config import PipelineConfig
from ocrtran.jsonutil import parse_json_list
from ocrtran.providers import OpenAICompatibleProvider

from . import db, storage
from .secrets import get_cipher
from .settings import Settings

log = logging.getLogger("app.books")

_ENGINE_KEYS = {f.name for f in fields(PipelineConfig)}


def _engine_filter(data: dict) -> dict:
    """Drop app-only keys (session_id, chunk_size, from_page, ...) before from_dict."""
    return {k: v for k, v in data.items() if k in _ENGINE_KEYS}


GLOSSARY_PROMPT = """From this parallel {src}->{tgt} text, extract up to 20 important
recurring terms, proper nouns and technical phrases as a glossary. Use the exact
target wording that was used. Return strict JSON: [{{"source":"...","target":"..."}}]
Only include term pairs you are confident about. Return only JSON.

{text}
"""


def plan_chunks(
    n_pages: int, chunk_size: int, page_from: int = 1, page_to: int | None = None
) -> list[tuple[int, int, int]]:
    """Return ``[(idx, from, to), ...]`` covering the requested page range."""
    first = max(1, int(page_from or 1))
    last = min(int(page_to or n_pages), n_pages)
    size = max(1, int(chunk_size or 25))
    out: list[tuple[int, int, int]] = []
    idx, p = 0, first
    while p <= last:
        end = min(p + size - 1, last)
        out.append((idx, p, end))
        idx += 1
        p = end + 1
    return out


def create_book(
    s,
    settings: Settings,
    doc: db.Document,
    config: dict,
    *,
    chunk_size: int,
    page_from: int = 1,
    page_to: int | None = None,
    session_id: str = "default",
) -> db.Job:
    """Create a durable book job + its queued chunks (caller commits)."""
    chunks = plan_chunks(doc.n_pages, chunk_size, page_from, page_to)
    total = sum(b - a + 1 for _i, a, b in chunks)
    cfg = dict(config)
    cfg["chunk_size"] = chunk_size
    cfg["session_id"] = session_id
    mode = "translated_only" if not cfg.get("bilingual", True) else cfg.get("combine", "interleave")
    book = db.Job(
        document_id=doc.id,
        config=cfg,
        status="running",
        kind="book",
        model=cfg.get("model") or settings.default_model,
        source_lang=cfg.get("source_lang", "auto"),
        target_lang=cfg.get("target_lang", ""),
        mode=mode,
        total_pages=total,
        progress=0.0,
    )
    s.add(book)
    s.flush()
    for idx, a, b in chunks:
        s.add(db.Chunk(book_job_id=book.id, idx=idx, page_from=a, page_to=b))
    s.commit()
    return book


class BookDispatcher:
    """Durable scheduler: dispatches chunks, pauses on budget, resumes on restart."""

    def __init__(self, settings: Settings, provider_factory=None) -> None:
        self.settings = settings
        self._make_provider = provider_factory or _default_provider
        self._executor = ThreadPoolExecutor(max_workers=max(1, settings.book_chunk_concurrency))
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    # -- lifecycle -----------------------------------------------------
    def start(self) -> None:
        self.recover()
        self._thread = threading.Thread(target=self._loop, name="book-dispatcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self._executor.shutdown(wait=False, cancel_futures=True)

    def wake(self) -> None:
        self._wake.set()

    def recover(self) -> None:
        """Reset chunks orphaned by a restart so they run again."""
        s = db.get_session()
        try:
            for chunk in s.execute(select(db.Chunk).where(db.Chunk.state == "running")).scalars():
                chunk.state = "queued"
                chunk.started_at = None
            s.commit()
        finally:
            s.close()

    # -- scheduler loop ------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - keep the dispatcher alive
                log.exception("dispatcher tick failed")
            self._wake.wait(timeout=2.0)
            self._wake.clear()

    def _inflight(self, s) -> int:
        return len(s.execute(select(db.Chunk).where(db.Chunk.state == "running")).scalars().all())

    def tick(self) -> None:
        s = db.get_session()
        try:
            books = (
                s.execute(select(db.Job).where(db.Job.kind == "book", db.Job.status == "running"))
                .scalars()
                .all()
            )
            for book in books:
                chunks = s.execute(select(db.Chunk).where(db.Chunk.book_job_id == book.id)).scalars().all()
                if not chunks:
                    continue
                spend = sum(c.cost_usd for c in chunks)
                if spend >= self.settings.max_book_usd:
                    book.status = "paused"
                    s.commit()
                    _emit(book.id, "paused", "budget reached", {"spend": spend})
                    continue
                if all(c.state in ("done", "failed") for c in chunks):
                    book.status = "finalizing"
                    s.commit()
                    self._executor.submit(self._finalize, book.id)
                    continue
                free = self.settings.book_chunk_concurrency - self._inflight(s)
                queued = sorted((c for c in chunks if c.state == "queued"), key=lambda c: c.idx)
                for chunk in queued[: max(0, free)]:
                    chunk.state = "running"
                    chunk.started_at = db.utcnow()
                    chunk.attempts += 1
                    s.commit()
                    self._executor.submit(self._run_chunk, book.id, chunk.id)
                done_pages = sum(c.page_to - c.page_from + 1 for c in chunks if c.state == "done")
                book.progress = min(0.99, done_pages / max(1, book.total_pages))
                s.commit()
        finally:
            s.close()

    # -- chunk execution ----------------------------------------------
    def _run_chunk(self, book_id: str, chunk_id: str) -> None:
        s = db.get_session()
        try:
            book = s.get(db.Job, book_id)
            chunk = s.get(db.Chunk, chunk_id)
            doc = s.get(db.Document, book.document_id)
            book_config, doc_id, chunk_range = dict(book.config), doc.id, (chunk.page_from, chunk.page_to)
        finally:
            s.close()
        _emit(book_id, "chunk_started", f"pages {chunk_range[0]}-{chunk_range[1]}", {"chunk_id": chunk_id})
        try:
            cfg = self._chunk_config(book_config, doc_id, book_id, chunk_id, chunk_range)
            provider = self._make_provider(book_config, self._api_key(book_config), self.settings)
            calls_before = (getattr(provider, "usage", {}) or {}).get("calls", 0)
            results = ocr.run_ocr(cfg, provider)
            annotate.run_annotate(cfg, provider, results)
            latex.run_build(cfg, results)
            self._collect_pages(cfg, book_config, doc_id, book_id, chunk_range)
            usage = getattr(provider, "usage", {}) or {}
            cost = pricing.cost_usd(
                cfg.model, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)
            )
            # only mine the glossary from chunks that actually translated new pages
            if self.settings.rolling_glossary and usage.get("calls", 0) > calls_before:
                self._update_glossary(book_id, book_config, provider, results)
            self._finish_chunk(chunk_id, cost)
            _emit(
                book_id,
                "chunk_done",
                f"pages {chunk_range[0]}-{chunk_range[1]}",
                {"chunk_id": chunk_id, "cost": cost},
            )
        except Exception as exc:  # noqa: BLE001 - retried / recorded per chunk
            log.exception("chunk %s failed", chunk_id)
            self._fail_chunk(chunk_id, str(exc))
        finally:
            self.wake()

    def _api_key(self, book_config: dict) -> str | None:
        sid = book_config.get("session_id") or "default"
        s = db.get_session()
        try:
            row = s.get(db.Session, sid)
            if row and row.api_key_enc:
                return get_cipher(self.settings).decrypt(row.api_key_enc)
        finally:
            s.close()
        return os.environ.get(self.settings.api_key_env)

    def _chunk_config(
        self, book_config: dict, doc_id: str, book_id: str, chunk_id: str, chunk_range: tuple[int, int]
    ) -> PipelineConfig:
        base = storage.source_path(self.settings, doc_id)
        data = _engine_filter({k: v for k, v in book_config.items() if v is not None})
        data.update(
            {
                "sources": [str(base)],
                "workdir": str(storage.work_dir(self.settings, doc_id, book_id) / "chunks" / chunk_id),
                "cache_dir": str(storage.doc_dir(self.settings, doc_id) / "cache"),
                "pages": f"{chunk_range[0]}-{chunk_range[1]}",
                "api_key_env": self.settings.api_key_env,
            }
        )
        return PipelineConfig.from_dict(data)

    def _collect_pages(
        self, cfg: PipelineConfig, book_config: dict, doc_id: str, book_id: str, chunk_range: tuple[int, int]
    ) -> None:
        """Copy the chunk's built per-page PDFs into the shared book tex dir."""
        book_tex = storage.work_dir(self.settings, doc_id, book_id) / "source" / "tex"
        book_tex.mkdir(parents=True, exist_ok=True)
        for page in range(chunk_range[0], chunk_range[1] + 1):
            src = paths.page_pdf(cfg.workdir, "source", page)
            if src.exists():
                shutil.copy2(src, book_tex / f"p{page:02d}.pdf")
            figdir = src.parent
            for fig in figdir.glob(f"fig_{page}_*.png"):
                shutil.copy2(fig, book_tex / fig.name)

    def _update_glossary(self, book_id: str, book_config: dict, provider, results: dict) -> None:
        pairs = extract_glossary(
            provider,
            results,
            book_config.get("source_lang", "auto"),
            book_config.get("target_lang", "English"),
        )
        if not pairs:
            return
        s = db.get_session()
        try:
            book = s.get(db.Job, book_id)
            cfg = dict(book.config)
            glossary = list(cfg.get("auto_glossary") or [])
            seen = {g.get("source") for g in glossary}
            for pair in pairs:
                if pair["source"] not in seen:
                    glossary.append(pair)
                    seen.add(pair["source"])
            cfg["auto_glossary"] = glossary[:400]
            book.config = cfg
            s.commit()
        finally:
            s.close()

    def _finish_chunk(self, chunk_id: str, cost: float) -> None:
        s = db.get_session()
        try:
            chunk = s.get(db.Chunk, chunk_id)
            chunk.state = "done"
            chunk.cost_usd = cost
            chunk.error = None
            chunk.finished_at = db.utcnow()
            s.commit()
        finally:
            s.close()

    def _fail_chunk(self, chunk_id: str, error: str) -> None:
        s = db.get_session()
        try:
            chunk = s.get(db.Chunk, chunk_id)
            chunk.error = error[:1000]
            if chunk.attempts < chunk.max_attempts:
                chunk.state = "queued"  # will be retried
            else:
                chunk.state = "failed"
                chunk.finished_at = db.utcnow()
            s.commit()
        finally:
            s.close()

    # -- final merge ---------------------------------------------------
    def _finalize(self, book_id: str) -> None:
        s = db.get_session()
        try:
            book = s.get(db.Job, book_id)
            doc = s.get(db.Document, book.document_id)
            book_config, doc_id = dict(book.config), doc.id
            chunks = s.execute(select(db.Chunk).where(db.Chunk.book_job_id == book_id)).scalars().all()
            cost = sum(c.cost_usd for c in chunks)
        finally:
            s.close()
        if chunks:
            first, last = min(c.page_from for c in chunks), max(c.page_to for c in chunks)
        else:
            first, last = 1, cfg_total_pages(book_config, doc_id, self.settings)
        try:
            cfg = self._final_config(book_config, doc_id, book_id, first, last)
            outputs = assemble_mod.assemble(cfg)
            book_tex = storage.work_dir(self.settings, doc_id, book_id) / "source" / "tex"
            missing = [p for p in range(first, last + 1) if not (book_tex / f"p{p:02d}.pdf").exists()]
            name = (book_config.get("output_name") or "").strip() or "book"
            for _base, path in outputs.items():
                dest = storage.copy_artifact(
                    self.settings,
                    book_id,
                    path,
                    "output",
                    dest_name=storage.safe_filename(name, fallback=Path(path).name, ext=".pdf"),
                )
                s = db.get_session()
                try:
                    s.add(db.Artifact(job_id=book_id, kind="book", path=str(dest), bytes=dest.stat().st_size))
                    s.commit()
                finally:
                    s.close()
            report = {"missing_pages": missing, "cost_usd": round(cost, 4), "chunks": len(chunks)}
            rdir = storage.artifact_dir(self.settings, book_id)
            rdir.mkdir(parents=True, exist_ok=True)
            cache.save_json(rdir / "report.json", report)
            s = db.get_session()
            try:
                b = s.get(db.Job, book_id)
                b.status = "done"
                b.progress = 1.0
                b.cost_usd = cost
                b.error = (
                    "missing translation for pages: " + ", ".join(map(str, missing)) if missing else None
                )
                b.finished_at = db.utcnow()
                s.commit()
            finally:
                s.close()
            _emit(book_id, "book_done", "assembled", report)
        except Exception as exc:  # noqa: BLE001
            log.exception("finalize failed for book %s", book_id)
            s = db.get_session()
            try:
                b = s.get(db.Job, book_id)
                b.status = "failed"
                b.error = str(exc)
                b.finished_at = db.utcnow()
                s.commit()
            finally:
                s.close()

    def _final_config(
        self, book_config: dict, doc_id: str, book_id: str, first: int = 1, last: int | None = None
    ) -> PipelineConfig:
        data = _engine_filter({k: v for k, v in book_config.items() if v is not None})
        data.update(
            {
                "sources": [str(storage.source_path(self.settings, doc_id))],
                "workdir": str(storage.work_dir(self.settings, doc_id, book_id)),
                "cache_dir": str(storage.doc_dir(self.settings, doc_id) / "cache"),
                "pages": "all" if (first <= 1 and last is None) else f"{first}-{last}",
                "api_key_env": self.settings.api_key_env,
            }
        )
        return PipelineConfig.from_dict(data)


def cfg_total_pages(book_config: dict, doc_id: str, settings: Settings) -> int:
    import fitz

    with fitz.open(storage.source_path(settings, doc_id)) as doc:
        return doc.page_count


def extract_glossary(
    provider, results: dict, source_lang: str, target_lang: str, max_chars: int = 6000
) -> list[dict]:
    """Cheap rolling glossary: term pairs from a chunk's source/target blocks."""
    lines: list[str] = []
    budget = max_chars
    for entries in results.values():
        for entry in entries:
            for b in entry.get("blocks", []):
                src, tgt = b.get("source", "").strip(), b.get("target", "").strip()
                if not src or not tgt or b.get("type") == "figure":
                    continue
                line = f"{src[:120]} || {tgt[:120]}"
                if len(line) + 1 > budget:
                    break
                lines.append(line)
                budget -= len(line) + 1
    if not lines:
        return []
    prompt = GLOSSARY_PROMPT.format(src=source_lang, tgt=target_lang, text="\n".join(lines))
    obj = parse_json_list(provider.text(prompt, 2000))
    if not obj:
        return []
    out = []
    for item in obj:
        if isinstance(item, dict) and item.get("source") and item.get("target"):
            out.append({"source": str(item["source"])[:120], "target": str(item["target"])[:120]})
    return out


def _default_provider(book_config: dict, api_key: str | None, settings: Settings):
    if not api_key:
        raise RuntimeError("no API key for book (session or env)")
    return OpenAICompatibleProvider(
        api_base=book_config.get("api_base") or settings.api_base,
        model=book_config.get("model") or settings.default_model,
        api_key=api_key,
        timeout=book_config.get("timeout") or 600,
        reasoning_effort=book_config.get("reasoning_effort", "none"),
    )


def _emit(book_id: str, kind: str, message: str, data: dict | None = None) -> None:
    s = db.get_session()
    try:
        s.add(db.EventRecord(job_id=book_id, stage="book", status=kind, message=message, data=data or {}))
        s.commit()
    finally:
        s.close()
