"""Background job execution: run the engine, persist events, register artifacts."""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields
from pathlib import Path

from ocrtran import Pipeline, PipelineConfig, verify
from ocrtran.cache import save_json
from ocrtran.events import CancelToken, Event
from ocrtran.pricing import cost_usd

from . import db, storage
from .settings import Settings

log = logging.getLogger("app.runner")

_ENGINE_KEYS = {f.name for f in fields(PipelineConfig)}

_FONT_HINTS = {
    "chinese": ("Noto Sans CJK SC", "zh"),
    "japanese": ("Noto Sans CJK JP", "ja"),
    "korean": ("Noto Sans CJK KR", "ko"),
}


def default_font(target_lang: str) -> tuple[str, str]:
    low = target_lang.lower()
    for key, (font, loc) in _FONT_HINTS.items():
        if key in low:
            return font, loc
    return "Noto Serif", ""


class JobRunner:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.pool = ThreadPoolExecutor(max_workers=settings.worker_concurrency)
        self.tokens: dict[str, CancelToken] = {}
        # events arrive from parallel page workers -> serialize DB writes
        self._db_lock = threading.Lock()

    # -- public --------------------------------------------------------
    def submit(self, job_id: str, api_key: str | None) -> None:
        self.tokens[job_id] = CancelToken()
        self.pool.submit(self._run, job_id, api_key)

    def cancel(self, job_id: str) -> bool:
        token = self.tokens.get(job_id)
        if token:
            token.cancel()
            return True
        return False

    def shutdown(self) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)

    # -- persistence helpers ------------------------------------------
    def _record(self, job_id: str, e: Event) -> None:
        with self._db_lock:
            s = db.get_session()
            try:
                s.add(
                    db.EventRecord(
                        job_id=job_id,
                        stage=e.stage,
                        status=e.status,
                        base=e.base,
                        page=e.page,
                        message=e.message,
                        data=e.data or {},
                    )
                )
                s.commit()
            finally:
                s.close()

    def _update(self, job_id: str, **fields) -> None:
        with self._db_lock:
            s = db.get_session()
            try:
                job = s.get(db.Job, job_id)
                if job:
                    for k, v in fields.items():
                        setattr(job, k, v)
                    s.commit()
            finally:
                s.close()

    # -- config --------------------------------------------------------
    def config_for(self, job_row: dict, doc_id: str, job_id: str) -> PipelineConfig:
        data = {k: v for k, v in job_row.items() if v is not None and k in _ENGINE_KEYS}
        font = data.pop("font_main", None)
        loc = data.pop("linebreak_locale", None)
        if not font:
            font, auto_loc = default_font(data.get("target_lang", "English"))
            loc = loc if loc is not None else auto_loc
        return PipelineConfig.from_dict(
            {
                **data,
                "font_main": font,
                "linebreak_locale": loc or "",
                "api_key_env": self.settings.api_key_env,
                "sources": [str(storage.source_path(self.settings, doc_id))],
                "workdir": str(storage.work_dir(self.settings, doc_id, job_id)),
                # document-level cache: re-runs / edits reuse OCR across jobs
                "cache_dir": str(storage.doc_dir(self.settings, doc_id) / "cache"),
            }
        )

    # -- worker --------------------------------------------------------
    def _run(self, job_id: str, api_key: str | None) -> None:
        s = db.get_session()
        try:
            job = s.get(db.Job, job_id)
            if job is None:
                return
            doc_id, n_pages = job.document_id, job.total_pages
            job_config = dict(job.config)
        finally:
            s.close()

        self._update(job_id, status="running", started_at=db.utcnow())
        counters = {"ocr": 0, "build": 0}
        total = max(1, n_pages)
        state_lock = threading.Lock()

        def on_event(e: Event) -> None:
            # Only count a page as *done* when its OCR/build actually finishes,
            # so a long model call doesn't falsely jump the bar forward.
            self._record(job_id, e)
            with state_lock:
                prog = None
                if e.stage == "ocr" and e.status in ("ok", "warn") and e.page:
                    counters["ocr"] = max(counters["ocr"], e.index or e.page)
                    prog = 0.60 * counters["ocr"] / total
                elif e.stage == "build" and e.status == "ok":
                    counters["build"] += 1
                    prog = 0.60 + 0.35 * counters["build"] / total
                elif e.stage == "assemble" and e.status == "ok":
                    prog = 0.99
                if prog is not None:
                    self._update(
                        job_id,
                        progress=min(prog, 0.99),
                        done_pages=min(total, counters["ocr"] + counters["build"]),
                    )

        try:
            cfg = self.config_for(job_config, doc_id, job_id)
            pipe = Pipeline(cfg, api_key=api_key, on_event=on_event, cancel=self.tokens[job_id])
            result = pipe.run()
            if result.canceled:
                self._update(job_id, status="canceled", finished_at=db.utcnow())
                return
            if not result.outputs:
                self._update(job_id, status="failed", error="no output produced", finished_at=db.utcnow())
                return
            usage = result.usage or {}
            cost = cost_usd(cfg.model, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))
            requested = (job_config.get("output_name") or "").strip()
            items = list(result.outputs.items())
            s = db.get_session()
            try:
                for _base, path in items:
                    if requested:
                        stem = requested if len(items) == 1 else f"{requested} ({Path(path).stem})"
                        name = storage.safe_filename(stem, fallback=Path(path).name, ext=".pdf")
                    else:
                        name = Path(path).name
                    dest = storage.copy_artifact(self.settings, job_id, path, "output", dest_name=name)
                    s.add(
                        db.Artifact(
                            job_id=job_id, kind=cfg.output_mode, path=str(dest), bytes=dest.stat().st_size
                        )
                    )
                s.add(
                    db.Usage(
                        job_id=job_id,
                        model=cfg.model,
                        prompt_tokens=usage.get("prompt_tokens", 0),
                        completion_tokens=usage.get("completion_tokens", 0),
                        calls=usage.get("calls", 0),
                        cost_usd=cost,
                    )
                )
                s.commit()
            finally:
                s.close()
            missing = verify.missing_pages(cfg, result.ocr)
            warn = None
            if missing:
                warn = "pages not typeset: " + ", ".join(f"{b} {ms}" for b, ms in missing.items())
                self._record(job_id, Event("build", "error", message=warn))
            try:
                rdir = storage.artifact_dir(self.settings, job_id)
                rdir.mkdir(parents=True, exist_ok=True)
                save_json(rdir / "report.json", result.report)
            except OSError:
                pass
            self._update(
                job_id, status="done", progress=1.0, done_pages=total, cost_usd=cost, finished_at=db.utcnow()
            )
        except Exception as exc:  # noqa: BLE001 - reported to the job
            log.exception("job %s failed", job_id)
            self._record(job_id, Event("error", "error", message=str(exc)))
            self._update(job_id, status="failed", error=str(exc), finished_at=db.utcnow())
        finally:
            self.tokens.pop(job_id, None)
