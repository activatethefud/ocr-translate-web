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

from . import cache, geometry, layout, paths, render
from . import pages as pages_mod
from .concurrency import parallel_map
from .config import PipelineConfig
from .events import CancelToken, Emitter, Event, emit

# Letters outside the math font's coverage (Cyrillic, Greek, CJK, ...): typeset them
# with the main font via \text{...} so they render instead of "Missing character".
_NONLATIN = re.compile(r"[^\x00-\x7F]+")


def textify_math(s: str) -> str:
    return _NONLATIN.sub(lambda m: "\\text{" + m.group(0) + "}", s or "")


_TABLE_HEAD = re.compile(r"\\begin\{(array|tabular)\}\{([^}]*)\}")


def fix_table_spec(s: str) -> str:
    """Pad an array/tabular column spec so rows with extra ``&`` don't error.

    Models often emit a table whose rows use more columns than the spec declares
    ("Extra alignment tab has been changed to \\cr" -> no output). This widens the
    spec to the maximum number of columns actually used.
    """
    m = _TABLE_HEAD.search(s or "")
    if not m:
        return s
    spec = m.group(2)
    body = s[m.end() :]
    body = body.split("\\end{", 1)[0]
    max_cols = 0
    for row in re.split(r"\\\\", body):
        max_cols = max(max_cols, row.count("&") + 1)
    spec_cols = len(re.findall(r"[lcr]", spec)) + len(re.findall(r"p\{[^}]*\}", spec))
    if max_cols > spec_cols:
        add = max_cols - spec_cols
        if spec.endswith("|"):
            spec = spec[:-1] + "|c" * add + "|"
        else:
            spec = spec + "c" * add
        s = s[: m.start(2)] + spec + s[m.end(2) :]
    return s


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
            out.append("$" + textify_math(seg) + "$")
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


def _doc_header(cfg: PipelineConfig) -> str:
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
    )


def preamble(cfg: PipelineConfig) -> str:
    return _doc_header(cfg) + f"\\begin{{minipage}}{{{cfg.text_width}}}\n"


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
        raw = b.get("latex", "")
        if t == "table":
            raw = fix_table_spec(raw)
        w = wrap_math(textify_math(raw))
        if not w:
            return None
        number = str(b.get("number") or "").strip()
        if t == "math" and number and w.endswith("\\]"):
            w = w[:-2] + " \\qquad \\text{" + esc_text(number) + "} \\]"
        return w
    if t == "page_number":
        return None  # placed as a footer by build_tex, not in the reading flow
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


def _assemble_scale(cfg: PipelineConfig, source_pdf: str) -> float:
    """How much the translated page is scaled up when placed on the output page.

    Used to size figure crops: a crop rendered at ``figure_dpi`` on the translated
    page is displayed at ``figure_dpi * scale`` after assembly.
    """
    out_w, _out_h = _output_dims(cfg, source_pdf)
    area_w = max(1.0, out_w - 2 * cfg.margin_pt)
    textw = layout.parse_length_pt(cfg.text_width)
    base = area_w / max(1.0, textw + 16.0)  # 16pt = the standalone 8pt border x2
    cap = 1.0 if cfg.scale_mode == "fit" else cfg.max_scale
    return min(base, cap) if cap else base


def _crop_figures(cfg: PipelineConfig, base: str, source_pdf: str, entry: dict) -> tuple[dict, dict]:
    """Crop this page's figures at their *placed* size; return (fig_names, fig_info)."""
    page = entry["page"]
    blocks = entry.get("blocks", [])
    tight = entry.get("tight") or []
    figs = [b for b in blocks if b.get("type") == "figure"]
    fig_names: dict[int, tuple[str | None, float]] = {}
    fig_info: dict[int, dict] = {}
    textw = layout.parse_length_pt(cfg.text_width)
    scale = _assemble_scale(cfg, source_pdf)
    for i, fig in enumerate(figs):
        main_bb = fig.get("bbox")
        tight_bb = tight[i] if i < len(tight) and tight[i] else None
        bb = geometry.choose_box(tight_bb, main_bb)
        if not bb:
            continue
        frac = max(0.05, float(bb[2]) - float(bb[0]))
        # pixels needed for the *displayed* size at figure_dpi (capped, with a floor)
        disp_frac = min(cfg.figure_max_width, frac)
        target_px = int(max(96, min(cfg.figure_px, round(disp_frac * textw / 72.0 * cfg.figure_dpi * scale))))
        dest = paths.figure_path(cfg.workdir, base, page, i)
        path = render.render_figure(
            source_pdf,
            page,
            bb,
            dest,
            target_px,
            pad_frac=cfg.figure_pad,
            fmt=cfg.figure_format,
            jpeg_quality=cfg.jpeg_quality,
        )
        name = path.name if path else None
        if path is not None:  # drop a stale crop with the other extension
            for old in path.parent.glob(f"fig_{page}_{i}.*"):
                if old != path:
                    old.unlink(missing_ok=True)
        aspect = 1.0
        if path is not None:
            try:
                from PIL import Image

                with Image.open(path) as im:
                    w, h = im.size
                aspect = (h / w) if w else 1.0
            except Exception:  # noqa: BLE001
                aspect = 1.0
        fig_names[id(fig)] = (name, frac)
        fig_info[id(fig)] = {
            "name": name,
            "bbox": [float(x) for x in bb],
            "width": frac,
            "aspect": aspect,
            "caption": str(fig.get("caption") or ""),
            "target_px": target_px,
        }
    return fig_names, fig_info


def _figure_row_tex(row: layout.FigRow, fig_names: dict) -> str:
    """Render figures side by side (widths were chosen by the layout planner)."""
    parts = []
    for fig, w in zip(row.figs, row.widths, strict=True):
        entry = fig_names.get(id(fig.block))
        name = entry[0] if entry else None
        caption = str(fig.caption or "").strip()
        w = max(0.05, min(0.98, w))
        inner = ""
        if name:
            # the minipage is already {w}\textwidth wide, so the image fills it
            inner = f"\\includegraphics[width=\\textwidth]{{{name}}}"
        if caption:
            cap = text_to_tex(caption)
            inner = (inner + "\\\\[1pt]\\small " + cap) if inner else ("\\small\\textit{" + cap + "}")
        if not inner:
            continue
        parts.append(
            "\\begin{minipage}[t]{" + f"{w:.3f}" + "\\textwidth}\\centering " + inner + "\\end{minipage}"
        )
    if not parts:
        return ""
    return "\\begin{center}" + "\\hfill".join(parts) + "\\end{center}"


def build_page_tex(cfg: PipelineConfig, items: list, fig_names: dict) -> str:
    """One planned output page -> a standalone LaTeX document."""
    body: list[str] = []
    numbers: list[str] = []
    for it in items:
        if it.kind == "figrow":
            tex = _figure_row_tex(it.row, fig_names)
            if tex:
                body.append(tex)
            continue
        b = it.block or {}
        if b.get("type") == "page_number":
            num = str(b.get("text") or b.get("target") or b.get("source") or "").strip()
            if num:
                numbers.append(num)
            continue
        rendered = _block_tex(b, fig_names)
        if rendered:
            body.append(rendered)
    footer = ""
    if numbers:
        footer = (
            "\\par\\vspace{8pt}\\begin{center}\\small "
            + " \\quad ".join(esc_text(n) for n in numbers)
            + "\\end{center}"
        )
    text = preamble(cfg) + "\n\n".join(body)
    if footer:
        text += "\n" + footer
    return text + "\n" + POSTAMBLE


_ITEMH = re.compile(r"ITEMH (\d+) ([\d.]+)pt")
_TEXTW = re.compile(r"TEXTW ([\d.]+)pt")


def _parse_measures(log: str, n: int) -> dict | None:
    heights = [0.0] * n
    textw = None
    found = False
    for line in (log or "").splitlines():
        m = _ITEMH.search(line)
        if m:
            heights[int(m.group(1))] = float(m.group(2))
            found = True
        m2 = _TEXTW.search(line)
        if m2:
            textw = float(m2.group(1))
    return {"heights": heights, "textw": textw} if found else None


def measure_items(cfg: PipelineConfig, base: str, page: int, items: list, fig_names: dict) -> dict | None:
    """Measure each item's height with one ``\\setbox`` XeLaTeX pass.

    Returns ``{"heights": [...], "textw": pt}`` or ``None`` if it could not run.
    """
    tdir = paths.tex_dir(cfg.workdir, base)
    tdir.mkdir(parents=True, exist_ok=True)
    lines = [
        _doc_header(cfg),
        "\\newbox\\mb",
        "\\setbox\\mb=\\vbox{\\begin{minipage}{"
        + cfg.text_width
        + "}\\typeout{TEXTW \\the\\textwidth}\\end{minipage}}",
    ]
    for i, it in enumerate(items):
        tex = _figure_row_tex(it.row, fig_names) if it.kind == "figrow" else _block_tex(it.block, fig_names)
        if not tex:
            lines.append(f"\\typeout{{ITEMH {i} 0pt}}")
            continue
        lines.append(
            "\\setbox\\mb=\\vbox{\\begin{minipage}{" + cfg.text_width + "}" + tex + "\\end{minipage}}"
        )
        lines.append(f"\\typeout{{ITEMH {i} \\the\\dimexpr\\ht\\mb+\\dp\\mb\\relax}}")
    lines.append("\\end{document}")
    mpath = tdir / f"m{page:02d}.tex"
    mpath.write_text("\n".join(lines))
    ok, _ = compile_tex(mpath, tdir)
    log_path = mpath.with_suffix(".log")
    if not log_path.exists():
        return None
    try:
        return _parse_measures(log_path.read_text(errors="ignore"), len(items))
    except OSError:
        return None


def _output_dims(cfg: PipelineConfig, source_pdf: str) -> tuple[float, float]:
    import fitz

    with fitz.open(source_pdf) as doc:
        w, h = doc[0].rect.width, doc[0].rect.height
    return pages_mod.output_size(cfg.output_page_size, w, h)


def build_pages(cfg: PipelineConfig, base: str, source_pdf: str, entry: dict) -> list[tuple[int, str]]:
    """Plan + typeset one source page into ``[(part, tex), ...]`` output pages."""
    fig_names, fig_info = _crop_figures(cfg, base, source_pdf, entry)
    out_w, out_h = _output_dims(cfg, source_pdf)
    textw_pt = layout.parse_length_pt(cfg.text_width)
    page = int(entry.get("page") or 1)

    def measure(items):
        return measure_items(cfg, base, page, items, fig_names)

    plans = layout.plan_pages(cfg, entry.get("blocks", []), fig_info, out_w, out_h, textw_pt, measure)
    return [(part, build_page_tex(cfg, items, fig_names)) for part, items in enumerate(plans, start=1)]


def build_tex(cfg: PipelineConfig, base: str, source_pdf: str, entry: dict) -> str:
    """Backwards-compatible: the first output page's TeX ("" when there is none)."""
    pages = build_pages(cfg, base, source_pdf, entry)
    return pages[0][1] if pages else ""


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
            if entry.get("blank") or not entry.get("blocks"):
                # blank or failed-OCR page -> no translated PDF; the original is kept
                # and the page is reported as missing (not a silent blank page)
                return
            page = entry["page"]
            parts = build_pages(cfg, base, source_pdf, entry)
            built = 0
            for part, tex in parts:
                tex_path = paths.page_tex(cfg.workdir, base, page, part)
                tex_path.write_text(tex)
                ok, log = compile_tex(tex_path, tdir)
                if ok:
                    with lock:
                        pdfs.append(paths.page_pdf(cfg.workdir, base, page, part))
                    built += 1
                    emit(
                        on_event,
                        Event("build", "ok", base=base, page=page, data={"part": part, "parts": len(parts)}),
                    )
                else:
                    emit(on_event, Event("build", "error", base=base, page=page, message=log))
            if built == 0 and parts:
                emit(on_event, Event("build", "error", base=base, page=page, message="no part compiled"))

        parallel_map(build_one, entries, cfg.concurrency)
        out[base] = sorted(pdfs, key=lambda p: p.name)
    return out
