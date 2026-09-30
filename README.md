# OCR + Translate — Web App

A self-hosted web app that turns documents in one language into well-typeset PDFs
in another — **without losing the mathematics**.

Upload a PDF (scanned or text), pick a target language, and the app renders each
page, sends it to a vision model that returns structured content (prose +
real LaTeX + figure boxes), re-typesets it with XeLaTeX, and gives you a
bilingual or translated-only PDF with the original figures intact.

> Status: **M1 done** — working local web app (FastAPI + SQLite + SSE, React SPA,
> Docker). See `PLAN.md` for the roadmap (M2: review editor polish, verification,
> cost controls).

## Why not plain OCR?

Tesseract and `pdftotext` mangle formulas. Here the vision model reads the page
image and emits LaTeX, so fractions, roots, exponents, matrices and set notation
survive, and diagrams are cropped from the original and re-inserted.

## Docs

- [`PLAN.md`](PLAN.md) — design, architecture, features, milestones, decisions.
- [`TESTING.md`](TESTING.md) — test strategy, layers, failure injection, security tests.
- [`BIG_JOBS.md`](BIG_JOBS.md) — plan for translating whole books (chunking, durability, cost, merge).
- [`PAGE_FITTING.md`](PAGE_FITTING.md) — plan for keeping text size consistent and laying out figures intelligently (split across pages instead of shrinking).
- [`ABUSE_PROTECTION.md`](ABUSE_PROTECTION.md) — rate limits, cooldowns, budgets, anti-flood plan.
- [`AGENTS.md`](AGENTS.md) — guidance for coding agents (rules, layout, commands).
- [`MEMORY.md`](MEMORY.md) — session/context log.

## Engine

The pipeline is implemented as a reusable skill:

```
~/.agents/skills/ocr-translate/
├── SKILL.md
├── config.example.json
└── scripts/vision_translate.py     # ocr | annot | build | assemble | run
```

It will be refactored into this repo's `backend/ocrtran/` package (same behavior,
importable, with progress + cancellation) so the web app and CLI share one engine.

## Quickstart

```bash
cp .env.example .env          # optional: set DS_KEY to use the server-key fallback
docker compose up --build     # http://localhost:8000
```

Or run locally (no Docker):

```bash
./run.sh            # build the SPA and serve everything on http://localhost:8000
./run.sh dev        # backend :8000 (reload) + Vite dev server :5173
./run.sh api        # backend only
```

`run.sh` reproduces the local setup: it loads `.env`, checks dependencies, sets up
the FastAPI/Starlette override in `backend/.deps` if needed, builds the frontend,
and starts uvicorn (and Vite in `dev` mode). Env: `HOST`, `PORT`, `STORAGE_DIR`,
`DS_KEY`, `DEFAULT_MODEL`, `API_BASE`.

Manual backend/frontend:

```bash
# backend API (note: PYTHONPATH=.deps:. works around a local FastAPI/Starlette
# version clash; see AGENTS.md)
cd backend && PYTHONPATH=.deps:. uvicorn app.main:app --reload

# frontend dev server (proxies /api -> :8000)
cd frontend && npm install && npm run dev
```

Bring your own key (BYOK) in the UI — it is **stored encrypted for your browser
session** (click *Forget* to clear it), or set `DS_KEY` in the environment for
testing. You can also add a **glossary**, **do-not-translate** terms, and free-form
**additional LLM instructions** per job.

## Output options

- `bilingual` — keep the original pages (or not).
- `combine` — `interleave` (original, translation, …) · `grouped` (all originals,
  then all translations) · `side-by-side` (both languages on one page).
- `translated-only` — just the translated document.
- **Page numbers** found on the original (header/footer) are reproduced as a small footer
  on the translated page, so numbering survives translation.

When you translate a **page selection** (e.g. `10,15,20`), the default is to **omit** the
unselected pages (`unprocessed=skip`), so only those pages appear. Switch to
**keep original page** to keep the whole document with just the chosen pages translated.

## Multiple documents (sequential batches)

Tick **queue multiple documents** and select any number of documents; they are
processed **one after another** (never in parallel) with one shared config. Each
document gets its own child job, events, progress and artifact, named `NN <name>
(<target>).pdf`; the batch page shows a per-document table. Because each document has
its own cache, re-queuing a document is free where it is unchanged.

Tick **queue multiple documents** together with **book mode** to queue **whole books**:
each document becomes a durable, chunked `kind="book"` child (chunked + resumable),
still processed one book at a time.

API: `POST /api/batch` (`{document_ids, book, chunk_size, ...JobCreate}`) → a
`kind="batch"` job;
`GET /api/jobs/{id}/children` lists the child jobs; `POST /api/jobs/{id}/cancel`
cancels the batch. A restart re-queues only the interrupted document.

## License

GPL-3.0-or-later — see [LICENSE](LICENSE).
