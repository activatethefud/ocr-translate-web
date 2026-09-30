# Page fitting: consistent text size + smarter figure layout

Status: **implemented** (M1–M4). See `ocrtran/layout.py`, `ocrtran/latex.py`
(`build_pages`, `measure_items`, `_figure_row_tex`), `ocrtran/assemble.py`
(`paths.page_pdfs`), and the `layout_mode` config / UI. Milestone 5 (native flow,
wrapfig) remains future work.

## 1. Problem

Today the translated side of a page is typeset as **one** standalone LaTeX page and
then uniformly scaled to fit the output page area:

- `ocrtran/latex.py::_block_tex` renders every figure centered, full-ish width:
  `width = min(0.92, max(0.30, frac * 1.25))` — the original *arrangement* of the
  figures (a row of two, a 2×2 grid, …) is thrown away and they are stacked.
- `ocrtran/latex.py::build_tex` emits one `standalone` document per source page.
- `ocrtran/assemble.py::_place` then computes `scale = min(area.w / pdf.w, area.h / pdf.h)`.
  A tall translated page (text + several stacked figures) is **shrunk** until it fits.

Result: pages with 2–4 figures render at, say, 55–75 % of normal size, so body text
is visibly smaller and the document looks inconsistent page to page. The shrink is
applied to *everything* (text and figures), not just the figures — which is the wrong
lever.

## 2. Goals / non-goals

**Goals**
1. Keep body text at a **consistent, readable size** across translated pages (target
   `scale ≈ 1.0`, never below `min_page_scale`, default 0.90).
2. When content still doesn't fit, **flow it onto additional output page(s)** rather
   than shrinking.
3. Lay multiple figures out **intelligently** (reproduce the original relative
   arrangement where possible; otherwise n-up packing) so more content fits at full
   size.
4. Deterministic, local-only (no extra model calls), cache-friendly.

**Non-goals**
- Reflowing *reconstructed* original pages pixel-faithfully (that's "layout preserve",
  a bigger, separate feature).
- Text wrapping around figures (`wrapfig`) — deferred (fragile with page breaks).
- Changing OCR/translation output or the block schema.

## 3. Current pipeline (where this plugs in)

```
render → ocr → annotate → build (per page: standalone PDF) → assemble (scale-to-fit)
                                 ^^^^^^                        ^^^^^^^^^
                          latex.build_tex/run_build        assemble._place
```

Everything we need is already present: figure `bbox` fractions and `tight` boxes in
`ocr.json`, cropped PNGs from `render.render_figure`, and the block list.

## 4. Proposed architecture

A new **layout planner** between `build` and `assemble`, with one measurement pass:

```
per source page:
  blocks ──▶ (1) MEASURE   each block/figure-row height via one LaTeX \setbox pass
        ──▶ (2) PLAN       figure rows + page split into k output pages
        ──▶ (3) TYPESET     compile each of the k pages (fixed geometry, scale 1)
        ──▶ (4) ASSEMBLE    source page maps to k translated pages
```

- **(1) MEASURE** — build `measure.tex` that `\setbox`s each block (and each candidate
  figure row) and `\typeout`s `BLOCKH <i> <dimen>` / `BLOCKW <i> <dimen>`. One compile
  per source page yields real heights/widths for all blocks. Cached per
  `(page, block-hash)`.
- **(2) PLAN** — cluster figures into rows (from their bboxes), size them, then greedily
  pack blocks into pages against a height budget.
- **(3) TYPESET** — compile each planned page as a standalone PDF (unchanged mechanism),
  so the only new thing is *how many* pages and *what goes on each*.
- **(4) ASSEMBLE** — `assemble` inserts all k translated pages for a source page.

This keeps the existing per-page review/rebuild model intact: "rebuild page N" re-runs
planner → typeset for that source page.

## 5. Heuristics

### 5.1 Figure rows — reproduce the original arrangement

Input: figures with bbox fractions `(x0,y0,x1,y1)` (already sanitized). Output: rows.

```
sort figures by y0
same row  ⇔  vertical overlap ≥ 50 % of the shorter figure
            AND horizontal gap < 0.10 (page fraction)
            AND x-intervals do not overlap by more than 10 %
cluster transitively; order each row left→right by x0
drop figures whose box ≥ 0.85 of the page (already handled) 
```
Reproducing original rows is the highest-value, lowest-risk win: a source page that
showed two diagrams side by side keeps them side by side instead of stacking two
full-width images (~2× the height).

### 5.1b Flow (default)

`figure_layout="flow"` packs the figures of a page **left to right while they fit at
their natural width** (sum ≤ 0.95 text widths, ≤ `max_figures_per_row`). If they don't
fit without shrinking, they stay in a column. This means a page whose figures were
*stacked* in the source still gets a compact row when there is comfortably space,
instead of always staying a column. `preserve` keeps the original rows instead.

### 5.2 Figure sizing

For each row with original widths `w_i = x1-x0`:

```
row_scale   = min(1, target_row_width / Σ w_i)        # target_row_width = 0.95
display_w_i = clamp(row_scale * w_i, w_min=0.18, w_max=0.85)   # × \textwidth
display_h_i = display_w_i * aspect_i                  # aspect from cropped PNG px
```
- Cap height: if `display_h_i > 0.38 \textheight`, shrink that figure's width so the
  height cap holds; if that pushes width below `w_min`, promote the figure to its own
  row (or its own page if still too tall).
- If a row's figures would each be `< w_min`, wrap the row into multiple sub-rows
  (e.g. 3 → 2 + 1), never more than `max_figures_per_row` (default 3).
- Captions stay under their figure (minipage per figure) so rows align.
- Full-width single figure: `w_max_single = 0.85`.

### 5.3 Pagination (splitting across pages)

Budget `H` = usable height of the output page (`output_page_size` minus margins).

```
pages = [[]]; used = 0
for block in reading order:
    h = measured(block) or h(figure-row)         # figure row includes captions
    if used + h ≤ H: place
    else:
        if block is heading and next fits on a new page with it: break before heading
        elif h ≤ H:  new page; place (figures/rows/tables are atomic)
        else:        place alone on its own page (overfull; optionally shrink ≤ min)
avoid: page ending on a heading; splitting a figure from its caption;
        a list item split mid-item (allow only between items)
last page: if it is very empty (< page_fill_min = 0.25) AND the previous page can be
        grown by a *small* shrink (≥ min_page_scale), prefer one page
```

### 5.4 Consistency rule

- Fix the **geometry** to the output page size and the **font size** to a constant, so
  every translated page has `scale = 1` by construction. This is what "consistent page
  content size" means here.
- Change the default scaling for translated pages from `fill` (enlarge short pages) to
  **`fit`/`consistent`** (never enlarge), so a short page is simply short but is *not*
  blown up to a different apparent size.
- Report the distribution: `scale = 1` for all pages, plus `pages_added` per source
  page.

### 5.5 Worked example

Source page: 12 lines of prose (~180 pt) + 3 figures in a row (each 0.30 wide,
aspect 0.8) + 1 full-width figure (aspect 0.5) + caption.

- **Today**: 4 figures stacked → widths 0.38, 0.38, 0.38, 0.92 \textwidth → heights
  ≈ 0.30, 0.30, 0.30, 0.46 \textwidth ≈ 236 + 236 + 236 + 360 pt + text 180 → ≈ 1250 pt
  vs usable ~794 pt → **scale ≈ 0.63** (text ~63 % → unreadable).
- **Planned**: the 3 small figures stay in one row (0.30 each, height ≈ 0.24
  \textwidth ≈ 113 pt), full-width figure capped to 0.85 (height ≈ 0.42 ≈ 200 pt),
  text 180 pt, captions ~40 pt → ≈ 533 pt → **fits at scale 1.0**.

## 6. Measurement mechanism

`layout.measure(cfg, page, blocks, figure_rows) -> dict[index, (w_pt, h_pt)]`

```latex
\newbox\mb
\setbox\mb=\vbox{<block tex>}
\typeout{BLOCKH 3 \the\dimexpr\ht\mb+\dp\mb\relax}
\typeout{BLOCKW 3 \the\wd\mb\relax}
```

- One XeLaTeX compile per source page (in addition to the per-output-page compiles).
- Parse `BLOCKH/BLOCKW` from stdout; convert pt → fraction of usable height.
- Cache keyed by a hash of the rendered block TeX + width + font, stored next to
  `ocr.json`; invalidated when `layout.*`/font/`text_width` change.
- Fallback if the measurement compile fails: analytic estimate
  (`lines = ceil(chars/chars_per_line)`, figure height from aspect) so the feature
  degrades gracefully rather than failing the page.

## 7. Config & UI

New `PipelineConfig` fields (all engine keys, sent from `JobCreate`):

| key | default | meaning |
|---|---|---|
| `layout_mode` | `auto` | `single` (today) · `auto` (rows + split) · `flow` (native pagination, future) |
| `min_page_scale` | `0.90` | never shrink text below this; split instead |
| `figure_layout` | `flow` | `flow` (side by side when they fit) · `preserve` (source rows) · `grid` (always pack) · `stack` (today) |
| `figure_max_width` | `0.85` | cap a single figure |
| `figure_max_height` | `0.38` | fraction of usable height |
| `figure_min_width` | `0.18` | below this, reflow the row |
| `max_figures_per_row` | `3` | wrap beyond this |
| `page_fill_min` | `0.25` | avoid tiny last pages |
| `scale_mode` default | `fit` | (changed) don't enlarge short pages |
| `keep_together` | `true` | figure+caption, heading+next block |

UI: a **Page fitting** group in section 2 —
- radio: *Keep text size* (split pages) / *Fit one page* (shrink, current) / *Auto*;
- figure layout: *Preserve original arrangement* / *Auto grid* / *Stack*;
- sliders: min text size (85–100 %), max figure height.

Report/`report.json`: per source page `{pages: k, scale: 1.0, figure_rows: n}` so the UI
can show "page 7 → 2 pages" and the review tab can show the extra pages.

## 8. Compatibility & edge cases

- **Bilingual modes.** `interleave`/`grouped`/`translated_only`: a source page maps to
  `k` translated pages (original, then translations). `side_by_side` is ill-defined for
  `k>1` → keep the **first** translated page beside the original and append the rest as
  full pages, with a note in the UI. (Alternative: fall back to interleave for those
  pages.)
- **Review editor / rebuild.** `POST .../pages/{n}/rebuild` re-runs planner+typeset for
  that source page; the page list shows sub-pages `7a, 7b`.
- **TOC / chapter boundaries.** `boundaries` map to the *first* output page of the
  source page they point at.
- **Determinism.** Planner and measurement are deterministic; the same inputs produce
  the same page count (important for caching and tests).
- **Cache.** OCR cache unchanged; add a **layout cache** keyed by
  `(doc, page, layout config, font, text_width)`. `prompt_version` unaffected.
- **Cost / CPU.** No model calls added. Extra local XeLaTeX compiles (≈1 measure + k
  output pages per source page). Bounded by `concurrency`; on a constrained box this
  *adds* local CPU, so keep page-concurrency ≈ cores.
- **Formulas / ordering.** Blocks are rendered by the same `_block_tex`; formulas are
  untouched. Reading order is preserved because pagination is greedy over the ordered
  block list.
- **Degenerate figures.** Near-full-page boxes are excluded from rows; a single
  too-tall figure becomes its own page rather than shrinking the text.
- **Very long lists/tables.** Out of scope for v1 (kept atomic); note as follow-up.

## 9. Testing

- **Unit — rows:** bbox sets → expected row clustering (side-by-side, 2×2, stacked,
  gap/overlap thresholds).
- **Unit — sizing:** width allocation preserves ratios, respects min/max, promotes
  too-tall figures.
- **Unit — pagination:** greedy pack never exceeds `H` except for atomic oversized
  blocks; headings not orphaned; figure+caption never split.
- **Golden:** the §5.5 example → `k` pages at scale 1.0 (vs 1 page at ~0.63 today);
  assert `pages == 2` or `1` after row-packing.
- **Property:** for random block sets, `Σ page_height ≤ k·H + slack` and all pages are
  within `[1.0]` scale (consistency invariant).
- **Regression:** existing single-figure and no-figure pages produce byte-identical
  output when `layout_mode=single`.
- **Live:** real docs with multiple figures (`Preseci prizme...`,
  `Talesova teorema...`, `OMM Skripta`) — assert no page below `min_page_scale` and
  compare average text height before/after.

## 10. Milestones

| # | Deliverable | Risk | Effort |
|---|---|---|---|
| **M1** | Measurement pass + overflow detection; report `needed_scale` per page, no output change | low | 1 day |
| **M2** | **Figure rows** (preserve original arrangement) + sizing caps | low | 1–2 days |
| **M3** | **Pagination**: split into k pages; `assemble` maps 1→k; scale `fit` | med | 2–3 days |
| **M4** | Config + UI + report metrics; docs | low | 1 day |
| **M5** | Native-flow mode (`layout_mode=flow`), wrapfig experiments | high | later |

M2 alone likely removes most shrinking (the common failure is 2–3 side-by-side figures
that we currently stack). M3 handles the rest.

## 11. Alternatives considered

- **Native LaTeX pagination** (real `article`-like flow, floats): less code, but
  non-deterministic breaks, float drift, and it fights the per-page review model →
  rejected for v1, kept as `flow`.
- **Analytic height estimation only** (no measure pass): no extra compile, but LaTeX
  line-breaking/figure scaling makes it ±20 % → use as fallback, not the primary path.
- **Shrink only figures, keep text**: helps but a row of 4 figures still can't all fit;
  pagination is still needed. Figure sizing is a component, not the whole answer.

## 12. Open questions

1. For `interleave`, should a multi-page translation insert `original, t1, t2` or
   `original+t1` then `t2`? (Proposed: original first, then translations in order.)
2. Should `min_page_scale` be a hard floor (never shrink, always split) or a soft
   preference (prefer ≤1 extra page for ≥90 %)? (Proposed: soft, with `page_fill_min`.)
3. Do we ever want to **enlarge** figures that the model cropped too small? (No for v1.)
4. Per-document global font size (pick one size for the whole doc) vs per-page? (Fixed
   geometry gives this for free; confirm.)
