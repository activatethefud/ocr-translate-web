# MEMORY.md — session / context log

Rolling log of decisions and work so future sessions (human or agent) can pick up
without re-deriving everything. Newest entries at the top. **No secrets here.**

## Session: fix squares / missing glyphs in translated pages

Squares were XeLaTeX "Missing character" drops. `Noto Serif` lacks math/geometry
symbols; `Noto Sans CJK SC` lacks Cyrillic and Latin-ext. Fixes in `ocrtran/latex.py`:

- `SYMBOL_TO_MATH` / `SUBSCRIPTS` / `SUPERSCRIPTS` maps: `→ ∈ ∩ ⊆ ∪ ⇒ ≤ ∧ ∨ ⊥ − ▶ ■ ♦`
  and `a₀ b⁵` become math (`$\to$`, `\textsubscript{0}`, ...) in **text and math**
  segments, so the math font draws them.
- fallback font: `_fallback_font(cfg)` = `fallback_font` or `Noto Serif` when the main
  font is CJK. `\newfontfamily\glyphfallback{...}` is emitted in the header; letters in
  Greek/Cyrillic/Latin-ext ranges are wrapped in `{\glyphfallback X}` (text) or
  `\text{{\glyphfallback X}}` (math — a bare switch does nothing in math mode).
- threaded `fallback` through `esc_text`/`text_to_tex`/`_block_tex`/`_figure_row_tex`/
  `measure_items`; theorem `name` and figure captions included.
- new `fallback_font` config (auto).

Rebuilt all 15 affected jobs from cached OCR (no model calls): **0 missing glyphs** across
every log (was 163), and artifacts regenerated in place. Tests: `test_symbols.py` (14).
**383 passed** + 2 live.

---

## Session: figure-crop compression (right-size + png/jpeg)

Figures were cropped at a fixed `figure_px=1800` long side regardless of how big they
were placed, and saved as lossless PNG (sometimes with an alpha channel). A 7-page
textbook output was 7.46 MB, with 21pt icons stored at 1378x1801 (~4700 dpi).

Fix:
- `latex._crop_figures` sizes the crop to the **displayed** size:
  `target_px = disp_frac*text_width/72 * figure_dpi * assemble_scale` (clamped to
  [96, figure_px]). `_assemble_scale` = how much the translated page is scaled up on
  the output page (so big source pages still get enough pixels).
- `render.render_figure` returns the written `Path` (or `None`), renders with
  `alpha=False`, and supports `fmt` = png/jpeg/auto (`_photographic` = >1024 quantised
  colours -> JPEG, else PNG). New config: `figure_dpi` (300), `figure_format` (auto),
  `jpeg_quality` (85); `figure_px` stays a hard cap.
- stale crops with the other extension are removed; `books._collect_pages` copies
  `fig_{page}_*.*`.
- UI/schema expose `figure_dpi` / `figure_format`.

Result: the 7.46 MB output rebuilt from the same OCR -> **1.26 MB (-83%)**, 7 pages,
0 empty. Tests: `test_compression.py` (6, +integration) and updated render/figure tests.
**372 passed** + 2 live.

---

## Session: flow figures left-to-right when they fit

Added `figure_layout="flow"` (now the **default**): figures on a page are packed
left-to-right while they fit at their natural width (sum <= 0.95 text widths, <=
max_figures_per_row); they stay stacked only when they would have to shrink. `preserve`
keeps the source rows, `grid` always packs, `stack` is one per row.

Demo: 3 small stacked figures -> `preserve` = 1 page 381pt tall (column); `flow` = 1 page
160pt tall with all three side by side. On the user's `matematika I gimnazije` pages the
figures are one-per-page (or separated by text), so flow correctly leaves them alone.

Tests: +5 in `test_layout.py`. **366 passed** + 2 live.

---

## Session: fix "There's no line here to end" (page-number footer)

Bug: the new `build_page_tex` emitted the page-number footer as `\\par\vspace{...}`
(Python over-escaping), i.e. a stray `\\` line break -> XeLaTeX "There's no line here
to end" -> every page that had a `page_number` block **failed to compile**. In
`translated_only` those pages silently fell back to the original scan (6 of 9 pages in
the user's `matematika I gimnazije (English)-1.pdf` were untranslated images).

Fix: `\\par\vspace{8pt}...` (single backslash). Added a unit test (no `\\par` in the
tex) and an integration test that a page with a page number compiles. Regenerated the
job's 9 pages from its `ocr.json` (no model calls) -> 11 pages, 0 empty.

---

## Session: page fitting (consistent text size + smarter figure layout)

Implemented `PAGE_FITTING.md` (M1-M4). New `ocrtran/layout.py` (pure): figure row
clustering from bboxes (side-by-side figures stay side by side), n-up sizing with
width/height caps, greedy pagination against a measured height budget, and a
short-last-page merge bounded by `min_page_scale`.

- `ocrtran/latex.py`: `_doc_header` split out; `_crop_figures` -> `fig_info`;
  `_figure_row_tex` (side-by-side via minipages); `measure_items` (one `\setbox`
  XeLaTeX pass returns per-item heights + `\textwidth`); `build_pages` returns
  `[(part, tex)]`; `build_tex` kept for compatibility; `run_build` compiles every part.
- Split page naming: `p01.pdf`, `p01-2.pdf`, ... via `paths.page_pdf(part=)` /
  `paths.page_pdfs()`. `assemble` inserts every part (interleave/grouped/
  translated_only; side_by_side puts part 1 beside the original then extra pages).
  `books._collect_pages`/missing check and `verify.missing_pages` use all parts.
- Bug found while building it: figures were double-scaled (`0.25\textwidth` inside a
  `0.25\textwidth` minipage -> 6% wide). Now the image fills its minipage
  (`width=\textwidth`). Also fixed `pack` recomputing the running height before
  switching to carried (keep-together) items.
- Config/UI: `layout_mode` (single|auto), `min_page_scale`, `figure_layout`
  (preserve|grid|stack), `figure_max_width/height`, `max_figures_per_row`,
  `page_fill_min`, `keep_together`; a "Page fitting" section in the SPA.

Verified live (no model): 8 paragraphs + 3 side-by-side figures -> `single` = 1 page
at 0.86x; `auto` = 2 pages at 1.13x. Tests: **359 passed** + 2 live.

---

## Session: adaptive throttling (waits + concurrency reduction on limits)

Added `ocrtran/throttle.py` — a process-wide `THROTTLE` limiter that gates model calls
(`with THROTTLE:` around each page's OCR call). On HTTP **429/503** the provider calls
`note_rate_limited(retry_after)`: the limit is **cut** (`min(limit//2, max(1,inflight//2))`,
never < 1) and a **cooldown** is set (honours `Retry-After`, else 2s); the provider also
sleeps `max(backoff, Retry-After)` before retrying. On success `note_success()` **ramps the
limit back up** one slot per quiet period (5s) / interval (3s), capped at the ceiling.
Shared across pages, book chunks and batches. `MAX_CONCURRENT_CALLS` (default 16) sets the
ceiling at startup; live state in `/api/admin/stats -> throttle`.

Tests: `tests/test_throttle.py` (7), provider 429 test, `run_ocr` respects a reduced limit.
Live: 6-page run forced to limit=2 completed (0 empty) and ramped back to 3. Total:
**329 passed** + 2 live.

---

## Session: testing parallel page processing up to 8

Added tests + live audits for the "Parallel pages" (concurrency) setting, 1..8:
- unit: `parallel_map` stays in order, is actually parallel and never exceeds the cap,
  is serial at workers=1, propagates exceptions; `run_ocr` at concurrency=8 keeps page
  order and one result per page.
- live engine: 12-page doc, concurrency=1 -> 21.1s, concurrency=8 -> 4.6s
  (**4.56×**), both 12 pages / 0 empty / correct per-page order / 12 English pages.
- live book: `chunk_size=6`, page concurrency=8, book_chunk_concurrency=2 -> 2 chunks done,
  12 pages, 0 empty, 6.1s, no error.
- live batch: 2 docs at concurrency=8 + figure_mode=judge -> sequential (max 1 doc),
  each 3 pages / 0 empty / 1 figure cropped, $0.00416.

Tests: **319 passed + 2 live**. No code changes needed (behavior was already correct).

---

## Session: preserve page numbers on translated pages

The vision prompt (schema v2 -> `prompt_version="3"`) now instructs the model: if the
original page shows a page number, emit exactly one `page_number` block with `text` set
to the number as printed (no translation, no digit conversion). `latex.build_tex` pulls
those blocks out of the reading flow and renders them as a small centered **footer** at
the bottom of the translated page; `_block_tex` returns `None` for them. `prompt_version`
bumped 2 -> 3 so caches refresh.

Live check: a page with a "42" footer -> model returned `{"type":"page_number","text":"42"}`,
output PDF contains `42` after the prose. +3 tests.

---

## Session: multi-document sequential batches

Added `app/batch.py` (`create_batch` + `BatchDispatcher`) and UI/API support for
selecting several documents and translating them **one after another**.

- Data model: a `kind="batch"` parent job owns `kind="single"` child jobs
  (`parent_id` = batch id, ordered by `config._batch_index`). No new table.
- Dispatcher: thread + `ThreadPoolExecutor(batch_concurrency)` (default **1** = strictly
  sequential). Runs a child synchronously via new `JobRunner.run_now`. Recovers orphaned
  running children on start; finalizes the batch (progress/cost/status) when all children
  are terminal; partial failure -> `done` + error, all failed -> `failed`.
- Guards: batch **children are excluded** from `session_jobs` (the parent counts as the
  one active job). `_recover_orphaned_jobs` skips children (the batch dispatcher handles
  them).
- API: `POST /api/batch` (`BatchCreate(JobCreate){document_ids}`),
  `GET /api/jobs/{id}/children`, cancel handles `kind="batch"`.
- UI: "queue multiple documents" checkbox + document checklist; batch progress table.
- Output names: `NN <stem> (<target>).pdf` per child (unique, ordered).

Tests: `tests/test_batch.py` (9) + 2 API tests. Live audit: 2 docs (3+2 pages) ->
max 1 concurrent child, both done in English, batch done, ~$0.0018. Total: **313 passed**
(+2 live).

---

## Session: thorough book-pipeline testing (+ fixes)

Added ~14 focused tests in `tests/test_books.py` (scheduler concurrency cap, no
double-dispatch, budget pause+resume, retry-until-max, progress, start() orphan recovery,
glossary accumulate/dedupe, `_collect_pages`, finalize naming/cost/subset range) plus an
API test for `translated_only` + `from_page`/`to_page` and an opt-in `test_live_book_small`.

Real bugs fixed while testing:
1. **Book provider didn't disable reasoning** -> `_default_provider` now passes
   `reasoning_effort="none"` (+ timeout); dense book pages no longer return empty JSON.
2. **Subset books merged the whole document** and reported the entire rest as missing ->
   `_finalize`/`_final_config` now use the chunks' page span.
3. **Rolling glossary busted the OCR cache**: it was merged into the user glossary (part
   of the cache key), so every chunk had a new key and re-runs re-OCR'd everything. New
   `config.auto_glossary` is used in the **prompt only**, not the cache key; the dispatcher
   also skips the glossary pass on fully-cached chunks.

Live audit (`/tmp/live_book_audit.py`): Book A (6 pages, chunk_size=2) -> 3/3 chunks done,
12-page merged PDF, 0 empty, English translations + Serbian originals, $0.00333, 18
auto-glossary terms. Book B (same doc, chunk_size=3) -> **$0.00000**, 12 pages, 0 empty.
Tests: **302 passed + 2 live**.

---

## Session: "translated only" as a first-class combine option

`combine` now accepts **`translated_only`** directly (previously only reachable by
unchecking the bilingual box). `OUTPUT_MODES = COMBINE_MODES + ("translated_only",)`;
`output_mode` returns `translated_only` when `not bilingual OR combine == "translated_only"`.

- backend: `config.OUTPUT_MODES`, `output_mode`, `from_dict("mode")` mapping, schema Literal
- frontend: `Combine` type + an always-visible **Combine** select with the 4 options; the
  bilingual checkbox and the select stay in sync (translated_only <-> bilingual off)

Tests: **287 passed + 1 live**. Verified pages 1/2/5: translated_only -> 3 pages,
grouped -> 6 pages.

---

## Session: target-language change served stale cache (fixed)

Repro: same doc (`OMM Skripta sredjeno.pdf`, pages 60-65), translated to **Filipino**, then
re-run to **English** -> the "English" PDF was still Filipino (only captions changed).
The English job cost **$0.0002** (basically free) = it was served from cache.

Cause: the shared per-document OCR cache stores **already-translated** blocks, but the key
was `doc_sha + page + model + prompt_version` (+ glossary/instructions) -- **no language**.
So changing the target language reused the old-language translation.

Fix: `cache.prompt_sig` now also includes **source_lang, target_lang, figure_mode** (the two
languages and figure mode all change the cached text). `ocr.run_ocr` passes them.
Old unscoped cache entries simply become dead keys (no wrong output).

Verified: reran pages 60-65 to English -> regenerated
`OMM Skripta sredjeno (English, fixed).pdf` has **0 Filipino markers**, correct English.
Tests: **284 passed + 1 live**.

---

## Session: diagnosis — blank pages on a Cyrillic curriculum PDF (fixed)

Repro: `Програм-наставе-и-учења-за-1.-разред-ОШ.pdf` -> English, pages 1,2,5. Output had
2 blank pages.

Root causes (all fixed):
1. **`deepseek-flash` is a reasoning model**: on dense pages it spent the *entire*
   `max_tokens` on hidden reasoning (`finish_reason=length`, `reasoning_tokens=16000`,
   `content=""`), so no JSON. Fix: `reasoning_effort="none"` (default) disables thinking
   (~4.8k tokens, valid JSON); `max_tokens` -> 32000; provider retries a `length`+empty
   response with a bigger budget.
2. **Empty OCR** (valid JSON, `blocks: []`) was accepted -> a 716-byte empty PDF -> blank
   page. Fix: retry once, flag with `error`; `run_build` now skips block-less pages so the
   **original is kept** and the page is reported missing (no silent blank page).
3. **Table with more `&` than the `array` spec** -> "Extra alignment tab" -> no output.
   Fix: `latex.fix_table_spec` pads the column spec to the columns actually used.
4. **Cyrillic letters inside math** -> "Missing character ... cmmi10". Fix:
   `latex.textify_math` wraps non-Latin runs in `\text{...}` (main font covers them).

Verified: reran pages 1,2,5 -> 3 translated pages built, output **6 pages, 0 empty,
issues []**. Tests: **282 passed + 1 live**.

---

## Session: R1/R2 abuse protection, P3 book features, robustness (5)

**Abuse (R1/R2):** `app/ratelimit.py` weighted token bucket (job=20, book=50, upload=10)
as a pure-ASGI middleware (SSE-safe) + per-client **SSE connection slots**; 429 with
`Retry-After`; audit log. `GET /api/admin/stats` (X-Admin-Token). `deploy/nginx.conf`
(native limits + streaming) and `deploy/Caddyfile`; docker-compose gains a **proxy**
service and `APP_MODE=public`/`TRUST_PROXY`/`ADMIN_TOKEN` defaults. Defaults:
240 units/min, burst 200 (weights are large; the first 40 was too low and 429'd valid
uploads+jobs).

**P3 book features:** `render.page_ink_ratio`/`is_blank`; `skip_blank_pages` (default,
threshold 0.0003) - blank pages are not OCR'd/built and the original is kept;
chapter **boundaries** (`cfg.boundaries` + `BookCreate.boundaries`) -> a PDF outline/TOC
mapping source pages to the combined output.

**Robustness (5):** `verify.page_formulas` now also checks **inline** `$...$` formulas
(not just display math); `render_figure` **auto-expands** a crop when ink touches the
border (up to 4 tries); `estimate` gains an ETA (`est_seconds`, shown in the UI);
`backend/tools/broad_test.py` is a committed multi-document harness.

Tests: **276 passed + 1 live**.

---

## Session: default unprocessed = skip

- Changed the default for unselected pages from `original` to **`skip`** (engine
  `PipelineConfig`, `JobCreate`, UI). So a page selection like `10,15,20` now yields
  exactly the selected pages (3 originals + 3 translations), not the whole document.
  `unprocessed=original` still available to keep the rest.
- All 256 tests still pass (selection tests set the mode explicitly).

---

## Session: broad multi-document testing

Ran the full pipeline on 8 diverse documents from `~/Downloads`, 2 selected pages each,
mixed targets (French/Chinese), `figure_mode` judge/tight. All succeeded, no missing
pages, figures/lists/tables/theorems detected; total ~$0.20.

| doc | type | pages | out | figures |
|---|---|---|---|---|
| dragan_djokic_linearna_algebra | text math (343p) | 2-3 | 345 | 0 |
| Combinatorics (English) | text (357p) | 5-6 | 359 | 0 |
| 2.KOMBINATORIKA | text, lists/math/table | 1-2 | 11 | 0 |
| Preseci prizme i piramide | scan, geometry figures | 5-6 | 24 | 2 |
| drugo polugodiste sveska | landscape handwriting scan | 1-2 | 23 | 0 |
| 2026_ZI_Matematika_Test | form/tables/figures | 1-2 | 14 | 3 |
| 2023-skripta-OMM | 86p scan, tiny page (304x335) | 1-2 | 88 | 1 |
| camelot-py docs (English) | technical/code | 1-2 | 71 | 0 |

Notes:
- Default `unprocessed=original` keeps every unselected page, so translating 2 pages of a
  343-page book yields a 345-page output; use `unprocessed=skip` for a 2-page-only result.
- Most "\{" seen in outputs were from **untouched original pages** (set-difference
  notation), not artifacts. Still hardened `esc_text` to un-escape `\{`/`\}` and strip
  control chars from model prose.

---

## Session: diagnosis — "5 pages instead of 6" was a silent fail (fixed)

- Repro: `OMM Skripta sredjeno.pdf`, `pages=10,15,20`, `unprocessed=skip`,
  `interleave`, -> Chinese. Expected 6, got 5, status `done`, error None.
- Cause: page 10 had a figure whose bbox came back as **pixels** `[50,180,950,720]`
  (not 0..1), so the crop was rejected; `p10.pdf` was never built; the assembler
  skipped the missing translation -> 5 pages. The **verify report flagged
  `page_count 6->5`** and a `build/error` event existed, but the job stayed `done`
  with no error: a silent fail.
- Fixes:
  1. `figures.sanitize_box()` + `sanitize_result()` normalize pixel/percent boxes
     (using the rendered image size), clamp, reject degenerate; applied to fresh AND
     **cached** results in `run_ocr`.
  2. `latex.build_tex` only emits `\includegraphics` if the crop file exists; else it
     keeps the figure **caption** so nothing is silently lost.
  3. `verify.missing_pages()` + `runner`: a `done` job now sets `error` to
     "pages not typeset: ..." when a selected page produced no PDF (and the report
     lists them), so the UI surfaces it.
  4. Bonus: figure **captions are translated** in the annotate pass.
- Verified: re-running page 10 (from cache, $0) now sanitizes the pixel box
  (`[0.057,0.1,1.0,0.4]`), writes `fig_10_0.png`, builds `p10.pdf` and renders the
  Chinese translation + caption.

---

## Session: precise figure detection (grid + independent detect + LLM judge)

- **`ocrtran/figures.py`** reworked:
  - `grid_variant()` draws a labeled 10% coordinate grid on the page before bbox/detect
    calls -> the model localises far more precisely.
  - `detect_figures()` = an **independent detection pass** (finds figures even when the
    main OCR call found none, and figures without captions).
  - `boxes_variant()` draws the candidate boxes (numbered) and `refine_figures()` is an
    LLM **judge** that sees them: corrects boxes, sets `keep=false` for false positives,
    and can add missed figures.
  - `refine_page()` merges candidate + detections (IoU) + judge, **monotonic** (boxes
    only grow). `iou()`, `choose_box()`.
- **Bug found on the real doc** (`Preseci prizme i piramide`, 22-page scan): the main
  call's figure box was sometimes the **whole page**, and unioning it with the tight box
  made every crop the full page (all 1273x1800). `geometry.choose_box()` now prefers the
  tight box and ignores a near-full-page main box.
- **Verified on pages 1-2 of that scan**: `tight` found 2 figures and gave real sub-crops;
  `judge` found **3** (it caught a page-2 figure `tight` missed) and expanded boxes to
  include labels/captions.
- App default `figure_mode` is now **judge** (best figures); `off`/`tight` remain for
  speed. Judge adds a detect pass per page and a judge pass for figure pages.
- Tests: **+18** (iou, grid/boxes variants, detect/refine parse, refine_page merge /
  keep=false / union, choose_box).

---

## Session: richer vision schema for faithful recreation

- **OCR prompt v2** (`prompt_version` default now "2", so old caches are not reused):
  blocks can be `heading` (with `level`), `prose` (paragraph breaks via blank lines),
  `list` (`ordered` + `items[]` — each item its own entry), `math` (optional `number`),
  `table`, `figure` (bbox + `caption`), `quote`, and `theorem` (`kind` + `name`).
  Explicitly: "use list for every list; never merge items into one line".
- **Renderer** (`ocrtran/latex.py`, `_block_tex` + `text_to_tex`):
  headings -> subsection/subsubsection/paragraph by level; lists -> itemize/enumerate;
  quotes -> quote env; theorems -> **kind (name).** in a quote; numbered math appends
  `\text{(n)}`; figures get captions. `text_to_tex` splits paragraphs on blank lines.
- **Robustness fixes found by a real run**: collapse `$$…$$` / `\(…\)` / `\[…\]`
  to `$…$` before escaping (they were being escaped, mangling formulas); strip markdown
  `**`; strip leading "1)" / "ii." from ordered items (double numbering).
- Review UI shows list items as a real list.
- Tests: structure (+19 launcher/rendering) — see count above.

---

## Session: public mode (BYOK-only) + self-learning cost model

- **Public mode**: `APP_MODE=public` (or `team`) → the server never falls back to
  its own key; **BYOK is mandatory** (400 "BYOK required" otherwise). The key can be
  remembered **client-side** for the session (`sessionStorage`), and is still
  optionally stored server-side encrypted. `server_key_allowed` is true only in
  `local`.
- **Learning the cost**: `app/learning.py` aggregates the existing `usage` table into
  per-model **learned** `input/output tokens per call` (one query per 30 s, cached).
  `estimate.predict_cost(learned=...)` blends static heuristics with learned averages
  via smoothing (`w = calls/(calls+15)`) so predictions converge with use without
  overfitting small samples. `GET /api/learning` exposes it; the estimate shows
  "tuned from N past calls".
- Frontend: API key prefill/save in `sessionStorage`; estimate shows learned info.
- Tests: **210 passed + 1 live** (+15: learning aggregate/blend/smoothing, public
  BYOK for jobs and books).

---

## Session: cost predictor from all job inputs

- New `ocrtran/estimate.py`:
  - `analyze_document()` samples the real PDF: dominant page size, average
    chars/page, and fraction of pages that look like **formulas** (math symbols) or
    **figures** (images / many vector ops). Cached by (path, mtime).
  - `predict_cost()` models every cost driver: image tokens (dpi/max_px), OCR
    output tokens (from chars/page + target script), figure passes
    (`off|tight|judge`), formula check (`verify_math`), annotation calls, glossary /
    do-not-translate / instruction overhead, rolling-glossary calls (book), and a
    retry factor. Returns a `breakdown`, `assumptions`, and a low/high range.
  - **Calibrated from real usage**: the model emits ~4-9k completion tokens/call
    (it returns source + translation + LaTeX as JSON), so output is estimated as
    `(3000 + chars/page*4) * (1.3 for CJK)`; input ~1500/call. On the modifikovani
    doc: predicted **$0.148** vs actual **$0.127** (range $0.089-$0.252).
- API: `GET /api/documents/{id}/estimate` now takes model, pages, figure_mode,
  target_lang, glossary_terms, extra_chars, dpi, max_px, chunk_size, verify_math;
  new `GET /api/pricing`.
- UI: estimate shows the range + a per-bucket cost breakdown.
- Tests: **+14** (207 passed + 1 live).

---

## Session: book resume correctness fixes

- **Failed chunk no longer stalls the book**: `tick` finalizes when all chunks are
  terminal (`done`/`failed`), not only when all are `done`. Missing pages are kept
  as originals with a warning (as decided).
- **Assembler duplicate fix**: a missing translation no longer inserts the original
  *again* in `interleave`/`grouped` (it duplicated the page). It only falls back to
  the original in `translated_only`.
- **Cache respects custom prompt content**: `cache.prompt_sig()` folds glossary /
  do-not-translate / instructions into the cache key, so editing them is never
  served from a stale cache (default runs keep their existing cache).
- Tests: **179 passed + 1 live** (+4).

---

## Session: diagnosis — Chinese translation "issues" were verifier false alarms

- Repro: `~/Downloads/modifikovani-model-nadmetanja-dve-vrste.pdf` (17 pages, 16
  letter + 1 A4, figures on pp. 14-15) -> Chinese. The report showed
  `page_count expected 34 got 22` and a `page_size` mismatch.
- **Not a translation bug.** The job used a page *selection* (`1-5`) with
  `unprocessed=original` (-> 5 tx + 12 originals = 22 pages), but `verify_output`
  assumed all pages and compared every page to page 1 (source mixes letter + A4).
- **Fix** (`ocrtran/verify.py`): expected pages account for `pages`/`unprocessed`;
  page-size check uses the set of source sizes (+ configured output size); empty-page
  check uses computed translation positions.
- Also: **orphaned single-job recovery** on startup (mark queued/running single jobs
  failed; book chunks already recover).
- **Re-ran full doc**: 17 pages -> 34-page interleaved Chinese PDF, report issues
  `[]`, $0.1266 (1-5 from cache), 144 s.
- Note: the CJK font embeds under the TTC's shared name `NotoSansCJKjp-*`, but the
  **SC glyphs are used** (verified SC vs JP render differently; `fc-match` resolves
  the SC face). Cosmetic only.
- Tests: **175 passed + 1 live** (+3 verify selection/mixed-size).

---

## Session: L2 durable book translation (implemented)

- **DB**: `chunks` table + `jobs.kind`/`jobs.parent_id`; additive migration
  `db.ensure_columns` (ALTER TABLE) so existing SQLite DBs upgrade in place.
- **`app/books.py`**: `plan_chunks` (25-page default), `create_book`,
  `BookDispatcher` (durable): pulls queued chunks from SQLite, dispatches with
  `BOOK_CHUNK_CONCURRENCY`, **pauses when spend >= MAX_BOOK_USD**, resets orphaned
  `running` chunks on startup (`recover`), retries failed chunks up to `max_attempts`,
  page-level **final merge** (one assemble over the whole doc), and **keeps the
  original page + records a warning** for missing translations.
- **Rolling glossary** (`ROLLING_GLOSSARY=1`): after each chunk, a cheap text call
  extracts term pairs, merged into the book glossary for later chunks.
- **API**: `POST /api/documents/{id}/book`, `GET /api/jobs/{id}/chunks`,
  `POST .../pause|resume|cancel`, `POST .../chunks/{cid}/retry`; book events stream
  over the existing SSE endpoint. UI: **book mode** toggle + chunk size + chunk table
  with pause/resume/retry.
- **Verified live**: 3-page doc, chunk_size=2 -> chunks (1-2, 3-3) done, merged to a
  6-page interleaved FR PDF named `book3 (French).pdf`, $0.0476.
- Decisions: L2, chunk 25, pause on budget, chapters deferred, rolling glossary on,
  keep-original-with-warning. Tests: **172 passed + 1 live** (+16).
- Repo published: https://github.com/activatethefud/ocr-translate-web (private, via gh).

---

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
