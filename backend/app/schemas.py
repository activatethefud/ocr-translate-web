"""Pydantic request/response schemas."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Combine = Literal["interleave", "grouped", "side_by_side", "translated_only"]


class GlossaryItem(BaseModel):
    source: str
    target: str


class JobCreate(BaseModel):
    source_lang: str = "auto"
    target_lang: str = "English"
    model: str | None = None
    api_key: str | None = Field(default=None, description="BYOK; not stored")
    api_base: str | None = None
    font_main: str | None = None
    fallback_font: str | None = None
    linebreak_locale: str | None = None
    bilingual: bool = True
    combine: Combine = "interleave"
    dpi: int = 150
    max_px: int = 1800
    figure_px: int = 1800
    figure_dpi: int = 300
    figure_format: Literal["auto", "png", "jpeg"] = "auto"
    jpeg_quality: int = 85
    figure_pad: float = 0.06
    figure_mode: Literal["off", "tight", "judge"] = "judge"  # app default: best figures
    # page fitting / figures
    layout_mode: Literal["single", "auto"] = "auto"
    min_page_scale: float = 0.90
    figure_layout: Literal["flow", "preserve", "grid", "stack"] = "flow"
    figure_max_width: float = 0.85
    figure_max_height: float = 0.38
    max_figures_per_row: int = 3
    page_fill_min: float = 0.25
    keep_together: bool = True
    max_scale: float = 0.0
    text_width: str = "16.5cm"
    prompt_version: str = "3"
    glossary: list[GlossaryItem] = []
    do_not_translate: list[str] = []
    llm_instructions: str = ""
    verify_math: bool = False
    error_guard: Literal["off", "auto"] = "auto"
    subject: Literal["auto", "math", "physics", "chemistry", "biology", "general"] = "auto"
    # page control
    pages: str = "all"
    unprocessed: Literal["original", "skip"] = "skip"
    output_page_size: Literal["match", "a4", "letter"] = "match"
    scale_mode: Literal["fill", "fit"] = "fill"
    concurrency: int | None = None  # pages translated in parallel
    output_name: str | None = None  # output document name (blank -> derived)


class SessionUpdate(BaseModel):
    api_key: str | None = Field(default=None, description="BYOK; stored encrypted")
    api_base: str | None = None
    clear_key: bool = False


class SessionOut(BaseModel):
    id: str
    has_key: bool
    hint: str | None = None
    api_base: str | None = None


class DocumentOut(BaseModel):
    id: str
    filename: str
    sha256: str
    n_pages: int
    page_w: float
    page_h: float
    kind: str
    size_bytes: int
    created_at: str


class ArtifactOut(BaseModel):
    id: str
    kind: str
    bytes: int
    filename: str
    download_url: str


class JobOut(BaseModel):
    id: str
    document_id: str
    status: str
    kind: str = "single"
    model: str
    source_lang: str
    target_lang: str
    mode: str
    progress: float
    total_pages: int
    done_pages: int
    cost_usd: float
    error: str | None = None
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    artifacts: list[ArtifactOut] = []


class BlockOut(BaseModel):
    idx: int
    type: str
    source: str = ""
    target: str = ""
    latex: str = ""
    description: str = ""
    bbox: list[float] | None = None
    level: int | None = None
    ordered: bool | None = None
    items: list[dict] | None = None
    kind: str | None = None
    name: str | None = None
    caption: str | None = None
    number: str | None = None


class BlockPatch(BaseModel):
    source: str | None = None
    target: str | None = None
    latex: str | None = None


class BlockReorder(BaseModel):
    order: list[int]


class PageOut(BaseModel):
    page: int
    status: str
    image_url: str
    blocks: list[BlockOut] = []


class ModelInfo(BaseModel):
    id: str
    name: str | None = None
    inputs: list[str] = []


class ModelsResponse(BaseModel):
    models: list[ModelInfo]


class ErrorOut(BaseModel):
    detail: str


class BookCreate(JobCreate):
    chunk_size: int = 25
    from_page: int = 1
    to_page: int | None = None
    boundaries: list[BoundaryItem] = []  # [{"page": n, "title": "Chapter 1"}]


class BoundaryItem(BaseModel):
    page: int
    title: str


class BatchCreate(JobCreate):
    """Translate several documents sequentially with one shared config."""

    document_ids: list[str]
    book: bool = False  # each document is a durable, chunked book job
    chunk_size: int = 25


class ChunkOut(BaseModel):
    id: str
    idx: int
    page_from: int
    page_to: int
    state: str
    done_pages: int = 0
    attempts: int
    max_attempts: int
    cost_usd: float
    error: str | None = None


class EstimateOut(BaseModel):
    model: str
    pages: int
    est_calls: int
    est_prompt_tokens: int
    est_completion_tokens: int
    est_cost_usd: float
    est_cost_low: float | None = None
    est_cost_high: float | None = None
    est_seconds: int | None = None
    breakdown: dict[str, float] = {}
    assumptions: dict = {}


class UsageOut(BaseModel):
    jobs: int
    done: int
    failed: int
    cost_usd: float
    prompt_tokens: int
    completion_tokens: int
    calls: int


class OkOut(BaseModel):
    ok: bool = True
    detail: str = ""
    data: dict[str, Any] = {}
