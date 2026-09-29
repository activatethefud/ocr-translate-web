"""Combine source pages and translated pages into the final PDF.

Output modes (``PipelineConfig.output_mode``):

* ``interleave``      original, translation, original, translation, ...
* ``grouped``         originals, then translations
* ``side_by_side``    both languages on one page (original left, translation right)
* ``translated_only`` just the translation

Page control: ``cfg.pages`` selects which pages are translated; ``cfg.unprocessed``
decides what happens to the rest (``original`` = keep the page, ``skip`` = omit).
``cfg.output_page_size`` (match/a4/letter) and ``cfg.scale_mode`` (fill/fit)
control the translated pages.
"""

from __future__ import annotations

from pathlib import Path

import fitz

from . import paths
from .config import PipelineConfig
from .events import CancelToken, Emitter, Event, emit
from .pages import parse_page_spec

A4 = (595.28, 841.89)
LETTER = (612.0, 792.0)


def _output_size(cfg: PipelineConfig, src_w: float, src_h: float) -> tuple[float, float]:
    if cfg.output_page_size == "a4":
        return A4
    if cfg.output_page_size == "letter":
        return LETTER
    return (src_w, src_h)


def _eff_max_scale(cfg: PipelineConfig) -> float:
    """``fit`` never enlarges; ``fill`` enlarges to fill (capped by max_scale)."""
    return 1.0 if cfg.scale_mode == "fit" else cfg.max_scale


def _place(page: fitz.Page, doc: fitz.Document, pno: int, area: fitz.Rect, max_scale: float) -> None:
    r = doc[pno].rect
    sc = min(area.width / r.width, area.height / r.height)
    if max_scale:
        sc = min(sc, max_scale)
    w, h = r.width * sc, r.height * sc
    x0 = area.x0 + (area.width - w) / 2
    y0 = area.y0
    if y0 + h > area.y1:
        y0 = area.y1 - h
    page.show_pdf_page(fitz.Rect(x0, y0, x0 + w, y0 + h), doc, pno)


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
    page_no: int,
    size: tuple[float, float],
    area: fitz.Rect,
    fallback: bool = False,
) -> None:
    tp = _translated_pdf(cfg, base, page_no)
    if tp is None:
        # interleave/grouped already include the original page, so only fall back in
        # translated_only (where there is no original elsewhere).
        if fallback:
            _add_original(out, src, page_no - 1)
        return
    cd = fitz.open(str(tp))
    page = out.new_page(width=size[0], height=size[1])
    _place(page, cd, 0, area, _eff_max_scale(cfg))
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
        n = src.page_count
        selected = parse_page_spec(cfg.pages, n)
        keep_unselected = cfg.unprocessed == "original"

        W, H = src[0].rect.width, src[0].rect.height
        outW, outH = _output_size(cfg, W, H)
        margin = cfg.margin_pt
        size = (outW, outH)
        area = fitz.Rect(margin, margin, outW - margin, outH - margin)

        if mode == "interleave":
            for i in range(n):
                p = i + 1
                if p in selected:
                    _add_original(out, src, i)
                    _add_translation(out, src, cfg, base, p, size, area)
                elif keep_unselected:
                    _add_original(out, src, i)
        elif mode == "grouped":
            orig_pages = range(n) if keep_unselected else [p - 1 for p in selected]
            for i in orig_pages:
                _add_original(out, src, i)
            for p in selected:
                _add_translation(out, src, cfg, base, p, size, area)
        elif mode == "translated_only":
            for p in selected:
                _add_translation(out, src, cfg, base, p, size, area, fallback=True)
        elif mode == "side_by_side":
            half = (outW - 3 * margin) / 2
            left = fitz.Rect(margin, margin, margin + half, outH - margin)
            right = fitz.Rect(outW - margin - half, margin, outW - margin, outH - margin)
            for i in range(n):
                p = i + 1
                if p in selected:
                    page = out.new_page(width=outW, height=outH)
                    _place(page, src, i, left, _eff_max_scale(cfg))  # original left
                    tp = _translated_pdf(cfg, base, p)
                    if tp is not None:
                        cd = fitz.open(str(tp))
                        _place(page, cd, 0, right, _eff_max_scale(cfg))
                        cd.close()
                elif keep_unselected:
                    _add_original(out, src, i)

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
