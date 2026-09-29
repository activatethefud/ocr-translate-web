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
    if obj is None:  # single retry, as empty responses happen
        obj = parse_json(provider.vision(image_path, prompt, max_tokens))
    blocks = (obj or {}).get("blocks", []) or []
    figs = [b for b in blocks if b.get("type") == "figure"]
    tight: list[list[float] | None] = []

    if want_bbox and figs and figure_mode in ("tight", "judge"):
        bj = parse_json(provider.vision(image_path, figures.BBOX_PROMPT, 3000))
        if bj and isinstance(bj.get("figures"), list):
            tight = [f.get("bbox") for f in bj["figures"] if f.get("bbox")]

    if want_bbox and figs and figure_mode == "judge":
        candidates = [
            tight[i] if i < len(tight) and tight[i] else figs[i].get("bbox") for i in range(len(figs))
        ]
        judged = figures.judge_bboxes(
            provider,
            str(image_path),
            [{"bbox": c, "description": figs[i].get("description", "")} for i, c in enumerate(candidates)],
        )
        tight = figures.apply_judge(candidates, judged)

    return {"blocks": blocks, "tight": [b for b in tight if b]}


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
            sig = cache.prompt_sig(
                cfg.prompt_version, cfg.glossary, cfg.do_not_translate, cfg.llm_instructions
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
                    result = ocr_image(
                        provider,
                        img,
                        cfg.source_lang,
                        cfg.target_lang,
                        cfg.max_tokens,
                        glossary=cfg.glossary,
                        do_not_translate=cfg.do_not_translate,
                        instructions=cfg.llm_instructions,
                        figure_mode=cfg.figure_mode,
                    )
                    cache.save_json(cpath, result)
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
