"""DiagnosticsDialog displaying CrashGuard session status, crash.log/app.log tails, and export."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..core.crash_guard import CrashGuard
from ..core.feedback_reporter import anonymize_text
from ..core.win32_bridge import default_app_data_dir
from ..theme.palette import MONO_FONT_FAMILY, UI_FONT_FAMILY, get_palette
from .svg_icons import load_svg_icon
from .widgets import AdaptiveButton, FlowLayout, StatusTag


class DiagnosticsDialog(QDialog):
    """Diagnostics & CrashGuard inspection dialog reading crash.log and app.log safely."""

    def __init__(
        self,
        *,
        logs_dir: Path | None = None,
        crash_guard: CrashGuard | None = None,
        lang: str = "es",
        mode: str = "dark",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._lang = lang if lang in ("es", "en", "pt") else "es"
        self._mode = mode if mode in ("dark", "light") else "dark"
        if crash_guard is not None:
            self._guard = crash_guard
            self._logs_dir = crash_guard.logs_dir
        else:
            self._logs_dir = Path(logs_dir) if logs_dir is not None else (default_app_data_dir() / "logs")
            self._guard = CrashGuard(logs_dir=self._logs_dir)

        self.setWindowTitle("Diagnostico y Registros · PDF/MD Lector")
        self.setMinimumSize(620, 460)
        self.resize(680, 520)

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(10)

        head = QHBoxLayout()
        title = QLabel("Centro de Diagnostico y CrashGuard", self)
        title.setFont(QFont(UI_FONT_FAMILY, 14, QFont.Weight.Bold))
        head.addWidget(title, 1)

        has_crash = (self._logs_dir / "crash.log").exists() and (self._logs_dir / "crash.log").stat().st_size > 0
        self.status_tag = StatusTag(
            "INCIDENCIA DETECTADA" if has_crash else "SESION LIMPIA",
            tone="warn" if has_crash else "ok",
            parent=self,
        )
        head.addWidget(self.status_tag)
        root.addLayout(head)

        self.path_lbl = QLabel(f"Logs: {self._logs_dir}", self)
        self.path_lbl.setProperty("mono", "true")
        self.path_lbl.setProperty("tone", "muted")
        self.path_lbl.setFont(QFont(MONO_FONT_FAMILY, 8))
        self.path_lbl.setWordWrap(True)
        root.addWidget(self.path_lbl)

        self.log_view = QPlainTextEdit(self)
        self.log_view.setReadOnly(True)
        self.log_view.setFont(QFont(MONO_FONT_FAMILY, 8))
        root.addWidget(self.log_view, 1)

        btns = FlowLayout(h_spacing=8, v_spacing=6)
        self.refresh_btn = AdaptiveButton("Actualizar logs", self, role="GhostAction")
        self.refresh_btn.clicked.connect(self.refresh_logs)
        btns.addWidget(self.refresh_btn)

        self.copy_btn = AdaptiveButton("Copiar diagnostico anonimizado", self, role="PrimaryAction")
        self.copy_btn.clicked.connect(self.copy_anonymized_diagnostics)
        btns.addWidget(self.copy_btn)

        self.support_btn = AdaptiveButton("Abrir Centro de Soporte", self, role="GhostAction")
        self.support_btn.clicked.connect(self._open_support)
        btns.addWidget(self.support_btn)

        self.close_btn = AdaptiveButton("Cerrar", self, role="GhostAction")
        self.close_btn.clicked.connect(self.accept)
        btns.addWidget(self.close_btn)
        root.addLayout(btns)

        self.apply_theme(self._mode)
        self.refresh_logs()

    def refresh_logs(self) -> None:
        self.log_view.setPlainText(self._guard.build_diagnostic_text())

    def copy_anonymized_diagnostics(self) -> str:
        cleaned = anonymize_text(self._guard.build_diagnostic_text())
        app = QApplication.instance()
        if isinstance(app, QApplication):
            app.clipboard().setText(cleaned)
        return cleaned

    def _open_support(self) -> None:
        from .support_dialog import SupportDialog

        dlg = SupportDialog(
            initial_logs=self._guard.read_crash_log_tail(4000),
            crash_guard=self._guard,
            lang=self._lang,
            mode=self._mode,
            parent=self,
        )
        dlg.exec()

    def apply_theme(self, mode: str) -> None:
        self._mode = "light" if mode == "light" else "dark"
        p = get_palette(self._mode)
        self.status_tag.apply_theme(self._mode)
        self.refresh_btn.setIcon(load_svg_icon("refresh", color=p["icon_color"], size=14))
        self.copy_btn.setIcon(load_svg_icon("copy", color=p["primary_fg"], size=14))
        self.support_btn.setIcon(load_svg_icon("bug", color=p["icon_color"], size=14))
