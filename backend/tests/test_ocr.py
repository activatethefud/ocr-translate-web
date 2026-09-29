from __future__ import annotations

from ocrtran.ocr import build_ocr_prompt, ocr_image, parse_json
from tests.conftest import FakeProvider


def test_parse_json_plain():
    assert parse_json('{"a": 1}') == {"a": 1}


def test_parse_json_fenced():
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_parse_json_with_prose():
    assert parse_json('Here you go: {"a": 1} done') == {"a": 1}


def test_parse_json_bad():
    assert parse_json("not json") is None
    assert parse_json(None) is None


def test_prompt_placeholders():
    p = build_ocr_prompt("Serbian", "French")
    assert "Serbian" in p and "French" in p and "__SRC__" not in p and "__TGT__" not in p


def test_prompt_glossary_and_do_not_translate():
    p = build_ocr_prompt(
        "Serbian",
        "French",
        glossary=[{"source": "Prava", "target": "droite"}],
        do_not_translate=["Pythagore"],
    )
    assert "Prava => droite" in p
    assert "Pythagore" in p


def test_prompt_additional_instructions():
    p = build_ocr_prompt("Serbian", "French", instructions="Use a formal tone.")
    assert "Use a formal tone." in p
    assert "Additional" not in build_ocr_prompt("Serbian", "French", instructions="   ")


def test_ocr_image_blocks(tmp_path):
    img = tmp_path / "p.png"
    img.write_bytes(b"not a real image")  # never decoded by the fake provider
    result = ocr_image(FakeProvider(), img, "Serbian", "French")
    assert [b["type"] for b in result["blocks"]] == ["heading", "prose", "math"]
    assert result["tight"] == []


def test_ocr_image_with_figures(tmp_path):
    img = tmp_path / "p.png"
    img.write_bytes(b"x")
    blocks = [{"type": "figure", "description": "graph", "bbox": [0, 0, 1, 1]}]
    prov = FakeProvider(blocks=blocks, tight=[[0.1, 0.1, 0.5, 0.5]])
    result = ocr_image(prov, img, "Serbian", "French")
    assert result["tight"] == [[0.1, 0.1, 0.5, 0.5]]


class _FlakyProvider:
    """Returns invalid JSON once, then a valid page."""

    def __init__(self):
        self.n = 0

    def vision(self, image_path, prompt, max_tokens=None):
        self.n += 1
        if self.n == 1:
            return "sorry, no json here"
        return '{"blocks": [{"type": "prose", "target": "ok"}]}'

    def text(self, prompt, max_tokens=None):
        return "[]"


def test_ocr_image_retries_on_bad_json(tmp_path):
    img = tmp_path / "p.png"
    img.write_bytes(b"x")
    prov = _FlakyProvider()
    result = ocr_image(prov, img, "Serbian", "French")
    assert result["blocks"] and prov.n == 2


def _fig_blocks():
    return [{"type": "figure", "bbox": [0.1, 0.1, 0.2, 0.2], "description": "d"}]


def test_figure_mode_off_makes_one_call(tmp_path):
    img = tmp_path / "p.png"
    img.write_bytes(b"x")
    prov = FakeProvider(blocks=_fig_blocks(), tight=[[0.0, 0.0, 0.3, 0.3]])
    res = ocr_image(prov, img, "S", "F", figure_mode="off")
    assert res["tight"] == []
    assert prov.calls.count("vision") == 1


def test_figure_mode_tight_uses_bbox_pass(tmp_path):
    img = tmp_path / "p.png"
    img.write_bytes(b"x")
    prov = FakeProvider(blocks=_fig_blocks(), tight=[[0.0, 0.0, 0.3, 0.3]])
    res = ocr_image(prov, img, "S", "F", figure_mode="tight")
    assert res["tight"] == [[0.0, 0.0, 0.3, 0.3]]
    assert prov.calls.count("vision") == 2


def test_figure_mode_judge_adds_a_call(tmp_path):
    img = tmp_path / "p.png"
    img.write_bytes(b"x")
    prov = FakeProvider(blocks=_fig_blocks(), tight=[[0.0, 0.0, 0.3, 0.3]])
    res = ocr_image(prov, img, "S", "F", figure_mode="judge")
    assert res["tight"] == [[0.0, 0.0, 0.3, 0.3]]  # judge failed -> candidate kept
    assert prov.calls.count("vision") == 3
