from __future__ import annotations

import json
from pathlib import Path

from ocrtran import cache, guard, latex, paths
from ocrtran.config import PipelineConfig


class _G:
    def __init__(self, action, patches=None, raw=None):
        self.action = action
        self.patches = patches or []
        self.raw = raw
        self.calls = 0

    def text(self, prompt, max_tokens=None):
        self.calls += 1
        if self.raw is not None:
            return self.raw
        return json.dumps({"action": self.action, "reason": "because", "patches": self.patches})


def _cfg(tmp_path, tiny_pdf, **kw):
    return PipelineConfig(sources=[str(tiny_pdf)], workdir=str(tmp_path / "w"), target_lang="French", **kw)


def test_decide_parses_action_and_patches():
    d = guard.decide(
        _G("repair", [{"index": 1, "latex": r"\text{x}"}]),
        page=4,
        blocks=[],
        error="! err",
        target_lang="French",
        output_mode="interleave",
    )
    assert d["action"] == "repair" and d["patches"] == [{"index": 1, "latex": r"\text{x}"}]


def test_decide_default_action_depends_on_mode():
    bad = _G("x", raw="not json")
    assert (
        guard.decide(bad, page=1, blocks=[], error="e", target_lang="French", output_mode="translated_only")[
            "action"
        ]
        == "skip"
    )
    assert (
        guard.decide(bad, page=1, blocks=[], error="e", target_lang="French", output_mode="interleave")[
            "action"
        ]
        == "original"
    )


def test_guard_off_makes_no_calls(tmp_path, tiny_pdf):
    class _NoCall:
        def text(self, *a, **k):
            raise AssertionError("guard must not call the model when off")

    cfg = _cfg(tmp_path, tiny_pdf, error_guard="off")
    entry = {"page": 1, "blocks": [{"type": "prose"}], "build_error": "! boom"}
    guard.run_guard(cfg, _NoCall(), {tiny_pdf.stem: [entry]})
    assert "guard" not in entry


def test_guard_skip_and_original_are_saved(tmp_path, tiny_pdf):
    cfg = _cfg(tmp_path, tiny_pdf, error_guard="auto", combine="translated_only", bilingual=False)
    entry = {"page": 3, "blocks": [{"type": "prose"}], "error": "empty model response"}
    guard.run_guard(cfg, _G("skip"), {tiny_pdf.stem: [entry]})
    assert entry["guard"]["action"] == "skip"
    saved = cache.load_json(paths.ocr_json(cfg.workdir, tiny_pdf.stem))
    assert saved[0]["guard"]["action"] == "skip"


def test_guard_repair_patches_blocks_and_clears_error(tmp_path, tiny_pdf, monkeypatch):
    monkeypatch.setattr(latex, "compile_entry", lambda *a, **k: ([Path("p.pdf")], None))
    cfg = _cfg(tmp_path, tiny_pdf, error_guard="auto")
    entry = {"page": 1, "blocks": [{"type": "table", "latex": "BROKEN"}], "build_error": "! LaTeX Error"}
    guard.run_guard(cfg, _G("repair", [{"index": 0, "latex": "FIXED"}]), {tiny_pdf.stem: [entry]})
    assert entry["blocks"][0]["latex"] == "FIXED"
    assert "build_error" not in entry
    assert entry["guard"]["action"] == "repair"


def test_guard_repair_falls_back_when_it_still_fails(tmp_path, tiny_pdf, monkeypatch):
    monkeypatch.setattr(latex, "compile_entry", lambda *a, **k: ([], "! still broken"))
    cfg = _cfg(tmp_path, tiny_pdf, error_guard="auto", combine="translated_only", bilingual=False)
    entry = {"page": 1, "blocks": [{"type": "table", "latex": "BROKEN"}], "build_error": "! err"}
    guard.run_guard(cfg, _G("repair", [{"index": 0, "latex": "STILL"}]), {tiny_pdf.stem: [entry]})
    assert entry["guard"]["action"] == "skip"  # default for translated_only
