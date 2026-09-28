"""Encrypt BYOK keys at rest.

The Fernet key comes from ``SECRET_KEY`` (urlsafe base64 32 bytes) if set,
otherwise it is generated once and stored at ``<STORAGE_DIR>/secret.key`` (0600).
Keys are never logged and never returned to clients.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from .settings import Settings


def _load_key(storage_dir: Path) -> bytes:
    env = os.environ.get("SECRET_KEY")
    if env:
        return env.encode()
    path = storage_dir / "secret.key"
    if path.exists():
        return path.read_bytes().strip()
    key = Fernet.generate_key()
    storage_dir.mkdir(parents=True, exist_ok=True)
    path.write_bytes(key)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return key


class Cipher:
    def __init__(self, storage_dir: Path) -> None:
        self._f = Fernet(_load_key(storage_dir))

    def encrypt(self, value: str) -> str:
        return self._f.encrypt(value.encode()).decode()

    def decrypt(self, token: str | None) -> str | None:
        if not token:
            return None
        try:
            return self._f.decrypt(token.encode()).decode()
        except (InvalidToken, ValueError):
            return None


@lru_cache
def _cipher_for(storage_dir: str) -> Cipher:
    return Cipher(Path(storage_dir))


def get_cipher(settings: Settings) -> Cipher:
    return _cipher_for(str(settings.storage_dir))


def hint_for(api_key: str | None) -> str | None:
    if not api_key:
        return None
    tail = api_key[-4:] if len(api_key) >= 8 else ""
    return f"••••{tail}" if tail else "••••"
