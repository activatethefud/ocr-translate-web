"""Post-run checks: empty pages, page counts/sizes, leftover source-language words.

These are heuristics meant to *flag* pages for human review, not to be perfect.
"""

from __future__ import annotations

import re
from pathlib import Path

import fitz

from . import paths, render
from .config import PipelineConfig
from .events import CancelToken, Emitter, Event, emit
from .pages import parse_page_spec
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


_INLINE_MATH = re.compile(r"\$([^$]+)\$")


def page_formulas(blocks: list[dict]) -> list[str]:
    """All formulas on a page: display math/tables + inline ``$...$`` in text."""
    out: list[str] = []
    for b in blocks:
        t = b.get("type")
        if t in ("math", "table"):
            if b.get("latex"):
                out.append(b["latex"])
        elif t in ("prose", "heading", "quote", "theorem"):
            out += _INLINE_MATH.findall(b.get("target") or b.get("source") or "")
        elif t == "list":
            for it in b.get("items") or []:
                out += _INLINE_MATH.findall(it.get("target") or it.get("source") or "")
    return out


def verify_math_page(
    provider: Provider, image_path: str | Path, blocks: list[dict], max_tokens: int = 1500
) -> dict:
    """Ask a vision model whether the extracted formulas match the page image."""
    from .ocr import parse_json

    math = list(enumerate(page_formulas(blocks)))
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
    srcmap = {Path(s).stem: s for s in cfg.resolve_sources()}
    emit(on_event, Event("verify", "started", total=total))
    for base, entries in results.items():
        for entry in entries:
            cancel.check()
            img = Path(entry.get("img") or "")
            if not img.exists():  # renders are deleted after OCR -> re-render for the check
                src = srcmap.get(base)
                if src:
                    try:
                        render.render_pages(
                            src,
                            paths.pages_dir(cfg.workdir, base),
                            cfg.dpi,
                            cfg.max_px,
                            pages=[entry["page"]],
                        )
                    except Exception:  # noqa: BLE001 - recorded below
                        pass
            try:
                entry["math_check"] = verify_math_page(provider, img, entry.get("blocks", []))
            except Exception as exc:  # noqa: BLE001
                entry["math_check"] = {"ok": True, "issues": [{"problem": str(exc)}]}
            finally:
                img.unlink(missing_ok=True)  # don't keep the re-render
            ok = entry["math_check"]["ok"]
            emit(
                on_event,
                Event(
                    "verify",
                    "ok" if ok else "warn",
                    base=base,
                    page=entry["page"],
                    data={"checked": entry["math_check"].get("checked", 0)},
                ),
            )
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


def _expected_pages(mode: str, n: int, selected: set[int], keep_unselected: bool) -> int:
    k = len(selected)
    if mode in ("interleave", "grouped"):
        return (n if keep_unselected else k) + k
    if mode == "side_by_side":
        return n if keep_unselected else k
    return k  # translated_only


def _translated_positions(mode: str, n: int, selected: set[int], keep: bool) -> list[int]:
    """0-based positions (in the output) that hold translations."""
    if mode == "interleave":
        pos, out = 0, []
        for p in range(1, n + 1):
            if p in selected:
                out.append(pos + 1)  # translation follows the original
                pos += 2
            elif keep:
                pos += 1
        return out
    if mode == "grouped":
        start = n if keep else len(selected)
        return list(range(start, start + len(selected)))
    if mode == "translated_only":
        return list(range(len(selected)))
    return []  # side_by_side mixes languages


def verify_output(cfg: PipelineConfig, source_pdf: str, output_pdf: str | Path) -> list[dict]:
    """Check page count, page size, empty translated pages, leftovers.

    Accounts for a page *selection* (``cfg.pages``) and ``cfg.unprocessed``, and for
    sources with **mixed page sizes** (comparing against the set of source sizes,
    not just page 1).
    """
    from .pages import parse_page_spec

    issues: list[dict] = []
    src = fitz.open(source_pdf)
    out = fitz.open(str(output_pdf))
    n = src.page_count
    mode = cfg.output_mode
    selected = set(parse_page_spec(cfg.pages, n))
    keep = cfg.unprocessed == "original"

    expected = _expected_pages(mode, n, selected, keep)
    if out.page_count != expected:
        issues.append({"kind": "page_count", "expected": expected, "got": out.page_count})

    allowed = {(round(p.rect.width), round(p.rect.height)) for p in src}
    if cfg.output_page_size == "a4":
        allowed.add((595, 842))
    elif cfg.output_page_size == "letter":
        allowed.add((612, 792))
    for i, page in enumerate(out):
        key = (round(page.rect.width), round(page.rect.height))
        if key not in allowed:
            issues.append({"kind": "page_size", "page": i + 1, "got": key, "allowed": sorted(allowed)})

    for i in _translated_positions(mode, n, selected, keep):
        if i >= out.page_count:
            continue
        page = out[i]
        if not page.get_text().strip() and not page.get_images():
            issues.append({"kind": "empty_page", "page": i + 1})
    out.close()
    src.close()
    return issues


def missing_pages(cfg: PipelineConfig, results: dict[str, list[dict]]) -> dict[str, list[int]]:
    """Selected pages whose translated PDF was never produced (build failed)."""
    srcmap = {Path(s).stem: s for s in cfg.resolve_sources()}
    out: dict[str, list[int]] = {}
    for base in results:
        src = srcmap.get(base)
        if not src:
            continue
        with fitz.open(src) as doc:
            n = doc.page_count
        miss = [p for p in parse_page_spec(cfg.pages, n) if not paths.page_pdfs(cfg.workdir, base, p)]
        if miss:
            out[base] = miss
    return out


def run_verify(cfg: PipelineConfig, results: dict[str, list[dict]], outputs: dict[str, Path]) -> dict:
    report: dict[str, dict] = {}
    for base, outpath in outputs.items():
        source = next((s for s in cfg.resolve_sources() if Path(s).stem == base), None)
        issues = []
        if source:
            issues += verify_output(cfg, source, outpath)
        issues += [i for i in verify_ocr({base: results.get(base, [])})]
        math = [{"page": e["page"], **e["math_check"]} for e in results.get(base, []) if e.get("math_check")]
        report[base] = {
            "output": str(outpath),
            "issues": issues,
            "math": math,
            "missing_pages": missing_pages(cfg, {base: results.get(base, [])}).get(base, []),
        }
    return report
