"""File storage layout, upload sanitization, and PDF inspection."""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

import fitz

from ocrtran.cache import file_sha256
from ocrtran.render import looks_scanned

from .settings import Settings


# -- paths -----------------------------------------------------------------
def doc_dir(settings: Settings, doc_id: str) -> Path:
    return settings.docs_dir / doc_id


def source_path(settings: Settings, doc_id: str) -> Path:
    return doc_dir(settings, doc_id) / "source.pdf"


def source_blob(settings: Settings, sha256: str) -> Path:
    """Content-addressed stored source: one file per unique document content."""
    return settings.sources_dir / f"{sha256}.pdf"


def artifact_blob(settings: Settings, sha256: str) -> Path:
    return settings.artifacts_dir / "blobs" / f"{sha256}.pdf"


def _link_or_copy(src: str | Path, dst: str | Path) -> None:
    """Hard-link ``src`` to ``dst`` (so identical content is stored once)."""
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.unlink(missing_ok=True)
    try:
        os.link(str(src), str(dst))
    except OSError:
        shutil.copy2(src, dst)


def work_dir(settings: Settings, doc_id: str, job_id: str | None = None) -> Path:
    base = doc_dir(settings, doc_id) / "work"
    return base / job_id if job_id else base


def artifact_dir(settings: Settings, job_id: str) -> Path:
    return settings.artifacts_dir / job_id


# -- sanitization ----------------------------------------------------------
def sanitize_pdf(src: str | Path, dst: str | Path) -> None:
    """Strip active content (JS, launch actions, embedded files, XFA).

    Uses pikepdf when available, else a PyMuPDF re-save. Never serves the
    original bytes back to the client.
    """
    src, dst = str(src), str(dst)
    try:
        import pikepdf

        with pikepdf.open(src) as pdf:
            root = pdf.Root
            for key in ("/OpenAction", "/AA"):
                if key in root:
                    del root[key]
            names = root.get("/Names")
            if names is not None:
                for key in ("/JavaScript", "/EmbeddedFiles"):
                    if key in names:
                        del names[key]
            acro = root.get("/AcroForm")
            if acro is not None and "/XFA" in acro:
                del acro["/XFA"]
            pdf.save(dst, linearize=True)
        return
    except Exception:  # noqa: BLE001 - fall back to a clean re-save
        pass
    doc = fitz.open(src)
    try:
        doc.save(dst, garbage=4, clean=True, deflate=True)
    finally:
        doc.close()


def inspect_pdf(path: str | Path) -> dict:
    with fitz.open(path) as doc:
        if doc.page_count == 0:
            raise ValueError("PDF has no pages")
        r = doc[0].rect
        return {
            "n_pages": doc.page_count,
            "page_w": r.width,
            "page_h": r.height,
            "kind": "scan" if looks_scanned(path) else "text",
        }


def finalize_upload(settings: Settings, doc_id: str, tmp_path: Path, filename: str) -> dict:
    """Store the sanitized upload once (content-addressed) and link it into the doc.

    The stored bytes live at ``sources/<sha256>.pdf``; every document that uploads the
    same content gets a **hard link** to that one file, so a re-uploaded book is not
    stored again. The sha is taken from the raw upload so it is stable across runs.
    """
    raw_sha = file_sha256(tmp_path)
    blob = source_blob(settings, raw_sha)
    if not blob.exists():
        sanitize_pdf(tmp_path, blob)  # sanitize straight into the shared blob
    tmp_path.unlink(missing_ok=True)
    dest = source_path(settings, doc_id)
    _link_or_copy(blob, dest)
    info = inspect_pdf(dest)
    return {
        "path": dest,
        "blob": str(blob),
        "filename": Path(filename).name or "upload.pdf",
        "sha256": raw_sha,
        "size_bytes": blob.stat().st_size,
        **info,
    }


def safe_filename(name: str | None, fallback: str, ext: str = "") -> str:
    """Turn a user-supplied name into a safe file name.

    Strips any directory components (defeats ``../`` traversal), replaces unsafe
    characters, ensures the extension, and caps the length. Falls back to
    ``fallback`` when the result is empty.
    """
    raw = (name or "").strip()
    base = Path(raw).name if raw else ""  # drop directories / traversal
    base = re.sub(r"[^\w\-. ()\[\]]+", "_", base, flags=re.UNICODE).strip(" .")
    if not base:
        base = fallback
    if ext and not base.lower().endswith(ext.lower()):
        base += ext
    return base[:140]


def copy_artifact(
    settings: Settings,
    job_id: str,
    src: str | Path,
    kind: str,
    dest_name: str | None = None,
) -> Path:
    outdir = artifact_dir(settings, job_id)
    outdir.mkdir(parents=True, exist_ok=True)
    dest = outdir / (dest_name or Path(src).name)
    # dedupe identical outputs: store the bytes once in artifacts/blobs and hard-link
    sha = file_sha256(src)
    blob = artifact_blob(settings, sha)
    if not blob.exists():
        blob.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, blob)  # a real copy, so blob nlink counts job links only
    _link_or_copy(blob, dest)
    return dest


def prune_artifacts(settings, keep_job_ids: set[str] | None = None, ttl_hours: float = 24.0) -> dict:
    """Delete output artifacts of jobs idle longer than ``ttl_hours`` (and free blobs)."""
    import time

    keep = set(keep_job_ids or ())
    cutoff = time.time() - ttl_hours * 3600.0
    freed = 0
    removed = 0
    for d in settings.artifacts_dir.glob("*"):
        if not d.is_dir() or d.name in ("blobs",) or d.name in keep:
            continue
        try:
            if d.stat().st_mtime >= cutoff:
                continue
        except OSError:
            continue
        freed += _dir_size(d)
        shutil.rmtree(d, ignore_errors=True)
        removed += 1
    blobs = settings.artifacts_dir / "blobs"
    if blobs.exists():
        for b in blobs.glob("*"):
            try:
                if b.stat().st_nlink <= 1:  # no job hard-links it anymore
                    freed += b.stat().st_size
                    b.unlink()
            except OSError:
                pass
    return {"freed_bytes": freed, "freed_mb": freed // (1024 * 1024), "removed": removed}


def free_mb(path) -> int:
    """Free megabytes on the filesystem holding ``path``."""
    import shutil

    try:
        return shutil.disk_usage(str(path)).free // (1024 * 1024)
    except OSError:
        return 1 << 30  # unknown -> don't block


def _dir_size(path) -> int:
    total = 0
    for p in Path(path).rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            pass
    return total


def prune_work(settings, keep_job_ids: set[str] | None = None) -> dict:
    """Delete heavy regenerable intermediates from finished jobs' work dirs.

    Removes ``source/pages`` (page renders), ``source/tex`` (LaTeX + crops + page PDFs)
    and ``chunks/`` for every job whose id is not in ``keep_job_ids``. Keeps
    ``source/ocr.json`` (review), the per-document OCR ``cache`` and the output
    ``artifacts`` — everything else is rebuilt from the cache for free.
    """
    import shutil

    keep = set(keep_job_ids or ())
    root = Path(settings.storage_dir) / "docs"
    freed = 0
    removed = 0
    for work in root.glob("*/work/*"):
        if work.name in keep:
            continue
        for sub in ("source/pages", "source/tex", "chunks"):
            target = work / sub
            if target.exists():
                freed += _dir_size(target)
                shutil.rmtree(target, ignore_errors=True)
                removed += 1
    return {"freed_bytes": freed, "freed_mb": freed // (1024 * 1024), "removed": removed}


def prune_old_work(settings, keep_job_ids: set[str] | None = None, ttl_hours: float = 5.0) -> dict:
    """Delete whole job work dirs whose last activity is older than ``ttl_hours``.

    Work dirs hold only regenerateable intermediates (rendered pages, LaTeX, figure
    crops, per-chunk copies, built page PDFs) — book ``chunks/`` are the biggest by far.
    The OCR ``cache`` (tiny) and the output ``artifacts`` are kept, so a re-run is
    cache-cheap. Active jobs and recent work are never touched.
    """
    import shutil
    import time

    keep = set(keep_job_ids or ())
    cutoff = time.time() - ttl_hours * 3600.0
    root = Path(settings.storage_dir) / "docs"
    freed = 0
    removed = 0
    for work in root.glob("*/work/*"):
        if work.name in keep:
            continue
        try:
            if work.stat().st_mtime >= cutoff:
                continue
        except OSError:
            continue
        freed += _dir_size(work)
        shutil.rmtree(work, ignore_errors=True)
        removed += 1
    return {"freed_bytes": freed, "freed_mb": freed // (1024 * 1024), "removed": removed}
