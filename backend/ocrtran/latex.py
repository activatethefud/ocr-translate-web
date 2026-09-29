"""Typeset the translated page with XeLaTeX (one standalone page per source page).

Security: LaTeX here is model-generated and possibly user-edited, so it is
treated as untrusted — no shell escape, restricted file I/O, per-page timeout.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
from pathlib import Path

from . import cache, geometry, paths, render
from .concurrency import parallel_map
from .config import PipelineConfig
from .events import CancelToken, Emitter, Event, emit

DISPLAY_ENVS = (
    "align",
    "align*",
    "gather",
    "gather*",
    "multline",
    "multline*",
    "equation",
    "equation*",
    "flalign",
    "flalign*",
    "alignat",
    "alignat*",
    "eqnarray",
    "eqnarray*",
    "displaymath",
    "math",
)


def esc_text(s: str) -> str:
    """Escape LaTeX specials **outside** ``$...$`` only."""
    parts, out = s.split("$"), []
    for i, seg in enumerate(parts):
        if i % 2 == 1:
            out.append("$" + seg + "$")
        else:
            # drop control chars and un-escape set braces the model writes as \{ \}
            seg = "".join(c for c in seg if ord(c) >= 32 or c in "\t")
            seg = seg.replace("\\{", "{").replace("\\}", "}")
            seg = seg.replace("\\", "\\textbackslash{}")
            for a, b in [
                ("&", "\\&"),
                ("%", "\\%"),
                ("#", "\\#"),
                ("_", "\\_"),
                ("{", "\\{"),
                ("}", "\\}"),
                ("~", "\\textasciitilde{}"),
                ("^", "\\textasciicircum{}"),
            ]:
                seg = seg.replace(a, b)
            out.append(seg)
    return "".join(out)


def wrap_math(lx: str) -> str:
    """Wrap a math block in ``\\[ ... \\]`` unless it already is display math.

    Wrapping an existing ``align``/``equation`` inside ``\\[ \\]`` triggers
    "Erroneous nesting of equation structures".
    """
    s = lx.strip()
    if not s:
        return ""
    if s.startswith("$") or s.startswith("\\[") or s.startswith("\\("):
        return s
    m = re.match(r"^\\begin\{([^}]+)\}", s)
    if m and m.group(1) in DISPLAY_ENVS:
        return s
    if re.search(r"\\begin\{(align|gather|multline|equation|flalign|eqnarray)\*?\}", s):
        return s
    return "\\[\n" + s + "\n\\]"


def preamble(cfg: PipelineConfig) -> str:
    lb = ""
    if cfg.linebreak_locale:
        lb = f'\\XeTeXlinebreaklocale "{cfg.linebreak_locale}"\n\\XeTeXlinebreakskip=0pt plus 1pt\n'
    return (
        "\\documentclass[border=8pt]{standalone}\n"
        "\\usepackage{amsmath,amssymb,mathtools}\n"
        "\\usepackage{graphicx}\n\\usepackage{xcolor}\n\\usepackage{cancel}\n"
        "\\usepackage{fontspec}\n"
        f"\\setmainfont{{{cfg.font_main}}}\n{lb}{cfg.extra_preamble}\n"
        "\\setlength{\\parindent}{0pt}\\setlength{\\parskip}{5pt}\n"
        "\\begin{document}\n"
        f"\\begin{{minipage}}{{{cfg.text_width}}}\n"
    )


POSTAMBLE = "\\end{minipage}\n\\end{document}\n"


_DELIMS = (("$$", "$"), ("\\(", "$"), ("\\)", "$"), ("\\[", "$"), ("\\]", "$"))
_LIST_MARKER = re.compile(r"^\s*(?:\(?\d{1,2}[.)]|[ivxlcdm]{1,5}[.)]|[a-z][.)])\s+", re.IGNORECASE)


def _normalize_math(s: str) -> str:
    """Collapse display delimiters and stray markdown the model emits in prose."""
    for a, b in _DELIMS:
        s = s.replace(a, b)
    s = s.replace("**", "")  # markdown bold has no meaning in LaTeX
    return s


def text_to_tex(s: str) -> str:
    """Escape text and turn blank-line-separated paragraphs into LaTeX paragraphs."""
    s = _normalize_math(s or "")
    paras = [p.strip() for p in re.split(r"\n\s*\n", s.strip()) if p.strip()]
    return "\n\n".join(esc_text(p.replace("\n", " ")) for p in paras)


_HEADING_CMDS = {1: "\\subsection*", 2: "\\subsubsection*", 3: "\\paragraph*"}


def _block_tex(b: dict, fig_names: dict) -> str | None:
    """Render one structured block to LaTeX (lists/headings/quotes/theorems/…)."""
    t = b.get("type")
    textsrc = b.get("target") or b.get("source") or ""
    if t == "heading":
        cmd = _HEADING_CMDS.get(int(b.get("level") or 2), "\\subsubsection*")
        return cmd + "{" + text_to_tex(textsrc) + "}"
    if t == "prose":
        return text_to_tex(textsrc)
    if t == "quote":
        return "\\begin{quote}\n" + text_to_tex(textsrc) + "\n\\end{quote}"
    if t == "theorem":
        kind = esc_text(str(b.get("kind") or "Theorem").strip())
        name = str(b.get("name") or "").strip()
        head = f"\\textbf{{{kind}" + (f" ({esc_text(name)})" if name else "") + ".}"
        return "\\begin{quote}\n" + head + " " + text_to_tex(textsrc) + "\n\\end{quote}"
    if t == "list":
        ordered = bool(b.get("ordered"))
        env = "enumerate" if ordered else "itemize"
        lines = []
        for item in b.get("items") or []:
            txt = item.get("target") or item.get("source") or ""
            if ordered:
                # the model often keeps "1)" / "ii." inside the item; strip it so we
                # don't end up with double numbering
                txt = _LIST_MARKER.sub("", _normalize_math(txt), count=1)
            lines.append("  \\item " + text_to_tex(txt))
        if not lines:
            return None
        return f"\\begin{{{env}}}\n" + "\n".join(lines) + f"\n\\end{{{env}}}"
    if t in ("math", "table"):
        w = wrap_math(b.get("latex", ""))
        if not w:
            return None
        number = str(b.get("number") or "").strip()
        if t == "math" and number and w.endswith("\\]"):
            w = w[:-2] + " \\qquad \\text{" + esc_text(number) + "} \\]"
        return w
    if t == "figure":
        entry = fig_names.get(id(b))
        caption = str(b.get("caption") or "").strip()
        name = entry[0] if entry else None
        if name:
            width = min(0.92, max(0.30, entry[1] * 1.25))
            inner = f"\\includegraphics[width={width:.2f}\\textwidth]{{{name}}}"
            if caption:
                inner += "\\\\[2pt]\\small " + text_to_tex(caption)
            return "\\begin{center}" + inner + "\\end{center}"
        if caption:  # crop failed -> keep the caption so nothing is lost silently
            return "\\begin{center}\\small \\textit{" + text_to_tex(caption) + "}\\end{center}"
        return None
    # unknown type: treat like prose (backwards compatible)
    return text_to_tex(textsrc) if textsrc else None


def build_tex(
    cfg: PipelineConfig,
    base: str,
    source_pdf: str,
    entry: dict,
) -> str:
    """Turn one OCR page entry into a LaTeX document (and crop its figures)."""
    page = entry["page"]
    blocks = entry.get("blocks", [])
    figs = [b for b in blocks if b.get("type") == "figure"]
    tight = entry.get("tight") or []
    fig_names: dict[int, tuple[str, float]] = {}
    for i, fig in enumerate(figs):
        # Prefer the dedicated tight box. The main-call box is sometimes the whole
        # page (bad), and unioning with it made every crop the full page, so we only
        # fall back to it when there is no tight box. render_figure still pads.
        main_bb = fig.get("bbox")
        tight_bb = tight[i] if i < len(tight) and tight[i] else None
        bb = geometry.choose_box(tight_bb, main_bb)
        if not bb:
            continue
        name = f"fig_{page}_{i}.png"
        dest = paths.figure_path(cfg.workdir, base, page, i)
        ok = render.render_figure(source_pdf, page, bb, dest, cfg.figure_px, pad_frac=cfg.figure_pad)
        frac = max(0.05, float(bb[2]) - float(bb[0]))
        # if the crop could not be written, keep the caption but no \includegraphics
        fig_names[id(fig)] = (name, frac) if (ok and dest.exists()) else (None, frac)

    body: list[str] = []
    for b in blocks:
        rendered = _block_tex(b, fig_names)
        if rendered:
            body.append(rendered)
    return preamble(cfg) + "\n\n".join(body) + "\n" + POSTAMBLE


def compile_tex(tex_path: Path, workdir: Path, timeout: int = 120) -> tuple[bool, str]:
    """Compile with XeLaTeX in a restricted environment. Returns (ok, log)."""
    env = dict(os.environ)
    env.update({"openin_any": "p", "openout_any": "p", "shell_escape": "f"})
    cmd = [
        "xelatex",
        "-no-shell-escape",
        "-interaction=nonstopmode",
        "-halt-on-error",
        "-file-line-error",
        tex_path.name,
    ]
    try:
        proc = subprocess.run(cmd, cwd=str(workdir), env=env, timeout=timeout, capture_output=True, text=True)
    except subprocess.TimeoutExpired:
        return False, f"xelatex timed out after {timeout}s"
    pdf = tex_path.with_suffix(".pdf")
    if pdf.exists():
        return True, proc.stdout[-500:]
    errors = [line for line in proc.stdout.splitlines() if line.startswith("!")]
    return False, (errors[0] if errors else proc.stdout[-500:])


def run_build(
    cfg: PipelineConfig,
    results: dict[str, list[dict]] | None = None,
    on_event: Emitter | None = None,
    cancel: CancelToken | None = None,
) -> dict[str, list[Path]]:
    cancel = cancel or CancelToken()
    source_map = {Path(s).stem: s for s in cfg.resolve_sources()}
    if results is None:
        results = {b: cache.load_json(paths.ocr_json(cfg.workdir, b)) or [] for b in source_map}

    out: dict[str, list[Path]] = {}
    emit(on_event, Event("build", "started", total=sum(len(v) for v in results.values())))
    for base, entries in results.items():
        source_pdf = source_map[base]
        tdir = paths.tex_dir(cfg.workdir, base)
        tdir.mkdir(parents=True, exist_ok=True)
        lock = threading.Lock()
        pdfs: list[Path] = []

        def build_one(
            entry: dict,
            *,
            base=base,
            source_pdf=source_pdf,
            tdir=tdir,
            lock=lock,
            pdfs=pdfs,
        ) -> None:
            cancel.check()
            if entry.get("blank"):
                return  # blank page -> no translated PDF, original is kept
            page = entry["page"]
            tex = build_tex(cfg, base, source_pdf, entry)
            tex_path = paths.page_tex(cfg.workdir, base, page)
            # Distinct pNN.tex basenames mean parallel compiles share the dir safely.
            tex_path.write_text(tex)
            ok, log = compile_tex(tex_path, tdir)
            if ok:
                with lock:
                    pdfs.append(paths.page_pdf(cfg.workdir, base, page))
                emit(on_event, Event("build", "ok", base=base, page=page))
            else:
                emit(on_event, Event("build", "error", base=base, page=page, message=log))

        parallel_map(build_one, entries, cfg.concurrency)
        out[base] = sorted(pdfs, key=lambda p: p.name)
    return out
