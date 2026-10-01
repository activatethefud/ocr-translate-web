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


def textify_math(s: str, fallback: str = "") -> str:
    """Non-Latin letters inside math -> ``\\text{...}`` (fallback font when needed)."""
    out: list[str] = []
    for ch in s or "":
        if ord(ch) < 128:
            out.append(ch)
        elif fallback and _needs_fallback(ch):
            # inside math mode a font switch needs \text{} to take effect
            out.append("\\text{{\\" + FALLBACK_CMD + " " + ch + "}}")
        else:
            out.append("\\text{" + ch + "}")
    return "".join(out)


_TABLE_HEAD = re.compile(r"\\begin\{(array|tabular)\}")


def fix_table_spec(s: str) -> str:
    """Pad an array/tabular column spec so rows with extra ``&`` don't error.

    Models often emit a table whose rows use more columns than the spec declares
    ("Extra alignment tab has been changed to \\cr" -> no output). This widens the
    spec to the maximum number of columns actually used.
    """
    m = _TABLE_HEAD.search(s or "")
    if not m:
        return s
    i = m.end()
    if i >= len(s) or s[i] != "{":
        return s
    depth, j = 0, i
    while j < len(s):  # read the *balanced* column spec (it may contain p{...})
        if s[j] == "{":
            depth += 1
        elif s[j] == "}":
            depth -= 1
            if depth == 0:
                break
        j += 1
    if depth != 0:
        return s
    spec = s[i + 1 : j]
    body = s[j + 1 :].split("\\end{", 1)[0]
    max_cols = 0
    for row in re.split(r"\\\\", body):
        max_cols = max(max_cols, row.count("&") + 1)
    # count columns at top level: l/c/r letters + p{}/m{}/b{} specifiers
    spec_cols, d = 0, 0
    for ch in spec:
        if ch == "{":
            d += 1
        elif ch == "}":
            d = max(0, d - 1)
        elif d == 0 and ch in "lcrXpmb":
            spec_cols += 1
    _pad_at = m.start() + len(m.group(0)) + 1 + len(spec)  # position of the spec's "}"
    if max_cols > spec_cols:
        add = max_cols - spec_cols
        spec = (spec[:-1] + "|c" * add + "|") if spec.endswith("|") else (spec + "c" * add)
        s = s[: i + 1] + spec + s[j:]
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


# Unicode symbols the prose fonts lack: render them in math mode (the math font has
# them) instead of letting XeLaTeX drop them as missing glyphs. Applied to text *and*
# inside $...$ (where the LaTeX command replaces the bare Unicode character).
SYMBOL_TO_MATH = {
    "←": r"\leftarrow",
    "→": r"\to",
    "↑": r"\uparrow",
    "↓": r"\downarrow",
    "↔": r"\leftrightarrow",
    "↕": r"\updownarrow",
    "⇐": r"\Leftarrow",
    "⇒": r"\Rightarrow",
    "⇔": r"\Leftrightarrow",
    "↦": r"\mapsto",
    "⟶": r"\longrightarrow",
    "⟵": r"\longleftarrow",
    "−": "-",
    "∓": r"\mp",
    "±": r"\pm",
    "×": r"\times",
    "÷": r"\div",
    "⋅": r"\cdot",
    "∘": r"\circ",
    "∗": r"\ast",
    "√": r"\surd",
    "∞": r"\infty",
    "∑": r"\sum",
    "∏": r"\prod",
    "∫": r"\int",
    "∮": r"\oint",
    "∂": r"\partial",
    "∇": r"\nabla",
    "∈": r"\in",
    "∉": r"\notin",
    "∋": r"\ni",
    "∩": r"\cap",
    "∪": r"\cup",
    "⊆": r"\subseteq",
    "⊇": r"\supseteq",
    "⊂": r"\subset",
    "⊃": r"\supset",
    "⊄": r"\not\subset",
    "⊅": r"\not\supset",
    "∧": r"\wedge",
    "∨": r"\vee",
    "¬": r"\neg",
    "∀": r"\forall",
    "∃": r"\exists",
    "∄": r"\nexists",
    "∅": r"\emptyset",
    "≤": r"\le",
    "≥": r"\ge",
    "≠": r"\ne",
    "≈": r"\approx",
    "≡": r"\equiv",
    "∼": r"\sim",
    "≃": r"\simeq",
    "≅": r"\cong",
    "∝": r"\propto",
    "⊥": r"\perp",
    "∥": r"\parallel",
    "≪": r"\ll",
    "≫": r"\gg",
    "■": r"\blacksquare",
    "□": r"\square",
    "▪": r"\blacksquare",
    "▫": r"\square",
    "▲": r"\blacktriangle",
    "△": r"\triangle",
    "▼": r"\blacktriangledown",
    "▽": r"\triangledown",
    "▶": r"\blacktriangleright",
    "▸": r"\triangleright",
    "◀": r"\blacktriangleleft",
    "◂": r"\triangleleft",
    "◤": r"\blacktriangle",
    "◥": r"\blacktriangle",
    "●": r"\bullet",
    "○": r"\circ",
    "◆": r"\blacklozenge",
    "◇": r"\lozenge",
    "★": r"\bigstar",
    "☆": r"\star",
    "♦": r"\blacklozenge",
    "♣": r"\clubsuit",
    "♠": r"\spadesuit",
    "♥": r"\heartsuit",
    "•": r"\textbullet",
    "·": r"\cdot",
    "…": r"\ldots",
    "°": r"^\circ",
    "′": r"^\prime",
    "″": r"^{\prime\prime}",
    "µ": r"\textmu",
    # letterlike symbols (double-struck, script) the prose fonts lack
    "ℝ": r"\mathbb{R}",
    "ℕ": r"\mathbb{N}",
    "ℤ": r"\mathbb{Z}",
    "ℚ": r"\mathbb{Q}",
    "ℂ": r"\mathbb{C}",
    "ℙ": r"\mathbb{P}",
    "ℍ": r"\mathbb{H}",
    "𝔽": r"\mathbb{F}",
    "ℓ": r"\ell",
    "ℏ": r"\hbar",
    "ℑ": r"\Im",
    "ℜ": r"\Re",
    "℘": r"\wp",
    "ℵ": r"\aleph",
    "ℶ": r"\beth",
}
# text (non-math) replacements
TEXT_REPL = {
    "–": "--",
    "—": "---",
    "†": r"\dag",
    "‡": r"\ddag",
    "§": r"\S",
    "¶": r"\P",
    "\u00a0": "~",
    "\u2009": r"\,",
    "\u202f": r"\,",
}
SUBSCRIPTS = {
    "₀": "0",
    "₁": "1",
    "₂": "2",
    "₃": "3",
    "₄": "4",
    "₅": "5",
    "₆": "6",
    "₇": "7",
    "₈": "8",
    "₉": "9",
    "₊": "+",
    "₋": "-",
    "ₙ": "n",
    "ₓ": "x",
}
SUPERSCRIPTS = {
    "⁰": "0",
    "¹": "1",
    "²": "2",
    "³": "3",
    "⁴": "4",
    "⁵": "5",
    "⁶": "6",
    "⁷": "7",
    "⁸": "8",
    "⁹": "9",
    "⁺": "+",
    "⁻": "-",
    "ⁿ": "n",
}
# scripts a CJK main font does not cover -> render with the fallback font
_FALLBACK_RANGES = (
    (0x0370, 0x03FF),  # Greek
    (0x0400, 0x052F),  # Cyrillic + supplement
    (0x1E00, 0x1EFF),  # Latin Extended Additional
    (0x0100, 0x024F),  # Latin Extended A/B
)
FALLBACK_CMD = "glyphfallback"


def _needs_fallback(ch: str) -> bool:
    o = ord(ch)
    return any(a <= o <= b for a, b in _FALLBACK_RANGES)


_ESC = {
    "&": r"\&",
    "%": r"\%",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
    "\\": r"\textbackslash{}",
}


# LaTeX math commands the model sometimes leaves in prose (no $...$): wrap them in
# inline math instead of escaping the backslash into a literal "\sqrt".
MATH_COMMANDS = frozenset(
    """sqrt frac dfrac tfrac binom choose overline underline vec hat bar dot ddot tilde
    widehat widetilde left right text mathrm mathbf mathit mathcal mathbb mathfrak
    operatorname displaystyle textstyle quad qquad
    cdot cdots times div pm mp ast star circ bullet le leq ge geq ne neq approx equiv
    sim simeq cong propto ll gg subset subseteq supset supseteq in notin ni cup cap
    setminus emptyset varnothing forall exists nexists neg land lor wedge vee
    to gets mapsto rightarrow leftarrow leftrightarrow Rightarrow Leftarrow Leftrightarrow
    uparrow downarrow updownarrow longrightarrow longleftarrow
    infty partial nabla sum prod int oint iint iiint lim limsup liminf
    sin cos tan cot sec csc arcsin arccos arctan sinh cosh tanh log ln exp min max gcd lcm
    alpha beta gamma delta epsilon varepsilon zeta eta theta vartheta iota kappa lambda mu
    nu xi pi varpi rho varrho sigma varsigma tau upsilon phi varphi chi psi omega
    Gamma Delta Theta Lambda Xi Pi Sigma Upsilon Phi Psi Omega
    ldots dots vdots ddots prime degree angle triangle square
    """.split()
)
_CMD_RE = re.compile(r"\\([A-Za-z]+)")


def _scan_math_command(seg: str, i: int) -> str | None:
    """From a backslash at ``i``, the command plus its {} / [] / _ ^ arguments."""
    m = _CMD_RE.match(seg, i)
    if not m or m.group(1) not in MATH_COMMANDS:
        return None
    j = m.end()
    while j < len(seg):
        c = seg[j]
        if c == "{":
            depth, k = 0, j
            while k < len(seg):
                if seg[k] == "{":
                    depth += 1
                elif seg[k] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                k += 1
            if depth != 0:
                break
            j = k + 1
        elif c == "[":
            k = seg.find("]", j + 1)
            if k < 0:
                break
            j = k + 1
        elif c in "^_":
            j += 1
            if j < len(seg) and seg[j] == "{":
                depth, k = 0, j
                while k < len(seg):
                    if seg[k] == "{":
                        depth += 1
                    elif seg[k] == "}":
                        depth -= 1
                        if depth == 0:
                            break
                    k += 1
                if depth != 0:
                    break
                j = k + 1
            elif j < len(seg):
                j += 1
        else:
            break
    return seg[i:j]


_ENV_HEAD = re.compile(r"\\begin\{([A-Za-z]+\*?)\}")


def _map_text(seg: str, fallback: str) -> str:
    seg = "".join(c for c in seg if ord(c) >= 32 or c in "\t")
    seg = seg.replace("\\{", "{").replace("\\}", "}")  # un-escape set braces
    out: list[str] = []
    i = 0
    while i < len(seg):
        ch = seg[i]
        if ch == "\\":
            # the model sometimes embeds a display-math environment in prose; keep it
            # as real math and drop stray $ inside it (e.g. $\left$ -> \left)
            em = _ENV_HEAD.match(seg, i)
            if em and em.group(1) in DISPLAY_ENVS:
                endtok = "\\end{" + em.group(1) + "}"
                end = seg.find(endtok, em.end())
                if end != -1:
                    end += len(endtok)
                    out.append(seg[i:end].replace("$", ""))
                    i = end
                    continue
            span = _scan_math_command(seg, i)
            if span:
                out.append("$" + _map_math(span, fallback) + "$")
                i += len(span)
                continue
        if ch in SYMBOL_TO_MATH:
            out.append("$" + SYMBOL_TO_MATH[ch] + "$")
        elif ch in SUBSCRIPTS:
            out.append(r"\textsubscript{" + SUBSCRIPTS[ch] + "}")
        elif ch in SUPERSCRIPTS:
            out.append(r"\textsuperscript{" + SUPERSCRIPTS[ch] + "}")
        elif ch in TEXT_REPL:
            out.append(TEXT_REPL[ch])
        elif fallback and _needs_fallback(ch):
            out.append("{\\" + FALLBACK_CMD + " " + ch + "}")
        else:
            out.append(_ESC.get(ch, ch))
        i += 1
    return "".join(out)


def _map_math(seg: str, fallback: str = "") -> str:
    out: list[str] = []
    for ch in seg:
        if ch in SYMBOL_TO_MATH:
            out.append(SYMBOL_TO_MATH[ch])
        elif ch in SUBSCRIPTS:
            out.append("{}_{" + SUBSCRIPTS[ch] + "}")
        elif ch in SUPERSCRIPTS:
            out.append("{}^{" + SUPERSCRIPTS[ch] + "}")
        else:
            out.append(ch)
    return textify_math(strip_tags("".join(out)), fallback)


# a display-math environment embedded in prose (may itself contain stray "$")
_ENV_BLOCK = re.compile(
    r"\\begin\{(" + "|".join(re.escape(e) for e in DISPLAY_ENVS) + r")\}.*?\\end\{\1\}",
    re.S,
)
_ENV_TOKEN = "@@OCRtranENV{}@@"


def _stash_envs(s: str) -> tuple[str, list[str]]:
    envs: list[str] = []

    def repl(m: re.Match) -> str:
        envs.append(m.group(0).replace("$", ""))  # it is already math; drop stray $
        return _ENV_TOKEN.format(len(envs) - 1)

    return _ENV_BLOCK.sub(repl, s), envs


def esc_text(s: str, fallback: str = "") -> str:
    """Escape LaTeX specials outside ``$...$``; map symbols/sub-superscripts.

    ``fallback`` is a font family used for letters the main font lacks (e.g. Cyrillic
    under a CJK font); empty means no fallback. Display-math environments embedded in
    prose are kept as real math.
    """
    s, envs = _stash_envs(s)
    parts, out = s.split("$"), []
    for i, seg in enumerate(parts):
        if i % 2 == 1:
            out.append("$" + _map_math(seg, fallback) + "$")
        else:
            out.append(_map_text(seg, fallback))
    text = "".join(out)
    for i, env in enumerate(envs):
        text = text.replace(_ENV_TOKEN.format(i), env)
    return text


_TAG = re.compile(r"\\tag\*?\{([^{}]*)\}")


def strip_tags(lx: str) -> str:
    """``\\tag{1}`` is invalid inside ``\\[ ... \\]``; turn it into ``\\qquad (1)``."""
    return _TAG.sub(lambda m: "\\qquad (" + m.group(1) + ")", lx or "")


_TEXT_GROUP = re.compile(r"\\text\{([^{}]*)\}")
_COL_WRAP = {"l": r"\raggedright", "c": r"\centering", "r": r"\raggedleft"}


def _wrap_spec(spec: str, width: str) -> str:
    """Turn l/c/r columns into wrapping ``p{...}`` columns (long cells wrap)."""
    out: list[str] = []
    i = 0
    while i < len(spec):
        ch = spec[i]
        if ch == "{":  # a group belonging to >{...} @{...} !{...}
            depth, j = 0, i
            while j < len(spec):
                if spec[j] == "{":
                    depth += 1
                elif spec[j] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            out.append(spec[i : j + 1])
            i = j + 1
        elif ch in _COL_WRAP:
            out.append(">{" + _COL_WRAP[ch] + r"\arraybackslash}p{" + width + "}")
            i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def wrap_table_cells(s: str) -> str:
    """Make a table's cells wrap: l/c/r columns -> p{} columns; also multicolumn."""
    m = _TABLE_HEAD.search(s or "")
    if not m:
        return s
    i = m.end()
    if i >= len(s) or s[i] != "{":
        return s
    depth, j = 0, i
    while j < len(s):
        if s[j] == "{":
            depth += 1
        elif s[j] == "}":
            depth -= 1
            if depth == 0:
                break
        j += 1
    if depth != 0:
        return s
    spec = s[i + 1 : j]
    ncols, d = 0, 0
    for ch in spec:
        if ch == "{":
            d += 1
        elif ch == "}":
            d = max(0, d - 1)
        elif d == 0 and ch in "lcrXpmb":
            ncols += 1
    base = f"{0.90 / max(1, ncols):.3f}\\textwidth"
    s = s[: i + 1] + _wrap_spec(spec, base) + s[j:]

    def multicol(mm: re.Match) -> str:
        n = int(mm.group(1))
        width = f"{0.90 * n / max(1, ncols):.3f}\\textwidth"
        return "\\multicolumn{" + str(n) + "}{" + _wrap_spec(mm.group(2), width) + "}"

    return re.sub(r"\\multicolumn\{(\d+)\}\{([^{}]*)\}", multicol, s)


def fix_text_ellipsis(s: str) -> str:
    """``\\ldots``/``\\dots``/``\\cdots`` are math-only; inside ``\\text{...}`` they are
    undefined and break the compile (common right before CJK text). Use ``…`` there."""

    def repl(m: re.Match) -> str:
        inner = m.group(1)
        for cmd in ("\\ldots", "\\cdots", "\\dots", "\\vdots", "\\ddots"):
            inner = inner.replace(cmd, "…")
        return "\\text{" + inner + "}"

    return _TEXT_GROUP.sub(repl, s or "")


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


def _fallback_font(cfg: PipelineConfig) -> str:
    """Font used for letters the main font lacks (Cyrillic/Latin-ext under CJK)."""
    if cfg.fallback_font:
        return cfg.fallback_font
    return "Noto Serif" if "CJK" in cfg.font_main.upper() else ""


def _doc_header(cfg: PipelineConfig) -> str:
    lb = ""
    if cfg.linebreak_locale:
        lb = f'\\XeTeXlinebreaklocale "{cfg.linebreak_locale}"\n\\XeTeXlinebreakskip=0pt plus 1pt\n'
    return (
        "\\documentclass[border=8pt]{standalone}\n"
        "\\usepackage{amsmath,amssymb,mathtools}\n"
        "\\usepackage{graphicx}\n\\usepackage{xcolor}\n\\usepackage{cancel}\n"
        "\\usepackage{fontspec}\n\\usepackage{array}\n\\usepackage{adjustbox}\n"
        f"\\setmainfont{{{cfg.font_main}}}\n"
        + (f"\\newfontfamily\\{FALLBACK_CMD}{{{_fallback_font(cfg)}}}\n" if _fallback_font(cfg) else "")
        + f"{lb}{cfg.extra_preamble}\n"
        "\\setlength{\\parindent}{0pt}\\setlength{\\parskip}{5pt}\n"
        # European/Serbian math shorthands that are not standard LaTeX
        "\\providecommand{\\tg}{\\operatorname{tg}}\\providecommand{\\ctg}{\\operatorname{ctg}}\n"
        "\\providecommand{\\cotg}{\\operatorname{cotg}}\\providecommand{\\arctg}{\\operatorname{arctg}}\n"
        "\\providecommand{\\arcctg}{\\operatorname{arcctg}}\\providecommand{\\tgh}{\\operatorname{tgh}}\n"
        "\\providecommand{\\ctgh}{\\operatorname{ctgh}}\\providecommand{\\sh}{\\operatorname{sh}}\n"
        "\\providecommand{\\ch}{\\operatorname{ch}}\\providecommand{\\th}{\\operatorname{th}}\n"
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


def text_to_tex(s: str, fallback: str = "") -> str:
    """Escape text and turn blank-line-separated paragraphs into LaTeX paragraphs."""
    s = _normalize_math(s or "")
    paras = [p.strip() for p in re.split(r"\n\s*\n", s.strip()) if p.strip()]
    return "\n\n".join(esc_text(p.replace("\n", " "), fallback) for p in paras)


_HEADING_CMDS = {1: "\\subsection*", 2: "\\subsubsection*", 3: "\\paragraph*"}


def _block_tex(b: dict, fig_names: dict, fallback: str = "") -> str | None:
    """Render one structured block to LaTeX (lists/headings/quotes/theorems/…)."""
    t = b.get("type")
    textsrc = b.get("target") or b.get("source") or ""
    if t == "heading":
        cmd = _HEADING_CMDS.get(int(b.get("level") or 2), "\\subsubsection*")
        return cmd + "{" + text_to_tex(textsrc, fallback) + "}"
    if t == "prose":
        return text_to_tex(textsrc, fallback)
    if t == "quote":
        return "\\begin{quote}\n" + text_to_tex(textsrc, fallback) + "\n\\end{quote}"
    if t == "theorem":
        kind = esc_text(str(b.get("kind") or "Theorem").strip(), fallback)
        name = str(b.get("name") or "").strip()
        head = f"\\textbf{{{kind}" + (f" ({esc_text(name, fallback)})" if name else "") + ".}"
        return "\\begin{quote}\n" + head + " " + text_to_tex(textsrc, fallback) + "\n\\end{quote}"
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
            lines.append("  \\item " + text_to_tex(txt, fallback))
        if not lines:
            return None
        return f"\\begin{{{env}}}\n" + "\n".join(lines) + f"\n\\end{{{env}}}"
    if t in ("math", "table"):
        raw = strip_tags(b.get("latex", ""))
        if t == "table":
            raw = fix_table_spec(raw)
        if not raw.strip():
            return None
        if t == "table":
            # tables are often wider than the text width -> scale down to fit
            return (
                "\\adjustbox{max width=\\textwidth}{$\\displaystyle "
                + fix_text_ellipsis(textify_math(wrap_table_cells(raw), fallback))
                + "$}"
            )
        w = wrap_math(fix_text_ellipsis(textify_math(raw, fallback)))
        if not w:
            return None
        number = str(b.get("number") or "").strip()
        if number and w.endswith("\\]"):
            w = w[:-2] + " \\qquad \\text{" + esc_text(number, fallback) + "} \\]"
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
                inner += "\\\\[2pt]\\small " + text_to_tex(caption, fallback)
            return "\\begin{center}" + inner + "\\end{center}"
        if caption:  # crop failed -> keep the caption so nothing is lost silently
            return "\\begin{center}\\small \\textit{" + text_to_tex(caption, fallback) + "}\\end{center}"
        return None
    # unknown type: treat like prose (backwards compatible)
    return text_to_tex(textsrc, fallback) if textsrc else None


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


def _figure_row_tex(row: layout.FigRow, fig_names: dict, fallback: str = "") -> str:
    """Figures side by side, with captions **full width** below the row.

    Putting each caption inside its (narrow) figure minipage wrapped long captions
    into a tall narrow column; captions now span the text width instead.
    """
    minis: list[str] = []
    caps: list[str] = []
    multi = len(row.figs) > 1
    for i, (fig, w) in enumerate(zip(row.figs, row.widths, strict=True)):
        entry = fig_names.get(id(fig.block))
        name = entry[0] if entry else None
        caption = str(fig.caption or "").strip()
        w = max(0.05, min(0.98, w))
        if name:
            # the minipage is already {w}\textwidth wide, so the image fills it
            minis.append(
                "\\begin{minipage}[t]{"
                + f"{w:.3f}"
                + "\\textwidth}\\centering \\includegraphics[width=\\textwidth]{"
                + name
                + "}\\end{minipage}"
            )
        if caption:
            label = f"({chr(97 + i)}) " if multi else ""
            caps.append(label + text_to_tex(caption, fallback))
    out: list[str] = []
    if minis:
        out.append("\\begin{center}" + "\\hfill".join(minis) + "\\end{center}")
    if caps:
        out.append("\\begin{center}\\small " + " \\quad ".join(caps) + "\\end{center}")
    return "\n".join(out)


def build_page_tex(cfg: PipelineConfig, items: list, fig_names: dict) -> str:
    """One planned output page -> a standalone LaTeX document."""
    fallback = _fallback_font(cfg)
    body: list[str] = []
    numbers: list[str] = []
    for it in items:
        if it.kind == "figrow":
            tex = _figure_row_tex(it.row, fig_names, fallback)
            if tex:
                body.append(tex)
            continue
        b = it.block or {}
        if b.get("type") == "page_number":
            num = str(b.get("text") or b.get("target") or b.get("source") or "").strip()
            if num:
                numbers.append(num)
            continue
        rendered = _block_tex(b, fig_names, fallback)
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
    fallback = _fallback_font(cfg)
    lines = [
        _doc_header(cfg),
        "\\newbox\\mb",
        "\\setbox\\mb=\\vbox{\\begin{minipage}{"
        + cfg.text_width
        + "}\\typeout{TEXTW \\the\\textwidth}\\end{minipage}}",
    ]
    for i, it in enumerate(items):
        tex = (
            _figure_row_tex(it.row, fig_names, fallback)
            if it.kind == "figrow"
            else _block_tex(it.block, fig_names, fallback)
        )
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


def compile_entry(
    cfg: PipelineConfig,
    base: str,
    source_pdf: str,
    entry: dict,
    on_event: Emitter | None = None,
    cancel: CancelToken | None = None,
) -> tuple[list[Path], str | None]:
    """Plan + compile one source page into its output page PDFs.

    Returns ``(pdfs, error)`` where ``error`` is the last XeLaTeX error (or None).
    Sets ``entry["build_error"]`` on failure so the error guard can act on it.
    """
    cancel = cancel or CancelToken()
    if entry.get("blank") or not entry.get("blocks"):
        return [], None
    page = int(entry["page"])
    tdir = paths.tex_dir(cfg.workdir, base)
    tdir.mkdir(parents=True, exist_ok=True)
    parts = build_pages(cfg, base, source_pdf, entry)
    if not parts:
        entry["build_error"] = "no page produced"
        return [], entry["build_error"]
    pdfs: list[Path] = []
    last_err: str | None = None
    for part, tex in parts:
        cancel.check()
        tex_path = paths.page_tex(cfg.workdir, base, page, part)
        tex_path.write_text(tex)
        ok, log = compile_tex(tex_path, tdir)
        if ok:
            pdfs.append(paths.page_pdf(cfg.workdir, base, page, part))
            emit(
                on_event, Event("build", "ok", base=base, page=page, data={"part": part, "parts": len(parts)})
            )
        else:
            last_err = log
            emit(on_event, Event("build", "error", base=base, page=page, message=log))
    if not pdfs:
        entry["build_error"] = last_err or "no part compiled"
    else:
        entry.pop("build_error", None)
        entry.pop("guard", None)  # a page that builds needs no guard decision
    return pdfs, last_err if not pdfs else None


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
            built, _err = compile_entry(cfg, base, source_pdf, entry, on_event, cancel)
            if built:
                with lock:
                    pdfs.extend(built)

        parallel_map(build_one, entries, cfg.concurrency)
        out[base] = sorted(pdfs, key=lambda p: p.name)
        cache.save_json(paths.ocr_json(cfg.workdir, base), entries)
    return out
