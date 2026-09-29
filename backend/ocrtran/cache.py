"""Content-addressed caching so re-runs and single-page edits are cheap.

The OCR result for a page is keyed by
``sha256(source file) + page + model + prompt_version`` — never re-pay for a
page whose inputs haven't changed.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def file_sha256(path: str | Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while block := fh.read(chunk):
            h.update(block)
    return h.hexdigest()


def ocr_cache_key(doc_sha: str, page: int, model: str, prompt_version: str) -> str:
    raw = f"{doc_sha}:{page}:{model}:{prompt_version}"
    return hashlib.sha256(raw.encode()).hexdigest()


def cache_path(workdir: str | Path, key: str) -> Path:
    return Path(workdir) / "cache" / "ocr" / f"{key}.json"


def load_json(path: str | Path):
    p = Path(path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def save_json(path: str | Path, obj) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=1))


def prompt_sig(prompt_version: str, glossary=None, do_not_translate=None, instructions=None) -> str:
    """Cache signature: the prompt version, plus a hash of any custom prompt content.

    Keeps existing caches valid for default runs (empty glossary/instructions) while
    guaranteeing that changing the glossary / do-not-translate / instructions is
    **not** silently served from a stale cache.
    """
    extra = json.dumps(
        [glossary or [], do_not_translate or [], (instructions or "").strip()],
        sort_keys=True,
        ensure_ascii=False,
    )
    if extra == '[[], [], ""]':
        return prompt_version
    return f"{prompt_version}-{hashlib.sha256(extra.encode()).hexdigest()[:10]}"
