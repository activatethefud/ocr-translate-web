"""Pydantic request/response schemas."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Combine = Literal["interleave", "grouped", "side_by_side"]


class JobCreate(BaseModel):
    source_lang: str = "auto"
    target_lang: str = "English"
    model: str | None = None
    api_key: str | None = Field(default=None, description="BYOK; not stored")
    api_base: str | None = None
    font_main: str | None = None
    linebreak_locale: str | None = None
    bilingual: bool = True
    combine: Combine = "interleave"
    dpi: int = 150
    max_px: int = 1800
    figure_px: int = 1800
    max_scale: float = 0.0
    text_width: str = "16.5cm"
    prompt_version: str = "1"


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


class OkOut(BaseModel):
    ok: bool = True
    detail: str = ""
    data: dict[str, Any] = {}
