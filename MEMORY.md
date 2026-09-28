# MEMORY.md — session / context log

Rolling log of decisions and work so future sessions (human or agent) can pick up
without re-deriving everything. Newest entries at the top. **No secrets here.**

---

## Session: building the engine + planning the web app

### Where we are
Planning the **ocr-translate web app** (this repo). Decisions locked (see
`PLAN.md` §14). Next step: **M0 — refactor the skill's pipeline into the
`ocrtran/` package** (same behavior, importable, with progress callbacks +
cancellation), then M1 local web MVP.

### What the engine is
A vision-LLM OCR+translate pipeline (no Tesseract). Per page:
`render (PyMuPDF) → vision model → structured JSON blocks
{heading|prose|math|table|figure + target translation + LaTeX + figure bbox} →
annotate words inside math → XeLaTeX (standalone) → assemble (scale to page)`.

- Reference implementation / skill:
  `~/.agents/skills/ocr-translate/scripts/vision_translate.py`
- Skill docs (pitfalls, config, how figures are transferred):
  `~/.agents/skills/ocr-translate/SKILL.md`
- Subcommands: `ocr`, `annot`, `build`, `assemble`, `run`; config-driven; caches
  every page under `<workdir>/ocr/*.json`.

### Work completed this session
1. **matematiranje.in.rs (IV godina) → Chinese**, 4 handouts:
   `2.INVERZNA_FUNKCIJA`, `3.KOMPOZICIJA_FUNKCIJA`,
   `4.OBLAST_DEFINISANOSTI_FUNKCIJE`, `5.NULE_FUNKCIJE_I_ZNAK_FUNKCIJE`.
   - Output: `/home/nikola/matematiranje/out/*.bilingual.pdf` (Serbian page →
     Chinese page, alternating).
   - v1 was in-place text replacement (formulas re-typed as Unicode — messy).
   - **v2 (final)** re-created each Chinese page from a vision model with real
     LaTeX. Verified: Serbian pages text-identical, no Serbian leftovers.
2. **Created the `ocr-translate` skill** (see above) and improved it several
   times: `max_px`, `figure_px`, `max_scale: 0`, top-align, source-PDF figure
   crops, empty-page handling.
3. **Talesova teorema i sličnost trouglova (udžbenik) → French**:
   - Source: `/home/nikola/Transfer/nikola/Downloads/Talesova teorema i slicnost trouglova udzbenik.pdf`
     (16-page **scan**, page size 2480×3508 pt, one JPEG per page).
   - Workdir: `/home/nikola/tales_ocr_work/` (OCR JSON, `tex/`, `out/`).
   - Outputs (also copied to Downloads):
     `…French-bilingue.pdf` (32 p) and `…French-seulement.pdf` (16 p); all pages
     2480×3508, content scaled to fill.
   - Fixed: blank page 13 (model returned empty → re-OCR'd); originally content
     was tiny/centred → `max_scale: 0` + high-res figure crops.
4. **Planned this web app** (`PLAN.md`, `AGENTS.md`, this file).

### Locked decisions (web app)
Single-user tool tolerating ~3–4 concurrent jobs · **React SPA** · review UI =
**preview + simple block-order editor** in MVP (full block editing phase 2) ·
first-class output options (`bilingual` on/off; `combine` = interleave | grouped |
side-by-side; translated-only) · server-side API key from env · DeepSeek +
generic OpenAI-compatible providers · **Docker Compose** · upload sanitization +
LaTeX sandbox + sane defaults · **SQLite + filesystem + small worker pool** ·
git with `AGENTS.md` + `MEMORY.md`.

### Environment facts (this machine)
- Python 3.13; `pymupdf`, `requests`, `pillow`, `fontTools`, `reportlab`.
- `xelatex` (TeX Live 2025), `fontspec`, `amsmath`, `standalone`, `adjustbox`,
  `cancel`, `graphicx`. **No `xeCJK`/`ctex`** → CJK uses
  `\XeTeXlinebreaklocale "zh"` + `\XeTeXlinebreakskip`.
- `pdftoppm`, `pdfinfo` (poppler); `qpdf`/`pikepdf` for sanitization.
- Fonts: `Noto Sans CJK SC`, `Noto Serif CJK*`, `Noto Serif`, `DejaVu*`,
  `Liberation*`, `Noto Naskh Arabic` (for RTL).
- Vision model: DeepSeek **`deepseek-flash`** (accepts `image`;
  `deepseek-v4-pro` is text-only). Key is provided via env var **`DS_KEY`** at
  runtime only — **never written to the repo**.

### Cost observed
- matematiranje (33 pages + figure/annotation calls): ~$0.45.
- Talesova teorema (16 scanned pages, ~1 min/page): ~$0.40.

### Gotchas / lessons (also in AGENTS.md + skill SKILL.md)
- Math display-env nesting; escape only outside `$...$`; `standalone` for
  one-page-per-page; huge-scan caps; CJK line breaking; missing super/subscripts
  in CJK fonts; PDF private-use glyphs; **empty OCR responses must be retried**;
  figures = model gives bbox, code crops the source.

### Security note
The DeepSeek API key was shared in plain chat during this session. It is **not**
stored in this repo. If it is still active, **rotate it** and keep the new one in
`.env` (git-ignored) only.

### Open threads / next steps
1. **M0**: package `ocrtran/` from the skill; add events + cancellation; keep CLI
   behavior identical (add a regression test on a small fixture).
2. **M1**: FastAPI + SQLite + FS + SSE; React SPA upload → progress → preview →
   simple block-order editor → download (interleave/grouped/side-by-side/
   translated-only).
3. Docker Compose (api + worker + frontend build) with `.env` and the LaTeX
   sandbox.
4. Phase 2: full block editor, verification pass (formula re-check), model
   picker, glossaries.
