"""Learn real cost factors from the `usage` table (lightweight, cached).

Every finished job records total prompt/completion tokens and call count per model.
We aggregate those into *learned* per-call averages so the price predictor converges
to reality over time. One cheap aggregate query per TTL window; nothing per request.
"""

from __future__ import annotations

import time

from sqlalchemy import func, select

from . import db

TTL = 30.0  # seconds
_CACHE: dict = {"at": 0.0, "data": {}}


def _aggregate(s) -> dict:
    rows = s.execute(
        select(
            db.Usage.model,
            func.sum(db.Usage.prompt_tokens),
            func.sum(db.Usage.completion_tokens),
            func.sum(db.Usage.calls),
        ).group_by(db.Usage.model)
    ).all()
    out: dict = {}
    for model, prompt, completion, calls in rows:
        calls = int(calls or 0)
        if not model or calls <= 0:
            continue
        out[model] = {
            "calls": calls,
            "input_tokens_per_call": (prompt or 0) / calls,
            "output_tokens_per_call": (completion or 0) / calls,
        }
    return out


def model_stats(refresh: bool = False) -> dict:
    now = time.time()
    if refresh or now - _CACHE["at"] > TTL:
        s = db.get_session()
        try:
            _CACHE["data"] = _aggregate(s)
        finally:
            s.close()
        _CACHE["at"] = now
    return _CACHE["data"]


def learned_for(model: str) -> dict | None:
    return model_stats().get(model)


def reset_cache() -> None:
    _CACHE["at"] = 0.0
    _CACHE["data"] = {}
