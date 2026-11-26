"""SupportDialog with strict PII anonymization, editable preview, and zero-GitHub submission."""

from __future__ import annotations

from urllib.parse import urlparse

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..core.crash_guard import CrashGuard
from ..core.feedback_reporter import (
    KIND_BUG,
    KIND_CRASH,
    KIND_ERROR,
    KIND_SUGGESTION,
    FeedbackReport,
    anonymize_text,
    build_clipboard_payload,
    build_feedback_report,
    build_github_issue_url,
    build_mailto_uri,
    save_local_report,
    submit_feedback_endpoint,
)
from ..theme.palette import MONO_FONT_FAMILY, UI_FONT_FAMILY, get_palette
from .svg_icons import load_svg_icon
from .widgets import AdaptiveButton, FlowLayout, StatusTag


def _is_loopback_http(url: str) -> bool:
    parsed = urlparse((url or "").strip())
    return parsed.scheme.lower() == "http" and (parsed.hostname or "").lower() in {"127.0.0.1", "localhost", "::1"}


class SupportDialog(QDialog):
    """Serverless & consent-gated feedback/support center with full PII redaction preview."""

    def __init__(
        self,
        *,
        endpoint_url: str = "",
        initial_logs: str = "",
        crash_guard: CrashGuard | None = None,
        allow_localhost: bool | None = None,
        lang: str = "es",
        mode: str = "dark",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._lang = lang if lang in ("es", "en", "pt") else "es"
        self._mode = mode if mode in ("dark", "light") else "dark"
        self._crash_guard = crash_guard
        self._raw_logs = initial_logs or ""
        self._injected_endpoint = (endpoint_url or "").strip()
        if allow_localhost is not None:
            self._allow_localhost = bool(allow_localhost)
        else:
            # Production defaults to False; explicit loopback endpoint_url injected by tests opts in safely.
            self._allow_localhost = bool(self._injected_endpoint and _is_loopback_http(self._injected_endpoint))

        self._last_report: FeedbackReport | None = None

        self.setWindowTitle("Centro de Soporte y Reportes · PDF/MD Lector")
        self.setMinimumSize(640, 520)
        self.resize(700, 560)

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(10)

        head = QHBoxLayout()
        title = QLabel("Reportar Error o Sugerencia (Sin cuenta GitHub)", self)
        title.setFont(QFont(UI_FONT_FAMILY, 14, QFont.Weight.Bold))
        head.addWidget(title, 1)
        self.status_tag = StatusTag("ANONIMIZADO", tone="ok", parent=self)
        head.addWidget(self.status_tag)
        root.addLayout(head)

        meta_card = QFrame(self)
        meta_card.setObjectName("CardPanel")
        form = QFormLayout(meta_card)
        form.setContentsMargins(12, 10, 12, 10)
        form.setSpacing(8)

        self.kind_combo = QComboBox(meta_card)
        self.kind_combo.addItem("Error / Bug", KIND_BUG)
        self.kind_combo.addItem("Sugerencia", KIND_SUGGESTION)
        self.kind_combo.addItem("Fallo / Crash", KIND_CRASH)
        self.kind_combo.addItem("Incidencia", KIND_ERROR)
        self.kind_combo.currentIndexChanged.connect(lambda _: self._rebuild_preview())
        form.addRow("Tipo:", self.kind_combo)

        self.title_edit = QLineEdit("Reporte tecnico PDF/MD Lector", meta_card)
        self.title_edit.textChanged.connect(lambda _: self._rebuild_preview())
        form.addRow("Titulo:", self.title_edit)

        self.desc_edit = QLineEdit("", meta_card)
        self.desc_edit.setPlaceholderText("Describe brevemente el problema o sugerencia...")
        self.desc_edit.textChanged.connect(lambda _: self._rebuild_preview())
        form.addRow("Descripcion:", self.desc_edit)

        self.endpoint_edit = QLineEdit(self._injected_endpoint, meta_card)
        self.endpoint_edit.setPlaceholderText("https://... (endpoint HTTPS opcional del trabajador Cloudflare)")
        form.addRow("Endpoint HTTPS:", self.endpoint_edit)

        self.include_diag_chk = QCheckBox("Incluir diagnostico anonimizado (sin rutas, usuario, IP ni documentos)", meta_card)
        self.include_diag_chk.setChecked(True)
        self.include_diag_chk.toggled.connect(lambda _: self._rebuild_preview())
        form.addRow("", self.include_diag_chk)
        root.addWidget(meta_card)

        prev_lbl = QLabel("Vista previa completa y editable (PII censurada):", self)
        prev_lbl.setFont(QFont(UI_FONT_FAMILY, 10, QFont.Weight.Bold))
        root.addWidget(prev_lbl)

        self.preview_edit = QPlainTextEdit(self)
        self.preview_edit.setFont(QFont(MONO_FONT_FAMILY, 8))
        root.addWidget(self.preview_edit, 1)

        self.feedback_lbl = QLabel("", self)
        self.feedback_lbl.setProperty("tone", "secondary")
        self.feedback_lbl.setWordWrap(True)
        root.addWidget(self.feedback_lbl)

        actions = FlowLayout(h_spacing=8, v_spacing=6)
        self.submit_btn = AdaptiveButton("Enviar reporte", self, role="PrimaryAction")
        self.submit_btn.clicked.connect(self.submit_report)
        actions.addWidget(self.submit_btn)

        self.save_btn = AdaptiveButton("Guardar respaldo (.md/.json)", self, role="GhostAction")
        self.save_btn.clicked.connect(self.save_backup)
        actions.addWidget(self.save_btn)

        self.copy_btn = AdaptiveButton("Copiar al portapapeles", self, role="GhostAction")
        self.copy_btn.clicked.connect(self.copy_to_clipboard)
        actions.addWidget(self.copy_btn)

        self.mailto_btn = AdaptiveButton("Enviar por correo", self, role="GhostAction")
        self.mailto_btn.clicked.connect(self.open_mailto)
        actions.addWidget(self.mailto_btn)

        self.gh_btn = AdaptiveButton("GitHub Issue (opcional)", self, role="GhostAction")
        self.gh_btn.clicked.connect(self.open_github_issue)
        actions.addWidget(self.gh_btn)

        self.cancel_btn = AdaptiveButton("Cancelar", self, role="GhostAction")
        self.cancel_btn.clicked.connect(self.reject)
        actions.addWidget(self.cancel_btn)
        root.addLayout(actions)

        self.apply_theme(self._mode)
        self._rebuild_preview()

    def _rebuild_preview(self) -> None:
        rep = build_feedback_report(
            kind=str(self.kind_combo.currentData() or KIND_BUG),
            title=self.title_edit.text(),
            description=self.desc_edit.text(),
            crash_guard=self._crash_guard,
            error_text=self._raw_logs,
            include_diagnostics=self.include_diag_chk.isChecked(),
        )
        self._last_report = rep
        self.preview_edit.setPlainText(rep.markdown_body)

    def set_description(self, text: str) -> None:
        self.desc_edit.setText(text)
        self._rebuild_preview()

    def preview_text(self) -> str:
        return anonymize_text(self.preview_edit.toPlainText())

    def copy_to_clipboard(self) -> str:
        payload = build_clipboard_payload(self.preview_text())
        app = QApplication.instance()
        if isinstance(app, QApplication):
            app.clipboard().setText(payload)
        self.feedback_lbl.setText("Reporte anonimizado copiado al portapapeles.")
        return payload

    def save_backup(self):
        rep = self._last_report or build_feedback_report(
            kind=str(self.kind_combo.currentData() or KIND_BUG),
            title=self.title_edit.text(),
            description=self.desc_edit.text(),
            error_text=self._raw_logs,
        )
        md_p, json_p = save_local_report(rep, edited_body=self.preview_text())
        self.feedback_lbl.setText(f"Respaldo guardado: {md_p.name}, {json_p.name}")
        return md_p, json_p

    def open_mailto(self) -> str:
        uri, _ = build_mailto_uri(self.title_edit.text(), self.preview_text())
        QDesktopServices.openUrl(QUrl(uri))
        return uri

    def open_github_issue(self) -> str:
        url, _ = build_github_issue_url(self.title_edit.text(), self.preview_text())
        QDesktopServices.openUrl(QUrl(url))
        return url

    def submit_report(self) -> bool:
        url = self.endpoint_edit.text().strip()
        rep = self._last_report or build_feedback_report(
            kind=str(self.kind_combo.currentData() or KIND_BUG),
            title=self.title_edit.text(),
            description=self.desc_edit.text(),
            error_text=self._raw_logs,
        )
        use_localhost = self._allow_localhost and (url == self._injected_endpoint)
        ok, msg = submit_feedback_endpoint(
            rep,
            url,
            edited_body=self.preview_text(),
            allow_localhost=use_localhost,
        )
        if ok:
            self.status_tag.set_tone("ok", "ENVIADO")
            self.feedback_lbl.setText("Reporte enviado correctamente sin exponer PII.")
        else:
            self.status_tag.set_tone("warn", "RESPALDO LOCAL")
            self.feedback_lbl.setText(f"No enviado ({msg}). Usa Guardar respaldo o Copiar al portapapeles.")
        return ok

    def apply_theme(self, mode: str) -> None:
        self._mode = "light" if mode == "light" else "dark"
        p = get_palette(self._mode)
        self.status_tag.apply_theme(self._mode)
        self.submit_btn.setIcon(load_svg_icon("bug", color=p["primary_fg"], size=14))
        self.copy_btn.setIcon(load_svg_icon("copy", color=p["icon_color"], size=14))
        self.save_btn.setIcon(load_svg_icon("folder", color=p["icon_color"], size=14))
