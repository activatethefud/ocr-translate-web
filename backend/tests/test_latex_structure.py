from __future__ import annotations

import pytest

from ocrtran.config import PipelineConfig
from ocrtran.latex import _block_tex, build_tex, text_to_tex


def test_text_to_tex_splits_paragraphs():
    out = text_to_tex("First paragraph.\n\nSecond paragraph.")
    assert "First paragraph." in out and "Second paragraph." in out
    assert "\n\n" in out  # two LaTeX paragraphs


def test_text_to_tex_escapes_outside_math_only():
    out = text_to_tex("A & B and $x_1 + y$")
    assert "\\&" in out and "$x_1 + y$" in out


def test_heading_levels():
    assert _block_tex({"type": "heading", "level": 1, "target": "Title"}, {}).startswith("\\subsection*")
    assert _block_tex({"type": "heading", "level": 2, "target": "Sec"}, {}).startswith("\\subsubsection*")
    assert _block_tex({"type": "heading", "level": 3, "target": "Sub"}, {}).startswith("\\paragraph*")


def test_ordered_list_becomes_enumerate():
    block = {
        "type": "list",
        "ordered": True,
        "items": [{"target": "First"}, {"target": "Second"}, {"target": "Third"}],
    }
    tex = _block_tex(block, {})
    assert "\\begin{enumerate}" in tex and tex.count("\\item") == 3
    assert "First" in tex and "Third" in tex


def test_unordered_list_becomes_itemize():
    block = {"type": "list", "ordered": False, "items": [{"target": "a"}, {"target": "b"}]}
    tex = _block_tex(block, {})
    assert "\\begin{itemize}" in tex and tex.count("\\item") == 2


def test_empty_list_is_skipped():
    assert _block_tex({"type": "list", "items": []}, {}) is None


def test_quote_environment():
    tex = _block_tex({"type": "quote", "target": "Quoted words."}, {})
    assert "\\begin{quote}" in tex and "Quoted words." in tex


def test_theorem_environment():
    block = {"type": "theorem", "kind": "Theorem", "name": "Thales", "target": "Then a/b = c/d."}
    tex = _block_tex(block, {})
    assert "\\textbf{Theorem (Thales).}" in tex
    assert "\\begin{quote}" in tex and "Then a/b = c/d." in tex


def test_numbered_math_gets_number():
    tex = _block_tex({"type": "math", "latex": "a = b", "number": "(1)"}, {})
    assert "a = b" in tex and "\\text{(1)}" in tex


def test_math_display_env_not_nested():
    src = "\\begin{align}\na &= b\n\\end{align}"
    assert _block_tex({"type": "math", "latex": src}, {}) == src


def test_figure_with_caption():
    block = {"type": "figure", "bbox": [0, 0, 1, 1], "caption": "Figure 1: a graph"}
    tex = _block_tex(block, {id(block): ("fig_1_0.png", 0.4)})
    assert "\\includegraphics" in tex and "Figure 1: a graph" in tex


def test_unknown_type_falls_back_to_text():
    assert "Hello" in _block_tex({"type": "mystery", "target": "Hello"}, {})


@pytest.mark.integration
def test_build_tex_renders_list(tiny_pdf, tmp_path, xelatex_available):
    if not xelatex_available:
        pytest.skip("xelatex not installed")
    from ocrtran.config import PipelineConfig

    cfg = PipelineConfig(sources=[str(tiny_pdf)], workdir=str(tmp_path / "w"), font_main="Noto Serif")
    entry = {
        "page": 1,
        "tight": [],
        "blocks": [
            {"type": "heading", "level": 1, "target": "Steps"},
            {
                "type": "list",
                "ordered": True,
                "items": [{"target": "Alpha"}, {"target": "Beta"}, {"target": "Gamma"}],
            },
        ],
    }
    tex = build_tex(cfg, tiny_pdf.stem, str(tiny_pdf), entry)
    assert "\\begin{enumerate}" in tex
    from ocrtran import latex, paths

    tex_path = paths.page_tex(cfg.workdir, tiny_pdf.stem, 1)
    tex_path.parent.mkdir(parents=True, exist_ok=True)
    tex_path.write_text(tex)
    ok, log = latex.compile_tex(tex_path, tex_path.parent)
    assert ok, log
    import fitz

    doc = fitz.open(paths.page_pdf(cfg.workdir, tiny_pdf.stem, 1))
    text = doc[0].get_text()
    doc.close()
    for item in ("Alpha", "Beta", "Gamma"):
        assert item in text


def test_double_dollar_math_is_not_escaped():
    out = text_to_tex("ona $$(\\forall x \\in A)(f(x)=y)$$ dalje")
    assert "\\forall" in out and "\\textbackslash" not in out
    assert "$$" not in out


def test_paren_math_delimiters_normalised():
    out = text_to_tex("vredi \\(x_1 \\neq x_2\\) ovde")
    assert "\\neq" in out and "\\(" not in out


def test_ordered_list_strips_marker():
    block = {
        "type": "list",
        "ordered": True,
        "items": [{"target": "1) first thing"}, {"target": "ii. second thing"}],
    }
    tex = _block_tex(block, {})
    assert "1)" not in tex and "ii." not in tex
    assert "first thing" in tex and "second thing" in tex


def test_unordered_list_keeps_marker_like_text():
    block = {"type": "list", "ordered": False, "items": [{"target": "1) keep me"}]}
    assert "1) keep me" in _block_tex(block, {})


def test_markdown_bold_stripped():
    out = text_to_tex("**important result** here")
    assert "**" not in out and "important result" in out


def test_figure_without_crop_keeps_caption():
    block = {"type": "figure", "bbox": [0.1, 0.1, 0.4, 0.4], "caption": "Slika 1.2"}
    tex = _block_tex(block, {id(block): (None, 0.3)})
    assert "includegraphics" not in tex and "Slika 1.2" in tex


def test_figure_without_crop_or_caption_is_skipped():
    block = {"type": "figure", "bbox": [0.1, 0.1, 0.4, 0.4]}
    assert _block_tex(block, {id(block): (None, 0.3)}) is None


def test_build_tex_missing_crop_keeps_caption(tiny_pdf, tmp_path, monkeypatch):
    from ocrtran import render as render_mod

    monkeypatch.setattr(render_mod, "render_figure", lambda *a, **k: False)  # crop fails
    from ocrtran.config import PipelineConfig

    cfg = PipelineConfig(sources=[str(tiny_pdf)], workdir=str(tmp_path / "w"))
    entry = {
        "page": 1,
        "tight": [],
        "blocks": [
            {"type": "figure", "bbox": [0.1, 0.1, 0.4, 0.4], "caption": "Fig X"},
        ],
    }
    tex = build_tex(cfg, tiny_pdf.stem, str(tiny_pdf), entry)
    assert "includegraphics" not in tex and "Fig X" in tex


def test_build_tex_skips_empty_blocks(tiny_pdf, tmp_path):
    from ocrtran import latex, paths

    cfg = PipelineConfig(sources=[str(tiny_pdf)], workdir=str(tmp_path / "w"))
    entry = {"page": 1, "tight": [], "blocks": []}
    assert latex.build_tex(cfg, tiny_pdf.stem, str(tiny_pdf), entry)
    # run_build must not produce a page PDF for a block-less page
    latex.run_build(cfg, {tiny_pdf.stem: [entry]})
    assert not paths.page_pdf(cfg.workdir, tiny_pdf.stem, 1).exists()


def test_page_number_is_rendered_as_footer(tiny_pdf, tmp_path):
    from ocrtran import latex

    cfg = PipelineConfig(sources=[str(tiny_pdf)], workdir=str(tmp_path / "w"))
    entry = {
        "page": 1,
        "tight": [],
        "blocks": [
            {"type": "prose", "source": "hello", "target": "hello"},
            {"type": "page_number", "text": "12"},
        ],
    }
    tex = latex.build_tex(cfg, tiny_pdf.stem, str(tiny_pdf), entry)
    assert "12" in tex
    assert tex.index("12") > tex.index("hello")  # footer comes after the content


def test_block_tex_ignores_page_number_in_flow():
    from ocrtran.latex import _block_tex

    assert _block_tex({"type": "page_number", "text": "3"}, {}) is None


# -- page fitting (figure rows + pagination) --------------------------------
def _row2():
    from ocrtran import layout

    b1, b2 = {"type": "figure"}, {"type": "figure"}
    f1 = layout.Fig(block=b1, bbox=(0.0, 0.0, 0.3, 0.2), width=0.3, aspect=0.5)
    f2 = layout.Fig(block=b2, bbox=(0.4, 0.0, 0.7, 0.2), width=0.3, aspect=0.5)
    return layout.FigRow([f1, f2], [0.4, 0.4]), {id(b1): ("a.png", 0.3), id(b2): ("b.png", 0.3)}


def test_figure_row_tex_side_by_side():
    row, names = _row2()
    from ocrtran import latex

    tex = latex._figure_row_tex(row, names)
    assert tex.count("\\begin{minipage}") == 2  # two figures on one line
    assert "\\hfill" in tex
    assert "a.png" in tex and "b.png" in tex


def test_build_page_tex_renders_figure_row():
    from ocrtran import latex, layout

    row, names = _row2()
    cfg = PipelineConfig(sources=["a.pdf"])
    tex = latex.build_page_tex(cfg, [layout.Item(kind="figrow", row=row)], names)
    assert "minipage" in tex and "\\begin{document}" in tex


@pytest.mark.integration
def test_build_pages_splits_long_content(tiny_pdf, tmp_path, xelatex_available):
    if not xelatex_available:
        pytest.skip("xelatex not installed")
    from ocrtran import latex

    cfg = PipelineConfig(
        sources=[str(tiny_pdf)], workdir=str(tmp_path / "w"), layout_mode="auto", output_page_size="a4"
    )
    blocks = [{"type": "prose", "source": "x", "target": "word " * 500} for _ in range(5)]
    parts = latex.build_pages(cfg, tiny_pdf.stem, str(tiny_pdf), {"page": 1, "tight": [], "blocks": blocks})
    assert len(parts) >= 2  # overflows instead of shrinking
    assert all("\\begin{document}" in tex for _, tex in parts)


@pytest.mark.integration
def test_build_pages_single_mode_one_part(tiny_pdf, tmp_path, xelatex_available):
    if not xelatex_available:
        pytest.skip("xelatex not installed")
    from ocrtran import latex

    cfg = PipelineConfig(sources=[str(tiny_pdf)], workdir=str(tmp_path / "w"), layout_mode="single")
    blocks = [{"type": "prose", "source": "x", "target": "word " * 500} for _ in range(5)]
    parts = latex.build_pages(cfg, tiny_pdf.stem, str(tiny_pdf), {"page": 1, "tight": [], "blocks": blocks})
    assert len(parts) == 1


@pytest.mark.integration
def test_run_build_writes_all_parts(tiny_pdf, tmp_path, xelatex_available):
    if not xelatex_available:
        pytest.skip("xelatex not installed")
    from ocrtran import latex, paths

    cfg = PipelineConfig(
        sources=[str(tiny_pdf)], workdir=str(tmp_path / "w"), layout_mode="auto", output_page_size="a4"
    )
    blocks = [{"type": "prose", "source": "x", "target": "word " * 500} for _ in range(5)]
    latex.run_build(cfg, {tiny_pdf.stem: [{"page": 1, "tight": [], "blocks": blocks}]})
    assert len(paths.page_pdfs(cfg.workdir, tiny_pdf.stem, 1)) >= 2
