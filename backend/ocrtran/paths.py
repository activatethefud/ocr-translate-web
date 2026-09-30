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


def figure_path(workdir: str | Path, base: str, page: int, index: int, ext: str = "png") -> Path:
    return tex_dir(workdir, base) / f"fig_{page}_{index}.{ext.lstrip('.')}"


def page_pdf(workdir: str | Path, base: str, page: int, part: int = 1) -> Path:
    name = f"p{page:02d}.pdf" if part <= 1 else f"p{page:02d}-{part}.pdf"
    return tex_dir(workdir, base) / name


def page_pdfs(workdir: str | Path, base: str, page: int) -> list[Path]:
    """All output pages produced for one source page, in order."""
    d = tex_dir(workdir, base)
    out = []
    first = d / f"p{page:02d}.pdf"
    if first.exists():
        out.append(first)
    out.extend(sorted(d.glob(f"p{page:02d}-*.pdf")))
    return out


def page_tex(workdir: str | Path, base: str, page: int, part: int = 1) -> Path:
    name = f"p{page:02d}.tex" if part <= 1 else f"p{page:02d}-{part}.tex"
    return tex_dir(workdir, base) / name


def out_dir(workdir: str | Path) -> Path:
    return Path(workdir) / "out"


def output_pdf(workdir: str | Path, base: str, target_lang: str, mode: str) -> Path:
    slug = "".join(ch for ch in target_lang if ch.isalnum()) or "target"
    return out_dir(workdir) / f"{base}.{slug}.{mode}.pdf"
