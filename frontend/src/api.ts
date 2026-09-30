// Thin typed client for the FastAPI backend.
//
// A random session id is kept in localStorage and sent as X-Session-Id, so the
// server can store the BYOK key (encrypted) for this browser session.

export interface DocumentOut {
  id: string;
  filename: string;
  sha256: string;
  n_pages: number;
  page_w: number;
  page_h: number;
  kind: string;
  size_bytes: number;
  created_at: string;
}

export interface ArtifactOut {
  id: string;
  kind: string;
  bytes: number;
  filename: string;
  download_url: string;
}

export interface JobOut {
  id: string;
  document_id: string;
  kind?: string;
  status: "queued" | "running" | "paused" | "finalizing" | "done" | "failed" | "canceled";
  model: string;
  source_lang: string;
  target_lang: string;
  mode: string;
  progress: number;
  total_pages: number;
  done_pages: number;
  cost_usd: number;
  error?: string | null;
  created_at?: string;
  started_at?: string | null;
  finished_at?: string | null;
  artifacts: ArtifactOut[];
}

export interface Block {
  idx: number;
  type: string;
  source: string;
  target: string;
  latex: string;
  description: string;
  bbox?: number[] | null;
  level?: number | null;
  ordered?: boolean | null;
  items?: { source?: string; target?: string }[] | null;
  kind?: string | null;
  name?: string | null;
  caption?: string | null;
  number?: string | null;
}

export interface Page {
  page: number;
  status: string;
  image_url: string;
  blocks: Block[];
}

export interface Estimate {
  model: string;
  pages: number;
  est_calls: number;
  est_prompt_tokens: number;
  est_completion_tokens: number;
  est_cost_usd: number;
  est_cost_low?: number;
  est_cost_high?: number;
  est_seconds?: number;
  breakdown?: Record<string, number>;
  assumptions?: Record<string, number | string | boolean>;
}

export interface EstimateOptions {
  model: string;
  verifyMath: boolean;
  pages: string;
  figureMode: "off" | "tight" | "judge";
  targetLang: string;
  glossaryTerms: number;
  extraChars: number;
}

export interface Usage {
  jobs: number;
  done: number;
  failed: number;
  cost_usd: number;
  prompt_tokens: number;
  completion_tokens: number;
  calls: number;
}

export interface ModelInfo {
  id: string;
  name?: string | null;
  inputs: string[];
}

export interface SessionOut {
  id: string;
  has_key: boolean;
  hint?: string | null;
  api_base?: string | null;
}

export interface MathCheck {
  page: number;
  ok: boolean;
  issues: { block?: number | null; problem: string }[];
  checked: number;
}

export interface Report {
  [base: string]: {
    output: string;
    issues: { kind: string; page?: number; error?: string }[];
    math: MathCheck[];
  };
}

export interface Chunk {
  id: string;
  idx: number;
  page_from: number;
  page_to: number;
  state: string;
  done_pages: number;
  attempts: number;
  max_attempts: number;
  cost_usd: number;
  error?: string | null;
}

export interface BookCreate extends JobCreate {
  chunk_size?: number;
  from_page?: number;
  to_page?: number;
}

export interface BatchCreate extends JobCreate {
  document_ids: string[];
  book?: boolean;
  chunk_size?: number;
}

export interface JobCreate {
  source_lang: string;
  target_lang: string;
  model?: string;
  api_key?: string;
  font_main?: string;
  linebreak_locale?: string;
  bilingual: boolean;
  combine: "interleave" | "grouped" | "side_by_side" | "translated_only";
  dpi?: number;
  max_px?: number;
  figure_px?: number;
  max_scale?: number;
  text_width?: string;
  glossary?: { source: string; target: string }[];
  do_not_translate?: string[];
  llm_instructions?: string;
  verify_math?: boolean;
  error_guard?: "off" | "auto";
  pages?: string;
  unprocessed?: "original" | "skip";
  output_page_size?: "match" | "a4" | "letter";
  scale_mode?: "fill" | "fit";
  concurrency?: number;
  figure_pad?: number;
  figure_mode?: "off" | "tight" | "judge";
  figure_dpi?: number;
  figure_format?: "auto" | "png" | "jpeg";
  output_name?: string;
  // page fitting
  layout_mode?: "single" | "auto";
  min_page_scale?: number;
  figure_layout?: "flow" | "preserve" | "grid" | "stack";
  figure_max_width?: number;
  figure_max_height?: number;
  max_figures_per_row?: number;
  page_fill_min?: number;
  keep_together?: boolean;
}

function newSessionId(): string {
  const c = globalThis.crypto as Crypto | undefined;
  if (c?.randomUUID) return c.randomUUID();
  return `s-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

export function sessionId(): string {
  let v = localStorage.getItem("ocrtran_sid");
  if (!v) {
    v = newSessionId();
    localStorage.setItem("ocrtran_sid", v);
  }
  return v;
}

const SID = sessionId();

function headers(extra: Record<string, string> = {}): Record<string, string> {
  return { "X-Session-Id": SID, ...extra };
}

async function j<T>(r: Response): Promise<T> {
  if (!r.ok) throw new Error((await r.text()) || r.statusText);
  return (await r.json()) as T;
}

const json = (body: unknown) => ({
  method: "POST",
  headers: headers({ "Content-Type": "application/json" }),
  body: JSON.stringify(body),
});

export const api = {
  listDocuments: () => fetch("/api/documents", { headers: headers() }).then(j<DocumentOut[]>),

  upload: (file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return fetch("/api/documents", { method: "POST", headers: headers(), body: fd }).then(
      j<DocumentOut>,
    );
  },

  createJob: (docId: string, body: JobCreate) =>
    fetch(`/api/documents/${docId}/jobs`, json(body)).then(j<JobOut>),

  createBook: (docId: string, body: BookCreate) =>
    fetch(`/api/documents/${docId}/book`, json(body)).then(j<JobOut>),

  createBatch: (body: BatchCreate) => fetch("/api/batch", json(body)).then(j<JobOut>),

  children: (jobId: string) =>
    fetch(`/api/jobs/${jobId}/children`, { headers: headers() }).then(j<JobOut[]>),

  chunks: (jobId: string) =>
    fetch(`/api/jobs/${jobId}/chunks`, { headers: headers() }).then(j<Chunk[]>),

  pauseBook: (jobId: string) =>
    fetch(`/api/jobs/${jobId}/pause`, { method: "POST", headers: headers() }).then(j),

  resumeBook: (jobId: string) =>
    fetch(`/api/jobs/${jobId}/resume`, { method: "POST", headers: headers() }).then(j),

  retryChunk: (jobId: string, chunkId: string) =>
    fetch(`/api/jobs/${jobId}/chunks/${chunkId}/retry`, { method: "POST", headers: headers() }).then(j),

  getJob: (id: string) => fetch(`/api/jobs/${id}`, { headers: headers() }).then(j<JobOut>),

  cancel: (id: string) =>
    fetch(`/api/jobs/${id}/cancel`, { method: "POST", headers: headers() }).then(j),

  pages: (id: string) =>
    fetch(`/api/jobs/${id}/pages`, { headers: headers() }).then(j<Page[]>),

  patchBlock: (id: string, page: number, idx: number, patch: Partial<Block>) =>
    fetch(`/api/jobs/${id}/pages/${page}/blocks/${idx}`, {
      method: "PATCH",
      headers: headers({ "Content-Type": "application/json" }),
      body: JSON.stringify(patch),
    }).then(j<Block>),

  reorder: (id: string, page: number, order: number[]) =>
    fetch(`/api/jobs/${id}/pages/${page}/reorder`, json({ order })).then(j),

  rebuild: (id: string, page: number) =>
    fetch(`/api/jobs/${id}/pages/${page}/rebuild`, { method: "POST", headers: headers() }).then(j),

  estimate: (docId: string, o: EstimateOptions) => {
    const q = new URLSearchParams({
      model: o.model,
      verify_math: String(o.verifyMath),
      pages: o.pages || "all",
      figure_mode: o.figureMode,
      target_lang: o.targetLang,
      glossary_terms: String(o.glossaryTerms),
      extra_chars: String(o.extraChars),
    });
    return fetch(`/api/documents/${docId}/estimate?${q}`, { headers: headers() }).then(j<Estimate>);
  },

  report: (jobId: string) =>
    fetch(`/api/jobs/${jobId}/report`, { headers: headers() }).then(j<Report>),

  usage: () => fetch("/api/usage", { headers: headers() }).then(j<Usage>),

  models: (apiKey: string) =>
    fetch(`/api/models?api_key=${encodeURIComponent(apiKey)}`, { headers: headers() })
      .then(j<{ models: ModelInfo[] }>)
      .then((r) => r.models),

  // --- session (BYOK stored server-side, encrypted) ---
  getSession: () => fetch("/api/session", { headers: headers() }).then(j<SessionOut>),

  saveKey: (apiKey: string) =>
    fetch("/api/session", {
      method: "PUT",
      headers: headers({ "Content-Type": "application/json" }),
      body: JSON.stringify({ api_key: apiKey }),
    }).then(j<SessionOut>),

  clearKey: () =>
    fetch("/api/session/key", { method: "DELETE", headers: headers() }).then(j<SessionOut>),
};
