"""Page selection helpers.

A page spec is a comma-separated list of 1-based pages and ranges, e.g.
``"all"``, ``"3"``, ``"1-5"``, ``"2,4,7-9"``, ``"3-"`` (3 to end), ``"-4"``.
"""

from __future__ import annotations


class PageSpecError(ValueError):
    pass


def parse_page_spec(spec: str | None, n_pages: int) -> list[int]:
    """Return a sorted, de-duplicated list of valid page numbers (1-based)."""
    text = (spec or "all").strip().lower()
    if text in ("", "all", "*"):
        return list(range(1, n_pages + 1))
    out: set[int] = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            if "-" in part:
                a_str, b_str = part.split("-", 1)
                a = int(a_str) if a_str.strip() else 1
                b = int(b_str) if b_str.strip() else n_pages
                if a > b:
                    a, b = b, a
                out.update(p for p in range(a, b + 1) if 1 <= p <= n_pages)
            else:
                p = int(part)
                if 1 <= p <= n_pages:
                    out.add(p)
        except ValueError as exc:
            raise PageSpecError(f"invalid page spec: {part!r}") from exc
    if not out:
        raise PageSpecError(f"page spec {spec!r} selects no pages (1..{n_pages})")
    return sorted(out)
