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
- One 1-page generated fixture (simple prose + one formula + one figure box).
- Asserts: blocks non-empty, at least one `math` block, output PDF built, no
  empty page. Optionally snapshot-compare block *types* (not exact text).
- Optional "provider capability" test: `/models` lists a model whose
  `input_modalities` include `image`.

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
10. **`Event` status duplication** — regression for the constructor arg bug.

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
