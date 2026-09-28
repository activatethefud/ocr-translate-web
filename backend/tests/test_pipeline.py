from __future__ import annotations

import fitz
import pytest

from ocrtran.config import PipelineConfig
from ocrtran.pipeline import Pipeline
from tests.conftest import FakeProvider


@pytest.mark.integration
def test_full_pipeline_fake_provider(tiny_pdf, tmp_path, xelatex_available):
    if not xelatex_available:
        pytest.skip("xelatex not installed")
    cfg = PipelineConfig(
        sources=[str(tiny_pdf)],
        workdir=str(tmp_path / "work"),
        source_lang="Serbian",
        target_lang="French",
        font_main="Noto Serif",
        combine="interleave",
    )
    result = Pipeline(cfg, provider=FakeProvider()).run()
    base = tiny_pdf.stem
    assert base in result.outputs
    out = fitz.open(result.outputs[base])
    assert out.page_count == 4  # 2 originals + 2 translations
    out.close()
    # no empty pages, no empty-OCR flags
    issues = result.report[base]["issues"]
    assert not [i for i in issues if i["kind"] in ("empty_page", "empty_ocr", "page_count")]


@pytest.mark.integration
def test_pipeline_caching_avoids_second_ocr(tmp_path, xelatex_available):
    if not xelatex_available:
        pytest.skip("xelatex not installed")
    # make a 1-page source
    src = tmp_path / "one.pdf"
    doc = fitz.open()
    doc.new_page(width=200, height=200).insert_text((20, 40), "x")
    doc.save(src)
    doc.close()

    cfg = PipelineConfig(
        sources=[str(src)], workdir=str(tmp_path / "work"), target_lang="French", font_main="Noto Serif"
    )
    prov = FakeProvider()
    Pipeline(cfg, provider=prov).run()
    first_calls = prov.calls.count("vision")
    Pipeline(cfg, provider=prov).run()
    assert prov.calls.count("vision") == first_calls  # served from cache
