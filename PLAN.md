# OCR + Translate — Web App Plan

Turn the `ocr-translate` skill (a Python CLI) into a web application where a user
uploads a PDF/images, picks a target language, watches progress, reviews/fixes the
recognition, and downloads a translated or bilingual PDF.

The core engine already exists (`~/.agents/skills/ocr-translate/scripts/vision_translate.py`).
This app wraps it: upload + orchestration + progress + review UI + storage + cost
controls.

---

## 1. Goals / non-goals

**Goals**
- Upload PDFs or images; get a faithful translated/bilingual PDF with real
  formulas and original figures.
- Never make the user wait blind: per-page progress, previews, retries.
- Let the user **review and fix** OCR/translation per page before final render.
- Track and cap cost per job.
- Handle the messy real world: huge scans, scans vs text PDFs, many languages,
  RTL/CJK, missing glyphs, empty-page OCR failures.

**Non-goals (for now)**
- Collaborative multi-tenant SaaS (possible later).
- Re-drawing diagrams (we copy the originals).
- Perfect layout preservation (current output is a linear reflow; layout-preserving
  is a future mode).

---

## 2. Users & scenarios

| User | Scenario |
|---|---|
| Teacher / student | Translate a scanned textbook chapter into another language. |
| Researcher | Translate a paper/lecture notes, keep equations exact. |
| Translator | Draft translation with formulas, then edit wording per page. |
| Developer | Call the same pipeline via API / batch folder. |

Primary scenario (MVP): one person, one document, one target language, download.

---

## 3. Core user flow

```
1. Upload            drag a PDF (or images) -> page count / size detected
2. Configure         source lang, target lang, model, mode, font, options
                     -> cost + page estimate shown BEFORE starting
3. Run               per-page progress grid (render -> OCR -> build -> assemble)
4. Review (v1.1)     page list; open a page: original image | editable blocks
                     edit translation / LaTeX, re-crop figure, re-render that page
5. Download          bilingual PDF, translated-only PDF, or ZIP of pages
```

---

## 4. Feature breakdown

**MVP (phase 1)**
- Upload PDF/images, detect page count, size, text-vs-scan.
- Choose source/target language + font (sane defaults per language).
- **Output options**: `bilingual: on/off`; when on, `combine: interleave |
  grouped | side-by-side` (side-by-side = original + translation on the same
  page). Plus `translated-only` output.
- Run the existing pipeline as a background job; live progress; cancel.
- **Simple preview** of rendered pages, and a **basic block-order editor**
  (reorder blocks within a page; edit a block's text/LaTeX is phase 2).
- Download results; optionally as a ZIP.
- Persist document + job + artifacts; re-run is cached/idempotent.
- Cost estimate + usage meter.

**Phase 2**
- Block-level review editor (the big one).
- Per-page rebuild after edits; figure re-crop.
- Multi-document library, history, duplicate/re-run with new settings.
- Model/provider picker with capability check (`input_modalities`).
- Verification pass: re-check formulas against the image, flag suspicious pages.
- Glossaries / do-not-translate term lists; prompt templates.

**Phase 3**
- Auth, users, quotas; Postgres + object storage; horizontal workers.
- Layout-preserving mode; OCR engine fallback (Tesseract) for plain text to cut cost.
- Public API + webhooks; batch/folder ingest.

---

## 5. Architecture

```
                 ┌──────────────────────────────┐
   Browser ─────▶│  Frontend (React/Vite SPA)   │
     ▲           │  upload · progress · editor  │
     │ SSE       └──────────────┬───────────────┘
     │                          │ REST + SSE
     │           ┌──────────────▼───────────────┐
     └───────────│  API server (FastAPI)        │
                 │  auth · CRUD · uploads · SSE │
                 └───────┬───────────────┬──────┘
                         │               │
                 ┌───────▼──────┐  ┌─────▼───────────────┐
                 │  Database    │  │  Object store        │
                 │  (SQLite →   │  │  (local FS → S3)     │
                 │   Postgres)  │  │  uploads, renders,   │
                 └──────────────┘  │  JSON, tex, pdf      │
                                   └──────────────────────┘
                         ▲
                 ┌───────┴──────────────────────────────┐
                 │  Worker(s)  (arq/RQ/Celery, or       │
                 │  in-process asyncio for local MVP)   │
                 │  pipeline: render→vision→annot→      │
                 │  latex→assemble→verify               │
                 │  needs: pymupdf, xelatex, fonts      │
                 └──────────────────────────────────────┘
```

Why Python workers: the engine is Python and shells out to `xelatex`; keep OCR
and typesetting server-side. Frontend is only UI.

---

## 6. Engine refactor (do this first)

Turn the single CLI file into an importable package so both CLI and web use it.

```
ocrtran/
  __init__.py
  render.py      # PDF/image -> page PNGs (max_px cap)
  providers.py   # vision adapters: openai_compatible(base,model), deepseek, openrouter…
  ocr.py         # page -> blocks JSON (+ figure bbox pass)
  annotate.py    # translate source words inside \text{...}
  latex.py       # blocks -> standalone .tex -> xelatex -> 1-page PDF
  assemble.py    # interleave/translated-only, scale-to-fit
  verify.py      # empty pages, leftovers, formula re-check
  pipeline.py    # orchestrator with progress callbacks + cancellation
  config.py      # dataclass config (same keys as the JSON config)
```

- `Pipeline.run(cfg, on_event=..., cancel_token=...)`.
- Stages emit structured events: `{stage, page, index, total, status, message}`.
- Every artifact written to a per-job dir; the existing JSON cache becomes the
  job's resumable state.
- CLI becomes a thin wrapper over the package (keep `vision_translate.py` working).

---

## 7. Data model (SQLite → Postgres)

```
users(id, email, api_key_enc, created_at)            # phase 3
projects(id, user_id, name, created_at)
documents(id, project_id, filename, sha256, n_pages, page_w, page_h,
          kind[scan|text|mixed], status, created_at)
jobs(id, document_id, config_json, model, source_lang, target_lang, mode,
     status[queued|running|done|failed|canceled], progress, cost_usd,
     error, started_at, finished_at)
pages(job_id, page_no, status, render_path, ocr_json_path,
      tex_path, pdf_path, error, updated_at)
blocks(page_id, idx, type, source, target, latex, description, bbox_json,
       edited_by_user bool)
artifacts(id, job_id, kind[bilingual|translated_only|zip|page_pdf],
          path, bytes, created_at)
usage(id, job_id, provider, model, input_tokens, output_tokens, cost_usd, ts)
settings(key, value)                                  # default model, fonts, budget
```

Files live on disk keyed by job + page; DB stores metadata only.

---

## 8. API sketch (FastAPI)

```
POST   /api/documents                 upload (multipart, streamed)
GET    /api/documents/{id}            metadata + page list
POST   /api/documents/{id}/jobs       start job {config}
GET    /api/jobs/{id}                 status/progress
GET    /api/jobs/{id}/events          SSE progress stream
POST   /api/jobs/{id}/cancel
GET    /api/jobs/{id}/pages/{n}       {image_url, blocks[]}
PATCH  /api/jobs/{id}/pages/{n}/blocks/{idx}   edit target/latex/bbox
POST   /api/jobs/{id}/pages/{n}/rebuild        re-typeset one page
GET    /api/jobs/{id}/artifacts       list
GET    /api/artifacts/{id}            download
GET    /api/models                    vision models whose modalities include image
GET    /api/usage                     cost/token totals
```

---

## 9. Job orchestration & caching

- Long tasks (30–60 s/page) run in workers; the API never blocks.
- Local MVP: FastAPI `BackgroundTasks`/asyncio + a subprocess for xelatex; a
  single worker with a small concurrency (e.g. 2–4 pages) respects provider limits.
- Later: Redis + arq/RQ/Celery; workers are stateless and scale horizontally.
- **Idempotency/caching** keyed by `sha256(file) + page + model + prompt_version`.
  Re-running or editing one page never re-pays for finished pages.
- Progress events persisted so a page refresh resumes the UI.
- Cancellation: set a flag + terminate the xelatex subprocess.

---

## 10. Cost & rate control

- Estimate before running: `pages × avg_tokens × price` + figure/annotation calls.
- Show estimate and ask to confirm; enforce a per-job budget cap.
- Per-provider concurrency + retry/backoff, exponential.
- Usage meter per job/user; cache hit rate shown.
- Optional cheap path: if a page has a text layer and no formulas, skip the
  vision call (Tesseract/pdftotext) — big saving on plain documents.

---

## 11. Security — must-do, not optional

LaTeX compilation and file uploads are the risky parts. The LaTeX is
model-generated and user-editable, so treat it as **untrusted**.

- Run `xelatex` with `-no-shell-escape`, `openin_any=p`, `openout_any=p`
  in a per-job temp dir, as a non-root user, with `timeout` and `ulimit`
  (CPU/mem/file size), no network.
- Ideally run the worker in a container that has TeX Live and nothing else
  (or a separate short-lived "compile" container per page).
- Sanitize uploads (qpdf/pikepdf): strip JS, embedded files, launch actions.
- Validate type/size; cap pages; reject encrypted unless password supplied.
- API keys: only server-side (env or encrypted at rest); never return to client;
  redact from logs.
- Rate limiting + CSRF + auth once it's not purely local.

---

## 12. Deployment

- **Local MVP**: `docker compose` with two services: `api` and `worker`
  (same image: Python + TeX Live + fonts + poppler), plus a volume. Or run
  locally without Docker (system already has xelatex/pymupdf).
- **Later**: Postgres + Redis + MinIO/S3; reverse proxy (Caddy) with TLS; queue
  autoscaling; per-language worker images (CJK fonts, Arabic fonts…).
- Config via env: `DATABASE_URL`, `REDIS_URL`, `STORAGE_DIR`, `DS_KEY`,
  `DEFAULT_MODEL`, `MAX_JOB_USD`.

---

## 13. Milestones

**M0 — Engine packaging (1–2 days)**
- Split into `ocrtran/` package; CLI unchanged in behaviour; add event callbacks
  and cancellation. Tests around the known pitfalls (see [`TESTING.md`](TESTING.md)).
  - ✅ done: `backend/ocrtran/` with `render/ocr/annotate/latex/assemble/verify/`
    `pipeline/config/events/cache/paths/providers`, CLI, 48 tests (9 integration).
  - ✅ BYOK: `PipelineConfig.resolve_api_key(override)` — explicit key wins, env
    falls back for testing.

**M1 — Local web MVP (3–5 days)**
- FastAPI + SQLite + FS, one job at a time, SSE progress, download bilingual /
  translated-only. Minimal frontend (upload + progress + download).
- Reuses **everything** we already built.

**M2 — Review editor (1 week)**
- Side-by-side original vs blocks; edit target/LaTeX; per-page rebuild; figure
  re-crop; empty/failed page retry.

**M3 — Robustness & cost (1 week)**
- Verification pass, model picker with capability check, cost estimate/usage,
  glossaries, batch.

**M4 — Multi-user (as needed)**
- Auth, quotas, Postgres/S3/Redis, layout mode, public API.

---

## 14. Resolved decisions

- **Scale:** single-user tool, but handle **~3–4 concurrent users/jobs**
  gracefully (small worker pool + per-provider concurrency limit).
- **Frontend:** **React SPA** (Vite + TypeScript + Tailwind).
- **Review UI:** *not* a full editor in MVP. MVP = **page preview** + a **simple
  block-order editor** (reorder/allow-disable blocks). Full per-block text/LaTeX
  editing is phase 2.
- **Output options:** first-class `bilingual` on/off and `combine` mode:
  `interleave`, `grouped`, `side-by-side`; plus `translated-only`. Configurable
  per job, exposed in the UI.
- **API keys:** **BYOK (bring-your-own-key)** is the primary model — a key can be
  supplied per user/session (encrypted at rest, never returned to the client).
  A server-side env key (`.env`, never committed) is the **fallback used for
  local testing** only.
- **Providers:** DeepSeek first + a generic **OpenAI-compatible** adapter so
  OpenRouter / OpenAI / local endpoints work by config.
- **Deployment:** **Docker Compose** (api + worker + optional frontend build),
  sane defaults, `.env` for secrets. Also runnable locally without Docker.
- **Sanitization / sane defaults:** strip PDF JS/embedded files on upload
  (qpdf/pikepdf), size/page caps, `-no-shell-escape` LaTeX sandbox, budget caps.
- **Storage:** SQLite (WAL) + local filesystem + a small worker pool (enough for
  3–4 concurrent jobs); Postgres/S3/Redis only if it ever grows.
- **VCS:** git, with `AGENTS.md` (agent guidance) and `MEMORY.md` (session/context
  log) in the repo root.

Still open for phase 2+: exact side-by-side layout, glossary support,
layout-preserving mode.
