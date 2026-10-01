"""LLM error guard: when a page fails to typeset, ask the model what to do.

A page can "silently fail" (empty OCR, LaTeX error, no output). Without a guard it
is emitted as the untranslated original regardless of the output mode. With
``error_guard="auto"`` the failing page's **error log** and blocks are sent to the
model, which chooses:

* ``repair``  - return corrected LaTeX for the offending block(s); we rebuild it;
* ``original``- keep the untranslated source page (explicit, not silent);
* ``skip``    - drop the page from the output entirely.

The decision is stored on the page entry (``entry["guard"]``) and saved to
``ocr.json`` so ``assemble`` can honour it (e.g. actually omit a ``skip`` page).
"""

from __future__ import annotations

from pathlib import Path

from . import cache, latex, paths
from .config import PipelineConfig
from .events import CancelToken, Emitter, Event, emit
from .jsonutil import parse_json
from .providers import Provider

ACTIONS = ("repair", "original", "skip")

GUARD_PROMPT = """A translated page failed to typeset. Decide what to do with it.

Page: {page}
Target language: {tgt}
Output mode: {mode}
Error / log:
{log}

Blocks on the page:
{blocks}

Choose exactly ONE action:
- "repair": the LaTeX is broken and you can fix it. Provide corrected "latex" strings
  for the offending blocks (by their index) in "patches".
- "original": keep the untranslated source page in the output.
- "skip": drop this page from the output.

Return STRICT JSON:
{{"action":"repair"|"original"|"skip","reason":"...",
  "patches":[{{"index":0,"target":"..."}} or {{"index":1,"latex":"..."}}]}}

For a repair, give the corrected content for each offending block: use "target" for a
prose/heading/list/quote/theorem block and "latex" for a math/table block.

Rules:
- NEVER translate or change mathematics; a patch may only fix LaTeX structure/escaping
  (unbalanced braces, bad table column count, stray commands, missing \\text{{}}).
- If you cannot repair it confidently, choose "original" (or "skip" when the output mode
  is translated-only and showing the source page would be wrong).
- Return only JSON.
"""


def default_action(mode: str) -> str:
    return "skip" if mode == "translated_only" else "original"


def _summarize(blocks: list[dict], limit: int = 1800) -> str:
    lines: list[str] = []
    total = 0
    for i, b in enumerate(blocks or []):
        t = b.get("type")
        if t in ("math", "table"):
            body = (b.get("latex") or "")[:400]
        else:
            body = ((b.get("source") or "") + " => " + (b.get("target") or ""))[:220]
        line = f"{i}: {t}: {body}"
        lines.append(line)
        total += len(line)
        if total > limit:
            break
    return "\n".join(lines)


def decide(
    provider: Provider,
    *,
    page: int,
    blocks: list[dict],
    error: str,
    target_lang: str,
    output_mode: str,
) -> dict:
    """Ask the model what to do with a failed page. Always returns a valid action."""
    note = (
        "\nNOTE: this page produced NO text blocks - it is probably a full-page image "
        '(cover/photo). Prefer "original" so the image is not lost.\n'
        if not blocks
        else ""
    )
    prompt = GUARD_PROMPT.format(
        page=page,
        tgt=target_lang,
        mode=output_mode,
        log=(error or "")[-2500:],
        blocks=_summarize(blocks) + note,
    )
    try:
        raw = provider.text(prompt, max_tokens=4000) or ""
    except Exception as exc:  # noqa: BLE001 - a guard failure must not fail the job
        return {
            "action": default_action(output_mode),
            "reason": f"guard: provider error: {exc}"[:200],
            "patches": [],
        }
    obj = parse_json(raw)
    if not isinstance(obj, dict) or obj.get("action") not in ACTIONS:
        return {"action": default_action(output_mode), "reason": "guard: unparsable response", "patches": []}
    patches: list[dict] = []
    for p in obj.get("patches") or []:
        try:
            patch = {"index": int(p["index"])}
            if p.get("target") is not None:
                patch["target"] = str(p["target"])
            if p.get("latex") is not None:
                patch["latex"] = str(p["latex"])
            if "target" in patch or "latex" in patch:
                patches.append(patch)
        except (KeyError, TypeError, ValueError):
            continue
    return {"action": obj["action"], "reason": str(obj.get("reason") or "")[:400], "patches": patches}


def run_guard(
    cfg: PipelineConfig,
    provider: Provider,
    results: dict[str, list[dict]],
    on_event: Emitter | None = None,
    cancel: CancelToken | None = None,
) -> dict[str, list[dict]]:
    """Review failed pages and apply the model's decision. Saves ``ocr.json``."""
    if cfg.error_guard != "auto":
        return results
    cancel = cancel or CancelToken()
    srcmap = {Path(s).stem: s for s in cfg.resolve_sources()}
    for base, entries in results.items():
        source_pdf = srcmap.get(base)
        if not source_pdf:
            continue
        for entry in entries:
            cancel.check()
            error = entry.get("build_error") or entry.get("error")
            if not error:
                continue
            page = int(entry.get("page") or 0)
            decision = decide(
                provider,
                page=page,
                blocks=entry.get("blocks", []),
                error=error,
                target_lang=cfg.target_lang,
                output_mode=cfg.output_mode,
            )
            action = decision["action"]
            emit(
                on_event,
                Event(
                    "guard", "decided", base=base, page=page, message=f"{action}: {decision['reason'][:160]}"
                ),
            )
            if action == "repair" and decision["patches"]:
                for patch in decision["patches"]:
                    i = patch["index"]
                    if not (0 <= i < len(entry.get("blocks", []))):
                        continue
                    blk = entry["blocks"][i]
                    if patch.get("latex") is not None and blk.get("type") in ("math", "table"):
                        blk["latex"] = patch["latex"]
                    elif patch.get("target") is not None:
                        blk["target"] = patch["target"]
                    elif patch.get("latex") is not None:
                        blk["latex"] = patch["latex"]
                pdfs, err = latex.compile_entry(cfg, base, source_pdf, entry, on_event, cancel)
                if pdfs and not err:
                    entry.pop("build_error", None)
                    entry.pop("error", None)
                    entry["guard"] = {"action": "repair", "reason": decision["reason"]}
                    emit(on_event, Event("guard", "repaired", base=base, page=page))
                    continue
                action = default_action(cfg.output_mode)
                decision["reason"] = "repair failed: " + decision["reason"]
            entry["guard"] = {"action": action, "reason": decision["reason"]}
        cache.save_json(paths.ocr_json(cfg.workdir, base), entries)
    return results
