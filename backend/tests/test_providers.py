from __future__ import annotations

import threading

import pytest

from ocrtran.providers import OpenAICompatibleProvider, ProviderError


class FakeResponse:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.posts = 0
        self.last_json = None
        self.last_url = None

    def post(self, url, headers=None, json=None, timeout=None):
        self.posts += 1
        self.last_url = url
        self.last_json = json
        r = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(r, Exception):
            raise r
        return r

    def get(self, url, headers=None, timeout=None):
        self.last_url = url
        return FakeResponse({"data": [{"id": "vision-1", "name": "V1", "input_modalities": ["image"]}]})


def _ok(content="hi", pt=10, ct=5):
    return FakeResponse(
        {
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": pt, "completion_tokens": ct},
        }
    )


def _err(msg="boom"):
    return FakeResponse({"error": {"message": msg}})


def test_usage_accumulates():
    s = FakeSession([_ok(pt=10, ct=5)])
    p = OpenAICompatibleProvider("http://x/chat/completions", "m", "k", session=s, max_retries=1)
    assert p.text("hello") == "hi"
    assert p.usage == {"prompt_tokens": 10, "completion_tokens": 5, "calls": 1}


def test_retry_then_success():
    s = FakeSession([_err(), _ok("second")])
    p = OpenAICompatibleProvider("http://x/chat/completions", "m", "k", session=s, max_retries=3, backoff=0)
    assert p.text("hello") == "second"
    assert s.posts == 2
    assert p.usage["calls"] == 1


def test_provider_error_after_retries():
    s = FakeSession([_err()])
    p = OpenAICompatibleProvider("http://x/chat/completions", "m", "k", session=s, max_retries=2, backoff=0)
    with pytest.raises(ProviderError):
        p.text("hello")
    assert s.posts == 2


def test_vision_sends_image_content(tmp_path):
    img = tmp_path / "p.png"
    img.write_bytes(b"\x89PNG\r\n")
    s = FakeSession([_ok("ok")])
    p = OpenAICompatibleProvider("http://x/chat/completions", "m", "k", session=s)
    p.vision(img, "describe")
    content = s.last_json["messages"][0]["content"]
    assert content[0]["type"] == "image_url"
    assert content[0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert content[1] == {"type": "text", "text": "describe"}


def test_list_models_parses():
    s = FakeSession([_ok()])
    p = OpenAICompatibleProvider("http://x/chat/completions", "m", "k", session=s)
    models = p.list_models()
    assert models[0]["id"] == "vision-1"
    assert s.last_url.endswith("/models")


def test_sessions_are_thread_local():
    p = OpenAICompatibleProvider("http://x/chat/completions", "m", "k")
    sessions = []
    lock = threading.Lock()

    def grab():
        s = p._sess()
        with lock:
            sessions.append(s)  # keep a reference so ids are stable

    threads = [threading.Thread(target=grab) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len({id(s) for s in sessions}) == 3  # one session per thread
