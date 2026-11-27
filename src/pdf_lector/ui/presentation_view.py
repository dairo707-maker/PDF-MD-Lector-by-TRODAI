"""Full-screen Presentation Mode with wireless clicker support and auto-hiding bottom HUD."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)
import shiboken6

from ..core.pdf_engine import PdfDocumentSession
from ..theme.palette import MONO_FONT_FAMILY, UI_FONT_FAMILY
from .svg_icons import load_svg_icon


class PresentationWindow(QWidget):
    """Full-screen PDF slide presenter compatible with wireless clickers and hover bottom exit HUD."""

    closed_at_page = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.setWindowTitle("Modo Presentacion · PDF/MD Lector")
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self._session: PdfDocumentSession | None = None
        self._page_index: int = 0
        self._slide_pixmap: QPixmap | None = None
        self._blank_mode: str | None = None  # None | "black" | "white"

        # Auto-hiding bottom-center floating HUD
        self.hud = QFrame(self)
        self.hud.setObjectName("PresentationHud")
        self.hud.setStyleSheet(
            "QFrame#PresentationHud { background-color: #0D1526; border: 1px solid #334155; } "
            "QFrame#PresentationHud QLabel { color: #F8FAFC; background: transparent; border: none; } "
            "QFrame#PresentationHud QPushButton { background-color: transparent; color: #F8FAFC; "
            "border: 1px solid transparent; padding: 4px 10px; } "
            "QFrame#PresentationHud QPushButton:hover { background-color: #1E293B; }"
        )
        hud_lay = QHBoxLayout(self.hud)
        hud_lay.setContentsMargins(10, 6, 10, 6)
        hud_lay.setSpacing(6)

        self.prev_btn = QPushButton("", self.hud)
        self.prev_btn.setProperty("iconOnly", "true")
        self.prev_btn.setFixedSize(32, 30)
        self.prev_btn.setToolTip("Diapositiva anterior (PgUp / Flecha izq.)")
        self.prev_btn.setIcon(load_svg_icon("prev", color="#F8FAFC", size=16))
        self.prev_btn.clicked.connect(lambda: self.step_slide(-1))
        hud_lay.addWidget(self.prev_btn)

        self.page_lbl = QLabel("1 / 1", self.hud)
        self.page_lbl.setFont(QFont(MONO_FONT_FAMILY, 10, QFont.Weight.Bold))
        hud_lay.addWidget(self.page_lbl)

        self.next_btn = QPushButton("", self.hud)
        self.next_btn.setProperty("iconOnly", "true")
        self.next_btn.setFixedSize(32, 30)
        self.next_btn.setToolTip("Siguiente diapositiva (PgDn / Espacio / Clic)")
        self.next_btn.setIcon(load_svg_icon("next", color="#F8FAFC", size=16))
        self.next_btn.clicked.connect(lambda: self.step_slide(1))
        hud_lay.addWidget(self.next_btn)

        self.blank_btn = QPushButton("", self.hud)
        self.blank_btn.setProperty("iconOnly", "true")
        self.blank_btn.setFixedSize(32, 30)
        self.blank_btn.setToolTip("Alternar pantalla en negro (B / .)")
        self.blank_btn.setIcon(load_svg_icon("moon", color="#F8FAFC", size=15))
        self.blank_btn.clicked.connect(lambda: self.toggle_blank("black"))
        hud_lay.addWidget(self.blank_btn)

        self.exit_btn = QPushButton("", self.hud)
        self.exit_btn.setProperty("iconOnly", "true")
        self.exit_btn.setFixedSize(32, 30)
        self.exit_btn.setToolTip("Salir del modo presentacion (Esc)")
        self.exit_btn.setIcon(load_svg_icon("close", color="#FCA5A5", size=15))
        self.exit_btn.clicked.connect(self.exit_presentation)
        hud_lay.addWidget(self.exit_btn)

        self.hud.adjustSize()
        self.hud.setVisible(False)

        self._hide_hud_timer = QTimer(self)
        self._hide_hud_timer.setSingleShot(True)
        self._hide_hud_timer.setInterval(1500)
        self._hide_hud_timer.timeout.connect(self._on_hud_timeout)

    def start_presentation(self, session: PdfDocumentSession, start_page: int = 0) -> None:
        self._session = session
        self._blank_mode = None
        self._page_index = max(0, min(max(0, session.page_count - 1), int(start_page)))
        if QApplication.platformName() != "offscreen":
            self.showFullScreen()
        else:
            self.resize(1280, 720)
            self.show()
        self._render_current_slide()
        self._position_hud()
        self.setFocus()
        self.raise_()

    def _render_current_slide(self) -> None:
        if self._session is None or self._session.page_count <= 0:
            self._slide_pixmap = None
            return
        avail_w = max(640, self.width())
        avail_h = max(480, self.height())
        try:
            pw, ph = self._session.get_page_size(self._page_index)
            scale = max(1.0, min(3.5, min((avail_w - 24) / max(10.0, pw), (avail_h - 24) / max(10.0, ph))))
            img = self._session.render_page(self._page_index, scale=scale, rotation=0)
            self._slide_pixmap = QPixmap.fromImage(img) if not img.isNull() else None
        except Exception:
            self._slide_pixmap = None
        self.page_lbl.setText(f"{self._page_index + 1} / {self._session.page_count}")
        self.update()

    def step_slide(self, delta: int) -> None:
        if self._session is None or self._session.page_count <= 0:
            return
        self._blank_mode = None
        nxt = max(0, min(self._session.page_count - 1, self._page_index + int(delta)))
        if nxt != self._page_index:
            self._page_index = nxt
            self._render_current_slide()
        else:
            self.update()

    def goto_slide(self, page_index: int) -> None:
        if self._session is None or self._session.page_count <= 0:
            return
        self._blank_mode = None
        nxt = max(0, min(self._session.page_count - 1, int(page_index)))
        self._page_index = nxt
        self._render_current_slide()

    def toggle_blank(self, color_mode: str = "black") -> None:
        self._blank_mode = None if self._blank_mode == color_mode else color_mode
        self.update()

    def reveal_hud(self) -> None:
        self._position_hud()
        self.hud.setVisible(True)
        self.hud.raise_()
        self._hide_hud_timer.start()

    def _on_hud_timeout(self) -> None:
        if not shiboken6.isValid(self):
            return
        if not self.hud.underMouse():
            self.hud.setVisible(False)
        else:
            self._hide_hud_timer.start()

    def _position_hud(self) -> None:
        self.hud.adjustSize()
        x = max(12, (self.width() - self.hud.width()) // 2)
        y = max(12, self.height() - self.hud.height() - 18)
        self.hud.move(x, y)

    def exit_presentation(self) -> None:
        self._hide_hud_timer.stop()
        self.hud.setVisible(False)
        final_page = self._page_index
        self.hide()
        self.closed_at_page.emit(final_page)

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._position_hud()
        if self._session is not None:
            self._render_current_slide()

    def mouseMoveEvent(self, event) -> None:  # type: ignore[override]
        pos = event.position().toPoint()
        in_bottom_center = (
            pos.y() >= max(0, self.height() - 110)
            and abs(pos.x() - self.width() // 2) <= max(240, self.width() // 3)
        )
        if in_bottom_center:
            self.reveal_hud()
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if self.hud.isVisible() and self.hud.geometry().contains(event.position().toPoint()):
            super().mousePressEvent(event)
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self.step_slide(1)
            event.accept()
            return
        if event.button() == Qt.MouseButton.RightButton:
            self.step_slide(-1)
            event.accept()
            return
        super().mousePressEvent(event)

    def wheelEvent(self, event) -> None:  # type: ignore[override]
        dy = event.angleDelta().y()
        if dy < 0:
            self.step_slide(1)
        elif dy > 0:
            self.step_slide(-1)
        event.accept()

    def keyPressEvent(self, event) -> None:  # type: ignore[override]
        k = event.key()
        if k == Qt.Key.Key_Escape:
            self.exit_presentation()
            event.accept()
            return
        if k in (
            Qt.Key.Key_PageDown,
            Qt.Key.Key_Right,
            Qt.Key.Key_Down,
            Qt.Key.Key_Space,
            Qt.Key.Key_Return,
            Qt.Key.Key_Enter,
            Qt.Key.Key_N,
            getattr(Qt.Key, "Key_MediaNext", Qt.Key.Key_PageDown),
        ):
            self.step_slide(1)
            event.accept()
            return
        if k in (
            Qt.Key.Key_PageUp,
            Qt.Key.Key_Left,
            Qt.Key.Key_Up,
            Qt.Key.Key_Backspace,
            Qt.Key.Key_P,
            getattr(Qt.Key, "Key_MediaPrevious", Qt.Key.Key_PageUp),
        ):
            self.step_slide(-1)
            event.accept()
            return
        if k == Qt.Key.Key_Home:
            self.goto_slide(0)
            event.accept()
            return
        if k == Qt.Key.Key_End and self._session is not None:
            self.goto_slide(self._session.page_count - 1)
            event.accept()
            return
        if k in (Qt.Key.Key_B, Qt.Key.Key_Period):
            self.toggle_blank("black")
            event.accept()
            return
        if k == Qt.Key.Key_W:
            self.toggle_blank("white")
            event.accept()
            return
        super().keyPressEvent(event)

    def paintEvent(self, event) -> None:  # type: ignore[override]
        del event
        painter = QPainter(self)
        if self._blank_mode == "black":
            painter.fillRect(self.rect(), QColor("#000000"))
            painter.end()
            return
        if self._blank_mode == "white":
            painter.fillRect(self.rect(), QColor("#FFFFFF"))
            painter.end()
            return

        painter.fillRect(self.rect(), QColor("#05080F"))
        if self._slide_pixmap is not None and not self._slide_pixmap.isNull():
            scaled = self._slide_pixmap.scaled(
                max(100, self.width() - 16),
                max(100, self.height() - 16),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            x = (self.width() - scaled.width()) // 2
            y = (self.height() - scaled.height()) // 2
            painter.drawPixmap(x, y, scaled)
        painter.end()
