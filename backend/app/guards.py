"""Abuse guards: cooldown, active-job cap, hourly quota, daily budget, dedupe.

These are intentionally simple and DB-backed (single user / small team). They are
**disabled in ``APP_MODE=local``** so local use and tests are unaffected; set
``APP_MODE=team`` (or ``public``) to enforce them. See ABUSE_PROTECTION.md (R0).
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import select

from . import db
from .settings import Settings

ACTIVE_STATES = ("queued", "running", "paused", "finalizing")
# config keys that define "the same translation request" for dedupe
_DEDUPE_KEYS = (
    "source_lang",
    "target_lang",
    "model",
    "bilingual",
    "combine",
    "pages",
    "unprocessed",
    "output_page_size",
    "scale_mode",
    "figure_mode",
    "verify_math",
    "glossary",
    "do_not_translate",
    "llm_instructions",
    "concurrency",
)


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def session_jobs(s, session_id: str) -> list[db.Job]:
    # session id lives in job.config (set at creation)
    rows = s.execute(select(db.Job)).scalars().all()
    return [j for j in rows if (j.config or {}).get("session_id") == session_id]


def config_fingerprint(cfg: dict) -> str:
    data = {k: cfg.get(k) for k in _DEDUPE_KEYS}
    blob = json.dumps(data, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def find_duplicate(s, session_id: str, doc_sha: str, cfg: dict, window: int) -> db.Job | None:
    fp = config_fingerprint(cfg)
    cutoff = _now() - timedelta(seconds=window)
    for job in reversed(session_jobs(s, session_id)):
        if job.created_at and job.created_at < cutoff:
            continue
        meta = (job.config or {}).get("_fingerprint")
        if meta == f"{doc_sha[:16]}:{fp}":
            return job
    return None


def enforce_submit(s, settings: Settings, session_id: str) -> None:
    """Raise 429/402 if this session may not start another job."""
    if not settings.limits_enabled:
        return
    jobs = session_jobs(s, session_id)
    now = _now()

    active = [j for j in jobs if j.status in ACTIVE_STATES]
    if len(active) >= settings.max_active_jobs_per_session:
        raise HTTPException(
            429, "too many active jobs; wait for one to finish", headers={"Retry-After": "10"}
        )

    recent = [j for j in jobs if j.created_at and j.created_at > now - timedelta(hours=1)]
    if len(recent) >= settings.max_jobs_per_hour:
        raise HTTPException(
            429, f"hourly job limit reached ({settings.max_jobs_per_hour})", headers={"Retry-After": "300"}
        )

    last = max((j.created_at for j in jobs if j.created_at), default=None)
    if last and (now - last).total_seconds() < settings.job_cooldown_seconds:
        raise HTTPException(
            429,
            "please wait before starting another job",
            headers={"Retry-After": str(settings.job_cooldown_seconds)},
        )

    day = [j for j in jobs if j.created_at and j.created_at > now - timedelta(hours=24)]
    spent = sum(j.cost_usd or 0.0 for j in day)
    if settings.max_daily_usd and spent >= settings.max_daily_usd:
        raise HTTPException(402, f"daily budget reached (${settings.max_daily_usd:.2f})")
