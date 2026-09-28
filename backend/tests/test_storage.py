from __future__ import annotations

import shutil

import fitz
import pikepdf
import pytest

from app import storage
from app.settings import Settings


def _make_pdf(path, pages=1):
    doc = fitz.open()
    for _ in range(pages):
        p = doc.new_page(width=200, height=300)
        p.insert_text((20, 40), "Hello world, this is a real text page.", fontsize=12)
    doc.save(path)
    doc.close()


def test_sanitize_strips_active_content(tmp_path):
    src = tmp_path / "a.pdf"
    _make_pdf(src)
    with pikepdf.open(src, allow_overwriting_input=True) as pdf:
        pdf.Root.OpenAction = pikepdf.Dictionary(
            S=pikepdf.String("JavaScript"), JS=pikepdf.String("app.alert('x')")
        )
        pdf.Root.Names = pikepdf.Dictionary(
            JavaScript=pikepdf.Dictionary(Names=[pikepdf.String("x"), pikepdf.Dictionary()]),
            EmbeddedFiles=pikepdf.Dictionary(Names=[]),
        )
        pdf.save(src)
    dst = tmp_path / "b.pdf"
    storage.sanitize_pdf(src, dst)
    with pikepdf.open(dst) as pdf:
        assert "/OpenAction" not in pdf.Root
        names = pdf.Root.get("/Names")
        assert names is None or "/JavaScript" not in names


def test_inspect_pdf(tmp_path):
    pdf = tmp_path / "x.pdf"
    _make_pdf(pdf, pages=2)
    info = storage.inspect_pdf(pdf)
    assert info["n_pages"] == 2
    assert info["page_w"] == 200 and info["page_h"] == 300
    assert info["kind"] == "text"


def test_finalize_upload(tmp_path):
    settings = Settings(storage_dir=tmp_path / "store")
    settings.ensure_dirs()
    doc_id = "abc123"
    src = tmp_path / "u.pdf"
    _make_pdf(src)
    ddir = storage.doc_dir(settings, doc_id)
    ddir.mkdir(parents=True)
    tmp = ddir / "upload.tmp"
    shutil.copy(src, tmp)
    info = storage.finalize_upload(settings, doc_id, tmp, "My File.pdf")
    assert info["n_pages"] == 1
    assert info["filename"] == "My File.pdf"
    assert storage.source_path(settings, doc_id).exists()
    assert not tmp.exists()


def test_inspect_rejects_empty(tmp_path):
    pdf = tmp_path / "empty.pdf"
    _make_pdf(pdf)
    with pikepdf.open(pdf, allow_overwriting_input=True) as p:
        del p.pages[:]
        p.save(pdf)
    with pytest.raises(ValueError):
        storage.inspect_pdf(pdf)
