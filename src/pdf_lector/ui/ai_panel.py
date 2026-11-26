"""PySide6 Ask Document side panel (modern bottom-input chat UX, per-document history, export, DPAPI BYOK)."""

from __future__ import annotations

from collections.abc import Callable
from html import escape
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)
import shiboken6

from ..core.ai_chat_history import (
    AiChatHistoryStore,
    ChatMessage,
    format_chat_timestamp,
    local_chat_timestamp,
)
from ..core.ai_hardware import LocalAiHardwareReport, probe_local_ai_hardware
from ..core.ai_rag import (
    DocumentRagIndex,
    RagAnswer,
    ask_document,
    delete_byok_key,
    load_byok_key,
    save_byok_key,
)
from ..core.path_guard import validate_local_any_doc
from ..theme.palette import MONO_FONT_FAMILY, UI_FONT_FAMILY, get_palette
from .svg_icons import load_svg_icon, load_svg_pixmap
from .widgets import AdaptiveButton, FlowLayout, StatusTag

_AI_TEXTS: dict[str, dict[str, str]] = {
    "es": {
        "title": "Preguntar IA",
        "close": "Cerrar panel",
        "provider": "Motor",
        "consent": "Autorizar envio de fragmentos al proveedor externo (BYOK)",
        "key_placeholder": "Clave API BYOK (cifrada con Windows DPAPI)",
        "endpoint_placeholder": "Endpoint opcional (ej. http://127.0.0.1:11434/api/chat)",
        "save_key": "Guardar clave",
        "clear_key": "Borrar clave",
        "question_placeholder": "Escribe una pregunta sobre el documento...",
        "ask": "Enviar",
        "tag_local": "BM25 LOCAL",
        "tag_byok": "API BYOK",
        "tag_mcp": "MCP OFICIAL",
        "tag_hw_blocked": "HW NO APTO",
        "no_doc": "Abre primero un documento PDF o Markdown local valido.",
        "asking": "Analizando documento...",
        "key_saved": "Clave cifrada con DPAPI.",
        "key_cleared": "Clave eliminada.",
        "legal_notice": "Uso legal y codigo abierto: solo se envian fragmentos con tu permiso explicito, respetando terminos de cada API/MCP.",
        "hw_blocked_title": "LLM local no disponible en este equipo",
        "hw_blocked_body": "{reason}\n{recommendation}",
        "hw_use_bm25": "Usar BM25 Local",
        "hw_use_openai": "Conectar OpenAI",
        "hw_use_gemini": "Conectar Gemini",
        "hw_use_mcp": "Conectar MCP",
        "clear_chat": "Borrar historial del documento",
        "clear_confirm": "Borrar todo el historial de conversacion guardado para este documento?",
        "chat_cleared": "Historial del documento borrado.",
        "export_chat": "Exportar conversacion",
        "exported_ok": "Chat exportado en {name}",
        "empty_chat": "Haz una pregunta sobre este PDF o Markdown. El historial se guarda localmente por documento.",
        "user_label": "Tu",
        "ai_label": "Asistente IA",
        "citations_hdr": "Referencias en el documento",
        "no_active_doc": "Sin documento activo",
    },
    "en": {
        "title": "Ask AI",
        "close": "Close panel",
        "provider": "Engine",
        "consent": "Allow sending document excerpts to external BYOK provider",
        "key_placeholder": "BYOK API Key (encrypted with Windows DPAPI)",
        "endpoint_placeholder": "Optional endpoint URL",
        "save_key": "Save key",
        "clear_key": "Clear key",
        "question_placeholder": "Write a question about the document...",
        "ask": "Send",
        "tag_local": "LOCAL BM25",
        "tag_byok": "BYOK API",
        "tag_mcp": "OFFICIAL MCP",
        "tag_hw_blocked": "HW LIMITED",
        "no_doc": "Open a valid local PDF or Markdown document first.",
        "asking": "Analyzing document...",
        "key_saved": "Key encrypted via DPAPI.",
        "key_cleared": "Key removed.",
        "legal_notice": "Open-source & Terms compliant: excerpts are sent only with explicit consent under your provider API/MCP terms.",
        "hw_blocked_title": "Local LLM unavailable on this PC",
        "hw_blocked_body": "{reason}\n{recommendation}",
        "hw_use_bm25": "Use Local BM25",
        "hw_use_openai": "Connect OpenAI",
        "hw_use_gemini": "Connect Gemini",
        "hw_use_mcp": "Connect MCP",
        "clear_chat": "Clear document history",
        "clear_confirm": "Delete all saved chat history for this document?",
        "chat_cleared": "Document history cleared.",
        "export_chat": "Export conversation",
        "exported_ok": "Chat exported to {name}",
        "empty_chat": "Ask anything about this PDF or Markdown. History is saved locally per document.",
        "user_label": "You",
        "ai_label": "AI Assistant",
        "citations_hdr": "Document references",
        "no_active_doc": "No active document",
    },
    "pt": {
        "title": "Perguntar IA",
        "close": "Fechar painel",
        "provider": "Motor",
        "consent": "Autorizar envio de trechos ao provedor externo (BYOK)",
        "key_placeholder": "Chave API BYOK (criptografada com Windows DPAPI)",
        "endpoint_placeholder": "Endpoint opcional",
        "save_key": "Salvar chave",
        "clear_key": "Limpar chave",
        "question_placeholder": "Escreva uma pergunta sobre o documento...",
        "ask": "Enviar",
        "tag_local": "BM25 LOCAL",
        "tag_byok": "API BYOK",
        "tag_mcp": "MCP OFICIAL",
        "tag_hw_blocked": "HW LIMITADO",
        "no_doc": "Abra um documento PDF ou Markdown local valido primeiro.",
        "asking": "Analisando documento...",
        "key_saved": "Chave protegida com DPAPI.",
        "key_cleared": "Chave removida.",
        "legal_notice": "Codigo aberto e termos legais: trechos enviados apenas com consentimento explicito sob os termos oficiais de cada API/MCP.",
        "hw_blocked_title": "LLM local indisponivel neste PC",
        "hw_blocked_body": "{reason}\n{recommendation}",
        "hw_use_bm25": "Usar BM25 Local",
        "hw_use_openai": "Conectar OpenAI",
        "hw_use_gemini": "Conectar Gemini",
        "hw_use_mcp": "Conectar MCP",
        "clear_chat": "Limpar historico do documento",
        "clear_confirm": "Excluir todo o historico salvo para este documento?",
        "chat_cleared": "Historico do documento limpo.",
        "export_chat": "Exportar conversa",
        "exported_ok": "Chat exportado em {name}",
        "empty_chat": "Faca uma pergunta sobre este PDF ou Markdown. O historico fica salvo localmente por arquivo.",
        "user_label": "Voce",
        "ai_label": "Assistente IA",
        "citations_hdr": "Referencias no documento",
        "no_active_doc": "Nenhum documento ativo",
    },
}


def _tr(key: str, lang: str, **kwargs: object) -> str:
    lang_code = lang if lang in ("es", "en", "pt") else "es"
    tmpl = _AI_TEXTS[lang_code].get(key, _AI_TEXTS["es"].get(key, key))
    if kwargs:
        try:
            return tmpl.format(**kwargs)
        except Exception:
            return tmpl
    return tmpl


class _AskSignals(QObject):
    finished = Signal(object, object, str)


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
        self.signals.finished.emit(ans, self._doc_path, self._question)


class AskDocumentPanel(QWidget):
    """Collapsible AI Q&A side panel with modern bottom composer, timestamps, and per-document history."""

    close_requested = Signal()
    citation_clicked = Signal(int, str)

    def __init__(
        self,
        get_active_document_callback: Callable[[], tuple[Path | None, str]] | None = None,
        *,
        mode: str = "dark",
        lang: str = "es",
        history_store: AiChatHistoryStore | None = None,
        hardware_report: LocalAiHardwareReport | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("AskDocumentPanel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setMinimumWidth(310)
        self._get_active_doc = get_active_document_callback
        self._mode = "light" if mode == "light" else "dark"
        self._lang = lang if lang in ("es", "en", "pt") else "es"
        self._history_store = history_store or AiChatHistoryStore()
        self._hardware_report = hardware_report or probe_local_ai_hardware()
        self._active_doc_path: Path | None = None
        self._messages: list[ChatMessage] = []
        self._pool = QThreadPool.globalInstance()
        self._active_workers: list[_AskWorker] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        # Top Header Frame (crisp contrast in both Dark and Light modes)
        self._header_frame = QFrame(self)
        self._header_frame.setObjectName("AskHeader")
        hdr = QHBoxLayout(self._header_frame)
        hdr.setContentsMargins(8, 6, 8, 6)
        hdr.setSpacing(6)

        self._icon_lbl = QLabel(self._header_frame)
        self._icon_lbl.setFixedSize(16, 16)
        hdr.addWidget(self._icon_lbl)

        self.title_lbl = QLabel(_tr("title", self._lang), self._header_frame)
        self.title_lbl.setFont(QFont(UI_FONT_FAMILY, 9, QFont.Weight.Bold))
        hdr.addWidget(self.title_lbl, 1)

        self.mode_tag = StatusTag(_tr("tag_local", self._lang), tone="ok", parent=self._header_frame)
        hdr.addWidget(self.mode_tag)

        self.export_chat_btn = QPushButton("", self._header_frame)
        self.export_chat_btn.setObjectName("AskHeaderBtn")
        self.export_chat_btn.setFixedSize(26, 26)
        self.export_chat_btn.setToolTip(_tr("export_chat", self._lang))
        self.export_chat_btn.clicked.connect(lambda: self.export_current_chat())
        hdr.addWidget(self.export_chat_btn)

        self.clear_chat_btn = QPushButton("", self._header_frame)
        self.clear_chat_btn.setObjectName("AskHeaderBtn")
        self.clear_chat_btn.setFixedSize(26, 26)
        self.clear_chat_btn.setToolTip(_tr("clear_chat", self._lang))
        self.clear_chat_btn.clicked.connect(lambda: self.clear_current_chat())
        hdr.addWidget(self.clear_chat_btn)

        self.close_btn = QPushButton("", self._header_frame)
        self.close_btn.setObjectName("AskHeaderBtn")
        self.close_btn.setFixedSize(26, 26)
        self.close_btn.setToolTip(_tr("close", self._lang))
        self.close_btn.clicked.connect(self.close_requested.emit)
        hdr.addWidget(self.close_btn)
        root.addWidget(self._header_frame)

        # Provider + endpoint + DPAPI key controls
        prov_row = QHBoxLayout()
        prov_row.setSpacing(6)
        self.provider_lbl = QLabel(_tr("provider", self._lang), self)
        self.provider_lbl.setProperty("tone", "secondary")
        prov_row.addWidget(self.provider_lbl)

        self.provider_combo = QComboBox(self)
        self.provider_combo.addItem("Local BM25 (100% Offline, 0 GPU)", "local_bm25")
        self.provider_combo.addItem("OpenAI (API Oficial BYOK)", "openai")
        self.provider_combo.addItem("Gemini (API Oficial BYOK)", "gemini")
        self.provider_combo.addItem("Anthropic Claude (API Oficial BYOK)", "anthropic")
        self.provider_combo.addItem("Servidor MCP Oficial (JSON-RPC)", "mcp_official")
        self.provider_combo.addItem("OpenAI-Compatible (BYOK)", "openai_compatible")
        self.provider_combo.addItem("Ollama (LLM Local)", "ollama")
        self.provider_combo.currentIndexChanged.connect(self._on_provider_changed)
        prov_row.addWidget(self.provider_combo, 1)
        root.addLayout(prov_row)

        # Hardware capability gate card for local generative LLM (Ollama)
        self._hw_gate_box = QFrame(self)
        self._hw_gate_box.setObjectName("HwGateBox")
        hw_layout = QVBoxLayout(self._hw_gate_box)
        hw_layout.setContentsMargins(8, 8, 8, 8)
        hw_layout.setSpacing(6)

        self.hw_gate_title = QLabel(_tr("hw_blocked_title", self._lang), self._hw_gate_box)
        self.hw_gate_title.setFont(QFont(UI_FONT_FAMILY, 10, QFont.Weight.Bold))
        self.hw_gate_title.setWordWrap(True)
        hw_layout.addWidget(self.hw_gate_title)

        self.hw_gate_body = QLabel(self._hw_gate_box)
        self.hw_gate_body.setProperty("tone", "secondary")
        self.hw_gate_body.setWordWrap(True)
        hw_layout.addWidget(self.hw_gate_body)

        hw_btns = QGridLayout()
        hw_btns.setHorizontalSpacing(6)
        hw_btns.setVerticalSpacing(6)
        self.hw_bm25_btn = AdaptiveButton(_tr("hw_use_bm25", self._lang), self._hw_gate_box, role="PrimaryAction")
        self.hw_bm25_btn.setMinimumHeight(26)
        self.hw_bm25_btn.setStyleSheet("padding: 3px 6px; font-size: 11px;")
        self.hw_bm25_btn.clicked.connect(lambda: self._switch_provider("local_bm25"))
        hw_btns.addWidget(self.hw_bm25_btn, 0, 0)

        self.hw_openai_btn = AdaptiveButton(_tr("hw_use_openai", self._lang), self._hw_gate_box, role="GhostAction")
        self.hw_openai_btn.setMinimumHeight(26)
        self.hw_openai_btn.setStyleSheet("padding: 3px 6px; font-size: 11px;")
        self.hw_openai_btn.clicked.connect(lambda: self._switch_provider("openai"))
        hw_btns.addWidget(self.hw_openai_btn, 0, 1)

        self.hw_gemini_btn = AdaptiveButton(_tr("hw_use_gemini", self._lang), self._hw_gate_box, role="GhostAction")
        self.hw_gemini_btn.setMinimumHeight(26)
        self.hw_gemini_btn.setStyleSheet("padding: 3px 6px; font-size: 11px;")
        self.hw_gemini_btn.clicked.connect(lambda: self._switch_provider("gemini"))
        hw_btns.addWidget(self.hw_gemini_btn, 1, 0)

        self.hw_mcp_btn = AdaptiveButton(_tr("hw_use_mcp", self._lang), self._hw_gate_box, role="GhostAction")
        self.hw_mcp_btn.setMinimumHeight(26)
        self.hw_mcp_btn.setStyleSheet("padding: 3px 6px; font-size: 11px;")
        self.hw_mcp_btn.clicked.connect(lambda: self._switch_provider("mcp_official"))
        hw_btns.addWidget(self.hw_mcp_btn, 1, 1)
        hw_layout.addLayout(hw_btns)
        self._hw_gate_box.setVisible(False)
        root.addWidget(self._hw_gate_box)

        self._byok_box = QFrame(self)
        self._byok_box.setObjectName("ByokConfigBox")
        byok_layout = QVBoxLayout(self._byok_box)
        byok_layout.setContentsMargins(8, 8, 8, 8)
        byok_layout.setSpacing(6)

        self.endpoint_edit = QLineEdit(self._byok_box)
        self.endpoint_edit.setPlaceholderText(_tr("endpoint_placeholder", self._lang))
        byok_layout.addWidget(self.endpoint_edit)

        self.api_key_edit = QLineEdit(self._byok_box)
        self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_edit.setPlaceholderText(_tr("key_placeholder", self._lang))
        byok_layout.addWidget(self.api_key_edit)

        key_btns = FlowLayout(h_spacing=6, v_spacing=6)
        self.save_key_btn = AdaptiveButton(_tr("save_key", self._lang), self._byok_box, role="GhostAction")
        self.save_key_btn.clicked.connect(self._on_save_key)
        key_btns.addWidget(self.save_key_btn)

        self.clear_key_btn = AdaptiveButton(_tr("clear_key", self._lang), self._byok_box, role="GhostAction")
        self.clear_key_btn.clicked.connect(self._on_clear_key)
        key_btns.addWidget(self.clear_key_btn)
        byok_layout.addLayout(key_btns)

        self.consent_checkbox = QCheckBox(_tr("consent", self._lang), self._byok_box)
        self.consent_checkbox.setChecked(False)
        byok_layout.addWidget(self.consent_checkbox)

        self.legal_notice_lbl = QLabel(_tr("legal_notice", self._lang), self._byok_box)
        self.legal_notice_lbl.setProperty("tone", "muted")
        self.legal_notice_lbl.setWordWrap(True)
        self.legal_notice_lbl.setFont(QFont(UI_FONT_FAMILY, 8))
        byok_layout.addWidget(self.legal_notice_lbl)

        root.addWidget(self._byok_box)

        # Active Document Indicator
        self.doc_badge_lbl = QLabel(_tr("no_active_doc", self._lang), self)
        self.doc_badge_lbl.setFont(QFont(MONO_FONT_FAMILY, 8))
        self.doc_badge_lbl.setProperty("mono", "true")
        self.doc_badge_lbl.setProperty("tone", "muted")
        self.doc_badge_lbl.setWordWrap(True)
        root.addWidget(self.doc_badge_lbl)

        # Conversation Thread ABOVE (modern chat layout)
        self.answer_browser = QTextBrowser(self)
        self.answer_browser.setObjectName("AskChatBrowser")
        self.answer_browser.setOpenExternalLinks(False)
        self.answer_browser.anchorClicked.connect(self._on_anchor_clicked)
        root.addWidget(self.answer_browser, 1)

        # Subtle status line
        self.status_lbl = QLabel("", self)
        self.status_lbl.setProperty("tone", "secondary")
        self.status_lbl.setWordWrap(True)
        root.addWidget(self.status_lbl)

        # Composer Bar BELOW
        self._composer_frame = QFrame(self)
        self._composer_frame.setObjectName("AskComposerBox")
        q_row = QHBoxLayout(self._composer_frame)
        q_row.setContentsMargins(6, 6, 6, 6)
        q_row.setSpacing(6)

        self.question_edit = QLineEdit(self._composer_frame)
        self.question_edit.setPlaceholderText(_tr("question_placeholder", self._lang))
        self.question_edit.returnPressed.connect(self.ask_current_question)
        q_row.addWidget(self.question_edit, 1)

        self.ask_btn = AdaptiveButton("", self._composer_frame, role="PrimaryAction")
        self.ask_btn.setFixedWidth(36)
        self.ask_btn.setToolTip(_tr("ask", self._lang))
        self.ask_btn.clicked.connect(self.ask_current_question)
        q_row.addWidget(self.ask_btn)
        root.addWidget(self._composer_frame)

        self._on_provider_changed()
        self.refresh_active_document()
        self.apply_theme(self._mode)

    def current_provider(self) -> str:
        return str(self.provider_combo.currentData() or "local_bm25")

    def messages(self) -> list[ChatMessage]:
        return list(self._messages)

    def refresh_active_document(self) -> None:
        if self._get_active_doc is None:
            return
        doc_path, _ = self._get_active_doc()
        self.set_active_document(doc_path)

    def set_active_document(self, doc_path: Path | None) -> None:
        self._active_doc_path = Path(doc_path).resolve() if doc_path is not None else None
        if self._active_doc_path is not None:
            self.doc_badge_lbl.setText(self._active_doc_path.name)
            self.doc_badge_lbl.setToolTip(str(self._active_doc_path))
            self._messages = self._history_store.load_history(self._active_doc_path)
        else:
            self.doc_badge_lbl.setText(_tr("no_active_doc", self._lang))
            self.doc_badge_lbl.setToolTip("")
            self._messages = []
        self._render_conversation_html()

    def _switch_provider(self, provider_id: str) -> None:
        idx = self.provider_combo.findData(provider_id)
        if idx >= 0:
            self.provider_combo.setCurrentIndex(idx)

    def _is_ollama_blocked(self) -> bool:
        return self.current_provider() == "ollama" and not self._hardware_report.eligible_for_local_llm

    def _on_provider_changed(self) -> None:
        prov = self.current_provider()
        is_local_bm25 = prov == "local_bm25"
        ollama_blocked = self._is_ollama_blocked()
        self._hw_gate_box.setVisible(ollama_blocked)
        if ollama_blocked:
            self.hw_gate_title.setText(_tr("hw_blocked_title", self._lang))
            self.hw_gate_body.setText(
                _tr(
                    "hw_blocked_body",
                    self._lang,
                    reason=self._hardware_report.reason,
                    recommendation=self._hardware_report.recommendation,
                )
            )
            self._byok_box.setVisible(False)
            self.mode_tag.set_tone("risk", _tr("tag_hw_blocked", self._lang))
            self.ask_btn.setEnabled(False)
            self.status_lbl.setText(self._hardware_report.reason)
            return

        self.ask_btn.setEnabled(True)
        self._byok_box.setVisible(not is_local_bm25)
        if is_local_bm25:
            self.mode_tag.set_tone("ok", _tr("tag_local", self._lang))
        elif prov == "mcp_official":
            self.mode_tag.set_tone("warn", _tr("tag_mcp", self._lang))
        else:
            self.mode_tag.set_tone("warn", _tr("tag_byok", self._lang))

        if not is_local_bm25:
            existing = load_byok_key(prov)
            self.api_key_edit.setText(existing or "")
            if prov == "mcp_official" and not self.endpoint_edit.text().strip():
                self.endpoint_edit.setPlaceholderText("http://127.0.0.1:8787/mcp")
            else:
                self.endpoint_edit.setPlaceholderText(_tr("endpoint_placeholder", self._lang))
        if self.status_lbl.text() == self._hardware_report.reason:
            self.status_lbl.setText("")

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
        if self._is_ollama_blocked():
            self.status_lbl.setText(self._hardware_report.reason)
            return
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

        if doc_path is not None and (self._active_doc_path is None or doc_path.resolve() != self._active_doc_path):
            self.set_active_document(doc_path)

        user_msg = ChatMessage(
            role="user",
            text=question,
            timestamp=local_chat_timestamp(),
            provider=self.current_provider(),
        )
        self._messages.append(user_msg)
        if self._active_doc_path is not None:
            self._history_store.save_history(self._active_doc_path, self._messages)
        self._render_conversation_html()

        self.question_edit.clear()
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

    def _on_answer_ready(self, ans: Any, doc_path: Any = None, _question: str = "") -> None:
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

        cits_payload: list[dict[str, object]] = []
        for idx, cit in enumerate(ans.citations):
            if cit.page_number is not None:
                label = f"p. {cit.page_number}"
                href = f"trodai://page/{cit.page_number}"
            else:
                label = cit.section or Path(cit.source_path).name
                href = f"trodai://section/{idx}"
            snippet = " ".join(cit.text.split())[:180]
            cits_payload.append({"label": label, "href": href, "snippet": snippet})

        reply_msg = ChatMessage(
            role="assistant",
            text=ans.answer_text,
            timestamp=local_chat_timestamp(),
            provider=ans.provider,
            citations=cits_payload,
        )
        target_doc = Path(doc_path).resolve() if doc_path is not None else self._active_doc_path
        if target_doc is not None and self._active_doc_path is not None and target_doc == self._active_doc_path:
            self._messages.append(reply_msg)
            self._history_store.save_history(target_doc, self._messages)
            self._render_conversation_html()
        elif target_doc is not None:
            self._history_store.append_message(target_doc, reply_msg)
        else:
            self._messages.append(reply_msg)
            self._render_conversation_html()

    def _render_conversation_html(self) -> None:
        p = get_palette(self._mode)
        if not self._messages:
            self.answer_browser.setHtml(
                f"<div style='padding: 18px 8px; color: {p['text_muted']}; font-size: 12px; line-height: 1.45;'>"
                f"{escape(_tr('empty_chat', self._lang))}</div>"
            )
            return

        link_color = p["motion_cyan"]
        user_bg = p["bg_selected_hex"]
        ai_bg = p["bg_glass_elevated"]
        bd_color = p["border_strong"]
        fg = p["text_primary"]
        sec = p["text_secondary"]
        muted = p["text_muted"]

        blocks: list[str] = []
        for msg in self._messages:
            ts = escape(format_chat_timestamp(msg.timestamp))
            body_html = escape(msg.text).replace("\n", "<br/>")
            if msg.role == "user":
                sender = escape(_tr("user_label", self._lang))
                blocks.append(
                    f"<table width='100%' cellspacing='0' cellpadding='0' style='margin-bottom: 10px;'>"
                    f"<tr><td align='right'>"
                    f"<div style='background-color: {user_bg}; border: 1px solid {bd_color}; padding: 8px 10px; "
                    f"text-align: left; color: {fg}; font-size: 12px;'>"
                    f"<div style='font-size: 10px; color: {muted}; margin-bottom: 4px;'>"
                    f"<b style='color: {fg};'>{sender}</b> &middot; {ts}</div>"
                    f"<div style='color: {fg}; line-height: 1.4;'>{body_html}</div>"
                    f"</div></td></tr></table>"
                )
            else:
                prov_tag = f" ({escape(msg.provider)})" if msg.provider else ""
                sender = escape(_tr("ai_label", self._lang)) + prov_tag
                cit_html = ""
                if msg.citations:
                    items = []
                    for cit in msg.citations:
                        href = escape(str(cit.get("href") or "#"))
                        label = escape(str(cit.get("label") or "ref"))
                        snip = escape(str(cit.get("snippet") or ""))
                        items.append(
                            f"<li style='margin-bottom: 3px;'>"
                            f"<a href='{href}' style='color: {link_color}; font-weight: bold; text-decoration: none;'>[{label}]</a> "
                            f"<span style='color: {sec};'>{snip}</span></li>"
                        )
                    cit_html = (
                        f"<div style='margin-top: 8px; padding-top: 6px; border-top: 1px solid {bd_color}; "
                        f"font-size: 11px; color: {sec};'>"
                        f"<b>{escape(_tr('citations_hdr', self._lang))}:</b>"
                        f"<ul style='margin: 4px 0 0 16px; padding: 0;'>{''.join(items)}</ul></div>"
                    )
                blocks.append(
                    f"<table width='100%' cellspacing='0' cellpadding='0' style='margin-bottom: 10px;'>"
                    f"<tr><td align='left'>"
                    f"<div style='background-color: {ai_bg}; border: 1px solid {bd_color}; padding: 8px 10px; "
                    f"color: {fg}; font-size: 12px;'>"
                    f"<div style='font-size: 10px; color: {muted}; margin-bottom: 4px;'>"
                    f"<b style='color: {fg};'>{sender}</b> &middot; {ts}</div>"
                    f"<div style='color: {fg}; line-height: 1.45;'>{body_html}</div>"
                    f"{cit_html}"
                    f"</div></td></tr></table>"
                )

        self.answer_browser.setHtml("".join(blocks))
        sb = self.answer_browser.verticalScrollBar()
        if sb is not None:
            sb.setValue(sb.maximum())

    def clear_current_chat(self, *, confirm: bool | None = None) -> bool:
        need_confirm = (QApplication.platformName() != "offscreen") if confirm is None else confirm
        if need_confirm and self._messages:
            ans = QMessageBox.question(
                self,
                _tr("clear_chat", self._lang),
                _tr("clear_confirm", self._lang),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if ans != QMessageBox.StandardButton.Yes:
                return False
        self._messages.clear()
        if self._active_doc_path is not None:
            self._history_store.clear_history(self._active_doc_path)
        self._render_conversation_html()
        self.status_lbl.setText(_tr("chat_cleared", self._lang))
        return True

    def export_current_chat(self, output_path: Path | None = None) -> Path | None:
        doc = self._active_doc_path
        if doc is None:
            self.status_lbl.setText(_tr("no_doc", self._lang))
            return None
        if not self._messages:
            return None
        target = output_path
        if target is None:
            default_name = f"{doc.stem}_chat.md"
            chosen, _ = QFileDialog.getSaveFileName(
                self,
                _tr("export_chat", self._lang),
                str(doc.parent / default_name),
                "Markdown (*.md);;Texto plano (*.txt);;JSON (*.json)",
            )
            if not chosen:
                return None
            target = Path(chosen)
        out = self._history_store.export_chat(doc, target)
        self.status_lbl.setText(_tr("exported_ok", self._lang, name=out.name))
        return out

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
        self._mode = "light" if mode == "light" else "dark"
        p = get_palette(self._mode)
        self._icon_lbl.setPixmap(load_svg_pixmap("spark", color=p["icon_color"], size=16))
        self.export_chat_btn.setIcon(load_svg_icon("export", color=p["icon_color"], size=14))
        self.clear_chat_btn.setIcon(load_svg_icon("trash", color=p["icon_color"], size=14))
        self.close_btn.setIcon(load_svg_icon("close", color=p["icon_color"], size=14))
        self.ask_btn.setIcon(load_svg_icon("send", color=p["primary_fg"], size=14))
        self.mode_tag.apply_theme(self._mode)
        self.setStyleSheet(
            f"""
            QWidget#AskDocumentPanel {{
                background-color: {p["bg_sidebar"]};
                color: {p["text_primary"]};
                border-left: 1px solid {p["border_hairline"]};
            }}
            QWidget#AskDocumentPanel QLabel {{
                color: {p["text_primary"]};
                background: transparent;
            }}
            QWidget#AskDocumentPanel QLabel[tone="secondary"] {{
                color: {p["text_secondary"]};
            }}
            QWidget#AskDocumentPanel QLabel[tone="muted"] {{
                color: {p["text_muted"]};
            }}
            QFrame#AskHeader {{
                background-color: {p["bg_glass"]};
                border: 1px solid {p["border_strong"]};
            }}
            QPushButton#AskHeaderBtn {{
                background-color: transparent;
                color: {p["text_primary"]};
                border: 1px solid transparent;
                padding: 0px;
                text-align: center;
            }}
            QPushButton#AskHeaderBtn:hover {{
                background-color: {p["bg_hover"]};
                border: 1px solid transparent;
            }}
            QFrame#ByokConfigBox, QFrame#AskComposerBox, QFrame#HwGateBox {{
                background-color: {p["bg_glass"]};
                border: 1px solid {p["border_strong"]};
            }}
            QWidget#AskDocumentPanel QLineEdit,
            QWidget#AskDocumentPanel QComboBox {{
                background-color: {p["bg_glass_elevated"]};
                color: {p["text_primary"]};
                border: 1px solid {p["border_strong"]};
            }}
            QWidget#AskDocumentPanel QCheckBox {{
                color: {p["text_primary"]};
            }}
            QTextBrowser#AskChatBrowser {{
                background-color: {p["bg_glass"]};
                color: {p["text_primary"]};
                border: 1px solid {p["border_hairline"]};
                padding: 8px;
            }}
            """
        )
        self._render_conversation_html()

    def retranslate(self, lang: str) -> None:
        self._lang = lang if lang in ("es", "en", "pt") else "es"
        self.title_lbl.setText(_tr("title", self._lang))
        self.close_btn.setToolTip(_tr("close", self._lang))
        self.export_chat_btn.setToolTip(_tr("export_chat", self._lang))
        self.clear_chat_btn.setToolTip(_tr("clear_chat", self._lang))
        self.provider_lbl.setText(_tr("provider", self._lang))
        self.consent_checkbox.setText(_tr("consent", self._lang))
        self.api_key_edit.setPlaceholderText(_tr("key_placeholder", self._lang))
        self.endpoint_edit.setPlaceholderText(_tr("endpoint_placeholder", self._lang))
        self.save_key_btn.setText(_tr("save_key", self._lang))
        self.clear_key_btn.setText(_tr("clear_key", self._lang))
        self.legal_notice_lbl.setText(_tr("legal_notice", self._lang))
        self.hw_gate_title.setText(_tr("hw_blocked_title", self._lang))
        self.hw_bm25_btn.setText(_tr("hw_use_bm25", self._lang))
        self.hw_openai_btn.setText(_tr("hw_use_openai", self._lang))
        self.hw_gemini_btn.setText(_tr("hw_use_gemini", self._lang))
        self.hw_mcp_btn.setText(_tr("hw_use_mcp", self._lang))
        self.question_edit.setPlaceholderText(_tr("question_placeholder", self._lang))
        self.ask_btn.setToolTip(_tr("ask", self._lang))
        if self._active_doc_path is None:
            self.doc_badge_lbl.setText(_tr("no_active_doc", self._lang))
        self._on_provider_changed()
        self._render_conversation_html()
