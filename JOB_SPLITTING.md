# Job splitting — translating whole books

Status: **plan** (not implemented). The output-name field is done; this documents
how to make large documents (a whole book, 300–600 pages) practical.

## Why split at all

A single job for a book is bad for five reasons:

1. **Wall-clock**: pages are parallelised, but one 500-page job still runs for hours
   before you get any output.
2. **No incremental result**: you can't start reading a chapter while the rest runs.
3. **Blast radius**: one bad page (or a rate-limit) shouldn't force a full re-run.
4. **Cost lump**: you commit $5+ in one shot instead of per chunk.
5. **Rate limits**: providers cap concurrent requests; a book must be throttled.

What we already have (leverage it):
- per-page OCR + per-page translated PDFs (`p-NN.pdf`);
- **document-level shared OCR cache** → any page already done is free to reuse;
- parallel page OCR (`concurrency`) and page selection (`pages`);
- output modes: `interleave | grouped | side_by_side | translated_only`.

## Goals / non-goals

**Goals**: bounded chunks, resumable, cache-friendly, deterministic single output,
configurable chunk size + concurrency + budget, pause/resume/cancel, partial download,
cross-chunk terminology consistency.

**Non-goals** (later): layout-preserving reflow, multi-machine workers.

## Design A — manual chunking (works today)

Run the same document with `pages="1-50"`, `"51-100"`, … and merge the PDFs. Because
the cache is document-level, re-runs are free. Merge with `pymupdf`/`pikepdf`.

- Pros: zero new code.
- Cons: manual, no single page numbering/outline, no shared glossary, no combined progress.

## Design B — a “book” job with internal chunks (recommended)

### Data model
- `jobs.kind` = `single | book`, `jobs.parent_job_id` (nullable).
- `chunks(id, book_job_id, page_from, page_to, status, attempts, cost_usd, artifact_id, error)`.
- Child `jobs` reuse everything we have (`pages="from-to"`).

### Flow
1. **Create book job**: source, config, `chunk_size` (default 25), `concurrency`,
   optional `from`/`to`, optional chapter boundaries.
2. **Plan**: split `[from..to]` into contiguous chunks. Optionally align to chapter
   boundaries (detect headings, or user-provided).
3. **Schedule**: run up to `K` chunks at once (global cap), each as a child job with
   `pages="a-b"`. A global model-call semaphore enforces provider limits.
4. **Merge at the page level** (not chunk level): the engine already writes
   `p-NN.pdf` per page; the final document is built once by placing pages in order
   per the chosen `combine` mode. This makes order trivial and avoids re-merging.
5. **Progress** = Σ(pages done)/N; **cost** = Σ(chunk cost).
6. **Resume**: skip chunks whose pages exist and whose cache is warm; retry failures.
7. **Cancel**: propagate cancel tokens to running chunks.

### Money & limits
- Pre-run estimate (pages × per-page) + chunk count; require confirmation above a threshold.
- `MAX_BOOK_USD` budget: stop scheduling new chunks when projected cost exceeds it;
  keep completed chunks and offer partial download.

### Terminology consistency across a book
- **User glossary** (already supported) is the simplest lever.
- **Rolling glossary**: after each chunk, extract frequent source terms (cheap text
  pass or the OCR model’s headings) and merge them into the job glossary seeded into
  later chunks’ prompts. Cache key must include a **glossary hash**.
- Optional **style memory**: pass the previous chunk’s last translated paragraph as
  light context (careful with tokens).

### Failure handling
- Per-chunk retry/backoff; a failed chunk records an error without failing the book.
- Partial outputs downloadable at any time.
- Idempotent chunks via cache key `doc_sha + page + model + prompt_version (+ glossary hash)`.

### API sketch
```
POST /api/documents/{id}/book        {config, chunk_size, from, to}
GET  /api/jobs/{id}/chunks           per-chunk status/cost/error
POST /api/jobs/{id}/chunks/{cid}/retry
GET  /api/jobs/{id}/artifacts        merged + partial
GET  /api/jobs/{id}/estimate?chunk_size=25
```

### UI
- “Split into chunks” toggle + chunk size; a chunk progress table; per-chunk retry;
  “download partial”; headline progress/cost. Page control (already present) drives
  manual ranges too.

## Milestones
- **MS1 (core)**: `chunk_size` + planner + child jobs + scheduler (concurrency) +
  page-level merge + combined progress.
- **MS2**: budget cap, resume/skip, per-chunk retry, partial download.
- **MS3**: rolling glossary / term extraction; chapter detection + PDF bookmarks.
- **MS4**: distributed workers, per-chunk priority, incremental “add chapters later”.

## Edge cases
- Mixed page sizes/orientations — our per-page assembly already preserves the source
  page size when `output_page_size=match`.
- **Blank/near-empty pages** — auto-detect and keep the original (skip translation).
- Encrypted/huge single pages — existing caps (`max_pages`, `max_px`, timeouts).
- Duplicate pages — cache handles repeats.
- Chapter that starts mid-chunk — chunk planner can honour supplied boundaries.

## Testing (when implemented)
- **Planner**: ranges for N/chunk_size, `from`/`to`, chapter-aligned boundaries.
- **Scheduler**: concurrency cap, cancel, retry, budget stop, no double-dispatch.
- **Merge**: page order and counts for all four modes across chunk boundaries.
- **Resume**: second run makes **0** model calls (shared cache).
- **Integration**: 5-page doc, `chunk_size=2` → assert final page count/order; resume → 0 calls.

## Open questions
1. Default `chunk_size` — 25 or 50?
2. Chapter detection vs user-provided boundaries?
3. Rolling glossary — worth the extra cheap calls for consistency?
4. Merge at the page level (recommended) or chunk level?
5. Do we expose child chunk jobs in the UI, or keep them internal?
