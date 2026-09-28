from __future__ import annotations

import json

import pytest

from ocrtran.config import ConfigError, PipelineConfig


def test_defaults_and_output_mode():
    cfg = PipelineConfig(sources=["a.pdf"], bilingual=True, combine="interleave")
    assert cfg.output_mode == "interleave"
    cfg.bilingual = False
    assert cfg.output_mode == "translated_only"


def test_combine_modes():
    for mode in ("interleave", "grouped", "side_by_side"):
        cfg = PipelineConfig(sources=["a.pdf"], combine=mode)
        assert cfg.output_mode == mode


def test_combine_invalid():
    cfg = PipelineConfig(sources=["a.pdf"], combine="nope")
    with pytest.raises(ConfigError):
        _ = cfg.output_mode


def test_from_dict_unknown_key():
    with pytest.raises(ConfigError):
        PipelineConfig.from_dict({"sources": ["a.pdf"], "bogus": 1})


def test_from_dict_mode_compat():
    cfg = PipelineConfig.from_dict({"sources": ["a.pdf"], "mode": "translated-only"})
    assert cfg.output_mode == "translated_only"
    cfg2 = PipelineConfig.from_dict({"sources": ["a.pdf"], "mode": "grouped"})
    assert cfg2.output_mode == "grouped"


def test_roundtrip(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"sources": ["a.pdf"], "target_lang": "French"}))
    cfg = PipelineConfig.from_json(p)
    assert cfg.target_lang == "French"
    assert PipelineConfig.from_dict(cfg.to_dict()).target_lang == "French"


def test_validate_requires_sources():
    with pytest.raises(ConfigError):
        PipelineConfig(sources=[]).validate()


def test_validate_page_options():
    with pytest.raises(ConfigError):
        PipelineConfig(sources=["a.pdf"], output_page_size="b5").validate()
    with pytest.raises(ConfigError):
        PipelineConfig(sources=["a.pdf"], scale_mode="stretch").validate()
    with pytest.raises(ConfigError):
        PipelineConfig(sources=["a.pdf"], unprocessed="delete").validate()


def test_resolve_sources(tmp_path):
    (tmp_path / "a.pdf").write_bytes(b"x")
    (tmp_path / "b.pdf").write_bytes(b"x")
    cfg = PipelineConfig(sources=[str(tmp_path / "*.pdf")])
    assert len(cfg.resolve_sources()) == 2


def test_resolve_api_key_env(monkeypatch):
    cfg = PipelineConfig(sources=["a.pdf"], api_key_env="TEST_KEY")
    monkeypatch.delenv("TEST_KEY", raising=False)
    with pytest.raises(ConfigError):
        cfg.resolve_api_key()
    monkeypatch.setenv("TEST_KEY", "sekret")
    assert cfg.resolve_api_key() == "sekret"
    assert cfg.resolve_api_key("byok") == "byok"  # BYOK wins
