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
    assert out == "(R\\{−1\\}, ·)"  # \{ .. \} which TeX draws as { .. }


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
