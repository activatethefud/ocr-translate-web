import { useEffect, useMemo, useRef, useState } from "react";
import {
  api,
  type Block,
  type DocumentOut,
  type Estimate,
  type JobCreate,
  type Chunk,
  type JobOut,
  type ModelInfo,
  type Page,
  type Report,
  type Usage,
} from "./api";

type Combine = "interleave" | "grouped" | "side_by_side" | "translated_only";

const LANGUAGE_NAMES = [
  "Afrikaans", "Albanian", "Amharic", "Arabic", "Armenian", "Azerbaijani",
  "Basque", "Belarusian", "Bengali", "Bosnian", "Bulgarian", "Burmese",
  "Catalan", "Cebuano", "Chinese (Simplified)", "Chinese (Traditional)",
  "Croatian", "Czech", "Danish", "Dutch", "English", "Esperanto", "Estonian",
  "Filipino", "Finnish", "French", "Galician", "Georgian", "German", "Greek",
  "Gujarati", "Haitian Creole", "Hausa", "Hebrew", "Hindi", "Hmong", "Hungarian",
  "Icelandic", "Igbo", "Indonesian", "Irish", "Italian", "Japanese", "Javanese",
  "Kannada", "Kazakh", "Khmer", "Kinyarwanda", "Korean", "Kurdish", "Kyrgyz",
  "Lao", "Latin", "Latvian", "Lithuanian", "Luxembourgish", "Macedonian",
  "Malagasy", "Malay", "Malayalam", "Maltese", "Marathi", "Mongolian", "Nepali",
  "Norwegian", "Odia", "Pashto", "Persian", "Polish", "Portuguese", "Punjabi",
  "Romanian", "Russian", "Serbian", "Shona", "Sindhi", "Sinhala", "Slovak",
  "Slovenian", "Somali", "Spanish", "Sundanese", "Swahili", "Swedish", "Tajik",
  "Tamil", "Tatar", "Telugu", "Thai", "Turkish", "Turkmen", "Ukrainian", "Urdu",
  "Uyghur", "Uzbek", "Vietnamese", "Welsh", "Xhosa", "Yiddish", "Yoruba", "Zulu",
];
// sorted A→Z, with an "Other…" free-text option handled by <LangSelect>
const LANGUAGES = [...LANGUAGE_NAMES].sort((a, b) => a.localeCompare(b));
const OTHER = "__other__";

function parseGlossary(text: string): { source: string; target: string }[] {
  return text
    .split("\n")
    .map((l) => l.split(/=>|->/))
    .filter((p) => p.length === 2 && p[0].trim() && p[1].trim())
    .map((p) => ({ source: p[0].trim(), target: p[1].trim() }));
}

function stageLabel(e: { stage: string; status: string; page?: number | null }): string {
  const p = e.page ? ` ${e.page}` : "";
  switch (e.stage) {
    case "ocr":
      return e.status === "ok" || e.status === "warn"
        ? `read page${p}`
        : `reading page${p} (OCR + translation)…`;
    case "annotate":
      return "translating in-formula text…";
    case "build":
      return e.status === "ok" ? `typeset page${p}` : `typesetting page${p}…`;
    case "verify":
      return `verifying formulas${p}…`;
    case "assemble":
      return "assembling PDF…";
    case "done":
      return "done";
    default:
      return `${e.stage}${p}…`;
  }
}

function LangSelect(props: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  allowAuto?: boolean;
}) {
  const { label, value, onChange, allowAuto } = props;
  const known = LANGUAGES.includes(value);
  const selectValue = value === "auto" ? "auto" : known ? value : OTHER;
  const isOther = selectValue === OTHER;
  return (
    <label>{label}
      <select
        value={selectValue}
        onChange={(e) => onChange(e.target.value === OTHER ? "" : e.target.value)}
      >
        {allowAuto && <option value="auto">Auto-detect</option>}
        {LANGUAGES.map((l) => <option key={l} value={l}>{l}</option>)}
        <option value={OTHER}>Other…</option>
      </select>
      {isOther && (
        <input
          value={value}
          placeholder="Type a language, e.g. Tagalog"
          onChange={(e) => onChange(e.target.value)}
        />
      )}
    </label>
  );
}

export function App() {
  const [docs, setDocs] = useState<DocumentOut[]>([]);
  const [doc, setDoc] = useState<DocumentOut | null>(null);
  const [job, setJob] = useState<JobOut | null>(null);
  const [events, setEvents] = useState<string[]>([]);
  const [pages, setPages] = useState<Page[]>([]);
  const [active, setActive] = useState(1);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [estimate, setEstimate] = useState<Estimate | null>(null);
  const [report, setReport] = useState<Report | null>(null);
  const [usage, setUsage] = useState<Usage | null>(null);
  const [models, setModels] = useState<ModelInfo[]>([]);

  // BYOK key is kept in localStorage for convenience; only sent with the job
  // request and never persisted on the server.
  const [apiKey, setApiKey] = useState(() => sessionStorage.getItem("ocr_key") || "");
  const [sourceLang, setSourceLang] = useState("Serbian");
  const [targetLang, setTargetLang] = useState("French");
  const [model, setModel] = useState("deepseek-flash");
  const [fontMain, setFontMain] = useState("Noto Serif");
  const [linebreak, setLinebreak] = useState("");
  const [bilingual, setBilingual] = useState(true);
  const [combine, setCombine] = useState<Combine>("interleave");
  const [glossaryText, setGlossaryText] = useState("");
  const [doNotTranslate, setDoNotTranslate] = useState("");
  const [llmInstructions, setLlmInstructions] = useState("");
  const [verifyMath, setVerifyMath] = useState(false);
  const [pagesSpec, setPagesSpec] = useState("all");
  const [unprocessed, setUnprocessed] = useState<"original" | "skip">("skip");
  const [outputPageSize, setOutputPageSize] = useState<"match" | "a4" | "letter">("match");
  const [scaleMode, setScaleMode] = useState<"fill" | "fit">("fill");
  const [pageConcurrency, setPageConcurrency] = useState(4);
  const [figureMode, setFigureMode] = useState<"off" | "tight" | "judge">("judge");
  const [layoutMode, setLayoutMode] = useState<"single" | "auto">("auto");
  const [minPageScale, setMinPageScale] = useState(0.9);
  const [figureLayout, setFigureLayout] = useState<"flow" | "preserve" | "grid" | "stack">("flow");
  const [figureMaxHeight, setFigureMaxHeight] = useState(0.38);
  const [figureDpi, setFigureDpi] = useState(300);
  const [figureFormat, setFigureFormat] = useState<"auto" | "png" | "jpeg">("auto");
  const [outputName, setOutputName] = useState("");
  const [bookMode, setBookMode] = useState(false);
  const [chunkSize, setChunkSize] = useState(25);
  const [chunks, setChunks] = useState<Chunk[]>([]);
  const [queueMode, setQueueMode] = useState(false);
  const [batchDocs, setBatchDocs] = useState<string[]>([]);
  const [children, setChildren] = useState<JobOut[]>([]);
  const [sessionHasKey, setSessionHasKey] = useState(false);
  const [sessionHint, setSessionHint] = useState("");
  const [stage, setStage] = useState("");
  const [elapsed, setElapsed] = useState(0);

  const esRef = useRef<EventSource | null>(null);

  useEffect(() => {
    api.listDocuments().then(setDocs).catch((e) => setError(String(e)));
    api.usage().then(setUsage).catch(() => undefined);
    api
      .getSession()
      .then((s) => { setSessionHasKey(s.has_key); setSessionHint(s.hint ?? ""); })
      .catch(() => undefined);
  }, []);

  useEffect(() => {
    const low = targetLang.toLowerCase();
    if (low.includes("chinese")) { setFontMain("Noto Sans CJK SC"); setLinebreak("zh"); }
    else if (low.includes("japanese")) { setFontMain("Noto Sans CJK JP"); setLinebreak("ja"); }
    else if (low.includes("korean")) { setFontMain("Noto Sans CJK KR"); setLinebreak("ko"); }
    else { setFontMain("Noto Serif"); setLinebreak(""); }
  }, [targetLang]);

  useEffect(() => {
    if (!doc) { setEstimate(null); return; }
    api
      .estimate(doc.id, {
        model,
        verifyMath,
        pages: pagesSpec,
        figureMode,
        targetLang,
        glossaryTerms: parseGlossary(glossaryText).length,
        extraChars: llmInstructions.length + doNotTranslate.length,
      })
      .then(setEstimate)
      .catch(() => setEstimate(null));
  }, [doc?.id, model, verifyMath, pagesSpec, figureMode, targetLang, glossaryText, llmInstructions, doNotTranslate]);

  const running = job && (job.status === "queued" || job.status === "running");
  const activePage = useMemo(() => pages.find((p) => p.page === active), [pages, active]);

  // ticking elapsed timer while a job runs
  useEffect(() => {
    if (!running || !job) {
      setElapsed(0);
      return;
    }
    const start = job.started_at ? Date.parse(job.started_at) : Date.now();
    const tick = () => setElapsed(Math.max(0, Math.round((Date.now() - start) / 1000)));
    tick();
    const iv = setInterval(tick, 1000);
    return () => clearInterval(iv);
  }, [running, job?.id, job?.started_at]);

  async function refreshJob() {
    if (!job) return;
    const j = await api.getJob(job.id);
    setJob(j);
    if (j.status !== "queued") setPages(await api.pages(j.id));
    if (j.kind === "book") setChunks(await api.chunks(j.id));
    if (j.kind === "batch") setChildren(await api.children(j.id));
    if (j.status === "done") {
      api.report(j.id).then(setReport).catch(() => undefined);
      api.usage().then(setUsage).catch(() => undefined);
    }
  }

  useEffect(() => {
    if (!running || !job) return;
    const es = new EventSource(`/api/jobs/${job.id}/events`);
    esRef.current = es;
    es.addEventListener("progress", (ev) => {
      const d = JSON.parse((ev as MessageEvent).data);
      setStage(stageLabel(d));
      setEvents((p) => [...p.slice(-300), `${d.stage}/${d.status} p${d.page ?? ""} ${d.message ?? ""}`]);
    });
    es.addEventListener("end", () => {
      es.close();
      esRef.current = null;
      setStage("");
      refreshJob();
    });
    const iv = setInterval(refreshJob, 1500);
    return () => { es.close(); clearInterval(iv); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [running, job?.id]);

  async function onUpload(file: File) {
    setError(""); setBusy(true);
    try {
      const d = await api.upload(file);
      setDoc(d);
      if (queueMode) setBatchDocs((prev) => (prev.includes(d.id) ? prev : [...prev, d.id]));
      setDocs(await api.listDocuments());
      setJob(null); setPages([]); setEvents([]); setReport(null);
    } catch (e) { setError(String(e)); } finally { setBusy(false); }
  }

  async function onStart() {
    if (queueMode) {
      if (!batchDocs.length) { setError("Select at least one document to queue"); return; }
    } else if (!doc) {
      return;
    }
    if (!targetLang.trim()) { setError("Please choose or type a target language"); return; }
    setError(""); setBusy(true);
    if (apiKey) sessionStorage.setItem("ocr_key", apiKey);
    const body: JobCreate = {
      source_lang: sourceLang.trim() || "auto", target_lang: targetLang.trim(), model,
      api_key: apiKey || undefined, font_main: fontMain,
      linebreak_locale: linebreak, bilingual, combine,
      glossary: parseGlossary(glossaryText),
      do_not_translate: doNotTranslate.split(",").map((s) => s.trim()).filter(Boolean),
      llm_instructions: llmInstructions,
      verify_math: verifyMath,
      pages: pagesSpec,
      unprocessed,
      output_page_size: outputPageSize,
      scale_mode: scaleMode,
      concurrency: pageConcurrency,
      figure_mode: figureMode,
      output_name: outputName.trim() || undefined,
      layout_mode: layoutMode,
      min_page_scale: minPageScale,
      figure_layout: figureLayout,
      figure_max_height: figureMaxHeight,
      figure_dpi: figureDpi,
      figure_format: figureFormat,
    };
    try {
      const j = queueMode
        ? await api.createBatch({ ...body, document_ids: batchDocs })
        : bookMode && doc
          ? await api.createBook(doc.id, { ...body, chunk_size: chunkSize })
          : await api.createJob(doc!.id, body);
      setJob(j); setEvents([]); setPages([]); setReport(null); setChunks([]); setChildren([]);
      if (apiKey) setSessionHasKey(true);
    } catch (e) { setError(String(e)); } finally { setBusy(false); }
  }

  async function saveKey() {
    try {
      sessionStorage.setItem("ocr_key", apiKey);
      const s = await api.saveKey(apiKey);
      setSessionHasKey(s.has_key); setSessionHint(s.hint ?? "");
    } catch (e) { setError(String(e)); }
  }

  async function forgetKey() {
    try {
      const s = await api.clearKey();
      setSessionHasKey(s.has_key); setSessionHint(""); setApiKey("");
      sessionStorage.removeItem("ocr_key");
    } catch (e) { setError(String(e)); }
  }

  async function loadModels() {
    try { setModels(await api.models(apiKey)); } catch (e) { setError(String(e)); }
  }

  async function saveBlock(page: number, idx: number, target: string) {
    if (!job) return;
    const b = await api.patchBlock(job.id, page, idx, { target });
    setPages((ps) => ps.map((p) => p.page === page
      ? { ...p, blocks: p.blocks.map((x) => (x.idx === idx ? b : x)) } : p));
  }

  async function move(page: number, idx: number, dir: -1 | 1) {
    if (!job) return;
    const p = pages.find((x) => x.page === page);
    if (!p) return;
    const order = p.blocks.map((b) => b.idx);
    const j = idx + dir;
    if (j < 0 || j >= order.length) return;
    [order[idx], order[j]] = [order[j], order[idx]];
    await api.reorder(job.id, page, order);
    setPages(await api.pages(job.id));
  }

  async function rebuild(page: number) {
    if (!job) return;
    setBusy(true);
    try { await api.rebuild(job.id, page); } catch (e) { setError(String(e)); } finally { setBusy(false); }
  }

  const mathWarn = report
    ? Object.values(report).flatMap((r) => r.math).filter((m) => !m.ok).length : 0;

  return (
    <div className="app">
      <header>
        <h1>OCR + Translate</h1>
        <span className="muted">vision-model OCR → LaTeX → bilingual PDF</span>
        {usage && (
          <span className="muted right">
            {usage.jobs} jobs · {usage.calls} calls · ${usage.cost_usd.toFixed(4)}
          </span>
        )}
      </header>
      {error && <div className="error">{error}</div>}

      <div className="cols">
        <section className="card">
          <h2>1 · Document</h2>
          <input type="file" accept="application/pdf,image/*" disabled={busy}
                 onChange={(e) => e.target.files && onUpload(e.target.files[0])} />
          <select value={doc?.id ?? ""} onChange={(e) =>
            setDoc(docs.find((d) => d.id === e.target.value) ?? null)}>
            <option value="">— history —</option>
            {docs.map((d) => <option key={d.id} value={d.id}>{d.filename}</option>)}
          </select>
          <label className="check">
            <input type="checkbox" checked={queueMode}
                   onChange={(e) => setQueueMode(e.target.checked)} />
            queue multiple documents (processed one after another)
          </label>
          {queueMode && (
            <div className="doclist">
              {docs.length === 0 && <p className="muted">Upload or pick documents above.</p>}
              {docs.map((d) => (
                <label key={d.id} className="check">
                  <input type="checkbox" checked={batchDocs.includes(d.id)}
                         onChange={(e) =>
                           setBatchDocs((prev) =>
                             e.target.checked ? [...prev, d.id] : prev.filter((x) => x !== d.id))} />
                  {d.filename} <span className="muted">· {d.n_pages}p</span>
                </label>
              ))}
            </div>
          )}
          {doc && (
            <p className="muted">
              {doc.n_pages} pages · {Math.round(doc.page_w)}×{Math.round(doc.page_h)} pt · {doc.kind}
            </p>
          )}

          <h2>2 · Translate</h2>
          <label>API key (BYOK — saved for this browser session)
            <input type="password" value={apiKey} placeholder="sk-…"
                   onChange={(e) => setApiKey(e.target.value)} />
          </label>
          <div className="row spread">
            <span className="muted">
              {sessionHasKey ? `key saved ${sessionHint}` : "no key saved for this session"}
            </span>
            <span>
              <button className="ghost" type="button" onClick={saveKey} disabled={!apiKey}>Save</button>
              <button className="ghost" type="button" onClick={forgetKey} disabled={!sessionHasKey}>Forget</button>
            </span>
          </div>
          <label>Output name
            <input value={outputName}
                   placeholder={doc ? `${doc.filename.replace(/\.[^.]+$/, "")} (${targetLang}).pdf` : "output.pdf"}
                   onChange={(e) => setOutputName(e.target.value)} />
          </label>
          <label>Model
            <div className="row">
              <input list="models" value={model} onChange={(e) => setModel(e.target.value)} />
              <button className="ghost" type="button" onClick={loadModels}>Load</button>
              <datalist id="models">
                {models.map((m) => <option key={m.id} value={m.id}>{m.name ?? ""}</option>)}
              </datalist>
            </div>
          </label>
          <div className="row">
            <LangSelect label="From" value={sourceLang} onChange={setSourceLang} allowAuto />
            <LangSelect label="To" value={targetLang} onChange={setTargetLang} />
          </div>
          <div className="row">
            <label>Font<input value={fontMain} onChange={(e) => setFontMain(e.target.value)} /></label>
            <label>Line-break locale
              <input value={linebreak} placeholder="zh/ja/ko or blank"
                     onChange={(e) => setLinebreak(e.target.value)} />
            </label>
          </div>
          <label className="check">
            <input
              type="checkbox"
              checked={bilingual}
              onChange={(e) => {
                const on = e.target.checked;
                setBilingual(on);
                if (!on) setCombine("translated_only");
                else if (combine === "translated_only") setCombine("interleave");
              }}
            />
            keep original pages (bilingual)
          </label>
          <label>Combine
            <select
              value={combine}
              onChange={(e) => {
                const v = e.target.value as Combine;
                setCombine(v);
                setBilingual(v !== "translated_only");
              }}
            >
              <option value="interleave">interleave (original, translation, …)</option>
              <option value="grouped">grouped (all originals, then translations)</option>
              <option value="side_by_side">side by side (both on one page)</option>
              <option value="translated_only">translated only (no originals)</option>
            </select>
          </label>
          <label className="check">
            <input type="checkbox" checked={verifyMath} onChange={(e) => setVerifyMath(e.target.checked)} />
            verify formulas (+1 model call per page)
          </label>
          <h2>Page control</h2>
          <label>Pages (blank = all)
            <input value={pagesSpec} placeholder="all · 1-3,5,8 · 3- · -4"
                   onChange={(e) => setPagesSpec(e.target.value)} />
          </label>
          <div className="row">
            <label>Unselected pages
              <select value={unprocessed}
                      onChange={(e) => setUnprocessed(e.target.value as "original" | "skip")}>
                <option value="skip">skip / omit (default)</option>
                <option value="original">keep original page</option>
              </select>
            </label>
            <label>Output page size
              <select value={outputPageSize}
                      onChange={(e) => setOutputPageSize(e.target.value as "match" | "a4" | "letter")}>
                <option value="match">match source</option>
                <option value="a4">A4</option>
                <option value="letter">Letter</option>
              </select>
            </label>
          </div>
          <label>Content fit
            <select value={scaleMode}
                    onChange={(e) => setScaleMode(e.target.value as "fill" | "fit")}>
              <option value="fill">fill the page (may enlarge)</option>
              <option value="fit">fit (never enlarge)</option>
            </select>
          </label>
          <label>Parallel pages (speed)
            <input type="number" min={1} max={8} value={pageConcurrency}
                   onChange={(e) => setPageConcurrency(Math.max(1, Math.min(8, Number(e.target.value) || 1)))} />
          </label>
          <label>Figure boxes (quality vs speed)
            <select value={figureMode}
                    onChange={(e) => setFigureMode(e.target.value as "off" | "tight" | "judge")}>
              <option value="off">off — trust the main call (fastest)</option>
              <option value="tight">tight — extra pass for tighter boxes</option>
              <option value="judge">judge — LLM reviews &amp; expands boxes</option>
            </select>
          </label>
          <h2>Page fitting</h2>
          <label>When a page doesn't fit
            <select value={layoutMode} onChange={(e) => setLayoutMode(e.target.value as "single" | "auto")}>
              <option value="auto">keep text size — flow onto extra pages</option>
              <option value="single">shrink to one page (old behaviour)</option>
            </select>
          </label>
          {layoutMode === "auto" && (
            <>
              <label>Figure layout
                <select value={figureLayout}
                        onChange={(e) => setFigureLayout(e.target.value as "flow" | "preserve" | "grid" | "stack")}>
                  <option value="flow">flow (side by side when they fit)</option>
                  <option value="preserve">preserve original arrangement</option>
                  <option value="grid">auto grid (always pack side by side)</option>
                  <option value="stack">stack (one per row)</option>
                </select>
              </label>
              <div className="row">
                <label>Min text size %
                  <input type="number" min={80} max={100} value={Math.round(minPageScale * 100)}
                         onChange={(e) => setMinPageScale(Math.max(0.8, Math.min(1, (Number(e.target.value) || 90) / 100)))} />
                </label>
                <label>Max figure height %
                  <input type="number" min={15} max={60} value={Math.round(figureMaxHeight * 100)}
                         onChange={(e) => setFigureMaxHeight(Math.max(0.15, Math.min(0.6, (Number(e.target.value) || 38) / 100)))} />
                </label>
              </div>
              <div className="row">
                <label>Figure resolution (dpi)
                  <input type="number" min={96} max={600} value={figureDpi}
                         onChange={(e) => setFigureDpi(Math.max(96, Math.min(600, Number(e.target.value) || 300)))} />
                </label>
                <label>Figure images
                  <select value={figureFormat} onChange={(e) => setFigureFormat(e.target.value as "auto" | "png" | "jpeg")}>
                    <option value="auto">auto (jpeg for photos, png for line art)</option>
                    <option value="jpeg">jpeg (smallest)</option>
                    <option value="png">png (lossless)</option>
                  </select>
                </label>
              </div>
            </>
          )}
          <label>Glossary (one <code>source =&gt; target</code> per line)
            <textarea value={glossaryText} placeholder={"Prava => droite\nTalesova teorema => théorème de Thalès"}
                      onChange={(e) => setGlossaryText(e.target.value)} />
          </label>
          <label>Do not translate (comma separated)
            <input value={doNotTranslate} placeholder="Pythagore, Thalès"
                   onChange={(e) => setDoNotTranslate(e.target.value)} />
          </label>
          <label>Additional LLM instructions
            <textarea value={llmInstructions}
                      placeholder="e.g. Use a formal tone. Keep terminology consistent with the glossary."
                      onChange={(e) => setLlmInstructions(e.target.value)} />
          </label>
          {estimate && (
            <div className="muted">
              ≈ <b>${estimate.est_cost_usd.toFixed(4)}</b>
              {estimate.est_cost_low !== undefined && (
                <> (range ${estimate.est_cost_low.toFixed(3)}–${(estimate.est_cost_high ?? 0).toFixed(3)})</>
              )}{" "}
              · {estimate.est_calls} calls · {estimate.pages} pages
              {estimate.est_seconds ? <> · ~{Math.round(estimate.est_seconds / 60)} min</> : null}
              {Number(estimate.assumptions?.learned_calls ?? 0) > 0 && (
                <> · tuned from {String(estimate.assumptions?.learned_calls)} past calls</>
              )}
              {estimate.breakdown && (
                <div className="cost-breakdown">
                  {Object.entries(estimate.breakdown)
                    .filter(([, v]) => v > 0)
                    .map(([k, v]) => (
                      <span key={k}>{k.replace(/_/g, " ")} ${v.toFixed(4)}</span>
                    ))}
                </div>
              )}
            </div>
          )}
          <label className="check">
            <input type="checkbox" checked={bookMode} onChange={(e) => setBookMode(e.target.checked)} />
            book mode — translate in chunks (durable, resumable)
          </label>
          {bookMode && (
            <label>Chunk size (pages)
              <input type="number" min={1} max={200} value={chunkSize}
                     onChange={(e) => setChunkSize(Math.max(1, Number(e.target.value) || 1))} />
            </label>
          )}
          <button disabled={(queueMode ? batchDocs.length === 0 : !doc) || busy || !!running}
                  onClick={onStart}>
            {queueMode
              ? `Start batch (${batchDocs.length} document${batchDocs.length === 1 ? "" : "s"})`
              : bookMode ? "Start book job" : "Start job"}
          </button>
        </section>

        <section className="card">
          <h2>3 · Progress</h2>
          {!job && <p className="muted">No job yet.</p>}
          {job && (
            <>
              <div className="row spread">
                <b>{job.status}</b>
                <span className="muted">{job.mode}</span>
                {running && <button className="ghost" onClick={() => api.cancel(job.id)}>Cancel</button>}
              </div>
              <progress value={running ? undefined : job.progress} max={1} />
              <p className="muted">
                {running ? (
                  <>
                    <b>{stage || "working…"}</b> · {elapsed}s
                    {job.progress > 0 && <> · {Math.round(job.progress * 100)}%</>} · {job.done_pages}/
                    {job.total_pages} pages
                  </>
                ) : (
                  <>
                    {job.done_pages}/{job.total_pages} pages
                    {job.cost_usd > 0 && <> · ${job.cost_usd.toFixed(4)}</>}
                  </>
                )}
              </p>
              {job.error && <div className="error">{job.error}</div>}
              {job.artifacts.map((a) => (
                <a key={a.id} className="download" href={a.download_url}>⬇ {a.filename}</a>
              ))}
              <pre className="log">{events.join("\n")}</pre>
              {job.kind === "batch" && (
                <>
                  <div className="row spread">
                    <b>Documents</b>
                    <span className="muted">{children.filter((c) => c.status === "done").length}/{children.length} done</span>
                  </div>
                  <table className="chunks">
                    <thead><tr><th>#</th><th>Document</th><th>State</th><th>Pages</th><th>Cost</th></tr></thead>
                    <tbody>
                      {children.map((c, i) => (
                        <tr key={c.id}>
                          <td>{i + 1}</td>
                          <td>{docs.find((d) => d.id === c.document_id)?.filename ?? c.document_id}</td>
                          <td>{c.status}{c.error ? " ⚠" : ""}</td>
                          <td>{c.done_pages}/{c.total_pages}</td>
                          <td>{c.cost_usd > 0 ? `$${c.cost_usd.toFixed(4)}` : ""}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </>
              )}
              {job.kind === "book" && (
                <>
                  <div className="row spread">
                    <b>Chunks</b>
                    <span className="muted">
                      {chunks.filter((c) => c.state === "done").length}/{chunks.length} done ·{" "}
                      {chunks.reduce((a, c) => a + (c.done_pages || 0), 0)} pages
                    </span>
                    <span>
                      {running && <button className="ghost" onClick={() => api.pauseBook(job.id)}>Pause</button>}
                      {(job.status === "paused" || job.status === "failed") && (
                      <button className="ghost" onClick={() => api.resumeBook(job.id)}>Resume</button>
                    )}
                      {running && <button className="ghost" onClick={() => api.cancel(job.id)}>Cancel</button>}
                    </span>
                  </div>
                  <table className="chunks">
                    <thead><tr><th>Pages</th><th>Done</th><th>State</th><th>Cost</th><th></th></tr></thead>
                    <tbody>
                      {chunks.map((c) => (
                        <tr key={c.id}>
                          <td>{c.page_from}–{c.page_to}</td>
                          <td>{c.done_pages || 0}/{c.page_to - c.page_from + 1}</td>
                          <td>{c.state}{c.error ? ` ⚠` : ""}</td>
                          <td>{c.cost_usd > 0 ? `$${c.cost_usd.toFixed(4)}` : ""}</td>
                          <td>
                            {(c.state === "failed" || c.state === "done") && (
                              <button className="ghost" onClick={async () => { await api.retryChunk(job.id, c.id); refreshJob(); }}>retry</button>
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </>
              )}
            </>
          )}
          {report && (
            <>
              <h2>Verification</h2>
              {mathWarn > 0
                ? <div className="error">{mathWarn} page(s) flagged by the formula check</div>
                : <p className="muted">No structural issues or formula warnings.</p>}
              <pre className="log">{JSON.stringify(report, null, 1).slice(0, 2000)}</pre>
            </>
          )}
        </section>
      </div>

      {pages.length > 0 && (
        <section className="card">
          <h2>4 · Review</h2>
          <div className="tabs">
            {pages.map((p) => (
              <button key={p.page} className={p.page === active ? "tab on" : "tab"}
                      onClick={() => setActive(p.page)}>p{p.page}</button>
            ))}
          </div>
          {activePage && (
            <div className="review">
              <img src={activePage.image_url} alt={`page ${activePage.page}`} />
              <div className="blocks">
                {activePage.blocks.map((b, i) => (
                  <BlockRow key={b.idx} block={b} first={i === 0}
                            last={i === activePage.blocks.length - 1}
                            onMove={(d) => move(activePage.page, i, d)}
                            onSave={(t) => saveBlock(activePage.page, b.idx, t)} />
                ))}
                <button onClick={() => rebuild(activePage.page)} disabled={busy}>
                  Rebuild this page
                </button>
              </div>
            </div>
          )}
        </section>
      )}
    </div>
  );
}

function BlockRow(props: {
  block: Block;
  first: boolean;
  last: boolean;
  onMove: (d: -1 | 1) => void;
  onSave: (target: string) => void;
}) {
  const { block, first, last, onMove, onSave } = props;
  const [text, setText] = useState(block.target);
  useEffect(() => setText(block.target), [block.target]);
  return (
    <div className="block">
      <div className="block-head">
        <span className={`badge ${block.type}`}>{block.type}</span>
        <span className="muted">{block.source.slice(0, 60)}</span>
        <span className="spacer" />
        <button className="ghost" disabled={first} onClick={() => onMove(-1)}>↑</button>
        <button className="ghost" disabled={last} onClick={() => onMove(1)}>↓</button>
      </div>
      {block.type === "list" && block.items ? (
        <ol className={block.ordered ? "block-list ordered" : "block-list"}>
          {block.items.map((it, i) => (
            <li key={i}>{it.target || it.source || ""}</li>
          ))}
        </ol>
      ) : (
        <>
          <textarea value={text} onChange={(e) => setText(e.target.value)} />
          <button className="ghost" onClick={() => onSave(text)}>Save</button>
        </>
      )}
      {block.latex && <code className="muted">{block.latex.slice(0, 120)}</code>}
      {block.caption && <code className="muted">{block.caption}</code>}
    </div>
  );
}
