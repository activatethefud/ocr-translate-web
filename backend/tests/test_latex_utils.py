from __future__ import annotations

from ocrtran.latex import esc_text, wrap_math


def test_esc_outside_math_only():
    assert esc_text("100% & $a_1$ #x") == "100\\% \\& $a_1$ \\#x"


def test_esc_keeps_math_untouched():
    assert esc_text("$a \\cdot b$") == "$a \\cdot b$"


def test_esc_multiple_math_segments():
    assert esc_text("a_b $x_i$ c^d") == "a\\_b $x_i$ c\\textasciicircum{}d"


def test_wrap_math_plain():
    assert wrap_math("a = b").strip() == "\\[\na = b\n\\]"


def test_wrap_math_already_dollar():
    assert wrap_math("$a=b$").strip() == "$a=b$"


def test_wrap_math_display_env_not_nested():
    src = "\\begin{align}\na &= b\\\\\nc &= d\n\\end{align}"
    assert wrap_math(src) == src


def test_wrap_math_brackets_not_doubled():
    src = "\\[ a = b \\]"
    assert wrap_math(src) == src


def test_esc_unescapes_set_braces():
    # models emit set differences as \{..\}; render literal braces, not a backslash
    out = esc_text("(R\\{−1\\}, ·)")
    assert "\\textbackslash" not in out
    assert "\\{" in out and "\\}" in out  # \{ .. \} which TeX draws as { .. }
    # the symbols are now typeset in math mode instead of being dropped
    assert out == "(R\\{$-$1\\}, $\\cdot$)"


def test_esc_strips_control_chars():
    assert esc_text("va\x19i") == "vai"


def test_esc_textifies_cyrillic_in_math():
    out = esc_text("vrednost $и + ш$ ovde")
    assert "\\text{и}" in out and "\\text{ш}" in out
    assert "$и" not in out  # not bare Cyrillic in math mode


def test_block_math_textifies_nonlatin():
    from ocrtran.latex import _block_tex

    tex = _block_tex({"type": "math", "latex": "и = ш"}, {})
    assert "\\text{и}" in tex and "\\text{ш}" in tex


def test_fix_table_spec_pads_columns():
    from ocrtran.latex import fix_table_spec, wrap_math

    tex = r"\begin{array}{|c|c|} \hline a & b & c \\ \hline \end{array}"
    fixed = fix_table_spec(tex)
    assert fixed.startswith(r"\begin{array}{|c|c|c|}")
    # and it compiles (no "Extra alignment tab")
    assert wrap_math(fixed).count("&") == 2


def test_fix_table_spec_leaves_correct_tables():
    from ocrtran.latex import fix_table_spec

    tex = r"\begin{array}{cc} a & b \\ c & d \end{array}"
    assert fix_table_spec(tex) == tex


def test_strip_tags_invalid_in_display_math():
    from ocrtran.latex import strip_tags

    assert strip_tags(r"x = 1 \tag{2}") == r"x = 1 \qquad (2)"
    assert strip_tags(r"\tag*{A} y") == r"\qquad (A) y"


def test_preamble_defines_european_trig_shorthands():
    from ocrtran.config import PipelineConfig
    from ocrtran.latex import preamble

    tex = preamble(PipelineConfig(sources=["a.pdf"]))
    for cmd in ("\\tg", "\\ctg", "\\arctg", "\\tgh", "\\sh", "\\ch"):
        assert f"\\providecommand{{{cmd}}}" in tex


def test_fix_text_ellipsis_inside_text_groups():
    from ocrtran.latex import fix_text_ellipsis

    assert fix_text_ellipsis(r"\text{中国\ldots\ldots}") == r"\text{中国……}"
    assert fix_text_ellipsis(r"x \dots y") == r"x \dots y"  # math context untouched


def test_fix_table_spec_keeps_p_column_specs():
    from ocrtran.latex import fix_table_spec

    tex = r"\begin{array}{|p{0.45\textwidth}|p{0.45\textwidth}|} a & b \\ c & d \\ \end{array}"
    assert fix_table_spec(tex) == tex  # 2 columns declared, 2 used -> untouched


def test_fix_table_spec_pads_p_column_specs():
    from ocrtran.latex import fix_table_spec

    tex = r"\begin{array}{|p{0.4\textwidth}|p{0.4\textwidth}|} a & b & c \\ \end{array}"
    out = fix_table_spec(tex)
    assert out.startswith(r"\begin{array}{|p{0.4\textwidth}|p{0.4\textwidth}|c|}")
    assert "textwidthc}" not in out  # never mangled


def test_table_block_uses_adjustbox():
    from ocrtran.latex import _block_tex

    tex = _block_tex({"type": "table", "latex": r"\begin{array}{cc} a & b \\ \end{array}"}, {})
    assert tex.startswith(r"\adjustbox{max width=\textwidth}")


def test_wrap_table_cells_converts_columns_to_wrapping_p():
    from ocrtran.latex import wrap_table_cells

    tex = r"\begin{array}{|l|c|} a & b \\ \end{array}"
    out = wrap_table_cells(tex)
    assert r">{\raggedright\arraybackslash}p{0.450\textwidth}" in out
    assert r">{\centering\arraybackslash}p{0.450\textwidth}" in out


def test_wrap_table_cells_multicolumn_gets_full_width():
    from ocrtran.latex import wrap_table_cells

    tex = r"\begin{array}{|l|l|} \multicolumn{2}{|c|}{Total} \\ a & b \\ \end{array}"
    out = wrap_table_cells(tex)
    assert r"\multicolumn{2}{|>{\centering\arraybackslash}p{0.900\textwidth}|}" in out
