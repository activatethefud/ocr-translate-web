"""Figure crops must not cut off the diagram (the model's boxes are sometimes tight)."""

from __future__ import annotations

from pathlib import Path

import fitz
from PIL import Image

from ocrtran import latex, paths, render
from ocrtran.config import PipelineConfig

# a red patch just OUTSIDE a black square: if the crop includes red, it reached
# beyond the (too tight) black box.
RED = (80, 80, 90, 90)
BLACK = (100, 100, 110, 110)
BBOX_BLACK = [BLACK[0] / 300, BLACK[1] / 300, BLACK[2] / 300, BLACK[3] / 300]


def _marker_pdf(path: Path) -> Path:
    doc = fitz.open()
    page = doc.new_page(width=300, height=300)
    page.draw_rect(fitz.Rect(*RED), color=None, fill=(1, 0, 0))
    page.draw_rect(fitz.Rect(*BLACK), color=None, fill=(0, 0, 0))
    doc.save(path)
    doc.close()
    return path


def _has_red(p: Path) -> bool:
    im = Image.open(p).convert("RGB")
    return any(r > 150 and g < 100 and b < 100 for r, g, b in im.getdata())


def test_render_figure_without_padding_is_tight(tmp_path):
    pdf = _marker_pdf(tmp_path / "m.pdf")
    out = tmp_path / "fig.png"
    assert render.render_figure(pdf, 1, BBOX_BLACK, out, pad_frac=0.0, min_pad_pt=0)
    assert not _has_red(out)  # exact box -> no red


def test_render_figure_padding_reaches_surroundings(tmp_path):
    pdf = _marker_pdf(tmp_path / "m.pdf")
    out = tmp_path / "fig.png"
    assert render.render_figure(pdf, 1, BBOX_BLACK, out, pad_frac=1.2, min_pad_pt=0)
    assert _has_red(out)  # padded box -> includes the neighbouring red patch


def test_render_figure_clamps_at_page_edge(tmp_path):
    pdf = _marker_pdf(tmp_path / "m.pdf")
    out = tmp_path / "fig.png"
    assert render.render_figure(pdf, 1, [0.0, 0.0, 0.05, 0.05], out, pad_frac=0.5)
    assert out.exists()


def test_render_figure_degenerate_box(tmp_path):
    pdf = _marker_pdf(tmp_path / "m.pdf")
    assert render.render_figure(pdf, 1, [0.5, 0.5, 0.5, 0.5], tmp_path / "x.png") is False


def test_build_tex_uses_union_of_boxes(tiny_pdf, tmp_path):
    cfg = PipelineConfig(sources=[str(tiny_pdf)], workdir=str(tmp_path / "w"), figure_pad=0.0)
    entry = {
        "page": 1,
        # main box is much wider than the tight box -> union width 0.6 -> width 0.75
        "blocks": [{"type": "figure", "bbox": [0.1, 0.1, 0.7, 0.4]}],
        "tight": [[0.2, 0.2, 0.3, 0.3]],
    }
    tex = latex.build_tex(cfg, tiny_pdf.stem, str(tiny_pdf), entry)
    assert "0.75\\textwidth" in tex


def test_build_tex_creates_figure_file(tiny_pdf, tmp_path):
    cfg = PipelineConfig(sources=[str(tiny_pdf)], workdir=str(tmp_path / "w"))
    entry = {
        "page": 1,
        "blocks": [{"type": "figure", "bbox": [0.1, 0.1, 0.5, 0.4]}],
        "tight": [],
    }
    latex.build_tex(cfg, tiny_pdf.stem, str(tiny_pdf), entry)
    assert paths.figure_path(cfg.workdir, tiny_pdf.stem, 1, 0).exists()


def test_build_tex_figure_pad_config(tmp_path):
    pdf = _marker_pdf(tmp_path / "m.pdf")
    tight = [[BLACK[0] / 300, BLACK[1] / 300, BLACK[2] / 300, BLACK[3] / 300]]
    entry = {"page": 1, "blocks": [{"type": "figure", "bbox": BBOX_BLACK}], "tight": tight}

    cfg0 = PipelineConfig(sources=[str(pdf)], workdir=str(tmp_path / "w0"), figure_pad=0.0)
    latex.build_tex(cfg0, pdf.stem, str(pdf), entry)
    assert not _has_red(paths.figure_path(cfg0.workdir, pdf.stem, 1, 0))

    cfg1 = PipelineConfig(sources=[str(pdf)], workdir=str(tmp_path / "w1"), figure_pad=1.2)
    latex.build_tex(cfg1, pdf.stem, str(pdf), entry)
    assert _has_red(paths.figure_path(cfg1.workdir, pdf.stem, 1, 0))
