"""Windows DPAPI-encrypted local secret store for API keys and Bearer tokens."""

from __future__ import annotations

import base64
import binascii
import json
from pathlib import Path
import threading
import time

from .win32_bridge import (
    atomic_write_text,
    canonical_lock_key,
    default_app_data_dir,
    dpapi_protect,
    dpapi_unprotect,
)

SCHEMA_VERSION = 1

_REGISTRY_LOCK = threading.Lock()
_PATH_LOCKS: dict[str, threading.RLock] = {}


def _lock_for_path(path: Path) -> threading.RLock:
    key = canonical_lock_key(path)
    with _REGISTRY_LOCK:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PATH_LOCKS[key] = lock
        return lock


class SecretsStore:
    """Thread-safe DPAPI-encrypted secret storage in %LOCALAPPDATA%\\TRODAI\\PDFLector\\secrets.json."""

    def __init__(self, base_dir: Path | None = None) -> None:
        self.base_dir = Path(base_dir) if base_dir is not None else default_app_data_dir()
        self.file_path = self.base_dir / "secrets.json"
        self._lock = _lock_for_path(self.file_path)

    def _read_raw(self) -> dict[str, str]:
        if not self.file_path.exists():
            return {}
        raw_text: str | None = None
        for attempt in range(5):
            try:
                raw_text = self.file_path.read_text(encoding="utf-8")
                break
            except FileNotFoundError:
                return {}
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.03 * (attempt + 1))
            except OSError:
                return {}
        if raw_text is None:
            return {}
        try:
            payload = json.loads(raw_text)
        except Exception:
            return {}
        if not isinstance(payload, dict):
            return {}
        raw_secrets = payload.get("secrets")
        if not isinstance(raw_secrets, dict):
            return {}
        return {
            str(k): str(v)
            for k, v in raw_secrets.items()
            if isinstance(k, str) and isinstance(v, str)
        }

    def _write_raw(self, secrets_map: dict[str, str]) -> None:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "secrets": secrets_map,
        }
        atomic_write_text(
            self.file_path,
            json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False),
        )

    def set_secret(self, key: str, value: str) -> None:
        clean_key = str(key).strip()
        if not clean_key:
            raise ValueError("Secret key cannot be empty")
        if not isinstance(value, str):
            raise TypeError("Secret value must be a string")

        encrypted = dpapi_protect(value.encode("utf-8"))
        encoded = base64.b64encode(encrypted).decode("ascii")
        with self._lock:
            data = self._read_raw()
            data[clean_key] = encoded
            self._write_raw(data)

    def get_secret(self, key: str, default: str | None = None) -> str | None:
        clean_key = str(key).strip()
        if not clean_key:
            return default
        with self._lock:
            data = self._read_raw()
            encoded = data.get(clean_key)
        if not encoded:
            return default
        try:
            blob = base64.b64decode(encoded.encode("ascii"), validate=True)
            plaintext = dpapi_unprotect(blob)
            return plaintext.decode("utf-8")
        except (binascii.Error, ValueError, OSError, UnicodeDecodeError):
            return default

    def delete_secret(self, key: str) -> bool:
        clean_key = str(key).strip()
        if not clean_key:
            return False
        with self._lock:
            data = self._read_raw()
            if clean_key not in data:
                return False
            del data[clean_key]
            self._write_raw(data)
            return True

    def list_keys(self) -> list[str]:
        with self._lock:
            return sorted(self._read_raw().keys())

    def clear(self) -> None:
        with self._lock:
            self._write_raw({})


def set_secret(key: str, value: str, *, base_dir: Path | None = None) -> None:
    """Encrypt and store a secret using Windows DPAPI."""
    SecretsStore(base_dir=base_dir).set_secret(key, value)


def get_secret(key: str, default: str | None = None, *, base_dir: Path | None = None) -> str | None:
    """Retrieve and decrypt a DPAPI-protected secret, or return default if absent/corrupt."""
    return SecretsStore(base_dir=base_dir).get_secret(key, default=default)


def delete_secret(key: str, *, base_dir: Path | None = None) -> None:
    """Remove a stored secret if present."""
    SecretsStore(base_dir=base_dir).delete_secret(key)
