"""Collapsible right-side Notes Organizer panel for searchable, categorized, colored sticky notes."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)
import shiboken6

from ..core.pdf_annotations import (
    AnnotationRecord,
    CLASSIC_HIGHLIGHT_COLORS,
    NOTE_CATEGORIES,
    sanitize_color_hex,
    sanitize_note_category,
)
from ..theme.palette import MONO_FONT_FAMILY, UI_FONT_FAMILY, get_palette
from .svg_icons import load_svg_icon, load_svg_pixmap
from .widgets import AdaptiveButton, FlowLayout, StatusTag

_NOTES_TEXTS: dict[str, dict[str, str]] = {
    "es": {
        "title": "Notas del documento",
        "close": "Cerrar panel",
        "toggle_vis": "Mostrar / ocultar notas en la pagina",
        "new_note": "+ Nueva nota",
        "search_placeholder": "Buscar en notas o categoria...",
        "all_cats": "Todas",
        "all_colors": "Todos los colores",
        "empty": "Aun no hay notas adhesivas. Crea una o haz clic sobre una nota en la pagina.",
        "open_card": "Ir y abrir",
        "delete": "Borrar",
        "delete_confirm": "Eliminar permanentemente esta nota adhesiva?",
        "prompt_text": "Contenido de la nueva nota:",
    },
    "en": {
        "title": "Document Notes",
        "close": "Close panel",
        "toggle_vis": "Show / hide notes on page",
        "new_note": "+ New note",
        "search_placeholder": "Search notes or category...",
        "all_cats": "All",
        "all_colors": "All colors",
        "empty": "No sticky notes yet. Add one or click a sticky note on the page.",
        "open_card": "Jump & open",
        "delete": "Delete",
        "delete_confirm": "Permanently delete this sticky note?",
        "prompt_text": "New sticky note content:",
    },
    "pt": {
        "title": "Notas do documento",
        "close": "Fechar painel",
        "toggle_vis": "Mostrar / ocultar notas na pagina",
        "new_note": "+ Nova nota",
        "search_placeholder": "Buscar em notas ou categoria...",
        "all_cats": "Todas",
        "all_colors": "Todas as cores",
        "empty": "Ainda nao ha notas adesivas. Crie uma ou clique numa nota na pagina.",
        "open_card": "Ir e abrir",
        "delete": "Excluir",
        "delete_confirm": "Excluir permanentemente esta nota adesiva?",
        "prompt_text": "Conteudo da nova nota:",
    },
}


def _tn(key: str, lang: str) -> str:
    code = lang if lang in ("es", "en", "pt") else "es"
    return _NOTES_TEXTS[code].get(key, _NOTES_TEXTS["es"].get(key, key))


class NotesPanel(QFrame):
    """Searchable, filterable, color/category organizer for document sticky notes."""

    close_requested = Signal()
    jump_to_note_requested = Signal(int, str)
    add_note_requested = Signal(str, str, str)
    delete_note_requested = Signal(str)
    update_note_requested = Signal(str, str, str)
    notes_visibility_toggled = Signal(bool)

    def __init__(self, *, mode: str = "dark", lang: str = "es", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("NotesPanel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedWidth(284)
        self._mode = "light" if mode == "light" else "dark"
        self._applied_mode = ""
        self._lang = lang if lang in ("es", "en", "pt") else "es"
        self._notes: list[AnnotationRecord] = []
        self._selected_color: str = ""
        self._notes_visible: bool = True

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        # Header
        self._header_frame = QFrame(self)
        self._header_frame.setObjectName("NotesHeader")
        hdr = QHBoxLayout(self._header_frame)
        hdr.setContentsMargins(8, 6, 8, 6)
        hdr.setSpacing(6)

        self._icon_lbl = QLabel(self._header_frame)
        self._icon_lbl.setFixedSize(16, 16)
        hdr.addWidget(self._icon_lbl)

        self.title_lbl = QLabel(_tn("title", self._lang), self._header_frame)
        self.title_lbl.setFont(QFont(UI_FONT_FAMILY, 10, QFont.Weight.Bold))
        hdr.addWidget(self.title_lbl, 1)

        self.count_tag = StatusTag("0", tone="accent", parent=self._header_frame)
        hdr.addWidget(self.count_tag)

        self.toggle_vis_btn = QPushButton("", self._header_frame)
        self.toggle_vis_btn.setObjectName("NotesHeaderBtn")
        self.toggle_vis_btn.setCheckable(True)
        self.toggle_vis_btn.setChecked(True)
        self.toggle_vis_btn.setFixedSize(26, 26)
        self.toggle_vis_btn.setToolTip(_tn("toggle_vis", self._lang))
        self.toggle_vis_btn.clicked.connect(lambda ch: self.notes_visibility_toggled.emit(bool(ch)))
        hdr.addWidget(self.toggle_vis_btn)

        self.close_btn = QPushButton("", self._header_frame)
        self.close_btn.setObjectName("NotesHeaderBtn")
        self.close_btn.setFixedSize(26, 26)
        self.close_btn.setToolTip(_tn("close", self._lang))
        self.close_btn.clicked.connect(self.close_requested.emit)
        hdr.addWidget(self.close_btn)
        root.addWidget(self._header_frame)

        # Quick create bar
        add_row = QHBoxLayout()
        add_row.setSpacing(6)
        self.new_cat_combo = QComboBox(self)
        for cat in NOTE_CATEGORIES:
            self.new_cat_combo.addItem(cat, cat)
        add_row.addWidget(self.new_cat_combo, 1)

        self.add_note_btn = AdaptiveButton(_tn("new_note", self._lang), self, role="PrimaryAction")
        self.add_note_btn.setMinimumHeight(28)
        self.add_note_btn.clicked.connect(self._on_add_note_clicked)
        add_row.addWidget(self.add_note_btn)
        root.addLayout(add_row)

        # Search + Category Filter
        self.search_input = QLineEdit(self)
        self.search_input.setClearButtonEnabled(True)
        self.search_input.setPlaceholderText(_tn("search_placeholder", self._lang))
        self.search_input.textChanged.connect(self._rebuild_cards)
        root.addWidget(self.search_input)

        filter_row = QHBoxLayout()
        filter_row.setSpacing(6)
        self.cat_filter_combo = QComboBox(self)
        self.cat_filter_combo.addItem(_tn("all_cats", self._lang), "")
        for cat in NOTE_CATEGORIES:
            self.cat_filter_combo.addItem(cat, cat)
        self.cat_filter_combo.currentIndexChanged.connect(self._rebuild_cards)
        filter_row.addWidget(self.cat_filter_combo, 1)

        self.color_filter_combo = QComboBox(self)
        self.color_filter_combo.addItem(_tn("all_colors", self._lang), "")
        for col in CLASSIC_HIGHLIGHT_COLORS:
            self.color_filter_combo.addItem(col, col)
        self.color_filter_combo.currentIndexChanged.connect(self._rebuild_cards)
        filter_row.addWidget(self.color_filter_combo)
        root.addLayout(filter_row)

        # Scrollable note cards
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._cards_host = QWidget(scroll)
        self._cards_host.setObjectName("TransparentHost")
        self._cards_layout = QVBoxLayout(self._cards_host)
        self._cards_layout.setContentsMargins(0, 0, 2, 0)
        self._cards_layout.setSpacing(6)
        scroll.setWidget(self._cards_host)
        root.addWidget(scroll, 1)

        self.apply_theme(self._mode)
        self._rebuild_cards()

    def set_notes(self, records: list[AnnotationRecord]) -> None:
        self._notes = [r for r in records if r.kind == "text"]
        self.count_tag.setText(str(len(self._notes)))
        self._rebuild_cards()

    def _on_add_note_clicked(self) -> None:
        cat = sanitize_note_category(str(self.new_cat_combo.currentData() or "General"))
        col = CLASSIC_HIGHLIGHT_COLORS[0]
        if QApplication.platformName() == "offscreen":
            self.add_note_requested.emit("Nota rapida", cat, col)
            return
        txt, ok = QInputDialog.getText(
            self,
            _tn("new_note", self._lang),
            _tn("prompt_text", self._lang),
        )
        if ok and txt.strip():
            self.add_note_requested.emit(txt.strip(), cat, col)

    def _confirm_and_delete(self, note_id: str) -> None:
        if QApplication.platformName() != "offscreen":
            ans = QMessageBox.question(
                self,
                _tn("delete", self._lang),
                _tn("delete_confirm", self._lang),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if ans != QMessageBox.StandardButton.Yes:
                return
        self.delete_note_requested.emit(note_id)

    def _rebuild_cards(self) -> None:
        while self._cards_layout.count():
            item = self._cards_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.hide()
                w.setParent(None)
                w.deleteLater()

        p = get_palette(self._mode)
        query = self.search_input.text().strip().lower()
        cat_filter = str(self.cat_filter_combo.currentData() or "").strip().lower()
        col_filter = str(self.color_filter_combo.currentData() or "").strip().upper()

        filtered: list[AnnotationRecord] = []
        for rec in self._notes:
            rec_cat = sanitize_note_category(getattr(rec, "category", "General"))
            rec_col = sanitize_color_hex(rec.color_hex)
            if cat_filter and rec_cat.lower() != cat_filter:
                continue
            if col_filter and rec_col != col_filter:
                continue
            if query:
                hay = f"{rec.content} {rec_cat} p.{rec.page_index + 1}".lower()
                if query not in hay:
                    continue
            filtered.append(rec)

        if not filtered:
            empty_lbl = QLabel(_tn("empty", self._lang), self._cards_host)
            empty_lbl.setProperty("tone", "muted")
            empty_lbl.setWordWrap(True)
            self._cards_layout.addWidget(empty_lbl)
            self._cards_layout.addStretch(1)
            return

        for rec in filtered:
            rec_cat = sanitize_note_category(getattr(rec, "category", "General"))
            rec_col = sanitize_color_hex(rec.color_hex)

            card = QFrame(self._cards_host)
            card.setObjectName("NoteOrganizerCard")
            card.setStyleSheet(
                f"QFrame#NoteOrganizerCard {{ background-color: {p['bg_glass_elevated']}; "
                f"border: 1px solid {p['border_strong']}; border-left: 4px solid {rec_col}; }}"
            )
            cl = QVBoxLayout(card)
            cl.setContentsMargins(8, 6, 8, 6)
            cl.setSpacing(5)

            top_row = QHBoxLayout()
            top_row.setSpacing(4)
            page_tag = StatusTag(f"p. {rec.page_index + 1}", tone="accent", parent=card)
            page_tag.apply_theme(self._mode)
            top_row.addWidget(page_tag)

            cat_tag = StatusTag(rec_cat.upper(), tone="ok", parent=card)
            cat_tag.apply_theme(self._mode)
            top_row.addWidget(cat_tag)
            top_row.addStretch(1)

            swatch = QFrame(card)
            swatch.setFixedSize(12, 12)
            swatch.setStyleSheet(f"background-color: {rec_col}; border: 1px solid {p['border_strong']};")
            top_row.addWidget(swatch)
            cl.addLayout(top_row)

            body_lbl = QLabel(rec.content.strip() or "(Sin texto)", card)
            body_lbl.setWordWrap(True)
            body_lbl.setProperty("tone", "secondary")
            cl.addWidget(body_lbl)

            actions = FlowLayout(h_spacing=4, v_spacing=4)
            jump_btn = AdaptiveButton(_tn("open_card", self._lang), card, role="GhostAction")
            jump_btn.setMinimumHeight(24)
            jump_btn.setStyleSheet("padding: 2px 6px; font-size: 10px;")
            jump_btn.clicked.connect(
                lambda _=False, pi=rec.page_index, nid=rec.id: self.jump_to_note_requested.emit(pi, nid)
            )
            actions.addWidget(jump_btn)

            del_btn = AdaptiveButton(_tn("delete", self._lang), card, role="RiskAction")
            del_btn.setMinimumHeight(24)
            del_btn.setStyleSheet("padding: 2px 6px; font-size: 10px;")
            del_btn.clicked.connect(lambda _=False, nid=rec.id: self._confirm_and_delete(nid))
            actions.addWidget(del_btn)
            cl.addLayout(actions)

            self._cards_layout.addWidget(card)

        self._cards_layout.addStretch(1)

    def set_notes_visibility_state(self, visible: bool) -> None:
        self._notes_visible = bool(visible)
        self.toggle_vis_btn.blockSignals(True)
        self.toggle_vis_btn.setChecked(self._notes_visible)
        self.toggle_vis_btn.blockSignals(False)
        p = get_palette(self._mode)
        self.toggle_vis_btn.setIcon(
            load_svg_icon("eye" if self._notes_visible else "eye_off", color=p["icon_color"], size=14)
        )

    def apply_theme(self, mode: str) -> None:
        new_mode = "light" if mode == "light" else "dark"
        if new_mode == self._applied_mode:
            return
        self._applied_mode = new_mode
        self._mode = new_mode
        p = get_palette(self._mode)
        self._icon_lbl.setPixmap(load_svg_pixmap("comment", color=p["icon_color"], size=16))
        self.toggle_vis_btn.setIcon(
            load_svg_icon("eye" if self._notes_visible else "eye_off", color=p["icon_color"], size=14)
        )
        self.close_btn.setIcon(load_svg_icon("close", color=p["icon_color"], size=14))
        self.count_tag.apply_theme(self._mode)
        self.setStyleSheet(
            f"""
            QFrame#NotesPanel {{
                background-color: {p["bg_sidebar"]};
                color: {p["text_primary"]};
                border-left: 1px solid {p["border_hairline"]};
            }}
            QFrame#NotesHeader {{
                background-color: {p["bg_glass"]};
                border: 1px solid {p["border_strong"]};
            }}
            QPushButton#NotesHeaderBtn {{
                background-color: transparent;
                border: 1px solid transparent;
                padding: 0px;
                text-align: center;
            }}
            QPushButton#NotesHeaderBtn:hover {{
                background-color: {p["bg_hover"]};
                border: 1px solid transparent;
            }}
            """
        )
        if shiboken6.isValid(self._cards_host):
            self._rebuild_cards()

    def retranslate(self, lang: str) -> None:
        self._lang = lang if lang in ("es", "en", "pt") else "es"
        self.title_lbl.setText(_tn("title", self._lang))
        self.toggle_vis_btn.setToolTip(_tn("toggle_vis", self._lang))
        self.close_btn.setToolTip(_tn("close", self._lang))
        self.add_note_btn.setText(_tn("new_note", self._lang))
        self.search_input.setPlaceholderText(_tn("search_placeholder", self._lang))
        self.cat_filter_combo.setItemText(0, _tn("all_cats", self._lang))
        self.color_filter_combo.setItemText(0, _tn("all_colors", self._lang))
        self._rebuild_cards()
