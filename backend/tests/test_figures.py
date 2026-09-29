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


def test_build_tex_prefers_tight_box(tiny_pdf, tmp_path):
    cfg = PipelineConfig(sources=[str(tiny_pdf)], workdir=str(tmp_path / "w"), figure_pad=0.0)
    entry = {
        "page": 1,
        # a loose main box must NOT be unioned in; the tight box wins
        "blocks": [{"type": "figure", "bbox": [0.1, 0.1, 0.7, 0.4]}],
        "tight": [[0.2, 0.2, 0.3, 0.3]],
    }
    tex = latex.build_tex(cfg, tiny_pdf.stem, str(tiny_pdf), entry)
    assert "0.30\\textwidth" in tex


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


class _JudgeProvider:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def vision(self, image_path, prompt, max_tokens=None):
        self.calls += 1
        return self.payload

    def text(self, prompt, max_tokens=None):
        return ""


def test_judge_returns_corrected_box(tmp_path):
    from ocrtran import figures

    p = _JudgeProvider('{"figures":[{"index":0,"bbox":[0.05,0.05,0.5,0.5]}]}')
    figs = [{"bbox": [0.1, 0.1, 0.4, 0.4], "description": "graph"}]
    assert figures.judge_bboxes(p, str(tmp_path / "x.png"), figs) == [[0.05, 0.05, 0.5, 0.5]]


def test_judge_invalid_json_returns_none(tmp_path):
    from ocrtran import figures

    p = _JudgeProvider("sorry, no json")
    assert figures.judge_bboxes(p, "x", [{"bbox": [0.1, 0.1, 0.4, 0.4]}]) == [None]


def test_judge_empty_figures_makes_no_call(tmp_path):
    from ocrtran import figures

    p = _JudgeProvider("{}")
    assert figures.judge_bboxes(p, "x", []) == [] and p.calls == 0


def test_judge_ignores_bad_index_and_box(tmp_path):
    from ocrtran import figures

    payload = '{"figures":[{"index":9,"bbox":[0,0,1,1]},{"index":0,"bbox":[0.1,0.1,0.2,0.2]}]}'
    out = figures.judge_bboxes(_JudgeProvider(payload), "x", [{"bbox": [0, 0, 0.5, 0.5]}])
    assert out == [[0.1, 0.1, 0.2, 0.2]]


def test_apply_judge_is_monotonic():
    from ocrtran.figures import apply_judge

    assert apply_judge([[0.2, 0.2, 0.4, 0.4]], [[0.1, 0.1, 0.5, 0.5]]) == [[0.1, 0.1, 0.5, 0.5]]
    assert apply_judge([[0.2, 0.2, 0.4, 0.4]], [None]) == [[0.2, 0.2, 0.4, 0.4]]


def test_render_figure_auto_expand_reduces_border_ink(tmp_path):
    import fitz

    from ocrtran.render import _border_ink

    pdf = tmp_path / "black.pdf"
    d = fitz.open()
    page = d.new_page(width=300, height=300)
    page.draw_rect(fitz.Rect(100, 100, 200, 200), color=None, fill=(0, 0, 0))
    d.save(pdf)
    d.close()
    bbox = [0.4, 0.4, 0.55, 0.55]  # fully inside the black square -> border all black
    no = tmp_path / "no.png"
    au = tmp_path / "au.png"
    render.render_figure(pdf, 1, bbox, no, pad_frac=0.0, min_pad_pt=0.0, auto_expand=False)
    render.render_figure(pdf, 1, bbox, au, pad_frac=0.0, min_pad_pt=0.0, auto_expand=True)
    assert _border_ink(no) > _border_ink(au)  # expansion pulled in white margin
