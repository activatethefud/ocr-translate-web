"""Shared test fixtures. No network, no real API key."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

import fitz
import pytest

# Configure the app's storage/db for the whole test session BEFORE app import.
_TEST_STORE = tempfile.mkdtemp(prefix="ocrtran_test_")
os.environ["STORAGE_DIR"] = _TEST_STORE
os.environ["DATABASE_URL"] = f"sqlite:///{_TEST_STORE}/test.db"
os.environ["WORKER_CONCURRENCY"] = "2"
os.environ.setdefault("CORS_ORIGINS", "http://localhost:5173")


@pytest.fixture(autouse=True)
def _reset_throttle():
    """Keep the process-wide adaptive limiter from leaking between tests."""
    from ocrtran.throttle import THROTTLE

    THROTTLE.reset()
    yield
    THROTTLE.reset()


@pytest.fixture
def tiny_pdf2(tmp_path):
    """A second, *different* tiny PDF (so uploads aren't content-deduplicated)."""
    p = tmp_path / "tiny2.pdf"
    doc = fitz.open()
    for i in range(3):
        doc.new_page(width=200, height=200).insert_text((20, 40), f"other page {i + 1}")
    doc.save(p)
    doc.close()
    return p


@pytest.fixture
def tiny_pdf(tmp_path: Path) -> Path:
    """A small 2-page text PDF."""
    path = tmp_path / "tiny.pdf"
    doc = fitz.open()
    for i in range(2):
        page = doc.new_page(width=300, height=400)
        page.insert_text((30, 50), f"Page {i + 1}", fontsize=14)
        page.insert_text((30, 80), "Serbian text $x^2$ here.", fontsize=11)
    doc.save(path)
    doc.close()
    return path


@pytest.fixture
def xelatex_available() -> bool:
    return shutil.which("xelatex") is not None


def make_translated_page(path: Path, text: str = "Translated page") -> None:
    doc = fitz.open()
    page = doc.new_page(width=300, height=400)
    page.insert_text((30, 50), text, fontsize=12)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(path)
    doc.close()


class FakeProvider:
    """Deterministic provider for tests (implements the Provider protocol)."""

    def __init__(self, blocks=None, tight=None, translate=None):
        self.model = "fake"
        self._blocks = blocks or [
            {"type": "heading", "source": "Naslov", "target": "Title"},
            {"type": "prose", "source": "Tekst $x^2$.", "target": "Text $x^2$."},
            {"type": "math", "latex": "\\frac{a}{b} = c"},
        ]
        self._tight = tight or []
        self._translate = translate
        self.calls: list[str] = []

    def vision(self, image_path, prompt, max_tokens=None):
        self.calls.append("vision")
        if "FIGURE/GRAPH" in prompt or "identify each figure" in prompt.lower():
            return json.dumps({"figures": [{"index": 1, "bbox": b} for b in self._tight]})
        return json.dumps({"blocks": self._blocks})

    def text(self, prompt, max_tokens=None):
        self.calls.append("text")
        if self._translate:
            return self._translate(prompt)
        # identity translation for annotation batches
        start, end = prompt.find("["), prompt.rfind("]")
        if start >= 0 and end > start:
            return prompt[start : end + 1]
        return "translated"


@pytest.fixture
def fake_provider() -> FakeProvider:
    return FakeProvider()
