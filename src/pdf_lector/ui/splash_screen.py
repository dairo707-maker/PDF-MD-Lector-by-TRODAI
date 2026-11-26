"""Adobe-style temporary startup splash screen with PDF Lector logo, TRODAI seal, and live loader."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import QApplication, QHBoxLayout, QLabel, QProgressBar, QVBoxLayout, QWidget

from .. import APP_NAME, COMPANY_NAME, read_version
from ..core.i18n import t
from ..core.win32_bridge import apply_trodai_window_chrome
from ..theme.palette import MONO_FONT_FAMILY, UI_FONT_FAMILY, get_palette
from .svg_icons import load_multicolor_svg_pixmap, load_svg_pixmap
from .widgets import StatusTag


def _fast_font(primary: str, fallback: str, size: int, weight: QFont.Weight) -> QFont:
    font = QFont(primary, size)
    font.setFamilies([primary, fallback])
    font.setWeight(weight)
    return font


class SplashScreen(QWidget):
    """Frameless TRODAI 0px splash window displayed immediately (<300ms) until MainWindow is ready."""

    def __init__(self, *, mode: str = "dark", lang: str = "es", accent_override: str = "") -> None:
        super().__init__(None)
        self._mode = mode
        self._lang = lang
        self._accent_override = accent_override
        self._progress = 0

        self.setObjectName("SplashWindow")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.SplashScreen
        )
        self.setFixedSize(490, 276)

        root = QVBoxLayout(self)
        root.setContentsMargins(1, 1, 1, 1)
        root.setSpacing(0)

        self.top_stripe = QWidget(self)
        self.top_stripe.setFixedHeight(3)
        root.addWidget(self.top_stripe)

        body = QVBoxLayout()
        body.setContentsMargins(24, 20, 24, 16)
        body.setSpacing(12)

        head = QHBoxLayout()
        head.setSpacing(16)

        self.app_logo_lbl = QLabel(self)
        self.app_logo_lbl.setFixedSize(64, 64)
        head.addWidget(self.app_logo_lbl, 0, Qt.AlignmentFlag.AlignTop)

        title_col = QVBoxLayout()
        title_col.setSpacing(4)

        title_row = QHBoxLayout()
        title_row.setSpacing(10)
        self.title_lbl = QLabel(APP_NAME, self)
        self.title_lbl.setFont(_fast_font(UI_FONT_FAMILY, "Segoe UI", 20, QFont.Weight.Bold))
        title_row.addWidget(self.title_lbl)

        self.version_tag = StatusTag(f"v{read_version()}", tone="crimson", parent=self)
        title_row.addWidget(self.version_tag)
        title_row.addStretch(1)
        title_col.addLayout(title_row)

        self.subtitle_lbl = QLabel(t("app.subtitle", lang), self)
        self.subtitle_lbl.setFont(_fast_font(UI_FONT_FAMILY, "Segoe UI", 10, QFont.Weight.Normal))
        self.subtitle_lbl.setWordWrap(True)
        title_col.addWidget(self.subtitle_lbl)

        badges_row = QHBoxLayout()
        badges_row.setSpacing(6)
        self.badge_engine = StatusTag(t("splash.badge_engine", lang), tone="accent", parent=self)
        self.badge_local = StatusTag(t("splash.badge_local", lang), tone="ok", parent=self)
        self.badge_privacy = StatusTag(t("splash.badge_privacy", lang), tone="muted", parent=self)
        badges_row.addWidget(self.badge_engine)
        badges_row.addWidget(self.badge_local)
        badges_row.addWidget(self.badge_privacy)
        badges_row.addStretch(1)
        title_col.addLayout(badges_row)

        head.addLayout(title_col, 1)
        body.addLayout(head)
        body.addStretch(1)

        self.seal_divider = QWidget(self)
        self.seal_divider.setFixedHeight(1)
        body.addWidget(self.seal_divider)

        seal_row = QHBoxLayout()
        seal_row.setContentsMargins(0, 6, 0, 2)
        seal_row.setSpacing(8)

        self.sentinel_lbl = QLabel(self)
        self.sentinel_lbl.setFixedSize(22, 22)
        seal_row.addWidget(self.sentinel_lbl)

        self.company_lbl = QLabel(f"by {COMPANY_NAME}", self)
        comp_font = _fast_font(UI_FONT_FAMILY, "Segoe UI", 10, QFont.Weight.Medium)
        comp_font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.8)
        self.company_lbl.setFont(comp_font)
        seal_row.addWidget(self.company_lbl)
        seal_row.addStretch(1)

        self.license_lbl = QLabel("MIT OPEN SOURCE · WIN 11", self)
        self.license_lbl.setFont(_fast_font(MONO_FONT_FAMILY, "Consolas", 8, QFont.Weight.Medium))
        seal_row.addWidget(self.license_lbl)

        body.addLayout(seal_row)

        status_row = QHBoxLayout()
        status_row.setContentsMargins(0, 2, 0, 0)
        st_font = _fast_font(MONO_FONT_FAMILY, "Consolas", 8, QFont.Weight.Normal)
        self.status_lbl = QLabel(t("splash.step.fonts", lang), self)
        self.status_lbl.setFont(st_font)
        status_row.addWidget(self.status_lbl, 1)

        self.pct_lbl = QLabel("0%", self)
        self.pct_lbl.setFont(st_font)
        status_row.addWidget(self.pct_lbl)
        body.addLayout(status_row)

        root.addLayout(body, 1)

        self.progress_bar = QProgressBar(self)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(5)
        root.addWidget(self.progress_bar)

        self._apply_theme()
        self._center_on_screen()

    def _center_on_screen(self) -> None:
        screen = QApplication.primaryScreen()
        if screen is not None:
            geo = screen.availableGeometry()
            x = geo.x() + (geo.width() - self.width()) // 2
            y = geo.y() + (geo.height() - self.height()) // 2
            self.move(x, y)

    def _apply_theme(self) -> None:
        p = get_palette(self._mode, self._accent_override)
        self.top_stripe.setStyleSheet(f"background-color: {p['border_selected']};")
        self.seal_divider.setStyleSheet(f"background-color: {p['border_hairline']};")
        self.title_lbl.setStyleSheet(f"color: {p['text_primary']};")
        self.subtitle_lbl.setStyleSheet(f"color: {p['text_secondary']};")
        self.company_lbl.setStyleSheet(f"color: {p['text_primary']};")
        self.license_lbl.setStyleSheet(f"color: {p['text_muted']};")
        self.status_lbl.setStyleSheet(f"color: {p['text_secondary']};")
        self.pct_lbl.setStyleSheet(f"color: {p['text_primary']};")
        self.progress_bar.setStyleSheet(
            f"QProgressBar {{ background-color: {p['border_hairline']}; border: none; }}"
            f"QProgressBar::chunk {{ background-color: {p['app_crimson']}; }}"
        )

        self.app_logo_lbl.setPixmap(load_multicolor_svg_pixmap("pdf_lector_logo", size=64))
        self.sentinel_lbl.setPixmap(
            load_svg_pixmap("trodai_sentinel", color=p["text_secondary"], size=20)
        )
        if self._mode != "dark":
            self.version_tag.apply_theme(self._mode, self._accent_override)
            self.badge_engine.apply_theme(self._mode, self._accent_override)
            self.badge_local.apply_theme(self._mode, self._accent_override)
            self.badge_privacy.apply_theme(self._mode, self._accent_override)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        try:
            apply_trodai_window_chrome(int(self.winId()), dark_mode=(self._mode == "dark"), acrylic=False)
        except Exception:
            pass

    def paintEvent(self, event) -> None:
        del event
        p = get_palette(self._mode, self._accent_override)
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(p["bg_window_hex"]))
        painter.setPen(QColor(p["border_strong"]))
        painter.drawRect(0, 0, self.width() - 1, self.height() - 1)
        painter.end()

    def set_progress(self, percent: int, message: str) -> None:
        """Update bottom visual progress bar and status message immediately."""
        self._progress = max(0, min(100, int(percent)))
        self.progress_bar.setValue(self._progress)
        self.pct_lbl.setText(f"{self._progress}%")
        self.status_lbl.setText(message)
        self.repaint()
        app = QApplication.instance()
        if app is not None:
            app.processEvents()

    @property
    def progress_value(self) -> int:
        return self._progress

    def finish_and_close(self, target_window: QWidget | None = None) -> None:
        """Complete bar to 100%, show main window if provided, and close splash cleanly."""
        self.set_progress(100, t("splash.step.ready", self._lang))
        if target_window is not None:
            target_window.show()
            target_window.raise_()
            target_window.activateWindow()
        self.close()
