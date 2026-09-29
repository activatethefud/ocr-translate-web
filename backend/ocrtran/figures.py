"""Figure detection and bounding-box refinement.

Strategies for the *tight* box, chosen with ``PipelineConfig.figure_mode``:

* ``off``   — trust the main OCR call's box (1 model call per page; fastest).
* ``tight`` — a dedicated second call returns tight boxes.
* ``judge`` — independent **detection** pass + an LLM **judge** that sees the drawn
  boxes and corrects them (also catches figures the main call missed).

Detection gets a labeled percentage **grid** drawn on the page to localise precisely.
All boxes are normalised ``[x0, y0, x1, y1]`` in 0..1.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from PIL import Image, ImageDraw

from .geometry import union_bbox
from .jsonutil import parse_json
from .providers import Provider

BBOX_PROMPT = """Look at this page image. Identify each FIGURE/GRAPH (a plotted chart
with axes - not a table, not a formula, not text).
Return strict JSON: {"figures":[{"index":1,"description":"...","bbox":[x0,y0,x1,y1]}]}
bbox is the tight box around ONLY that figure as fractions of the page (0..1).
Do not include surrounding paragraphs, tables or headings. Return only JSON.
"""

DETECT_PROMPT = """You are a precise document-figure detector. The page has a light
percentage grid: vertical lines at 10..90 (labels along the top), horizontal lines at
10..90 (labels along the left). Use it to localise accurately.
Find EVERY figure, diagram, illustration, drawing, chart or graph on the page -
including figures that have no caption. For each, return a box that FULLY contains the
figure (including its labels/axis text and caption when directly attached) but no
surrounding paragraphs, page headers/footers, or table rules.
Return strict JSON:
{"figures":[{"bbox":[x0,y0,x1,y1],"description":"...","caption":"..."}]}
Boxes are fractions of the page (0..1). Return only JSON.
"""

JUDGE_PROMPT = """You are a strict reviewer of figure bounding boxes on this page.
Each figure below already has a candidate box, but some are slightly too tight and
clip the diagram. Look at the page and return a corrected box that FULLY contains the
figure - when in doubt, prefer a little extra margin over cutting anything off.
Boxes are fractions of the page [x0,y0,x1,y1] (0..1).
Return strict JSON: {"figures":[{"index":<index>,"bbox":[x0,y0,x1,y1]}]}
Figures:
{figures}
Return only JSON.
"""

JUDGE_DRAWN_PROMPT = """The page image has candidate figure boxes drawn in red and
numbered. For EACH numbered box, decide whether it is a real figure and whether the box
is correct, then return a corrected box that FULLY contains the figure (a little extra
margin is better than cutting anything off), or keep=false if it is not a figure
(e.g. it is body text or a formula). Also, if the page contains an obvious figure that
has NO box, add it.
Return strict JSON:
{"figures":[{"index":<i>,"keep":true|false,"bbox":[x0,y0,x1,y1],"caption":"..."}]}
Boxes are fractions of the page (0..1).
Candidates:
{figures}
Return only JSON.
"""


def _valid_box(b) -> bool:
    return (
        isinstance(b, (list, tuple))
        and len(b) == 4
        and all(isinstance(v, (int, float)) for v in b)
        and b[2] > b[0]
        and b[3] > b[1]
    )


def iou(a, b) -> float:
    """Intersection-over-union of two normalised boxes."""
    if not a or not b:
        return 0.0
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    area = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / area if area > 0 else 0.0


# -- image variants --------------------------------------------------------
def grid_variant(image_path: str | Path, max_side: int = 1400) -> str:
    """Page image with a labeled 10% grid (cached next to the original)."""
    src = Path(image_path)
    out = src.with_name(src.name + ".grid.png")
    if out.exists():
        return str(out)
    try:
        im = Image.open(src).convert("RGB")
    except (OSError, ValueError):
        return str(src)  # not a decodable image; send as-is
    im.thumbnail((max_side, max_side))
    d = ImageDraw.Draw(im)
    w, h = im.size
    for k in range(1, 10):
        x, y = int(w * k / 10), int(h * k / 10)
        d.line([(x, 0), (x, h)], fill=(255, 150, 0), width=1)
        d.line([(0, y), (w, y)], fill=(255, 150, 0), width=1)
        d.text((x + 2, 2), str(k * 10), fill=(200, 0, 0))
        d.text((2, y + 2), str(k * 10), fill=(200, 0, 0))
    im.save(out)
    return str(out)


def boxes_variant(image_path: str | Path, boxes: list, max_side: int = 1400) -> str:
    """Page image with candidate boxes drawn + numbered (for the judge)."""
    src = Path(image_path)
    key = hashlib.sha256(repr(boxes).encode()).hexdigest()[:10]
    out = src.with_name(src.name + f".boxes.{key}.png")
    if out.exists():
        return str(out)
    try:
        im = Image.open(src).convert("RGB")
    except (OSError, ValueError):
        return str(src)
    im.thumbnail((max_side, max_side))
    d = ImageDraw.Draw(im)
    w, h = im.size
    for i, bb in enumerate(boxes):
        if not _valid_box(bb):
            continue
        r = [bb[0] * w, bb[1] * h, bb[2] * w, bb[3] * h]
        d.rectangle(r, outline=(220, 0, 0), width=2)
        d.text((r[0] + 2, r[1] + 2), str(i), fill=(220, 0, 0))
    im.save(out)
    return str(out)


# -- model passes ----------------------------------------------------------
def detect_figures(provider: Provider, image_path: str | Path, max_tokens: int = 2500) -> list[dict]:
    """Independent detection of every figure on the page (gridded image)."""
    obj = parse_json(provider.vision(grid_variant(image_path), DETECT_PROMPT, max_tokens))
    out: list[dict] = []
    if obj and isinstance(obj.get("figures"), list):
        for item in obj["figures"]:
            bb = item.get("bbox")
            if _valid_box(bb):
                out.append(
                    {
                        "bbox": [float(v) for v in bb],
                        "description": str(item.get("description", ""))[:300],
                        "caption": str(item.get("caption", ""))[:200],
                    }
                )
    return out


def refine_figures(
    provider: Provider, image_path: str | Path, candidates: list[dict], max_tokens: int = 2500
) -> list[dict]:
    """Judge pass: see the drawn boxes and return corrections / keep flags."""
    if not candidates:
        return []
    variant = boxes_variant(image_path, [c.get("bbox") for c in candidates])
    listing = "\n".join(f"{i}: {str(c.get('description', 'figure'))[:120]}" for i, c in enumerate(candidates))
    obj = parse_json(provider.vision(variant, JUDGE_DRAWN_PROMPT.replace("{figures}", listing), max_tokens))
    out: list[dict] = []
    if obj and isinstance(obj.get("figures"), list):
        for item in obj["figures"]:
            try:
                idx = int(item["index"])
            except (KeyError, TypeError, ValueError):
                continue
            if 0 <= idx < len(candidates):
                out.append(
                    {
                        "index": idx,
                        "keep": bool(item.get("keep", True)),
                        "bbox": ([float(v) for v in item["bbox"]] if _valid_box(item.get("bbox")) else None),
                        "caption": item.get("caption"),
                    }
                )
    return out


def judge_bboxes(
    provider: Provider, image_path: str, figures: list[dict], max_tokens: int = 2000
) -> list[list[float] | None]:
    """Text-only variant of the judge (kept for backwards compatibility)."""
    if not figures:
        return []
    lines = []
    for i, fig in enumerate(figures):
        desc = str(fig.get("description", "figure"))[:200].replace("\n", " ")
        lines.append(f"{i}: {desc} current={fig.get('bbox')}")
    prompt = JUDGE_PROMPT.replace("{figures}", "\n".join(lines))
    obj = parse_json(provider.vision(image_path, prompt, max_tokens))
    if not obj or not isinstance(obj.get("figures"), list):
        return [None] * len(figures)
    out: list[list[float] | None] = [None] * len(figures)
    for item in obj["figures"]:
        try:
            idx = int(item["index"])
            box = item["bbox"]
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= idx < len(figures) and _valid_box(box):
            out[idx] = [float(v) for v in box]
    return out


def apply_judge(
    candidates: list[list[float] | None], judged: list[list[float] | None]
) -> list[list[float] | None]:
    """Union each candidate with the judged box (monotonic: never shrinks)."""
    return [union_bbox(c, judged[i] if i < len(judged) else None) for i, c in enumerate(candidates)]


# -- full page pipeline ----------------------------------------------------
def refine_page(
    provider: Provider, image_path: str | Path, blocks: list[dict], candidate_boxes: list
) -> tuple[list[dict], list]:
    """Detect + judge all figures on a page.

    Returns ``(blocks, tight)`` where ``tight`` aligns with the figure blocks in
    ``blocks`` order (extras appended as new figure blocks). Never shrinks a box.
    """
    fig_blocks = [b for b in blocks if b.get("type") == "figure"]
    merged: list[dict] = []
    for i, blk in enumerate(fig_blocks):
        cand = candidate_boxes[i] if i < len(candidate_boxes) and candidate_boxes[i] else blk.get("bbox")
        merged.append(
            {
                "bbox": cand,
                "description": blk.get("description", ""),
                "caption": blk.get("caption", ""),
                "block": blk,
            }
        )

    detections = detect_figures(provider, image_path)
    used = [False] * len(detections)
    for cand in merged:
        best_i, best_iou = -1, 0.0
        for j, det in enumerate(detections):
            if used[j]:
                continue
            v = iou(cand["bbox"], det["bbox"])
            if v > best_iou:
                best_i, best_iou = j, v
        if best_i >= 0 and best_iou >= 0.2:
            used[best_i] = True
            det = detections[best_i]
            cand["bbox"] = union_bbox(cand["bbox"], det["bbox"])
            cand["description"] = cand["description"] or det["description"]
            cand["caption"] = cand["caption"] or det["caption"]
    for j, det in enumerate(detections):
        if not used[j]:
            merged.append(
                {
                    "bbox": det["bbox"],
                    "description": det["description"],
                    "caption": det["caption"],
                    "block": None,
                }
            )

    if not merged:
        return blocks, []

    refined = {r["index"]: r for r in refine_figures(provider, image_path, merged)}
    final: list[dict] = []
    for i, cand in enumerate(merged):
        r = refined.get(i)
        if r is not None:
            if r.get("keep") is False:
                continue
            cand["bbox"] = union_bbox(cand["bbox"], r.get("bbox"))
            if r.get("caption"):
                cand["caption"] = str(r["caption"])[:200]
        if cand.get("bbox"):
            final.append(cand)

    by_block = {id(c["block"]): c for c in final if c["block"] is not None}
    new_blocks: list[dict] = []
    tight: list = []
    for blk in blocks:
        if blk.get("type") != "figure":
            new_blocks.append(blk)
            continue
        c = by_block.get(id(blk))  # dropped if the judge said keep=false
        if c is None:
            continue
        if c.get("caption"):
            blk["caption"] = c["caption"]
        if c.get("description") and not blk.get("description"):
            blk["description"] = c["description"]
        new_blocks.append(blk)
        tight.append(c["bbox"])
    for c in final:
        if c["block"] is None:
            new_blocks.append(
                {"type": "figure", "description": c.get("description", ""), "caption": c.get("caption", "")}
            )
            tight.append(c["bbox"])
    return new_blocks, tight


def sanitize_box(bb, img_w: float | None = None, img_h: float | None = None):
    """Return a box clamped to 0..1, or ``None`` if it can't be made sensible.

    Models occasionally return pixel coordinates (e.g. ``[50,180,950,720]``) or
    percentages instead of fractions. Those are normalised here so we never build a
    degenerate crop (which used to write a ``\\includegraphics`` for a missing file
    and fail the whole page).
    """
    if not _valid_box(bb):
        return None
    x0, y0, x1, y1 = (min(bb[0], bb[2]), min(bb[1], bb[3]), max(bb[0], bb[2]), max(bb[1], bb[3]))
    hi = max(x1, y1)
    if hi <= 1.05:
        pass  # already fractions
    elif 1.05 < hi <= 100.5:
        x0, y0, x1, y1 = x0 / 100, y0 / 100, x1 / 100, y1 / 100  # percentages
    elif img_w and img_h:
        x0, y0, x1, y1 = x0 / img_w, y0 / img_h, x1 / img_w, y1 / img_h  # pixels
    else:
        return None
    x0, y0 = max(0.0, min(1.0, x0)), max(0.0, min(1.0, y0))
    x1, y1 = max(0.0, min(1.0, x1)), max(0.0, min(1.0, y1))
    if x1 - x0 < 0.02 or y1 - y0 < 0.02:
        return None
    return [x0, y0, x1, y1]


def sanitize_result(result: dict, image_path) -> dict:
    """Normalise all figure boxes in an OCR result (works for cached results too)."""
    try:
        with Image.open(image_path) as im:
            w, h = im.size
    except Exception:  # noqa: BLE001
        w = h = None
    for b in result.get("blocks", []):
        if b.get("type") == "figure" and b.get("bbox"):
            b["bbox"] = sanitize_box(b["bbox"], w, h)
    result["tight"] = [sanitize_box(b, w, h) for b in (result.get("tight") or [])]
    return result
