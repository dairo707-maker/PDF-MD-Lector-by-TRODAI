"""PySide6 Ask Document side panel (local BM25 + consent-gated BYOK + DPAPI keys + MCP)."""

from __future__ import annotations

from collections.abc import Callable
from html import escape
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)
import shiboken6

from ..core.ai_rag import (
    DocumentRagIndex,
    RagAnswer,
    ask_document,
    delete_byok_key,
    load_byok_key,
    save_byok_key,
)
from ..core.path_guard import validate_local_any_doc
from ..theme.palette import get_palette
from .svg_icons import load_svg_icon, load_svg_pixmap
from .widgets import AdaptiveButton, FlowLayout, StatusTag

_AI_TEXTS: dict[str, dict[str, str]] = {
    "es": {
        "title": "Preguntar al documento",
        "close": "Cerrar",
        "provider": "Motor IA",
        "consent": "Autorizar envio de fragmentos al proveedor externo (BYOK)",
        "key_placeholder": "Clave API BYOK (cifrada con Windows DPAPI)",
        "endpoint_placeholder": "Endpoint opcional (ej. http://127.0.0.1:11434/api/chat)",
        "save_key": "Guardar clave",
        "clear_key": "Borrar clave",
        "question_placeholder": "Escribe una pregunta sobre el PDF o Markdown abierto...",
        "ask": "Preguntar",
        "tag_local": "BM25 LOCAL",
        "tag_byok": "BYOK",
        "no_doc": "Abre primero un documento PDF o Markdown local valido.",
        "asking": "Consultando...",
        "key_saved": "Clave cifrada con DPAPI.",
        "key_cleared": "Clave eliminada.",
    },
    "en": {
        "title": "Ask Document",
        "close": "Close",
        "provider": "AI Engine",
        "consent": "Allow sending document excerpts to external BYOK provider",
        "key_placeholder": "BYOK API Key (encrypted with Windows DPAPI)",
        "endpoint_placeholder": "Optional endpoint URL",
        "save_key": "Save key",
        "clear_key": "Clear key",
        "question_placeholder": "Ask a question about the open PDF or Markdown...",
        "ask": "Ask",
        "tag_local": "LOCAL BM25",
        "tag_byok": "BYOK",
        "no_doc": "Open a valid local PDF or Markdown document first.",
        "asking": "Querying...",
        "key_saved": "Key encrypted via DPAPI.",
        "key_cleared": "Key removed.",
    },
    "pt": {
        "title": "Perguntar ao documento",
        "close": "Fechar",
        "provider": "Motor IA",
        "consent": "Autorizar envio de trechos ao provedor externo (BYOK)",
        "key_placeholder": "Chave API BYOK (criptografada com Windows DPAPI)",
        "endpoint_placeholder": "Endpoint opcional",
        "save_key": "Salvar chave",
        "clear_key": "Limpar chave",
        "question_placeholder": "Pergunte sobre o PDF ou Markdown aberto...",
        "ask": "Perguntar",
        "tag_local": "BM25 LOCAL",
        "tag_byok": "BYOK",
        "no_doc": "Abra um documento PDF ou Markdown local valido primeiro.",
        "asking": "Consultando...",
        "key_saved": "Chave protegida com DPAPI.",
        "key_cleared": "Chave removida.",
    },
}


def _tr(key: str, lang: str) -> str:
    lang_code = lang if lang in ("es", "en", "pt") else "es"
    return _AI_TEXTS[lang_code].get(key, key)


class _AskSignals(QObject):
    finished = Signal(object)


class _AskWorker(QRunnable):
    def __init__(
        self,
        doc_path: Path | None,
        inline_text: str,
        question: str,
        provider: str,
        endpoint_url: str,
        consent_granted: bool,
    ) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self.signals = _AskSignals()
        self._doc_path = doc_path
        self._inline_text = inline_text
        self._question = question
        self._provider = provider
        self._endpoint_url = endpoint_url
        self._consent_granted = consent_granted

    def run(self) -> None:
        idx = DocumentRagIndex()
        try:
            if self._doc_path is not None:
                ok, _ = validate_local_any_doc(self._doc_path, check_exists=True)
                if ok:
                    if self._doc_path.suffix.lower() == ".pdf":
                        idx.index_pdf(self._doc_path)
                    else:
                        idx.index_markdown(self._doc_path)
            if idx.chunk_count == 0 and self._inline_text.strip():
                idx.index_text(self._inline_text, source_path="<active_editor>", doc_type="text")
            ans = ask_document(
                idx,
                self._question,
                provider=self._provider,
                endpoint_url=self._endpoint_url or None,
                consent_granted=self._consent_granted,
            )
        except Exception as exc:
            ans = RagAnswer(
                answer_text=f"Error: {exc}",
                provider=self._provider,
                error=str(exc),
                used_local_only=True,
            )
        self.signals.finished.emit(ans)


class AskDocumentPanel(QWidget):
    """Collapsible AI Q&A side panel powered by local BM25 and consent-gated BYOK."""

    close_requested = Signal()
    citation_clicked = Signal(int, str)

    def __init__(
        self,
        get_active_document_callback: Callable[[], tuple[Path | None, str]] | None = None,
        *,
        mode: str = "dark",
        lang: str = "es",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("AskDocumentPanel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setMinimumWidth(280)
        self._get_active_doc = get_active_document_callback
        self._mode = mode
        self._lang = lang
        self._pool = QThreadPool.globalInstance()
        self._active_workers: list[_AskWorker] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        # Header
        hdr = QHBoxLayout()
        hdr.setSpacing(6)
        self._icon_lbl = QLabel(self)
        hdr.addWidget(self._icon_lbl)

        self.title_lbl = QLabel(_tr("title", lang), self)
        self.title_lbl.setProperty("role", "SectionTitle")
        hdr.addWidget(self.title_lbl, 1)

        self.mode_tag = StatusTag(_tr("tag_local", lang), tone="ok", parent=self)
        hdr.addWidget(self.mode_tag)

        self.close_btn = AdaptiveButton("", self, role="GhostAction")
        self.close_btn.setToolTip(_tr("close", lang))
        self.close_btn.setFixedWidth(28)
        self.close_btn.clicked.connect(self.close_requested.emit)
        hdr.addWidget(self.close_btn)
        root.addLayout(hdr)

        # Provider + endpoint + DPAPI key controls
        prov_row = QHBoxLayout()
        prov_row.setSpacing(6)
        self.provider_lbl = QLabel(_tr("provider", lang), self)
        prov_row.addWidget(self.provider_lbl)

        self.provider_combo = QComboBox(self)
        self.provider_combo.addItem("Local BM25 (100% Offline)", "local_bm25")
        self.provider_combo.addItem("Ollama (Local Loopback)", "ollama")
        self.provider_combo.addItem("OpenAI (BYOK)", "openai")
        self.provider_combo.addItem("Gemini (BYOK)", "gemini")
        self.provider_combo.addItem("Anthropic Claude (BYOK)", "anthropic")
        self.provider_combo.addItem("OpenAI-Compatible (BYOK)", "openai_compatible")
        self.provider_combo.currentIndexChanged.connect(self._on_provider_changed)
        prov_row.addWidget(self.provider_combo, 1)
        root.addLayout(prov_row)

        self._byok_box = QFrame(self)
        self._byok_box.setObjectName("ByokConfigBox")
        byok_layout = QVBoxLayout(self._byok_box)
        byok_layout.setContentsMargins(8, 8, 8, 8)
        byok_layout.setSpacing(6)

        self.endpoint_edit = QLineEdit(self._byok_box)
        self.endpoint_edit.setPlaceholderText(_tr("endpoint_placeholder", lang))
        byok_layout.addWidget(self.endpoint_edit)

        self.api_key_edit = QLineEdit(self._byok_box)
        self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_edit.setPlaceholderText(_tr("key_placeholder", lang))
        byok_layout.addWidget(self.api_key_edit)

        key_btns = FlowLayout(h_spacing=6, v_spacing=6)
        self.save_key_btn = AdaptiveButton(_tr("save_key", lang), self._byok_box, role="GhostAction")
        self.save_key_btn.clicked.connect(self._on_save_key)
        key_btns.addWidget(self.save_key_btn)

        self.clear_key_btn = AdaptiveButton(_tr("clear_key", lang), self._byok_box, role="GhostAction")
        self.clear_key_btn.clicked.connect(self._on_clear_key)
        key_btns.addWidget(self.clear_key_btn)
        byok_layout.addLayout(key_btns)

        self.consent_checkbox = QCheckBox(_tr("consent", lang), self._byok_box)
        self.consent_checkbox.setChecked(False)
        byok_layout.addWidget(self.consent_checkbox)

        root.addWidget(self._byok_box)

        # Question bar
        q_row = QHBoxLayout()
        q_row.setSpacing(6)
        self.question_edit = QLineEdit(self)
        self.question_edit.setPlaceholderText(_tr("question_placeholder", lang))
        self.question_edit.returnPressed.connect(self.ask_current_question)
        q_row.addWidget(self.question_edit, 1)

        self.ask_btn = AdaptiveButton(_tr("ask", lang), self, role="PrimaryAction")
        self.ask_btn.clicked.connect(self.ask_current_question)
        q_row.addWidget(self.ask_btn)
        root.addLayout(q_row)

        # Status & Output
        self.status_lbl = QLabel("", self)
        self.status_lbl.setProperty("tone", "secondary")
        self.status_lbl.setWordWrap(True)
        root.addWidget(self.status_lbl)

        self.answer_browser = QTextBrowser(self)
        self.answer_browser.setOpenExternalLinks(False)
        self.answer_browser.anchorClicked.connect(self._on_anchor_clicked)
        root.addWidget(self.answer_browser, 1)

        self._on_provider_changed()
        self.apply_theme(mode)

    def current_provider(self) -> str:
        return str(self.provider_combo.currentData() or "local_bm25")

    def _on_provider_changed(self) -> None:
        prov = self.current_provider()
        is_local = prov == "local_bm25"
        self._byok_box.setVisible(not is_local)
        self.mode_tag.setText(_tr("tag_local", self._lang) if is_local else _tr("tag_byok", self._lang))
        if not is_local:
            existing = load_byok_key(prov)
            self.api_key_edit.setText(existing or "")

    def _on_save_key(self) -> None:
        prov = self.current_provider()
        val = self.api_key_edit.text().strip()
        if prov == "local_bm25" or not val:
            return
        save_byok_key(prov, val)
        self.status_lbl.setText(_tr("key_saved", self._lang))

    def _on_clear_key(self) -> None:
        prov = self.current_provider()
        if prov == "local_bm25":
            return
        delete_byok_key(prov)
        self.api_key_edit.clear()
        self.status_lbl.setText(_tr("key_cleared", self._lang))

    def ask_current_question(self) -> None:
        question = self.question_edit.text().strip()
        if not question:
            return

        doc_path: Path | None = None
        inline_text = ""
        if self._get_active_doc is not None:
            doc_path, inline_text = self._get_active_doc()

        if doc_path is None and not inline_text.strip():
            self.status_lbl.setText(_tr("no_doc", self._lang))
            return

        self.ask_btn.setEnabled(False)
        self.status_lbl.setText(_tr("asking", self._lang))

        worker = _AskWorker(
            doc_path=doc_path,
            inline_text=inline_text,
            question=question,
            provider=self.current_provider(),
            endpoint_url=self.endpoint_edit.text().strip(),
            consent_granted=self.consent_checkbox.isChecked(),
        )
        self._active_workers.append(worker)
        worker.signals.finished.connect(self._on_answer_ready, Qt.ConnectionType.QueuedConnection)
        self._pool.start(worker)

    def _on_answer_ready(self, ans: Any) -> None:
        if not shiboken6.isValid(self):
            return
        self.ask_btn.setEnabled(True)
        self._active_workers.clear()
        if not isinstance(ans, RagAnswer):
            return

        if ans.consent_blocked:
            self.status_lbl.setText("Consent required for external BYOK request.")
        elif ans.error:
            self.status_lbl.setText(ans.error)
        else:
            self.status_lbl.setText(f"OK ({ans.provider})")

        html_parts = [f"<p>{escape(ans.answer_text).replace(chr(10), '<br/>')}</p>"]
        if ans.citations:
            html_parts.append("<hr/><div><b>Citations:</b></div><ul>")
            for idx, cit in enumerate(ans.citations):
                if cit.page_number is not None:
                    label = f"p. {cit.page_number}"
                    href = f"trodai://page/{cit.page_number}"
                else:
                    label = cit.section or Path(cit.source_path).name
                    href = f"trodai://section/{idx}"
                snippet = escape(" ".join(cit.text.split())[:180])
                html_parts.append(f'<li><a href="{href}">[{escape(label)}]</a> {snippet}</li>')
            html_parts.append("</ul>")
        self.answer_browser.setHtml("".join(html_parts))

    def _on_anchor_clicked(self, url: Any) -> None:
        raw = url.toString() if hasattr(url, "toString") else str(url)
        if raw.startswith("trodai://page/"):
            try:
                page_num = int(raw.rsplit("/", 1)[-1])
                self.citation_clicked.emit(max(0, page_num - 1), "")
            except ValueError:
                pass
        elif raw.startswith("trodai://section/"):
            self.citation_clicked.emit(-1, raw)

    def apply_theme(self, mode: str) -> None:
        self._mode = mode
        p = get_palette(mode)
        self._icon_lbl.setPixmap(load_svg_pixmap("spark", color=p["icon_color"], size=16))
        self.close_btn.setIcon(load_svg_icon("close", color=p["icon_color"], size=14))
        self.mode_tag.apply_theme(mode)
        self.setStyleSheet(
            f"""
            QWidget#AskDocumentPanel {{
                background-color: {p["bg_sidebar"]};
                border-left: 1px solid {p["border_hairline"]};
            }}
            QFrame#ByokConfigBox {{
                background-color: {p["bg_glass"]};
                border: 1px solid {p["border_hairline"]};
            }}
            QTextBrowser {{
                background-color: {p["bg_glass"]};
                color: {p["text_primary"]};
                border: 1px solid {p["border_hairline"]};
                padding: 8px;
            }}
            """
        )

    def retranslate(self, lang: str) -> None:
        self._lang = lang
        self.title_lbl.setText(_tr("title", lang))
        self.close_btn.setToolTip(_tr("close", lang))
        self.provider_lbl.setText(_tr("provider", lang))
        self.consent_checkbox.setText(_tr("consent", lang))
        self.api_key_edit.setPlaceholderText(_tr("key_placeholder", lang))
        self.endpoint_edit.setPlaceholderText(_tr("endpoint_placeholder", lang))
        self.save_key_btn.setText(_tr("save_key", lang))
        self.clear_key_btn.setText(_tr("clear_key", lang))
        self.question_edit.setPlaceholderText(_tr("question_placeholder", lang))
        self.ask_btn.setText(_tr("ask", lang))
        self._on_provider_changed()
