# TESTING.md — test strategy

Goal: the engine can be trusted to produce correct PDFs, never burn API money on
avoidable bugs, and fail safely on hostile input. Tests are the contract that
lets us change the pipeline without re-checking everything by hand.

## Principles

1. **No network in the default test run.** Providers are faked. Live API tests
   are opt-in and clearly marked.
2. **Deterministic.** Time, randomness, and model output are injected or fixed.
3. **Pyramid.** Many fast unit tests; a few integration tests that really run
   `xelatex`/PyMuPDF; a tiny number of live tests.
4. **Fixtures over downloads.** Tiny generated PDFs (no copyrighted content), plus
   committed *recorded* model outputs so typesetting is tested offline.
5. **Every past bug becomes a regression test.** (See "Regression catalogue".)

## Commands

```bash
cd backend
PYTHONPATH=. pytest                     # unit + integration (no network)
PYTHONPATH=. pytest -m "not integration"  # pure unit, fastest
PYTHONPATH=. pytest -m integration      # needs xelatex + pymupdf
PYTHONPATH=. pytest --cov=ocrtran --cov-report=term-missing
ruff check . && ruff format --check .

# live (spends money; 1 tiny page)
OCRtran_LIVE=1 DS_KEY=... PYTHONPATH=. pytest -m live -v
```

`pyproject.toml` registers markers `integration` and `live`; integration tests
skip themselves when `xelatex` is missing.

## Layers

### 1. Unit (fast, no system tools)
- **config**: defaults, unknown-key rejection, `mode` back-compat,
  `output_mode` truth table, source-glob resolution, BYOK-vs-env key resolution.
- **latex utils**: `esc_text` escapes only outside `$...$`; `wrap_math` never
  nests a display env; preamble honours `font_main`, `linebreak_locale`,
  `extra_preamble`.
- **ocr**: tolerant `parse_json` (fences / prose / bad), prompt placeholder
  substitution, `ocr_image` returns blocks (+ tight boxes when figures exist).
- **annotate**: `find_text_groups` incl. nested braces; `is_source_text`
  filtering; batch + per-item fallback paths.
- **cache**: sha stability, cache key varies with page/model/prompt_version,
  corrupt-JSON returns `None`.
- **render** (needs pymupdf only): `page_count`, `page_size`, `max_px` cap,
  DPI mapping, `render_figure` bbox→clip + bad-bbox guard.
- **verify**: empty-OCR flagging, leftover-word helper, page-count check.
- **assemble**: mode→page-count truth table; fallback when a translated page is
  missing; page-size equality with the source.

### 2. Integration (real xelatex + PyMuPDF, still no network)
- Full `Pipeline.run()` with a `FakeProvider` → assert output exists, page count
  per mode, no `empty_page`/`empty_ocr`/`page_count` issues.
- **Typesetting goldens**: for a set of recorded OCR JSON fixtures, compile and
  assert the produced page's extracted text and page size match a stored
  expectation. Catches XeLaTeX/preamble regressions offline.
- **Figure transfer**: a fixture with a `figure` + `tight` bbox → assert
  `fig_P_I.png` is created, dimensions ≈ `figure_px`, and the compiled page
  contains an image.
- **Caching**: second run makes zero additional vision calls.
- **Cancellation**: cancel mid-run raises cleanly and leaves no half-written
  output PDF.
- **Malformed LaTeX recovery**: a fixture whose `latex` is broken → build reports
  an error for that page and the rest still compile.

### 3. Live (opt-in, network + cost)
- Marked `live`, skipped unless `OCRtran_LIVE=1` and a key is set.
  Run: `OCRtran_LIVE=1 DS_KEY=… PYTHONPATH=.deps:. pytest -m live -v`.
- `test_live_one_page`: one 1-page generated fixture (prose + formulas): asserts a PDF
  is produced and at least one model call was made (`result.usage["calls"] >= 1`).
- `test_live_book_small`: a real **6-page book** through `BookDispatcher` with
  `chunk_size=2`; asserts all chunks `done`, the merged PDF has 12 pages (6 originals +
  6 translations), cost > 0, and no missing-page error.

### 4. Book pipeline (no network)
`backend/tests/test_books.py` covers the durable dispatcher in depth:
- planner ranges (N/`chunk_size`, `from`/`to`); `create_book` rows + modes.
- scheduler: concurrency cap, **no double-dispatch**, budget pause **+ resume**,
  progress tracking, paused books not dispatched, `start()` orphan recovery.
- per-chunk retry until `max_attempts`, then `failed`; failed chunk doesn't stall.
- rolling glossary accumulation/dedupe/empty; `_collect_pages` copies pages + figures.
- finalize: output naming, cost aggregation, **subset page range only**, missing-page
  warning, keep-original fallback.
- API: create (incl. `translated_only` + `from_page`/`to_page`), chunks, pause/resume,
  retry.

**Live audit** (`backend/tools/` style): run two books over the same document with
*different* `chunk_size`; the second must cost **$0.00000** (fully cache-served).

### 5. Parallel pages (concurrency 1..8)
- `parallel_map` tests: order preserved, actual parallelism ≤ the worker cap, never
  parallel with `workers=1`, exceptions propagate, zero workers falls back.
- `run_ocr` at `concurrency=8` keeps the page order and one result per page.
- **Live**: a 12-page document at `concurrency=1` vs `8` -> identical structure
  (12 pages, 0 empty, correct per-page order) and **~4.6× faster** (21s -> 4.6s).
- **Live**: a book (`chunk_size=6`, page `concurrency=8`, chunk concurrency 2) and a
  batch of 2 docs at `concurrency=8` + `figure_mode=judge` -> all pages present, 0
  empty, figures cropped, batch still sequential.

### 6. Adaptive throttling (no network)
`backend/tests/test_throttle.py` + provider tests:
- a 429 (or 503) cuts the global limit and sets a cooldown; `Retry-After` is parsed and
  respected; the limit never drops below 1; the limit ramps back up after a quiet period;
  `acquire` never exceeds the limit under many threads.
- `OpenAICompatibleProvider` retries a 429 and reports it to the limiter.
- `run_ocr` at `concurrency=8` with a reduced limit (2) stays within it and keeps order.
- **Live**: a 6-page run forced to `limit=2` completed correctly (0 empty) and ramped the
  limit back up to 3 on successes.

### 7. Page fitting (figure rows + splitting)
- `backend/tests/test_layout.py` (pure): length parsing; figure row clustering
  (side-by-side vs stacked, gap/overlap thresholds, top-to-bottom order); sizing
  (relative widths, fit scaling, width/height caps, too-narrow -> split); item
  building (consecutive figures grouped, stack/grid modes, heading keep-with-next);
  height estimation; `pack` (budget, heading not orphaned) and `merge_short_last`;
  `plan_pages` in single/auto modes with measured heights.
- `backend/tests/test_latex_structure.py`: `_figure_row_tex` side by side,
  `build_page_tex` renders a row, `build_pages` splits long content / single mode is
  one part, `run_build` writes every part.
- `backend/tests/test_assemble.py`: a split source page inserts **all** parts
  (translated_only and interleave).
- `backend/tests/test_page_fitting.py` (integration, xelatex): a page with 3
  side-by-side figures + 8 paragraphs -> `single` = 1 page at ~0.86x, `auto` = 2
  pages at ~1.13x (text stays bigger); figure row stays side by side.

### 8. Multi-document batches (no network)
`backend/tests/test_batch.py` covers sequential batches:
- `create_batch`: one ordered child per document, page counts per doc, unique
  `NN <name> (<target>)` output names, parent kind/status/total.
- dispatcher: strict document order, **max one child running at a time**, progress and
  cost aggregation, partial-failure (`done` + error listing failed docs), all-failed
  (`failed`), `recover()` re-queues running children, `cancel()` cancels the rest.
- guards: children of a batch do **not** count against session limits (the parent does).
- API: `POST /api/batch` creates ordered children; `.../children`; cancel; empty list 422.

**Live audit**: two documents (3 + 2 pages) via `BatchDispatcher` with the real model ->
`max concurrent children = 1`, both `done`, English output, batch `done`, ~$0.0018.

## Failure injection (must not crash the job)

Drive these through the fake provider / small PDFs and assert graceful handling:

| Injected failure | Expected |
|---|---|
| empty response for a page | recorded as `empty_ocr`, retried once, flagged |
| invalid JSON / prose-wrapped JSON | parsed if possible else flagged |
| provider 429 / 500 | retried with backoff, then recorded per page |
| provider timeout | recorded per page, job continues |
| xelatex error / timeout | page error event, PDF not written, others continue |
| missing translated page at assemble | original page fallback |
| huge scanned page (points in thousands) | `max_px` caps render; `max_scale:0` fills |
| encrypted / password PDF | clear `ConfigError`/error, never hang |
| zero-page PDF | clean error |
| unknown config key | `ConfigError` |
| figure bbox empty/zero-area | skipped, page still builds |

## Security tests (important — LaTeX + uploads are untrusted)

- **No shell escape**: compile a `\write18{...}` / `\input{|"..."}` payload and
  assert it does not execute and does not create the target file.
- **Restricted file I/O**: `\input{/etc/passwd}` is blocked by
  `openin_any=p`/`openout_any=p`.
- **Upload sanitization** (when the API lands): a PDF containing JavaScript /
  launch action / embedded file is stripped before processing.
- **Key hygiene**: assert the API key never appears in logs, events, or saved
  artifacts; `.env` is git-ignored (a test can assert `.gitignore` contains it).
- **Resource limits**: a pathological LaTeX doc hits the timeout, not OOM.

## Language / script matrix (parametrized where cheap)

| Case | What to check |
|---|---|
| Latin → Latin (`Noto Serif`) | baseline, hyphenation |
| Serbian Cyrillic → French | source detection + translation |
| → Simplified Chinese (`Noto Sans CJK SC`, `linebreak_locale=zh`) | line breaking, no `xeCJK` needed |
| → Arabic/Hebrew (`extra_preamble` bidi) | RTL text, math stays LTR |
| math-heavy page | fractions/roots/sub-superscripts survive |

## Property tests (hypothesis, optional)

- `wrap_math` is idempotent and never produces `\[ \[`.
- `esc_text` never alters the content inside `$...$`.
- `parse_json` succeeds on any balanced JSON regardless of surrounding text.

## Regression catalogue (real bugs we already hit)

Each must have a named test:

1. **Display-env nesting** — `align` wrapped in `\[ \]` → "Erroneous nesting".
2. **Escaping outside math only** — `100% & $a_1$`.
3. **Huge scanned pages** — 2480×3508 pt page; assert render ≤ `max_px`.
4. **Fill the page** — `max_scale: 0` makes content fill, not sit tiny/centred.
5. **Sharp figures** — crop from source PDF, not the small OCR render.
6. **Empty model response** — page 13 of the Thales book returned nothing;
   must be flagged and retryable.
7. **CJK missing super/subscripts** — falls back to `^(...)`/`_(...)`.
8. **PDF private-use glyphs** — `\uf8xx` brackets mapped/stripped.
9. **One page per source page** — `standalone`; assert output pages == expected.
10. **`Event` status duplication** — regression for the constructor arg bug
    (recurred in `verify.run_math_check`).
11. **Pricing prefix ordering** — `gpt-4o` must not shadow `gpt-4o-mini`; match the
    longest prefix.
12. **Glossary / do-not-translate** must appear verbatim in the OCR prompt.
13. **Cost tracking** — usage is summed and cost computed per model; unknown model
    costs 0 (never crashes).
14. **BYOK storage** — keys are encrypted at rest, isolated per session, never
    returned (only `has_key` + masked hint), cleared by `DELETE /api/session/key`.
15. **No key, no job** — a session with no stored key and no env key returns `400`,
    not a late failure.
16. **Progress must not lie** — do not jump to 60% at the *start* of an OCR call
    (the bar froze for the whole ~45 s request). Advance on *completed* pages and
    cap `done_pages` at the total.
17. **UTC timestamps** — datetimes must be serialized with a UTC offset, otherwise
    the browser parses naive values as local time (elapsed timer was off by hours).
18. **Figure crops too tight** — use `union(tight, main)` + `figure_pad` (and a
    minimum pad), clamp to the page, and reject degenerate boxes so diagrams aren't
    clipped. `figure_mode=judge` adds an LLM review that can only *expand* boxes.
19. **Re-runs must be cached across jobs** — `cache_dir` is document-level; a second
    job on the same page makes zero model calls.
20. **Annotation batching** — all `\text{...}` groups on a page go in one call.
21. **Verifier must know the selection** — expected page count accounts for
    `pages`/`unprocessed`, and page-size checks compare against the *set* of source
    sizes (sources can mix letter + A4), not just page 1. (These caused false
    `page_count`/`page_size` warnings on real documents.)
25. **Silent build fails** — a figure box that isn't 0..1 (pixels/percent) must be
    normalised or rejected; a failed crop must not emit a broken `\includegraphics`;
    a `done` job with an unbuilt selected page must set `error` (else it looks silent).
24. **Figure detection precision** — prefer the tight box; never union a near-full-page
    main box (it made crops the whole page). Independent detection catches figures the
    main call missed; the judge can only expand boxes, never shrink.
23. **Richer structure renders correctly** — lists stay lists (never one line),
    heading levels, theorem/quote environments, numbered math, figure captions.
    Normalise `$$…$$`/`\(…\)` before escaping and strip markdown `**` and ordered-item
    markers (double numbering).
22. **Orphaned single jobs** — on startup, in-process `queued`/`running` single jobs
    are marked `failed` ("interrupted by server restart"); book chunks resume.

## Broad testing across real documents

`backend/tools/broad_test.py` runs the whole pipeline over a list of documents/pages and
reports block types, figures, missing pages, tokens, cost and wall time:

```bash
export DS_KEY=...
PYTHONPATH=backend python3 backend/tools/broad_test.py \
  --case "~/Downloads/book.pdf:2-3:French:judge" \
  --case "~/Downloads/scan.pdf:1-2:Chinese (Simplified):tight"
```

Good coverage: a text/math textbook, an English academic PDF, a page with lists/tables,
a scanned book with figures, a landscape/handwritten notebook, a form, and a code doc.

## Coverage & CI

- Target: **≥ 85%** line coverage on `ocrtran` (excluding CLI printing).
- CI pipeline: `ruff check` → `ruff format --check` → `pytest -m "not live"` →
  `pytest -m integration` in a container that has TeX Live + fonts.
- Live tests run **nightly / manually**, never on PRs, and fail loudly if spend
  exceeds a cap.

## Manual QA checklist (before a release)

- [ ] One scanned page, one text PDF, one math-heavy page.
- [ ] All four output modes: interleave, grouped, side-by-side, translated-only.
- [ ] Page sizes match the source; content fills the page.
- [ ] Figures crisp; no empty pages.
- [ ] Cancel mid-run leaves no corrupt output.
- [ ] Rotate/remove the test key afterwards.
