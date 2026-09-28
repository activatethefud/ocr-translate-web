"""Where artifacts live on disk.

Layout (per source document, under ``<workdir>/<base>/``)::

    pages/p-NN.png        rendered source pages
    ocr.json              vision output (blocks + tight figure boxes)
    tex/pNN.tex|pdf       typeset translated pages
    tex/fig_P_I.png       cropped figures

Final PDFs go to ``<workdir>/out/``.
"""

from __future__ import annotations

from pathlib import Path


def base_dir(workdir: str | Path, base: str) -> Path:
    return Path(workdir) / base


def pages_dir(workdir: str | Path, base: str) -> Path:
    return base_dir(workdir, base) / "pages"


def ocr_json(workdir: str | Path, base: str) -> Path:
    return base_dir(workdir, base) / "ocr.json"


def tex_dir(workdir: str | Path, base: str) -> Path:
    return base_dir(workdir, base) / "tex"


def figure_path(workdir: str | Path, base: str, page: int, index: int) -> Path:
    return tex_dir(workdir, base) / f"fig_{page}_{index}.png"


def page_pdf(workdir: str | Path, base: str, page: int) -> Path:
    return tex_dir(workdir, base) / f"p{page:02d}.pdf"


def page_tex(workdir: str | Path, base: str, page: int) -> Path:
    return tex_dir(workdir, base) / f"p{page:02d}.tex"


def out_dir(workdir: str | Path) -> Path:
    return Path(workdir) / "out"


def output_pdf(workdir: str | Path, base: str, target_lang: str, mode: str) -> Path:
    slug = "".join(ch for ch in target_lang if ch.isalnum()) or "target"
    return out_dir(workdir) / f"{base}.{slug}.{mode}.pdf"
