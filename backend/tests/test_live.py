"""Live API tests — opt-in, they call a real model and cost money.

Run with:
    OCRtran_LIVE=1 DS_KEY=... PYTHONPATH=.deps:. pytest -m live -v
"""

from __future__ import annotations

import os

import fitz
import pytest

from ocrtran.config import PipelineConfig
from ocrtran.pipeline import Pipeline

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not os.environ.get("OCRtran_LIVE"), reason="set OCRtran_LIVE=1 to run"),
    pytest.mark.skipif(not os.environ.get("DS_KEY"), reason="set DS_KEY"),
]


def test_live_one_page(tmp_path, xelatex_available):
    if not xelatex_available:
        pytest.skip("xelatex not installed")
    src = tmp_path / "page.pdf"
    doc = fitz.open()
    page = doc.new_page(width=400, height=300)
    page.insert_text((30, 60), "Povrsina kruga je pi*r^2.", fontsize=14)
    page.insert_text((30, 90), "Talesova teorema: AB/AB1 = AC/AC1.", fontsize=12)
    doc.save(src)
    doc.close()

    cfg = PipelineConfig(
        sources=[str(src)],
        workdir=str(tmp_path / "work"),
        source_lang="Serbian",
        target_lang="French",
        font_main="Noto Serif",
        bilingual=False,
        combine="interleave",
    )
    result = Pipeline(cfg).run()
    assert result.outputs, "no output produced"
    assert result.usage.get("calls", 0) >= 1
