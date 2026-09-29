from __future__ import annotations

import fitz
import pytest

from ocrtran import assemble, ocr, paths, render
from ocrtran.config import PipelineConfig
from tests.conftest import FakeProvider, make_translated_page


def _pdf_with_blank(path) -> None:
    d = fitz.open()
    d.new_page(width=300, height=400)  # blank first page
    p = d.new_page(width=300, height=400)
    p.draw_rect(fitz.Rect(20, 20, 280, 380), color=None, fill=(0, 0, 0))  # inked page
    d.save(path)
    d.close()


def test_page_ink_ratio(tmp_path):
    src = tmp_path / "b.pdf"
    _pdf_with_blank(src)
    assert render.page_ink_ratio(src, 1) < 0.01
    assert render.page_ink_ratio(src, 2) > 0.3
    assert render.is_blank(src, 1) is True
    assert render.is_blank(src, 2) is False


def test_run_ocr_skips_blank_pages(tmp_path):
    src = tmp_path / "b.pdf"
    _pdf_with_blank(src)
    cfg = PipelineConfig(
        sources=[str(src)], workdir=str(tmp_path / "w"), pages="all", concurrency=1, skip_blank_pages=True
    )
    prov = FakeProvider()
    res = ocr.run_ocr(cfg, prov)
    entries = res[src.stem]
    assert entries[0]["blank"] is True and entries[0]["blocks"] == []
    assert prov.calls.count("vision") == 1  # only the inked page was OCR'd


def test_run_ocr_can_disable_blank_skip(tmp_path):
    src = tmp_path / "b.pdf"
    _pdf_with_blank(src)
    cfg = PipelineConfig(
        sources=[str(src)], workdir=str(tmp_path / "w"), pages="all", concurrency=1, skip_blank_pages=False
    )
    prov = FakeProvider()
    ocr.run_ocr(cfg, prov)
    assert prov.calls.count("vision") == 2  # both pages OCR'd


@pytest.mark.integration
def test_blank_page_kept_as_original(xelatex_available, tmp_path):
    if not xelatex_available:
        pytest.skip("xelatex not installed")
    src = tmp_path / "b.pdf"
    _pdf_with_blank(src)
    workdir = tmp_path / "w"
    cfg = PipelineConfig(
        sources=[str(src)], workdir=str(workdir), pages="all", combine="interleave", font_main="Noto Serif"
    )
    # only page 2 has a translation (page 1 is blank -> no build)
    make_translated_page(paths.page_pdf(workdir, src.stem, 2))
    out = fitz.open(assemble.assemble(cfg)[src.stem])
    assert out.page_count == 3  # p1 original only, p2 original + translation
    out.close()


@pytest.mark.integration
def test_chapter_bookmarks(xelatex_available, tmp_path):
    if not xelatex_available:
        pytest.skip("xelatex not installed")
    src = tmp_path / "b.pdf"
    d = fitz.open()
    for _ in range(3):
        d.new_page(width=300, height=400).insert_text((20, 40), "x")
    d.save(src)
    d.close()
    workdir = tmp_path / "w"
    cfg = PipelineConfig(
        sources=[str(src)],
        workdir=str(workdir),
        pages="all",
        combine="interleave",
        font_main="Noto Serif",
        boundaries=[{"page": 2, "title": "Chapitre 2"}],
    )
    for p in (1, 2, 3):
        make_translated_page(paths.page_pdf(workdir, src.stem, p))
    out = fitz.open(assemble.assemble(cfg)[src.stem])
    assert out.get_toc() == [[1, "Chapitre 2", 3]]  # p1 (2 pp) then p2 starts at page 3
    out.close()
