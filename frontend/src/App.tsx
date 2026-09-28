import { useEffect, useMemo, useRef, useState } from "react";
import {
  api,
  type Block,
  type DocumentOut,
  type Estimate,
  type JobCreate,
  type JobOut,
  type ModelInfo,
  type Page,
  type Report,
  type Usage,
} from "./api";

type Combine = "interleave" | "grouped" | "side_by_side";

const LANGS = [
  "Serbian", "Croatian", "Bosnian", "English", "French", "German", "Spanish",
  "Italian", "Russian", "Simplified Chinese", "Japanese", "Korean", "Arabic",
];

function parseGlossary(text: string): { source: string; target: string }[] {
  return text
    .split("\n")
    .map((l) => l.split(/=>|->/))
    .filter((p) => p.length === 2 && p[0].trim() && p[1].trim())
    .map((p) => ({ source: p[0].trim(), target: p[1].trim() }));
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
  const [apiKey, setApiKey] = useState(localStorage.getItem("ocrtran_key") || "");
  const [sourceLang, setSourceLang] = useState("Serbian");
  const [targetLang, setTargetLang] = useState("French");
  const [model, setModel] = useState("deepseek-flash");
  const [fontMain, setFontMain] = useState("Noto Serif");
  const [linebreak, setLinebreak] = useState("");
  const [bilingual, setBilingual] = useState(true);
  const [combine, setCombine] = useState<Combine>("interleave");
  const [glossaryText, setGlossaryText] = useState("");
  const [doNotTranslate, setDoNotTranslate] = useState("");
  const [verifyMath, setVerifyMath] = useState(false);

  const esRef = useRef<EventSource | null>(null);

  useEffect(() => {
    api.listDocuments().then(setDocs).catch((e) => setError(String(e)));
    api.usage().then(setUsage).catch(() => undefined);
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
    api.estimate(doc.id, model, verifyMath).then(setEstimate).catch(() => setEstimate(null));
  }, [doc?.id, model, verifyMath]);

  const running = job && (job.status === "queued" || job.status === "running");
  const activePage = useMemo(() => pages.find((p) => p.page === active), [pages, active]);

  async function refreshJob() {
    if (!job) return;
    const j = await api.getJob(job.id);
    setJob(j);
    if (j.status !== "queued") setPages(await api.pages(j.id));
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
      setEvents((p) => [...p.slice(-300), `${d.stage}/${d.status} p${d.page ?? ""} ${d.message ?? ""}`]);
    });
    es.addEventListener("end", () => {
      es.close();
      esRef.current = null;
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
      setDocs(await api.listDocuments());
      setJob(null); setPages([]); setEvents([]); setReport(null);
    } catch (e) { setError(String(e)); } finally { setBusy(false); }
  }

  async function onStart() {
    if (!doc) return;
    setError(""); setBusy(true);
    localStorage.setItem("ocrtran_key", apiKey);
    const body: JobCreate = {
      source_lang: sourceLang, target_lang: targetLang, model,
      api_key: apiKey || undefined, font_main: fontMain,
      linebreak_locale: linebreak, bilingual, combine,
      glossary: parseGlossary(glossaryText),
      do_not_translate: doNotTranslate.split(",").map((s) => s.trim()).filter(Boolean),
      verify_math: verifyMath,
    };
    if (!bilingual) body.combine = "interleave";
    try {
      const j = await api.createJob(doc.id, body);
      setJob(j); setEvents([]); setPages([]); setReport(null);
    } catch (e) { setError(String(e)); } finally { setBusy(false); }
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
          {doc && (
            <p className="muted">
              {doc.n_pages} pages · {Math.round(doc.page_w)}×{Math.round(doc.page_h)} pt · {doc.kind}
            </p>
          )}

          <h2>2 · Translate</h2>
          <label>API key (BYOK, not stored)
            <input type="password" value={apiKey} placeholder="sk-…"
                   onChange={(e) => setApiKey(e.target.value)} />
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
            <label>From
              <select value={sourceLang} onChange={(e) => setSourceLang(e.target.value)}>
                <option>auto</option>
                {LANGS.map((l) => <option key={l}>{l}</option>)}
              </select>
            </label>
            <label>To
              <select value={targetLang} onChange={(e) => setTargetLang(e.target.value)}>
                {LANGS.map((l) => <option key={l}>{l}</option>)}
              </select>
            </label>
          </div>
          <div className="row">
            <label>Font<input value={fontMain} onChange={(e) => setFontMain(e.target.value)} /></label>
            <label>Line-break locale
              <input value={linebreak} placeholder="zh/ja/ko or blank"
                     onChange={(e) => setLinebreak(e.target.value)} />
            </label>
          </div>
          <label className="check">
            <input type="checkbox" checked={bilingual} onChange={(e) => setBilingual(e.target.checked)} />
            keep original pages (bilingual)
          </label>
          {bilingual && (
            <label>Combine
              <select value={combine} onChange={(e) => setCombine(e.target.value as Combine)}>
                <option value="interleave">interleave (original, translation, …)</option>
                <option value="grouped">grouped (all originals, then translations)</option>
                <option value="side_by_side">side by side (both on one page)</option>
              </select>
            </label>
          )}
          <label className="check">
            <input type="checkbox" checked={verifyMath} onChange={(e) => setVerifyMath(e.target.checked)} />
            verify formulas (+1 model call per page)
          </label>
          <label>Glossary (one <code>source =&gt; target</code> per line)
            <textarea value={glossaryText} placeholder={"Prava => droite\nTalesova teorema => théorème de Thalès"}
                      onChange={(e) => setGlossaryText(e.target.value)} />
          </label>
          <label>Do not translate (comma separated)
            <input value={doNotTranslate} placeholder="Pythagore, Thalès"
                   onChange={(e) => setDoNotTranslate(e.target.value)} />
          </label>
          {estimate && (
            <p className="muted">≈ ${estimate.est_cost_usd.toFixed(4)} · {estimate.est_calls} model calls
              ({estimate.pages} pages, {model})</p>
          )}
          <button disabled={!doc || busy || !!running} onClick={onStart}>Start job</button>
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
              <progress value={job.progress} max={1} />
              <p className="muted">{job.done_pages}/{job.total_pages} pages
                {job.cost_usd > 0 && <> · ${job.cost_usd.toFixed(4)}</>}</p>
              {job.error && <div className="error">{job.error}</div>}
              {job.artifacts.map((a) => (
                <a key={a.id} className="download" href={a.download_url}>⬇ {a.filename}</a>
              ))}
              <pre className="log">{events.join("\n")}</pre>
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
      <textarea value={text} onChange={(e) => setText(e.target.value)} />
      <button className="ghost" onClick={() => onSave(text)}>Save</button>
      {block.latex && <code className="muted">{block.latex.slice(0, 120)}</code>}
    </div>
  );
}
