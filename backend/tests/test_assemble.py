from __future__ import annotations

import fitz
import pytest

from ocrtran import assemble, paths
from ocrtran.config import PipelineConfig
from tests.conftest import make_translated_page

MODES = [
    ("interleave", 4),
    ("grouped", 4),
    ("side_by_side", 2),
    ("translated_only", 2),
]


@pytest.mark.integration
@pytest.mark.parametrize("mode,expected", MODES)
def test_assemble_modes(tiny_pdf, tmp_path, mode, expected):
    workdir = tmp_path / "work"
    base = tiny_pdf.stem
    cfg = PipelineConfig(
        sources=[str(tiny_pdf)],
        workdir=str(workdir),
        combine=mode if mode != "translated_only" else "interleave",
        bilingual=(mode != "translated_only"),
        target_lang="French",
    )
    for p in (1, 2):
        make_translated_page(paths.page_pdf(workdir, base, p), f"FR {p}")

    outputs = assemble.assemble(cfg)
    assert base in outputs
    out = fitz.open(outputs[base])
    assert out.page_count == expected
    # same page size as source
    assert (out[0].rect.width, out[0].rect.height) == (300.0, 400.0)
    out.close()


@pytest.mark.integration
def test_assemble_falls_back_when_translation_missing(tiny_pdf, tmp_path):
    cfg = PipelineConfig(
        sources=[str(tiny_pdf)], workdir=str(tmp_path / "w"), combine="translated_only", bilingual=False
    )
    outputs = assemble.assemble(cfg)  # no translated PDFs on disk
    out = fitz.open(outputs[tiny_pdf.stem])
    assert out.page_count == 2
    out.close()
