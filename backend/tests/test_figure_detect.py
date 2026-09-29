from __future__ import annotations

import json

import pytest

from ocrtran.figures import (
    boxes_variant,
    detect_figures,
    grid_variant,
    iou,
    refine_figures,
    refine_page,
)


class _FigProvider:
    """Returns detection / judge / main JSON depending on the prompt."""

    def __init__(self, detections=None, judge=None):
        self.detections = detections or []
        self.judge = judge or []
        self.calls: list[str] = []

    def vision(self, path, prompt, max_tokens=None):
        self.calls.append(prompt[:40])
        if "precise document-figure detector" in prompt:
            return json.dumps({"figures": self.detections})
        if "boxes drawn" in prompt:
            return json.dumps({"figures": self.judge})
        return json.dumps({"blocks": []})

    def text(self, *a, **k):
        return ""


def _img(tmp_path):
    p = tmp_path / "page.png"
    p.write_bytes(b"not a real png")  # helpers fall back gracefully
    return p


def test_iou():
    assert iou([0.1, 0.1, 0.5, 0.5], [0.1, 0.1, 0.5, 0.5]) == pytest.approx(1.0)
    assert iou([0.0, 0.0, 0.1, 0.1], [0.5, 0.5, 0.6, 0.6]) == 0.0
    assert iou(None, [0.1, 0.1, 0.2, 0.2]) == 0.0


def test_grid_and_boxes_variant_fall_back(tmp_path):
    img = _img(tmp_path)
    assert grid_variant(img) == str(img)  # undecodable -> passthrough
    assert boxes_variant(img, [[0.1, 0.1, 0.5, 0.5]]) == str(img)


def test_grid_variant_real_image(tmp_path):
    from PIL import Image

    img = tmp_path / "real.png"
    Image.new("RGB", (300, 400), "white").save(img)
    out = grid_variant(img)
    assert out.endswith(".grid.png")
    assert (tmp_path / "real.png.grid.png").exists()


def test_detect_figures_parses(tmp_path):
    prov = _FigProvider(
        detections=[{"bbox": [0.2, 0.2, 0.7, 0.6], "description": "diagram", "caption": "Fig 1"}]
    )
    out = detect_figures(prov, _img(tmp_path))
    assert out == [{"bbox": [0.2, 0.2, 0.7, 0.6], "description": "diagram", "caption": "Fig 1"}]


def test_detect_figures_ignores_invalid(tmp_path):
    prov = _FigProvider(detections=[{"bbox": [1, 2, 3]}, {"bbox": "x"}])
    assert detect_figures(prov, _img(tmp_path)) == []


def test_refine_figures_parses(tmp_path):
    prov = _FigProvider(judge=[{"index": 0, "keep": True, "bbox": [0.05, 0.05, 0.6, 0.6], "caption": "Cap"}])
    out = refine_figures(prov, _img(tmp_path), [{"bbox": [0.1, 0.1, 0.5, 0.5], "description": "d"}])
    assert out[0]["keep"] is True and out[0]["bbox"] == [0.05, 0.05, 0.6, 0.6]


def test_refine_page_adds_missed_figure(tmp_path):
    prov = _FigProvider(
        detections=[{"bbox": [0.2, 0.2, 0.8, 0.8], "description": "missed", "caption": ""}],
        judge=[{"index": 0, "keep": True, "bbox": [0.2, 0.2, 0.8, 0.8]}],
    )
    blocks, tight = refine_page(prov, _img(tmp_path), [], [])
    assert [b["type"] for b in blocks] == ["figure"]
    assert tight == [[0.2, 0.2, 0.8, 0.8]]


def test_refine_page_judge_drops_false_positive(tmp_path):
    blocks = [{"type": "figure", "description": "not really", "bbox": [0.2, 0.2, 0.4, 0.4]}]
    prov = _FigProvider(detections=[], judge=[{"index": 0, "keep": False}])
    out_blocks, tight = refine_page(prov, _img(tmp_path), blocks, [[0.2, 0.2, 0.4, 0.4]])
    assert out_blocks == [] and tight == []


def test_refine_page_union_never_shrinks(tmp_path):
    blocks = [{"type": "figure", "description": "graph", "bbox": [0.2, 0.2, 0.5, 0.5]}]
    prov = _FigProvider(
        detections=[{"bbox": [0.25, 0.25, 0.55, 0.55], "description": "graph", "caption": ""}],
        judge=[{"index": 0, "keep": True, "bbox": [0.15, 0.15, 0.6, 0.6]}],
    )
    _blocks, tight = refine_page(prov, _img(tmp_path), blocks, [[0.2, 0.2, 0.5, 0.5]])
    assert tight[0] == [0.15, 0.15, 0.6, 0.6]  # union of candidate + detection + judge


def test_refine_page_empty(tmp_path):
    blocks = [{"type": "prose", "target": "hi"}]
    out, tight = refine_page(_FigProvider(), _img(tmp_path), blocks, [])
    assert out == blocks and tight == []


def test_sanitize_box_fractions_passthrough():
    from ocrtran.figures import sanitize_box

    assert sanitize_box([0.1, 0.2, 0.5, 0.6]) == [0.1, 0.2, 0.5, 0.6]


def test_sanitize_box_percentages():
    from ocrtran.figures import sanitize_box

    assert sanitize_box([10, 20, 50, 60]) == [0.1, 0.2, 0.5, 0.6]


def test_sanitize_box_pixels_need_image_size():
    from ocrtran.figures import sanitize_box

    box = sanitize_box([50, 180, 950, 720], 1272, 1800)
    assert box == pytest.approx([50 / 1272, 0.1, 950 / 1272, 0.4])
    # pixels without a reference size -> reject rather than build a broken crop
    assert sanitize_box([50, 180, 950, 720]) is None


def test_sanitize_box_rejects_degenerate():
    from ocrtran.figures import sanitize_box

    assert sanitize_box([0.5, 0.5, 0.5, 0.5]) is None
    assert sanitize_box(None) is None
    assert sanitize_box([0.1, 0.1, 0.1, 0.5]) is None
