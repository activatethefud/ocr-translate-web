"""Post-run checks: empty pages, page counts/sizes, leftover source-language words.

These are heuristics meant to *flag* pages for human review, not to be perfect.
"""

from __future__ import annotations

import re
from pathlib import Path

import fitz

from .config import PipelineConfig
from .events import CancelToken, Emitter, Event, emit
from .providers import Provider

WORD_RE = re.compile(r"[A-Za-z\u00C0-\u024F\u0400-\u04FF]{4,}")

MATH_CHECK_PROMPT = """You are verifying OCR of one page image.
Below is the list of math blocks extracted from this page. Compare them against the
image and report ONLY real discrepancies (a wrong formula, a missing/extra symbol,
a changed exponent or sign). Ignore translation, wording and formatting.
Return strict JSON: {{"ok": true|false, "issues": [{{"block": <index>, "problem": "..."}}]}}

Extracted math blocks (index: LaTeX):
{blocks}
"""


def verify_math_page(
    provider: Provider, image_path: str | Path, blocks: list[dict], max_tokens: int = 1500
) -> dict:
    """Ask a vision model whether the extracted formulas match the page image."""
    from .ocr import parse_json

    math = [
        (i, b.get("latex", ""))
        for i, b in enumerate(blocks)
        if b.get("type") in ("math", "table") and b.get("latex")
    ]
    if not math:
        return {"ok": True, "issues": [], "checked": 0}
    listing = "\n".join(f"{i}: {lx}" for i, lx in math)
    raw = provider.vision(image_path, MATH_CHECK_PROMPT.replace("{blocks}", listing), max_tokens)
    obj = parse_json(raw)
    if obj is None:
        return {
            "ok": True,
            "issues": [{"block": None, "problem": "checker returned no JSON"}],
            "checked": len(math),
        }
    return {"ok": bool(obj.get("ok", True)), "issues": obj.get("issues", []) or [], "checked": len(math)}


def run_math_check(
    cfg: PipelineConfig,
    provider: Provider,
    results: dict[str, list[dict]],
    on_event: Emitter | None = None,
    cancel: CancelToken | None = None,
) -> dict[str, list[dict]]:
    """Opt-in formula re-check (extra vision calls). Stores ``math_check`` per page."""
    cancel = cancel or CancelToken()
    total = sum(len(v) for v in results.values())
    emit(on_event, Event("verify", "started", total=total))
    for base, entries in results.items():
        for entry in entries:
            cancel.check()
            try:
                entry["math_check"] = verify_math_page(provider, entry["img"], entry.get("blocks", []))
            except Exception as exc:  # noqa: BLE001
                entry["math_check"] = {"ok": True, "issues": [{"problem": str(exc)}]}
            ok = entry["math_check"]["ok"]
            emit(on_event, Event("verify", "ok" if ok else "warn", base=base, page=entry["page"]))
    return results


EXPECTED_PAGE_MULTIPLIER = {
    "interleave": 2,
    "grouped": 2,
    "side_by_side": 1,
    "translated_only": 1,
}


def leftover_words(text: str, allow: set[str] | None = None) -> list[str]:
    """Candidate source-language words (filter ``allow`` for target/math words)."""
    allow = {a.lower() for a in (allow or set())}
    return sorted({w for w in WORD_RE.findall(text) if w.lower() not in allow})


def verify_ocr(results: dict[str, list[dict]]) -> list[dict]:
    """Flag pages where the vision model returned nothing."""
    issues: list[dict] = []
    for base, entries in results.items():
        for e in entries:
            if not e.get("blocks"):
                issues.append(
                    {"kind": "empty_ocr", "base": base, "page": e["page"], "error": e.get("error", "")}
                )
    return issues


def verify_output(cfg: PipelineConfig, source_pdf: str, output_pdf: str | Path) -> list[dict]:
    """Check page count, page size, empty translated pages, leftovers."""
    issues: list[dict] = []
    src = fitz.open(source_pdf)
    out = fitz.open(str(output_pdf))
    n = src.page_count
    mode = cfg.output_mode

    expected = n * EXPECTED_PAGE_MULTIPLIER[mode]
    if out.page_count != expected:
        issues.append({"kind": "page_count", "expected": expected, "got": out.page_count})

    sw, sh = src[0].rect.width, src[0].rect.height
    for i, page in enumerate(out):
        if abs(page.rect.width - sw) > 1 or abs(page.rect.height - sh) > 1:
            issues.append(
                {
                    "kind": "page_size",
                    "page": i + 1,
                    "expected": (sw, sh),
                    "got": (page.rect.width, page.rect.height),
                }
            )
            break

    # which output pages are translations?
    if mode == "interleave":
        translated = list(range(1, out.page_count, 2))
    elif mode == "grouped":
        translated = list(range(n, out.page_count))
    elif mode == "translated_only":
        translated = list(range(out.page_count))
    else:  # side_by_side mixes languages -> skip content checks
        translated = []

    for i in translated:
        page = out[i]
        if not page.get_text().strip() and not page.get_images():
            issues.append({"kind": "empty_page", "page": i + 1})
    out.close()
    src.close()
    return issues


def run_verify(cfg: PipelineConfig, results: dict[str, list[dict]], outputs: dict[str, Path]) -> dict:
    report: dict[str, dict] = {}
    for base, outpath in outputs.items():
        source = next((s for s in cfg.resolve_sources() if Path(s).stem == base), None)
        issues = []
        if source:
            issues += verify_output(cfg, source, outpath)
        issues += [i for i in verify_ocr({base: results.get(base, [])})]
        math = [{"page": e["page"], **e["math_check"]} for e in results.get(base, []) if e.get("math_check")]
        report[base] = {"output": str(outpath), "issues": issues, "math": math}
    return report
