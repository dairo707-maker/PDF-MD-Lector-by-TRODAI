"""PDF/MD Lector by TRODAI — Fast, privacy-first modular PDF and Markdown reader for Windows 11."""

from __future__ import annotations

from pathlib import Path
import sys
import threading

APP_NAME = "PDF/MD Lector"
COMPANY_NAME = "TRODAI"
APP_USER_MODEL_ID = "TRODAI.PDFMDLector"
__version__ = "0.4.3"

_VERSION_LOCK = threading.Lock()
_VERSION_CACHE: dict[Path, tuple[int | None, str]] = {}


def get_base_dir() -> Path:
    """Return project root (or frozen bundle root) containing assets/."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parents[2]


def read_version() -> str:
    """Read VERSION file if present, falling back to __version__."""
    base_dir = get_base_dir()
    version_file = base_dir / "VERSION"
    try:
        mtime_ns: int | None = version_file.stat().st_mtime_ns
    except OSError:
        mtime_ns = None

    with _VERSION_LOCK:
        cached = _VERSION_CACHE.get(base_dir)
        if cached is not None and cached[0] == mtime_ns:
            return cached[1]

    result = __version__
    if mtime_ns is not None:
        try:
            text = version_file.read_text(encoding="utf-8").strip()
            if text:
                result = text
        except OSError:
            mtime_ns = None

    with _VERSION_LOCK:
        _VERSION_CACHE[base_dir] = (mtime_ns, result)
    return result
