"""Pipeline orchestrator: render -> ocr -> annotate -> build -> assemble -> verify."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import assemble as assemble_mod
from . import guard, latex, ocr, verify
from .annotate import run_annotate
from .config import PipelineConfig
from .events import Canceled, CancelToken, Emitter, Event, emit
from .providers import Provider, build_provider


@dataclass
class PipelineResult:
    ocr: dict[str, list[dict]] = field(default_factory=dict)
    outputs: dict[str, Path] = field(default_factory=dict)
    report: dict = field(default_factory=dict)
    usage: dict = field(default_factory=dict)
    canceled: bool = False

    @property
    def ok(self) -> bool:
        return not self.canceled and bool(self.outputs)


class Pipeline:
    """Runs the whole job. One instance per job; reusable per stage.

    ``provider`` can be injected (tests, alternative backends); otherwise it is
    built from the config using BYOK *or* the env-var fallback.
    """

    def __init__(
        self,
        cfg: PipelineConfig,
        provider: Provider | None = None,
        api_key: str | None = None,
        on_event: Emitter | None = None,
        cancel: CancelToken | None = None,
    ) -> None:
        self.cfg = cfg
        self._provider = provider
        self._api_key = api_key
        self.on_event = on_event
        self.cancel = cancel or CancelToken()

    @property
    def provider(self) -> Provider:
        """Created lazily so stages that need no model (build/assemble) work
        without a key."""
        if self._provider is None:
            self._provider = build_provider(self.cfg, self._api_key)
        return self._provider

    # -- stages --------------------------------------------------------
    def run_ocr(self) -> dict[str, list[dict]]:
        return ocr.run_ocr(self.cfg, self.provider, self.on_event, self.cancel)

    def run_annotate(self, results: dict | None = None) -> dict:
        return run_annotate(self.cfg, self.provider, results, self.on_event, self.cancel)

    def run_build(self, results: dict | None = None) -> dict:
        return latex.run_build(self.cfg, results, self.on_event, self.cancel)

    def run_assemble(self) -> dict[str, Path]:
        return assemble_mod.assemble(self.cfg, self.on_event, self.cancel)

    # -- full run ------------------------------------------------------
    def run(self) -> PipelineResult:
        result = PipelineResult()
        try:
            result.ocr = self.run_ocr()
            self.run_annotate(result.ocr)
            self.run_build(result.ocr)
            if self.cfg.error_guard == "auto":
                guard.run_guard(self.cfg, self.provider, result.ocr, self.on_event, self.cancel)
            if self.cfg.verify_math:
                verify.run_math_check(self.cfg, self.provider, result.ocr, self.on_event, self.cancel)
            result.outputs = self.run_assemble()
            result.report = verify.run_verify(self.cfg, result.ocr, result.outputs)
            snap = getattr(self.provider, "usage_snapshot", None)
            result.usage = snap() if callable(snap) else {"calls": 0}
            emit(
                self.on_event,
                Event("done", "done", data={"outputs": {k: str(v) for k, v in result.outputs.items()}}),
            )
        except Canceled:
            result.canceled = True
            emit(self.on_event, Event("done", "canceled"))
        return result
