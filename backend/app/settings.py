"""Runtime settings from environment (see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass
class Settings:
    storage_dir: Path = field(default_factory=lambda: Path("data"))
    database_url: str = ""
    default_model: str = "deepseek-flash"
    api_base: str = "https://api.deepseek.com/chat/completions"
    api_key_env: str = "DS_KEY"
    max_upload_mb: int = 200
    min_free_mb: int = 200  # refuse new jobs below this much free disk
    cache_ttl_hours: float = 24.0  # drop work intermediates older than this
    artifact_ttl_hours: float = 0.0  # drop output artifacts older than this (0 = keep)
    max_pages: int = 1000
    max_job_usd: float = 2.0
    max_book_usd: float = 5.0
    max_daily_usd: float = 1.0
    app_mode: str = "local"  # local | team | public
    job_cooldown_seconds: int = 5
    max_active_jobs_per_session: int = 1
    max_jobs_per_hour: int = 10
    max_uploads_per_hour: int = 20
    dedupe_window: int = 60
    rate_limit_per_min: int = 240
    rate_burst: int = 200
    max_sse_per_session: int = 3
    trust_proxy: bool = False
    admin_token: str = ""
    book_chunk_size: int = 25
    book_chunk_concurrency: int = 2
    rolling_glossary: bool = True
    batch_concurrency: int = 1  # documents processed sequentially by default
    max_concurrent_calls: int = 16  # global ceiling for in-flight model calls
    worker_concurrency: int = 3
    page_concurrency: int = 4
    cors_origins: list[str] = field(default_factory=lambda: ["http://localhost:5173"])

    @property
    def limits_enabled(self) -> bool:
        return self.app_mode != "local"

    @property
    def server_key_allowed(self) -> bool:
        """Only local runs may fall back to the server's own API key; in public
        mode BYOK is mandatory."""
        return self.app_mode == "local"

    @property
    def docs_dir(self) -> Path:
        return self.storage_dir / "docs"

    @property
    def artifacts_dir(self) -> Path:
        return self.storage_dir / "artifacts"

    @property
    def sources_dir(self) -> Path:
        return self.storage_dir / "sources"

    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite:///{self.storage_dir / 'app.db'}"

    def ensure_dirs(self) -> None:
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.docs_dir.mkdir(parents=True, exist_ok=True)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.sources_dir.mkdir(parents=True, exist_ok=True)


def load_settings() -> Settings:
    origins = os.environ.get("CORS_ORIGINS", "http://localhost:5173")
    return Settings(
        storage_dir=Path(os.environ.get("STORAGE_DIR", "data")),
        database_url=os.environ.get("DATABASE_URL", ""),
        default_model=os.environ.get("DEFAULT_MODEL", "deepseek-flash"),
        api_base=os.environ.get("API_BASE", "https://api.deepseek.com/chat/completions"),
        api_key_env=os.environ.get("API_KEY_ENV", "DS_KEY"),
        max_upload_mb=_int("MAX_UPLOAD_MB", 200),
        min_free_mb=_int("MIN_FREE_MB", 200),
        cache_ttl_hours=_float("CACHE_TTL_HOURS", 24.0),
        artifact_ttl_hours=_float("ARTIFACT_TTL_HOURS", 0.0),
        max_pages=_int("MAX_PAGES", 1000),
        max_job_usd=_float("MAX_JOB_USD", 2.0),
        max_book_usd=_float("MAX_BOOK_USD", 5.0),
        max_daily_usd=_float("MAX_DAILY_USD", 1.0),
        app_mode=os.environ.get("APP_MODE", "local"),
        job_cooldown_seconds=_int("JOB_COOLDOWN_SECONDS", 5),
        max_active_jobs_per_session=_int("MAX_ACTIVE_JOBS_PER_SESSION", 1),
        max_jobs_per_hour=_int("MAX_JOBS_PER_HOUR", 10),
        max_uploads_per_hour=_int("MAX_UPLOADS_PER_HOUR", 20),
        dedupe_window=_int("DEDUPE_WINDOW", 60),
        rate_limit_per_min=_int("RATE_LIMIT_PER_MIN", 240),
        rate_burst=_int("RATE_BURST", 200),
        max_sse_per_session=_int("MAX_SSE_PER_SESSION", 3),
        trust_proxy=os.environ.get("TRUST_PROXY", "0") in ("1", "true", "yes"),
        admin_token=os.environ.get("ADMIN_TOKEN", ""),
        book_chunk_size=_int("BOOK_CHUNK_SIZE", 25),
        book_chunk_concurrency=_int("BOOK_CHUNK_CONCURRENCY", 2),
        rolling_glossary=os.environ.get("ROLLING_GLOSSARY", "1") not in ("0", "false", "no"),
        batch_concurrency=_int("BATCH_CONCURRENCY", 1),
        max_concurrent_calls=_int("MAX_CONCURRENT_CALLS", 16),
        worker_concurrency=_int("WORKER_CONCURRENCY", 3),
        page_concurrency=_int("PAGE_CONCURRENCY", 4),
        cors_origins=[o.strip() for o in origins.split(",") if o.strip()],
    )


@lru_cache
def get_settings() -> Settings:
    s = load_settings()
    s.ensure_dirs()
    return s
