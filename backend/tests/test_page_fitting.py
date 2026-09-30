"""End-to-end page fitting: shrinking vs splitting (local XeLaTeX, no model)."""

from __future__ import annotations

import fitz
import pytest

from ocrtran import latex, paths
from ocrtran.config import PipelineConfig

PARA = "Neka je f neprekidna funkcija na intervalu [a, b] i neka je F njena primitivna funkcija. " * 8
BOXES = [(0.05, 0.62, 0.30, 0.82), (0.35, 0.62, 0.60, 0.82), (0.65, 0.62, 0.90, 0.82)]


def _page_with_figures(path) -> None:
    d = fitz.open()
    p = d.new_page(width=595, height=842)
    for i in range(14):
        p.insert_text((40, 60 + i * 16), f"Line {i + 1}: text on the page.", fontsize=11)
    for b in BOXES:
        p.draw_rect(fitz.Rect(b[0] * 595, b[1] * 842, b[2] * 595, b[3] * 842), color=(0, 0, 0), width=2)
    d.save(path)
    d.close()


def _entry():
    blocks = [{"type": "prose", "source": "x", "target": PARA} for _ in range(8)]
    blocks += [{"type": "figure", "bbox": list(b), "description": "d"} for b in BOXES]
    return {"page": 1, "tight": [list(b) for b in BOXES], "blocks": blocks}


def _scales(cfg, base) -> list[float]:
    aw, ah = 595 - 48, 842 - 48  # A4-ish minus margins (output_page_size=match)
    out = []
    for part in paths.page_pdfs(cfg.workdir, base, 1):
        with fitz.open(part) as doc:
            r = doc[0].rect
        out.append(min(aw / r.width, ah / r.height))
    return out


@pytest.mark.integration
def test_auto_layout_splits_instead_of_shrinking(tmp_path, xelatex_available):
    if not xelatex_available:
        pytest.skip("xelatex not installed")
    src = tmp_path / "src.pdf"
    _page_with_figures(src)
    base = "src"

    single = PipelineConfig(
        sources=[str(src)],
        workdir=str(tmp_path / "single"),
        source_lang="Serbian",
        target_lang="English",
        font_main="Noto Serif",
        layout_mode="single",
        output_page_size="match",
    )
    auto = PipelineConfig(
        sources=[str(src)],
        workdir=str(tmp_path / "auto"),
        source_lang="Serbian",
        target_lang="English",
        font_main="Noto Serif",
        layout_mode="auto",
        output_page_size="match",
    )
    latex.run_build(single, {base: [_entry()]})
    latex.run_build(auto, {base: [_entry()]})

    s_scales = _scales(single, base)
    a_scales = _scales(auto, base)

    assert len(s_scales) == 1  # old behaviour: one shrunk page
    assert len(a_scales) >= 2  # new: flows onto extra pages
    assert min(a_scales) > min(s_scales)  # text stays bigger
    # consistent size across the split pages
    assert max(a_scales) - min(a_scales) < 1e-6


@pytest.mark.integration
def test_figure_row_stays_side_by_side(tmp_path, xelatex_available):
    if not xelatex_available:
        pytest.skip("xelatex not installed")
    src = tmp_path / "src.pdf"
    _page_with_figures(src)
    cfg = PipelineConfig(
        sources=[str(src)],
        workdir=str(tmp_path / "w"),
        source_lang="Serbian",
        target_lang="English",
        font_main="Noto Serif",
        layout_mode="auto",
        output_page_size="match",
        figure_layout="preserve",
    )
    tex = latex.build_tex(cfg, "src", str(src), _entry())
    parts = latex.build_pages(cfg, "src", str(src), _entry())
    lines = [ln for _, t in parts for ln in t.splitlines() if "includegraphics" in ln]
    assert lines, "figure row not found in any part"
    line = lines[0]
    assert line.count("includegraphics") == 3  # all three figures on one line
    assert line.count("\\hfill") == 2
    assert tex  # first page still builds
