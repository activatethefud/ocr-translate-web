# Handling big jobs (whole books)

Status: **core implemented**. Replaces the earlier `JOB_SPLITTING.md` sketch.

Implemented (L2 durable): chunk planner, durable `chunks` table + orphan recovery,
a background dispatcher with chunk concurrency, **pause on budget**, pause/resume/
cancel/retry, page-level merge, keep-original-with-warning for missing pages, and an
opt-in **rolling glossary**. `POST /api/documents/{id}/book`, `.../chunks`,
`.../pause|resume|cancel`, `.../chunks/{cid}/retry`. Remaining: chapter bookmarks,
auto chapter detection, multi-worker.

## 1. What "big" means

| | today | big |
|---|---|---|
| pages | 1–30 | 200–1000+ |
| wall time | seconds–minutes | hours |
| model calls | ~pages | ~pages (+figure passes) |
| cost | cents | $2–$20 |
| failure exposure | one job | must not lose hours of work |

Today one job = one in-process `ThreadPoolExecutor` task. That's fine for a
handout; for a book it's not: a **server restart loses a running job**, there's no
pause, and one rate-limit spike can stall everything.

## 2. Requirements

**Must**
- Bounded, resumable work: a restart or crash never forces re-doing finished pages.
- Cache-first: any page already translated is reused (document-level cache exists).
- One deterministic output PDF with correct page order and page numbering.
- Global throttling (provider rate limits) + per-run concurrency.
- Cost estimate before start, hard budget cap, live cost/progress.
- Pause / resume / cancel; partial download at any point.
- Terminology consistency across the book (user glossary + optional rolling glossary).

**Should**
- Chapter-aware splitting and PDF bookmarks.
- Automatic detection/skip of blank pages.
- Per-chunk retry with backoff; a failed chunk doesn't fail the book.

**Won't (yet)**: layout-preserving reflow, multi-machine workers.

## 3. Architecture

Three layers:

```
Book job  ──▶ Chunk (contiguous page range) ──▶ page pipeline (render→ocr→build)
   │                                              (already exists)
   └── scheduler: global concurrency + budget + retry + pause/resume
```

Two viable durability levels:

- **L1 — in-process (simple).** A dispatcher thread inside the API process.
  Fast to build; still not survivable across a restart.
- **L2 — durable dispatcher (recommended).** Job/chunk state lives in SQLite; a
  dispatcher loop pulls *queued* chunks from the DB and marks them running. On
  startup, any `running` row is reset to `queued` (orphan recovery) and work
  continues from the cache. This is what makes "hours-long" safe.

Recommendation: build **L1 first** (reuse the current runner), but design the
schema/states so **L2 is a drop-in upgrade** (same rows, dispatcher becomes a loop).

## 4. Data model (additions)

```
jobs.kind        'single' | 'book'
jobs.parent_id   null | book job id          -- chunk jobs point at the book
jobs.state       queued|running|paused|interrupted|done|failed|canceled

chunks(
  id, book_job_id, idx,
  page_from, page_to,                        -- inclusive, 1-based
  state, attempts, max_attempts,
  cost_usd, error, job_id,                   -- job_id = child job
  created_at, started_at, finished_at
)
book_events(id, book_job_id, seq, kind, data, created_at)   -- aggregated stream
```

Migrations: SQLite `create_all` adds new tables; **adding columns to `jobs` needs a
tiny migration** (`ALTER TABLE jobs ADD COLUMN ...`) — implement a small
`ensure_columns()` at startup. (Rows are additive/nullable, so it's safe.)

## 5. Scheduler

One dispatcher (thread or asyncio task) per process:

```
loop:
  if paused/canceled: wait
  budget_left = MAX_BOOK_USD - spend
  for each book with free slots and budget_left:
      take next 'queued' chunk (ordered by idx)
      mark running, submit to the executor as a child job (pages="from-to")
  sleep small interval / wait on an event
```

- **Global call limiter**: a semaphore for in-flight *model calls* across all
  chunks (e.g. 8), so N chunks × page-concurrency can't exceed the provider limit.
- **Per-chunk page concurrency** = existing `concurrency`.
- **Rate-limit response**: on HTTP 429/5xx the provider already retries with
  backoff; the scheduler additionally *reduces* the in-flight cap temporarily and
  restores it after a quiet period (adaptive).
- **Pause**: stop dispatching; running chunks finish their current page and stop at
  the next checkpoint. **Resume**: set chunks back to `queued` and continue.
- **Cancel**: cancel child tokens, stop dispatching, keep finished chunks.

## 6. Merge (page-level, once)

The engine already writes a translated PDF per page (`p-NN.pdf`). Build the final
document **once, in page order**, per `combine`:

- `interleave`: p1 original, p1 translation, p2 original, …
- `grouped`: originals then translations
- `side_by_side` / `translated_only`: as today

Because chunks are contiguous ranges, "merge" is just iterating `1..N` and placing
each page — no chunk-level PDF surgery. `pymupdf.insert_pdf` streams, so memory is
flat. A missing page (failed chunk) is either a hard error (default) or kept as the
original with a warning (configurable). Chapter boundaries add PDF bookmarks/TOC.

## 7. Resources

- **Memory**: never hold more than `page_concurrency` page pixmaps; free after each
  `.save()`. The final merge is streaming. Cap `max_px` for huge scans (exists).
- **Disk**: per chunk keep `ocr.json`, `p-NN.pdf`, artifact. Page PNGs and `.tex`
  can be deleted after a chunk finishes (config `keep_intermediates`). Provide an
  LRU/`--prune` for the cache (keyed by doc sha).
- **DB**: SQLite WAL + a single-writer lock (the runner already serialises writes).
  Fine for a single user; Postgres if multi-user.

## 8. Cost

- **Estimate** up front: `pages × per-page` (+ figure/verify calls) and the chunk
  count; show it before starting.
- **Budget cap** `MAX_BOOK_USD`: when exceeded, stop dispatching (chunks finish or
  pause) and expose partial download + "continue anyway".
- **Accounting** per chunk and aggregated on the book (`sum(usage)`), shown live.
- Cache hits cost $0 — the estimate should note expected savings on re-runs.

## 9. API & UI

```
POST /api/documents/{id}/book     {config, chunk_size, from, to, boundaries[]}
GET  /api/jobs/{id}               book: state, progress, cost, chunks[]
GET  /api/jobs/{id}/chunks
POST /api/jobs/{id}/pause|resume|cancel
POST /api/jobs/{id}/chunks/{cid}/retry
GET  /api/jobs/{id}/artifacts      merged + partial
GET  /api/jobs/{id}/estimate?chunk_size=
```

UI: a book progress view — headline progress/cost/ETA, a **chunk table** (range,
state, cost, retry), pause/resume/cancel, and **Download partial**. Chapter
boundaries editable before start.

## 10. Testing

- **Planner**: ranges for N/chunk_size, `from`/`to`, chapter alignment.
- **Scheduler**: concurrency cap, global call limiter, budget stop, pause/resume,
  cancel, no double-dispatch.
- **Recovery**: a `running` chunk becomes `queued` on startup; resume makes **0**
  model calls for finished pages (shared cache).
- **Merge**: page order/count for all four modes across chunk boundaries; missing
  page handling.
- **Failure injection**: 429 storms (adaptive backoff), xelatex failure in one
  chunk, provider timeout.
- **Live**: small book (10 pages) with `chunk_size=3`, assert merged page count and
  0 re-calls on resume (opt-in).

## 11. Phased roadmap

| Phase | Deliverable | Effort |
|---|---|---|
| **P0** | `chunk_size` planner + child chunk jobs + page-level merge + combined progress (L1) | 2–3 days |
| **P1** | pause/resume/cancel, per-chunk retry, partial download, chunk table UI | 2–3 days |
| **P2** | durable dispatcher (L2) + orphan recovery + budget cap + adaptive throttling | 2–4 days |
| **P3** | chapter boundaries + bookmarks, blank-page skip, rolling glossary | 3–5 days |
| **P4** | multi-worker / Postgres (only if needed) | later |

## 12. Decisions (resolved)

1. **Durability** — **L2 durable dispatcher** (chosen).
2. **Chunk size** — **25 pages** default (`BOOK_CHUNK_SIZE`).
3. **Budget reached** — **pause for confirmation** (`MAX_BOOK_USD`; resume to continue).
4. **Chapters** — auto-detection **deferred** (later phase).
5. **Rolling glossary** — **on** (`ROLLING_GLOSSARY`, opt-out).
6. **Missing page** — **keep the original + warning** (job error lists the pages).
7. Chunk concurrency — `BOOK_CHUNK_CONCURRENCY` (default 2).

## 13. Implementation notes (found by thorough testing)

- **Rolling glossary is not part of the OCR cache key.** Cached blocks hold *translated*
  text, so the cache signature includes the languages, figure mode and the **user**
  glossary/instructions — but **not** the auto-accumulated glossary (stored separately in
  `config.auto_glossary`). Otherwise the key changes every chunk and every re-run re-OCRs
  everything. Verified live: a second book over the same document (different chunk size)
  costs **$0.00000**.
- **Rolling glossary is only mined from chunks that translated new pages.** On a fully
  cache-served chunk the dispatcher makes **0** model calls (no glossary pass), so
  resume/re-run is genuinely free.
- **Finalize respects the book's page span.** A book created with `from_page`/`to_page`
  merges and reports missing pages only within that range (previously it used every page
  and warned about the whole rest of the document).
- **Book provider disables a reasoning model's hidden thinking**
  (`reasoning_effort="none"`) exactly like the engine, so dense pages return JSON instead
  of burning the token budget.
- Chunk retry: `attempts` is bumped on dispatch; a failed chunk is re-queued until
  `max_attempts` (3), then `failed`; a permanently failed chunk never stalls the book
  (finalize runs once all chunks are terminal).
