"""Progress events and cancellation shared across pipeline stages."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


class Canceled(RuntimeError):
    """Raised when a job is cancelled; the pipeline stops cleanly."""


@dataclass
class Event:
    stage: str  # render | ocr | annotate | build | assemble | verify | done | error
    status: str  # started | progress | ok | skipped | error | done
    base: str = ""
    page: int | None = None
    index: int = 0
    total: int = 0
    message: str = ""
    data: dict[str, Any] = field(default_factory=dict)


Emitter = Callable[[Event], None]


class CancelToken:
    def __init__(self) -> None:
        self._canceled = False

    def cancel(self) -> None:
        self._canceled = True

    @property
    def canceled(self) -> bool:
        return self._canceled

    def check(self) -> None:
        if self._canceled:
            raise Canceled("job cancelled")


def emit(cb: Emitter | None, event: Event) -> None:
    if cb is not None:
        cb(event)
