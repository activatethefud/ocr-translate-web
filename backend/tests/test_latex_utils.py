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
