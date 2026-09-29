"""Cost prediction from everything we know about a job.

The estimate is deliberately transparent: it samples the real document (page size,
average text length, and the fraction of pages that look like they contain formulas
or figures) and combines that with the job options (model, dpi/max_px, figure mode,
formula check, glossary/instruction size, output script, book chunking) to predict
tokens and price, with a low/high range.

Assumptions are returned alongside the number so nothing is a black box.
"""

from __future__ import annotations

import math
import re
from functools import lru_cache
from pathlib import Path

import fitz

from .pricing import PRICES, price_for

# --- tunable heuristics ---------------------------------------------------
IMAGE_PX_PER_TOKEN = 750  # OpenAI-ish: ~1 vision token per 750 rendered pixels
IMAGE_TOKEN_CAP = 1200  # observed vision input is modest; cap its contribution
BASE_PROMPT_TOKENS = 400  # OCR system/rules prompt
INPUT_TOKENS_PER_CALL = 800  # baseline input per OCR call (calibrated from usage)
GLOSSARY_TOKENS_PER_TERM = 12  # "- src => tgt" line, both scripts
# The model returns the whole page as JSON: source + translation + LaTeX. Observed
# completion usage is ~4-9k tokens/call, so estimate it from source text length.
OUTPUT_BASE_TOKENS = 3000
OUTPUT_CHARS_FACTOR = 4.0
CJK_OUTPUT_MULT = 1.3  # observed Chinese output runs longer
OUTPUT_MIN, OUTPUT_MAX = 1200, 16000
BBOX_CALL_TOKENS = 300  # figure bbox pass (text side)
FIG_OUTPUT_TOKENS = 150
VERIFY_INPUT_TOKENS = 350  # formula re-check prompt + listing
VERIFY_OUTPUT_TOKENS = 200
ANNOT_INPUT_TOKENS = 600  # in-math annotation translation
ANNOT_OUTPUT_TOKENS = 120
ROLL_GLOSSARY_INPUT = 2500  # rolling glossary pass per chunk
ROLL_GLOSSARY_OUTPUT = 300
RETRY_FACTOR = 1.05  # occasional retries/failed pages
CJK_FACTORS = ("chinese", "japanese", "korean")


def estimate_image_tokens(page_w: float, page_h: float, dpi: int, max_px: int) -> int:
    """Vision input tokens for one rendered page (rough)."""
    if page_w <= 0 or page_h <= 0:
        page_w, page_h = 612, 792
    zoom = dpi / 72.0
    if max_px:
        zoom = min(zoom, max_px / max(page_w, page_h))
    px = (page_w * zoom) * (page_h * zoom)
    return max(85, int(px / IMAGE_PX_PER_TOKEN))


def is_cjk(target_lang: str) -> bool:
    low = (target_lang or "").lower()
    return any(k in low for k in CJK_FACTORS)


_MATH_CHARS = re.compile(r"[=≤≥≠≈∑∏∫√∞±×÷∘→⇒∈∀∃⊂⊆∪∩πθαβγδλμσ]")
_DRAWING_HINT = 25  # a page with this many vector ops is probably a figure


@lru_cache(maxsize=64)
def _analyze_cached(path: str, _mtime: float, sample: int = 8) -> dict:
    with fitz.open(path) as doc:
        n = doc.page_count
        if n == 0:
            return {
                "n_pages": 0,
                "page_w": 612,
                "page_h": 792,
                "avg_chars_per_page": 1200,
                "formula_fraction": 0.0,
                "figure_fraction": 0.0,
                "kind": "empty",
            }
        sizes: dict[tuple[int, int], int] = {}
        for p in doc:
            key = (round(p.rect.width), round(p.rect.height))
            sizes[key] = sizes.get(key, 0) + 1
        (pw, ph), _ = max(sizes.items(), key=lambda kv: kv[1])
        step = max(1, n // max(1, sample))
        idxs = list(range(0, n, step))[:sample]
        chars, math_pages, fig_pages = [], 0, 0
        for i in idxs:
            page = doc[i]
            text = page.get_text()
            chars.append(len(text))
            if len(_MATH_CHARS.findall(text)) >= 3:
                math_pages += 1
            try:
                if page.get_images() or len(page.get_drawings()) > _DRAWING_HINT:
                    fig_pages += 1
            except Exception:  # noqa: BLE001
                pass
        got = max(1, len(idxs))
        kind = "scan" if sum(chars) < 20 * got else "text"
        avg = sum(chars) / got
        if kind == "scan":
            avg = max(avg, 1200)  # can't read scanned text; assume a normal page
        return {
            "n_pages": n,
            "page_w": float(pw),
            "page_h": float(ph),
            "avg_chars_per_page": float(avg),
            "formula_fraction": math_pages / got,
            "figure_fraction": fig_pages / got,
            "kind": kind,
        }


def analyze_document(pdf: str | Path, sample: int = 8) -> dict:
    p = Path(pdf)
    try:
        mtime = p.stat().st_mtime
    except OSError:
        mtime = 0.0
    return dict(_analyze_cached(str(p), mtime, sample))


def predict_cost(
    model: str,
    n_pages: int,
    *,
    page_w: float = 612,
    page_h: float = 792,
    dpi: int = 150,
    max_px: int = 1800,
    verify_math: bool = False,
    figure_mode: str = "tight",
    glossary_terms: int = 0,
    extra_chars: int = 0,
    target_lang: str = "English",
    formula_fraction: float = 0.5,
    figure_fraction: float = 0.15,
    avg_chars_per_page: float = 1500,
    chunk_size: int = 0,
    rolling_glossary: bool = False,
    retry_factor: float = RETRY_FACTOR,
) -> dict:
    """Predict calls/tokens/USD for a job (or a book's worth of pages)."""
    pin, pout = price_for(model)
    n_pages = max(0, int(n_pages))
    formula_pages = round(n_pages * max(0.0, min(1.0, formula_fraction)))
    figure_pages = round(n_pages * max(0.0, min(1.0, figure_fraction)))
    img = estimate_image_tokens(page_w, page_h, dpi, max_px)
    prompt_extra = BASE_PROMPT_TOKENS + (glossary_terms * GLOSSARY_TOKENS_PER_TERM + extra_chars) // 4
    in_per_call = INPUT_TOKENS_PER_CALL + prompt_extra + min(img, IMAGE_TOKEN_CAP) // 4
    base_out = OUTPUT_BASE_TOKENS + avg_chars_per_page * OUTPUT_CHARS_FACTOR
    if is_cjk(target_lang):
        base_out *= CJK_OUTPUT_MULT
    out_per_page = max(OUTPUT_MIN, min(OUTPUT_MAX, base_out))

    # (calls, input_tokens, output_tokens) per bucket
    buckets: dict[str, tuple[int, int, int]] = {}
    buckets["ocr_translation"] = (n_pages, n_pages * in_per_call, int(n_pages * out_per_page))
    per_fig = {"tight": 1, "judge": 2}.get(figure_mode, 0)
    buckets["figure_passes"] = (
        figure_pages * per_fig,
        figure_pages * per_fig * (in_per_call + BBOX_CALL_TOKENS),
        figure_pages * per_fig * FIG_OUTPUT_TOKENS,
    )
    buckets["formula_check"] = (
        formula_pages if verify_math else 0,
        (formula_pages if verify_math else 0) * (in_per_call + VERIFY_INPUT_TOKENS),
        (formula_pages if verify_math else 0) * VERIFY_OUTPUT_TOKENS,
    )
    buckets["annotations"] = (
        formula_pages,
        formula_pages * ANNOT_INPUT_TOKENS,
        formula_pages * ANNOT_OUTPUT_TOKENS,
    )
    if rolling_glossary and chunk_size > 0:
        n_chunks = math.ceil(n_pages / chunk_size) if n_pages else 0
        buckets["rolling_glossary"] = (
            n_chunks,
            n_chunks * ROLL_GLOSSARY_INPUT,
            n_chunks * ROLL_GLOSSARY_OUTPUT,
        )

    def usd(inp: int, out: int) -> float:
        return inp / 1e6 * pin + out / 1e6 * pout

    breakdown = {k: round(usd(i, o), 6) for k, (_c, i, o) in buckets.items()}
    subtotal = sum(breakdown.values())
    total = subtotal * retry_factor
    calls = sum(c for c, _i, _o in buckets.values())
    inp = sum(i for _c, i, _o in buckets.values())
    out = sum(o for _c, _i, o in buckets.values())
    return {
        "model": model,
        "pages": n_pages,
        "est_calls": calls,
        "est_prompt_tokens": inp,
        "est_completion_tokens": out,
        "est_cost_usd": round(total, 4),
        "est_cost_low": round(total * 0.6, 4),
        "est_cost_high": round(total * 1.7, 4),
        "breakdown": {**breakdown, "retry_overhead": round(subtotal * (retry_factor - 1), 6)},
        "assumptions": {
            "image_tokens_per_page": img,
            "input_tokens_per_call": in_per_call,
            "output_tokens_per_page": round(out_per_page),
            "formula_pages": formula_pages,
            "figure_pages": figure_pages,
            "price_in_per_mtok": pin,
            "price_out_per_mtok": pout,
            "target_script": "cjk" if is_cjk(target_lang) else "latin",
            "figure_mode": figure_mode,
            "verify_math": verify_math,
        },
        "prices": {m: {"in": p[0], "out": p[1]} for m, p in PRICES.items()},
    }
