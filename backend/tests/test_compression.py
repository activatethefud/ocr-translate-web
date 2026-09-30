"""Figure-crop compression: right-sized resolution + png/jpeg choice."""

from __future__ import annotations

from pathlib import Path

import fitz
import pytest

from ocrtran import latex, paths, render
from ocrtran.config import PipelineConfig


def _imsize(p: Path) -> tuple[int, int]:
    from PIL import Image

    with Image.open(p) as im:
        return im.size


def _rect_pdf(path: Path, w: int = 400, h: int = 300) -> None:
    d = fitz.open()
    p = d.new_page(width=w, height=h)
    p.draw_rect(fitz.Rect(40, 40, 160, 140), color=(0, 0, 0), fill=(0.9, 0.9, 0.9))
    d.save(path)
    d.close()


def _photo_pdf(path: Path, tmp: Path) -> None:
    from PIL import Image

    im = Image.new("RGB", (160, 120))
    for x in range(160):
        for y in range(120):
            im.putpixel((x, y), (x * 255 // 160, y * 255 // 120, (x + y) % 256))
    g = tmp / "grad.png"
    im.save(g)
    d = fitz.open()
    p = d.new_page(width=400, height=300)
    p.insert_image(fitz.Rect(40, 40, 200, 180), filename=str(g))
    d.save(path)
    d.close()


# -- render_figure: resolution + format -------------------------------------
def test_render_figure_respects_target_px(tmp_path):
    pdf = tmp_path / "r.pdf"
    _rect_pdf(pdf)
    out = render.render_figure(pdf, 1, [0.05, 0.05, 0.95, 0.9], tmp_path / "f.png", target_px=128, fmt="png")
    assert out is not None and out.suffix == ".png"
    w, h = _imsize(out)
    assert 100 <= max(w, h) <= 140  # right-sized, not the old fixed 1800


def test_render_figure_jpeg_and_png(tmp_path):
    pdf = tmp_path / "r.pdf"
    _rect_pdf(pdf)
    jpg = render.render_figure(
        pdf, 1, [0.05, 0.05, 0.95, 0.9], tmp_path / "f.png", target_px=200, fmt="jpeg", jpeg_quality=80
    )
    assert jpg is not None and jpg.suffix == ".jpg" and jpg.exists()
    png = render.render_figure(pdf, 1, [0.05, 0.05, 0.95, 0.9], tmp_path / "g.png", target_px=200, fmt="png")
    assert png is not None and png.suffix == ".png"


def test_render_figure_auto_picks_jpeg_for_photos(tmp_path):
    photo = tmp_path / "p.pdf"
    _photo_pdf(photo, tmp_path)
    out = render.render_figure(
        photo, 1, [0.05, 0.05, 0.6, 0.7], tmp_path / "f.png", target_px=200, fmt="auto"
    )
    assert out is not None and out.suffix == ".jpg"


def test_render_figure_auto_picks_png_for_line_art(tmp_path):
    pdf = tmp_path / "r.pdf"
    _rect_pdf(pdf)
    out = render.render_figure(pdf, 1, [0.05, 0.05, 0.95, 0.9], tmp_path / "f.png", target_px=200, fmt="auto")
    assert out is not None and out.suffix == ".png"


def test_config_validates_compression_fields():
    from ocrtran.config import ConfigError

    assert PipelineConfig(sources=["a.pdf"], figure_dpi=200).figure_dpi == 200
    assert PipelineConfig(sources=["a.pdf"], figure_format="jpeg").figure_format == "jpeg"
    with pytest.raises(ConfigError):
        PipelineConfig(sources=["a.pdf"], figure_format="webp").validate()
    with pytest.raises(ConfigError):
        PipelineConfig(sources=["a.pdf"], figure_dpi=0).validate()
    with pytest.raises(ConfigError):
        PipelineConfig(sources=["a.pdf"], jpeg_quality=0).validate()


# -- crop resolution follows dpi + placement (integration) -------------------
@pytest.mark.integration
def test_crop_resolution_follows_dpi_and_placement(tmp_path, xelatex_available):
    if not xelatex_available:
        pytest.skip("xelatex not installed")
    src = tmp_path / "src.pdf"
    d = fitz.open()
    p = d.new_page(width=595, height=842)
    p.insert_text((40, 60), "some text", fontsize=12)
    p.draw_rect(fitz.Rect(60, 120, 178, 220), color=(0, 0, 0), fill=(0.9, 0.9, 0.9))
    d.save(src)
    d.close()
    entry = {
        "page": 1,
        "tight": [[0.10, 0.14, 0.30, 0.27]],  # a small figure (~0.2 of the page wide)
        "blocks": [
            {"type": "prose", "source": "x", "target": "hello " * 40},
            {"type": "figure", "bbox": [0.10, 0.14, 0.30, 0.27]},
        ],
    }
    sizes: dict[int, int] = {}
    for dpi in (150, 600):
        cfg = PipelineConfig(
            sources=[str(src)],
            workdir=str(tmp_path / f"w{dpi}"),
            layout_mode="auto",
            output_page_size="a4",
            figure_dpi=dpi,
            figure_format="png",
        )
        latex.run_build(cfg, {"src": [entry]})
        crops = list(paths.tex_dir(cfg.workdir, "src").glob("fig_1_*.png"))
        assert crops
        sizes[dpi] = max(_imsize(crops[0]))
    assert sizes[600] > sizes[150] * 3  # resolution tracks figure_dpi

    cfg = PipelineConfig(
        sources=[str(src)], workdir=str(tmp_path / "wdef"), layout_mode="auto", output_page_size="a4"
    )
    latex.run_build(cfg, {"src": [entry]})
    crop = max(_imsize(list(paths.tex_dir(cfg.workdir, "src").glob("fig_1_*.png"))[0]))
    assert crop < 800  # default (300 dpi, scaled to placement) — was a fixed 1800
