"""Small concurrency helper used to process pages in parallel."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from typing import TypeVar

T = TypeVar("T")
R = TypeVar("R")


def parallel_map(fn: Callable[[T], R], items: Iterable[T], workers: int) -> list[R]:
    """Map ``fn`` over ``items`` in order, using up to ``workers`` threads.

    ``fn`` is expected to handle its own errors (the pipeline records per-page
    failures), so exceptions here abort the whole batch as usual.
    """
    seq = list(items)
    workers = max(1, int(workers or 1))
    if workers == 1 or len(seq) <= 1:
        return [fn(x) for x in seq]
    with ThreadPoolExecutor(max_workers=min(workers, len(seq))) as ex:
        return list(ex.map(fn, seq))
