from __future__ import annotations

from ocrtran.annotate import annotate_blocks, find_text_groups, is_source_text
from tests.conftest import FakeProvider


def test_find_text_groups():
    groups = find_text_groups(r"a \text{hello} b \textit{world}")
    assert [g[2] for g in groups] == ["hello", "world"]


def test_find_text_groups_nested_braces():
    groups = find_text_groups(r"\text{a {b} c}")
    assert groups[0][2] == "a {b} c"


def test_is_source_text():
    assert is_source_text("kvadriramo")
    assert not is_source_text("frac")
    assert not is_source_text("x")


def test_annotate_identity_translation():
    blocks = [{"type": "math", "latex": r"x = y \quad \text{kvadriramo}"}]
    n = annotate_blocks(FakeProvider(), blocks, "French")
    assert n == 1
    assert r"\text{kvadriramo}" in blocks[0]["latex"]  # identity fake keeps it


def test_annotate_skips_pure_math():
    blocks = [{"type": "math", "latex": r"x = \frac{a}{b}"}]
    assert annotate_blocks(FakeProvider(), blocks, "French") == 0


class _MismatchProvider:
    """Batch returns the wrong length, forcing the per-item fallback."""

    def text(self, prompt, max_tokens=None):
        if prompt.startswith("Translate each string"):
            return '["only-one"]'
        return "ZH"


def test_annotate_batch_mismatch_falls_back_per_item():
    blocks = [{"type": "math", "latex": r"\text{aaa} + \text{bbb}"}]
    n = annotate_blocks(_MismatchProvider(), blocks, "Chinese")
    assert n == 1
    assert blocks[0]["latex"].count("ZH") == 2


class _CountingProvider:
    def __init__(self):
        self.text_calls = 0

    def text(self, prompt, max_tokens=None):
        self.text_calls += 1
        start, end = prompt.find("["), prompt.rfind("]")
        return prompt[start : end + 1] if 0 <= start < end else "[]"


def test_annotate_batches_all_blocks_into_one_call():
    blocks = [
        {"type": "math", "latex": r"x + \text{aaa}"},
        {"type": "math", "latex": r"y + \text{bbb}"},
    ]
    prov = _CountingProvider()
    n = annotate_blocks(prov, blocks, "French")
    assert n == 2
    assert prov.text_calls == 1  # one batch for the whole page
