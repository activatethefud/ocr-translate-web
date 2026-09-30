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
