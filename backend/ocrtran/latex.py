"""Typeset the translated page with XeLaTeX (one standalone page per source page).

Security: LaTeX here is model-generated and possibly user-edited, so it is
treated as untrusted — no shell escape, restricted file I/O, per-page timeout.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from . import cache, paths, render
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
        bb = tight[i] if i < len(tight) and tight[i] else fig.get("bbox")
        if not bb:
            continue
        name = f"fig_{page}_{i}.png"
        render.render_figure(
            source_pdf, page, bb, paths.figure_path(cfg.workdir, base, page, i), cfg.figure_px
        )
        fig_names[id(fig)] = (name, max(0.05, float(bb[2]) - float(bb[0])))

    body: list[str] = []
    for b in blocks:
        t = b.get("type")
        if t in ("heading", "prose"):
            txt = b.get("target") or b.get("source") or ""
            body.append("\\subsection*{" + esc_text(txt) + "}" if t == "heading" else esc_text(txt))
        elif t in ("math", "table"):
            w = wrap_math(b.get("latex", ""))
            if w:
                body.append(w)
        elif t == "figure" and id(b) in fig_names:
            name, frac = fig_names[id(b)]
            width = min(0.92, max(0.30, frac * 1.25))
            body.append(
                f"\\begin{{center}}\\includegraphics[width={width:.2f}\\textwidth]{{{name}}}\\end{{center}}"
            )
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
        pdfs: list[Path] = []
        for entry in entries:
            cancel.check()
            page = entry["page"]
            tex = build_tex(cfg, base, source_pdf, entry)
            tex_path = paths.page_tex(cfg.workdir, base, page)
            tex_path.write_text(tex)
            ok, log = compile_tex(tex_path, tdir)
            if ok:
                pdfs.append(paths.page_pdf(cfg.workdir, base, page))
                emit(on_event, Event("build", "ok", base=base, page=page))
            else:
                emit(on_event, Event("build", "error", base=base, page=page, message=log))
        out[base] = pdfs
    return out
