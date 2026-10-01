# Abuse protection — rate limits, cooldowns, anti-flood

Status: **plan**. Goal: keep the app usable under normal load and make flooding,
scraping, and accidental self-DoS (or a stranger draining your API key) fail
cheaply and predictably.

## 0. Threat model (what actually hurts this app)

Unlike a plain website, the expensive resources here are **not** bandwidth:

1. **Money** — every page costs a model call. An attacker (or a stuck loop) can
   spend your `DS_KEY` budget fast.
2. **Long jobs** — OCR is ~1 min/page; a few big jobs starve the worker pool.
3. **Disk/CPU** — uploads, page renders, XeLaTeX.
4. Classic flood/slowloris on the HTTP layer.

So the priority order is: **budget + job caps + BYOK first**, then request rate
limits, then edge protection.

## 1. Deployment modes (pick limits per mode)

| mode | who | auth | server key |
|---|---|---|---|
| `local` | one user, localhost | none | allowed |
| `team` | a few people | shared token / basic auth | allowed, capped |
| `public` | anyone | accounts (or none) | **BYOK required** |

Everything below is configurable; `local` disables most limits.

## 2. Defense layers

```
edge (Caddy/nginx) → app middleware → endpoint weights → job/cost gates → worker caps
```

### 2a. Edge (reverse proxy) — cheapest, do this first
- HTTPS only; HTTP/2; request body cap (`250 MB`).
- Per-IP: max **20 concurrent connections**, **10 req/s** (burst 20), slowloris
  timeouts (`header 10s`, `body 60s`).
- Short ban on repeat `429/4xx` bursts (fail2ban-style).
- Never expose the model key; only the API container.

### 2b. App middleware — a weighted token bucket
One small ASGI middleware (`app/ratelimit.py`) evaluated before route handlers:

- **Key** (in priority order): `X-Session-Id` (our session cookie/id), then client
  IP (respecting `X-Forwarded-For` from the trusted proxy), then a global bucket.
- **Weighted cost per endpoint** so one job submission is worth many `GET`s:

  | endpoint | weight |
  |---|---|
  | `GET /api/health`, `GET /api/*/pages` | 1 |
  | `POST /api/documents` (upload) | 10 |
  | `POST /api/documents/{id}/jobs` | 20 |
  | `POST /api/documents/{id}/book` | 50 |
  | `GET /api/jobs/{id}/events` (SSE) | 2 + a held connection slot |

- Return **`429` + `Retry-After`** (and `RateLimit-*` headers). Config: `RATE_LIMIT_PER_MIN`, `RATE_BURST`.
- Storage: in-memory (single process) by default; **Redis** (`RATELIMIT_REDIS_URL`)
  when running multiple workers so limits are global.

### 2c. Cooldowns & quotas (job-level)
- **Cooldown**: minimum `JOB_COOLDOWN_SECONDS` (default **5 s**) between job
  creations per session → kills double-clicks and tight loops.
- **Active-job cap**: `MAX_ACTIVE_JOBS_PER_SESSION` (default **1**; books count as 1).
  New submissions beyond it return `429` (or queue behind an explicit opt-in).
- **Quotas**: `MAX_JOBS_PER_HOUR` (10), `MAX_UPLOADS_PER_HOUR` (20) per session.
- **Deduplication**: hash `(sha256(file), config)`; if an identical job was created
  in the last `DEDUPE_WINDOW` (60 s) and is still running/done, **return that job**
  instead of starting a new one. Also lets the client re-poll safely.

### 2d. Global worker/queue caps (protect the machine)
- `WORKER_CONCURRENCY` (jobs, default 3) and `BOOK_CHUNK_CONCURRENCY` (default 2)
  already bound CPU/TeX memory.
- Add a **queue depth cap** (`MAX_QUEUED_JOBS`, e.g. 50); beyond it, reject with
  `503` rather than piling work onto a dead pool.

### 2e. Cost controls (the real anti-DoS)
- **BYOK**: the model key belongs to the user; the server key is disabled in
  `public` mode. This is the single most effective control.
- **Budgets**: `MAX_JOB_USD` / `MAX_BOOK_USD` (exist) + a **daily per-session cap**
  `MAX_DAILY_USD` (default $1 local, $5 team). Exceeding it pauses/rejects with a
  clear message.
- The **price predictor** (already built) runs before submission; if the estimate
  exceeds the remaining daily budget, reject with `402`/`429` + the estimate.
- **Page caps**: `MAX_PAGES` (exists); add `MAX_BOOK_PAGES`.

### 2f. SSE / long connections
- Cap **concurrent SSE streams per session** (default 3); the middleware holds a
  slot for the connection lifetime.
- Server-side **keepalive** every 15 s and **idle timeout** 120 s.
- The client already reconnects; rate-limit reconnects via the bucket.

### 2g. Upload hardening (mostly done)
- size/page caps (exist), PDF **sanitization** (exist), reject non-PDF/image.
- Optionally: reject encrypted PDFs unless a password is supplied; verify magic
  bytes; strip to a canonical PDF before storing (exists).

### 2h. Identity & admin
- `local`: no auth. `team`: a shared bearer token (`APP_TOKEN`). `public`: real
  accounts (later) or anonymous + BYOK + tight limits.
- Optional **captcha / proof-of-work** on upload for anonymous public use.
- **Audit + metrics**: log `429`s, jobs created, spend per session; expose
  `/api/admin/stats` (token-protected); maintain a temporary **IP blocklist**.

## 3. Sane default config

```ini
# mode
APP_MODE=local                 # local | team | public
# edge (set in the reverse proxy, not the app)
#   limit_conn 20; limit_req 10r/s burst=20; client_max_body_size 250m;
# app rate limit (weighted units per minute, per key)
RATE_LIMIT_PER_MIN=120
RATE_BURST=40
# jobs
JOB_COOLDOWN_SECONDS=5
MAX_ACTIVE_JOBS_PER_SESSION=1
MAX_JOBS_PER_HOUR=10
MAX_UPLOADS_PER_HOUR=20
DEDUPE_WINDOW=60
MAX_QUEUED_JOBS=50
# cost
MAX_JOB_USD=2.00
MAX_BOOK_USD=5.00
MAX_DAILY_USD=1.00
# sse
MAX_SSE_PER_SESSION=3
SSE_KEEPALIVE=15
SSE_IDLE_TIMEOUT=120
```

## 4. Implementation sketch

- `app/ratelimit.py` — `RateLimiter` (token bucket, weighted) + `RateLimitMiddleware`.
  In-memory `dict[key, bucket]` swept periodically; Redis backend behind the same API.
- `app/guards.py` — `check_job_quota(session)`, `cooldown_ok(session)`,
  `dedupe_lookup(session, doc, config)`, `daily_budget_ok(session)`.
- Wire into `create_job` / `create_book` endpoints (return `429`/`402`) and the SSE
  generator (slot acquire/release).
- Settings: add the env vars above to `app/settings.py` + `.env.example`.
- Edge templates: `deploy/Caddyfile` and `deploy/nginx.conf` with the limits above.
- Multi-worker: `RATELIMIT_REDIS_URL` (Redis + `redis-py`), or keep one process.

## 5. Testing

- **Unit**: bucket refill/deny math; weighted costs; `Retry-After` header; per-key
  isolation; cooldown boundary; dedupe window; daily budget stop.
- **API (TestClient)**: 11th job in an hour → 429; two rapid uploads → 2nd delayed
  → 429; identical submissions within 60 s return the **same** job id; over-budget
  estimate → 402/429; SSE beyond the cap → 429.
- **Load**: a script hammering `/docs` + upload with a fake provider, assert p95
  latency and that workers aren't starved; a 1000-connection SSE test.
- **Regression**: limits are disabled in `local` mode (existing tests unaffected).

## 6. Phased rollout

| Phase | Deliverable |
|---|---|
| **R0** | settings + guards: cooldown, active-job cap, dedupe, daily budget (no middleware) |
| **R1** | weighted rate-limit middleware (in-memory) + `429`/`Retry-After` + SSE slots |
| **R2** | edge templates (Caddy/nginx) + `/api/admin/stats` + audit log |
| **R3** | Redis backend for multi-worker + optional captcha/PoW, IP blocklist |

## 7. Decisions needed

1. **Mode / hosting**: `local` only for now, or do you intend to expose it (team or
   public)? This sets how aggressive the defaults must be.
2. **Auth**: none (localhost), shared `APP_TOKEN`, or real accounts?
3. **BYOK required?** (strongly recommended for anything public).
4. **Limiter backend**: in-memory single process, or Redis now for multi-worker?
5. **Queue behaviour**: reject when full (`503`, simplest) or accept and queue
   (needs a durable queue)?


## Disk hygiene (work intermediates)

Translation writes a lot of *regenerateable* intermediates — especially book
``chunks/`` (each chunk keeps its own rendered pages, LaTeX, figure crops and page
PDFs). The OCR **cache** is tiny by comparison (single-digit MB) and the output
**artifacts** are the deliverables.

**Storage is content-addressed** so the same bytes are never stored twice:
uploaded sources live at ``sources/<sha256>.pdf`` and every document that uploads the
same content gets a **hard link** to that one file (and re-uploading an identical file
reuses the existing document row instead of creating a copy); output artifacts are
stored at ``artifacts/blobs/<sha256>.pdf`` with each job hard-linking its copy.
``ARTIFACT_TTL_HOURS`` (default **0 = keep**) prunes old output artifacts.

**Use-and-delete lifecycle** (biggest win): page renders are deleted right after the
vision call (the page-image endpoint re-renders on demand); a book chunk's built page
PDFs/figures are **moved** (not copied) into the book's tex dir and the rest of the chunk
work dir is purged immediately; a book's work dir is purged right after it is assembled.
Peak disk is therefore ~one chunk + the current book's page PDFs instead of the whole
book (≫10× less). If free space drops below ``MIN_FREE_MB`` the book/batch dispatcher
**pauses** ("paused: low disk space") and auto-resumes once space is back.

``CACHE_TTL_HOURS`` (default **5**) is the backstop for anything left behind: `storage.prune_old_work`
deletes whole job work dirs whose last activity is older than the TTL, keeping active
jobs, the per-document OCR cache and the output artifacts. It runs **at startup and every
30 min** in the app, is exposed at ``POST /api/admin/prune`` and via
``python tools/prune.py``.
