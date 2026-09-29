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


def test_validate_figure_pad():
    with pytest.raises(ConfigError):
        PipelineConfig(sources=["a.pdf"], figure_pad=-0.1).validate()


def test_validate_concurrency():
    with pytest.raises(ConfigError):
        PipelineConfig(sources=["a.pdf"], concurrency=0).validate()


def test_from_dict_page_control():
    cfg = PipelineConfig.from_dict(
        {
            "sources": ["a.pdf"],
            "pages": "1-3",
            "unprocessed": "skip",
            "output_page_size": "a4",
            "scale_mode": "fit",
            "concurrency": 6,
            "figure_pad": 0.1,
        }
    )
    assert cfg.pages == "1-3"
    assert cfg.unprocessed == "skip"
    assert cfg.output_page_size == "a4"
    assert cfg.scale_mode == "fit"
    assert cfg.concurrency == 6
    assert cfg.figure_pad == 0.1


def test_to_dict_contains_page_control():
    cfg = PipelineConfig(sources=["a.pdf"], pages="2,4")
    d = cfg.to_dict()
    assert d["pages"] == "2,4"
    assert "output_page_size" in d and "scale_mode" in d and "figure_pad" in d


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


def test_validate_figure_mode():
    PipelineConfig(sources=["a.pdf"]).validate()  # default ok
    assert PipelineConfig(sources=["a.pdf"]).figure_mode == "tight"
    with pytest.raises(ConfigError):
        PipelineConfig(sources=["a.pdf"], figure_mode="magic").validate()


def test_cache_dir_field():
    cfg = PipelineConfig(sources=["a.pdf"], cache_dir="/tmp/shared")
    assert cfg.cache_dir == "/tmp/shared"
    assert cfg.to_dict()["cache_dir"] == "/tmp/shared"


def test_default_unprocessed_is_skip():
    cfg = PipelineConfig(sources=["a.pdf"])
    assert cfg.unprocessed == "skip"
    assert cfg.to_dict()["unprocessed"] == "skip"


def test_combine_translated_only_direct():
    """translated_only can be chosen via ``combine`` while ``bilingual`` stays True."""
    cfg = PipelineConfig(sources=["a.pdf"], combine="translated_only", bilingual=True)
    assert cfg.output_mode == "translated_only"
    assert cfg.validate() is None
    cfg2 = PipelineConfig.from_dict({"sources": ["a.pdf"], "combine": "translated_only"})
    assert cfg2.output_mode == "translated_only"
