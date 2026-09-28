from __future__ import annotations

from ocrtran import cache


def test_file_sha256_stable(tmp_path):
    p = tmp_path / "a.bin"
    p.write_bytes(b"hello")
    assert cache.file_sha256(p) == cache.file_sha256(p)
    p.write_bytes(b"hello2")
    assert cache.file_sha256(p) != cache.file_sha256


def test_ocr_cache_key_varies():
    a = cache.ocr_cache_key("sha", 1, "m", "1")
    assert a == cache.ocr_cache_key("sha", 1, "m", "1")
    assert a != cache.ocr_cache_key("sha", 2, "m", "1")
    assert a != cache.ocr_cache_key("sha", 1, "m2", "1")
    assert a != cache.ocr_cache_key("sha", 1, "m", "2")


def test_load_save_roundtrip(tmp_path):
    p = tmp_path / "x.json"
    assert cache.load_json(p) is None
    cache.save_json(p, {"a": 1})
    assert cache.load_json(p) == {"a": 1}


def test_load_json_corrupt(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json")
    assert cache.load_json(p) is None
