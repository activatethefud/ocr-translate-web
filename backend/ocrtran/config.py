"""Pipeline configuration.

Mirrors the ``ocr-translate`` skill's JSON config and adds output options
(``bilingual`` / ``combine``). Immutable-ish dataclass, easy to serialise.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

COMBINE_MODES = ("interleave", "grouped", "side_by_side")


class ConfigError(ValueError):
    pass


@dataclass
class PipelineConfig:
    # --- provider ---
    api_base: str = "https://api.deepseek.com/chat/completions"
    api_key_env: str = "DS_KEY"
    model: str = "deepseek-flash"
    timeout: int = 600
    max_tokens: int = 16000
    prompt_version: str = "2"

    # --- input ---
    sources: list[str] = field(default_factory=list)
    dpi: int = 150
    max_px: int = 1800
    workdir: str = "ocr_work"
    cache_dir: str = ""  # shared OCR cache root (falls back to workdir)
    output_name: str = ""  # optional output document name (default is derived)

    # --- translation / typesetting ---
    source_lang: str = "auto"
    target_lang: str = "English"
    concurrency: int = 4  # pages translated in parallel (1 = sequential)
    glossary: list[dict[str, str]] = field(default_factory=list)
    do_not_translate: list[str] = field(default_factory=list)
    llm_instructions: str = ""
    verify_math: bool = False
    font_main: str = "Noto Serif"
    linebreak_locale: str = ""
    extra_preamble: str = ""
    text_width: str = "16.5cm"
    figure_px: int = 1800
    figure_pad: float = 0.06  # pad figure boxes by this fraction (avoids clipping)
    skip_blank_pages: bool = True  # don't translate (near-)blank pages
    blank_threshold: float = 0.002  # ink ratio below which a page is "blank"
    boundaries: list[dict] = field(default_factory=list)  # [{"page": n, "title": "..."}]
    figure_mode: str = "tight"  # off | tight | judge (extra model calls for better boxes)

    # --- assembly ---
    margin_pt: int = 24
    max_scale: float = 0.0  # 0 = no cap (enlarge to fill the page)
    bilingual: bool = True
    combine: str = "interleave"  # interleave | grouped | side_by_side

    # --- page control ---
    pages: str = "all"  # "all" | "1-5" | "2,4,7-9" | "3-" | "-4"
    unprocessed: str = "skip"  # skip | original (what to do with unselected pages)
    output_page_size: str = "match"  # match | a4 | letter
    scale_mode: str = "fill"  # fill (enlarge to fill) | fit (never upscale)

    # --- behaviour ---
    force: bool = False

    # ------------------------------------------------------------------
    @property
    def output_mode(self) -> str:
        """One of: interleave | grouped | side_by_side | translated_only."""
        if not self.bilingual:
            return "translated_only"
        if self.combine not in COMBINE_MODES:
            raise ConfigError(f"combine must be one of {COMBINE_MODES}, got {self.combine!r}")
        return self.combine

    @property
    def workpath(self) -> Path:
        return Path(self.workdir)

    # ------------------------------------------------------------------
    @classmethod
    def from_dict(cls, data: dict) -> PipelineConfig:
        data = dict(data or {})
        # backward/UX compatibility with the skill's "mode" key
        mode = data.pop("mode", None)
        if mode is not None:
            if mode == "translated-only":
                data.setdefault("bilingual", False)
            elif mode in COMBINE_MODES:
                data.setdefault("combine", mode)
        known = {f.name for f in fields(cls)}
        unknown = set(data) - known
        if unknown:
            raise ConfigError(f"unknown config keys: {sorted(unknown)}")
        cfg = cls(**data)
        cfg.validate()
        return cfg

    @classmethod
    def from_json(cls, path: str | Path) -> PipelineConfig:
        return cls.from_dict(json.loads(Path(path).read_text()))

    def to_dict(self) -> dict:
        return asdict(self)

    def validate(self) -> None:
        if not self.sources:
            raise ConfigError("no sources configured")
        if self.dpi <= 0:
            raise ConfigError("dpi must be positive")
        if self.output_mode not in COMBINE_MODES + ("translated_only",):
            raise ConfigError(f"bad output mode {self.output_mode!r}")
        if self.unprocessed not in ("original", "skip"):
            raise ConfigError("unprocessed must be 'original' or 'skip'")
        if self.output_page_size not in ("match", "a4", "letter"):
            raise ConfigError("output_page_size must be match, a4 or letter")
        if self.scale_mode not in ("fill", "fit"):
            raise ConfigError("scale_mode must be 'fill' or 'fit'")
        if self.figure_pad < 0:
            raise ConfigError("figure_pad must be >= 0")
        if self.figure_mode not in ("off", "tight", "judge"):
            raise ConfigError("figure_mode must be off, tight or judge")
        if self.concurrency < 1:
            raise ConfigError("concurrency must be >= 1")

    # ------------------------------------------------------------------
    def resolve_sources(self) -> list[str]:
        """Expand globs, keep order, drop duplicates, verify existence."""
        import glob

        out: list[str] = []
        for src in self.sources:
            matches = sorted(glob.glob(src)) if any(c in src for c in "*?[") else [src]
            if not matches:
                raise ConfigError(f"source not found: {src}")
            for m in matches:
                if m not in out:
                    out.append(m)
        return out

    def resolve_api_key(self, override: str | None = None) -> str:
        """BYOK-first: an explicit key wins; otherwise fall back to the env var
        (used for local testing)."""
        key = override or os.environ.get(self.api_key_env)
        if not key:
            raise ConfigError(
                f"no API key: pass one explicitly (bring-your-own-key) or set ${self.api_key_env}"
            )
        return key
