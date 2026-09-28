"""FastAPI application: upload, jobs, SSE progress, pages, artifacts."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from contextlib import asynccontextmanager
from datetime import UTC
from pathlib import Path

from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sse_starlette.sse import EventSourceResponse

from ocrtran import latex, paths
from ocrtran.cache import load_json, save_json
from ocrtran.pricing import estimate_for_pages
from ocrtran.providers import OpenAICompatibleProvider, ProviderError

from . import db, storage
from .runner import JobRunner
from .schemas import (
    BlockOut,
    BlockPatch,
    BlockReorder,
    DocumentOut,
    EstimateOut,
    JobCreate,
    JobOut,
    ModelInfo,
    ModelsResponse,
    OkOut,
    PageOut,
    SessionOut,
    SessionUpdate,
    UsageOut,
)
from .secrets import get_cipher, hint_for
from .settings import Settings, get_settings

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    db.init_db(settings.resolved_database_url())
    app.state.settings = settings
    app.state.runner = JobRunner(settings)
    yield
    app.state.runner.shutdown()


app = FastAPI(title="OCR + Translate", version="0.1.0", lifespan=lifespan)

_cors = [o.strip() for o in os.environ.get("CORS_ORIGINS", "http://localhost:5173").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors or ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _settings_dep() -> Settings:
    return app.state.settings


def _settings() -> Settings:
    return app.state.settings


def _runner() -> JobRunner:
    return app.state.runner


# --------------------------------------------------------------------------
# sessions (BYOK stored server-side, encrypted)
# --------------------------------------------------------------------------
def _session_id(x_session_id: str | None = Header(default=None)) -> str:
    return (x_session_id or "default")[:64]


def _get_session(s, sid: str) -> db.Session:
    row = s.get(db.Session, sid)
    if row is None:
        row = db.Session(id=sid)
        s.add(row)
        s.commit()
    return row


def _stored_key(settings: Settings, sid: str) -> str | None:
    s = db.get_session()
    try:
        row = s.get(db.Session, sid)
        if not row or not row.api_key_enc:
            return None
        return get_cipher(settings).decrypt(row.api_key_enc)
    finally:
        s.close()


def _session_out(settings: Settings, sid: str, row: db.Session) -> SessionOut:
    key = get_cipher(settings).decrypt(row.api_key_enc)
    return SessionOut(id=sid, has_key=bool(key), hint=hint_for(key), api_base=row.api_base)


@app.get("/api/session", response_model=SessionOut)
def get_session_info(settings: Settings = Depends(_settings_dep), sid: str = Depends(_session_id)):
    s = db.get_session()
    try:
        return _session_out(settings, sid, _get_session(s, sid))
    finally:
        s.close()


@app.put("/api/session", response_model=SessionOut)
def update_session(
    body: SessionUpdate, settings: Settings = Depends(_settings_dep), sid: str = Depends(_session_id)
):
    s = db.get_session()
    try:
        row = _get_session(s, sid)
        if body.clear_key or body.api_key == "":
            row.api_key_enc = None
        elif body.api_key:
            row.api_key_enc = get_cipher(settings).encrypt(body.api_key.strip())
        if body.api_base is not None:
            row.api_base = body.api_base or None
        s.commit()
        return _session_out(settings, sid, row)
    finally:
        s.close()


@app.delete("/api/session/key", response_model=SessionOut)
def clear_session_key(settings: Settings = Depends(_settings_dep), sid: str = Depends(_session_id)):
    s = db.get_session()
    try:
        row = _get_session(s, sid)
        row.api_key_enc = None
        s.commit()
        return _session_out(settings, sid, row)
    finally:
        s.close()


# --------------------------------------------------------------------------
# serialization helpers
# --------------------------------------------------------------------------
def _iso(dt) -> str | None:
    """Serialize datetimes as UTC. SQLite drops tzinfo, which made the browser
    parse them as local time (elapsed timers were off by the UTC offset)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat()


def _doc_out(d: db.Document) -> DocumentOut:
    return DocumentOut(
        id=d.id,
        filename=d.filename,
        sha256=d.sha256,
        n_pages=d.n_pages,
        page_w=d.page_w,
        page_h=d.page_h,
        kind=d.kind,
        size_bytes=d.size_bytes,
        created_at=_iso(d.created_at) or "",
    )


def _job_out(s, job: db.Job) -> JobOut:
    arts = [
        {
            "id": a.id,
            "kind": a.kind,
            "bytes": a.bytes,
            "filename": Path(a.path).name,
            "download_url": f"/api/artifacts/{a.id}/download",
        }
        for a in s.execute(select(db.Artifact).where(db.Artifact.job_id == job.id)).scalars()
    ]
    return JobOut(
        id=job.id,
        document_id=job.document_id,
        status=job.status,
        model=job.model,
        source_lang=job.source_lang,
        target_lang=job.target_lang,
        mode=job.mode,
        progress=job.progress,
        total_pages=job.total_pages,
        done_pages=job.done_pages,
        cost_usd=job.cost_usd,
        error=job.error,
        created_at=_iso(job.created_at) or "",
        started_at=_iso(job.started_at),
        finished_at=_iso(job.finished_at),
        artifacts=arts,
    )


def _ocr_path(settings: Settings, job: db.Job) -> Path:
    workdir = storage.work_dir(settings, job.document_id, job.id)
    return paths.ocr_json(workdir, "source")


def _load_pages(settings: Settings, job: db.Job) -> list[dict]:
    return load_json(_ocr_path(settings, job)) or []


# --------------------------------------------------------------------------
# health + docs
# --------------------------------------------------------------------------
@app.get("/api/health")
def health() -> dict:
    return {"ok": True}


@app.post("/api/documents", response_model=DocumentOut)
async def upload_document(file: UploadFile = File(...), settings: Settings = Depends(_settings_dep)):
    doc_id = db.new_id()
    ddir = storage.doc_dir(settings, doc_id)
    ddir.mkdir(parents=True, exist_ok=True)
    tmp = ddir / "upload.tmp"
    limit = settings.max_upload_mb * 1024 * 1024
    size = 0
    with open(tmp, "wb") as out:
        while chunk := await file.read(1 << 20):
            size += len(chunk)
            if size > limit:
                out.close()
                shutil.rmtree(ddir, ignore_errors=True)
                raise HTTPException(413, f"file exceeds {settings.max_upload_mb} MB")
            out.write(chunk)
    try:
        info = storage.finalize_upload(settings, doc_id, tmp, file.filename or "upload.pdf")
    except Exception as exc:  # noqa: BLE001
        shutil.rmtree(ddir, ignore_errors=True)
        raise HTTPException(400, f"invalid or unreadable PDF: {exc}") from exc
    if info["n_pages"] > settings.max_pages:
        shutil.rmtree(ddir, ignore_errors=True)
        raise HTTPException(413, f"document has more than {settings.max_pages} pages")

    s = db.get_session()
    try:
        doc = db.Document(
            id=doc_id,
            filename=info["filename"],
            sha256=info["sha256"],
            n_pages=info["n_pages"],
            page_w=info["page_w"],
            page_h=info["page_h"],
            kind=info["kind"],
            size_bytes=info["size_bytes"],
        )
        s.add(doc)
        s.commit()
        return _doc_out(doc)
    finally:
        s.close()


@app.get("/api/documents", response_model=list[DocumentOut])
def list_documents():
    s = db.get_session()
    try:
        docs = s.execute(select(db.Document).order_by(db.Document.created_at.desc())).scalars()
        return [_doc_out(d) for d in docs]
    finally:
        s.close()


@app.get("/api/documents/{doc_id}", response_model=DocumentOut)
def get_document(doc_id: str):
    s = db.get_session()
    try:
        doc = s.get(db.Document, doc_id)
        if not doc:
            raise HTTPException(404, "document not found")
        return _doc_out(doc)
    finally:
        s.close()


@app.get("/api/documents/{doc_id}/estimate", response_model=EstimateOut)
def estimate_document(
    doc_id: str,
    model: str | None = None,
    verify_math: bool = False,
    settings: Settings = Depends(_settings_dep),
):
    s = db.get_session()
    try:
        doc = s.get(db.Document, doc_id)
        if not doc:
            raise HTTPException(404, "document not found")
        n_pages = doc.n_pages
    finally:
        s.close()
    return EstimateOut(
        **estimate_for_pages(model or settings.default_model, n_pages, verify_math=verify_math)
    )


# --------------------------------------------------------------------------
# jobs
# --------------------------------------------------------------------------
@app.post("/api/documents/{doc_id}/jobs", response_model=JobOut)
def create_job(
    doc_id: str, body: JobCreate, settings: Settings = Depends(_settings_dep), sid: str = Depends(_session_id)
):
    s = db.get_session()
    try:
        doc = s.get(db.Document, doc_id)
        if not doc:
            raise HTTPException(404, "document not found")
        cfg = body.model_dump()
        provided_key = (cfg.pop("api_key", None) or "").strip()
        if provided_key:
            # remember the key for this session (encrypted at rest)
            row = _get_session(s, sid)
            row.api_key_enc = get_cipher(settings).encrypt(provided_key)
            api_key = provided_key
        else:
            row = s.get(db.Session, sid)
            api_key = get_cipher(settings).decrypt(row.api_key_enc) if row else None
        if not api_key:
            api_key = os.environ.get(settings.api_key_env)
        if not api_key:
            raise HTTPException(400, f"no API key: save one for this session or set ${settings.api_key_env}")
        cfg["model"] = body.model or settings.default_model
        job = db.Job(
            document_id=doc_id,
            config=cfg,
            status="queued",
            model=cfg["model"],
            source_lang=body.source_lang,
            target_lang=body.target_lang,
            mode="translated_only" if not body.bilingual else body.combine,
            total_pages=doc.n_pages,
        )
        s.add(job)
        s.commit()
        out = _job_out(s, job)
    finally:
        s.close()
    _runner().submit(out.id, api_key)
    return out


@app.get("/api/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: str):
    s = db.get_session()
    try:
        job = s.get(db.Job, job_id)
        if not job:
            raise HTTPException(404, "job not found")
        return _job_out(s, job)
    finally:
        s.close()


@app.post("/api/jobs/{job_id}/cancel", response_model=OkOut)
def cancel_job(job_id: str):
    s = db.get_session()
    try:
        if not s.get(db.Job, job_id):
            raise HTTPException(404, "job not found")
    finally:
        s.close()
    ok = _runner().cancel(job_id)
    return OkOut(ok=ok, detail="cancel requested" if ok else "job not running")


@app.get("/api/jobs/{job_id}/events")
async def job_events(job_id: str):
    async def gen():
        last = 0
        while True:
            s = db.get_session()
            try:
                if not s.get(db.Job, job_id):
                    yield {"event": "error", "data": "job not found"}
                    return
                rows = (
                    s.execute(
                        select(db.EventRecord)
                        .where(db.EventRecord.job_id == job_id, db.EventRecord.id > last)
                        .order_by(db.EventRecord.id)
                    )
                    .scalars()
                    .all()
                )
                status = s.get(db.Job, job_id).status
                events = [(r.id, r.stage, r.status, r.page, r.message, r.data) for r in rows]
            finally:
                s.close()
            for eid, stage, st, page, msg, data in events:
                last = eid
                yield {
                    "event": "progress",
                    "data": json.dumps(
                        {"stage": stage, "status": st, "page": page, "message": msg, "data": data}
                    ),
                }
            if status in ("done", "failed", "canceled"):
                yield {"event": "end", "data": status}
                return
            await asyncio.sleep(0.4)

    return EventSourceResponse(gen())


# --------------------------------------------------------------------------
# pages (preview + simple block editor)
# --------------------------------------------------------------------------
@app.get("/api/jobs/{job_id}/pages", response_model=list[PageOut])
def job_pages(job_id: str, settings: Settings = Depends(_settings_dep)):
    s = db.get_session()
    try:
        job = s.get(db.Job, job_id)
        if not job:
            raise HTTPException(404, "job not found")
        pages = _load_pages(settings, job)
    finally:
        s.close()
    out: list[PageOut] = []
    for entry in pages:
        blocks = [
            BlockOut(
                idx=i,
                type=b.get("type", ""),
                source=b.get("source", ""),
                target=b.get("target", ""),
                latex=b.get("latex", ""),
                description=b.get("description", ""),
                bbox=b.get("bbox"),
            )
            for i, b in enumerate(entry.get("blocks", []))
        ]
        out.append(
            PageOut(
                page=entry["page"],
                status="done" if blocks else "pending",
                image_url=f"/api/jobs/{job_id}/pages/{entry['page']}/image",
                blocks=blocks,
            )
        )
    return out


@app.get("/api/jobs/{job_id}/pages/{page}/image")
def job_page_image(job_id: str, page: int, settings: Settings = Depends(_settings_dep)):
    s = db.get_session()
    try:
        job = s.get(db.Job, job_id)
        if not job:
            raise HTTPException(404, "job not found")
        doc_id = job.document_id
    finally:
        s.close()
    img = paths.pages_dir(storage.work_dir(settings, doc_id, job_id), "source") / f"p-{page:02d}.png"
    if not img.exists():
        raise HTTPException(404, "page image not ready")
    return FileResponse(str(img), media_type="image/png")


@app.patch("/api/jobs/{job_id}/pages/{page}/blocks/{idx}", response_model=BlockOut)
def patch_block(
    job_id: str, page: int, idx: int, patch: BlockPatch, settings: Settings = Depends(_settings_dep)
):
    s = db.get_session()
    try:
        job = s.get(db.Job, job_id)
        if not job:
            raise HTTPException(404, "job not found")
        ocr_path = _ocr_path(settings, job)
    finally:
        s.close()
    pages = load_json(ocr_path) or []
    entry = next((e for e in pages if e["page"] == page), None)
    if entry is None:
        raise HTTPException(404, "page not found")
    blocks = entry.get("blocks", [])
    if idx < 0 or idx >= len(blocks):
        raise HTTPException(404, "block not found")
    for field, value in patch.model_dump(exclude_none=True).items():
        blocks[idx][field] = value
    save_json(ocr_path, pages)
    b = blocks[idx]
    return BlockOut(
        idx=idx,
        type=b.get("type", ""),
        source=b.get("source", ""),
        target=b.get("target", ""),
        latex=b.get("latex", ""),
        description=b.get("description", ""),
        bbox=b.get("bbox"),
    )


@app.post("/api/jobs/{job_id}/pages/{page}/reorder", response_model=OkOut)
def reorder_blocks(job_id: str, page: int, body: BlockReorder, settings: Settings = Depends(_settings_dep)):
    s = db.get_session()
    try:
        job = s.get(db.Job, job_id)
        if not job:
            raise HTTPException(404, "job not found")
        ocr_path = _ocr_path(settings, job)
    finally:
        s.close()
    pages = load_json(ocr_path) or []
    entry = next((e for e in pages if e["page"] == page), None)
    if entry is None:
        raise HTTPException(404, "page not found")
    blocks = entry.get("blocks", [])
    if sorted(body.order) != list(range(len(blocks))):
        raise HTTPException(422, "order must be a permutation of the block indices")
    entry["blocks"] = [blocks[i] for i in body.order]
    save_json(ocr_path, pages)
    return OkOut(ok=True, detail="blocks reordered", data={"page": page, "count": len(blocks)})


@app.post("/api/jobs/{job_id}/pages/{page}/rebuild", response_model=OkOut)
def rebuild_page(job_id: str, page: int, settings: Settings = Depends(_settings_dep)):
    s = db.get_session()
    try:
        job = s.get(db.Job, job_id)
        if not job:
            raise HTTPException(404, "job not found")
        doc_id = job.document_id
        job_config = dict(job.config)
    finally:
        s.close()
    runner = _runner()
    cfg = runner.config_for(job_config, doc_id, job_id)
    pages = _load_pages(settings, job)
    entry = next((e for e in pages if e["page"] == page), None)
    if entry is None:
        raise HTTPException(404, "page not found")
    source = str(storage.source_path(settings, doc_id))
    tex = latex.build_tex(cfg, "source", source, entry)
    tex_path = paths.page_tex(cfg.workdir, "source", page)
    tex_path.write_text(tex)
    ok, log = latex.compile_tex(tex_path, tex_path.parent)
    if not ok:
        raise HTTPException(422, f"typesetting failed: {log[:300]}")
    return OkOut(
        ok=True,
        detail="page rebuilt",
        data={"page": page, "pdf": str(paths.page_pdf(cfg.workdir, "source", page))},
    )


# --------------------------------------------------------------------------
# artifacts + models + usage
# --------------------------------------------------------------------------
@app.get("/api/jobs/{job_id}/report")
def job_report(job_id: str, settings: Settings = Depends(_settings_dep)) -> dict:
    s = db.get_session()
    try:
        if not s.get(db.Job, job_id):
            raise HTTPException(404, "job not found")
    finally:
        s.close()
    path = storage.artifact_dir(settings, job_id) / "report.json"
    return load_json(path) or {}


@app.get("/api/artifacts/{artifact_id}/download")
def download_artifact(artifact_id: str):
    s = db.get_session()
    try:
        art = s.get(db.Artifact, artifact_id)
        if not art or not Path(art.path).exists():
            raise HTTPException(404, "artifact not found")
        return FileResponse(art.path, filename=Path(art.path).name, media_type="application/pdf")
    finally:
        s.close()


@app.get("/api/models", response_model=ModelsResponse)
def list_models(
    api_key: str | None = None,
    api_base: str | None = None,
    settings: Settings = Depends(_settings_dep),
    sid: str = Depends(_session_id),
):
    key = api_key or _stored_key(settings, sid) or os.environ.get(settings.api_key_env)
    if not key:
        return ModelsResponse(models=[ModelInfo(id=settings.default_model, inputs=["image"])])
    base = api_base or settings.api_base
    try:
        prov = OpenAICompatibleProvider(base, "probe", key)
        models = prov.list_models()
    except (ProviderError, Exception):  # noqa: BLE001
        raise HTTPException(400, "could not list models (check key/endpoint)") from None
    out = []
    for m in models:
        inputs = m.get("input_modalities") or m.get("modalities") or []
        out.append(ModelInfo(id=m.get("id", ""), name=m.get("name"), inputs=list(inputs)))
    return ModelsResponse(models=out)


@app.get("/api/usage", response_model=UsageOut)
def usage():
    s = db.get_session()
    try:
        jobs = s.execute(select(db.Job)).scalars().all()
        rows = s.execute(select(db.Usage)).scalars().all()
        return UsageOut(
            jobs=len(jobs),
            done=sum(1 for j in jobs if j.status == "done"),
            failed=sum(1 for j in jobs if j.status == "failed"),
            cost_usd=round(sum(u.cost_usd for u in rows), 4),
            prompt_tokens=sum(u.prompt_tokens for u in rows),
            completion_tokens=sum(u.completion_tokens for u in rows),
            calls=sum(u.calls for u in rows),
        )
    finally:
        s.close()


# --------------------------------------------------------------------------
# serve the built frontend, if present
# --------------------------------------------------------------------------
_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if _dist.exists():
    app.mount("/", StaticFiles(directory=str(_dist), html=True), name="frontend")
