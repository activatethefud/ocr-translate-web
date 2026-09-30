"""Missing-glyph fixes: symbol -> math mapping + fallback font for letters."""

from __future__ import annotations

import pytest

from ocrtran import latex, paths
from ocrtran.config import PipelineConfig


def test_symbols_mapped_to_math():
    assert esc("A → B") == "A $\\to$ B"
    for ch, cmd in [
        ("∈", "\\in"),
        ("∩", "\\cap"),
        ("∪", "\\cup"),
        ("⊆", "\\subseteq"),
        ("⇒", "\\Rightarrow"),
        ("∧", "\\wedge"),
        ("∨", "\\vee"),
        ("≤", "\\le"),
        ("≥", "\\ge"),
        ("⊥", "\\perp"),
        ("−", "-"),
    ]:
        assert cmd in esc(ch), (ch, esc(ch))


def esc(s, **k):
    return latex.esc_text(s, **k)


def test_geometry_shapes_mapped():
    assert "\\blacktriangleright" in esc("▶")
    assert "\\blacksquare" in esc("■")
    assert "\\blacklozenge" in esc("♦")
    assert "\\blacksquare" in esc("▪")


def test_sub_and_superscripts():
    assert esc("a₀b⁵") == "a\\textsubscript{0}b\\textsuperscript{5}"
    assert esc("H₂O") == "H\\textsubscript{2}O"


def test_symbols_inside_math_use_commands_not_nested_math():
    assert esc("$A → B$") == "$A \\to B$"
    assert "$\\to$" not in esc("$A → B$")


def test_fallback_font_wraps_uncovered_letters():
    assert esc("Њ", fallback="Noto Serif") == "{\\glyphfallback Њ}"
    assert esc("Њ") == "Њ"  # no fallback configured -> left alone


def test_fallback_font_auto_for_cjk_main():
    cjk = PipelineConfig(sources=["a.pdf"], font_main="Noto Sans CJK SC")
    latin = PipelineConfig(sources=["a.pdf"], font_main="Noto Serif")
    assert latex._fallback_font(cjk) == "Noto Serif"
    assert latex._fallback_font(latin) == ""
    assert (
        latex._fallback_font(PipelineConfig(sources=["a.pdf"], fallback_font="DejaVu Serif"))
        == "DejaVu Serif"
    )


def test_escaping_is_preserved():
    assert esc("a & b_1 % # {x}") == "a \\& b\\_1 \\% \\# \\{x\\}"
    assert esc("\\{a\\}") == "\\{a\\}"  # set braces


def test_build_page_tex_declares_fallback_for_cjk():
    from ocrtran import layout

    cfg = PipelineConfig(sources=["a.pdf"], font_main="Noto Sans CJK SC")
    items = [layout.Item(kind="block", block={"type": "prose", "target": "текст"})]
    tex = latex.build_page_tex(cfg, items, {})
    assert "\\newfontfamily\\glyphfallback{Noto Serif}" in tex
    assert "{\\glyphfallback т}" in tex  # Cyrillic wrapped


@pytest.mark.integration
def test_symbols_and_cyrillic_compile_without_missing_glyphs(tiny_pdf, tmp_path, xelatex_available):
    if not xelatex_available:
        pytest.skip("xelatex not installed")
    cfg = PipelineConfig(
        sources=[str(tiny_pdf)],
        workdir=str(tmp_path / "w"),
        target_lang="Chinese (Simplified)",
        font_main="Noto Sans CJK SC",
        layout_mode="single",
        output_page_size="a4",
    )
    target = "A → B, x ∈ A ∩ B ⊆ C, њ č š, a₀ × b⁵ ▶ ■ ♦ ⇒ ≤ ⊥"
    entry = {"page": 1, "tight": [], "blocks": [{"type": "prose", "source": "x", "target": target}]}
    latex.run_build(cfg, {tiny_pdf.stem: [entry]})
    assert paths.page_pdfs(cfg.workdir, tiny_pdf.stem, 1)
    log = (paths.tex_dir(cfg.workdir, tiny_pdf.stem) / "p01.log").read_text(errors="ignore")
    assert "Missing character" not in log


def test_cyrillic_inside_math_uses_fallback():
    out = esc("$A њ B$", fallback="Noto Serif")
    assert "{\\glyphfallback њ}" in out
    assert "\\text{њ}" not in out


def test_cyrillic_in_math_block_latex_uses_fallback():
    from ocrtran.latex import _block_tex

    tex = _block_tex({"type": "math", "latex": "a = њ + 1"}, {}, "Noto Serif")
    assert "{\\glyphfallback њ}" in tex


def test_cjk_inside_math_not_sent_to_latin_fallback():
    out = esc("$x \\text{中文}$", fallback="Noto Serif")
    assert "{\\glyphfallback 中文}" not in out  # CJK stays with the main font


def test_theorem_name_cyrillic_uses_fallback():
    from ocrtran.latex import _block_tex

    tex = _block_tex(
        {"type": "theorem", "kind": "Solution", "name": "РЕШЕЊЕ", "target": "text"}, {}, "Noto Serif"
    )
    assert "{\\glyphfallback Р}" in tex  # wrapped per character
    assert "(РЕШЕЊЕ)" not in tex  # nothing left unwrapped


def test_latin_ext_inside_math_uses_text_and_fallback():
    out = esc("$Površina$", fallback="Noto Serif")
    assert "\\text{{\\glyphfallback š}}" in out


# -- bare LaTeX math commands left in prose --------------------------------
def test_bare_math_command_wrapped_in_inline_math():
    assert esc(r"c = \sqrt{289} = 17") == r"c = $\sqrt{289}$ = 17"
    assert esc(r"a \cdot b") == r"a $\cdot$ b"


def test_bare_frac_with_two_groups():
    assert esc(r"P = \frac{a h_a}{2}") == r"P = $\frac{a h_a}{2}$"


def test_bare_math_command_with_optional_arg():
    assert esc(r"\sqrt[3]{x}") == r"$\sqrt[3]{x}$"


def test_unknown_command_is_still_escaped_not_math():
    assert esc(r"unknown \foo{x}") == r"unknown \textbackslash{}foo\{x\}"


def test_bare_math_command_with_symbol_inside():
    # a unicode symbol inside the command arguments is still mapped
    assert esc(r"\sqrt{a − b}") == r"$\sqrt{a - b}$"


@pytest.mark.integration
def test_bare_sqrt_compiles_and_has_no_literal_backslash(tiny_pdf, tmp_path, xelatex_available):
    if not xelatex_available:
        pytest.skip("xelatex not installed")
    cfg = PipelineConfig(
        sources=[str(tiny_pdf)],
        workdir=str(tmp_path / "w"),
        layout_mode="single",
        output_page_size="a4",
        font_main="Noto Serif",
    )
    target = r"Thus c = \sqrt{82 + 152} = \sqrt{289} = 17 and P = \frac{a h_a}{2} \cdot 2."
    entry = {"page": 1, "tight": [], "blocks": [{"type": "prose", "source": "x", "target": target}]}
    latex.run_build(cfg, {tiny_pdf.stem: [entry]})
    pdfs = paths.page_pdfs(cfg.workdir, tiny_pdf.stem, 1)
    assert pdfs
    import fitz

    with fitz.open(pdfs[0]) as d:
        text = d[0].get_text()
    assert "\\sqrt" not in text and "\\frac" not in text and "\\cdot" not in text
    assert "289" in text and "17" in text
