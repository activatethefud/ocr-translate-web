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


def test_annotate_translates_figure_caption():
    blocks = [{"type": "figure", "caption": "Slika 1.2: Ciklus izrade modela"}]
    n = annotate_blocks(_CountingProvider(), blocks, "Chinese")
    assert n == 1  # caption was translated (identity provider keeps the text)


def test_annotate_skips_empty_caption():
    blocks = [{"type": "figure", "caption": ""}]
    assert annotate_blocks(_CountingProvider(), blocks, "Chinese") == 0


def test_annotate_translates_textbf_and_bare_table_cells():
    blocks = [
        {
            "type": "table",
            "latex": (
                r"\begin{array}{|l|l|} \hline \textbf{Особине} & \textbf{Хлорела} \\ \hline"
                r" Облик тела & сталан \\ \hline \end{array}"
            ),
        }
    ]
    n = annotate_blocks(_CountingProvider(), blocks, "Chinese")
    out = blocks[0]["latex"]
    assert n == 1
    assert r"\textbf{Особине}" in out  # command kept (identity translation)
    assert r"\text{Облик тела}" in out  # bare cell wrapped + translated
    assert r"\text{сталан}" in out


def test_annotate_table_skips_numbers_and_dot_leaders():
    blocks = [{"type": "table", "latex": r"\begin{array}{c} 300--500 дана \\ 92 \\ \dots \\ \end{array}"}]
    annotate_blocks(_CountingProvider(), blocks, "Chinese")
    out = blocks[0]["latex"]
    assert "\\dots" in out and "92" in out
    assert r"\text{300--500 дана}" in out


def test_annotate_math_blocks_do_not_translate_bare_variables():
    blocks = [{"type": "math", "latex": r"x = alpha + beta"}]
    assert annotate_blocks(_CountingProvider(), blocks, "Chinese") == 0


def test_find_text_spans_bare_cells_are_whole_phrases():
    from ocrtran.annotate import find_text_spans

    spans = find_text_spans(r"a & Облик тела & b \\", include_bare=True)
    assert "Облик тела" in [t[2] for t in spans]


def test_annotate_translates_theorem_name():
    blocks = [{"type": "theorem", "kind": "Definition", "name": "Описна дефиниција", "target": "x"}]
    n = annotate_blocks(_CountingProvider(), blocks, "Chinese")
    assert n == 1  # name counted + translated (identity provider keeps it)
    assert blocks[0]["name"] == "Описна дефиниција"


def test_is_source_text_detects_short_words_and_abbreviations():
    assert is_source_text("Сл. 7")  # figure label
    assert is_source_text("и")
    assert is_source_text("за")
    assert not is_source_text("x")
    assert not is_source_text("frac")


def test_annotate_translates_a_short_figure_label():
    blocks = [{"type": "figure", "caption": "Сл. 7"}]
    assert annotate_blocks(_CountingProvider(), blocks, "Chinese") == 1
