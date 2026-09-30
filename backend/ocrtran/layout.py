"""Page fitting: figure rows + pagination so text stays a consistent size.

The translated side of a source page is planned here:

1. **Figure rows** — consecutive figure blocks are clustered using their original
   bounding boxes (side-by-side figures stay side by side) and sized to fit the text
   width, with min/max width and max-height caps.
2. **Pagination** — items are greedily packed against the usable page height, so an
   over-full page *flows onto another page* instead of being shrunk to fit.

The module is pure (no LaTeX import): heights are either measured by the caller
(``measure`` callback, see :func:`ocrtran.latex.measure_items`) or estimated.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

# 1 cm / 1 in in TeX points
_PT_PER = {"pt": 1.0, "cm": 28.4527, "mm": 2.84527, "in": 72.27, "bp": 1.00375}


def parse_length_pt(text: str, default: float = 468.0) -> float:
    """``"16.5cm"`` -> points (TeΧ pt). Unknown formats fall back to ``default``."""
    m = re.match(r"^\s*(-?[\d.]+)\s*([a-z]+)?\s*$", text or "")
    if not m:
        return default
    val = float(m.group(1))
    unit = (m.group(2) or "pt").lower()
    return val * _PT_PER.get(unit, 1.0)


@dataclass
class Fig:
    block: dict
    bbox: tuple[float, float, float, float]
    width: float  # original width as a fraction of the page
    aspect: float  # cropped image height / width
    caption: str = ""

    @property
    def yy(self) -> tuple[float, float]:
        return (self.bbox[1], self.bbox[3])

    @property
    def xx(self) -> tuple[float, float]:
        return (self.bbox[0], self.bbox[2])


@dataclass
class FigRow:
    figs: list[Fig]
    widths: list[float]  # display widths, fraction of \textwidth


@dataclass
class Item:
    kind: str  # "block" | "figrow"
    block: dict | None = None
    row: FigRow | None = None
    height: float = 0.0  # points
    keep_with_next: bool = False


# ---------------------------------------------------------------------------
# figure clustering / sizing
# ---------------------------------------------------------------------------
def _v_overlap(a: Fig, b: Fig) -> float:
    lo = max(a.yy[0], b.yy[0])
    hi = min(a.yy[1], b.yy[1])
    if hi <= lo:
        return 0.0
    return (hi - lo) / max(1e-6, min(a.yy[1] - a.yy[0], b.yy[1] - b.yy[0]))


def _h_gap(a: Fig, b: Fig) -> float:
    if a.xx[1] < b.xx[0]:
        return b.xx[0] - a.xx[1]
    if b.xx[1] < a.xx[0]:
        return a.xx[0] - b.xx[1]
    return 0.0  # horizontally overlapping


def cluster_rows(figs: list[Fig], overlap: float = 0.5, gap: float = 0.10) -> list[list[Fig]]:
    """Group figures that sit on the same visual row (vertical overlap, small gap)."""
    rows: list[list[Fig]] = []
    for fig in sorted(figs, key=lambda f: (f.yy[0], f.xx[0])):
        for row in rows:
            if any(_v_overlap(fig, other) >= overlap and _h_gap(fig, other) < gap for other in row):
                row.append(fig)
                break
        else:
            rows.append([fig])
    for row in rows:
        row.sort(key=lambda f: f.xx[0])
    rows.sort(key=lambda r: min(f.yy[0] for f in r))
    return rows


def _flow_rows(figs: list[Fig], cfg) -> list[list[Fig]]:
    """Pack figures left-to-right while they fit at their **natural** width.

    If the figures don't fit side by side without being shrunk, they stay stacked
    (a column). This is the "flow into rows when there is comfortably space" rule.
    """
    ordered = sorted(figs, key=lambda f: (round(f.yy[0], 3), f.xx[0]))
    rows: list[list[Fig]] = []
    cur: list[Fig] = []
    total = 0.0
    for fig in ordered:
        w = max(0.05, fig.width)
        if cur and (len(cur) >= cfg.max_figures_per_row or total + w > 0.95):
            rows.append(cur)
            cur, total = [], 0.0
        cur.append(fig)
        total += w
    if cur:
        rows.append(cur)
    return rows


def _grid_rows(figs: list[Fig], cfg) -> list[list[Fig]]:
    """Ignore the original layout: pack figures n-up, width-weighted."""
    rows: list[list[Fig]] = []
    cur: list[Fig] = []
    width = 0.0
    for fig in figs:
        w = max(0.05, fig.width)
        if cur and (len(cur) >= cfg.max_figures_per_row or width + w > 0.95):
            rows.append(cur)
            cur, width = [], 0.0
        cur.append(fig)
        width += w
    if cur:
        rows.append(cur)
    return rows


def size_row(figs: list[Fig], cfg, page_ar: float) -> list[float] | None:
    """Display widths (fractions of ``\\textwidth``); ``None`` if the row must split.

    ``page_ar`` = usable_height / usable_width, used for the max-height cap.
    """
    widths = [max(0.05, f.width) for f in figs]
    total = sum(widths)
    scale = min(1.0, 0.95 / total) if total > 0 else 1.0
    disp = [min(cfg.figure_max_width, scale * w) for w in widths]
    # max-height cap: displayed height / usable height = disp*aspect / page_ar
    for i, f in enumerate(figs):
        frac = disp[i] * f.aspect / max(1e-6, page_ar)
        if frac > cfg.figure_max_height:
            disp[i] = cfg.figure_max_height * page_ar / max(1e-6, f.aspect)
    if sum(disp) > 0.95:  # re-normalise after height caps
        s = 0.95 / sum(disp)
        disp = [d * s for d in disp]
    if len(figs) > 1 and min(disp) < cfg.figure_min_width:
        return None  # too small to sit next to each other -> split
    return disp


def _row_items(row: list[Fig], cfg, page_ar: float) -> list[Item]:
    """One row -> item(s); split in half if the figures would become too narrow."""
    if not row:
        return []
    widths = size_row(row, cfg, page_ar)
    if widths is not None:
        return [Item(kind="figrow", row=FigRow(row, widths))]
    mid = len(row) // 2
    if mid == 0:
        w = min(cfg.figure_max_width, max(cfg.figure_min_width, row[0].width))
        return [Item(kind="figrow", row=FigRow(row, [w]))]
    return _row_items(row[:mid], cfg, page_ar) + _row_items(row[mid:], cfg, page_ar)


def build_items(blocks: list[dict], fig_info: dict[int, dict], cfg, page_ar: float) -> list[Item]:
    """Turn a page's blocks into a layout item list (figure runs become rows)."""
    items: list[Item] = []
    i = 0
    n = len(blocks)
    while i < n:
        b = blocks[i]
        if b.get("type") == "figure" and id(b) in fig_info:
            run: list[Fig] = []
            j = i
            while j < n and blocks[j].get("type") == "figure" and id(blocks[j]) in fig_info:
                info = fig_info[id(blocks[j])]
                run.append(
                    Fig(
                        block=blocks[j],
                        bbox=tuple(info.get("bbox") or (0, 0, 0, 0)),
                        width=float(info.get("width") or 0.3),
                        aspect=float(info.get("aspect") or 1.0),
                        caption=str(info.get("caption") or ""),
                    )
                )
                j += 1
            if cfg.figure_layout == "stack":
                rows = [[f] for f in run]
            elif cfg.figure_layout == "preserve":
                rows = cluster_rows(run)
            elif cfg.figure_layout == "grid":
                rows = _grid_rows(run, cfg)
            else:  # "flow" (default): left-to-right when they comfortably fit
                rows = _flow_rows(run, cfg)
            for row in rows:
                items.extend(_row_items(row, cfg, page_ar))
            i = j
        else:
            items.append(
                Item(
                    kind="block",
                    block=b,
                    keep_with_next=bool(cfg.keep_together and b.get("type") == "heading"),
                )
            )
            i += 1
    return items


# ---------------------------------------------------------------------------
# height estimation (fallback when no measurement is available)
# ---------------------------------------------------------------------------
def estimate_height(item: Item, textw_pt: float, page_ar: float, cfg) -> float:
    if item.kind == "figrow" and item.row:
        tallest = 0.0
        for f, w in zip(item.row.figs, item.row.widths, strict=True):
            tallest = max(tallest, w * textw_pt * f.aspect)
        cap = cfg.figure_max_height * textw_pt * page_ar
        captions = sum(1 for f in item.row.figs if f.caption.strip())
        return min(tallest, cap * 1.05) + (16 if captions else 0) + 10
    b = item.block or {}
    t = b.get("type")
    src = b.get("target") or b.get("source") or ""
    cpl = max(20.0, textw_pt / 6.0)
    if t == "page_number":
        return 0.0
    if t == "heading":
        return 24.0
    if t == "list":
        return 16.0 * max(1, len(b.get("items") or [])) + 8
    if t in ("math", "table"):
        lines = (b.get("latex", "") or "").count("\\\\") + 2
        return 16.0 * lines + 10
    if t in ("quote", "theorem"):
        base = 16.0 * max(1, math.ceil(len(src) / cpl)) + 12
        return base + (16 if t == "theorem" else 8)
    paras = max(1, src.count("\n\n") + 1)
    return 15.0 * max(1, math.ceil(len(src) / cpl)) + 6 * (paras - 1) + 6


# ---------------------------------------------------------------------------
# pagination
# ---------------------------------------------------------------------------
def pack(items: list[Item], budget: float, cfg) -> list[list[Item]]:
    pages: list[list[Item]] = []
    cur: list[Item] = []
    h = 0.0
    for it in items:
        if cur and h + it.height > budget:
            carry: list[Item] = []
            while cur and cur[-1].keep_with_next:
                carry.insert(0, cur.pop())
            if cur:
                pages.append(cur)
            cur = carry
            h = sum(x.height for x in cur)
        cur.append(it)
        h += it.height
    if cur:
        pages.append(cur)
    return pages or [[]]


def merge_short_last(pages: list[list[Item]], budget: float, cfg) -> list[list[Item]]:
    """Merge a nearly-empty last page into the previous one when text stays readable."""
    while len(pages) >= 2:
        last, prev = pages[-1], pages[-2]
        h_last = sum(x.height for x in last)
        h_prev = sum(x.height for x in prev)
        if h_last >= cfg.page_fill_min * budget:
            break
        total = h_last + h_prev
        if total <= budget / max(0.3, cfg.min_page_scale):  # scale stays >= min_page_scale
            pages[-2] = prev + last
            pages.pop()
        break
    return pages


def plan_pages(
    cfg,
    blocks: list[dict],
    fig_info: dict[int, dict],
    out_w: float,
    out_h: float,
    textw_pt: float,
    measure=None,
) -> list[list[Item]]:
    """Plan the translated pages for one source page."""
    area_w = max(1.0, out_w - 2 * cfg.margin_pt)
    area_h = max(1.0, out_h - 2 * cfg.margin_pt)
    page_ar = area_h / area_w
    items = build_items(blocks, fig_info, cfg, page_ar)
    if cfg.layout_mode == "single":
        for it in items:
            it.height = estimate_height(it, textw_pt, page_ar, cfg)
        return [items]

    heights = None
    if measure is not None:
        measured = measure(items)
        if measured:
            textw_pt = float(measured.get("textw") or textw_pt)
            heights = measured.get("heights")
    if heights and len(heights) == len(items):
        for it, h in zip(items, heights, strict=True):
            it.height = max(0.0, float(h))
    else:
        for it in items:
            it.height = estimate_height(it, textw_pt, page_ar, cfg)

    budget = textw_pt * page_ar
    pages = pack(items, budget, cfg)
    return merge_short_last(pages, budget, cfg)
