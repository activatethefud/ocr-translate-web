# AGENTS.md

Guidance for coding agents (and humans) working in this repository.
Read `PLAN.md` for the design and `MEMORY.md` for session context.

## What this is

A self-hosted web app that wraps the **ocr-translate** vision pipeline: upload a
PDF/images, translate to another language, review/preview, and download a
bilingual or translated-only PDF with real LaTeX formulas and original figures.

The engine already exists as a CLI + skill. **Do not re-implement it.** Reuse and
refactor it into the importable `ocrtran/` package.

- Skill / engine source: `~/.agents/skills/ocr-translate/`
  - `SKILL.md` — how the pipeline works + hard-won pitfalls
  - `scripts/vision_translate.py` — reference implementation

## Non-negotiable rules

1. **Never commit secrets.** API keys live in `.env` (git-ignored) and are read
   from env at runtime. Never log or return keys to the frontend.
2. **LaTeX is untrusted code.** Compile with `-no-shell-escape`,
   `openin_any=p`, `openout_any=p`, in a per-job temp dir, as a non-root user,
   with timeouts and ulimits. See `PLAN.md` §11.
3. **Sanitize uploads** (qpdf/pikepdf: strip JS, embedded files, launch actions)
   before doing anything else with them.
4. **Reuse the engine.** Pipeline logic (render/ocr/annotate/latex/assemble/
   verify) lives in one place: `ocrtran/`. The CLI is a thin wrapper over it.
5. **Keep it runnable locally and via Docker Compose.** No hidden global state.
6. **Cache by content hash** (`sha256(file) + page + model + prompt_version`) so
   re-runs and single-page edits never re-pay for finished work.

## Target repo layout

```
ocr-translate-web/
├── AGENTS.md                # this file
├── MEMORY.md                # session / context log
├── PLAN.md                  # design + decisions
├── README.md                # user-facing intro + quickstart
├── docker-compose.yml       # api + worker (+ build frontend)
├── .env.example             # config template (no secrets)
├── backend/
│   ├── ocrtran/             # the engine package (refactored from the skill)
│   │   ├── render.py
│   │   ├── providers.py     # openai-compatible / deepseek / openrouter
│   │   ├── ocr.py
│   │   ├── annotate.py
│   │   ├── latex.py
│   │   ├── assemble.py      # interleave | grouped | side-by-side
│   │   ├── verify.py
│   │   ├── pipeline.py      # orchestrator: events + cancellation
│   │   └── config.py
│   ├── app/                 # FastAPI: routes, models, db, jobs, storage
│   ├── tests/
│   └── pyproject.toml
└── frontend/                # React + Vite + TS + Tailwind
    ├── src/
    └── package.json
```

## Dev commands (once scaffolded)

```bash
# backend
cd backend && python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
uvicorn app.main:app --reload            # api
python -m ocrtran.cli run --config x.json   # engine via CLI (unchanged behavior)

# frontend
cd frontend && npm install && npm run dev

# tests / lint
pytest
ruff check . && ruff format .
npm run lint && npm run build

# docker
cp .env.example .env        # then edit
docker compose up --build
```

## Conventions

- Python 3.13, FastAPI, SQLModel/SQLAlchemy, `ruff`, `pytest`.
- Frontend: React + TypeScript + Vite + Tailwind; talk to the API via REST + SSE.
- Config: a dataclass with the same keys as the skill's JSON config
  (`source_lang`, `target_lang`, `model`, `font_main`, `linebreak_locale`,
  `max_px`, `figure_px`, `max_scale`, output `bilingual`/`combine`, …).
- Jobs run in workers, never inside request handlers.
- Emit structured progress events: `{stage, page, index, total, status, message}`.
- Small, focused PRs; update `PLAN.md`/`MEMORY.md` when decisions change.

## Hard-won gotchas (from building the engine — don't relearn these)

- Never wrap a math block that is already `align`/`equation`/`gather` in `\[…\]`
  → "Erroneous nesting of equation structures".
- Escape `& % # _ { } ~ ^` **only outside** `$...$` math.
- One output page per source page: `standalone` class + fixed-width `minipage`,
  then scale-to-fit in assembly (`adjustbox` can still spill to 2 pages).
- `max_px` caps rendering of huge scans; `max_scale: 0` enlarges the translation
  to fill the same large page; `figure_px` crops figures from the **source PDF**
  so they stay sharp.
- Vision models lack many super/subscripts in CJK fonts → `^(…)`/`_(…)`.
- PDF private-use glyphs (`\uf8xx`) are matrix brackets → map/strip.
- The model sometimes returns **empty** for a page → always scan for
  `blocks == []` and retry those pages.
- Figures: model outputs **coordinates only**; the code crops the original.

## Definition of done

- Engine behavior unchanged (CLI outputs match before/after refactor).
- New features covered by tests, including an end-to-end run on a small fixture.
- No secrets in diff; `ruff`/`pytest`/frontend build green.
