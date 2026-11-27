"""Interactive PDF canvas with text selection, ISO 32000 markup, stylus/touch ink, and Kindle dictionary lookup."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import math
from pathlib import Path
import time
from typing import Callable, Sequence
import uuid

from PySide6.QtCore import QEvent, QPoint, QPointF, QRectF, QSize, QTimer, Qt, QUrl, Signal
from PySide6.QtGui import (
    QColor,
    QCursor,
    QDesktopServices,
    QFont,
    QKeySequence,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPointingDevice,
    QImage,
    QShortcut,
    QTabletEvent,
    QTouchEvent,
)
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)
import shiboken6

from ..core.i18n import TRANSLATIONS, t
from ..core.pdf_annotations import (
    AnnotationRecord,
    CLASSIC_HIGHLIGHT_COLORS,
    DEFAULT_ANON_AUTHOR,
    DEFAULT_HIGHLIGHT_COLOR,
    NOTE_CATEGORIES,
    sanitize_author,
    sanitize_color_hex,
    sanitize_note_category,
)
from ..core.pdf_engine import PdfDocumentSession, PdfLink, WordBox, is_safe_pdf_uri
from ..theme.palette import MONO_FONT_FAMILY, UI_FONT_FAMILY, get_palette
from .svg_icons import load_svg_icon
from .widgets import AdaptiveButton

CLASSIC_HIGHLIGHT_PALETTE = CLASSIC_HIGHLIGHT_COLORS
VALID_TOOL_MODES = ("select", "highlight", "free_highlight", "note", "ink", "eraser")

_LOCAL_CANVAS_TEXTS: dict[str, dict[str, str]] = {
    "es": {
        "dict.context_lookup": "Diccionario",
        "canvas.highlight": "Resaltar",
        "canvas.free_highlight": "Resaltado libre",
        "canvas.add_note": "Nota adhesiva",
        "canvas.comment": "Comentar",
        "canvas.copy": "Copiar",
        "canvas.reply": "Responder",
        "canvas.undo": "Deshacer",
        "canvas.note_prompt": "Texto de la nota adhesiva:",
        "canvas.reply_prompt": "Comentario o respuesta:",
        "canvas.link_page": "Ir a pagina {page}",
        "note.card_title": "Nota adhesiva",
        "note.category": "Categoria",
        "note.close": "Cerrar tarjeta",
        "note.delete": "Borrar nota",
        "note.delete_confirm": "Eliminar permanentemente esta nota adhesiva?",
        "note.placeholder": "Escribe el contenido de la nota...",
    },
    "en": {
        "dict.context_lookup": "Dictionary",
        "canvas.highlight": "Highlight",
        "canvas.free_highlight": "Free highlight",
        "canvas.add_note": "Sticky note",
        "canvas.comment": "Comment",
        "canvas.copy": "Copy",
        "canvas.reply": "Reply",
        "canvas.undo": "Undo",
        "canvas.note_prompt": "Sticky note content:",
        "canvas.reply_prompt": "Comment or reply:",
        "canvas.link_page": "Go to page {page}",
        "note.card_title": "Sticky note",
        "note.category": "Category",
        "note.close": "Close card",
        "note.delete": "Delete note",
        "note.delete_confirm": "Permanently delete this sticky note?",
        "note.placeholder": "Write note content...",
    },
    "pt": {
        "dict.context_lookup": "Dicionario",
        "canvas.highlight": "Realcar",
        "canvas.free_highlight": "Realce livre",
        "canvas.add_note": "Nota adesiva",
        "canvas.comment": "Comentar",
        "canvas.copy": "Copiar",
        "canvas.reply": "Responder",
        "canvas.undo": "Desfazer",
        "canvas.note_prompt": "Texto da nota adesiva:",
        "canvas.reply_prompt": "Comentario ou resposta:",
        "canvas.link_page": "Ir para pagina {page}",
        "note.card_title": "Nota adesiva",
        "note.category": "Categoria",
        "note.close": "Fechar cartao",
        "note.delete": "Excluir nota",
        "note.delete_confirm": "Excluir permanentemente esta nota adesiva?",
        "note.placeholder": "Escreva o conteudo da nota...",
    },
}


@dataclass
class SharedAnnotationState:
    """Shared document-wide annotation store across multi-page PdfCanvas widgets."""

    records: list[AnnotationRecord] = field(default_factory=list)
    undo_stack: list[tuple[str, AnnotationRecord]] = field(default_factory=list)
    dirty: bool = False
    listeners: list[Callable[[], None]] = field(default_factory=list)

    def notify(self) -> None:
        for cb in list(self.listeners):
            try:
                cb()
            except Exception:
                pass


def _tc(key: str, lang: str = "es") -> str:
    code = lang if lang in ("es", "en", "pt") else "es"
    if key in TRANSLATIONS.get(code, {}):
        return t(key, code)
    return _LOCAL_CANVAS_TEXTS.get(code, _LOCAL_CANVAS_TEXTS["es"]).get(key, key)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _point_to_segment_distance(px: float, py: float, x1: float, y1: float, x2: float, y2: float) -> float:
    dx = x2 - x1
    dy = y2 - y1
    len2 = dx * dx + dy * dy
    if len2 <= 1e-9:
        return math.hypot(px - x1, py - y1)
    proj = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / len2))
    return math.hypot(px - (x1 + proj * dx), py - (y1 + proj * dy))


class PdfCanvas(QWidget):
    """Interactive PDF page sheet supporting highlights, notes, threaded comments, stylus/touch ink, and Kindle lookup."""

    lookup_word_requested = Signal(str)
    annotations_changed = Signal()
    tool_mode_changed = Signal(str)
    touch_writing_changed = Signal(bool)
    zoom_delta_requested = Signal(float)
    pan_delta_requested = Signal(float, float)
    annotation_selected = Signal(object)
    link_page_requested = Signal(int)
    link_uri_requested = Signal(str)

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        shared_state: SharedAnnotationState | None = None,
    ) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_AcceptTouchEvents, True)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self._lang = "es"
        self._mode = "dark"
        self._session: PdfDocumentSession | None = None
        self._page_index: int = 0
        self._page_size_pdf: tuple[float, float] = (612.0, 792.0)
        self._zoom: float = 1.0
        self._rotation: int = 0
        self._rendered_pixmap: QPixmap | None = None
        self._notes_visible: bool = True

        self._shared_state = shared_state or SharedAnnotationState()
        self._shared_state.listeners.append(self._on_shared_state_mutated)

        self._tool_mode: str = "select"
        self._active_color: str = DEFAULT_HIGHLIGHT_COLOR
        self._ink_color: str = "#0EA5E9"
        self._ink_width: float = 2.5
        self._author: str = DEFAULT_ANON_AUTHOR
        self._anonymous_author: bool = True

        self._touch_writing_enabled: bool = False
        self._stylus_in_proximity: bool = False
        self._last_stylus_ts: float = 0.0
        self._active_touch_stroke: list[tuple[float, float, float]] = []
        self._active_mouse_stroke: list[tuple[float, float, float]] = []
        self._active_free_hl_stroke: list[tuple[float, float]] = []
        self._active_tablet_stroke: list[tuple[float, float, float]] = []
        self._prev_pinch_centroid: QPointF | None = None
        self._prev_pinch_dist: float | None = None
        self._prev_pan_point: QPointF | None = None
        self._app_filter_installed: bool = False

        self._drag_start_pdf: tuple[float, float] | None = None
        self._selection_quads_pdf: list[tuple[float, float, float, float]] = []
        self._selected_text: str = ""
        self._selected_word: str = ""
        self._popover: QFrame | None = None
        self._search_rects_pdf: list[tuple[float, float, float, float]] = []
        self._active_search_idx: int = -1

        self._dragging_note_id: str | None = None
        self._dragging_note_start_px: QPointF | None = None
        self._dragging_note_offset_pdf: tuple[float, float] = (0.0, 0.0)
        self._dragging_note_moved: bool = False
        self._resizing_note_id: str | None = None
        self._resizing_handle: str = ""
        self._resizing_orig_rect: tuple[float, float, float, float] | None = None
        self._hovered_note_id: str | None = None
        self._sticky_card: QFrame | None = None
        self._sticky_card_note_id: str | None = None
        self._word_boxes_cache: list[WordBox] | None = None
        self._page_links_cache: list[PdfLink] | None = None
        self._hovered_link: PdfLink | None = None
        self._press_pos_px: QPointF | None = None

        undo_sc = QShortcut(QKeySequence("Ctrl+Z"), self)
        undo_sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        undo_sc.activated.connect(self.undo_last_annotation)

        esc_sc = QShortcut(QKeySequence("Escape"), self)
        esc_sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        esc_sc.activated.connect(self.clear_selection)

    @property
    def _annotations(self) -> list[AnnotationRecord]:
        return self._shared_state.records

    @_annotations.setter
    def _annotations(self, value: list[AnnotationRecord]) -> None:
        self._shared_state.records = value

    @property
    def _undo_stack(self) -> list[tuple[str, AnnotationRecord]]:
        return self._shared_state.undo_stack

    @_undo_stack.setter
    def _undo_stack(self, value: list[tuple[str, AnnotationRecord]]) -> None:
        self._shared_state.undo_stack = value

    @property
    def _dirty(self) -> bool:
        return self._shared_state.dirty

    @_dirty.setter
    def _dirty(self, value: bool) -> None:
        self._shared_state.dirty = bool(value)

    def _on_shared_state_mutated(self) -> None:
        if shiboken6.isValid(self):
            self.update()

    def clear_selection(self) -> None:
        self._selection_quads_pdf.clear()
        self._selected_text = ""
        self._selected_word = ""
        self._word_boxes_cache = None
        self._page_links_cache = None
        self._hovered_link = None
        self._press_pos_px = None
        self._hide_popover()
        self.update()

    def hide_popover(self) -> None:
        self._hide_popover()

    def set_search_highlights(
        self,
        rects_pdf: list[tuple[float, float, float, float]],
        *,
        active_index: int = -1,
    ) -> None:
        self._search_rects_pdf = list(rects_pdf or [])
        self._active_search_idx = int(active_index)
        self.update()

    def set_session(self, session: PdfDocumentSession | None, page_index: int = 0) -> None:
        self._session = session
        self._page_index = max(0, int(page_index))
        self._page_size_pdf = (612.0, 792.0)
        if session is not None and 0 <= self._page_index < session.page_count:
            try:
                self._page_size_pdf = session.get_page_size(self._page_index)
            except Exception:
                self._page_size_pdf = (612.0, 792.0)
        self._selection_quads_pdf.clear()
        self._selected_text = ""
        self._selected_word = ""
        self._word_boxes_cache = None
        self._page_links_cache = None
        self._hovered_link = None
        self._press_pos_px = None
        self._hide_popover()
        self._sync_canvas_size()
        self.update()

    def set_rotation_and_zoom(self, rotation: int, zoom: float) -> None:
        self._rotation = int(rotation) % 360
        self._zoom = max(0.10, min(12.0, float(zoom)))
        self._sync_canvas_size()
        self.update()

    def set_rendered_image(self, image: QImage) -> None:
        pm = QPixmap.fromImage(image)
        self._rendered_pixmap = pm if not pm.isNull() else None
        # Keep widget size strictly locked to PDF point size * zoom so scroll/highlights never shift!
        self.update()

    def _sync_canvas_size(self) -> None:
        w, h = self._page_size_pdf
        if self._session is not None and 0 <= self._page_index < self._session.page_count:
            try:
                w, h = self._session.get_page_size(self._page_index)
                self._page_size_pdf = (float(w), float(h))
            except Exception:
                pass
        if self._rotation in (90, 270):
            w, h = h, w
        self.setFixedSize(
            QSize(max(60, int(round(w * self._zoom))), max(60, int(round(h * self._zoom))))
        )

    def notes_visible(self) -> bool:
        return self._notes_visible

    def set_notes_visible(self, visible: bool) -> None:
        want = bool(visible)
        if want != self._notes_visible:
            self._notes_visible = want
            if not want:
                self.hide_sticky_card()
            self.update()

    def load_annotations(self, records: Sequence[AnnotationRecord]) -> None:
        self._annotations = list(records)
        self._undo_stack.clear()
        self._dirty = False
        self.update()

    def annotations(self) -> list[AnnotationRecord]:
        return list(self._annotations)

    def has_unsaved_changes(self) -> bool:
        return self._dirty

    def mark_saved(self) -> None:
        self._dirty = False

    def set_tool_mode(self, mode: str) -> None:
        clean = (mode or "select").strip().lower()
        if clean not in VALID_TOOL_MODES:
            clean = "select"
        if clean != self._tool_mode:
            self._tool_mode = clean
            if clean in ("ink", "free_highlight"):
                self.setCursor(Qt.CursorShape.CrossCursor)
            elif clean == "highlight":
                self.setCursor(Qt.CursorShape.IBeamCursor)
            elif clean == "eraser":
                self.setCursor(Qt.CursorShape.PointingHandCursor)
            else:
                self.setCursor(Qt.CursorShape.ArrowCursor)
            self.tool_mode_changed.emit(clean)

    def tool_mode(self) -> str:
        return self._tool_mode

    def set_author(self, author: str) -> None:
        clean = (author or "").strip()
        if clean:
            self._author = clean
            self._anonymous_author = False
        else:
            self._author = DEFAULT_ANON_AUTHOR
            self._anonymous_author = True

    def set_anonymous_author(self, enabled: bool) -> None:
        self._anonymous_author = bool(enabled)

    def active_author(self) -> str:
        if self._anonymous_author:
            return DEFAULT_ANON_AUTHOR
        return sanitize_author(self._author)

    def set_highlight_color(self, color_hex: str) -> None:
        self._active_color = sanitize_color_hex(color_hex, DEFAULT_HIGHLIGHT_COLOR)

    def set_ink_color(self, color_hex: str) -> None:
        self._ink_color = sanitize_color_hex(color_hex, "#0EA5E9")

    def set_ink_width(self, width: float) -> None:
        self._ink_width = max(0.5, min(20.0, float(width)))

    def add_highlight_rect(
        self,
        rect_pdf: tuple[float, float, float, float],
        color_hex: str | None = None,
        content: str = "",
    ) -> AnnotationRecord:
        x0, y0, x1, y1 = (float(v) for v in rect_pdf)
        norm_rect = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
        rec = AnnotationRecord(
            id=f"hl-{uuid.uuid4().hex[:8]}",
            page_index=self._page_index,
            kind="highlight",
            rect_pdf=norm_rect,
            points_pdf=(),
            color_hex=sanitize_color_hex(color_hex or self._active_color, DEFAULT_HIGHLIGHT_COLOR),
            author=self.active_author(),
            timestamp_iso=_utc_now_iso(),
            content=content,
            parent_id=None,
        )
        self._annotations.append(rec)
        self._undo_stack.append(("add", rec))
        self._dirty = True
        self._shared_state.notify()
        self.annotations_changed.emit()
        self.update()
        return rec

    def add_sticky_note(
        self,
        pos_pdf: tuple[float, float],
        content: str,
        *,
        color_hex: str = DEFAULT_HIGHLIGHT_COLOR,
        category: str = "General",
        anchor_pdf: tuple[float, float] | None = None,
        size_pdf: tuple[float, float] = (148.0, 74.0),
    ) -> AnnotationRecord:
        px, py = float(pos_pdf[0]), float(pos_pdf[1])
        w_pdf = max(48.0, float(size_pdf[0]))
        h_pdf = max(32.0, float(size_pdf[1]))
        anchor = (
            (float(anchor_pdf[0]), float(anchor_pdf[1]))
            if anchor_pdf is not None
            else (px, py)
        )
        rec = AnnotationRecord(
            id=f"note-{uuid.uuid4().hex[:8]}",
            page_index=self._page_index,
            kind="text",
            rect_pdf=(px, py, px + w_pdf, py + h_pdf),
            points_pdf=(),
            color_hex=sanitize_color_hex(color_hex, DEFAULT_HIGHLIGHT_COLOR),
            author=self.active_author(),
            timestamp_iso=_utc_now_iso(),
            content=content,
            parent_id=None,
            category=sanitize_note_category(category),
            anchor_pdf=anchor,
        )
        self._annotations.append(rec)
        self._undo_stack.append(("add", rec))
        self._dirty = True
        self._shared_state.notify()
        self.annotations_changed.emit()
        self.update()
        return rec

    def find_annotation(self, annot_id: str) -> AnnotationRecord | None:
        return next((a for a in self._annotations if a.id == annot_id), None)

    def move_sticky_note(self, note_id: str, new_pos_pdf: tuple[float, float]) -> AnnotationRecord | None:
        rec = self.find_annotation(note_id)
        if rec is None:
            return None
        w = max(20.0, rec.rect_pdf[2] - rec.rect_pdf[0])
        h = max(20.0, rec.rect_pdf[3] - rec.rect_pdf[1])
        nx, ny = float(new_pos_pdf[0]), float(new_pos_pdf[1])
        if rec.anchor_pdf is None:
            rec.anchor_pdf = (rec.rect_pdf[0], rec.rect_pdf[1])
        rec.rect_pdf = (nx, ny, nx + w, ny + h)
        self._dirty = True
        self._shared_state.notify()
        self.annotations_changed.emit()
        self.update()
        return rec

    def resize_sticky_note(
        self,
        note_id: str,
        new_rect_pdf: tuple[float, float, float, float],
    ) -> AnnotationRecord | None:
        rec = self.find_annotation(note_id)
        if rec is None:
            return None
        x0, y0, x1, y1 = (float(v) for v in new_rect_pdf)
        nx0, nx1 = min(x0, x1), max(x0, x1)
        ny0, ny1 = min(y0, y1), max(y0, y1)
        if (nx1 - nx0) < 56.0:
            nx1 = nx0 + 56.0
        if (ny1 - ny0) < 36.0:
            ny1 = ny0 + 36.0
        nx1 = min(nx0 + 420.0, nx1)
        ny1 = min(ny0 + 360.0, ny1)
        rec.rect_pdf = (nx0, ny0, nx1, ny1)
        self._dirty = True
        self._shared_state.notify()
        self.annotations_changed.emit()
        self.update()
        return rec

    def update_sticky_note(
        self,
        note_id: str,
        *,
        content: str | None = None,
        color_hex: str | None = None,
        category: str | None = None,
    ) -> AnnotationRecord | None:
        rec = self.find_annotation(note_id)
        if rec is None:
            return None
        if content is not None:
            rec.content = content
        if color_hex is not None:
            rec.color_hex = sanitize_color_hex(color_hex, rec.color_hex)
        if category is not None:
            rec.category = sanitize_note_category(category)
        self._dirty = True
        self._shared_state.notify()
        self.annotations_changed.emit()
        self.update()
        return rec

    def delete_annotation(self, annot_id: str) -> AnnotationRecord | None:
        idx = next((i for i, a in enumerate(self._annotations) if a.id == annot_id), -1)
        if idx < 0:
            return None
        removed = self._annotations.pop(idx)
        self._annotations = [a for a in self._annotations if a.parent_id != annot_id]
        self._undo_stack.append(("remove", removed))
        if self._sticky_card_note_id == annot_id:
            self.hide_sticky_card()
        self._dirty = True
        self._shared_state.notify()
        self.annotations_changed.emit()
        self.update()
        return removed

    def add_reply(self, parent_id: str, content: str) -> AnnotationRecord:
        parent = next((a for a in self._annotations if a.id == parent_id), None)
        if parent is not None:
            px, py = parent.rect_pdf[0], parent.rect_pdf[1]
            rect = (px + 28.0, max(0.0, py - 20.0), px + 180.0, py + 40.0)
            page_idx = parent.page_index
            color = parent.color_hex
        else:
            rect = (100.0, 100.0, 260.0, 160.0)
            page_idx = self._page_index
            color = DEFAULT_HIGHLIGHT_COLOR

        rec = AnnotationRecord(
            id=f"reply-{uuid.uuid4().hex[:8]}",
            page_index=page_idx,
            kind="popup",
            rect_pdf=rect,
            points_pdf=(),
            color_hex=color,
            author=self.active_author(),
            timestamp_iso=_utc_now_iso(),
            content=content,
            parent_id=parent_id,
        )
        self._annotations.append(rec)
        self._undo_stack.append(("add", rec))
        self._dirty = True
        self.annotations_changed.emit()
        self.update()
        return rec

    def record_ink_stroke(
        self,
        points: Sequence[tuple[float, float] | tuple[float, float, float]],
        *,
        color_hex: str | None = None,
        stroke_width: float | None = None,
    ) -> AnnotationRecord | None:
        if len(points) < 2:
            return None

        stroke_pts: list[tuple[float, ...]] = []
        total_len = 0.0
        prev_xy: tuple[float, float] | None = None
        for pt in points:
            x, y = float(pt[0]), float(pt[1])
            if len(pt) >= 3:
                p_val = max(0.1, min(1.0, float(pt[2])))
                stroke_pts.append((x, y, p_val))
            else:
                stroke_pts.append((x, y))
            if prev_xy is not None:
                total_len += math.hypot(x - prev_xy[0], y - prev_xy[1])
            prev_xy = (x, y)

        if total_len < 1e-3:
            return None

        w = max(0.5, min(20.0, float(stroke_width if stroke_width is not None else self._ink_width)))
        xs = [p[0] for p in stroke_pts]
        ys = [p[1] for p in stroke_pts]
        pad = max(1.0, w)
        rect_pdf = (min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad)

        rec = AnnotationRecord(
            id=f"ink-{uuid.uuid4().hex[:8]}",
            page_index=self._page_index,
            kind="ink",
            rect_pdf=rect_pdf,
            points_pdf=[stroke_pts],
            color_hex=sanitize_color_hex(color_hex or self._ink_color, "#0EA5E9"),
            author=self.active_author(),
            timestamp_iso=_utc_now_iso(),
            content="",
            parent_id=None,
            stroke_width=w,
            opacity=1.0,
        )
        self._annotations.append(rec)
        self._undo_stack.append(("add", rec))
        self._dirty = True
        self.annotations_changed.emit()
        self.update()
        return rec

    def record_free_highlight_stroke(
        self,
        points: Sequence[tuple[float, float] | tuple[float, float, float]],
        *,
        color_hex: str | None = None,
        stroke_width: float = 12.0,
        opacity: float = 0.42,
    ) -> AnnotationRecord | None:
        """Record a freehand pointer highlighter stroke saved as translucent ISO 32000 /Ink."""
        if len(points) < 2:
            return None
        stroke_pts: list[tuple[float, ...]] = []
        total_len = 0.0
        prev_xy: tuple[float, float] | None = None
        for pt in points:
            x, y = float(pt[0]), float(pt[1])
            stroke_pts.append((x, y))
            if prev_xy is not None:
                total_len += math.hypot(x - prev_xy[0], y - prev_xy[1])
            prev_xy = (x, y)
        if total_len < 1e-3:
            return None

        w = max(4.0, min(24.0, float(stroke_width)))
        ca = max(0.18, min(0.75, float(opacity)))
        xs = [p[0] for p in stroke_pts]
        ys = [p[1] for p in stroke_pts]
        pad = max(2.0, w * 0.5)
        rec = AnnotationRecord(
            id=f"fhl-{uuid.uuid4().hex[:8]}",
            page_index=self._page_index,
            kind="ink",
            rect_pdf=(min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad),
            points_pdf=[stroke_pts],
            color_hex=sanitize_color_hex(color_hex or self._active_color, DEFAULT_HIGHLIGHT_COLOR),
            author=self.active_author(),
            timestamp_iso=_utc_now_iso(),
            content="",
            parent_id=None,
            stroke_width=w,
            opacity=ca,
        )
        self._annotations.append(rec)
        self._undo_stack.append(("add", rec))
        self._dirty = True
        self._shared_state.notify()
        self.annotations_changed.emit()
        self.update()
        return rec

    @property
    def touch_writing_enabled(self) -> bool:
        return self._touch_writing_enabled

    def set_touch_writing_enabled(self, enabled: bool) -> None:
        flag = bool(enabled)
        if flag != self._touch_writing_enabled:
            self._touch_writing_enabled = flag
            self.touch_writing_changed.emit(flag)

    def set_stylus_in_proximity(self, active: bool) -> None:
        self._stylus_in_proximity = bool(active)
        if self._stylus_in_proximity:
            self._last_stylus_ts = time.monotonic()

    def _effective_scale(self) -> float:
        pw, ph = self._page_size_pdf
        rot_w = ph if self._rotation in (90, 270) else pw
        if self.width() > 0 and rot_w > 1.0:
            return max(0.05, float(self.width()) / float(rot_w))
        return max(0.05, self._zoom)

    def _screen_to_pdf(self, sx: float, sy: float) -> tuple[float, float]:
        eff = self._effective_scale()
        if self._session is not None and 0 <= self._page_index < self._session.page_count:
            try:
                return self._session.screen_to_pdf_point(
                    self._page_index, sx, sy, scale=eff, rotation=self._rotation
                )
            except Exception:
                pass
        return (sx / eff, sy / eff)

    def _pdf_to_screen_rect(self, rect_pdf: tuple[float, float, float, float]) -> QRectF:
        eff = self._effective_scale()
        if self._session is not None and 0 <= self._page_index < self._session.page_count:
            try:
                sx0, sy0, sx1, sy1 = self._session.pdf_to_screen_rect(
                    self._page_index, rect_pdf, scale=eff, rotation=self._rotation
                )
                return QRectF(sx0, sy0, max(2.0, sx1 - sx0), max(2.0, sy1 - sy0))
            except Exception:
                pass
        x0, y0, x1, y1 = rect_pdf
        return QRectF(x0 * eff, y0 * eff, max(2.0, (x1 - x0) * eff), max(2.0, (y1 - y0) * eff))

    def _pdf_to_screen_point(self, x_pdf: float, y_pdf: float) -> QPointF:
        eff = self._effective_scale()
        if self._session is not None and 0 <= self._page_index < self._session.page_count:
            try:
                sx, sy = self._session.pdf_to_screen_point(
                    self._page_index, x_pdf, y_pdf, scale=eff, rotation=self._rotation
                )
                return QPointF(sx, sy)
            except Exception:
                pass
        return QPointF(x_pdf * eff, y_pdf * eff)

    def handle_touch_points(
        self,
        points: Sequence[tuple[QPointF, int] | QPointF],
        *,
        phase: str = "draw",
    ) -> str:
        """Process touch points: palm-reject during stylus proximity, 2-finger pan/zoom, or 1-finger ink."""
        if not points:
            self._prev_pinch_centroid = None
            self._prev_pinch_dist = None
            return "ignored"

        if self._stylus_in_proximity or (time.monotonic() - self._last_stylus_ts) < 0.25:
            self._active_touch_stroke.clear()
            self._prev_pinch_centroid = None
            self._prev_pinch_dist = None
            return "palm_rejected"

        norm_pts: list[tuple[QPointF, int]] = []
        for item in points:
            if isinstance(item, tuple):
                norm_pts.append((QPointF(item[0]), int(item[1])))
            else:
                norm_pts.append((QPointF(item), 0))

        unique_ids = {pid for _, pid in norm_pts}
        if len(unique_ids) >= 2 or phase in ("pan", "zoom", "pinch"):
            self._active_touch_stroke.clear()
            first_per_id: dict[int, QPointF] = {}
            for pt, pid in norm_pts:
                first_per_id.setdefault(pid, pt)
            pts_list = list(first_per_id.values())
            if len(pts_list) >= 2:
                if phase == "begin":
                    self._prev_pinch_centroid = None
                    self._prev_pinch_dist = None
                c = QPointF(
                    sum(p.x() for p in pts_list) / len(pts_list),
                    sum(p.y() for p in pts_list) / len(pts_list),
                )
                dist = math.hypot(pts_list[1].x() - pts_list[0].x(), pts_list[1].y() - pts_list[0].y())
                if self._prev_pinch_centroid is not None:
                    self.pan_delta_requested.emit(
                        c.x() - self._prev_pinch_centroid.x(),
                        c.y() - self._prev_pinch_centroid.y(),
                    )
                if self._prev_pinch_dist is not None and self._prev_pinch_dist > 5.0:
                    ratio = dist / self._prev_pinch_dist
                    if abs(ratio - 1.0) > 0.02:
                        self.zoom_delta_requested.emit((ratio - 1.0) * 0.5)
                if phase == "end":
                    self._prev_pinch_centroid = None
                    self._prev_pinch_dist = None
                else:
                    self._prev_pinch_centroid = c
                    self._prev_pinch_dist = dist
            else:
                self._prev_pinch_centroid = None
                self._prev_pinch_dist = None
            return "pan_zoom"

        self._prev_pinch_centroid = None
        self._prev_pinch_dist = None

        if self._touch_writing_enabled:
            pdf_pts = [self._screen_to_pdf(pt.x(), pt.y()) for pt, _ in norm_pts]
            if phase == "draw":
                self.record_ink_stroke([(x, y, 0.55) for x, y in pdf_pts])
                return "ink"
            if phase == "begin":
                self._active_touch_stroke = [(x, y, 0.55) for x, y in pdf_pts]
                self.update()
                return "ink"
            if phase == "update":
                self._active_touch_stroke.extend((x, y, 0.55) for x, y in pdf_pts)
                self.update()
                return "ink"
            if phase == "end":
                self._active_touch_stroke.extend((x, y, 0.55) for x, y in pdf_pts)
                stroke = list(self._active_touch_stroke)
                self._active_touch_stroke.clear()
                self.record_ink_stroke(stroke)
                return "ink"

        # Default single-finger pan when touch writing is off
        pt = norm_pts[0][0]
        if phase == "begin":
            self._prev_pan_point = pt
        elif phase == "update" and self._prev_pan_point is not None:
            self.pan_delta_requested.emit(pt.x() - self._prev_pan_point.x(), pt.y() - self._prev_pan_point.y())
            self._prev_pan_point = pt
        elif phase == "end":
            self._prev_pan_point = None
        return "pan"

    def _get_word_boxes(self) -> list[WordBox]:
        if self._word_boxes_cache is None:
            if self._session is not None and 0 <= self._page_index < self._session.page_count:
                try:
                    self._word_boxes_cache = list(self._session.get_page_word_boxes(self._page_index))
                except Exception:
                    self._word_boxes_cache = []
            else:
                self._word_boxes_cache = []
        return self._word_boxes_cache

    def _is_near_text(self, x_pdf: float, y_pdf: float) -> bool:
        boxes = self._get_word_boxes()
        if not boxes:
            return False
        tol = max(3.0, min(10.0, 6.0 / max(0.25, self._effective_scale())))
        for wb in boxes:
            x0, y0, x1, y1 = wb.rect_pdf
            if (x0 - tol) <= x_pdf <= (x1 + tol) and (y0 - tol) <= y_pdf <= (y1 + tol):
                return True
        return False

    def _get_page_links(self) -> list[PdfLink]:
        if self._page_links_cache is None:
            if self._session is not None and 0 <= self._page_index < self._session.page_count:
                try:
                    self._page_links_cache = list(self._session.get_page_links(self._page_index))
                except Exception:
                    self._page_links_cache = []
            else:
                self._page_links_cache = []
        return self._page_links_cache

    def _link_at(self, x_pdf: float, y_pdf: float) -> PdfLink | None:
        tol = 2.0
        for lnk in self._get_page_links():
            x0, y0, x1, y1 = lnk.rect_pdf
            if (x0 - tol) <= x_pdf <= (x1 + tol) and (y0 - tol) <= y_pdf <= (y1 + tol):
                return lnk
        return None

    def _set_link_hover_tooltip(self, link: PdfLink | None) -> None:
        if link == self._hovered_link:
            return
        self._hovered_link = link
        if link is None:
            self.setToolTip("")
        elif link.uri:
            self.setToolTip(link.uri)
        elif link.target_page is not None:
            self.setToolTip(_tc("canvas.link_page", self._lang).format(page=int(link.target_page) + 1))

    def _activate_link(self, link: PdfLink) -> bool:
        if link.target_page is not None:
            self.link_page_requested.emit(int(link.target_page))
            return True
        if link.uri and is_safe_pdf_uri(link.uri):
            self.link_uri_requested.emit(link.uri)
            if QApplication.platformName() != "offscreen":
                QDesktopServices.openUrl(QUrl(link.uri))
            return True
        return False

    def _annotation_at(
        self,
        x_pdf: float,
        y_pdf: float,
        *,
        radius_pdf: float = 8.0,
    ) -> tuple[int, AnnotationRecord] | None:
        rad = max(2.0, float(radius_pdf))
        for idx in range(len(self._annotations) - 1, -1, -1):
            rec = self._annotations[idx]
            if rec.page_index != self._page_index:
                continue
            if not self._notes_visible and rec.kind in ("text", "freetext", "popup"):
                continue
            hit = False
            if rec.kind == "ink" and rec.points_pdf:
                for stroke in rec.points_pdf:
                    for j in range(len(stroke) - 1):
                        d = _point_to_segment_distance(
                            x_pdf,
                            y_pdf,
                            float(stroke[j][0]),
                            float(stroke[j][1]),
                            float(stroke[j + 1][0]),
                            float(stroke[j + 1][1]),
                        )
                        if d <= rad + rec.stroke_width:
                            hit = True
                            break
                    if hit:
                        break
            else:
                x0, y0, x1, y1 = rec.rect_pdf
                if (x0 - rad) <= x_pdf <= (x1 + rad) and (y0 - rad) <= y_pdf <= (y1 + rad):
                    hit = True
            if hit:
                return (idx, rec)
        return None

    def _note_handle_points(self, sr: QRectF) -> dict[str, QPointF]:
        cx = sr.center().x()
        cy = sr.center().y()
        return {
            "nw": QPointF(sr.left(), sr.top()),
            "n": QPointF(cx, sr.top()),
            "ne": QPointF(sr.right(), sr.top()),
            "e": QPointF(sr.right(), cy),
            "se": QPointF(sr.right(), sr.bottom()),
            "s": QPointF(cx, sr.bottom()),
            "sw": QPointF(sr.left(), sr.bottom()),
            "w": QPointF(sr.left(), cy),
        }

    def _note_resize_handle_at(self, pos_px: QPointF) -> tuple[str, str] | None:
        if not self._notes_visible or self._tool_mode not in ("select", "note"):
            return None
        for rec in reversed(self._annotations):
            if rec.page_index != self._page_index or rec.kind != "text":
                continue
            sr = self._pdf_to_screen_rect(rec.rect_pdf)
            handles = self._note_handle_points(sr)
            if sr.width() < 36.0 or sr.height() < 28.0:
                se_pt = handles["se"]
                if (
                    math.hypot(pos_px.x() - se_pt.x(), pos_px.y() - se_pt.y()) <= 6.5
                    and math.hypot(pos_px.x() - sr.center().x(), pos_px.y() - sr.center().y()) >= 6.0
                ):
                    return (rec.id, "se")
                continue
            for name, pt in handles.items():
                if math.hypot(pos_px.x() - pt.x(), pos_px.y() - pt.y()) <= 8.0:
                    return (rec.id, name)
        return None

    def erase_annotation_at(
        self,
        x_pdf: float,
        y_pdf: float,
        *,
        radius_pdf: float = 8.0,
    ) -> AnnotationRecord | None:
        found = self._annotation_at(x_pdf, y_pdf, radius_pdf=radius_pdf)
        if found is None:
            return None
        idx, _ = found
        removed = self._annotations.pop(idx)
        self._undo_stack.append(("remove", removed))
        if self._sticky_card_note_id == removed.id:
            self.hide_sticky_card()
        self._dirty = True
        self._shared_state.notify()
        self.annotations_changed.emit()
        self.update()
        return removed

    def undo_last_annotation(self) -> AnnotationRecord | None:
        if not self._undo_stack:
            return None
        op, rec = self._undo_stack.pop()
        if op == "add":
            self._annotations = [a for a in self._annotations if a.id != rec.id]
            if self._sticky_card_note_id == rec.id:
                self.hide_sticky_card()
        elif op == "remove":
            self._annotations.append(rec)
        self._dirty = True
        self._shared_state.notify()
        self.annotations_changed.emit()
        self.update()
        return rec

    def request_dictionary_lookup(self, word: str) -> None:
        clean = "".join(ch for ch in (word or "").strip() if ch.isalnum() or ch in ("-", "'"))
        if clean:
            self.lookup_word_requested.emit(clean)

    def hide_sticky_card(self) -> None:
        card = self._sticky_card
        self._sticky_card = None
        self._sticky_card_note_id = None
        if card is not None and shiboken6.isValid(card):
            card.hide()
            card.deleteLater()

    def open_sticky_note_card(self, note_id: str) -> QFrame | None:
        rec = self.find_annotation(note_id)
        if rec is None or rec.page_index != self._page_index:
            return None
        self.hide_sticky_card()
        self._hide_popover()

        p = get_palette(self._mode)
        card_color = sanitize_color_hex(rec.color_hex)
        card = QFrame(self)
        card.setObjectName("StickyNoteCard")
        card.setFixedWidth(258)
        card_bg = "#0F172A" if self._mode == "dark" else "#FFFFFF"
        editor_bg = "#111C2E" if self._mode == "dark" else "#F8FAFC"
        fg_col = "#F8FAFC" if self._mode == "dark" else "#0F172A"
        muted_col = "#94A3B8" if self._mode == "dark" else "#475569"
        bd_col = "#334155" if self._mode == "dark" else "#CBD5E1"
        card.setStyleSheet(
            f"QFrame#StickyNoteCard {{ background-color: {card_bg}; color: {fg_col}; "
            f"border: 1px solid {bd_col}; border-top: 4px solid {card_color}; }}"
        )

        root = QVBoxLayout(card)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(6)

        # Header row: Title/Category + Delete + Close
        hdr = QHBoxLayout()
        hdr.setSpacing(4)
        cat_combo = QComboBox(card)
        cat_combo.setStyleSheet(
            f"QComboBox {{ background-color: {editor_bg}; color: {fg_col}; border: 1px solid {bd_col}; padding: 3px 6px; }}"
        )
        for cat in NOTE_CATEGORIES:
            cat_combo.addItem(cat, cat)
        cur_cat = sanitize_note_category(getattr(rec, "category", "General"))
        idx = cat_combo.findData(cur_cat)
        if idx < 0:
            cat_combo.addItem(cur_cat, cur_cat)
            idx = cat_combo.count() - 1
        cat_combo.setCurrentIndex(idx)
        cat_combo.currentIndexChanged.connect(
            lambda _i, nid=rec.id, cb=cat_combo: self.update_sticky_note(
                nid, category=str(cb.currentData() or "General")
            )
        )
        hdr.addWidget(cat_combo, 1)

        del_btn = QPushButton("", card)
        del_btn.setObjectName("StickyDelBtn")
        del_btn.setProperty("role", "RiskAction")
        del_btn.setFixedSize(24, 24)
        del_btn.setIcon(load_svg_icon("trash", color=p["icon_color"], size=13))
        del_btn.setToolTip(_tc("note.delete", self._lang))

        def _on_delete() -> None:
            if QApplication.platformName() != "offscreen":
                ans = QMessageBox.question(
                    self,
                    _tc("note.delete", self._lang),
                    _tc("note.delete_confirm", self._lang),
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if ans != QMessageBox.StandardButton.Yes:
                    return
            QTimer.singleShot(0, lambda nid=rec.id: self.delete_annotation(nid))

        del_btn.clicked.connect(_on_delete)
        hdr.addWidget(del_btn)

        close_btn = QPushButton("", card)
        close_btn.setObjectName("StickyCloseBtn")
        close_btn.setProperty("role", "GhostAction")
        close_btn.setFixedSize(24, 24)
        close_btn.setIcon(load_svg_icon("close", color=p["icon_color"], size=13))
        close_btn.setToolTip(_tc("note.close", self._lang))
        close_btn.clicked.connect(lambda: QTimer.singleShot(0, self.hide_sticky_card))
        hdr.addWidget(close_btn)
        root.addLayout(hdr)

        # Color swatches strip
        swatch_row = QHBoxLayout()
        swatch_row.setSpacing(5)
        for hex_col in CLASSIC_HIGHLIGHT_PALETTE:
            sw = QPushButton("", card)
            sw.setFixedSize(16, 16)
            sw.setStyleSheet(f"background-color: {hex_col}; border: 1px solid {p['border_strong']}; padding: 0px;")

            def _pick_color(_checked: bool = False, c_val: str = hex_col, nid: str = rec.id, c_frame: QFrame = card) -> None:
                self.update_sticky_note(nid, color_hex=c_val)
                if shiboken6.isValid(c_frame):
                    c_frame.setStyleSheet(
                        f"QFrame#StickyNoteCard {{ background-color: {card_bg}; color: {fg_col}; "
                        f"border: 1px solid {bd_col}; border-top: 4px solid {c_val}; }}"
                    )

            sw.clicked.connect(_pick_color)
            swatch_row.addWidget(sw)
        swatch_row.addStretch(1)
        root.addLayout(swatch_row)

        # Live editable content (explicit dark/light foreground so text is always crisp)
        editor = QTextEdit(card)
        editor.setObjectName("StickyNoteEditor")
        editor.setFixedHeight(92)
        editor.setStyleSheet(
            f"QTextEdit#StickyNoteEditor {{ background-color: {editor_bg}; color: {fg_col}; "
            f"border: 1px solid {bd_col}; padding: 6px; }}"
        )
        editor.setPlaceholderText(_tc("note.placeholder", self._lang))
        editor.setPlainText(rec.content or "")
        editor.textChanged.connect(
            lambda nid=rec.id, ed=editor: self.update_sticky_note(nid, content=ed.toPlainText())
        )
        root.addWidget(editor)

        ts_str = (rec.timestamp_iso or "")[:16].replace("T", " ")
        meta_lbl = QLabel(f"{rec.author}  {ts_str}".strip(), card)
        meta_lbl.setFont(QFont(MONO_FONT_FAMILY, 8))
        meta_lbl.setStyleSheet(f"color: {muted_col}; background: transparent; border: none;")
        root.addWidget(meta_lbl)

        card.adjustSize()
        sr = self._pdf_to_screen_rect(rec.rect_pdf)
        px = max(6, min(max(6, self.width() - card.width() - 6), int(sr.right() + 8)))
        py = max(6, min(max(6, self.height() - card.height() - 6), int(sr.top())))
        card.move(px, py)
        card.show()
        card.raise_()
        editor.setFocus()
        self._sticky_card = card
        self._sticky_card_note_id = rec.id
        return card

    def _hide_popover(self) -> None:
        pop = self._popover
        self._popover = None
        if pop is not None and shiboken6.isValid(pop):
            pop.hide()
            pop.deleteLater()

    def _show_kindle_popover(self, word: str, pos_px: QPoint) -> None:
        self._hide_popover()
        clean = "".join(ch for ch in (word or "").strip() if ch.isalnum() or ch in ("-", "'"))
        if not clean:
            return

        p = get_palette(self._mode)
        pop = QFrame(self)
        pop.setObjectName("SelectionPopover")
        pop.setStyleSheet(
            f"QFrame#SelectionPopover {{ background-color: {p['bg_menu']}; color: {p['text_primary']}; "
            f"border: 1px solid {p['border_strong']}; }}"
        )
        pop_layout = QHBoxLayout(pop)
        pop_layout.setContentsMargins(6, 5, 6, 5)
        pop_layout.setSpacing(4)

        hl_btn = AdaptiveButton(_tc("canvas.highlight", self._lang), pop, role="PrimaryAction")
        hl_btn.setIcon(load_svg_icon("edit", color=p["primary_fg"], size=13))
        hl_btn.setMinimumHeight(26)
        hl_btn.setStyleSheet("padding: 3px 8px; font-size: 11px;")

        def _on_highlight() -> None:
            if self._selection_quads_pdf:
                for qrect in self._selection_quads_pdf:
                    self.add_highlight_rect(qrect, color_hex=self._active_color)
                self._selection_quads_pdf.clear()
            QTimer.singleShot(0, self._hide_popover)

        hl_btn.clicked.connect(_on_highlight)
        pop_layout.addWidget(hl_btn)

        for col in CLASSIC_HIGHLIGHT_PALETTE:
            sw = QPushButton("", pop)
            sw.setFixedSize(16, 16)
            sw.setToolTip(f"{_tc('canvas.highlight', self._lang)} ({col})")
            sw.setStyleSheet(f"background-color: {col}; border: 1px solid {p['border_strong']}; padding: 0px;")

            def _on_swatch(_checked: bool = False, hex_c: str = col) -> None:
                self.set_highlight_color(hex_c)
                if self._selection_quads_pdf:
                    for qrect in self._selection_quads_pdf:
                        self.add_highlight_rect(qrect, color_hex=hex_c)
                    self._selection_quads_pdf.clear()
                QTimer.singleShot(0, self._hide_popover)

            sw.clicked.connect(_on_swatch)
            pop_layout.addWidget(sw)

        note_btn = QPushButton("", pop)
        note_btn.setProperty("role", "GhostAction")
        note_btn.setFixedSize(26, 26)
        note_btn.setStyleSheet("padding: 2px;")
        note_btn.setIcon(load_svg_icon("comment", color=p["icon_color"], size=13))
        note_btn.setToolTip(_tc("canvas.add_note", self._lang))

        def _on_note() -> None:
            anchor_rect = self._selection_quads_pdf[0] if self._selection_quads_pdf else (72.0, 680.0, 120.0, 700.0)
            txt, ok = QInputDialog.getText(
                self,
                _tc("canvas.add_note", self._lang),
                _tc("canvas.note_prompt", self._lang),
            )
            if ok and txt.strip():
                ax = (anchor_rect[0] + anchor_rect[2]) * 0.5
                ay = (anchor_rect[1] + anchor_rect[3]) * 0.5
                nx = min(max(8.0, anchor_rect[2] + 14.0), max(8.0, self._page_size_pdf[0] - 156.0))
                ny = max(8.0, anchor_rect[1] - 28.0)
                self.add_sticky_note((nx, ny), txt.strip(), anchor_pdf=(ax, ay))
            QTimer.singleShot(0, self._hide_popover)

        note_btn.clicked.connect(_on_note)
        pop_layout.addWidget(note_btn)

        comment_btn = QPushButton("", pop)
        comment_btn.setProperty("role", "GhostAction")
        comment_btn.setFixedSize(26, 26)
        comment_btn.setStyleSheet("padding: 2px;")
        comment_btn.setIcon(load_svg_icon("spark", color=p["icon_color"], size=13))
        comment_btn.setToolTip(_tc("canvas.comment", self._lang))

        def _on_comment() -> None:
            quads = list(self._selection_quads_pdf)
            txt, ok = QInputDialog.getText(
                self,
                _tc("canvas.comment", self._lang),
                _tc("canvas.reply_prompt", self._lang),
            )
            if ok and txt.strip() and quads:
                parent_hl = self.add_highlight_rect(quads[0], color_hex=self._active_color, content=txt.strip())
                self.add_reply(parent_hl.id, txt.strip())
                self._selection_quads_pdf.clear()
            QTimer.singleShot(0, self._hide_popover)

        comment_btn.clicked.connect(_on_comment)
        pop_layout.addWidget(comment_btn)

        copy_btn = QPushButton("", pop)
        copy_btn.setProperty("role", "GhostAction")
        copy_btn.setFixedSize(26, 26)
        copy_btn.setStyleSheet("padding: 2px;")
        copy_btn.setIcon(load_svg_icon("copy", color=p["icon_color"], size=13))
        copy_btn.setToolTip(_tc("canvas.copy", self._lang))

        def _on_copy() -> None:
            cb = QApplication.clipboard()
            payload = (self._selected_text or clean).strip()
            if cb is not None and payload:
                cb.setText(payload)
            QTimer.singleShot(0, self._hide_popover)

        copy_btn.clicked.connect(_on_copy)
        pop_layout.addWidget(copy_btn)

        dict_btn = QPushButton("", pop)
        dict_btn.setProperty("role", "GhostAction")
        dict_btn.setFixedSize(26, 26)
        dict_btn.setStyleSheet("padding: 2px;")
        dict_btn.setIcon(load_svg_icon("book", color=p["icon_color"], size=13))
        dict_btn.setToolTip(_tc("dict.context_lookup", self._lang))

        def _on_lookup() -> None:
            self.request_dictionary_lookup(clean)
            QTimer.singleShot(0, self._hide_popover)

        dict_btn.clicked.connect(_on_lookup)
        pop_layout.addWidget(dict_btn)

        pop.adjustSize()
        px = max(4, min(max(4, self.width() - pop.width() - 4), pos_px.x() - pop.width() // 2))
        py = max(4, pos_px.y() - pop.height() - 8)
        pop.move(px, py)
        pop.show()
        self._popover = pop

    def wheelEvent(self, event) -> None:  # type: ignore[override]
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            delta_y = event.angleDelta().y()
            if delta_y != 0:
                self.zoom_delta_requested.emit(0.15 if delta_y > 0 else -0.15)
            event.accept()
            return
        self._hide_popover()
        event.ignore()

    def tabletEvent(self, event: QTabletEvent) -> None:
        etype = event.type()
        self._last_stylus_ts = time.monotonic()
        if etype == QEvent.Type.TabletEnterProximity:
            self.set_stylus_in_proximity(True)
            event.accept()
            return
        if etype == QEvent.Type.TabletLeaveProximity:
            self.set_stylus_in_proximity(False)
            event.accept()
            return

        pos = event.position()
        x_pdf, y_pdf = self._screen_to_pdf(pos.x(), pos.y())
        pressure = max(0.1, min(1.0, float(event.pressure())))
        is_eraser = event.pointerType() == QPointingDevice.PointerType.Eraser

        if etype == QEvent.Type.TabletPress:
            self.set_stylus_in_proximity(True)
            if is_eraser or self._tool_mode == "eraser":
                self.erase_annotation_at(x_pdf, y_pdf)
            else:
                self._active_tablet_stroke = [(x_pdf, y_pdf, pressure)]
                self.update()
            event.accept()
            return
        if etype == QEvent.Type.TabletMove:
            if is_eraser or self._tool_mode == "eraser":
                self.erase_annotation_at(x_pdf, y_pdf)
            elif self._active_tablet_stroke:
                self._active_tablet_stroke.append((x_pdf, y_pdf, pressure))
                self.update()
            event.accept()
            return
        if etype == QEvent.Type.TabletRelease:
            if self._active_tablet_stroke:
                stroke = list(self._active_tablet_stroke)
                self._active_tablet_stroke.clear()
                self.record_ink_stroke(stroke)
            self.set_stylus_in_proximity(False)
            event.accept()
            return
        super().tabletEvent(event)

    def event(self, ev: QEvent) -> bool:
        etype = ev.type()
        if etype == QEvent.Type.TabletEnterProximity:
            self.set_stylus_in_proximity(True)
            self._last_stylus_ts = time.monotonic()
            ev.accept()
            return True
        if etype == QEvent.Type.TabletLeaveProximity:
            self.set_stylus_in_proximity(False)
            self._last_stylus_ts = time.monotonic()
            ev.accept()
            return True
        if etype in (
            QEvent.Type.TouchBegin,
            QEvent.Type.TouchUpdate,
            QEvent.Type.TouchEnd,
            QEvent.Type.TouchCancel,
        ):
            if isinstance(ev, QTouchEvent):
                pts = [(p.position(), p.id()) for p in ev.points()]
                phase = {
                    QEvent.Type.TouchBegin: "begin",
                    QEvent.Type.TouchUpdate: "update",
                    QEvent.Type.TouchEnd: "end",
                    QEvent.Type.TouchCancel: "end",
                }[etype]
                res = self.handle_touch_points(pts, phase=phase)
                if res != "ignored":
                    ev.accept()
                    return True
        return super().event(ev)

    def _sync_app_tablet_filter(self, enable: bool) -> None:
        app = QApplication.instance()
        if app is None:
            return
        if enable and not self._app_filter_installed:
            app.installEventFilter(self)
            self._app_filter_installed = True
        elif not enable and self._app_filter_installed:
            app.removeEventFilter(self)
            self._app_filter_installed = False

    def showEvent(self, event) -> None:  # type: ignore[override]
        super().showEvent(event)
        self._sync_app_tablet_filter(True)

    def hideEvent(self, event) -> None:  # type: ignore[override]
        self._sync_app_tablet_filter(False)
        super().hideEvent(event)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self._sync_app_tablet_filter(False)
        super().closeEvent(event)

    def eventFilter(self, watched, event) -> bool:  # type: ignore[override]
        del watched
        if not shiboken6.isValid(self):
            return False
        etype = event.type()
        if etype == QEvent.Type.TabletEnterProximity:
            if self.isVisible():
                self.set_stylus_in_proximity(True)
        elif etype == QEvent.Type.TabletLeaveProximity:
            if self.isVisible():
                self.set_stylus_in_proximity(False)
        return False

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._hide_popover()
            pos = event.position()
            x_pdf, y_pdf = self._screen_to_pdf(pos.x(), pos.y())

            resize_hit = self._note_resize_handle_at(pos)
            if resize_hit is not None:
                nid, handle = resize_hit
                rec = self.find_annotation(nid)
                if rec is not None:
                    self._resizing_note_id = nid
                    self._resizing_handle = handle
                    self._resizing_orig_rect = rec.rect_pdf
                    self.hide_sticky_card()
                    event.accept()
                    return

            if self._notes_visible and self._tool_mode in ("select", "note"):
                hit = self._annotation_at(x_pdf, y_pdf, radius_pdf=6.0)
                if hit is not None and hit[1].kind == "text":
                    rec = hit[1]
                    self._dragging_note_id = rec.id
                    self._dragging_note_start_px = QPointF(pos)
                    self._dragging_note_offset_pdf = (x_pdf - rec.rect_pdf[0], y_pdf - rec.rect_pdf[1])
                    self._dragging_note_moved = False
                    event.accept()
                    return

            if self._tool_mode == "free_highlight":
                self._active_free_hl_stroke = [(x_pdf, y_pdf)]
                self.update()
                event.accept()
                return
            if self._tool_mode == "ink":
                self._active_mouse_stroke = [(x_pdf, y_pdf, 0.55)]
                self.update()
                event.accept()
                return
            if self._tool_mode == "eraser":
                self.erase_annotation_at(x_pdf, y_pdf)
                event.accept()
                return
            if self._tool_mode == "note":
                if QApplication.platformName() == "offscreen":
                    rec = self.add_sticky_note((x_pdf, y_pdf), "Nota rapida", anchor_pdf=(x_pdf, y_pdf))
                    self.open_sticky_note_card(rec.id)
                else:
                    rec = self.add_sticky_note((x_pdf, y_pdf), "", anchor_pdf=(x_pdf, y_pdf))
                    self.open_sticky_note_card(rec.id)
                event.accept()
                return

            self.hide_sticky_card()
            self._press_pos_px = QPointF(pos)
            self._drag_start_pdf = (x_pdf, y_pdf)
            self._selection_quads_pdf.clear()
            self._selected_text = ""
            self._selected_word = ""
            self.update()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        pos = event.position()
        x_pdf, y_pdf = self._screen_to_pdf(pos.x(), pos.y())
        if (event.buttons() & Qt.MouseButton.LeftButton) and self._resizing_note_id is not None and self._resizing_orig_rect is not None:
            sr = self._pdf_to_screen_rect(self._resizing_orig_rect)
            left, top, right, bottom = sr.left(), sr.top(), sr.right(), sr.bottom()
            h = self._resizing_handle
            min_w, min_h = 44.0, 28.0
            if "w" in h:
                left = min(right - min_w, pos.x())
            if "e" in h:
                right = max(left + min_w, pos.x())
            if "n" in h:
                top = min(bottom - min_h, pos.y())
            if "s" in h:
                bottom = max(top + min_h, pos.y())
            p0 = self._screen_to_pdf(left, top)
            p1 = self._screen_to_pdf(right, bottom)
            self.resize_sticky_note(
                self._resizing_note_id,
                (min(p0[0], p1[0]), min(p0[1], p1[1]), max(p0[0], p1[0]), max(p0[1], p1[1])),
            )
            event.accept()
            return
        if (event.buttons() & Qt.MouseButton.LeftButton) and self._dragging_note_id is not None:
            if self._dragging_note_start_px is not None:
                dist = math.hypot(
                    pos.x() - self._dragging_note_start_px.x(),
                    pos.y() - self._dragging_note_start_px.y(),
                )
                if dist >= 4.0:
                    self._dragging_note_moved = True
                    self.hide_sticky_card()
            if self._dragging_note_moved:
                ox, oy = self._dragging_note_offset_pdf
                self.move_sticky_note(self._dragging_note_id, (x_pdf - ox, y_pdf - oy))
            event.accept()
            return
        if (event.buttons() & Qt.MouseButton.LeftButton) and self._tool_mode == "free_highlight" and self._active_free_hl_stroke:
            lx, ly = self._active_free_hl_stroke[-1]
            if math.hypot(x_pdf - lx, y_pdf - ly) >= 1.5:
                self._active_free_hl_stroke.append((x_pdf, y_pdf))
                self.update()
            event.accept()
            return
        if (event.buttons() & Qt.MouseButton.LeftButton) and self._tool_mode == "ink" and self._active_mouse_stroke:
            self._active_mouse_stroke.append((x_pdf, y_pdf, 0.55))
            self.update()
            event.accept()
            return
        if (event.buttons() & Qt.MouseButton.LeftButton) and self._tool_mode == "eraser":
            self.erase_annotation_at(x_pdf, y_pdf)
            event.accept()
            return
        if (
            (event.buttons() & Qt.MouseButton.LeftButton)
            and self._drag_start_pdf is not None
            and self._session is not None
            and self._tool_mode in ("select", "highlight")
        ):
            sx0, sy0 = self._drag_start_pdf
            if math.hypot(x_pdf - sx0, y_pdf - sy0) >= 3.0:
                sel_text, quads = self._session.select_text_in_rect(
                    self._page_index, (sx0, sy0, x_pdf, y_pdf)
                )
                self._selected_text = sel_text
                self._selection_quads_pdf = quads
                self._selected_word = sel_text.split()[0] if sel_text.split() else ""
                self.update()
        elif not (event.buttons() & Qt.MouseButton.LeftButton) and self._tool_mode in ("select", "note"):
            r_hit = self._note_resize_handle_at(pos)
            if r_hit is not None:
                nid, handle = r_hit
                self._hovered_note_id = nid
                self._set_link_hover_tooltip(None)
                cursor_map = {
                    "nw": Qt.CursorShape.SizeFDiagCursor,
                    "se": Qt.CursorShape.SizeFDiagCursor,
                    "ne": Qt.CursorShape.SizeBDiagCursor,
                    "sw": Qt.CursorShape.SizeBDiagCursor,
                    "n": Qt.CursorShape.SizeVerCursor,
                    "s": Qt.CursorShape.SizeVerCursor,
                    "e": Qt.CursorShape.SizeHorCursor,
                    "w": Qt.CursorShape.SizeHorCursor,
                }
                self.setCursor(cursor_map.get(handle, Qt.CursorShape.SizeAllCursor))
            else:
                hit = self._annotation_at(x_pdf, y_pdf, radius_pdf=6.0) if self._notes_visible else None
                if hit is not None and hit[1].kind == "text":
                    self._hovered_note_id = hit[1].id
                    self._set_link_hover_tooltip(None)
                    self.setCursor(Qt.CursorShape.PointingHandCursor)
                else:
                    if self._hovered_note_id is not None:
                        self._hovered_note_id = None
                        self.update()
                    if self._tool_mode == "select":
                        link = self._link_at(x_pdf, y_pdf)
                        if link is not None:
                            self._set_link_hover_tooltip(link)
                            self.setCursor(Qt.CursorShape.PointingHandCursor)
                        elif self._is_near_text(x_pdf, y_pdf):
                            self._set_link_hover_tooltip(None)
                            self.setCursor(Qt.CursorShape.IBeamCursor)
                        else:
                            self._set_link_hover_tooltip(None)
                            self.setCursor(Qt.CursorShape.ArrowCursor)
                    else:
                        self._set_link_hover_tooltip(None)
                        self.setCursor(Qt.CursorShape.ArrowCursor)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            if self._resizing_note_id is not None:
                self._resizing_note_id = None
                self._resizing_handle = ""
                self._resizing_orig_rect = None
                self.update()
                event.accept()
                return
            if self._dragging_note_id is not None:
                nid = self._dragging_note_id
                moved = self._dragging_note_moved
                self._dragging_note_id = None
                self._dragging_note_start_px = None
                self._dragging_note_moved = False
                if not moved:
                    self.open_sticky_note_card(nid)
                event.accept()
                return
            if self._tool_mode == "free_highlight" and self._active_free_hl_stroke:
                stroke = list(self._active_free_hl_stroke)
                self._active_free_hl_stroke.clear()
                self.record_free_highlight_stroke(stroke)
                event.accept()
                return
            if self._tool_mode == "ink" and self._active_mouse_stroke:
                stroke = list(self._active_mouse_stroke)
                self._active_mouse_stroke.clear()
                self.record_ink_stroke(stroke)
                event.accept()
                return
            if self._drag_start_pdf is not None:
                sx0, sy0 = self._drag_start_pdf
                self._drag_start_pdf = None
                pos = event.position()
                drag_px = (
                    math.hypot(pos.x() - self._press_pos_px.x(), pos.y() - self._press_pos_px.y())
                    if self._press_pos_px is not None
                    else 0.0
                )
                self._press_pos_px = None
                ex_pdf, ey_pdf = self._screen_to_pdf(pos.x(), pos.y())
                if (
                    self._tool_mode == "select"
                    and drag_px < 4.5
                    and not self._selected_word
                    and not self._selection_quads_pdf
                ):
                    link = self._link_at(ex_pdf, ey_pdf)
                    if link is not None and self._activate_link(link):
                        event.accept()
                        return
                if self._tool_mode == "highlight":
                    if self._selection_quads_pdf:
                        for qrect in self._selection_quads_pdf:
                            self.add_highlight_rect(qrect, color_hex=self._active_color)
                        self._selection_quads_pdf.clear()
                    elif math.hypot(ex_pdf - sx0, ey_pdf - sy0) >= 8.0:
                        self.add_highlight_rect((sx0, sy0, ex_pdf, ey_pdf), color_hex=self._active_color)
                elif self._selected_word:
                    self._show_kindle_popover(self._selected_word, pos.toPoint())
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._session is not None:
            pos = event.position()
            try:
                hit = self._session.hit_test_word_screen(
                    self._page_index,
                    pos.x(),
                    pos.y(),
                    scale=self._effective_scale(),
                    rotation=self._rotation,
                )
            except Exception:
                hit = None
            if hit is not None:
                self._selected_word = hit.word
                self._selected_text = hit.word
                self._selection_quads_pdf = [hit.rect_pdf]
                self.update()
                self._show_kindle_popover(hit.word, pos.toPoint())
                event.accept()
                return
        super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event) -> None:
        menu = QMenu(self)
        lookup_word = self._selected_word
        if not lookup_word and self._session is not None:
            try:
                hit = self._session.hit_test_word_screen(
                    self._page_index,
                    float(event.pos().x()),
                    float(event.pos().y()),
                    scale=self._effective_scale(),
                    rotation=self._rotation,
                )
                if hit is not None:
                    lookup_word = hit.word
                    self._selected_word = hit.word
                    self._selection_quads_pdf = [hit.rect_pdf]
            except Exception:
                pass

        dict_act = menu.addAction(_tc("dict.context_lookup", self._lang))
        dict_act.setEnabled(bool(lookup_word))
        dict_act.triggered.connect(lambda: self.request_dictionary_lookup(lookup_word))

        hl_menu = menu.addMenu(_tc("canvas.highlight", self._lang))
        for col in CLASSIC_HIGHLIGHT_PALETTE:
            act = hl_menu.addAction(col)
            act.setEnabled(bool(self._selection_quads_pdf))
            act.triggered.connect(
                lambda _=False, c=col: [self.add_highlight_rect(q, color_hex=c) for q in self._selection_quads_pdf]
            )

        note_act = menu.addAction(_tc("canvas.add_note", self._lang))
        x_pdf, y_pdf = self._screen_to_pdf(float(event.pos().x()), float(event.pos().y()))

        def _prompt_note() -> None:
            txt, ok = QInputDialog.getText(
                self,
                _tc("canvas.add_note", self._lang),
                _tc("canvas.note_prompt", self._lang),
            )
            if ok and txt.strip():
                self.add_sticky_note((x_pdf, y_pdf), txt.strip(), anchor_pdf=(x_pdf, y_pdf))

        note_act.triggered.connect(_prompt_note)

        hit_annot = self._annotation_at(x_pdf, y_pdf)
        if hit_annot is not None:
            _, target_annot = hit_annot
            reply_act = menu.addAction(_tc("canvas.reply", self._lang))

            def _prompt_reply(_checked: bool = False, parent_id: str = target_annot.id) -> None:
                txt, ok = QInputDialog.getText(
                    self,
                    _tc("canvas.reply", self._lang),
                    _tc("canvas.reply_prompt", self._lang),
                )
                if ok and txt.strip():
                    self.add_reply(parent_id, txt.strip())

            reply_act.triggered.connect(_prompt_reply)

        undo_act = menu.addAction(_tc("canvas.undo", self._lang))
        undo_act.setEnabled(bool(self._undo_stack))
        undo_act.triggered.connect(self.undo_last_annotation)

        menu.exec(QCursor.pos())

    def paintEvent(self, event) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        # Crisp white paper sheet scaled 1:1 to widget rect (zero drift on scroll/zoom)
        painter.fillRect(self.rect(), QColor("#FFFFFF"))
        if self._rendered_pixmap is not None and not self._rendered_pixmap.isNull():
            painter.drawPixmap(self.rect(), self._rendered_pixmap)

        # In-PDF search matches (Ctrl+F)
        if self._search_rects_pdf:
            for idx, rect_pdf in enumerate(self._search_rects_pdf):
                sr = self._pdf_to_screen_rect(rect_pdf)
                if idx == self._active_search_idx:
                    painter.setPen(QPen(QColor("#D97706"), 2))
                    painter.setBrush(QColor(245, 158, 11, 130))
                else:
                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.setBrush(QColor(253, 224, 71, 90))
                painter.drawRect(sr)

        # Selection quads
        if self._selection_quads_pdf:
            sel_fill = QColor(14, 165, 233, 64)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(sel_fill)
            for quad in self._selection_quads_pdf:
                painter.drawRect(self._pdf_to_screen_rect(quad))

        eff_scale = self._effective_scale()

        # Highlight rectangles and translucent freehand highlights (Multiply preserves dark text crisp)
        painter.save()
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Multiply)
        for rec in self._annotations:
            if rec.page_index != self._page_index:
                continue
            col = QColor(sanitize_color_hex(rec.color_hex))
            if rec.kind == "highlight":
                fill = QColor(col)
                fill.setAlpha(135)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(fill)
                painter.drawRect(self._pdf_to_screen_rect(rec.rect_pdf))
            elif rec.kind == "ink" and float(rec.opacity) < 0.95:
                stroke_col = QColor(col)
                stroke_col.setAlpha(int(max(40, min(210, round(float(rec.opacity) * 255)))))
                pen = QPen(
                    stroke_col,
                    max(4.0, rec.stroke_width * eff_scale),
                    Qt.PenStyle.SolidLine,
                    Qt.PenCapStyle.RoundCap,
                    Qt.PenJoinStyle.RoundJoin,
                )
                painter.setPen(pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                for stroke in rec.points_pdf:
                    if len(stroke) < 2:
                        continue
                    path = QPainterPath(self._pdf_to_screen_point(float(stroke[0][0]), float(stroke[0][1])))
                    for pt in stroke[1:]:
                        path.lineTo(self._pdf_to_screen_point(float(pt[0]), float(pt[1])))
                    painter.drawPath(path)

        if len(self._active_free_hl_stroke) >= 2:
            fhl_col = QColor(sanitize_color_hex(self._active_color))
            fhl_col.setAlpha(115)
            painter.setPen(
                QPen(
                    fhl_col,
                    max(4.0, 12.0 * eff_scale),
                    Qt.PenStyle.SolidLine,
                    Qt.PenCapStyle.RoundCap,
                    Qt.PenJoinStyle.RoundJoin,
                )
            )
            painter.setBrush(Qt.BrushStyle.NoBrush)
            f_path = QPainterPath(self._pdf_to_screen_point(self._active_free_hl_stroke[0][0], self._active_free_hl_stroke[0][1]))
            for pt in self._active_free_hl_stroke[1:]:
                f_path.lineTo(self._pdf_to_screen_point(pt[0], pt[1]))
            painter.drawPath(f_path)
        painter.restore()

        # Opaque ink strokes + visible sticky notes / guide lines / resize handles + popups
        for rec in self._annotations:
            if rec.page_index != self._page_index:
                continue
            col = QColor(sanitize_color_hex(rec.color_hex))
            if rec.kind == "ink" and float(rec.opacity) >= 0.95:
                for stroke in rec.points_pdf:
                    if len(stroke) < 2:
                        continue
                    for idx in range(len(stroke) - 1):
                        p0 = self._pdf_to_screen_point(float(stroke[idx][0]), float(stroke[idx][1]))
                        p1 = self._pdf_to_screen_point(float(stroke[idx + 1][0]), float(stroke[idx + 1][1]))
                        pressure = float(stroke[idx][2]) if len(stroke[idx]) >= 3 else 0.6
                        pen = QPen(
                            col,
                            max(1.0, rec.stroke_width * eff_scale * (0.6 + 0.8 * pressure)),
                            Qt.PenStyle.SolidLine,
                            Qt.PenCapStyle.RoundCap,
                            Qt.PenJoinStyle.RoundJoin,
                        )
                        painter.setPen(pen)
                        painter.drawLine(p0, p1)
            elif rec.kind == "text" and self._notes_visible:
                sr = self._pdf_to_screen_rect(rec.rect_pdf)
                # Subtle guide line from note card edge to text anchor
                anchor = getattr(rec, "anchor_pdf", None)
                if anchor is not None and len(anchor) >= 2:
                    ap = self._pdf_to_screen_point(float(anchor[0]), float(anchor[1]))
                    cx = max(sr.left(), min(sr.right(), ap.x()))
                    cy = max(sr.top(), min(sr.bottom(), ap.y()))
                    if math.hypot(ap.x() - cx, ap.y() - cy) > 3.0:
                        guide_pen = QPen(QColor(100, 116, 139, 110), 1.0, Qt.PenStyle.DashLine)
                        painter.setPen(guide_pen)
                        painter.drawLine(QPointF(cx, cy), ap)
                        painter.setPen(Qt.PenStyle.NoPen)
                        dot_col = QColor(col)
                        dot_col.setAlpha(200)
                        painter.setBrush(dot_col)
                        painter.drawRect(QRectF(ap.x() - 2.0, ap.y() - 2.0, 4.0, 4.0))

                if sr.width() >= 48.0 and sr.height() >= 30.0:
                    bg = QColor(
                        (255 * 4 + col.red()) // 5,
                        (255 * 4 + col.green()) // 5,
                        (255 * 4 + col.blue()) // 5,
                        245,
                    )
                    painter.setPen(QPen(QColor("#334155"), 1.0))
                    painter.setBrush(bg)
                    painter.drawRect(sr)

                    hdr_h = min(18.0, sr.height() * 0.30)
                    hdr_rect = QRectF(sr.left(), sr.top(), sr.width(), hdr_h)
                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.setBrush(col)
                    painter.drawRect(hdr_rect)

                    cat_lbl = sanitize_note_category(getattr(rec, "category", "General"))
                    painter.setPen(QColor("#0F172A"))
                    painter.setFont(QFont(UI_FONT_FAMILY, 7, QFont.Weight.Bold))
                    painter.drawText(
                        hdr_rect.adjusted(5.0, 0.0, -4.0, 0.0),
                        Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                        cat_lbl,
                    )

                    body_txt = (rec.content or "").strip() or "..."
                    painter.setFont(QFont(UI_FONT_FAMILY, 8))
                    painter.drawText(
                        sr.adjusted(5.0, hdr_h + 3.0, -5.0, -4.0),
                        Qt.TextFlag.TextWordWrap | Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft,
                        body_txt,
                    )

                    # Square polygonal resize handle points at corners (and edges when hovered/active)
                    handles = self._note_handle_points(sr)
                    show_edges = (rec.id == self._hovered_note_id or rec.id == self._resizing_note_id)
                    keys = handles.keys() if show_edges else ("nw", "ne", "se", "sw")
                    painter.setPen(QPen(QColor("#0F172A"), 1.0))
                    painter.setBrush(QColor("#FFFFFF"))
                    for k in keys:
                        hp = handles[k]
                        painter.drawRect(QRectF(hp.x() - 2.5, hp.y() - 2.5, 5.0, 5.0))
                else:
                    painter.setPen(QPen(QColor("#334155"), 1))
                    painter.setBrush(col)
                    painter.drawRect(sr)
                    painter.drawLine(
                        int(sr.left() + 4),
                        int(sr.top() + sr.height() * 0.4),
                        int(sr.right() - 4),
                        int(sr.top() + sr.height() * 0.4),
                    )
            elif rec.kind in ("freetext", "popup") and self._notes_visible:
                sr = self._pdf_to_screen_rect(rec.rect_pdf)
                painter.setPen(QPen(col, 1))
                painter.setBrush(QColor("#FFFFFF" if rec.kind == "freetext" else "#FEF9C3"))
                painter.drawRect(sr)
                if rec.content:
                    painter.setPen(QColor("#0F172A"))
                    painter.drawText(sr.adjusted(4, 2, -4, -2), Qt.TextFlag.TextWordWrap, rec.content)

        # In-progress opaque ink strokes
        for active in (self._active_tablet_stroke, self._active_mouse_stroke, self._active_touch_stroke):
            if len(active) >= 2:
                pen = QPen(
                    QColor(self._ink_color),
                    max(1.2, self._ink_width * eff_scale),
                    Qt.PenStyle.SolidLine,
                    Qt.PenCapStyle.RoundCap,
                    Qt.PenJoinStyle.RoundJoin,
                )
                painter.setPen(pen)
                path = QPainterPath(self._pdf_to_screen_point(active[0][0], active[0][1]))
                for pt in active[1:]:
                    path.lineTo(self._pdf_to_screen_point(pt[0], pt[1]))
                painter.drawPath(path)

        # Crisp 1px page border
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor("#334155"), 1))
        painter.drawRect(0, 0, max(1, self.width() - 1), max(1, self.height() - 1))
        painter.end()

    def retranslate(self, lang: str) -> None:
        self._lang = lang if lang in ("es", "en", "pt") else "es"

    def apply_theme(self, mode: str) -> None:
        self._mode = "light" if mode == "light" else "dark"
        self.update()
