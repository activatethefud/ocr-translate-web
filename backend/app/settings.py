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
    max_pages: int = 1000
    max_job_usd: float = 2.0
    worker_concurrency: int = 3
    cors_origins: list[str] = field(default_factory=lambda: ["http://localhost:5173"])

    @property
    def docs_dir(self) -> Path:
        return self.storage_dir / "docs"

    @property
    def artifacts_dir(self) -> Path:
        return self.storage_dir / "artifacts"

    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite:///{self.storage_dir / 'app.db'}"

    def ensure_dirs(self) -> None:
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.docs_dir.mkdir(parents=True, exist_ok=True)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)


def load_settings() -> Settings:
    origins = os.environ.get("CORS_ORIGINS", "http://localhost:5173")
    return Settings(
        storage_dir=Path(os.environ.get("STORAGE_DIR", "data")),
        database_url=os.environ.get("DATABASE_URL", ""),
        default_model=os.environ.get("DEFAULT_MODEL", "deepseek-flash"),
        api_base=os.environ.get("API_BASE", "https://api.deepseek.com/chat/completions"),
        api_key_env=os.environ.get("API_KEY_ENV", "DS_KEY"),
        max_upload_mb=_int("MAX_UPLOAD_MB", 200),
        max_pages=_int("MAX_PAGES", 1000),
        max_job_usd=_float("MAX_JOB_USD", 2.0),
        worker_concurrency=_int("WORKER_CONCURRENCY", 3),
        cors_origins=[o.strip() for o in origins.split(",") if o.strip()],
    )


@lru_cache
def get_settings() -> Settings:
    s = load_settings()
    s.ensure_dirs()
    return s
