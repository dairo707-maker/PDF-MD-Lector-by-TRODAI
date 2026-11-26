"""Reusable TRODAI widgets: FlowLayout, AdaptiveButton, Solid StatusTag, Uniform ToolCard & LiquidGlass Sliding Controls."""

from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QPoint, QRect, QRectF, QSize, QVariantAnimation, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLayoutItem,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..theme.palette import MONO_FONT_FAMILY, UI_FONT_FAMILY, get_palette
from .svg_icons import load_svg_pixmap


def _ui_font(point_size: int, weight: QFont.Weight = QFont.Weight.Normal) -> QFont:
    font = QFont(UI_FONT_FAMILY, point_size)
    font.setFamilies([UI_FONT_FAMILY, "Segoe UI"])
    font.setWeight(weight)
    return font


def _mono_font(point_size: int, weight: QFont.Weight = QFont.Weight.Medium) -> QFont:
    font = QFont(MONO_FONT_FAMILY, point_size)
    font.setFamilies([MONO_FONT_FAMILY, "Consolas"])
    font.setWeight(weight)
    return font


def set_button_role(button: QPushButton, role: str) -> None:
    """Assign TRODAI semantic role property and refresh widget polish."""
    button.setProperty("role", role)
    button.style().unpolish(button)
    button.style().polish(button)


class FlowLayout(QLayout):
    """Responsive wrapping layout preventing horizontal button or card clipping."""

    def __init__(self, parent: QWidget | None = None, margin: int = 0, h_spacing: int = 8, v_spacing: int = 8) -> None:
        super().__init__(parent)
        self._items: list[QLayoutItem] = []
        self._h_space = h_spacing
        self._v_space = v_spacing
        self.setContentsMargins(margin, margin, margin, margin)

    def addItem(self, item: QLayoutItem) -> None:
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> QLayoutItem | None:
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index: int) -> QLayoutItem | None:
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self) -> Qt.Orientation:
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._do_layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        size += QSize(margins.left() + margins.right(), margins.top() + margins.bottom())
        return size

    def _do_layout(self, rect: QRect, *, test_only: bool) -> int:
        left, top, right, bottom = self.getContentsMargins()
        effective = rect.adjusted(left, top, -right, -bottom)
        x = effective.x()
        y = effective.y()
        line_height = 0

        for item in self._items:
            wid = item.widget()
            if wid is not None and not wid.isVisibleTo(wid.parentWidget()):
                continue
            item_sz = item.sizeHint()
            next_x = x + item_sz.width() + self._h_space
            if next_x - self._h_space > effective.right() + 1 and line_height > 0:
                x = effective.x()
                y = y + line_height + self._v_space
                next_x = x + item_sz.width() + self._h_space
                line_height = 0

            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), item_sz))

            x = next_x
            line_height = max(line_height, item_sz.height())

        return y + line_height - rect.y() + bottom


class AdaptiveButton(QPushButton):
    """PushButton ensuring full text fit and adaptive multi-variant labels across widths."""

    def __init__(self, text: str = "", parent: QWidget | None = None, *, role: str = "GhostAction") -> None:
        super().__init__(text, parent)
        self._variants: list[str] = [text] if text else []
        self._explicit_tooltip = ""
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        self.setMinimumHeight(32)
        set_button_role(self, role)

    def setText(self, text: str) -> None:
        super().setText(text)
        if not self._variants and text:
            self._variants = [text]

    def setToolTip(self, tip: str) -> None:
        self._explicit_tooltip = tip
        super().setToolTip(tip)

    def set_variants(self, variants: list[str]) -> None:
        self._variants = [v for v in variants if v]
        if self._variants:
            super().setText(self._variants[0])
            if len(self._variants) > 1 and not self._explicit_tooltip:
                super().setToolTip(self._variants[0])
        self.updateGeometry()
        if self.width() > 0:
            self._fit_variant_to_width(self.width())

    def resize(self, *args) -> None:
        super().resize(*args)
        self._fit_variant_to_width(self.width())

    def setFixedWidth(self, w: int) -> None:  # noqa: N802
        super().setFixedWidth(w)
        self._fit_variant_to_width(self.width())

    def setGeometry(self, *args) -> None:  # noqa: N802
        super().setGeometry(*args)
        self._fit_variant_to_width(self.width())

    def _fit_variant_to_width(self, width: int) -> None:
        if not self._variants:
            return
        fm = QFontMetrics(self.font())
        icon_w = (self.iconSize().width() + 8) if not self.icon().isNull() else 0
        avail = max(0, width - icon_w - 24)
        chosen = self._variants[-1]
        for variant in self._variants:
            if fm.horizontalAdvance(variant) <= avail:
                chosen = variant
                break
        if chosen != self.text():
            super().setText(chosen)
        if chosen != self._variants[0]:
            super().setToolTip(self._explicit_tooltip or self._variants[0])
        elif self._explicit_tooltip:
            super().setToolTip(self._explicit_tooltip)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._fit_variant_to_width(event.size().width())

    def minimumSizeHint(self) -> QSize:
        fm = QFontMetrics(self.font())
        icon_w = (self.iconSize().width() + 8) if not self.icon().isNull() else 0
        if self._variants:
            shortest = min(fm.horizontalAdvance(v) for v in self._variants)
            return QSize(shortest + icon_w + 20, max(32, super().minimumSizeHint().height()))
        text_w = fm.horizontalAdvance(self.text())
        return QSize(text_w + icon_w + 24, max(32, super().minimumSizeHint().height()))

    def sizeHint(self) -> QSize:
        base = super().sizeHint()
        fm = QFontMetrics(self.font())
        ref_text = self._variants[0] if self._variants else self.text()
        text_w = fm.horizontalAdvance(ref_text)
        icon_w = (self.iconSize().width() + 8) if not self.icon().isNull() else 0
        min_w = text_w + icon_w + 30
        return QSize(max(base.width(), min_w), max(base.height(), 32))


class StatusTag(QLabel):
    """100% opaque solid flat technical 0px status badge (zero transparency)."""

    _SOLID_THEMES: dict[str, dict[str, tuple[str, str, str]]] = {
        "dark": {
            "ok": ("#152E28", "#2B6A5A", "#6EE7B7"),
            "accent": ("#1A2438", "#334155", "#E2E8F0"),
            "crimson": ("#3A191E", "#7F2933", "#FCA5A5"),
            "warn": ("#352814", "#78561F", "#FCD34D"),
            "muted": ("#161E2E", "#2A364B", "#CBD5E1"),
            "risk": ("#3A191E", "#7F2933", "#FCA5A5"),
        },
        "light": {
            "ok": ("#DCFCE7", "#86EFAC", "#166534"),
            "accent": ("#E2E8F0", "#CBD5E1", "#0F172A"),
            "crimson": ("#FEE2E2", "#FCA5A5", "#991B1B"),
            "warn": ("#FEF3C7", "#FCD34D", "#92400E"),
            "muted": ("#EEF2F6", "#CBD5E1", "#334155"),
            "risk": ("#FEE2E2", "#FCA5A5", "#991B1B"),
        },
    }

    def __init__(self, text: str = "", tone: str = "accent", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self._tone = tone
        self._mode = "dark"
        self._accent_override = ""
        self.setFont(_mono_font(8, QFont.Weight.Medium))
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setContentsMargins(7, 2, 7, 2)
        self.apply_theme("dark")

    def set_tone(self, tone: str, text: str | None = None) -> None:
        self._tone = tone
        if text is not None:
            self.setText(text)
        self.apply_theme(self._mode, self._accent_override)

    def apply_theme(self, mode: str, accent_override: str = "") -> None:
        self._mode = "light" if mode == "light" else "dark"
        self._accent_override = accent_override
        palette = self._SOLID_THEMES[self._mode]
        bg, bd, fg = palette.get(self._tone, palette["accent"])
        self.setStyleSheet(
            f"background-color: {bg}; color: {fg}; border: 1px solid {bd}; "
            f"padding: 2px 6px; font-family: '{MONO_FONT_FAMILY}', 'Consolas', monospace; "
            f"font-size: 10px; font-weight: 500;"
        )


class ToolCard(QFrame):
    """Uniform standard-size architectural card (232x104) for harmonic Hub alignment."""

    CARD_WIDTH = 232
    CARD_HEIGHT = 104

    clicked = Signal(str)

    def __init__(
        self,
        tool_id: str,
        icon_name: str,
        title: str,
        description: str,
        badge: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.tool_id = tool_id
        self._icon_name = icon_name
        self._badge_text = badge
        self.setObjectName("ToolCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(self.CARD_WIDTH, self.CARD_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 10, 12, 10)
        outer.setSpacing(6)

        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(8)

        self.icon_lbl = QLabel(self)
        self.icon_lbl.setFixedSize(18, 18)
        head.addWidget(self.icon_lbl)

        self.title_lbl = QLabel(title, self)
        self.title_lbl.setFont(_ui_font(10, QFont.Weight.Bold))
        self.title_lbl.setWordWrap(True)
        head.addWidget(self.title_lbl, 1)

        self.badge_tag = StatusTag(badge, tone="accent", parent=self)
        self.badge_tag.setVisible(bool(badge))
        head.addWidget(self.badge_tag)

        outer.addLayout(head)

        self.desc_lbl = QLabel(description, self)
        self.desc_lbl.setProperty("tone", "muted")
        self.desc_lbl.setFont(_ui_font(9, QFont.Weight.Normal))
        self.desc_lbl.setWordWrap(True)
        self.desc_lbl.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        outer.addWidget(self.desc_lbl, 1)

        self.apply_theme("dark")

    def set_content(self, title: str, description: str) -> None:
        self.title_lbl.setText(title)
        self.desc_lbl.setText(description)

    def apply_theme(self, mode: str, accent_override: str = "") -> None:
        p = get_palette(mode, accent_override)
        self.icon_lbl.setPixmap(load_svg_pixmap(self._icon_name, color=p["text_secondary"], size=17))
        if self._badge_text:
            self.badge_tag.apply_theme(mode, accent_override)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.tool_id)
        super().mousePressEvent(event)


class SlidingSegmentedControl(QWidget):
    """LiquidGlass-style fluid horizontal sliding selector with solid neutral surfaces (zero accent)."""

    selection_changed = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._options: list[tuple[str, str]] = []
        self._current_index = 0
        self._hover_index = -1
        self._mode = "dark"
        self._animate = True
        self._pill_x = 2.0
        self._pill_w = 60.0

        self._anim = QVariantAnimation(self)
        self._anim.setDuration(200)
        self._anim.setEasingCurve(QEasingCurve.Type.OutQuint)
        self._anim.valueChanged.connect(self._on_anim_value)

        self.setFixedHeight(34)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_options(self, options: list[tuple[str, str]]) -> None:
        self._options = list(options)
        self._current_index = min(self._current_index, max(0, len(self._options) - 1))
        self.updateGeometry()
        self._snap_pill()
        self.update()

    def set_animations_enabled(self, enabled: bool) -> None:
        self._animate = bool(enabled)

    def apply_theme(self, mode: str) -> None:
        self._mode = "light" if mode == "light" else "dark"
        self.update()

    def current_key(self) -> str:
        if 0 <= self._current_index < len(self._options):
            return self._options[self._current_index][0]
        return ""

    def set_current(self, key: str, *, animate: bool = True, emit_signal: bool = False) -> None:
        for idx, (opt_key, _) in enumerate(self._options):
            if opt_key == key:
                self._select_index(idx, animate=animate, emit_signal=emit_signal)
                return

    def sizeHint(self) -> QSize:
        if not self._options:
            return QSize(120, 34)
        fm = QFontMetrics(self.font())
        max_txt = max(fm.horizontalAdvance(label) for _, label in self._options)
        seg_w = max(72, max_txt + 22)
        return QSize(seg_w * len(self._options) + 4, 34)

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def _segment_rect(self, index: int) -> QRectF:
        count = max(1, len(self._options))
        inner_w = max(10.0, float(self.width() - 4))
        seg_w = inner_w / count
        return QRectF(2.0 + index * seg_w, 2.0, seg_w, float(self.height() - 4))

    def _snap_pill(self) -> None:
        rect = self._segment_rect(self._current_index)
        self._pill_x = rect.x()
        self._pill_w = rect.width()

    def _on_anim_value(self, value: object) -> None:
        if isinstance(value, QRectF):
            self._pill_x = value.x()
            self._pill_w = value.width()
        else:
            self._pill_x = float(value)
        self.update()

    def _select_index(self, idx: int, *, animate: bool, emit_signal: bool) -> None:
        if not (0 <= idx < len(self._options)):
            return
        changed = idx != self._current_index
        self._current_index = idx
        target = self._segment_rect(idx)

        if animate and self._animate and self.isVisible() and self.width() > 20:
            self._anim.stop()
            self._anim.setStartValue(QRectF(self._pill_x, 2.0, self._pill_w, target.height()))
            self._anim.setEndValue(target)
            self._anim.start()
        else:
            self._anim.stop()
            self._pill_x = target.x()
            self._pill_w = target.width()
            self.update()

        if changed and emit_signal:
            self.selection_changed.emit(self._options[idx][0])

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._anim.state() != QVariantAnimation.State.Running:
            self._snap_pill()

    def mouseMoveEvent(self, event) -> None:
        count = max(1, len(self._options))
        seg_w = max(1.0, (self.width() - 4) / count)
        idx = int((event.position().x() - 2.0) / seg_w)
        idx = idx if 0 <= idx < len(self._options) else -1
        if idx != self._hover_index:
            self._hover_index = idx
            self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:
        self._hover_index = -1
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._options:
            seg_w = max(1.0, (self.width() - 4) / len(self._options))
            idx = max(0, min(len(self._options) - 1, int((event.position().x() - 2.0) / seg_w)))
            self._select_index(idx, animate=True, emit_signal=True)
        super().mousePressEvent(event)

    def paintEvent(self, event) -> None:
        del event
        p = get_palette(self._mode)
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(p["bg_track_hex"]))
        painter.setPen(QPen(QColor(p["border_hairline"]), 1))
        painter.drawRect(0, 0, self.width() - 1, self.height() - 1)

        if not self._options:
            painter.end()
            return

        if 0 <= self._hover_index < len(self._options) and self._hover_index != self._current_index:
            hr = self._segment_rect(self._hover_index)
            painter.fillRect(hr.toRect(), QColor(p["bg_hover"]))

        pill_rect = QRect(int(round(self._pill_x)), 2, max(8, int(round(self._pill_w))), self.height() - 4)
        painter.fillRect(pill_rect, QColor(p["bg_selected_hex"]))
        painter.setPen(QPen(QColor(p["border_selected"]), 1))
        painter.drawRect(pill_rect.adjusted(0, 0, -1, -1))
        sheen = "#475569" if self._mode == "dark" else "#FFFFFF"
        painter.setPen(QPen(QColor(sheen), 1))
        painter.drawLine(pill_rect.left() + 1, pill_rect.top() + 1, pill_rect.right() - 1, pill_rect.top() + 1)

        base_font = _ui_font(9, QFont.Weight.Medium)
        for idx, (_, label) in enumerate(self._options):
            sr = self._segment_rect(idx).toRect()
            is_sel = idx == self._current_index
            base_font.setWeight(QFont.Weight.Bold if is_sel else QFont.Weight.Medium)
            painter.setFont(base_font)
            painter.setPen(QColor(p["text_primary"] if is_sel else p["text_secondary"]))
            painter.drawText(sr, Qt.AlignmentFlag.AlignCenter, label)

        painter.end()


class SlidingNavPanel(QWidget):
    """Sidebar navigation with smooth vertical gliding neutral pill (zero cyan accent wash)."""

    current_changed = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._items: list[tuple[str, str]] = []
        self._current_index = 0
        self._hover_index = -1
        self._mode = "dark"
        self._animate = True
        self._row_height = 40
        self._row_gap = 4
        self._pill_y = 0.0

        self._anim = QVariantAnimation(self)
        self._anim.setDuration(190)
        self._anim.setEasingCurve(QEasingCurve.Type.OutQuint)
        self._anim.valueChanged.connect(self._on_anim_val)

        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_items(self, items: list[tuple[str, str]]) -> None:
        self._items = list(items)
        total_h = len(self._items) * (self._row_height + self._row_gap)
        self.setFixedHeight(max(40, total_h))
        self._pill_y = float(self._row_y(self._current_index))
        self.update()

    def update_labels(self, labels: list[str]) -> None:
        for idx, lbl in enumerate(labels):
            if idx < len(self._items):
                self._items[idx] = (self._items[idx][0], lbl)
        self.update()

    def set_animations_enabled(self, enabled: bool) -> None:
        self._animate = bool(enabled)

    def apply_theme(self, mode: str) -> None:
        self._mode = "light" if mode == "light" else "dark"
        self.update()

    def set_current_index(self, idx: int, *, animate: bool = True, emit_signal: bool = False) -> None:
        if not (0 <= idx < len(self._items)):
            return
        changed = idx != self._current_index
        self._current_index = idx
        target_y = float(self._row_y(idx))
        if animate and self._animate and self.isVisible():
            self._anim.stop()
            self._anim.setStartValue(self._pill_y)
            self._anim.setEndValue(target_y)
            self._anim.start()
        else:
            self._anim.stop()
            self._pill_y = target_y
            self.update()
        if changed and emit_signal:
            self.current_changed.emit(idx)

    def _row_y(self, idx: int) -> int:
        return idx * (self._row_height + self._row_gap)

    def _on_anim_val(self, val: object) -> None:
        self._pill_y = float(val)
        self.update()

    def _index_at_y(self, y: float) -> int:
        step = self._row_height + self._row_gap
        idx = int(y // step)
        if 0 <= idx < len(self._items) and (y % step) <= self._row_height:
            return idx
        return -1

    def mouseMoveEvent(self, event) -> None:
        idx = self._index_at_y(event.position().y())
        if idx != self._hover_index:
            self._hover_index = idx
            self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:
        self._hover_index = -1
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            idx = self._index_at_y(event.position().y())
            if idx >= 0:
                self.set_current_index(idx, animate=True, emit_signal=True)
        super().mousePressEvent(event)

    def paintEvent(self, event) -> None:
        del event
        p = get_palette(self._mode)
        painter = QPainter(self)

        if 0 <= self._hover_index < len(self._items) and self._hover_index != self._current_index:
            hy = self._row_y(self._hover_index)
            painter.fillRect(0, hy, self.width(), self._row_height, QColor(p["bg_hover"]))

        if self._items:
            py = int(round(self._pill_y))
            pill = QRect(0, py, self.width(), self._row_height)
            painter.fillRect(pill, QColor(p["bg_selected_hex"]))
            painter.setPen(QPen(QColor(p["border_strong"]), 1))
            painter.drawRect(pill.adjusted(0, 0, -1, -1))
            sheen = "#475569" if self._mode == "dark" else "#FFFFFF"
            painter.setPen(QPen(QColor(sheen), 1))
            painter.drawLine(pill.left() + 1, pill.top() + 1, pill.right() - 1, pill.top() + 1)

        font = _ui_font(10, QFont.Weight.Medium)
        for idx, (icon_name, label) in enumerate(self._items):
            ry = self._row_y(idx)
            is_sel = idx == self._current_index
            icon_color = p["text_primary"] if is_sel else p["text_secondary"]
            pm = load_svg_pixmap(icon_name, color=icon_color, size=16)
            painter.drawPixmap(12, ry + (self._row_height - 16) // 2, pm)

            font.setWeight(QFont.Weight.Bold if is_sel else QFont.Weight.Medium)
            painter.setFont(font)
            painter.setPen(QColor(p["text_primary"] if is_sel else p["text_secondary"]))
            text_rect = QRect(38, ry, max(10, self.width() - 46), self._row_height)
            painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, label)

        painter.end()
