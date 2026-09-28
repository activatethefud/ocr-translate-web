# OCR + Translate — Web App

A self-hosted web app that turns documents in one language into well-typeset PDFs
in another — **without losing the mathematics**.

Upload a PDF (scanned or text), pick a target language, and the app renders each
page, sends it to a vision model that returns structured content (prose +
real LaTeX + figure boxes), re-typesets it with XeLaTeX, and gives you a
bilingual or translated-only PDF with the original figures intact.

> Status: **planning / scaffolding.** The engine exists as the
> `ocr-translate` skill; the web app is being built on top of it.

## Why not plain OCR?

Tesseract and `pdftotext` mangle formulas. Here the vision model reads the page
image and emits LaTeX, so fractions, roots, exponents, matrices and set notation
survive, and diagrams are cropped from the original and re-inserted.

## Docs

- [`PLAN.md`](PLAN.md) — design, architecture, features, milestones, decisions.
- [`TESTING.md`](TESTING.md) — test strategy, layers, failure injection, security tests.
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

## Quickstart (planned)

```bash
cp .env.example .env          # add DS_KEY and preferred settings
docker compose up --build     # api + worker (+ frontend)
# open http://localhost:8000
```

Local dev (without Docker):

```bash
# backend
cd backend && pip install -e ".[dev]" && uvicorn app.main:app --reload
# frontend
cd frontend && npm install && npm run dev
```

## Output options

- `bilingual` — keep the original pages (or not).
- `combine` — `interleave` (original, translation, …) · `grouped` (all originals,
  then all translations) · `side-by-side` (both languages on one page).
- `translated-only` — just the translated document.

## License

TBD.
