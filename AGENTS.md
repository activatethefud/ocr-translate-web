# AGENTS.md

Guidance for coding agents (and humans) working in this repository.
Read `PLAN.md` for the design, `TESTING.md` for the test strategy, and
`MEMORY.md` for session context.

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

1. **Never commit secrets.** API keys are **BYOK**: stored per browser session in
   the `sessions` table, encrypted with Fernet (`app/secrets.py`, key from
   `SECRET_KEY` or `STORAGE_DIR/secret.key`). They are never logged and never
   returned to the frontend (only `has_key` + a masked hint). A server-side env
   key is a **testing fallback** only. The engine accepts an explicit key:
   `Pipeline(cfg, api_key=...)`. (`/api/session` set/get/clear; `app/secrets.py`.)
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
├── TESTING.md               # test strategy
├── README.md                # user-facing intro + quickstart
├── docker-compose.yml       # api service (SQLite + FS volume)
├── Dockerfile               # multi-stage: build SPA -> python + TeX + fonts
├── .env.example             # config template (no secrets)
├── backend/
│   ├── ocrtran/             # ENGINE (done, M0) — importable package
│   │   ├── config.py        # PipelineConfig (BYOK, output modes)
│   │   ├── events.py        # Event + CancelToken
│   │   ├── providers.py     # OpenAI-compatible / DeepSeek adapter
│   │   ├── render.py        # pages + figure crops (PyMuPDF)
│   │   ├── ocr.py           # vision call -> blocks
│   │   ├── annotate.py      # translate words inside \text{...}
│   │   ├── latex.py         # standalone XeLaTeX compile (sandboxed)
│   │   ├── assemble.py      # interleave|grouped|side_by_side|translated_only
│   │   ├── verify.py        # empty pages, counts, leftovers
│   │   ├── cache.py         # content-hash caching
│   │   ├── paths.py         # artifact layout
│   │   ├── pipeline.py      # orchestrator
│   │   └── cli.py           # thin CLI over the engine
│   ├── app/                 # FastAPI service (done, M1)
│   │   ├── main.py          # routes: upload, jobs, SSE, pages, artifacts
│   │   ├── db.py            # SQLAlchemy models (SQLite)
│   │   ├── runner.py        # background job runner (ThreadPool)
│   │   ├── storage.py       # upload sanitization (pikepdf) + layout
│   │   ├── schemas.py       # pydantic request/response
│   │   └── settings.py      # env config
│   ├── tests/               # 69 tests + 1 live (unit + integration + API)
│   └── pyproject.toml
└── frontend/                # React + Vite + TS (done, M1)
    ├── src/App.tsx          # upload · configure · progress(SSE) · review
    ├── src/api.ts
    └── package.json
```

## Dev commands

> On this machine there is **no `python3-venv`** (`ensurepip` missing); the system
> Python already has `pymupdf`, `requests`, `pillow`, `pytest`, `ruff`. Run tests
> with `PYTHONPATH=.`.

```bash
# one-shot local run (no Docker): builds SPA, serves API + SPA on :8000
./run.sh            # ./run.sh dev (Vite :5173 + API :8000) | ./run.sh api

# engine + API tests (no network)
cd backend
PYTHONPATH=.deps:. pytest                  # 69 tests + 1 live (skipped)
PYTHONPATH=.deps:. pytest -m integration   # needs xelatex + pymupdf
ruff check . && ruff format --check .

# API server (local)
PYTHONPATH=.deps:. uvicorn app.main:app --reload

# frontend
cd frontend && npm install && npm run dev   # proxies /api -> :8000
npm run build                               # FastAPI serves ./frontend/dist at /

# docker (recommended)
docker compose up --build                   # http://localhost:8000
```

> **Why `PYTHONPATH=.deps:.`?** This machine's global env has FastAPI 0.115 with
> an incompatible Starlette 1.3.1. `backend/.deps` holds a matching pair
> (`fastapi==0.115.0`, `starlette==0.41.3`) installed with `pip install --target
> .deps --no-deps`. Docker installs the pinned versions from `pyproject.toml`
> instead, so this workaround is local-only. `backend/.deps/` is git-ignored.

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
