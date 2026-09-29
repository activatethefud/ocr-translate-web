"""Vision/text provider adapters (OpenAI-compatible).

DeepSeek, OpenRouter, OpenAI, and most local servers expose the same
``/chat/completions`` shape, so one adapter covers them; only ``api_base`` and
``model`` differ. BYOK: the caller passes the API key in.
"""

from __future__ import annotations

import base64
import json
import threading
import time
from pathlib import Path
from typing import Any, Protocol

import requests


class ProviderError(RuntimeError):
    pass


class Provider(Protocol):
    model: str

    def vision(self, image_path: str | Path, prompt: str, max_tokens: int | None = None) -> str: ...
    def text(self, prompt: str, max_tokens: int | None = None) -> str: ...


def _data_url(image_path: str | Path) -> str:
    b64 = base64.b64encode(Path(image_path).read_bytes()).decode()
    return "data:image/png;base64," + b64


class OpenAICompatibleProvider:
    """Minimal OpenAI-compatible chat client with retries.

    Parameters
    ----------
    api_base: full chat-completions URL, e.g. https://api.deepseek.com/chat/completions
    model:    model id (must accept images for ``vision``)
    api_key:  BYOK key
    """

    def __init__(
        self,
        api_base: str,
        model: str,
        api_key: str,
        timeout: int = 600,
        max_retries: int = 3,
        backoff: float = 2.0,
        reasoning_effort: str = "",
        session: requests.Session | None = None,
    ) -> None:
        self.api_base = api_base
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff = backoff
        # "none" disables a reasoning model's hidden thinking (deepseek-flash);
        # "" leaves the provider default.
        self.reasoning_effort = reasoning_effort
        # One requests.Session per thread: the client is used from a thread pool
        # when pages are translated in parallel.
        self._session = session
        self._local = threading.local()
        self._lock = threading.Lock()
        self.usage: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0}

    def _sess(self) -> requests.Session:
        if self._session is not None:
            return self._session
        sess = getattr(self._local, "session", None)
        if sess is None:
            sess = requests.Session()
            self._local.session = sess
        return sess

    def reset_usage(self) -> None:
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0}

    def usage_snapshot(self) -> dict[str, int]:
        return dict(self.usage)

    # -- low level -----------------------------------------------------
    def _post(self, content: Any, max_tokens: int) -> str:
        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": content}],
            "temperature": 0.0,
            "max_tokens": max_tokens,
        }
        if self.reasoning_effort:
            body["reasoning_effort"] = self.reasoning_effort
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        last = ""
        for attempt in range(self.max_retries):
            try:
                resp = self._sess().post(self.api_base, headers=headers, json=body, timeout=self.timeout)
                data = resp.json()
                if "choices" in data:
                    u = data.get("usage") or {}
                    with self._lock:
                        self.usage["prompt_tokens"] += int(u.get("prompt_tokens") or 0)
                        self.usage["completion_tokens"] += int(u.get("completion_tokens") or 0)
                        self.usage["calls"] += 1
                    choice = data["choices"][0]
                    text = choice.get("message", {}).get("content") or ""
                    # a reasoning model can burn the whole budget thinking and return
                    # nothing; retry with a bigger budget instead of failing the page
                    if choice.get("finish_reason") == "length" and not text.strip() and max_tokens < 96000:
                        max_tokens = min(96000, max_tokens * 2)
                        continue
                    return text
                last = json.dumps(data)[:300]
            except Exception as exc:  # noqa: BLE001 - retried
                last = str(exc)
            if attempt < self.max_retries - 1:
                time.sleep(self.backoff * (attempt + 1))
        raise ProviderError(f"{self.model}: API failed after {self.max_retries} tries: {last}")

    # -- public --------------------------------------------------------
    def vision(self, image_path: str | Path, prompt: str, max_tokens: int | None = None) -> str:
        content = [
            {"type": "image_url", "image_url": {"url": _data_url(image_path)}},
            {"type": "text", "text": prompt},
        ]
        return self._post(content, max_tokens or 16000)

    def text(self, prompt: str, max_tokens: int | None = None) -> str:
        return self._post(prompt, max_tokens or 4000)

    # -- discovery -----------------------------------------------------
    def list_models(self) -> list[dict]:
        url = self.api_base.rsplit("/chat/completions", 1)[0] + "/models"
        resp = self._sess().get(url, headers={"Authorization": f"Bearer {self.api_key}"}, timeout=60)
        return resp.json().get("data", [])


def build_provider(config, api_key: str | None = None) -> OpenAICompatibleProvider:
    """Create a provider from a PipelineConfig (BYOK key or env fallback)."""
    return OpenAICompatibleProvider(
        api_base=config.api_base,
        model=config.model,
        reasoning_effort=getattr(config, "reasoning_effort", ""),
        api_key=config.resolve_api_key(api_key),
        timeout=config.timeout,
    )
