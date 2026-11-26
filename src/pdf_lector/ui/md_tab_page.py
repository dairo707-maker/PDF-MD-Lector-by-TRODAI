"""3-mode Markdown reader/editor tab (Lectura | Dividido | Editor) with sanitized QTextBrowser preview."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QTimer, QUrl, Qt, Signal
from PySide6.QtGui import QCursor, QFont, QFontDatabase, QImage, QTextCursor, QTextDocument
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QSplitter,
    QTextBrowser,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core.bookmarks_store import BookmarksStore
from ..core.i18n import TRANSLATIONS, t
from ..core.markdown_engine import (
    TocItem,
    normalize_toc_outline_levels,
    parse_markdown_toc,
    render_markdown_to_safe_html,
)
from ..core.md_security import validate_doc_resource_ref
from ..core.path_guard import resolve_safe_doc_resource, validate_local_markdown_path
from ..core.win32_bridge import atomic_write_text
from ..theme.motion_engine import MotionBackdropWidget, MotionPerformanceGuard
from ..theme.palette import MONO_FONT_FAMILY, UI_FONT_FAMILY, get_palette
from ..theme.theme_profile import ThemeProfile
from .svg_icons import load_svg_icon
from .widgets import AdaptiveButton, FlowLayout, SlidingSegmentedControl, StatusTag

_LOCAL_MD_TEXTS: dict[str, dict[str, str]] = {
    "es": {
        "md.mode.read": "Lectura",
        "md.mode.split": "Dividido",
        "md.mode.edit": "Editor",
        "md.toc_header": "Tabla de contenido (MD)",
        "md.toc_empty": "Este Markdown no contiene encabezados.",
        "md.safe_badge": "MD SEGURO",
        "md.blocked_badge": "{count} BLOQUEADOS",
        "md.save": "Guardar",
        "md.save_as": "Guardar como",
        "md.fmt.bold": "Negrita",
        "md.fmt.italic": "Cursiva",
        "md.fmt.code": "Codigo",
        "md.fmt.list": "Lista",
        "md.fmt.table": "Tabla",
        "md.fmt.link": "Enlace",
        "dict.context_lookup": "Buscar en diccionario",
    },
    "en": {
        "md.mode.read": "Read",
        "md.mode.split": "Split",
        "md.mode.edit": "Edit",
        "md.toc_header": "Table of Contents (MD)",
        "md.toc_empty": "This Markdown document has no headings.",
        "md.safe_badge": "SAFE MD",
        "md.blocked_badge": "{count} BLOCKED",
        "md.save": "Save",
        "md.save_as": "Save as",
        "md.fmt.bold": "Bold",
        "md.fmt.italic": "Italic",
        "md.fmt.code": "Code",
        "md.fmt.list": "List",
        "md.fmt.table": "Table",
        "md.fmt.link": "Link",
        "dict.context_lookup": "Look up in dictionary",
    },
    "pt": {
        "md.mode.read": "Leitura",
        "md.mode.split": "Dividido",
        "md.mode.edit": "Editor",
        "md.toc_header": "Indice (MD)",
        "md.toc_empty": "Este Markdown nao contem cabecalhos.",
        "md.safe_badge": "MD SEGURO",
        "md.blocked_badge": "{count} BLOQUEADOS",
        "md.save": "Salvar",
        "md.save_as": "Salvar como",
        "md.fmt.bold": "Negrito",
        "md.fmt.italic": "Italico",
        "md.fmt.code": "Codigo",
        "md.fmt.list": "Lista",
        "md.fmt.table": "Tabela",
        "md.fmt.link": "Link",
        "dict.context_lookup": "Buscar no dicionario",
    },
}


def _tm(key: str, lang: str = "es", **kwargs: object) -> str:
    code = lang if lang in ("es", "en", "pt") else "es"
    if key in TRANSLATIONS.get(code, {}):
        return t(key, code, **kwargs)
    tmpl = _LOCAL_MD_TEXTS.get(code, _LOCAL_MD_TEXTS["es"]).get(
        key, _LOCAL_MD_TEXTS["es"].get(key, key)
    )
    try:
        return tmpl.format(**kwargs) if kwargs else tmpl
    except Exception:
        return tmpl


class SafeMarkdownBrowser(QTextBrowser):
    """Hardened QTextBrowser blocking external links, remote images, UNC shares, and traversal."""

    lookup_word_requested = Signal(str)
    resource_blocked = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setOpenExternalLinks(False)
        self.setOpenLinks(False)
        self._lang = "es"
        self._base_dir: Path | None = None
        self._blocked_resources: list[str] = []
        self._allow_remote = False
        self._empty_img = QImage(1, 1, QImage.Format.Format_ARGB32)
        self._empty_img.fill(Qt.GlobalColor.transparent)
        self.anchorClicked.connect(self._on_anchor_clicked)

    def set_base_dir(self, base_dir: Path | None) -> None:
        self._base_dir = base_dir

    def set_lang(self, lang: str) -> None:
        self._lang = lang if lang in ("es", "en", "pt") else "es"

    def set_blocked_resources(self, blocked: list[str]) -> None:
        self._blocked_resources = list(blocked)

    def loadResource(self, resource_type: int, name: QUrl):  # type: ignore[override]
        raw = name.toString().strip()
        if resource_type == int(QTextDocument.ResourceType.ImageResource.value) or resource_type == 2:
            allowed, _, _ = validate_doc_resource_ref(
                raw,
                base_dir=self._base_dir,
                is_image=True,
                allow_remote_images=self._allow_remote,
            )
            if allowed and self._base_dir is not None:
                try:
                    resolved = resolve_safe_doc_resource(
                        self._base_dir,
                        raw.split("#", 1)[0],
                        check_exists=True,
                    )
                    img = QImage(str(resolved))
                    if not img.isNull():
                        return img
                except ValueError:
                    pass
            if raw and raw not in self._blocked_resources:
                self._blocked_resources.append(raw)
                self.resource_blocked.emit(raw)
            return self._empty_img
        return None

    def _on_anchor_clicked(self, url: QUrl) -> None:
        raw = url.toString().strip()
        frag = url.fragment().strip()
        if raw.startswith("#") or frag:
            target = frag or raw.lstrip("#")
            if target:
                self.scrollToAnchor(target)

    def contextMenuEvent(self, event) -> None:
        menu = self.createStandardContextMenu()
        menu.addSeparator()
        cursor = self.textCursor()
        selected = cursor.selectedText().strip()
        if not selected:
            probe = self.cursorForPosition(event.pos())
            probe.select(QTextCursor.SelectionType.WordUnderCursor)
            selected = probe.selectedText().strip()
        clean = "".join(ch for ch in selected if ch.isalnum() or ch in ("-", "'"))
        act = menu.addAction(_tm("dict.context_lookup", self._lang))
        act.setEnabled(bool(clean))
        act.triggered.connect(lambda: self.lookup_word_requested.emit(clean))
        menu.exec(QCursor.pos())


class MarkdownTabPage(QWidget):
    """3-mode Markdown viewer/editor tab symmetric with PdfTabPage."""

    state_changed = Signal()
    page_words_ready = Signal(list)
    lookup_word_requested = Signal(str)

    def __init__(
        self,
        md_path: Path,
        *,
        bookmarks_store: BookmarksStore,
        guard: MotionPerformanceGuard | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.md_path = Path(md_path).resolve()
        self.file_path = self.md_path
        self.bookmarks_store = bookmarks_store
        self._lang = "es"
        self._mode = "dark"
        self._view_mode = "read"
        self._rail_visible = True
        self._rail_mode = "toc"
        self._profile = ThemeProfile()
        self.blocked_resources: list[str] = []

        self._saved_text = self.md_path.read_text(encoding="utf-8-sig")

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Left Navigation Rail (TOC + Bookmarks)
        self.nav_rail = QFrame(self)
        self.nav_rail.setObjectName("ThumbRail")
        self.nav_rail.setFixedWidth(256)
        rail_layout = QVBoxLayout(self.nav_rail)
        rail_layout.setContentsMargins(8, 8, 8, 8)
        rail_layout.setSpacing(8)

        self.rail_mode_switch = SlidingSegmentedControl(self.nav_rail)
        self.rail_mode_switch.set_options(
            [
                ("thumbs", t("viewer.rail.thumbs", self._lang)),
                ("toc", t("viewer.rail.toc", self._lang)),
                ("both", t("viewer.rail.both", self._lang)),
            ]
        )
        self.rail_mode_switch.set_current("toc", animate=False)
        self.rail_mode_switch.selection_changed.connect(self.set_rail_mode)
        rail_layout.addWidget(self.rail_mode_switch)

        self.rail_splitter = QSplitter(Qt.Orientation.Vertical, self.nav_rail)
        self.rail_splitter.setHandleWidth(2)

        self.toc_pane = QWidget(self.rail_splitter)
        self.toc_pane.setObjectName("TransparentHost")
        toc_layout = QVBoxLayout(self.toc_pane)
        toc_layout.setContentsMargins(0, 0, 0, 0)
        toc_layout.setSpacing(6)

        self.toc_hdr_lbl = QLabel("", self.toc_pane)
        th_f = QFont(UI_FONT_FAMILY, 9)
        th_f.setWeight(QFont.Weight.Bold)
        self.toc_hdr_lbl.setFont(th_f)
        toc_layout.addWidget(self.toc_hdr_lbl)

        self.toc_tree = QTreeWidget(self.toc_pane)
        self.toc_tree.setHeaderHidden(True)
        self.toc_tree.setIndentation(14)
        self.toc_tree.itemClicked.connect(self._on_toc_item_clicked)
        toc_layout.addWidget(self.toc_tree, 3)

        self.toc_empty_lbl = QLabel("", self.toc_pane)
        self.toc_empty_lbl.setProperty("tone", "muted")
        self.toc_empty_lbl.setWordWrap(True)
        self.toc_empty_lbl.setVisible(False)
        toc_layout.addWidget(self.toc_empty_lbl)

        self.bm_hdr_lbl = QLabel("", self.toc_pane)
        self.bm_hdr_lbl.setFont(th_f)
        toc_layout.addWidget(self.bm_hdr_lbl)

        bm_actions = FlowLayout(h_spacing=6, v_spacing=4)
        self.bm_add_btn = AdaptiveButton("", self.toc_pane, role="GhostAction")
        self.bm_add_btn.setMinimumHeight(26)
        self.bm_add_btn.setStyleSheet("padding: 4px 10px; font-size: 11px;")
        self.bm_add_btn.clicked.connect(self._prompt_add_bookmark)
        bm_actions.addWidget(self.bm_add_btn)

        self.bm_del_btn = AdaptiveButton("", self.toc_pane, role="GhostAction")
        self.bm_del_btn.setMinimumHeight(26)
        self.bm_del_btn.setStyleSheet("padding: 4px 10px; font-size: 11px;")
        self.bm_del_btn.clicked.connect(self._remove_selected_bookmark)
        bm_actions.addWidget(self.bm_del_btn)
        toc_layout.addLayout(bm_actions)

        self.bm_list = QListWidget(self.toc_pane)
        self.bm_list.itemClicked.connect(self._on_bookmark_clicked)
        toc_layout.addWidget(self.bm_list, 2)

        self.rail_splitter.addWidget(self.toc_pane)
        self.thumb_list = QListWidget(self.rail_splitter)
        self.thumb_list.setVisible(False)
        self.rail_splitter.addWidget(self.thumb_list)
        rail_layout.addWidget(self.rail_splitter, 1)
        root.addWidget(self.nav_rail)

        # Right Workspace Frame with MotionBackdropWidget
        self.canvas_frame = QFrame(self)
        self.canvas_frame.setObjectName("ViewerCanvasFrame")
        canvas_wrap = QVBoxLayout(self.canvas_frame)
        canvas_wrap.setContentsMargins(0, 0, 0, 0)

        self.backdrop = MotionBackdropWidget(self.canvas_frame, guard=guard, is_viewer_surface=True)
        canvas_wrap.addWidget(self.backdrop)

        main_col = QVBoxLayout(self.backdrop)
        main_col.setContentsMargins(10, 10, 10, 10)
        main_col.setSpacing(8)

        # Top Mode Bar (Lectura | Dividido | Editor + Security StatusTag)
        mode_bar = QFrame(self.backdrop)
        mode_bar.setObjectName("CardPanel")
        mode_bar_layout = QHBoxLayout(mode_bar)
        mode_bar_layout.setContentsMargins(8, 6, 8, 6)
        mode_bar_layout.setSpacing(8)

        self.view_mode_switch = SlidingSegmentedControl(mode_bar)
        self.view_mode_switch.set_options(
            [
                ("read", _tm("md.mode.read", self._lang)),
                ("split", _tm("md.mode.split", self._lang)),
                ("edit", _tm("md.mode.edit", self._lang)),
            ]
        )
        self.view_mode_switch.set_current("read", animate=False)
        self.view_mode_switch.selection_changed.connect(self.set_view_mode)
        mode_bar_layout.addWidget(self.view_mode_switch, 1)

        self.blocked_tag = StatusTag(_tm("md.safe_badge", self._lang), tone="ok", parent=mode_bar)
        mode_bar_layout.addWidget(self.blocked_tag)
        main_col.addWidget(mode_bar)

        # Split Body: Editor Pane + Preview Browser
        self.body_splitter = QSplitter(Qt.Orientation.Horizontal, self.backdrop)
        self.body_splitter.setHandleWidth(4)

        self.editor_pane = QFrame(self.body_splitter)
        self.editor_pane.setObjectName("CardPanel")
        ed_layout = QVBoxLayout(self.editor_pane)
        ed_layout.setContentsMargins(6, 6, 6, 6)
        ed_layout.setSpacing(6)

        self.format_toolbar = QWidget(self.editor_pane)
        self.format_toolbar.setObjectName("TransparentHost")
        fmt_flow = FlowLayout(self.format_toolbar, margin=0, h_spacing=4, v_spacing=4)

        self._fmt_btns: dict[str, AdaptiveButton] = {}
        fmt_specs = [
            ("h1", "H1", lambda: self.insert_markdown_snippet("# ")),
            ("h2", "H2", lambda: self.insert_markdown_snippet("## ")),
            ("h3", "H3", lambda: self.insert_markdown_snippet("### ")),
            ("bold", _tm("md.fmt.bold", self._lang), lambda: self._wrap_selection("**", "**", "texto")),
            ("italic", _tm("md.fmt.italic", self._lang), lambda: self._wrap_selection("*", "*", "texto")),
            ("code", _tm("md.fmt.code", self._lang), lambda: self.insert_markdown_snippet("\n```python\n\n```\n")),
            ("list", _tm("md.fmt.list", self._lang), lambda: self.insert_markdown_snippet("\n- ")),
            (
                "table",
                _tm("md.fmt.table", self._lang),
                lambda: self.insert_markdown_snippet("\n| Col 1 | Col 2 |\n| --- | --- |\n| A | B |\n"),
            ),
            ("link", _tm("md.fmt.link", self._lang), lambda: self.insert_markdown_snippet("[titulo](./ruta.md)")),
            ("save", _tm("md.save", self._lang), self.save_document),
            ("save_as", _tm("md.save_as", self._lang), lambda: self.save_document_as(None)),
        ]
        for key, label, slot in fmt_specs:
            role = "PrimaryAction" if key == "save" else "GhostAction"
            btn = AdaptiveButton(label, self.format_toolbar, role=role)
            btn.setMinimumHeight(26)
            btn.setStyleSheet("padding: 3px 8px; font-size: 11px;")
            btn.clicked.connect(slot)
            fmt_flow.addWidget(btn)
            self._fmt_btns[key] = btn

        ed_layout.addWidget(self.format_toolbar)

        self.editor_widget = QPlainTextEdit(self.editor_pane)
        mono_fam = MONO_FONT_FAMILY if QFontDatabase.hasFamily(MONO_FONT_FAMILY) else "Consolas"
        ed_font = QFont(mono_fam, 10)
        ed_font.setWeight(QFont.Weight.Normal)
        self.editor_widget.setFont(ed_font)
        self.editor_widget.setPlainText(self._saved_text)
        self.editor_widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.editor_widget.customContextMenuRequested.connect(self._on_editor_context_menu)
        self.editor_widget.textChanged.connect(self._on_editor_text_changed)
        ed_layout.addWidget(self.editor_widget, 1)

        self.body_splitter.addWidget(self.editor_pane)

        self.preview_browser = SafeMarkdownBrowser(self.body_splitter)
        self.preview_browser.set_base_dir(self.md_path.parent)
        self.preview_browser.lookup_word_requested.connect(self.lookup_word_requested.emit)
        self.preview_browser.resource_blocked.connect(lambda _r: self._sync_blocked_badge())
        self.body_splitter.addWidget(self.preview_browser)

        main_col.addWidget(self.body_splitter, 1)
        root.addWidget(self.canvas_frame, 1)

        self._render_preview()
        self._populate_toc()
        self._populate_bookmarks()
        self.set_view_mode("read")
        self.set_rail_mode("toc")
        self.retranslate(self._lang)
        self.apply_profile(self._profile)

    def keyPressEvent(self, event) -> None:
        if (
            event.key() == Qt.Key.Key_S
            and (event.modifiers() & Qt.KeyboardModifier.ControlModifier)
            and not (event.modifiers() & (Qt.KeyboardModifier.AltModifier | Qt.KeyboardModifier.ShiftModifier))
        ):
            self.save_document()
            event.accept()
            return
        super().keyPressEvent(event)

    def view_mode(self) -> str:
        return self._view_mode

    def set_view_mode(self, mode: str) -> None:
        clean = mode if mode in ("read", "split", "edit") else "read"
        self._view_mode = clean
        self.view_mode_switch.set_current(clean, animate=True, emit_signal=False)
        show_preview = clean in ("read", "split")
        show_editor = clean in ("split", "edit")
        if show_preview:
            self._render_preview()
        self.preview_browser.setVisible(show_preview)
        self.editor_pane.setVisible(show_editor)
        self.format_toolbar.setVisible(show_editor)
        self.editor_widget.setVisible(show_editor)
        if clean == "split":
            self.body_splitter.setSizes([420, 420])

    def _wrap_selection(self, prefix: str, suffix: str, placeholder: str) -> None:
        cur = self.editor_widget.textCursor()
        selected = cur.selectedText() or placeholder
        cur.insertText(f"{prefix}{selected}{suffix}")
        self.editor_widget.setTextCursor(cur)

    def insert_markdown_snippet(self, snippet: str) -> None:
        cur = self.editor_widget.textCursor()
        cur.insertText(snippet)
        self.editor_widget.setTextCursor(cur)

    def _on_editor_text_changed(self) -> None:
        self._render_preview()
        self._populate_toc()
        self.state_changed.emit()

    def has_unsaved_changes(self) -> bool:
        return self.editor_widget.toPlainText() != self._saved_text

    def save_document(self) -> bool:
        ok, _ = validate_local_markdown_path(self.md_path, check_exists=False)
        if not ok:
            return False
        text = self.editor_widget.toPlainText()
        atomic_write_text(self.md_path, text)
        self._saved_text = text
        self._populate_toc()
        self.state_changed.emit()
        return True

    def save_document_as(self, target_path: Path | None = None) -> bool:
        if target_path is None:
            chosen, _ = QFileDialog.getSaveFileName(
                self,
                _tm("md.save_as", self._lang),
                str(self.md_path),
                "Markdown (*.md *.markdown)",
            )
            if not chosen:
                return False
            target_path = Path(chosen)
        ok, _ = validate_local_markdown_path(target_path, check_exists=False)
        if not ok:
            return False
        resolved = Path(target_path).resolve()
        text = self.editor_widget.toPlainText()
        atomic_write_text(resolved, text)
        self.md_path = resolved
        self.file_path = resolved
        self.setProperty("base_tab_title", resolved.name)
        self.preview_browser.set_base_dir(resolved.parent)
        self._saved_text = text
        self._populate_toc()
        self._populate_bookmarks()
        self.state_changed.emit()
        return True

    def _sync_blocked_badge(self) -> None:
        merged = list(self.blocked_resources)
        for item in self.preview_browser._blocked_resources:
            if item not in merged:
                merged.append(item)
        self.blocked_resources = merged
        if self.blocked_resources:
            self.blocked_tag.set_tone(
                "warn", _tm("md.blocked_badge", self._lang, count=len(self.blocked_resources))
            )
        else:
            self.blocked_tag.set_tone("ok", _tm("md.safe_badge", self._lang))

    def _render_preview(self) -> None:
        text = self.editor_widget.toPlainText()
        body_html, blocked = render_markdown_to_safe_html(
            text,
            base_dir=self.md_path.parent,
            allow_remote_images=False,
        )
        self.blocked_resources = list(blocked)
        self.preview_browser.set_blocked_resources(blocked)
        self._sync_blocked_badge()

        is_dark = self._mode != "light"
        bg = "#131B2C" if is_dark else "#FFFFFF"
        fg = "#F8FAFC" if is_dark else "#0A0F1D"
        sec = "#CBD5E1" if is_dark else "#334155"
        muted = "#94A3B8" if is_dark else "#4A5568"
        code_bg = "#182234" if is_dark else "#EEF2F6"
        border = "#334155" if is_dark else "#D5DCE6"
        link_col = "#38BDF8" if is_dark else "#2563EB"
        ui_fam = UI_FONT_FAMILY if QFontDatabase.hasFamily(UI_FONT_FAMILY) else "Segoe UI"
        mono_fam = MONO_FONT_FAMILY if QFontDatabase.hasFamily(MONO_FONT_FAMILY) else "Consolas"

        styled_html = (
            "<!DOCTYPE html><html><head><meta charset='utf-8'><style>"
            f"body {{ background-color: {bg}; color: {sec}; font-family: '{ui_fam}', sans-serif; "
            f"font-size: 13px; font-weight: 400; line-height: 1.55; margin: 18px; }}"
            f"h1, h2, h3, h4, h5, h6 {{ color: {fg}; font-weight: 700; margin-top: 14px; margin-bottom: 6px; }}"
            f"strong, b {{ font-weight: 700; color: {fg}; }}"
            f"a {{ color: {link_col}; text-decoration: underline; }}"
            f"code, pre {{ font-family: '{mono_fam}', monospace; background-color: {code_bg}; "
            f"color: {fg}; font-weight: 400; }}"
            f"pre {{ padding: 10px; border: 1px solid {border}; }}"
            f"blockquote {{ margin-left: 14px; padding-left: 10px; color: {muted}; border-left: 3px solid {border}; }}"
            f"table {{ border-collapse: collapse; margin: 10px 0; }}"
            f"th, td {{ border: 1px solid {border}; padding: 6px 10px; text-align: left; }}"
            f"th {{ background-color: {code_bg}; color: {fg}; font-weight: 700; }}"
            "</style></head><body>"
            f"{body_html}"
            "</body></html>"
        )
        self.preview_browser.setHtml(styled_html)

    def _populate_toc(self) -> None:
        self.toc_tree.clear()
        toc_items: list[TocItem] = parse_markdown_toc(self.editor_widget.toPlainText())
        if not toc_items:
            self.toc_tree.setVisible(False)
            self.toc_empty_lbl.setVisible(True)
            return

        self.toc_tree.setVisible(True)
        self.toc_empty_lbl.setVisible(False)
        levels = normalize_toc_outline_levels(toc_items)
        stack: list[tuple[int, QTreeWidgetItem]] = []

        for entry, lvl in zip(toc_items, levels):
            node = QTreeWidgetItem([entry.title])
            node.setData(0, Qt.ItemDataRole.UserRole, entry.line_number)
            node.setData(0, Qt.ItemDataRole.UserRole + 1, entry.anchor)
            while stack and stack[-1][0] >= lvl:
                stack.pop()
            if stack:
                stack[-1][1].addChild(node)
            else:
                self.toc_tree.addTopLevelItem(node)
            stack.append((lvl, node))
        self.toc_tree.expandAll()

    def _on_toc_item_clicked(self, item: QTreeWidgetItem, _col: int) -> None:
        anchor = str(item.data(0, Qt.ItemDataRole.UserRole + 1) or "")
        line_num = int(item.data(0, Qt.ItemDataRole.UserRole) or 1)
        if anchor and self.preview_browser.isVisible():
            self.preview_browser.scrollToAnchor(anchor)
        block = self.editor_widget.document().findBlockByLineNumber(max(0, line_num - 1))
        if block.isValid():
            cur = self.editor_widget.textCursor()
            cur.setPosition(block.position())
            self.editor_widget.setTextCursor(cur)

    def _populate_bookmarks(self) -> None:
        self.bm_list.clear()
        for bm in self.bookmarks_store.list_bookmarks(self.md_path):
            item = QListWidgetItem(f"{bm.title}   [L{bm.page_index + 1}]")
            item.setData(Qt.ItemDataRole.UserRole, bm.page_index)
            item.setData(Qt.ItemDataRole.UserRole + 1, bm.bookmark_id)
            self.bm_list.addItem(item)

    def add_bookmark(self, title: str, line_index: int | None = None) -> None:
        target = self.editor_widget.textCursor().blockNumber() if line_index is None else line_index
        self.bookmarks_store.add_bookmark(self.md_path, title, max(0, int(target)))
        QTimer.singleShot(0, self._populate_bookmarks)

    def _prompt_add_bookmark(self) -> None:
        line_idx = self.editor_widget.textCursor().blockNumber()
        default_name = f"Marcador L{line_idx + 1}"
        title, ok = QInputDialog.getText(
            self,
            t("viewer.bookmarks_header", self._lang),
            t("viewer.bookmark_prompt", self._lang, page=line_idx + 1),
            text=default_name,
        )
        if ok and title.strip():
            self.add_bookmark(title.strip(), line_idx)

    def _remove_selected_bookmark(self) -> None:
        item = self.bm_list.currentItem()
        if item is None:
            return
        bm_id = str(item.data(Qt.ItemDataRole.UserRole + 1) or "")
        if bm_id and self.bookmarks_store.remove_bookmark(self.md_path, bm_id):
            QTimer.singleShot(0, self._populate_bookmarks)

    def _on_bookmark_clicked(self, item: QListWidgetItem) -> None:
        line_idx = int(item.data(Qt.ItemDataRole.UserRole) or 0)
        block = self.editor_widget.document().findBlockByLineNumber(max(0, line_idx))
        if block.isValid():
            cur = self.editor_widget.textCursor()
            cur.setPosition(block.position())
            self.editor_widget.setTextCursor(cur)

    def _on_editor_context_menu(self, pos) -> None:
        menu = self.editor_widget.createStandardContextMenu()
        menu.addSeparator()
        cur = self.editor_widget.textCursor()
        selected = cur.selectedText().strip()
        if not selected:
            probe = self.editor_widget.cursorForPosition(pos)
            probe.select(QTextCursor.SelectionType.WordUnderCursor)
            selected = probe.selectedText().strip()
        clean = "".join(ch for ch in selected if ch.isalnum() or ch in ("-", "'"))
        act = menu.addAction(_tm("dict.context_lookup", self._lang))
        act.setEnabled(bool(clean))
        act.triggered.connect(lambda: self.lookup_word_requested.emit(clean))
        menu.exec(QCursor.pos())

    def toggle_rail(self) -> None:
        self._rail_visible = not self._rail_visible
        self.nav_rail.setVisible(self._rail_visible)

    def set_rail_mode(self, mode: str) -> None:
        clean = mode if mode in ("thumbs", "toc", "both") else "toc"
        self._rail_mode = clean
        self.rail_mode_switch.set_current(clean, animate=True, emit_signal=False)
        self.toc_pane.setVisible(clean in ("toc", "both"))
        self.thumb_list.setVisible(clean in ("thumbs", "both"))

    def rail_mode(self) -> str:
        return self._rail_mode

    def emit_document_words(self) -> None:
        text = self.editor_widget.toPlainText()
        seen: set[str] = set()
        words: list[str] = []
        buf: list[str] = []
        for ch in text:
            if ch.isalpha():
                buf.append(ch)
            else:
                if len(buf) >= 4:
                    w = "".join(buf)
                    lw = w.lower()
                    if lw not in seen:
                        seen.add(lw)
                        words.append(w)
                        if len(words) >= 16:
                            break
                buf.clear()
        if len(buf) >= 4 and len(words) < 16:
            w = "".join(buf)
            if w.lower() not in seen:
                words.append(w)
        self.page_words_ready.emit(words)

    emit_current_page_words = emit_document_words

    def retranslate(self, lang: str) -> None:
        self._lang = lang if lang in ("es", "en", "pt") else "es"
        self.preview_browser.set_lang(self._lang)
        self.rail_mode_switch.set_options(
            [
                ("thumbs", t("viewer.rail.thumbs", self._lang)),
                ("toc", t("viewer.rail.toc", self._lang)),
                ("both", t("viewer.rail.both", self._lang)),
            ]
        )
        self.rail_mode_switch.set_current(self._rail_mode, animate=False)
        self.view_mode_switch.set_options(
            [
                ("read", _tm("md.mode.read", self._lang)),
                ("split", _tm("md.mode.split", self._lang)),
                ("edit", _tm("md.mode.edit", self._lang)),
            ]
        )
        self.view_mode_switch.set_current(self._view_mode, animate=False)
        self.toc_hdr_lbl.setText(_tm("md.toc_header", self._lang))
        self.toc_empty_lbl.setText(_tm("md.toc_empty", self._lang))
        self.bm_hdr_lbl.setText(t("viewer.bookmarks_header", self._lang))
        self.bm_add_btn.setText(t("viewer.bookmark_add", self._lang))
        self.bm_del_btn.setText(t("viewer.bookmark_del", self._lang))

        label_map = {
            "bold": "md.fmt.bold",
            "italic": "md.fmt.italic",
            "code": "md.fmt.code",
            "list": "md.fmt.list",
            "table": "md.fmt.table",
            "link": "md.fmt.link",
            "save": "md.save",
            "save_as": "md.save_as",
        }
        for key, i18n_key in label_map.items():
            if key in self._fmt_btns:
                self._fmt_btns[key].setText(_tm(i18n_key, self._lang))
        self._sync_blocked_badge()

    def apply_profile(self, profile: ThemeProfile) -> None:
        self._profile = profile
        self._mode = "light" if profile.base_mode == "light" else "dark"
        p = get_palette(self._mode)
        self.backdrop.set_profile(profile)
        self.rail_mode_switch.apply_theme(self._mode)
        self.rail_mode_switch.set_animations_enabled(profile.motion_level != "static")
        self.view_mode_switch.apply_theme(self._mode)
        self.view_mode_switch.set_animations_enabled(profile.motion_level != "static")
        self.blocked_tag.apply_theme(self._mode)
        if "save" in self._fmt_btns:
            self._fmt_btns["save"].setIcon(load_svg_icon("check", color=p["primary_fg"], size=13))
        if "save_as" in self._fmt_btns:
            self._fmt_btns["save_as"].setIcon(load_svg_icon("export", color=p["icon_color"], size=13))
        self._render_preview()

    def cleanup(self) -> None:
        self.backdrop.cleanup()
