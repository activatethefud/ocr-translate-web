"""Render PDF pages and figures with PyMuPDF."""

from __future__ import annotations

from pathlib import Path

import fitz


def page_count(pdf: str | Path) -> int:
    with fitz.open(pdf) as doc:
        return doc.page_count


def page_size(pdf: str | Path, page_no: int) -> tuple[float, float]:
    """Width/height in points of a 1-indexed page."""
    with fitz.open(pdf) as doc:
        r = doc[page_no - 1].rect
        return r.width, r.height


def render_pages(pdf: str | Path, outdir: str | Path, dpi: int = 150, max_px: int = 1800) -> list[Path]:
    """Render every page to ``outdir/p-NN.png``.

    ``max_px`` caps the longest side so huge scanned pages (page points in the
    thousands) don't produce enormous images.
    """
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    made: list[Path] = []
    with fitz.open(pdf) as doc:
        for i, page in enumerate(doc):
            zoom = dpi / 72.0
            if max_px:
                zoom = min(zoom, max_px / max(page.rect.width, page.rect.height))
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
            path = outdir / f"p-{i + 1:02d}.png"
            pix.save(str(path))
            made.append(path)
    return made


def render_figure(
    pdf: str | Path,
    page_no: int,
    bbox: list[float],
    out_path: str | Path,
    target_px: int = 1800,
) -> bool:
    """Crop a figure from the *source* PDF at high resolution.

    ``bbox`` is normalised ``[x0, y0, x1, y1]`` (0..1). Cropping from the source
    (rather than the small OCR render) keeps figures sharp when the whole page is
    later scaled up.
    """
    with fitz.open(pdf) as doc:
        page = doc[page_no - 1]
        w, h = page.rect.width, page.rect.height
        clip = fitz.Rect(bbox[0] * w, bbox[1] * h, bbox[2] * w, bbox[3] * h)
        if clip.width <= 0 or clip.height <= 0:
            return False
        zoom = min(target_px / max(clip.width, clip.height), 24.0)
        pix = page.get_pixmap(clip=clip, matrix=fitz.Matrix(zoom, zoom))
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        pix.save(str(out_path))
    return True


def looks_scanned(pdf: str | Path, sample: int = 3) -> bool:
    """Heuristic: a page with almost no extractable text is a scan."""
    with fitz.open(pdf) as doc:
        pages = list(range(min(sample, doc.page_count)))
        chars = sum(len(doc[i].get_text().strip()) for i in pages)
        return chars < 20 * max(1, len(pages))
