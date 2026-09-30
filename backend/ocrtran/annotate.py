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

# text-command variants that carry translatable content inside math/tables
TEXTCMD = re.compile(
    r"\\(?:text|textit|textbf|textsf|texttt|textsc|textsl|textup|textmd|textrm|"
    r"textnormal|emph|mbox|hbox|mathrm|mathbf|mathit)\s*\{"
)
# a whole bare table cell (letters/digits/spaces/punct) not inside a command
BARE_CELL = re.compile(
    r"(?<![\\\w{])([A-Za-z0-9\u00C0-\u024F\u0400-\u04FF]"
    r"[A-Za-z0-9\u00C0-\u024F\u0400-\u04FF ,.\-()/]{1,})"
)
LATEX_KEYWORDS = {
    "array",
    "tabular",
    "multicolumn",
    "multirow",
    "hline",
    "cline",
    "begin",
    "end",
    "textwidth",
    "linewidth",
    "columnwidth",
    "textheight",
    "dots",
    "cdots",
    "ldots",
    "vdots",
    "ddots",
    "quad",
    "qquad",
    "rule",
    "hspace",
    "vspace",
}
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


def find_text_spans(s: str, include_bare: bool = False) -> list[tuple[int, int, str, int]]:
    """Spans to translate: ``(start, end, inner, inner_start)``.

    ``inner_start == start`` marks a *bare* word (wrap it in ``\text{...}``);
    otherwise the command (e.g. ``\textbf``) is kept and only the inner text replaced.
    ``include_bare`` also catches plain words in a table that the model did not wrap.
    """
    spans: list[tuple[int, int, str, int]] = []
    for a, z, inner in find_text_groups(s):
        m = TEXTCMD.match(s, a)
        spans.append((a, z, inner, m.end() if m else a + 1))
    if not include_bare:
        return spans
    occupied = [(a, z) for a, z, _ in find_text_groups(s)]
    dollars: list[tuple[int, int]] = []
    start = None
    for i, ch in enumerate(s):
        if ch == "$":
            if start is None:
                start = i
            else:
                dollars.append((start, i))
                start = None

    def covered(i: int) -> bool:
        return any(a <= i < z for a, z in occupied) or any(a < i < b for a, b in dollars)

    for m in BARE_CELL.finditer(s):
        text = m.group(1).strip()
        if not text or covered(m.start()):
            continue
        if text.lower() in MATH_WORDS or text.lower() in LATEX_KEYWORDS:
            continue
        if not is_source_text(text):
            continue
        a = m.start() + m.group(1).index(text) if text in m.group(1) else m.start()
        spans.append((a, a + len(text), text, a))
    spans.sort()
    return spans


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
    """Translate in-math annotations and figure captions in place. Returns count.

    All strings on the page are translated in **one** batch call, with a per-item
    fallback on mismatch.
    """
    work: list[tuple[dict, str, list]] = []
    caps: list[dict] = []
    for b in blocks:
        t = b.get("type")
        if t in ("math", "table"):
            lx = b.get("latex", "")
            groups = [g for g in find_text_spans(lx, include_bare=(t == "table")) if is_source_text(g[2])]
            if groups:
                work.append((b, lx, groups))
        elif t == "figure":
            cap = (b.get("caption") or "").strip()
            if cap and is_source_text(cap):
                caps.append(b)
    if not work and not caps:
        return 0

    items = [g[2] for _, _, groups in work for g in groups] + [b["caption"].strip() for b in caps]
    tr = _translate_batch(provider, items, target_lang)
    if len(tr) != len(items):
        tr = [_translate_one(provider, it, target_lang) for it in items]

    cursor = 0
    for b, lx, groups in work:
        replacements = tr[cursor : cursor + len(groups)]
        cursor += len(groups)
        for (a, z, _inner, istart), zh in sorted(
            zip(groups, replacements, strict=False), key=lambda x: -x[0][0]
        ):
            if istart == a:  # bare word -> wrap it
                lx = lx[:a] + "\\text{" + zh + "}" + lx[z:]
            else:  # keep the command (bold headers stay bold)
                lx = lx[:istart] + zh + lx[z - 1 :]
        b["latex"] = lx
    for b in caps:
        b["caption"] = tr[cursor]
        cursor += 1
    return len(work) + len(caps)


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
