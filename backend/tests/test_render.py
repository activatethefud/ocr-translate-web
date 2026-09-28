from __future__ import annotations

from PIL import Image

from ocrtran import render


def test_page_count_and_size(tiny_pdf):
    assert render.page_count(tiny_pdf) == 2
    assert render.page_size(tiny_pdf, 1) == (300.0, 400.0)


def test_render_pages_max_px(tiny_pdf, tmp_path):
    made = render.render_pages(tiny_pdf, tmp_path / "pages", dpi=150, max_px=200)
    assert len(made) == 2
    assert [p for p, _ in made] == [1, 2]
    w, h = Image.open(made[0][1]).size
    assert max(w, h) <= 200


def test_render_pages_dpi(tiny_pdf, tmp_path):
    made = render.render_pages(tiny_pdf, tmp_path / "pages", dpi=72, max_px=0)
    # 300x400 pt at 72dpi == same numbers
    assert Image.open(made[0][1]).size == (300, 400)


def test_render_pages_selection(tiny_pdf, tmp_path):
    made = render.render_pages(tiny_pdf, tmp_path / "pages", pages=[2])
    assert [p for p, _ in made] == [2]
    assert made[0][1].name == "p-02.png"


def test_render_figure(tiny_pdf, tmp_path):
    out = tmp_path / "fig.png"
    ok = render.render_figure(tiny_pdf, 1, [0.1, 0.1, 0.6, 0.6], out, target_px=100)
    assert ok and out.exists()
    assert max(Image.open(out).size) <= 102


def test_render_figure_bad_bbox(tiny_pdf, tmp_path):
    assert render.render_figure(tiny_pdf, 1, [0.5, 0.5, 0.5, 0.5], tmp_path / "x.png") is False
