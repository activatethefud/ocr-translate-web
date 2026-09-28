from __future__ import annotations

import fitz
import pytest

from ocrtran import paths, verify
from ocrtran.config import PipelineConfig
from tests.conftest import make_translated_page


def test_leftover_words():
    words = verify.leftover_words("Attention le chat kvadriramo", allow={"attention"})
    assert "chat" in words and "attention" not in words


def test_verify_ocr_flags_empty():
    issues = verify.verify_ocr(
        {"doc": [{"page": 1, "blocks": []}, {"page": 2, "blocks": [{"type": "prose"}]}]}
    )
    assert len(issues) == 1 and issues[0]["kind"] == "empty_ocr"


def test_verify_math_page_no_math(tmp_path):
    from tests.conftest import FakeProvider

    res = verify.verify_math_page(FakeProvider(), tmp_path / "x.png", [{"type": "prose"}])
    assert res["ok"] is True and res["checked"] == 0


def test_verify_math_page_checks_formulas(tmp_path):
    from tests.conftest import FakeProvider

    img = tmp_path / "p.png"
    img.write_bytes(b"x")
    blocks = [{"type": "math", "latex": "a=b"}, {"type": "prose", "target": "hi"}]
    res = verify.verify_math_page(FakeProvider(), img, blocks)
    assert res["checked"] == 1


def test_run_math_check_stores_result(tmp_path):
    from tests.conftest import FakeProvider

    img = tmp_path / "p.png"
    img.write_bytes(b"x")
    cfg = PipelineConfig(sources=[str(img)])
    results = {"d": [{"page": 1, "img": str(img), "blocks": [{"type": "math", "latex": "a=b"}]}]}
    verify.run_math_check(cfg, FakeProvider(), results)
    assert results["d"][0]["math_check"]["checked"] == 1


@pytest.mark.integration
def test_verify_output_page_count(tiny_pdf, tmp_path):
    workdir = tmp_path / "work"
    base = tiny_pdf.stem
    cfg = PipelineConfig(
        sources=[str(tiny_pdf)], workdir=str(workdir), combine="interleave", target_lang="French"
    )
    for p in (1, 2):
        make_translated_page(paths.page_pdf(workdir, base, p))
    from ocrtran import assemble

    outputs = assemble.assemble(cfg)
    issues = verify.verify_output(cfg, str(tiny_pdf), outputs[base])
    assert issues == []  # 4 pages, no empty pages, right size


@pytest.mark.integration
def test_verify_detects_wrong_count(tiny_pdf, tmp_path):
    out = tmp_path / "wrong.pdf"
    doc = fitz.open()
    doc.new_page(width=300, height=400)
    doc.save(out)
    doc.close()
    cfg = PipelineConfig(sources=[str(tiny_pdf)], workdir=str(tmp_path), combine="interleave")
    issues = verify.verify_output(cfg, str(tiny_pdf), out)
    assert any(i["kind"] == "page_count" for i in issues)
