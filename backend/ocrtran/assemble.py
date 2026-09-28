"""Combine source pages and translated pages into the final PDF.

Output modes (``PipelineConfig.output_mode``):

* ``interleave``      original, translation, original, translation, ...
* ``grouped``         all originals, then all translations
* ``side_by_side``    both languages on one page (original left, translation right)
* ``translated_only`` just the translation
"""

from __future__ import annotations

from pathlib import Path

import fitz

from . import paths
from .config import PipelineConfig
from .events import CancelToken, Emitter, Event, emit


def _fit_scale(area: fitz.Rect, content: fitz.Rect, max_scale: float) -> float:
    sc = min(area.width / content.width, area.height / content.height)
    if max_scale:
        sc = min(sc, max_scale)
    return sc


def _place(
    page: fitz.Page, content_pdf: fitz.Document, area: fitz.Rect, margin: float, max_scale: float
) -> None:
    r = content_pdf[0].rect
    sc = _fit_scale(area, r, max_scale)
    w, h = r.width * sc, r.height * sc
    x0 = area.x0 + (area.width - w) / 2
    y0 = area.y0  # top-aligned; content fills the page
    if y0 + h > area.y1:
        y0 = area.y1 - h
    page.show_pdf_page(fitz.Rect(x0, y0, x0 + w, y0 + h), content_pdf, 0)


def _translated_pdf(cfg: PipelineConfig, base: str, page: int) -> Path | None:
    p = paths.page_pdf(cfg.workdir, base, page)
    return p if p.exists() else None


def _add_original(out: fitz.Document, src: fitz.Document, i: int) -> None:
    out.insert_pdf(src, from_page=i, to_page=i)


def _add_translation(
    out: fitz.Document,
    src: fitz.Document,
    cfg: PipelineConfig,
    base: str,
    i: int,
    size: tuple[float, float],
    area: fitz.Rect,
) -> None:
    tp = _translated_pdf(cfg, base, i + 1)
    if tp is None:
        out.insert_pdf(src, from_page=i, to_page=i)  # graceful fallback
        return
    cd = fitz.open(str(tp))
    page = out.new_page(width=size[0], height=size[1])
    _place(page, cd, area, cfg.margin_pt, cfg.max_scale)
    cd.close()


def assemble(
    cfg: PipelineConfig,
    on_event: Emitter | None = None,
    cancel: CancelToken | None = None,
) -> dict[str, Path]:
    cancel = cancel or CancelToken()
    mode = cfg.output_mode
    outputs: dict[str, Path] = {}
    emit(on_event, Event("assemble", "started", total=len(cfg.resolve_sources())))

    for si, source in enumerate(cfg.resolve_sources()):
        cancel.check()
        base = Path(source).stem
        src = fitz.open(source)
        out = fitz.open()
        W, H = src[0].rect.width, src[0].rect.height
        margin = cfg.margin_pt
        area = fitz.Rect(margin, margin, W - margin, H - margin)
        size = (W, H)
        n = src.page_count

        if mode == "interleave":
            for i in range(n):
                _add_original(out, src, i)
                _add_translation(out, src, cfg, base, i, size, area)
        elif mode == "grouped":
            for i in range(n):
                _add_original(out, src, i)
            for i in range(n):
                _add_translation(out, src, cfg, base, i, size, area)
        elif mode == "translated_only":
            for i in range(n):
                _add_translation(out, src, cfg, base, i, size, area)
        elif mode == "side_by_side":
            half = (W - 3 * margin) / 2
            left = fitz.Rect(margin, margin, margin + half, H - margin)
            right = fitz.Rect(W - margin - half, margin, W - margin, H - margin)
            for i in range(n):
                page = out.new_page(width=W, height=H)
                page.show_pdf_page(left, src, i)  # original left
                tp = _translated_pdf(cfg, base, i + 1)
                if tp is not None:
                    cd = fitz.open(str(tp))
                    _place(page, cd, right, margin, cfg.max_scale)
                    cd.close()

        outpath = paths.output_pdf(cfg.workdir, base, cfg.target_lang, mode)
        outpath.parent.mkdir(parents=True, exist_ok=True)
        pages = out.page_count
        out.subset_fonts()
        out.save(str(outpath), deflate=True, garbage=4)
        out.close()
        src.close()
        outputs[base] = outpath
        emit(
            on_event,
            Event(
                "assemble",
                "ok",
                base=base,
                index=si + 1,
                total=len(cfg.resolve_sources()),
                data={"path": str(outpath), "pages": pages},
            ),
        )
    return outputs
