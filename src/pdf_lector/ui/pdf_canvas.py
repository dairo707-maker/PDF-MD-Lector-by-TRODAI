"""Interactive PDF canvas with text selection, ISO 32000 markup, stylus/touch ink, and Kindle dictionary lookup."""

from __future__ import annotations

from datetime import datetime, timezone
import math
from pathlib import Path
import time
from typing import Sequence
import uuid

from PySide6.QtCore import QEvent, QPoint, QPointF, QRectF, QSize, QTimer, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QCursor,
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
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QMenu,
    QPushButton,
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
    sanitize_author,
    sanitize_color_hex,
)
from ..core.pdf_engine import PdfDocumentSession
from .widgets import AdaptiveButton

CLASSIC_HIGHLIGHT_PALETTE = CLASSIC_HIGHLIGHT_COLORS
VALID_TOOL_MODES = ("select", "highlight", "note", "ink", "eraser")

_LOCAL_CANVAS_TEXTS: dict[str, dict[str, str]] = {
    "es": {
        "dict.context_lookup": "Buscar en diccionario",
        "canvas.highlight": "Resaltar",
        "canvas.add_note": "Anadir nota",
        "canvas.reply": "Responder",
        "canvas.undo": "Deshacer",
        "canvas.note_prompt": "Texto de la nota adhesiva:",
        "canvas.reply_prompt": "Respuesta al comentario:",
    },
    "en": {
        "dict.context_lookup": "Look up in dictionary",
        "canvas.highlight": "Highlight",
        "canvas.add_note": "Add note",
        "canvas.reply": "Reply",
        "canvas.undo": "Undo",
        "canvas.note_prompt": "Sticky note content:",
        "canvas.reply_prompt": "Reply to comment:",
    },
    "pt": {
        "dict.context_lookup": "Buscar no dicionario",
        "canvas.highlight": "Realcar",
        "canvas.add_note": "Adicionar nota",
        "canvas.reply": "Responder",
        "canvas.undo": "Desfazer",
        "canvas.note_prompt": "Texto da nota adesiva:",
        "canvas.reply_prompt": "Resposta ao comentario:",
    },
}


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

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_AcceptTouchEvents, True)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self._lang = "es"
        self._mode = "dark"
        self._session: PdfDocumentSession | None = None
        self._page_index: int = 0
        self._zoom: float = 1.0
        self._rotation: int = 0
        self._rendered_pixmap: QPixmap | None = None

        self._annotations: list[AnnotationRecord] = []
        self._undo_stack: list[tuple[str, AnnotationRecord]] = []
        self._dirty: bool = False

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

        undo_sc = QShortcut(QKeySequence("Ctrl+Z"), self)
        undo_sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        undo_sc.activated.connect(self.undo_last_annotation)

    def set_session(self, session: PdfDocumentSession | None, page_index: int = 0) -> None:
        self._session = session
        self._page_index = max(0, int(page_index))
        self._selection_quads_pdf.clear()
        self._selected_text = ""
        self._selected_word = ""
        self._hide_popover()
        self._sync_canvas_size()
        self.update()

    def set_rotation_and_zoom(self, rotation: int, zoom: float) -> None:
        self._rotation = int(rotation) % 360
        self._zoom = max(0.2, min(4.0, float(zoom)))
        self._sync_canvas_size()
        self.update()

    def set_rendered_image(self, image: QImage) -> None:
        pm = QPixmap.fromImage(image)
        self._rendered_pixmap = pm if not pm.isNull() else None
        if self._rendered_pixmap is not None:
            self.setFixedSize(self._rendered_pixmap.size())
        else:
            self._sync_canvas_size()
        self.update()

    def _sync_canvas_size(self) -> None:
        if self._rendered_pixmap is not None and not self._rendered_pixmap.isNull():
            self.setFixedSize(self._rendered_pixmap.size())
            return
        if self._session is not None and 0 <= self._page_index < self._session.page_count:
            try:
                w, h = self._session.get_page_size(self._page_index)
                if self._rotation in (90, 270):
                    w, h = h, w
                self.setFixedSize(
                    QSize(max(120, int(round(w * self._zoom))), max(120, int(round(h * self._zoom))))
                )
                return
            except Exception:
                pass
        self.setFixedSize(QSize(612, 792))

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
            if clean == "ink":
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
        self.annotations_changed.emit()
        self.update()
        return rec

    def add_sticky_note(
        self,
        pos_pdf: tuple[float, float],
        content: str,
        *,
        color_hex: str = DEFAULT_HIGHLIGHT_COLOR,
    ) -> AnnotationRecord:
        px, py = float(pos_pdf[0]), float(pos_pdf[1])
        rec = AnnotationRecord(
            id=f"note-{uuid.uuid4().hex[:8]}",
            page_index=self._page_index,
            kind="text",
            rect_pdf=(px, py, px + 24.0, py + 24.0),
            points_pdf=(),
            color_hex=sanitize_color_hex(color_hex, DEFAULT_HIGHLIGHT_COLOR),
            author=self.active_author(),
            timestamp_iso=_utc_now_iso(),
            content=content,
            parent_id=None,
        )
        self._annotations.append(rec)
        self._undo_stack.append(("add", rec))
        self._dirty = True
        self.annotations_changed.emit()
        self.update()
        return rec

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

    def _screen_to_pdf(self, sx: float, sy: float) -> tuple[float, float]:
        if self._session is not None and 0 <= self._page_index < self._session.page_count:
            try:
                return self._session.screen_to_pdf_point(
                    self._page_index, sx, sy, scale=self._zoom, rotation=self._rotation
                )
            except Exception:
                pass
        return (sx / max(0.05, self._zoom), sy / max(0.05, self._zoom))

    def _pdf_to_screen_rect(self, rect_pdf: tuple[float, float, float, float]) -> QRectF:
        if self._session is not None and 0 <= self._page_index < self._session.page_count:
            try:
                sx0, sy0, sx1, sy1 = self._session.pdf_to_screen_rect(
                    self._page_index, rect_pdf, scale=self._zoom, rotation=self._rotation
                )
                return QRectF(sx0, sy0, max(2.0, sx1 - sx0), max(2.0, sy1 - sy0))
            except Exception:
                pass
        x0, y0, x1, y1 = rect_pdf
        return QRectF(x0 * self._zoom, y0 * self._zoom, max(2.0, (x1 - x0) * self._zoom), max(2.0, (y1 - y0) * self._zoom))

    def _pdf_to_screen_point(self, x_pdf: float, y_pdf: float) -> QPointF:
        if self._session is not None and 0 <= self._page_index < self._session.page_count:
            try:
                sx, sy = self._session.pdf_to_screen_point(
                    self._page_index, x_pdf, y_pdf, scale=self._zoom, rotation=self._rotation
                )
                return QPointF(sx, sy)
            except Exception:
                pass
        return QPointF(x_pdf * self._zoom, y_pdf * self._zoom)

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
        self._dirty = True
        self.annotations_changed.emit()
        self.update()
        return removed

    def undo_last_annotation(self) -> AnnotationRecord | None:
        if not self._undo_stack:
            return None
        op, rec = self._undo_stack.pop()
        if op == "add":
            self._annotations = [a for a in self._annotations if a.id != rec.id]
        elif op == "remove":
            self._annotations.append(rec)
        self._dirty = True
        self.annotations_changed.emit()
        self.update()
        return rec

    def request_dictionary_lookup(self, word: str) -> None:
        clean = "".join(ch for ch in (word or "").strip() if ch.isalnum() or ch in ("-", "'"))
        if clean:
            self.lookup_word_requested.emit(clean)

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

        pop = QFrame(self)
        pop.setObjectName("CardPanel")
        pop_layout = QHBoxLayout(pop)
        pop_layout.setContentsMargins(6, 4, 6, 4)
        pop_layout.setSpacing(4)

        dict_btn = AdaptiveButton(_tc("dict.context_lookup", self._lang), pop, role="PrimaryAction")
        dict_btn.setMinimumHeight(26)
        dict_btn.setStyleSheet("padding: 3px 8px; font-size: 11px;")

        def _on_lookup() -> None:
            self.request_dictionary_lookup(clean)
            QTimer.singleShot(0, self._hide_popover)

        dict_btn.clicked.connect(_on_lookup)
        pop_layout.addWidget(dict_btn)

        for col in CLASSIC_HIGHLIGHT_PALETTE:
            sw = QPushButton("", pop)
            sw.setFixedSize(16, 16)
            sw.setStyleSheet(f"background-color: {col}; border: 1px solid #334155; padding: 0px;")

            def _on_swatch(_checked: bool = False, hex_c: str = col) -> None:
                if self._selection_quads_pdf:
                    for qrect in self._selection_quads_pdf:
                        self.add_highlight_rect(qrect, color_hex=hex_c)
                QTimer.singleShot(0, self._hide_popover)

            sw.clicked.connect(_on_swatch)
            pop_layout.addWidget(sw)

        pop.adjustSize()
        px = max(4, min(max(4, self.width() - pop.width() - 4), pos_px.x()))
        py = max(4, pos_px.y() - pop.height() - 6)
        pop.move(px, py)
        pop.show()
        self._popover = pop

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
                txt, ok = QInputDialog.getText(
                    self,
                    _tc("canvas.add_note", self._lang),
                    _tc("canvas.note_prompt", self._lang),
                )
                if ok and txt.strip():
                    self.add_sticky_note((x_pdf, y_pdf), txt.strip())
                event.accept()
                return

            self._drag_start_pdf = (x_pdf, y_pdf)
            self._selection_quads_pdf.clear()
            self._selected_text = ""
            self._selected_word = ""
            self.update()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        pos = event.position()
        x_pdf, y_pdf = self._screen_to_pdf(pos.x(), pos.y())
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
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            if self._tool_mode == "ink" and self._active_mouse_stroke:
                stroke = list(self._active_mouse_stroke)
                self._active_mouse_stroke.clear()
                self.record_ink_stroke(stroke)
                event.accept()
                return
            if self._drag_start_pdf is not None:
                self._drag_start_pdf = None
                if self._tool_mode == "highlight" and self._selection_quads_pdf:
                    for qrect in self._selection_quads_pdf:
                        self.add_highlight_rect(qrect, color_hex=self._active_color)
                    self._selection_quads_pdf.clear()
                elif self._selected_word:
                    self._show_kindle_popover(self._selected_word, event.position().toPoint())
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._session is not None:
            pos = event.position()
            try:
                hit = self._session.hit_test_word_screen(
                    self._page_index,
                    pos.x(),
                    pos.y(),
                    scale=self._zoom,
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
                    scale=self._zoom,
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
                self.add_sticky_note((x_pdf, y_pdf), txt.strip())

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

        # Crisp white paper sheet
        painter.fillRect(self.rect(), QColor("#FFFFFF"))
        if self._rendered_pixmap is not None and not self._rendered_pixmap.isNull():
            painter.drawPixmap(0, 0, self._rendered_pixmap)

        # Selection quads
        if self._selection_quads_pdf:
            sel_fill = QColor(14, 165, 233, 64)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(sel_fill)
            for quad in self._selection_quads_pdf:
                painter.drawRect(self._pdf_to_screen_rect(quad))

        # Committed annotations for current page
        for rec in self._annotations:
            if rec.page_index != self._page_index:
                continue
            col = QColor(sanitize_color_hex(rec.color_hex))
            if rec.kind == "highlight":
                fill = QColor(col)
                fill.setAlpha(110)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(fill)
                painter.drawRect(self._pdf_to_screen_rect(rec.rect_pdf))
            elif rec.kind == "text":
                sr = self._pdf_to_screen_rect(rec.rect_pdf)
                painter.setPen(QPen(QColor("#334155"), 1))
                painter.setBrush(col)
                painter.drawRect(sr)
                painter.drawLine(
                    int(sr.left() + 4),
                    int(sr.top() + sr.height() * 0.4),
                    int(sr.right() - 4),
                    int(sr.top() + sr.height() * 0.4),
                )
            elif rec.kind == "ink":
                for stroke in rec.points_pdf:
                    if len(stroke) < 2:
                        continue
                    for idx in range(len(stroke) - 1):
                        p0 = self._pdf_to_screen_point(float(stroke[idx][0]), float(stroke[idx][1]))
                        p1 = self._pdf_to_screen_point(float(stroke[idx + 1][0]), float(stroke[idx + 1][1]))
                        pressure = float(stroke[idx][2]) if len(stroke[idx]) >= 3 else 0.6
                        pen = QPen(
                            col,
                            max(1.0, rec.stroke_width * self._zoom * (0.6 + 0.8 * pressure)),
                            Qt.PenStyle.SolidLine,
                            Qt.PenCapStyle.RoundCap,
                            Qt.PenJoinStyle.RoundJoin,
                        )
                        painter.setPen(pen)
                        painter.drawLine(p0, p1)
            elif rec.kind in ("freetext", "popup"):
                sr = self._pdf_to_screen_rect(rec.rect_pdf)
                painter.setPen(QPen(col, 1))
                painter.setBrush(QColor("#FFFFFF" if rec.kind == "freetext" else "#FEF9C3"))
                painter.drawRect(sr)
                if rec.content:
                    painter.setPen(QColor("#0F172A"))
                    painter.drawText(sr.adjusted(4, 2, -4, -2), Qt.TextFlag.TextWordWrap, rec.content)

        # In-progress strokes
        for active in (self._active_tablet_stroke, self._active_mouse_stroke, self._active_touch_stroke):
            if len(active) >= 2:
                pen = QPen(
                    QColor(self._ink_color),
                    max(1.2, self._ink_width * self._zoom),
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
