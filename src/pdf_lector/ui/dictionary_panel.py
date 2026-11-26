"""Collapsible Offline Dictionary side panel and On-Demand Language Manager dialog."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QTimer, Qt, Signal
import shiboken6
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..core.dictionary_engine import AVAILABLE_LANGUAGE_PACKS, DictionaryEngine
from ..core.i18n import TRANSLATIONS, t
from ..theme.palette import MONO_FONT_FAMILY, UI_FONT_FAMILY, get_palette
from .svg_icons import load_svg_icon, load_svg_pixmap
from .widgets import AdaptiveButton, FlowLayout, StatusTag

_LOCAL_DICT_TEXTS: dict[str, dict[str, str]] = {
    "es": {
        "dict.detach": "Desacoplar",
        "dict.dock": "Acoplar",
        "dict.close": "Cerrar",
        "dict.copy": "Copiar definicion",
        "dict.copied": "Definicion copiada al portapapeles.",
        "dict.suggestions": "Sugerencias aproximadas:",
        "dict.online_btn": "Buscar en Wiktionary (requiere permiso)",
        "dict.online_confirm": "Consultar en {lang}.wiktionary.org la palabra '{word}' y guardarla localmente?",
        "dict.online_cached": "Definicion obtenida y guardada en cache offline.",
        "dict.online_offline": "Modo Sin Conexion activo: desactivalo en Preferencias para buscar online.",
        "dict.online_err": "No se encontro '{word}' en Wiktionary o sin conexion.",
        "dict.consent_title": "Descargar paquete de idioma",
        "dict.consent_body": "Se conectara a {url} para descargar el diccionario '{lang}' bajo tu solicitud explicita. Continuar?",
    },
    "en": {
        "dict.detach": "Detach",
        "dict.dock": "Dock",
        "dict.close": "Close",
        "dict.copy": "Copy definition",
        "dict.copied": "Definition copied to clipboard.",
        "dict.suggestions": "Approximate suggestions:",
        "dict.online_btn": "Search Wiktionary (consent required)",
        "dict.online_confirm": "Query {lang}.wiktionary.org for '{word}' and cache locally offline?",
        "dict.online_cached": "Definition fetched and cached locally offline.",
        "dict.online_offline": "Offline-Only mode enabled: disable it in Preferences to query online.",
        "dict.online_err": "'{word}' was not found on Wiktionary or network is unreachable.",
        "dict.consent_title": "Download language pack",
        "dict.consent_body": "Connect to {url} to download the '{lang}' dictionary at your explicit request. Continue?",
    },
    "pt": {
        "dict.detach": "Desacoplar",
        "dict.dock": "Acoplar",
        "dict.close": "Fechar",
        "dict.copy": "Copiar definicao",
        "dict.copied": "Definicao copiada para a area de transferencia.",
        "dict.suggestions": "Sugestoes aproximadas:",
        "dict.online_btn": "Buscar no Wiktionary (requer permissao)",
        "dict.online_confirm": "Consultar {lang}.wiktionary.org para '{word}' e salvar localmente?",
        "dict.online_cached": "Definicao obtida e salva no cache offline.",
        "dict.online_offline": "Modo Offline ativo: desative nas Preferencias para consultar online.",
        "dict.online_err": "Nao encontrado em '{word}' no Wiktionary ou sem conexao.",
        "dict.consent_title": "Baixar pacote de idioma",
        "dict.consent_body": "Conectar a {url} para baixar o dicionario '{lang}' sob sua solicitacao explicita. Continuar?",
    },
}


def _td(key: str, lang: str = "es", **kwargs: object) -> str:
    lang_code = lang if lang in ("es", "en", "pt") else "es"
    if key in TRANSLATIONS.get(lang_code, {}):
        return t(key, lang_code, **kwargs)
    tmpl = _LOCAL_DICT_TEXTS.get(lang_code, _LOCAL_DICT_TEXTS["es"]).get(
        key, _LOCAL_DICT_TEXTS["es"].get(key, key)
    )
    try:
        return tmpl.format(**kwargs) if kwargs else tmpl
    except Exception:
        return tmpl


class DictionaryManagerDialog(QDialog):
    """Manage bundled offline dictionaries, user-triggered language downloads, and JSON glossaries."""

    packs_changed = Signal()

    def __init__(self, engine: DictionaryEngine, *, mode: str = "dark", lang: str = "es", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._engine = engine
        self._mode = mode
        self._lang = lang
        self.setWindowTitle(t("dict.manager_title", lang))
        self.setMinimumSize(500, 400)
        self.resize(560, 440)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        note = QLabel(t("dict.manager_note", lang), self)
        note.setProperty("tone", "secondary")
        note.setWordWrap(True)
        root.addWidget(note)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        host = QWidget(scroll)
        host.setObjectName("TransparentHost")
        self._rows_layout = QVBoxLayout(host)
        self._rows_layout.setContentsMargins(0, 0, 4, 0)
        self._rows_layout.setSpacing(8)
        scroll.setWidget(host)
        root.addWidget(scroll, 1)

        footer = FlowLayout(h_spacing=8, v_spacing=8)
        self.import_btn = AdaptiveButton(t("dict.import_json", lang), self, role="PrimaryAction")
        self.import_btn.clicked.connect(self._import_json)
        footer.addWidget(self.import_btn)

        self.close_btn = AdaptiveButton("OK", self, role="GhostAction")
        self.close_btn.clicked.connect(self.accept)
        footer.addWidget(self.close_btn)
        root.addLayout(footer)

        self._rebuild_rows()

    def _rebuild_rows(self) -> None:
        while self._rows_layout.count():
            item = self._rows_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        p = get_palette(self._mode)
        installed = set(self._engine.installed_languages())
        for pack in AVAILABLE_LANGUAGE_PACKS:
            row_frame = QFrame(self)
            row_frame.setObjectName("CardPanel")
            rl = QHBoxLayout(row_frame)
            rl.setContentsMargins(12, 8, 12, 8)
            rl.setSpacing(10)

            info_col = QVBoxLayout()
            info_col.setSpacing(2)
            name_lbl = QLabel(f"{pack.name} ({pack.code.upper()})", row_frame)
            nf = QFont(UI_FONT_FAMILY, 10)
            nf.setWeight(QFont.Weight.Bold)
            name_lbl.setFont(nf)
            info_col.addWidget(name_lbl)

            src_lbl = QLabel(pack.source_label, row_frame)
            src_lbl.setProperty("tone", "muted")
            src_lbl.setWordWrap(True)
            info_col.addWidget(src_lbl)
            rl.addLayout(info_col, 1)

            if pack.code in installed:
                cnt = self._engine.entry_count(pack.code)
                tag = StatusTag(t("dict.installed", self._lang, count=cnt), tone="ok", parent=row_frame)
                tag.apply_theme(self._mode)
                rl.addWidget(tag)
                if not pack.bundled:
                    rm_btn = AdaptiveButton(t("dict.remove", self._lang), row_frame, role="RiskAction")
                    rm_btn.clicked.connect(lambda _=False, c=pack.code: self._remove_pack(c))
                    rl.addWidget(rm_btn)
            else:
                tag = StatusTag(t("dict.available", self._lang), tone="muted", parent=row_frame)
                tag.apply_theme(self._mode)
                rl.addWidget(tag)
                dl_btn = AdaptiveButton(t("dict.download", self._lang), row_frame, role="GhostAction")
                dl_btn.setIcon(load_svg_icon("download", color=p["icon_color"], size=14))
                dl_btn.clicked.connect(lambda _=False, c=pack.code: self._download_pack(c))
                rl.addWidget(dl_btn)

            self._rows_layout.addWidget(row_frame)
        self._rows_layout.addStretch(1)

    def _download_pack(self, code: str) -> None:
        pack = next((p for p in AVAILABLE_LANGUAGE_PACKS if p.code == code), None)
        if pack is not None and not pack.bundled:
            reply = QMessageBox.question(
                self,
                _td("dict.consent_title", self._lang),
                _td("dict.consent_body", self._lang, lang=pack.name, url=pack.url),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
        try:
            count = self._engine.download_language_pack(code, consent_granted=True)
            QTimer.singleShot(0, self._rebuild_rows)
            self.packs_changed.emit()
            QMessageBox.information(
                self,
                t("dict.manager_title", self._lang),
                t("dict.download_ok", self._lang, lang=code.upper(), count=count),
            )
        except Exception as exc:
            QMessageBox.warning(
                self,
                t("dict.manager_title", self._lang),
                t("dict.download_err", self._lang, lang=code.upper(), err=str(exc)),
            )

    def _remove_pack(self, code: str) -> None:
        if self._engine.remove_language_pack(code):
            QTimer.singleShot(0, self._rebuild_rows)
            self.packs_changed.emit()

    def _import_json(self) -> None:
        chosen, _ = QFileDialog.getOpenFileName(
            self,
            t("dict.import_json", self._lang),
            "",
            "JSON (*.json)",
        )
        if not chosen:
            return
        try:
            code = Path(chosen).stem.lower()[:2]
            if not code.isalpha():
                code = self._lang
            count = self._engine.import_local_glossary(code, Path(chosen))
            self._rebuild_rows()
            self.packs_changed.emit()
            QMessageBox.information(
                self,
                t("dict.manager_title", self._lang),
                t("dict.download_ok", self._lang, lang=code.upper(), count=count),
            )
        except Exception as exc:
            QMessageBox.warning(self, t("dict.manager_title", self._lang), str(exc))


class DictionaryPanel(QFrame):
    """Collapsible, closable, detachable Kindle-style right side panel for offline dictionary lookup."""

    word_looked_up = Signal(str)
    close_requested = Signal()
    detach_toggled = Signal(bool)

    def __init__(
        self,
        engine: DictionaryEngine,
        *,
        offline_only_getter: Callable[[], bool] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._engine = engine
        self._offline_only_getter = offline_only_getter
        self._ui_lang = "es"
        self._mode = "dark"
        self._is_detached = False
        self._page_words: list[str] = []
        self._last_missing_word: str = ""
        self._online_consent_given: bool = False

        self.setObjectName("DictPanel")
        self.setFixedWidth(266)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        # Header Row
        head = QHBoxLayout()
        head.setSpacing(6)
        self.icon_lbl = QLabel(self)
        self.icon_lbl.setFixedSize(18, 18)
        head.addWidget(self.icon_lbl)

        self.title_lbl = QLabel("", self)
        tf = QFont(UI_FONT_FAMILY, 11)
        tf.setWeight(QFont.Weight.Bold)
        self.title_lbl.setFont(tf)
        head.addWidget(self.title_lbl, 1)
        root.addLayout(head)

        # Action Strip (FlowLayout prevents clipping at 266px width across ES/EN/PT)
        actions_flow = FlowLayout(h_spacing=6, v_spacing=4)
        self.manage_btn = AdaptiveButton("", self, role="GhostAction")
        self.manage_btn.setMinimumHeight(26)
        self.manage_btn.setStyleSheet("padding: 3px 8px; font-size: 11px;")
        self.manage_btn.clicked.connect(self._open_manager)
        actions_flow.addWidget(self.manage_btn)

        self.detach_btn = AdaptiveButton("", self, role="GhostAction")
        self.detach_btn.setMinimumHeight(26)
        self.detach_btn.setStyleSheet("padding: 3px 8px; font-size: 11px;")
        self.detach_btn.clicked.connect(lambda: self.detach_toggled.emit(not self._is_detached))
        actions_flow.addWidget(self.detach_btn)

        self.close_btn = AdaptiveButton("", self, role="GhostAction")
        self.close_btn.setMinimumHeight(26)
        self.close_btn.setStyleSheet("padding: 3px 8px; font-size: 11px;")
        self.close_btn.clicked.connect(self.close_requested.emit)
        actions_flow.addWidget(self.close_btn)
        root.addLayout(actions_flow)

        # Language & Search Row
        lang_row = QHBoxLayout()
        lang_row.setSpacing(6)
        self.dict_lang_combo = QComboBox(self)
        self.dict_lang_combo.setFixedWidth(74)
        self.dict_lang_combo.currentIndexChanged.connect(lambda _: self.lookup_word(self.search_input.text()))
        lang_row.addWidget(self.dict_lang_combo)

        self.search_input = QLineEdit(self)
        self.search_input.setClearButtonEnabled(True)
        self.search_input.returnPressed.connect(lambda: self.lookup_word(self.search_input.text()))
        self.search_input.textChanged.connect(self._on_text_changed)
        lang_row.addWidget(self.search_input, 1)
        root.addLayout(lang_row)

        # Result Card (100% solid opaque)
        self.result_card = QFrame(self)
        self.result_card.setObjectName("CardPanel")
        rc_layout = QVBoxLayout(self.result_card)
        rc_layout.setContentsMargins(10, 10, 10, 10)
        rc_layout.setSpacing(6)

        res_head = QHBoxLayout()
        res_head.setSpacing(4)
        self.res_word_lbl = QLabel("—", self.result_card)
        wf = QFont(UI_FONT_FAMILY, 12)
        wf.setWeight(QFont.Weight.Bold)
        self.res_word_lbl.setFont(wf)
        self.res_word_lbl.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        self.res_word_lbl.setCursor(Qt.CursorShape.IBeamCursor)
        res_head.addWidget(self.res_word_lbl, 1)

        self.res_pos_tag = StatusTag("LOCAL", tone="muted", parent=self.result_card)
        res_head.addWidget(self.res_pos_tag)

        self.copy_btn = QPushButton("", self.result_card)
        self.copy_btn.setProperty("role", "GhostAction")
        self.copy_btn.setFixedSize(26, 24)
        self.copy_btn.setStyleSheet("padding: 2px;")
        self.copy_btn.setToolTip(_td("dict.copy", self._ui_lang))
        self.copy_btn.clicked.connect(self.copy_current_result)
        res_head.addWidget(self.copy_btn)
        rc_layout.addLayout(res_head)

        self.res_def_lbl = QLabel("", self.result_card)
        self.res_def_lbl.setProperty("tone", "secondary")
        self.res_def_lbl.setWordWrap(True)
        self.res_def_lbl.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        self.res_def_lbl.setCursor(Qt.CursorShape.IBeamCursor)
        rc_layout.addWidget(self.res_def_lbl)

        self.res_syn_lbl = QLabel("", self.result_card)
        self.res_syn_lbl.setProperty("mono", "true")
        self.res_syn_lbl.setProperty("tone", "muted")
        self.res_syn_lbl.setWordWrap(True)
        self.res_syn_lbl.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        self.res_syn_lbl.setCursor(Qt.CursorShape.IBeamCursor)
        rc_layout.addWidget(self.res_syn_lbl)

        self.copy_status_lbl = QLabel("", self.result_card)
        self.copy_status_lbl.setProperty("mono", "true")
        self.copy_status_lbl.setProperty("tone", "muted")
        self.copy_status_lbl.setVisible(False)
        rc_layout.addWidget(self.copy_status_lbl)

        self._suggest_host = QWidget(self.result_card)
        self._suggest_host.setObjectName("TransparentHost")
        self._suggest_flow = FlowLayout(self._suggest_host, margin=0, h_spacing=4, v_spacing=4)
        self._suggest_host.setVisible(False)
        rc_layout.addWidget(self._suggest_host)

        self.online_lookup_btn = AdaptiveButton(_td("dict.online_btn", self._ui_lang), self.result_card, role="GhostAction")
        self.online_lookup_btn.setMinimumHeight(26)
        self.online_lookup_btn.setStyleSheet("padding: 3px 8px; font-size: 11px;")
        self.online_lookup_btn.setVisible(False)
        self.online_lookup_btn.clicked.connect(self.fetch_missing_online)
        rc_layout.addWidget(self.online_lookup_btn)
        root.addWidget(self.result_card)

        # Clickable Page Word Chips
        self.words_hdr = QLabel("", self)
        self.words_hdr.setProperty("tone", "muted")
        root.addWidget(self.words_hdr)

        chips_scroll = QScrollArea(self)
        chips_scroll.setWidgetResizable(True)
        chips_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._chips_host = QWidget(chips_scroll)
        self._chips_host.setObjectName("TransparentHost")
        self._chips_flow = FlowLayout(self._chips_host, margin=0, h_spacing=5, v_spacing=5)
        chips_scroll.setWidget(self._chips_host)
        root.addWidget(chips_scroll, 1)

        self._refresh_languages()
        self.retranslate("es")

    def _refresh_languages(self) -> None:
        current = str(self.dict_lang_combo.currentData() or self._ui_lang)
        self.dict_lang_combo.blockSignals(True)
        self.dict_lang_combo.clear()
        for code in self._engine.installed_languages():
            self.dict_lang_combo.addItem(code.upper(), code)
        idx = self.dict_lang_combo.findData(current)
        if idx < 0:
            idx = max(0, self.dict_lang_combo.findData("es"))
        self.dict_lang_combo.setCurrentIndex(max(0, idx))
        self.dict_lang_combo.blockSignals(False)

    def current_dict_lang(self) -> str:
        return str(self.dict_lang_combo.currentData() or "es")

    def set_page_words(self, words: list[str]) -> None:
        self._page_words = list(words)
        while self._chips_flow.count():
            item = self._chips_flow.takeAt(0)
            w = item.widget()
            if w is not None:
                w.hide()
                w.setParent(None)
                w.deleteLater()

        for word in self._page_words:
            btn = QPushButton(word, self._chips_host)
            btn.setProperty("role", "GhostAction")
            btn.setStyleSheet("padding: 3px 7px; font-size: 11px;")
            btn.clicked.connect(lambda _=False, wd=word: self._select_chip(wd))
            self._chips_flow.addWidget(btn)

        # Auto-preview the first word found in the current dictionary if search box is empty
        if not self.search_input.text().strip():
            lang = self.current_dict_lang()
            for candidate in self._page_words:
                if self._engine.lookup(candidate, lang):
                    self.lookup_word(candidate, record_event=False)
                    return
            self.lookup_word("documento" if lang == "es" else "document", record_event=False)

    def _select_chip(self, word: str) -> None:
        self.search_input.setText(word)
        self.lookup_word(word, record_event=True)

    def _on_text_changed(self, text: str) -> None:
        if len(text.strip()) >= 2:
            self.lookup_word(text, record_event=False)

    def copy_current_result(self) -> str:
        word = self.res_word_lbl.text().strip()
        pos = self.res_pos_tag.text().strip()
        defn = self.res_def_lbl.text().strip()
        syns = self.res_syn_lbl.text().strip()
        parts = [f"{word} ({pos})" if pos else word]
        if defn:
            parts.append(defn)
        if syns:
            parts.append(syns)
        payload = "\n".join(p for p in parts if p)
        cb = QApplication.clipboard()
        if cb is not None and payload:
            cb.setText(payload)
            self.copy_status_lbl.setText(_td("dict.copied", self._ui_lang))
            self.copy_status_lbl.setVisible(True)
            QTimer.singleShot(1800, lambda: self.copy_status_lbl.setVisible(False) if self.copy_status_lbl else None)
        return payload

    def _clear_suggestions(self) -> None:
        while self._suggest_flow.count():
            item = self._suggest_flow.takeAt(0)
            w = item.widget()
            if w is not None:
                w.hide()
                w.setParent(None)
                w.deleteLater()
        self._suggest_host.setVisible(False)

    def _populate_suggestions(self, words: list[str]) -> None:
        self._clear_suggestions()
        if not words:
            return
        for word in words:
            chip = QPushButton(word, self._suggest_host)
            chip.setProperty("role", "GhostAction")
            chip.setStyleSheet("padding: 2px 7px; font-size: 11px;")
            chip.clicked.connect(lambda _=False, wd=word: self._select_chip(wd))
            self._suggest_flow.addWidget(chip)
        self._suggest_host.setVisible(True)

    def fetch_missing_online(self, *, consent_granted: bool | None = None, fetcher=None) -> list:
        clean = (self._last_missing_word or self.search_input.text()).strip()
        if not clean:
            return []
        lang = self.current_dict_lang()
        offline_only = bool(self._offline_only_getter()) if self._offline_only_getter is not None else False
        if offline_only:
            self.res_syn_lbl.setText(_td("dict.online_offline", self._ui_lang))
            return []

        granted = consent_granted if consent_granted is not None else self._online_consent_given
        if not granted and QApplication.platformName() != "offscreen":
            ans = QMessageBox.question(
                self,
                _td("dict.online_btn", self._ui_lang),
                _td("dict.online_confirm", self._ui_lang, lang=lang, word=clean),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            granted = ans == QMessageBox.StandardButton.Yes
        elif not granted:
            granted = True

        if not granted:
            return []
        self._online_consent_given = True
        try:
            entries = self._engine.lookup_online_wiktionary(
                clean,
                lang,
                consent_granted=True,
                offline_only=offline_only,
                fetcher=fetcher,
            )
            if entries:
                self.lookup_word(clean, record_event=True)
                if shiboken6.isValid(self.copy_status_lbl):
                    self.copy_status_lbl.setText(_td("dict.online_cached", self._ui_lang))
                    self.copy_status_lbl.setVisible(True)
                return entries
        except Exception:
            pass
        self.res_syn_lbl.setText(_td("dict.online_err", self._ui_lang, word=clean))
        return []

    def lookup_word(self, word: str, *, record_event: bool = True) -> None:
        clean = (word or "").strip(" \t\r\n.,;:!?¡¿()[]{}\"'«»“”—–-•*/")
        lang = self.current_dict_lang()
        if not clean:
            return
        self.copy_status_lbl.setVisible(False)
        hits = self._engine.lookup(clean, lang)
        if hits:
            hit = hits[0]
            self._last_missing_word = ""
            self._clear_suggestions()
            self.online_lookup_btn.setVisible(False)
            self.res_word_lbl.setText(hit.word)
            self.res_pos_tag.set_tone("ok", hit.pos.upper())
            self.res_def_lbl.setText(hit.definition)
            self.res_syn_lbl.setText(
                t("dict.synonyms", self._ui_lang, syns=", ".join(hit.synonyms)) if hit.synonyms else ""
            )
            if record_event:
                self.word_looked_up.emit(lang)
        else:
            self._last_missing_word = clean
            self.res_word_lbl.setText(clean)
            self.res_pos_tag.set_tone("muted", lang.upper())
            self.res_def_lbl.setText(t("dict.not_found", self._ui_lang, word=clean, lang=lang.upper()))
            suggestions = self._engine.suggest_fuzzy(clean, lang, limit=6)
            self.res_syn_lbl.setText(_td("dict.suggestions", self._ui_lang) if suggestions else "")
            self._populate_suggestions(suggestions)
            self.online_lookup_btn.setVisible(True)

    def _open_manager(self) -> None:
        dlg = DictionaryManagerDialog(self._engine, mode=self._mode, lang=self._ui_lang, parent=self)
        dlg.packs_changed.connect(self._refresh_languages)
        dlg.exec()

    def set_detached_state(self, detached: bool) -> None:
        self._is_detached = bool(detached)
        if self._is_detached:
            self.setMinimumWidth(240)
            self.setMaximumWidth(16777215)
        else:
            self.setFixedWidth(266)
        self.detach_btn.setText(_td("dict.dock" if self._is_detached else "dict.detach", self._ui_lang))

    def set_dict_lang(self, lang: str) -> bool:
        code = (lang or "").strip().lower()
        if not code:
            return False
        self._refresh_languages()
        idx = self.dict_lang_combo.findData(code)
        if idx < 0:
            return False
        self.dict_lang_combo.blockSignals(True)
        self.dict_lang_combo.setCurrentIndex(idx)
        self.dict_lang_combo.blockSignals(False)
        return True

    def retranslate(self, lang: str) -> None:
        self._ui_lang = lang
        self.title_lbl.setText(t("dict.title", lang))
        self.search_input.setPlaceholderText(t("dict.search_placeholder", lang))
        self.words_hdr.setText(t("dict.page_words", lang))
        self.manage_btn.setText(t("dict.manage_btn", lang))
        self.detach_btn.setText(_td("dict.dock" if self._is_detached else "dict.detach", lang))
        self.close_btn.setText(_td("dict.close", lang))
        self.copy_btn.setToolTip(_td("dict.copy", lang))
        self.online_lookup_btn.setText(_td("dict.online_btn", lang))

    def apply_theme(self, mode: str) -> None:
        self._mode = mode
        p = get_palette(mode)
        self.icon_lbl.setPixmap(load_svg_pixmap("book", color=p["text_secondary"], size=16))
        self.detach_btn.setIcon(load_svg_icon("split", color=p["icon_color"], size=13))
        self.close_btn.setIcon(load_svg_icon("close", color=p["icon_color"], size=13))
        self.copy_btn.setIcon(load_svg_icon("copy", color=p["icon_color"], size=13))
        self.res_pos_tag.apply_theme(mode)
