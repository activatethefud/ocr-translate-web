"""Geometry helpers for figure crops.

Vision models return figure boxes that are sometimes a little too tight and clip
the edge of a diagram. We mitigate that by (a) taking the union of the model's two
boxes (its main-call box is usually looser than the dedicated tight box) and
(b) padding the box by a fraction of its size before cropping.
"""

from __future__ import annotations

BBox = list[float]


def union_bbox(a: BBox | None, b: BBox | None) -> BBox | None:
    """Smallest box containing both ``a`` and ``b`` (``None``-tolerant)."""
    if a is None:
        return list(b) if b is not None else None
    if b is None:
        return list(a)
    return [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]


def expand_bbox(
    bbox: BBox,
    pad_frac: float = 0.06,
    min_pad_pt: float = 6.0,
    page_w: float = 0.0,
    page_h: float = 0.0,
    bounds: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0),
) -> BBox:
    """Grow ``bbox`` by ``pad_frac`` of its size (and at least ``min_pad_pt``),
    clamped to ``bounds`` (normalized). Padding never shrinks a box.
    """
    x0, y0, x1, y1 = (
        min(bbox[0], bbox[2]),
        min(bbox[1], bbox[3]),
        max(bbox[0], bbox[2]),
        max(bbox[1], bbox[3]),
    )
    w, h = x1 - x0, y1 - y0
    pad_x = abs(pad_frac) * w
    pad_y = abs(pad_frac) * h
    if page_w > 0:
        pad_x = max(pad_x, min_pad_pt / page_w)
    if page_h > 0:
        pad_y = max(pad_y, min_pad_pt / page_h)
    bx0, by0, bx1, by1 = bounds
    return [
        max(bx0, x0 - pad_x),
        max(by0, y0 - pad_y),
        min(bx1, x1 + pad_x),
        min(by1, y1 + pad_y),
    ]


def _area(b) -> float:
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def is_near_full_page(bbox, threshold: float = 0.85) -> bool:
    """True if the box covers (almost) the whole page - usually a model mistake."""
    return bool(bbox) and _area(bbox) >= threshold


def choose_box(tight=None, main=None, full_page: float = 0.85):
    """Pick the best figure box: the tight box when usable, else the main-call box.

    A loose main box that covers the whole page is ignored rather than unioned in
    (that used to turn every figure crop into the full page).
    """
    tight_ok = tight and tight[2] > tight[0] and tight[3] > tight[1]
    main_ok = main and main[2] > main[0] and main[3] > main[1]
    if tight_ok and not is_near_full_page(tight, full_page):
        return list(tight)
    if main_ok and not is_near_full_page(main, full_page):
        return list(main)
    # both absent or both suspicious: use whatever exists rather than dropping it
    if tight_ok:
        return list(tight)
    return list(main) if main_ok else None
