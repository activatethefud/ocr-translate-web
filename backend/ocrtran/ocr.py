"""Vision OCR: render pages, call the model, parse structured blocks."""

from __future__ import annotations

import threading
from pathlib import Path

from . import cache, figures, paths, render
from .concurrency import parallel_map
from .config import PipelineConfig
from .events import CancelToken, Emitter, Event, emit
from .jsonutil import parse_json  # re-exported for callers/tests
from .pages import parse_page_spec
from .providers import Provider
from .throttle import THROTTLE

# Prompts use plain placeholders (not str.format) to avoid brace-escaping bugs.
OCR_PROMPT = """You are an expert OCR and translation engine.
Transcribe this page (written in __SRC__) and translate its natural language into __TGT__.
Return STRICT JSON (no markdown fences):
{
 "blocks": [
   {"type":"heading","level":1,"source":"...","target":"..."},
   {"type":"prose","source":"...","target":"..."},
   {"type":"list","ordered":true,"items":[{"source":"...","target":"..."}]},
   {"type":"math","latex":"...","number":"(1)"},
   {"type":"table","latex":"..."},
   {"type":"figure","description":"...","bbox":[x0,y0,x1,y1],"caption":"..."},
   {"type":"quote","source":"...","target":"..."},
   {"type":"page_number","text":"5"},
   {"type":"theorem","kind":"Theorem","name":"...","source":"...","target":"..."}
 ]
}
Rules:
- Preserve reading order top to bottom.
- Use "list" for EVERY bulleted or numbered list. Put each item in its own "items"
  entry. NEVER merge a list into one prose block or one line. Set "ordered" true for
  numbered lists.
- In "prose", separate distinct paragraphs with a blank line (\n\n); never merge
  separate paragraphs into one.
- "heading": set "level" (1 = document/chapter title, 2 = section, 3 = subsection).
- "math": standalone/display equations; set "number" (e.g. "(1)") only if numbered.
- "theorem": use for theorem/definition/example/proof/lemma/proposition/remark
  environments; set "kind" and, when named, "name".
- NEVER translate or alter mathematics; keep every symbol identical.
- Reproduce every formula exactly: fractions, roots, exponents, subscripts,
  matrices, set notation, vectors. Use standard amsmath.
- Keep inline math inside $...$ in both "source" and "target".
- For tables output a complete \\begin{array}...\\end{array}.
- If the original page shows a page number (usually in a header or footer), add exactly
  one `page_number` block with "text" set to the number exactly as printed. Do not
  translate it or convert the digits, and do not also include it as prose. If there is
  no page number on the page, do not add this block.
- For figures give an accurate tight "bbox" and, if present, a "caption".
- Return only the JSON.
"""

BBOX_PROMPT = figures.BBOX_PROMPT  # kept for backwards compatibility


def build_ocr_prompt(
    source_lang: str, target_lang: str, glossary=None, do_not_translate=None, instructions=None
) -> str:
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
    if instructions and instructions.strip():
        prompt += (
            "\nAdditional instructions from the user (follow them, but never "
            "alter mathematics or the JSON schema above):\n" + instructions.strip() + "\n"
        )
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
    instructions=None,
    figure_mode: str = "tight",
) -> dict:
    """One page -> ``{"blocks": [...], "tight": [bbox, ...]}``.

    ``figure_mode`` controls the extra calls: ``off`` (none), ``tight`` (a dedicated
    bbox pass) or ``judge`` (tight boxes reviewed/expanded by the model).
    """
    prompt = build_ocr_prompt(source_lang, target_lang, glossary, do_not_translate, instructions)
    obj = parse_json(provider.vision(image_path, prompt, max_tokens))
    if obj is None or not obj.get("blocks"):  # retry on invalid/empty responses
        obj2 = parse_json(provider.vision(image_path, prompt, max_tokens))
        if obj2 and obj2.get("blocks"):
            obj = obj2
    blocks = (obj or {}).get("blocks", []) or []
    try:
        from PIL import Image

        with Image.open(image_path) as _im:
            img_w, img_h = _im.size
    except Exception:  # noqa: BLE001 - not a decodable image
        img_w = img_h = None
    for b in blocks:
        if b.get("type") == "figure" and b.get("bbox"):
            b["bbox"] = figures.sanitize_box(b["bbox"], img_w, img_h)
    figs = [b for b in blocks if b.get("type") == "figure"]
    tight: list[list[float] | None] = []

    if want_bbox and figs and figure_mode in ("tight", "judge"):
        bj = parse_json(provider.vision(image_path, figures.BBOX_PROMPT, 3000))
        if bj and isinstance(bj.get("figures"), list):
            tight = [figures.sanitize_box(f["bbox"], img_w, img_h) for f in bj["figures"] if f.get("bbox")]

    if want_bbox and figure_mode == "judge":
        # independent detection + LLM judge (runs even if the main call found no
        # figures, so missed figures get added)
        blocks, tight = figures.refine_page(provider, image_path, blocks, tight)

    out: dict = {"blocks": blocks, "tight": [b for b in tight if b]}
    if not blocks:
        out["error"] = "empty model response (no blocks)"
    return out


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
    THROTTLE.ensure_ceiling(cfg.concurrency)
    emit(on_event, Event("ocr", "started", total=len(sources)))

    for si, source in enumerate(sources):
        base = Path(source).stem
        doc_sha = cache.file_sha256(source)
        n_pages = render.page_count(source)
        selected = parse_page_spec(cfg.pages, n_pages)
        images = render.render_pages(
            source, paths.pages_dir(cfg.workdir, base), cfg.dpi, cfg.max_px, pages=selected
        )
        prior = {e["page"]: e for e in (cache.load_json(paths.ocr_json(cfg.workdir, base)) or [])}
        cache_root = cfg.cache_dir or cfg.workdir
        total_pages = len(images)
        collected: dict[int, dict] = {}
        lock = threading.Lock()
        ocr_path = paths.ocr_json(cfg.workdir, base)

        def process(
            task: tuple[int, int, object],
            *,
            source=source,
            base=base,
            doc_sha=doc_sha,
            prior=prior,
            cache_root=cache_root,
            total_pages=total_pages,
            lock=lock,
            collected=collected,
            ocr_path=ocr_path,
        ) -> dict:
            idx, pi, img = task
            cancel.check()
            emit(
                on_event,
                Event("ocr", "progress", base=base, page=pi, index=idx, total=total_pages, message="page"),
            )
            if cfg.skip_blank_pages and render.is_blank(source, pi, cfg.blank_threshold):
                entry = {"page": pi, "img": str(img), "blocks": [], "tight": [], "blank": True}
                with lock:
                    collected[pi] = entry
                    cache.save_json(ocr_path, [collected[k] for k in sorted(collected)])
                emit(
                    on_event,
                    Event(
                        "ocr",
                        "skipped",
                        base=base,
                        page=pi,
                        index=idx,
                        total=total_pages,
                        message="blank page",
                    ),
                )
                return entry
            sig = cache.prompt_sig(
                cfg.prompt_version,
                cfg.glossary,
                cfg.do_not_translate,
                cfg.llm_instructions,
                cfg.source_lang,
                cfg.target_lang,
                cfg.figure_mode,
            )
            key = cache.ocr_cache_key(doc_sha, pi, cfg.model, sig)
            cpath = cache.cache_path(cache_root, key)
            cached = None if cfg.force else cache.load_json(cpath)
            if not cached and not cfg.force:
                old = prior.get(pi)
                if old and old.get("blocks"):
                    cached = old
            try:
                if cached and cached.get("blocks"):
                    result = {"blocks": cached["blocks"], "tight": cached.get("tight", [])}
                else:
                    # gate model calls through the shared adaptive limiter so a
                    # provider 429 backs off every page/job, not just this one
                    with THROTTLE:
                        result = ocr_image(
                            provider,
                            img,
                            cfg.source_lang,
                            cfg.target_lang,
                            cfg.max_tokens,
                            glossary=list(cfg.glossary or []) + list(cfg.auto_glossary or []),
                            do_not_translate=cfg.do_not_translate,
                            instructions=cfg.llm_instructions,
                            figure_mode=cfg.figure_mode,
                        )
                    cache.save_json(cpath, result)
                figures.sanitize_result(result, img)
                entry = {"page": pi, "img": str(img), **result}
            except Exception as exc:  # noqa: BLE001 - recorded per page
                entry = {"page": pi, "img": str(img), "blocks": [], "tight": [], "error": str(exc)}
                emit(on_event, Event("ocr", "error", base=base, page=pi, message=str(exc)))
            with lock:
                collected[pi] = entry
                cache.save_json(ocr_path, [collected[k] for k in sorted(collected)])
            emit(
                on_event,
                Event(
                    "ocr",
                    "ok" if entry["blocks"] else "warn",
                    base=base,
                    page=pi,
                    index=idx,
                    total=total_pages,
                    data={"blocks": len(entry["blocks"]), "cached": bool(cached)},
                ),
            )
            return entry

        tasks = [(idx, pi, img) for idx, (pi, img) in enumerate(images, start=1)]
        parallel_map(process, tasks, cfg.concurrency)
        entries = [collected[k] for k in sorted(collected)]
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
