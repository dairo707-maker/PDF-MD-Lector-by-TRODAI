"""Lazy local REST / OpenAPI server package (disabled by default; imports FastAPI on demand)."""

from __future__ import annotations

from typing import Any

__all__ = ["LocalApiServer", "create_fastapi_app"]


def __getattr__(name: str) -> Any:
    if name in ("LocalApiServer", "create_fastapi_app"):
        from . import server

        return getattr(server, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
