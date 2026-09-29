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


def _prep(tiny_pdf, tmp_path, **kwargs):
    workdir = tmp_path / "work"
    base = tiny_pdf.stem
    cfg = PipelineConfig(sources=[str(tiny_pdf)], workdir=str(workdir), **kwargs)
    for p in (1, 2):
        make_translated_page(paths.page_pdf(workdir, base, p), f"FR {p}")
    return cfg, base


@pytest.mark.integration
def test_assemble_page_selection_skip(tiny_pdf, tmp_path):
    cfg, base = _prep(tiny_pdf, tmp_path, combine="interleave", pages="1", unprocessed="skip")
    out = fitz.open(assemble.assemble(cfg)[base])
    assert out.page_count == 2  # orig 1 + translation 1
    out.close()


@pytest.mark.integration
def test_assemble_page_selection_keep_original(tiny_pdf, tmp_path):
    cfg, base = _prep(tiny_pdf, tmp_path, combine="interleave", pages="1", unprocessed="original")
    out = fitz.open(assemble.assemble(cfg)[base])
    assert out.page_count == 3  # orig 1 + translation 1 + unselected orig 2
    out.close()


@pytest.mark.integration
def test_assemble_translated_only_selected(tiny_pdf, tmp_path):
    cfg, base = _prep(tiny_pdf, tmp_path, combine="translated_only", bilingual=False, pages="2")
    out = fitz.open(assemble.assemble(cfg)[base])
    assert out.page_count == 1
    out.close()


@pytest.mark.integration
def test_assemble_output_page_size(tiny_pdf, tmp_path):
    cfg, base = _prep(tiny_pdf, tmp_path, combine="interleave", output_page_size="a4")
    out = fitz.open(assemble.assemble(cfg)[base])
    assert (out[0].rect.width, out[0].rect.height) == (300.0, 400.0)  # original
    assert round(out[1].rect.width) == 595 and round(out[1].rect.height) == 842  # A4
    out.close()


def test_eff_max_scale():
    from ocrtran.assemble import _eff_max_scale

    assert _eff_max_scale(PipelineConfig(sources=["a.pdf"], scale_mode="fit")) == 1.0
    assert _eff_max_scale(PipelineConfig(sources=["a.pdf"], scale_mode="fill", max_scale=0)) == 0


@pytest.mark.integration
def test_assemble_grouped_skip(tiny_pdf, tmp_path):
    cfg, base = _prep(tiny_pdf, tmp_path, combine="grouped", pages="1", unprocessed="skip")
    out = fitz.open(assemble.assemble(cfg)[base])
    assert out.page_count == 2  # orig 1 + translation 1
    out.close()


@pytest.mark.integration
def test_assemble_side_by_side_keeps_unselected(tiny_pdf, tmp_path):
    cfg, base = _prep(tiny_pdf, tmp_path, combine="side_by_side", pages="1", unprocessed="original")
    out = fitz.open(assemble.assemble(cfg)[base])
    assert out.page_count == 2  # page 1 side-by-side + page 2 original
    out.close()


@pytest.mark.integration
def test_assemble_output_size_letter(tiny_pdf, tmp_path):
    cfg, base = _prep(
        tiny_pdf, tmp_path, combine="translated_only", bilingual=False, output_page_size="letter"
    )
    out = fitz.open(assemble.assemble(cfg)[base])
    assert (round(out[0].rect.width), round(out[0].rect.height)) == (612, 792)
    out.close()


@pytest.mark.integration
def test_assemble_default_omits_unselected(tiny_pdf, tmp_path):
    # with the new default (skip) a page selection yields ONLY those pages
    cfg, base = _prep(tiny_pdf, tmp_path, combine="interleave", pages="1")
    out = fitz.open(assemble.assemble(cfg)[base])
    assert out.page_count == 2  # orig 1 + translation 1, page 2 omitted
    out.close()
