from __future__ import annotations

import pytest

from ocrtran.geometry import expand_bbox, union_bbox


def test_union_both():
    assert union_bbox([0.1, 0.1, 0.2, 0.2], [0.15, 0.05, 0.3, 0.4]) == [0.1, 0.05, 0.3, 0.4]


def test_union_none_left():
    assert union_bbox(None, [0.1, 0.2, 0.3, 0.4]) == [0.1, 0.2, 0.3, 0.4]


def test_union_none_right():
    assert union_bbox([0.1, 0.2, 0.3, 0.4], None) == [0.1, 0.2, 0.3, 0.4]


def test_union_both_none():
    assert union_bbox(None, None) is None


def test_union_disjoint():
    assert union_bbox([0.0, 0.0, 0.1, 0.1], [0.5, 0.5, 0.6, 0.6]) == [0.0, 0.0, 0.6, 0.6]


def test_expand_frac():
    box = expand_bbox([0.4, 0.4, 0.6, 0.6], pad_frac=0.1, min_pad_pt=0)
    assert box == [0.38, 0.38, 0.62, 0.62]


def test_expand_min_padding():
    # min_pad_pt / page dimension dominates when the box is tiny
    box = expand_bbox([0.4, 0.4, 0.6, 0.6], pad_frac=0.0, min_pad_pt=10, page_w=100, page_h=100)
    assert box == pytest.approx([0.3, 0.3, 0.7, 0.7])


def test_expand_clamps_to_bounds():
    box = expand_bbox([0.0, 0.0, 0.1, 0.1], pad_frac=1.0, min_pad_pt=0)
    assert box == [0.0, 0.0, 0.2, 0.2]


def test_expand_never_shrinks():
    box = expand_bbox([0.2, 0.2, 0.8, 0.8], pad_frac=0.0, min_pad_pt=0)
    assert box == [0.2, 0.2, 0.8, 0.8]


def test_expand_normalises_inverted_box():
    box = expand_bbox([0.8, 0.8, 0.2, 0.2], pad_frac=0.0, min_pad_pt=0)
    assert box == [0.2, 0.2, 0.8, 0.8]


def test_choose_box_prefers_tight():
    from ocrtran.geometry import choose_box

    assert choose_box([0.2, 0.2, 0.5, 0.5], [0.0, 0.0, 1.0, 1.0]) == [0.2, 0.2, 0.5, 0.5]


def test_choose_box_uses_main_when_no_tight():
    from ocrtran.geometry import choose_box

    assert choose_box(None, [0.1, 0.1, 0.5, 0.5]) == [0.1, 0.1, 0.5, 0.5]


def test_choose_box_falls_back_to_tight_when_all_suspicious():
    from ocrtran.geometry import choose_box

    # both boxes are near-full; prefer the tighter one rather than dropping it
    assert choose_box([0.0, 0.0, 0.95, 0.95], [0.0, 0.0, 1.0, 1.0]) == [0.0, 0.0, 0.95, 0.95]


def test_choose_box_none_when_nothing():
    from ocrtran.geometry import choose_box

    assert choose_box(None, None) is None
