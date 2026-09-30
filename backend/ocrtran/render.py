"""Render PDF pages and figures with PyMuPDF."""

from __future__ import annotations

from pathlib import Path

import fitz

from .geometry import expand_bbox


def page_count(pdf: str | Path) -> int:
    with fitz.open(pdf) as doc:
        return doc.page_count


def page_size(pdf: str | Path, page_no: int) -> tuple[float, float]:
    """Width/height in points of a 1-indexed page."""
    with fitz.open(pdf) as doc:
        r = doc[page_no - 1].rect
        return r.width, r.height


def render_pages(
    pdf: str | Path,
    outdir: str | Path,
    dpi: int = 150,
    max_px: int = 1800,
    pages: list[int] | None = None,
) -> list[tuple[int, Path]]:
    """Render pages to ``outdir/p-NN.png`` and return ``[(page_no, path), ...]``.

    ``max_px`` caps the longest side so huge scanned pages don't produce enormous
    images; ``pages`` (1-based) restricts which pages are rendered.
    """
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    made: list[tuple[int, Path]] = []
    wanted = set(pages) if pages is not None else None
    with fitz.open(pdf) as doc:
        for i, page in enumerate(doc):
            pno = i + 1
            if wanted is not None and pno not in wanted:
                continue
            zoom = dpi / 72.0
            if max_px:
                zoom = min(zoom, max_px / max(page.rect.width, page.rect.height))
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
            path = outdir / f"p-{pno:02d}.png"
            pix.save(str(path))
            made.append((pno, path))
    return made


def _border_ink(path: str | Path, band: int = 2) -> float:
    """Fraction of dark pixels along the crop border (high = likely clipped)."""
    try:
        from PIL import Image

        im = Image.open(path).convert("L")
    except Exception:  # noqa: BLE001
        return 0.0
    w, h = im.size
    px = im.load()
    ink = tot = 0
    for x in range(w):
        for y in range(0, band):
            tot += 1
            ink += px[x, y] < 235
        for y in range(h - band, h):
            tot += 1
            ink += px[x, y] < 235
    for y in range(h):
        for x in range(0, band):
            tot += 1
            ink += px[x, y] < 235
        for x in range(w - band, w):
            tot += 1
            ink += px[x, y] < 235
    return ink / max(1, tot)


def _photographic(pix: fitz.Pixmap, max_colors: int = 1024) -> bool:
    """True if a crop looks photo-like (many colours) rather than line art."""
    try:
        p = fitz.Pixmap(pix)
        while max(p.width, p.height) > 256:
            p.shrink(1)
        n = p.n
        if n == 1:
            ch = 1
        elif n in (3, 4):  # ignore an alpha channel if present
            ch = 3
        else:  # CMYK etc.
            return True
        data = p.samples
        seen: set[bytes] = set()
        for i in range(0, len(data) - n + 1, n):
            seen.add(bytes(data[i : i + ch]))
            if len(seen) > max_colors:
                return True
        return False
    except Exception:  # noqa: BLE001
        return False


def render_figure(
    pdf: str | Path,
    page_no: int,
    bbox: list[float],
    out_path: str | Path,
    target_px: int = 1800,
    pad_frac: float = 0.06,
    min_pad_pt: float = 6.0,
    auto_expand: bool = True,
    fmt: str = "png",
    jpeg_quality: int = 85,
) -> Path | None:
    """Crop a figure from the *source* PDF and save it (returns the file written).

    ``bbox`` is normalised ``[x0, y0, x1, y1]`` (0..1). ``target_px`` caps the
    longest side: callers size it to the figure's **placed** size (``figure_dpi``),
    not a fixed value, so figures aren't stored at absurd resolutions. ``fmt`` is
    ``png`` (lossless), ``jpeg`` (small) or ``auto`` (jpeg for photo-like crops,
    png for line art). Pads the box slightly so tight model boxes don't clip.
    """
    if bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
        return None  # degenerate box (padding would manufacture a crop)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with fitz.open(pdf) as doc:
        page = doc[page_no - 1]
        w, h = page.rect.width, page.rect.height
        grow = pad_frac
        extra_pt = min_pad_pt
        written: Path | None = None
        for attempt in range(4):
            x0, y0, x1, y1 = expand_bbox(bbox, grow, extra_pt, w, h)
            clip = fitz.Rect(x0 * w, y0 * h, x1 * w, y1 * h)
            if clip.width <= 0 or clip.height <= 0:
                return None
            zoom = min(max(1e-3, target_px / max(clip.width, clip.height)), 24.0)
            pix = page.get_pixmap(clip=clip, matrix=fitz.Matrix(zoom, zoom), alpha=False)
            use_jpeg = fmt in ("jpeg", "jpg") or (fmt == "auto" and _photographic(pix))
            written = out_path.with_suffix(".jpg" if use_jpeg else ".png")
            if use_jpeg:
                try:
                    written.write_bytes(pix.tobytes("jpg", jpg_quality=jpeg_quality))
                except Exception:  # noqa: BLE001 - older PyMuPDF
                    pix.save(str(written))
            else:
                pix.save(str(written))
            # if ink still touches the border, the box is probably still too tight
            if not auto_expand or _border_ink(written) < 0.06 or attempt == 3:
                break
            grow = min(0.6, grow * 2 + 0.06)
            extra_pt = min(60.0, extra_pt * 2 + 6)
    return written


def looks_scanned(pdf: str | Path, sample: int = 3) -> bool:
    """Heuristic: a page with almost no extractable text is a scan."""
    with fitz.open(pdf) as doc:
        pages = list(range(min(sample, doc.page_count)))
        chars = sum(len(doc[i].get_text().strip()) for i in pages)
        return chars < 20 * max(1, len(pages))


def page_ink_ratio(pdf: str | Path, page_no: int, dpi: int = 50) -> float:
    """Fraction of non-white pixels on a page (low = blank)."""
    with fitz.open(pdf) as doc:
        page = doc[page_no - 1]
        zoom = dpi / 72.0
        zoom = min(zoom, 400 / max(page.rect.width, page.rect.height, 1))
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), colorspace=fitz.csGRAY)
        data = pix.samples
        if not data:
            return 0.0
        return sum(1 for b in data if b < 245) / len(data)


def is_blank(pdf: str | Path, page_no: int, threshold: float = 0.0003) -> bool:
    return page_ink_ratio(pdf, page_no) < threshold
