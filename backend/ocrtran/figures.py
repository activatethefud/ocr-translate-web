"""Figure bounding-box refinement.

Two strategies for the *tight* box, chosen with ``PipelineConfig.figure_mode``:

* ``off``   — trust the main OCR call's box (1 model call per page; fastest).
* ``tight`` — a dedicated second call returns tight boxes (the default).
* ``judge`` — the tight boxes are then reviewed by the model acting as a judge,
  which is asked to expand any box that clips the diagram. The result is the
  *union* of the candidate and the judged box, so it can only grow (never clip).

All boxes are normalised ``[x0, y0, x1, y1]`` in 0..1.
"""

from __future__ import annotations

from .geometry import union_bbox
from .jsonutil import parse_json
from .providers import Provider

BBOX_PROMPT = """Look at this page image. Identify each FIGURE/GRAPH (a plotted chart
with axes - not a table, not a formula, not text).
Return strict JSON: {"figures":[{"index":1,"description":"...","bbox":[x0,y0,x1,y1]}]}
bbox is the tight box around ONLY that figure as fractions of the page (0..1).
Do not include surrounding paragraphs, tables or headings. Return only JSON.
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


def _valid_box(b) -> bool:
    return (
        isinstance(b, (list, tuple))
        and len(b) == 4
        and all(isinstance(v, (int, float)) for v in b)
        and b[2] > b[0]
        and b[3] > b[1]
    )


def judge_bboxes(
    provider: Provider,
    image_path: str,
    figures: list[dict],
    max_tokens: int = 2000,
) -> list[list[float] | None]:
    """Ask the model to review/expand candidate boxes.

    ``figures`` is a list of ``{"bbox": ..., "description": ...}``. Returns a list
    (same length) of corrected boxes, or ``None`` where the judge had no answer.
    The caller unions the result with the candidate, so nothing shrinks.
    """
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
    out: list[list[float] | None] = []
    for i, cand in enumerate(candidates):
        judged_box = judged[i] if i < len(judged) else None
        out.append(union_bbox(cand, judged_box))
    return out
