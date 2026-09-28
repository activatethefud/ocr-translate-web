"""Post-run checks: empty pages, page counts/sizes, leftover source-language words.

These are heuristics meant to *flag* pages for human review, not to be perfect.
"""

from __future__ import annotations

import re
from pathlib import Path

import fitz

from .config import PipelineConfig

WORD_RE = re.compile(r"[A-Za-z\u00C0-\u024F\u0400-\u04FF]{4,}")

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
        report[base] = {"output": str(outpath), "issues": issues}
    return report
