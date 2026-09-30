from __future__ import annotations

import pytest

from ocrtran import layout
from ocrtran.config import PipelineConfig


def _cfg(**kw) -> PipelineConfig:
    return PipelineConfig(sources=["a.pdf"], **kw)


def _fig(x0, y0, x1, y1, aspect=1.0, caption=""):
    return layout.Fig(
        block={"type": "figure"},
        bbox=(x0, y0, x1, y1),
        width=x1 - x0,
        aspect=aspect,
        caption=caption,
    )


def _item(h, keep=False, kind="block"):
    return layout.Item(kind=kind, block={"type": "prose"}, height=h, keep_with_next=keep)


# -- length parsing --------------------------------------------------------
def test_parse_length_pt():
    assert layout.parse_length_pt("16.5cm") == pytest.approx(469.5, abs=0.5)
    assert layout.parse_length_pt("1in") == pytest.approx(72.27, abs=0.01)
    assert layout.parse_length_pt("10pt") == 10.0
    assert layout.parse_length_pt("garbage", default=123) == 123


# -- figure clustering -----------------------------------------------------
def test_cluster_side_by_side_is_one_row():
    a, b = _fig(0.1, 0.1, 0.4, 0.4), _fig(0.5, 0.1, 0.9, 0.4)
    rows = layout.cluster_rows([a, b])
    assert len(rows) == 1
    assert rows[0] == [a, b]  # ordered left -> right


def test_cluster_stacked_is_two_rows():
    a, b = _fig(0.1, 0.1, 0.4, 0.25), _fig(0.1, 0.5, 0.4, 0.65)
    assert len(layout.cluster_rows([a, b])) == 2


def test_cluster_orders_rows_top_to_bottom():
    top = _fig(0.1, 0.05, 0.4, 0.2)
    bottom = _fig(0.1, 0.7, 0.4, 0.85)
    rows = layout.cluster_rows([bottom, top])
    assert rows[0] == [top] and rows[1] == [bottom]


def test_cluster_big_horizontal_gap_is_not_a_row():
    a, b = _fig(0.02, 0.1, 0.15, 0.4), _fig(0.85, 0.1, 0.98, 0.4)  # gap 0.70
    assert len(layout.cluster_rows([a, b])) == 2


# -- figure sizing ---------------------------------------------------------
def test_size_row_keeps_relative_widths():
    row = [_fig(0.1, 0.1, 0.4, 0.4), _fig(0.5, 0.1, 0.9, 0.4)]  # 0.3, 0.4
    w = layout.size_row(row, _cfg(), page_ar=1.5)
    assert w == pytest.approx([0.3, 0.4])


def test_size_row_scales_down_to_fit():
    row = [_fig(0.0, 0.0, 0.6, 0.4), _fig(0.6, 0.0, 1.0, 0.4)]  # 0.6 + 0.4
    w = layout.size_row(row, _cfg(), page_ar=1.5)
    assert sum(w) <= 0.95 + 1e-9
    assert w[0] > w[1]  # ratio preserved


def test_size_row_caps_single_figure_width():
    wide = _fig(0.0, 0.0, 1.0, 0.2, aspect=0.3)  # short & wide -> no height cap
    assert layout.size_row([wide], _cfg(), page_ar=1.5) == [0.85]


def test_size_row_applies_height_cap():
    w = layout.size_row([_fig(0.0, 0.0, 0.8, 0.1, aspect=5.0)], _cfg(), page_ar=1.5)
    assert w[0] < 0.2  # squashed to satisfy figure_max_height


def test_size_row_returns_none_when_too_narrow():
    row = [_fig(0.0, 0.0, 0.05, 0.1), _fig(0.06, 0.0, 0.11, 0.1)]
    assert layout.size_row(row, _cfg(), page_ar=1.5) is None


# -- item building ---------------------------------------------------------
def _fig_info(blocks):
    return {
        id(b): {"bbox": b["bbox"], "width": b["bbox"][2] - b["bbox"][0], "aspect": 1.0}
        for b in blocks
        if b.get("type") == "figure"
    }


def test_build_items_groups_consecutive_figures_into_a_row():
    blocks = [
        {"type": "figure", "bbox": [0.1, 0.1, 0.4, 0.3]},
        {"type": "figure", "bbox": [0.5, 0.1, 0.9, 0.3]},
        {"type": "prose", "target": "hi"},
    ]
    items = layout.build_items(blocks, _fig_info(blocks), _cfg(), 1.5)
    assert [i.kind for i in items] == ["figrow", "block"]
    assert len(items[0].row.figs) == 2


def test_build_items_stack_mode_splits_every_figure():
    blocks = [
        {"type": "figure", "bbox": [0.1, 0.1, 0.4, 0.3]},
        {"type": "figure", "bbox": [0.5, 0.1, 0.9, 0.3]},
    ]
    items = layout.build_items(blocks, _fig_info(blocks), _cfg(figure_layout="stack"), 1.5)
    assert [i.kind for i in items] == ["figrow", "figrow"]


def test_build_items_heading_keeps_with_next():
    blocks = [{"type": "heading", "target": "T"}, {"type": "prose", "target": "x"}]
    items = layout.build_items(blocks, {}, _cfg(), 1.5)
    assert items[0].keep_with_next is True


# -- estimation / packing --------------------------------------------------
def test_estimate_height_grows_with_text():
    short = layout.estimate_height(
        _item(0).__class__(kind="block", block={"type": "prose", "target": "a" * 20}), 400, 1.5, _cfg()
    )
    long = layout.estimate_height(
        layout.Item(kind="block", block={"type": "prose", "target": "a" * 2000}), 400, 1.5, _cfg()
    )
    assert long > short


def test_pack_respects_budget():
    pages = layout.pack([_item(60), _item(60), _item(60)], budget=100, cfg=_cfg())
    assert [len(p) for p in pages] == [1, 1, 1]


def test_pack_keeps_heading_with_next_block():
    pages = layout.pack([_item(90), _item(20, keep=True), _item(90)], budget=100, cfg=_cfg())
    assert [i.height for i in pages[0]] == [90]
    assert [i.height for i in pages[1]] == [20, 90]  # heading moved with its block


def test_pack_empty_items_yields_one_empty_page():
    assert layout.pack([], 100, _cfg()) == [[]]


def test_merge_short_last_page():
    cfg = _cfg()  # min_page_scale 0.9, page_fill_min 0.25
    assert len(layout.merge_short_last([[_item(90)], [_item(5)]], 100, cfg)) == 1
    assert len(layout.merge_short_last([[_item(90)], [_item(50)]], 100, cfg)) == 2
    assert len(layout.merge_short_last([[_item(99)], [_item(15)]], 100, cfg)) == 2


# -- plan_pages ------------------------------------------------------------
def _prose_blocks(n=4):
    return [{"type": "prose", "target": "a" * 200} for _ in range(n)]


def test_plan_single_mode_is_one_page():
    cfg = _cfg(layout_mode="single")
    plans = layout.plan_pages(cfg, _prose_blocks(6), {}, 595, 842, 468)
    assert len(plans) == 1
    assert len(plans[0]) == 6


def test_plan_auto_splits_using_measured_heights():
    cfg = _cfg(layout_mode="auto")
    measure = lambda items: {"heights": [80.0] * len(items), "textw": 100.0}  # noqa: E731
    plans = layout.plan_pages(cfg, _prose_blocks(4), {}, 200, 300, 100, measure)
    assert len(plans) == 2  # budget ~166pt, 2 x 80pt per page
    assert sum(len(p) for p in plans) == 4


def test_plan_auto_falls_back_to_estimation_without_measure():
    cfg = _cfg(layout_mode="auto")
    blocks = [{"type": "prose", "target": "a" * 4000} for _ in range(8)]  # ~1 page each
    plans = layout.plan_pages(cfg, blocks, {}, 595, 842, 468)
    assert len(plans) >= 2
