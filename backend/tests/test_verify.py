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


def test_verify_ocr_flags_multiple_empties():
    issues = verify.verify_ocr({"d": [{"page": 1, "blocks": []}, {"page": 2, "blocks": []}]})
    assert [i["page"] for i in issues] == [1, 2]


def test_side_by_side_multiplier():
    assert verify.EXPECTED_PAGE_MULTIPLIER["side_by_side"] == 1
    assert verify.EXPECTED_PAGE_MULTIPLIER["grouped"] == 2


class _NoneProvider:
    def vision(self, image_path, prompt, max_tokens=None):
        return None

    def text(self, prompt, max_tokens=None):
        return ""


def test_verify_math_page_handles_no_json(tmp_path):
    img = tmp_path / "p.png"
    img.write_bytes(b"x")
    res = verify.verify_math_page(_NoneProvider(), img, [{"type": "math", "latex": "a=b"}])
    assert res["ok"] is True  # unknown is not a failure
    assert any("no JSON" in i["problem"] for i in res["issues"])


def test_leftover_words_allow_list():
    assert verify.leftover_words("Attention le chat", allow={"attention", "chat"}) == []


def test_expected_pages_accounts_for_selection():
    from ocrtran.verify import _expected_pages

    assert _expected_pages("interleave", 17, set(range(1, 18)), True) == 34
    assert _expected_pages("interleave", 17, set(range(1, 6)), True) == 22  # 5 tx + 12 orig
    assert _expected_pages("interleave", 17, set(range(1, 6)), False) == 10
    assert _expected_pages("grouped", 17, set(range(1, 6)), True) == 22
    assert _expected_pages("side_by_side", 17, set(range(1, 6)), True) == 17
    assert _expected_pages("translated_only", 17, set(range(1, 6)), True) == 5


def test_translated_positions_selection():
    from ocrtran.verify import _translated_positions

    # pages 1 and 3 selected, all kept, interleave -> positions 1 and 4 (0-based)
    assert _translated_positions("interleave", 4, {1, 3}, True) == [1, 4]
    assert _translated_positions("grouped", 4, {1, 3}, True) == [4, 5]
    assert _translated_positions("translated_only", 4, {1, 3}, True) == [0, 1]


@pytest.mark.integration
def test_verify_output_selection_aware(tiny_pdf, tmp_path):
    from ocrtran import assemble, paths

    workdir = tmp_path / "w"
    base = tiny_pdf.stem
    cfg = PipelineConfig(
        sources=[str(tiny_pdf)], workdir=str(workdir), combine="interleave", pages="1", unprocessed="original"
    )
    make_translated_page(paths.page_pdf(workdir, base, 1))
    out = assemble.assemble(cfg)[base]
    issues = verify.verify_output(cfg, str(tiny_pdf), out)
    assert issues == []  # 1 tx + 2 originals = 3 pages, no false alarm


@pytest.mark.integration
def test_verify_output_mixed_page_sizes(tmp_path):
    import fitz

    from ocrtran import assemble, paths

    src = tmp_path / "mixed.pdf"
    d = fitz.open()
    d.new_page(width=300, height=400).insert_text((20, 40), "a")
    d.new_page(width=400, height=300).insert_text((20, 40), "b")
    d.save(src)
    d.close()
    workdir = tmp_path / "w"
    base = src.stem
    cfg = PipelineConfig(sources=[str(src)], workdir=str(workdir), combine="interleave")
    for p in (1, 2):
        make_translated_page(paths.page_pdf(workdir, base, p))
    out = assemble.assemble(cfg)[base]
    assert verify.verify_output(cfg, str(src), out) == []


class _CountingProvider:
    def __init__(self):
        self.calls = 0

    def vision(self, *a, **k):
        self.calls += 1
        return '{"ok": true, "issues": []}'

    def text(self, *a, **k):
        return ""


def test_verify_math_skips_model_when_no_formulas(tmp_path):
    img = tmp_path / "p.png"
    img.write_bytes(b"x")
    prov = _CountingProvider()
    res = verify.verify_math_page(prov, img, [{"type": "prose", "target": "hi"}])
    assert res["checked"] == 0
    assert prov.calls == 0  # no model call for a page without formulas


def test_verify_math_calls_model_only_for_formulas(tmp_path):
    img = tmp_path / "p.png"
    img.write_bytes(b"x")
    prov = _CountingProvider()
    res = verify.verify_math_page(prov, img, [{"type": "math", "latex": "a=b"}])
    assert res["checked"] == 1
    assert prov.calls == 1


@pytest.mark.integration
def test_missing_pages_reports_unbuilt(tmp_path):
    import fitz

    from ocrtran import paths

    src = tmp_path / "s.pdf"
    d = fitz.open()
    d.new_page(width=200, height=200)
    d.new_page(width=200, height=200)
    d.save(src)
    d.close()
    workdir = tmp_path / "w"
    cfg = PipelineConfig(sources=[str(src)], workdir=str(workdir), pages="all", combine="interleave")
    make_translated_page(paths.page_pdf(workdir, src.stem, 1))  # page 2 not built
    miss = verify.missing_pages(cfg, {src.stem: [{"page": 1}, {"page": 2}]})
    assert miss == {src.stem: [2]}


@pytest.mark.integration
def test_missing_pages_empty_when_all_built(tmp_path):
    import fitz

    from ocrtran import paths

    src = tmp_path / "s.pdf"
    d = fitz.open()
    d.new_page(width=200, height=200)
    d.new_page(width=200, height=200)
    d.save(src)
    d.close()
    workdir = tmp_path / "w"
    cfg = PipelineConfig(sources=[str(src)], workdir=str(workdir), pages="all")
    for p in (1, 2):
        make_translated_page(paths.page_pdf(workdir, src.stem, p))
    assert verify.missing_pages(cfg, {src.stem: [{"page": 1}, {"page": 2}]}) == {}


def test_page_formulas_collects_display_and_inline():
    blocks = [
        {"type": "prose", "target": "so $x^2$ and $y$"},
        {"type": "math", "latex": "a=b"},
        {"type": "list", "items": [{"target": "$z$"}]},
        {"type": "figure", "caption": "no math here"},
    ]
    assert verify.page_formulas(blocks) == ["x^2", "y", "a=b", "z"]


class _CountingInline:
    def __init__(self):
        self.calls = 0

    def vision(self, *a, **k):
        self.calls += 1
        return '{"ok": true, "issues": []}'

    def text(self, *a, **k):
        return ""


def test_verify_math_page_checks_inline_formulas(tmp_path):
    img = tmp_path / "p.png"
    img.write_bytes(b"x")
    prov = _CountingInline()
    res = verify.verify_math_page(prov, img, [{"type": "prose", "target": "value $x^2$ here"}])
    assert res["checked"] == 1 and prov.calls == 1


def test_verify_math_page_skips_when_no_formulas(tmp_path):
    img = tmp_path / "p.png"
    img.write_bytes(b"x")
    prov = _CountingInline()
    res = verify.verify_math_page(prov, img, [{"type": "prose", "target": "plain text"}])
    assert res["checked"] == 0 and prov.calls == 0
