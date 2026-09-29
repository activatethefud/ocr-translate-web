# MEMORY.md — session / context log

Rolling log of decisions and work so future sessions (human or agent) can pick up
without re-deriving everything. Newest entries at the top. **No secrets here.**

## Session: output name + book-splitting plan

- **Output name**: default is derived from the **original upload filename** +
  target language (`"<stem> (<target>).pdf"`, e.g. `doktorske (French).pdf`);
  a user-supplied name is used as-is. Saved as
  `storage.safe_filename(name, fallback, ".pdf")` (strips directories/traversal,
  sanitises chars, ensures `.pdf`, caps length). Blank -> the engine's default
  `<base>.<target>.<mode>.pdf`. UI input with a computed placeholder.
- **Big jobs** planned in [`BIG_JOBS.md`](BIG_JOBS.md) (supersedes JOB_SPLITTING.md):
  book job -> contiguous chunks (child jobs via `pages="a-b"`), a dispatcher with a
  global model-call limiter + budget cap + pause/resume/cancel, **page-level merge**
  (we emit per-page `p-NN.pdf`), durable resume (L2) via the shared cache, per-chunk
  retry, chapter bookmarks, opt-in rolling glossary. Phases P0-P4.
- Tests: **156 passed + 1 live** (+7: safe_filename/traversal/ext/fallback/truncate,
  copy_artifact dest_name, output-name API default + explicit).

---

## Session: speed + figure-boundary judge

**Speed (quality-neutral):**
- Annotation translations are **batched**: one text call per page (was one per math
  block) — `annotate.annotate_blocks`.
- **Shared OCR cache at the document level** (`cache_dir`): re-runs and page edits
  across jobs are ~free. Measured: **44.7 s / $0.011 -> 1.4 s / $0.000** for a
  cached re-run of one page.
- `figure_mode="off"` skips the extra figure calls (1 model call/page, fastest).
- Already in place: parallel page OCR + parallel XeLaTeX builds.

**Figure boundaries (LLM judge):**
- `ocrtran/figures.py`: `judge_bboxes` asks the model to review candidate boxes and
  expand any that clip a diagram; the result is `union(candidate, judged)` so a box
  can only grow. `build_tex` still unions with the main-call box and pads
  (`figure_pad`).
- Config `figure_mode`: `off | tight | judge` (default `tight`).
- `ocrtran/jsonutil.py` (`parse_json`) to avoid an import cycle with `ocr`.

**Tests:** **149 passed + 1 live** (+14 this round: judge, figure modes, one-call
annotation batching, shared cache across workdirs, `cache_dir` config).

---

## Session: language picker expansion

- Frontend `LANGUAGES`: ~100 languages, sorted A→Z, plus an **Other…** option that
  reveals a free-text input (typed value is sent as source/target language).
  Source also keeps **Auto-detect**. Empty target language is rejected client-side.

---

## Session: figure-crop padding fix + test expansion

- **Symptom**: OCR figure crops were sometimes a tad too tight and clipped the edge
  of a diagram.
- **Fix**: new `ocrtran/geometry.py` (`union_bbox`, `expand_bbox`). `build_tex` now
  uses the **union of the model's tight box and its main-call box** (the main one is
  usually looser) and then pads by `figure_pad` (default `0.06`) with a minimum of
  `min_pad_pt=6`; `render_figure` clamps to the page and rejects degenerate boxes.
  New validated config `figure_pad`.
- **Tests: +54** (135 test functions; **137 passed + 1 live**). New files
  `test_geometry.py`, `test_figures.py` (marker-based: does a crop that should
  include the area *around* a box actually contain a nearby red patch?),
  `test_providers.py` (usage, retries, thread-local sessions),
  `test_concurrency.py`, `test_events.py`; extended pages/config/verify/assemble/
  ocr/annotate.

---

## Session: front-end progress fix (from the /tmp/doktorske.pdf report)

- **Symptom**: a 1-page job looked like it wasn't progressing in the UI.
- **Cause 1**: the runner set `progress=0.60` at the *start* of the OCR call, so the
  bar jumped to 60% and then froze for the ~45 s model call; `done_pages` only
  counted finished *builds*.
- **Cause 2**: `started_at` was serialized naive (SQLite drops tzinfo), so the
  browser parsed it as local time — the elapsed timer read ~7200 s.
- **Fix**: progress advances on *completed* pages (OCR "ok" per page → 0–60%,
  build → 95%, assemble → 99%, `done_pages` capped at total). The UI now shows an
  **indeterminate animated bar**, a live **stage label** (“reading page 1
  (OCR + translation)…”), and a ticking **elapsed** timer; all timestamps are
  serialized as UTC (`_iso`).
- **Verified** with a real headless Chromium over CDP (browser-use): 1-page run
  showed live stage + timer then completed; 3-page run progressed
  0 → 0.20 → 0.40 → 0.72 → 1.00; downloaded artifact checked (2-page FR PDF,
  correct translation).

---

## Session: M2b — session BYOK + LLM instructions

- **BYOK stored per session** server-side: `sessions` table, **Fernet-encrypted**
  (`app/secrets.py`; key from `SECRET_KEY` or `<STORAGE_DIR>/secret.key`, 0600).
  Endpoints `GET/PUT /api/session`, `DELETE /api/session/key`; the client sends a
  random `X-Session-Id` (localStorage). Job creation uses the session key (and a
  key sent with a job is saved to the session). `400` if no key and no env key.
  The key is **never returned** (only `has_key` + masked hint) and never logged.
- **`llm_instructions`** field (config + `JobCreate`) appended to the OCR prompt.
- Frontend: Save/Forget key + status; “Additional LLM instructions” textarea; the
  key is no longer kept in `localStorage` (only the session id is).
- `cryptography>=42` added to server deps. Tests: **72 passing + 1 live**.

---

## Session: M2 complete — verification, cost, glossary, model picker

### Engine (`ocrtran/`)
- `pricing.py`: token→USD (`cost_usd`) + pre-run `estimate_for_pages`.
- `providers.py`: captures `usage` (prompt/completion tokens, calls) per call.
- `config.py`: new fields `glossary` (list of {source,target}),
  `do_not_translate`, `verify_math`.
- `ocr.py`: glossary / do-not-translate injected into the OCR prompt.
- `verify.py`: `verify_math_page` + `run_math_check` (opt-in; vision model compares
  extracted formulas against the page image). Report now includes `math`.
- `pipeline.py`: `PipelineResult.usage`; runs the math check when enabled.

### API (`app/`)
- `GET /api/documents/{id}/estimate`, `GET /api/jobs/{id}/report`,
  `GET /api/usage` (real numbers), `Usage` table, `cost_usd` persisted on jobs.
- Job config carries glossary/do-not-translate/verify_math.

### Frontend
- Model input with **Load** (from `/api/models`) + datalist; glossary textarea
  (`source => target` per line); do-not-translate field; **verify formulas**
  checkbox; **cost estimate** line; **verification report** panel; usage in header.

### Tests
- 69 passing + 1 live (skipped unless `OCRtran_LIVE=1` + key). New: `test_pricing`,
  glossary prompt, `verify_math`, estimate/report/usage API.
- Bugs fixed as regressions: `Event(status=…)` duplicate kwarg (again) in
  `verify.run_math_check`; `pricing.price_for` prefix ordering (`gpt-4o` matched
  before `gpt-4o-mini`) → match longest prefix.

---

## Session: M1 complete — local web MVP

### Done
- **FastAPI service** `backend/app/`: upload (multipart, size cap, pikepdf
  sanitization), documents, jobs, **SSE progress**, pages, block edit + **reorder**,
  per-page **rebuild**, artifact download, models, usage, health.
- **SQLAlchemy models** (SQLite + WAL): documents, jobs, artifacts, events.
- **Background `JobRunner`** (ThreadPool, `WORKER_CONCURRENCY`, cancel tokens);
  persists every pipeline event; progress = 60% ocr + 35% build + assemble.
- **BYOK end to end**: key travels in the job request and is **never persisted**;
  env key remains a testing fallback.
- **React SPA** `frontend/` (Vite + TS, plain CSS): upload, config (languages,
  model, font, bilingual/combine), live progress via `EventSource`, page preview +
  block editor (edit target, reorder, rebuild page), download links.
- **Docker**: multi-stage `Dockerfile` (node build → python:3.12-slim + texlive +
  noto fonts) and `docker-compose.yml` (api + `/data` volume).
- **58 tests** (11 integration, incl. API through `TestClient` with a fake
  provider), ruff clean. Frontend builds (`npm run build`).

### Local env gotcha (important)
- Global env has **FastAPI 0.115 + Starlette 1.3.1 (incompatible)**. Installed a
  matching pair into `backend/.deps` (`fastapi==0.115.0`, `starlette==0.41.3`,
  `pip install --target .deps --no-deps`). Run tests/server with
  **`PYTHONPATH=.deps:.`**. `.deps/` is git-ignored. Docker uses the pinned
  versions from `pyproject.toml`, so the workaround is local-only.
- Frontend deliberately avoids Tailwind (plain CSS) to keep the dep tree tiny.

### Next: M2 — robustness & cost
- Verification pass (re-check formulas vs image, flag low-confidence pages).
- Model/provider picker in the UI (uses `/api/models`).
- Cost estimate before run + usage meter; glossaries / do-not-translate lists.
- Live API test (opt-in).

---

## Session: M0 complete — engine packaged

### Done
- Refactored the skill pipeline into an importable package `backend/ocrtran/`:
  `config`, `events`, `providers`, `render`, `ocr`, `annotate`, `latex`,
  `assemble`, `verify`, `cache`, `paths`, `pipeline`, `cli`.
- **BYOK**: `PipelineConfig.resolve_api_key(override)` — an explicit key wins,
  otherwise falls back to the env var (for local testing). `Pipeline(cfg,
  api_key=...)`.
- **Output modes**: `interleave | grouped | side_by_side | translated_only`
  (`bilingual` on/off + `combine`), implemented in `assemble.py`.
- **Sandboxed compile**: `xelatex -no-shell-escape` + `openin_any=p` /
  `openout_any=p`, timeout.
- **Content-hash caching** (`cache.py`): `sha256(file)+page+model+prompt_version`.
- **CLI** unchanged in spirit: `python -m ocrtran.cli {ocr,annot,build,assemble,run}`.
- **48 tests** (9 integration, real xelatex/PyMuPDF), `ruff` clean. See `TESTING.md`.
- Docs: `PLAN.md`, `TESTING.md`, `AGENTS.md`, `README.md`, `.gitignore`, `.env.example`.

### Notes
- Tests run with **system Python + `PYTHONPATH=.`** because this box has no
  `python3-venv` (ensurepip missing). System Python already has pymupdf/requests/
  pillow/pytest/ruff.
- Commit: "M0: engine package …" (see git log).

### Next: M1 — local web MVP
- FastAPI (`backend/app/`) + SQLite + filesystem + SSE; React SPA (`frontend/`).
- Upload → configure (BYOK key in UI, languages, `bilingual`/`combine`) → progress
  → **preview + simple block-order editor** → download.
- Docker Compose (api + worker + frontend), upload sanitization (qpdf), rate/cost caps.

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
