"""Translate source-language words that remain *inside* math (``\\text{...}``).

Formula derivations often carry annotations like ``\\text{kvadriramo}`` or
``\\textit{stavimo y}``. This step translates those text groups only, leaving
every mathematical symbol untouched.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import cache, paths
from .config import PipelineConfig
from .events import CancelToken, Emitter, Event, emit
from .providers import Provider

TEXTCMD = re.compile(r"\\(?:text|textit|textrm|textnormal|mbox)\s*\{")
# Latin / Latin-Extended / Cyrillic words (extend for other source scripts).
WORD = re.compile(r"[A-Za-z\u00C0-\u024F\u0400-\u04FF]{3,}")
MATH_WORDS = {
    "log",
    "ln",
    "sin",
    "cos",
    "tan",
    "cotg",
    "tg",
    "ctg",
    "lim",
    "max",
    "min",
    "arcsin",
    "arccos",
    "arctg",
    "sec",
    "csc",
    "text",
    "textit",
    "textrm",
    "textnormal",
    "mbox",
    "frac",
    "sqrt",
    "begin",
    "end",
    "hline",
    "quad",
    "cdot",
    "left",
    "right",
    "array",
    "aligned",
    "matrix",
    "pmatrix",
    "bmatrix",
    "displaystyle",
    "mathbb",
    "mathrm",
    "mathbf",
    "operatorname",
    "textwidth",
}


def find_text_groups(s: str) -> list[tuple[int, int, str]]:
    """Return ``(start, end, inner)`` for each ``\\text{...}`` group."""
    out: list[tuple[int, int, str]] = []
    i = 0
    while True:
        m = TEXTCMD.search(s, i)
        if not m:
            return out
        j, depth = m.end(), 1
        while j < len(s) and depth:
            depth += (s[j] == "{") - (s[j] == "}")
            j += 1
        out.append((m.start(), j, s[m.end() : j - 1]))
        i = j


def is_source_text(t: str) -> bool:
    return any(w.lower() not in MATH_WORDS for w in WORD.findall(t))


def _translate_batch(provider: Provider, items: list[str], target_lang: str) -> list[str]:
    prompt = (
        f"Translate each string in this JSON array into {target_lang}. They are short "
        "annotations inside math; keep any math symbols as-is. Return ONLY a JSON array "
        "of the same length with the translations.\n" + json.dumps(items, ensure_ascii=False)
    )
    raw = provider.text(prompt, max_tokens=4000) or ""
    raw = re.sub(r"^```[a-z]*|```$", "", raw.strip()).strip()
    try:
        arr = json.loads(raw)
        if isinstance(arr, list) and len(arr) == len(items):
            return [str(x) for x in arr]
    except json.JSONDecodeError:
        pass
    return []


def _translate_one(provider: Provider, item: str, target_lang: str) -> str:
    raw = provider.text(
        f"Translate into {target_lang}, keep math symbols, return only the translation:\n{item}",
        max_tokens=500,
    )
    return (raw or item).strip()


def annotate_blocks(provider: Provider, blocks: list[dict], target_lang: str) -> int:
    """Translate in-math annotations across a page's blocks in place. Returns count."""
    n = 0
    for b in blocks:
        if b.get("type") not in ("math", "table"):
            continue
        lx = b.get("latex", "")
        groups = [g for g in find_text_groups(lx) if is_source_text(g[2])]
        if not groups:
            continue
        items = [g[2] for g in groups]
        tr = _translate_batch(provider, items, target_lang)
        if len(tr) != len(items):
            tr = [_translate_one(provider, it, target_lang) for it in items]
        for (a, z, _), zh in sorted(zip(groups, tr, strict=False), key=lambda x: -x[0][0]):
            lx = lx[:a] + "\\text{" + zh + "}" + lx[z:]
        b["latex"] = lx
        n += 1
    return n


def run_annotate(
    cfg: PipelineConfig,
    provider: Provider,
    results: dict[str, list[dict]] | None = None,
    on_event: Emitter | None = None,
    cancel: CancelToken | None = None,
) -> dict[str, list[dict]]:
    cancel = cancel or CancelToken()
    if results is None:
        results = {}
        for source in cfg.resolve_sources():
            base = Path(source).stem
            results[base] = cache.load_json(paths.ocr_json(cfg.workdir, base)) or []

    emit(on_event, Event("annotate", "started", total=len(results)))
    for bi, (base, entries) in enumerate(results.items()):
        total = 0
        for entry in entries:
            cancel.check()
            total += annotate_blocks(provider, entry.get("blocks", []), cfg.target_lang)
        cache.save_json(paths.ocr_json(cfg.workdir, base), entries)
        emit(
            on_event,
            Event("annotate", "ok", base=base, index=bi + 1, total=len(results), data={"blocks": total}),
        )
    return results
