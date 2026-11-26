"""Centralized HiDPI polygonal SVG renderer (monochrome UI icons + controlled app logo)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

from .. import get_base_dir


def _icons_dir() -> Path:
    return get_base_dir() / "assets" / "icons"


def _resolve_svg_path(name: str) -> Path:
    clean = (name or "").strip()
    if not clean:
        return _icons_dir() / "__missing__.svg"
    if clean.endswith(".svg"):
        clean = clean[:-4]
    return _icons_dir() / f"{clean}.svg"


@lru_cache(maxsize=160)
def load_svg_icon(name: str, color: str = "#F8FAFC", size: int = 16) -> QIcon:
    """Render a named monochrome polygonal SVG icon tinted to the target color at 2x HiDPI."""
    svg_path = _resolve_svg_path(name)
    if not svg_path.exists():
        return QIcon()

    svg_text = svg_path.read_text(encoding="utf-8").replace("currentColor", color)
    renderer = QSvgRenderer(QByteArray(svg_text.encode("utf-8")))
    if not renderer.isValid():
        return QIcon()
    px_size = max(8, int(size))
    pixmap = QPixmap(px_size * 2, px_size * 2)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    renderer.render(painter)
    painter.end()
    pixmap.setDevicePixelRatio(2.0)
    return QIcon(pixmap)


def load_svg_pixmap(name: str, color: str = "#F8FAFC", size: int = 16) -> QPixmap:
    """Return a HiDPI QPixmap from a monochrome polygonal SVG."""
    icon = load_svg_icon(name, color=color, size=size)
    if icon.isNull():
        return QPixmap()
    return icon.pixmap(size, size)


@lru_cache(maxsize=32)
def load_multicolor_svg_pixmap(
    name: str,
    size: int = 48,
    current_color: str = "#F8FAFC",
) -> QPixmap:
    """Render a multicolor brand/app SVG (replacing currentColor while keeping fixed accents)."""
    svg_path = _resolve_svg_path(name)
    if not svg_path.exists():
        return QPixmap()

    svg_text = svg_path.read_text(encoding="utf-8").replace("currentColor", current_color)
    renderer = QSvgRenderer(QByteArray(svg_text.encode("utf-8")))
    if not renderer.isValid():
        return QPixmap()
    px_size = max(8, int(size))
    pixmap = QPixmap(px_size * 2, px_size * 2)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    renderer.render(painter)
    painter.end()
    pixmap.setDevicePixelRatio(2.0)
    return pixmap
