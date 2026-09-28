"""Vision OCR: render pages, call the model, parse structured blocks."""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import cache, paths, render
from .config import PipelineConfig
from .events import CancelToken, Emitter, Event, emit
from .providers import Provider

# Prompts use plain placeholders (not str.format) to avoid brace-escaping bugs.
OCR_PROMPT = """You are an expert OCR and translation engine.
Transcribe this page (written in __SRC__) and translate its natural language into __TGT__.
Return STRICT JSON (no markdown fences):
{
 "blocks": [
   {"type":"heading"|"prose"|"math"|"table"|"figure",
    "source":"...",        // heading/prose: exact __SRC__ text, inline math in $...$
    "target":"...",        // heading/prose: __TGT__ translation, inline $...$ unchanged
    "latex":"...",         // math: exact LaTeX; table: complete LaTeX array/tabular
    "description":"...",   // figure: what it shows
    "bbox":[x0,y0,x1,y1]   // figure: tight box as fractions of the page (0..1)
   }
 ]
}
Rules:
- Preserve reading order top to bottom.
- NEVER translate or alter mathematics; keep every symbol identical.
- Reproduce every formula exactly: fractions, roots, exponents, subscripts,
  matrices, set notation, vectors. Use standard amsmath.
- Keep inline math inside $...$ in both "source" and "target".
- For tables output a complete \\begin{array}...\\end{array}.
- For figures give an accurate tight "bbox".
- Return only the JSON.
"""

BBOX_PROMPT = """Look at this page image. Identify each FIGURE/GRAPH (a plotted chart
with axes - not a table, not a formula, not text).
Return strict JSON: {"figures":[{"index":1,"description":"...","bbox":[x0,y0,x1,y1]}]}
bbox is the tight box around ONLY that figure as fractions of the page (0..1).
Do not include surrounding paragraphs, tables or headings. Return only JSON.
"""


def parse_json(txt: str | None) -> dict | None:
    """Tolerant JSON extraction (models sometimes wrap output in fences/prose)."""
    if not txt:
        return None
    txt = txt.strip()
    txt = re.sub(r"^```[a-zA-Z]*\s*", "", txt)
    txt = re.sub(r"\s*```$", "", txt)
    i, j = txt.find("{"), txt.rfind("}")
    if i < 0 or j < 0:
        return None
    try:
        return json.loads(txt[i : j + 1])
    except json.JSONDecodeError:
        return None


def build_ocr_prompt(source_lang: str, target_lang: str, glossary=None, do_not_translate=None) -> str:
    prompt = OCR_PROMPT.replace("__SRC__", source_lang).replace("__TGT__", target_lang)
    if glossary:
        lines = [f"- {g.get('source', '')} => {g.get('target', '')}" for g in glossary if g.get("source")]
        if lines:
            prompt += (
                "\nGlossary - use these translations exactly when the term appears:\n"
                + "\n".join(lines)
                + "\n"
            )
    if do_not_translate:
        prompt += "\nDo NOT translate these terms; keep them verbatim: " + ", ".join(do_not_translate) + "\n"
    return prompt


def ocr_image(
    provider: Provider,
    image_path: str | Path,
    source_lang: str,
    target_lang: str,
    max_tokens: int = 16000,
    want_bbox: bool = True,
    glossary=None,
    do_not_translate=None,
) -> dict:
    """One page -> ``{"blocks": [...], "tight": [bbox, ...]}``."""
    prompt = build_ocr_prompt(source_lang, target_lang, glossary, do_not_translate)
    obj = parse_json(provider.vision(image_path, prompt, max_tokens))
    if obj is None:  # single retry, as empty responses happen
        obj = parse_json(provider.vision(image_path, prompt, max_tokens))
    blocks = (obj or {}).get("blocks", []) or []
    tight: list[list[float]] = []
    if want_bbox and any(b.get("type") == "figure" for b in blocks):
        bj = parse_json(provider.vision(image_path, BBOX_PROMPT, 3000))
        if bj and isinstance(bj.get("figures"), list):
            tight = [f.get("bbox") for f in bj["figures"] if f.get("bbox")]
    return {"blocks": blocks, "tight": tight}


def run_ocr(
    cfg: PipelineConfig,
    provider: Provider,
    on_event: Emitter | None = None,
    cancel: CancelToken | None = None,
) -> dict[str, list[dict]]:
    """OCR every source document. Returns ``{base: [page_entry, ...]}``."""
    cancel = cancel or CancelToken()
    results: dict[str, list[dict]] = {}
    sources = cfg.resolve_sources()
    emit(on_event, Event("ocr", "started", total=len(sources)))

    for si, source in enumerate(sources):
        base = Path(source).stem
        doc_sha = cache.file_sha256(source)
        images = render.render_pages(source, paths.pages_dir(cfg.workdir, base), cfg.dpi, cfg.max_px)
        prior = {e["page"]: e for e in (cache.load_json(paths.ocr_json(cfg.workdir, base)) or [])}
        entries: list[dict] = []

        for pi, img in enumerate(images, start=1):
            cancel.check()
            emit(
                on_event,
                Event("ocr", "progress", base=base, page=pi, index=pi, total=len(images), message="page"),
            )
            key = cache.ocr_cache_key(doc_sha, pi, cfg.model, cfg.prompt_version)
            cpath = cache.cache_path(cfg.workdir, key)
            cached = None if cfg.force else cache.load_json(cpath)
            if not cached and not cfg.force:
                old = prior.get(pi)
                if old and old.get("blocks"):
                    cached = old
            try:
                if cached and cached.get("blocks"):
                    result = {"blocks": cached["blocks"], "tight": cached.get("tight", [])}
                else:
                    result = ocr_image(
                        provider,
                        img,
                        cfg.source_lang,
                        cfg.target_lang,
                        cfg.max_tokens,
                        glossary=cfg.glossary,
                        do_not_translate=cfg.do_not_translate,
                    )
                    cache.save_json(cpath, result)
                entry = {"page": pi, "img": str(img), **result}
            except Exception as exc:  # noqa: BLE001 - recorded per page
                entry = {"page": pi, "img": str(img), "blocks": [], "tight": [], "error": str(exc)}
                emit(on_event, Event("ocr", "error", base=base, page=pi, message=str(exc)))
            entries.append(entry)
            cache.save_json(paths.ocr_json(cfg.workdir, base), entries)

        results[base] = entries
        empties = [e["page"] for e in entries if not e["blocks"]]
        emit(
            on_event,
            Event(
                "ocr",
                "ok",
                base=base,
                index=si + 1,
                total=len(sources),
                data={"pages": len(entries), "empty": empties},
            ),
        )
    return results
