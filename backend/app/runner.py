"""Background job execution: run the engine, persist events, register artifacts."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

from ocrtran import Pipeline, PipelineConfig
from ocrtran.events import CancelToken, Event

from . import db, storage
from .settings import Settings

log = logging.getLogger("app.runner")

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
        data = {k: v for k, v in job_row.items() if v is not None}
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

        def on_event(e: Event) -> None:
            self._record(job_id, e)
            prog = None
            if e.stage == "ocr" and e.status == "progress":
                counters["ocr"] = e.index
                prog = 0.60 * e.index / max(1, e.total)
            elif e.stage == "build" and e.status == "ok":
                counters["build"] += 1
                prog = 0.60 + 0.35 * counters["build"] / total
            elif e.stage == "assemble" and e.status == "ok":
                prog = 0.99
            if prog is not None:
                self._update(job_id, progress=min(prog, 0.99), done_pages=counters["build"])

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
            s = db.get_session()
            try:
                for _base, path in result.outputs.items():
                    dest = storage.copy_artifact(self.settings, job_id, path, "output")
                    s.add(
                        db.Artifact(
                            job_id=job_id, kind=cfg.output_mode, path=str(dest), bytes=dest.stat().st_size
                        )
                    )
                s.commit()
            finally:
                s.close()
            self._update(job_id, status="done", progress=1.0, done_pages=total, finished_at=db.utcnow())
        except Exception as exc:  # noqa: BLE001 - reported to the job
            log.exception("job %s failed", job_id)
            self._record(job_id, Event("error", "error", message=str(exc)))
            self._update(job_id, status="failed", error=str(exc), finished_at=db.utcnow())
        finally:
            self.tokens.pop(job_id, None)
