"""Tolerant JSON extraction helpers (shared, avoids import cycles)."""

from __future__ import annotations

import json
import re


def parse_json(txt: str | None) -> dict | None:
    """Parse JSON that models sometimes wrap in fences or surrounding prose."""
    if not txt:
        return None
    txt = txt.strip()
    txt = re.sub(r"^```[a-zA-Z]*\s*", "", txt)
    txt = re.sub(r"\s*```$", "", txt)
    i, j = txt.find("{"), txt.rfind("}")
    if i < 0 or j < 0:
        return None
    try:
        return json.loads(txt[i : j + 1])
    except json.JSONDecodeError:
        return None
