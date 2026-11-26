"""Multi-tab PDF/Markdown viewer with 3-mode rail, interactive PdfCanvas, 3-mode MD tab, and Kindle dictionary."""

from __future__ import annotations

from pathlib import Path
from typing import Union

from PySide6.QtCore import QSize, QThreadPool, QTimer, Qt, Signal
from PySide6.QtGui import QFont, QIcon, QImage, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
import shiboken6

from ..core.bookmarks_store import BookmarksStore
from ..core.dictionary_engine import DictionaryEngine
from ..core.i18n import TRANSLATIONS, t
from ..core.path_guard import (
    validate_local_any_doc,
    validate_local_markdown_path,
    validate_local_pdf_path,
)
from ..core.pdf_engine import AsyncPageRenderTask, PdfDocumentSession
from ..core.win32_bridge import apply_trodai_window_chrome
from ..theme.motion_engine import MotionBackdropWidget, MotionPerformanceGuard
from ..theme.palette import UI_FONT_FAMILY, get_palette
from ..theme.theme_profile import ThemeProfile
from .dictionary_panel import DictionaryPanel
from .md_tab_page import MarkdownTabPage
from .pdf_canvas import CLASSIC_HIGHLIGHT_PALETTE, PdfCanvas
from .svg_icons import load_svg_icon
from .widgets import AdaptiveButton, FlowLayout, SlidingSegmentedControl, StatusTag

_LOCAL_VIEWER_TEXTS: dict[str, dict[str, str]] = {
    "es": {
        "viewer.save": "Guardar",
        "viewer.save_as": "Guardar como",
        "viewer.empty_dual": "Abre cualquier PDF o Markdown local o suelta un archivo aqui para leerlo.",
        "viewer.tool.select": "Seleccionar",
        "viewer.tool.highlight": "Resaltar",
        "viewer.tool.note": "Nota",
        "viewer.tool.ink": "Tinta",
        "viewer.tool.eraser": "Borrador",
        "viewer.tool.touch_write": "Escribir tactil",
        "viewer.tool.undo": "Deshacer",
        "dict.floating_title": "Diccionario · PDF/MD Lector",
    },
    "en": {
        "viewer.save": "Save",
        "viewer.save_as": "Save as",
        "viewer.empty_dual": "Open any local PDF or Markdown file or drop a file here to read.",
        "viewer.tool.select": "Select",
        "viewer.tool.highlight": "Highlight",
        "viewer.tool.note": "Note",
        "viewer.tool.ink": "Ink",
        "viewer.tool.eraser": "Eraser",
        "viewer.tool.touch_write": "Touch writing",
        "viewer.tool.undo": "Undo",
        "dict.floating_title": "Dictionary · PDF/MD Lector",
    },
    "pt": {
        "viewer.save": "Salvar",
        "viewer.save_as": "Salvar como",
        "viewer.empty_dual": "Abra qualquer PDF ou Markdown local ou solte um arquivo aqui para ler.",
        "viewer.tool.select": "Selecionar",
        "viewer.tool.highlight": "Realcar",
        "viewer.tool.note": "Nota",
        "viewer.tool.ink": "Tinta",
        "viewer.tool.eraser": "Borracha",
        "viewer.tool.touch_write": "Escrita tatil",
        "viewer.tool.undo": "Desfazer",
        "dict.floating_title": "Dicionario · PDF/MD Lector",
    },
}


def _tv(key: str, lang: str = "es", **kwargs: object) -> str:
    code = lang if lang in ("es", "en", "pt") else "es"
    if key in TRANSLATIONS.get(code, {}):
        return t(key, code, **kwargs)
    tmpl = _LOCAL_VIEWER_TEXTS.get(code, _LOCAL_VIEWER_TEXTS["es"]).get(
        key, _LOCAL_VIEWER_TEXTS["es"].get(key, key)
    )
    try:
        return tmpl.format(**kwargs) if kwargs else tmpl
    except Exception:
        return tmpl


class _FloatingDictionaryWindow(QWidget):
    """Reusable non-deleting floating container for the detachable Kindle dictionary panel."""

    closed_by_user = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.setObjectName("RootCanvas")
        self.setMinimumSize(280, 380)
        self.resize(320, 480)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)

    def attach_panel(self, panel: DictionaryPanel) -> None:
        if panel.parentWidget() is not self:
            panel.setParent(self)
        if self._layout.indexOf(panel) < 0:
            self._layout.addWidget(panel)

    def detach_panel(self, panel: DictionaryPanel) -> None:
        self._layout.removeWidget(panel)

    def apply_theme(self, mode: str) -> None:
        try:
            apply_trodai_window_chrome(int(self.winId()), dark_mode=(mode != "light"), acrylic=True)
        except Exception:
            pass

    def closeEvent(self, event) -> None:  # type: ignore[override]
        event.ignore()
        self.hide()
        self.closed_by_user.emit()


class PdfTabPage(QWidget):
    """Individual PDF tab with 3-mode TOC/Bookmarks/Thumbnails rail and interactive PdfCanvas."""

    state_changed = Signal()
    page_words_ready = Signal(list)
    lookup_word_requested = Signal(str)

    def __init__(
        self,
        pdf_path: Path,
        *,
        bookmarks_store: BookmarksStore,
        guard: MotionPerformanceGuard | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.pdf_path = Path(pdf_path)
        self.file_path = self.pdf_path
        self.bookmarks_store = bookmarks_store
        self.session = PdfDocumentSession(self.pdf_path)
        self.current_page = 0
        self.zoom = 1.15
        self.rotation = 0
        self._lang = "es"
        self._mode = "dark"
        self._rail_visible = True
        self._rail_mode = "both"
        self._active_tasks: set[AsyncPageRenderTask] = set()
        self._profile = ThemeProfile()

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Collapsible Left Navigation Rail (Thumbnails / TOC+Bookmarks / Both)
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
        self.rail_mode_switch.set_current("both", animate=False)
        self.rail_mode_switch.selection_changed.connect(self.set_rail_mode)
        rail_layout.addWidget(self.rail_mode_switch)

        self.rail_splitter = QSplitter(Qt.Orientation.Vertical, self.nav_rail)
        self.rail_splitter.setHandleWidth(2)

        # TOC + Bookmarks Pane
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

        # Thumbnails List
        self.thumb_list = QListWidget(self.rail_splitter)
        self.thumb_list.setIconSize(QSize(112, 146))
        self.thumb_list.setSpacing(6)
        self.thumb_list.currentRowChanged.connect(self._on_thumb_selected)
        self.rail_splitter.addWidget(self.thumb_list)
        self.rail_splitter.setSizes([250, 260])

        rail_layout.addWidget(self.rail_splitter, 1)
        root.addWidget(self.nav_rail)

        # Outer Backdrop Frame + Annotation Strip + Interactive PdfCanvas
        self.canvas_frame = QFrame(self)
        self.canvas_frame.setObjectName("ViewerCanvasFrame")
        canvas_wrap = QVBoxLayout(self.canvas_frame)
        canvas_wrap.setContentsMargins(0, 0, 0, 0)

        self.backdrop = MotionBackdropWidget(self.canvas_frame, guard=guard, is_viewer_surface=True)
        canvas_wrap.addWidget(self.backdrop)

        backdrop_layout = QVBoxLayout(self.backdrop)
        backdrop_layout.setContentsMargins(6, 6, 6, 6)
        backdrop_layout.setSpacing(6)

        # Compact Annotation & Touch Ink Bar (FlowLayout prevents button clipping)
        self.annot_bar = QFrame(self.backdrop)
        self.annot_bar.setObjectName("CardPanel")
        annot_flow = FlowLayout(self.annot_bar, margin=6, h_spacing=6, v_spacing=4)

        self._tool_btns: dict[str, AdaptiveButton] = {}
        for mode_id, i18n_key in (
            ("select", "viewer.tool.select"),
            ("highlight", "viewer.tool.highlight"),
            ("note", "viewer.tool.note"),
            ("ink", "viewer.tool.ink"),
            ("eraser", "viewer.tool.eraser"),
        ):
            btn = AdaptiveButton(_tv(i18n_key, self._lang), self.annot_bar, role="ModeTab")
            btn.setCheckable(True)
            btn.setChecked(mode_id == "select")
            btn.setMinimumHeight(26)
            btn.setStyleSheet("padding: 3px 9px; font-size: 11px;")
            btn.clicked.connect(lambda _=False, m=mode_id: self._on_annot_tool_clicked(m))
            annot_flow.addWidget(btn)
            self._tool_btns[mode_id] = btn

        self._swatch_btns: list[QPushButton] = []
        for col_hex in CLASSIC_HIGHLIGHT_PALETTE:
            sw = QPushButton("", self.annot_bar)
            sw.setFixedSize(18, 18)
            sw.setStyleSheet(f"background-color: {col_hex}; border: 1px solid #334155; padding: 0px;")
            sw.clicked.connect(lambda _=False, c=col_hex: self._on_color_swatch_clicked(c))
            annot_flow.addWidget(sw)
            self._swatch_btns.append(sw)

        self.touch_write_btn = AdaptiveButton(_tv("viewer.tool.touch_write", self._lang), self.annot_bar, role="ModeTab")
        self.touch_write_btn.setCheckable(True)
        self.touch_write_btn.setMinimumHeight(26)
        self.touch_write_btn.setStyleSheet("padding: 3px 9px; font-size: 11px;")
        self.touch_write_btn.clicked.connect(self._on_touch_write_toggled)
        annot_flow.addWidget(self.touch_write_btn)

        self.undo_annot_btn = AdaptiveButton(_tv("viewer.tool.undo", self._lang), self.annot_bar, role="GhostAction")
        self.undo_annot_btn.setMinimumHeight(26)
        self.undo_annot_btn.setStyleSheet("padding: 3px 9px; font-size: 11px;")
        annot_flow.addWidget(self.undo_annot_btn)

        self.save_annot_btn = AdaptiveButton(_tv("viewer.save", self._lang), self.annot_bar, role="GhostAction")
        self.save_annot_btn.setMinimumHeight(26)
        self.save_annot_btn.setStyleSheet("padding: 3px 9px; font-size: 11px;")
        self.save_annot_btn.clicked.connect(self.save_document)
        annot_flow.addWidget(self.save_annot_btn)

        backdrop_layout.addWidget(self.annot_bar)

        self.scroll = QScrollArea(self.backdrop)
        self.scroll.setWidgetResizable(True)
        self.scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.scroll.setObjectName("TransparentHost")
        self.scroll.viewport().setObjectName("TransparentHost")

        page_host = QWidget(self.scroll)
        page_host.setObjectName("TransparentHost")
        page_host_layout = QVBoxLayout(page_host)
        page_host_layout.setContentsMargins(24, 24, 24, 24)
        page_host_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.pdf_canvas = PdfCanvas(page_host)
        self.pdf_canvas.set_session(self.session, page_index=0)
        self.pdf_canvas.set_rotation_and_zoom(self.rotation, self.zoom)
        self.pdf_canvas.annotations_changed.connect(self.state_changed.emit)
        self.pdf_canvas.lookup_word_requested.connect(self.lookup_word_requested.emit)
        self.pdf_canvas.tool_mode_changed.connect(self._sync_tool_buttons)
        self.pdf_canvas.touch_writing_changed.connect(self._sync_touch_button)
        self.pdf_canvas.zoom_delta_requested.connect(lambda d: self.set_zoom(self.zoom + d))
        self.pdf_canvas.pan_delta_requested.connect(self._pan_scroll_by)
        self.undo_annot_btn.clicked.connect(self.pdf_canvas.undo_last_annotation)
        page_host_layout.addWidget(self.pdf_canvas)

        self.scroll.setWidget(page_host)
        backdrop_layout.addWidget(self.scroll, 1)
        root.addWidget(self.canvas_frame, 1)

        # Load persisted ISO 32000 annotations lazily
        try:
            from ..core.pdf_annotations import load_pdf_annotations

            self.pdf_canvas.load_annotations(load_pdf_annotations(self.pdf_path))
        except Exception:
            pass

        self._populate_toc()
        self._populate_bookmarks()
        self._populate_thumbnails()
        self.set_rail_mode("both")
        self.retranslate(self._lang)
        self.render_current_page()
        self.emit_current_page_words()

    def _on_annot_tool_clicked(self, mode_id: str) -> None:
        self.pdf_canvas.set_tool_mode(mode_id)
        self._sync_tool_buttons(mode_id)

    def _sync_tool_buttons(self, active_mode: str) -> None:
        for mid, btn in self._tool_btns.items():
            btn.blockSignals(True)
            btn.setChecked(mid == active_mode)
            btn.blockSignals(False)

    def _on_color_swatch_clicked(self, col_hex: str) -> None:
        self.pdf_canvas.set_highlight_color(col_hex)
        self.pdf_canvas.set_ink_color(col_hex)

    def _on_touch_write_toggled(self, checked: bool) -> None:
        self.pdf_canvas.set_touch_writing_enabled(bool(checked))

    def _sync_touch_button(self, enabled: bool) -> None:
        self.touch_write_btn.blockSignals(True)
        self.touch_write_btn.setChecked(bool(enabled))
        self.touch_write_btn.blockSignals(False)

    def _pan_scroll_by(self, dx: float, dy: float) -> None:
        hbar = self.scroll.horizontalScrollBar()
        vbar = self.scroll.verticalScrollBar()
        hbar.setValue(hbar.value() - int(round(dx)))
        vbar.setValue(vbar.value() - int(round(dy)))

    def has_unsaved_changes(self) -> bool:
        return self.pdf_canvas.has_unsaved_changes()

    def save_document(self) -> bool:
        from ..core.pdf_annotations import save_pdf_annotations

        annotations = self.pdf_canvas.annotations()
        page_idx = self.current_page
        self.session.close()
        try:
            save_pdf_annotations(self.pdf_path, annotations, create_backup=True, flatten=False)
            self.pdf_canvas.mark_saved()
            saved = True
        except Exception:
            saved = False
        finally:
            self.session = PdfDocumentSession(self.pdf_path)
            self.pdf_canvas.set_session(self.session, page_index=page_idx)
        if saved:
            self.render_current_page()
            self.state_changed.emit()
        return saved

    def save_as_document(self, output_path: Path | None = None, *, flatten: bool = False) -> bool:
        if output_path is None:
            chosen, _ = QFileDialog.getSaveFileName(
                self,
                _tv("viewer.save_as", self._lang),
                str(self.pdf_path),
                "PDF (*.pdf)",
            )
            if not chosen:
                return False
            output_path = Path(chosen)
        from ..core.pdf_annotations import save_pdf_annotations

        try:
            overwrite_current = Path(output_path).resolve() == self.pdf_path.resolve()
        except Exception:
            overwrite_current = False
        page_idx = self.current_page
        if overwrite_current:
            self.session.close()
        try:
            saved = save_pdf_annotations(
                self.pdf_path,
                self.pdf_canvas.annotations(),
                output_path=output_path,
                create_backup=False,
                flatten=flatten,
            )
            if saved.resolve() == self.pdf_path.resolve():
                self.pdf_canvas.mark_saved()
            saved_ok = True
        except Exception:
            saved_ok = False
        finally:
            if overwrite_current:
                self.session = PdfDocumentSession(self.pdf_path)
                self.pdf_canvas.set_session(self.session, page_index=page_idx)
        if saved_ok:
            if overwrite_current:
                self.render_current_page()
            self.state_changed.emit()
        return saved_ok

    def _populate_toc(self) -> None:
        self.toc_tree.clear()
        toc_items = self.session.get_toc()
        if not toc_items:
            self.toc_tree.setVisible(False)
            self.toc_empty_lbl.setVisible(True)
            return

        self.toc_tree.setVisible(True)
        self.toc_empty_lbl.setVisible(False)
        stack: list[tuple[int, QTreeWidgetItem]] = []
        for entry in toc_items:
            node = QTreeWidgetItem([f"{entry.title}   [p.{entry.page_index + 1}]"])
            node.setData(0, Qt.ItemDataRole.UserRole, entry.page_index)
            while stack and stack[-1][0] >= entry.level:
                stack.pop()
            if stack:
                stack[-1][1].addChild(node)
            else:
                self.toc_tree.addTopLevelItem(node)
            stack.append((entry.level, node))
        self.toc_tree.expandAll()

    def _populate_bookmarks(self) -> None:
        self.bm_list.clear()
        for bm in self.bookmarks_store.list_bookmarks(self.pdf_path):
            item = QListWidgetItem(f"{bm.title}   [p.{bm.page_index + 1}]")
            item.setData(Qt.ItemDataRole.UserRole, bm.page_index)
            item.setData(Qt.ItemDataRole.UserRole + 1, bm.bookmark_id)
            self.bm_list.addItem(item)

    def add_bookmark(self, title: str, page_index: int | None = None) -> None:
        target = self.current_page if page_index is None else page_index
        self.bookmarks_store.add_bookmark(self.pdf_path, title, target)
        QTimer.singleShot(0, self._populate_bookmarks)

    def _prompt_add_bookmark(self) -> None:
        default_name = f"Marcador p.{self.current_page + 1}"
        title, ok = QInputDialog.getText(
            self,
            t("viewer.bookmarks_header", self._lang),
            t("viewer.bookmark_prompt", self._lang, page=self.current_page + 1),
            text=default_name,
        )
        if ok and title.strip():
            self.add_bookmark(title.strip(), self.current_page)

    def _remove_selected_bookmark(self) -> None:
        item = self.bm_list.currentItem()
        if item is None:
            return
        bm_id = str(item.data(Qt.ItemDataRole.UserRole + 1) or "")
        if bm_id and self.bookmarks_store.remove_bookmark(self.pdf_path, bm_id):
            QTimer.singleShot(0, self._populate_bookmarks)

    def _on_toc_item_clicked(self, item: QTreeWidgetItem, _col: int) -> None:
        page_idx = item.data(0, Qt.ItemDataRole.UserRole)
        if page_idx is not None:
            self.set_page(int(page_idx))

    def _on_bookmark_clicked(self, item: QListWidgetItem) -> None:
        page_idx = item.data(Qt.ItemDataRole.UserRole)
        if page_idx is not None:
            self.set_page(int(page_idx))

    def set_rail_mode(self, mode: str) -> None:
        clean = mode if mode in ("thumbs", "toc", "both") else "both"
        self._rail_mode = clean
        self.rail_mode_switch.set_current(clean, animate=True)
        self.thumb_list.setVisible(clean in ("thumbs", "both"))
        self.toc_pane.setVisible(clean in ("toc", "both"))
        if clean == "both":
            self.rail_splitter.setSizes([250, 260])

    def rail_mode(self) -> str:
        return self._rail_mode

    def _populate_thumbnails(self) -> None:
        self.thumb_list.clear()
        total = self.session.page_count
        for idx in range(total):
            item = QListWidgetItem(f"{idx + 1}")
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            try:
                thumb = self.session.render_thumbnail(idx, width=104)
                item.setIcon(QIcon(QPixmap.fromImage(thumb)))
            except Exception:
                pass
            self.thumb_list.addItem(item)
        if total > 0:
            self.thumb_list.setCurrentRow(0)

    def _on_thumb_selected(self, row: int) -> None:
        if 0 <= row < self.session.page_count and row != self.current_page:
            self.current_page = row
            self.pdf_canvas.set_session(self.session, page_index=self.current_page)
            self.render_current_page()
            self.emit_current_page_words()
            self.state_changed.emit()

    def toggle_rail(self) -> None:
        self._rail_visible = not self._rail_visible
        self.nav_rail.setVisible(self._rail_visible)

    def set_page(self, page_index: int) -> None:
        if 0 <= page_index < self.session.page_count:
            self.current_page = page_index
            self.pdf_canvas.set_session(self.session, page_index=self.current_page)
            if self.thumb_list.currentRow() != page_index:
                self.thumb_list.setCurrentRow(page_index)
            self.render_current_page()
            self.emit_current_page_words()
            self.state_changed.emit()

    def emit_current_page_words(self) -> None:
        words = self.session.extract_page_words(self.current_page, limit=16)
        self.page_words_ready.emit(words)

    def set_zoom(self, zoom: float) -> None:
        self.zoom = max(0.4, min(3.0, round(float(zoom), 2)))
        self.pdf_canvas.set_rotation_and_zoom(self.rotation, self.zoom)
        self.render_current_page()
        self.state_changed.emit()

    def fit_width(self) -> None:
        avail_w = max(240, self.scroll.viewport().width() - 64)
        self.set_zoom(avail_w / 612.0)

    def rotate_cw(self) -> None:
        self.rotation = (self.rotation + 90) % 360
        self.pdf_canvas.set_rotation_and_zoom(self.rotation, self.zoom)
        self.render_current_page()
        self.state_changed.emit()

    def render_current_page(self) -> None:
        if self.session.page_count <= 0:
            return
        cached = self.session.get_cached_page(self.current_page, scale=self.zoom, rotation=self.rotation)
        if cached is not None:
            self._apply_page_image(cached)
            return

        task = AsyncPageRenderTask(
            self.session,
            self.session.doc_id,
            self.current_page,
            self.zoom,
            self.rotation,
        )
        self._active_tasks.add(task)
        task.signals.rendered.connect(self._on_async_rendered, Qt.ConnectionType.QueuedConnection)
        task.signals.failed.connect(self._on_async_failed, Qt.ConnectionType.QueuedConnection)
        self.backdrop.set_heavy_task_active(True)
        QThreadPool.globalInstance().start(task)

    def _on_async_rendered(self, doc_id: str, page_index: int, scale: float, rotation: int, image: QImage) -> None:
        self._active_tasks = {t for t in self._active_tasks if t.page_index != page_index}
        if not self._active_tasks:
            self.backdrop.set_heavy_task_active(False)
        if doc_id != self.session.doc_id:
            return
        if page_index == self.current_page and abs(scale - self.zoom) < 0.01 and rotation == self.rotation:
            self._apply_page_image(image)

    def _on_async_failed(self, doc_id: str, page_index: int, error: str) -> None:
        del doc_id, page_index, error
        self._active_tasks.clear()
        self.backdrop.set_heavy_task_active(False)

    def _apply_page_image(self, image: QImage) -> None:
        self.pdf_canvas.set_rendered_image(image)

    def retranslate(self, lang: str) -> None:
        self._lang = lang
        self.rail_mode_switch.set_options(
            [
                ("thumbs", t("viewer.rail.thumbs", lang)),
                ("toc", t("viewer.rail.toc", lang)),
                ("both", t("viewer.rail.both", lang)),
            ]
        )
        self.rail_mode_switch.set_current(self._rail_mode, animate=False)
        self.toc_hdr_lbl.setText(t("viewer.toc_header", lang))
        self.toc_empty_lbl.setText(t("viewer.toc_empty", lang))
        self.bm_hdr_lbl.setText(t("viewer.bookmarks_header", lang))
        self.bm_add_btn.setText(t("viewer.bookmark_add", lang))
        self.bm_del_btn.setText(t("viewer.bookmark_del", lang))

        for mid, i18n_key in (
            ("select", "viewer.tool.select"),
            ("highlight", "viewer.tool.highlight"),
            ("note", "viewer.tool.note"),
            ("ink", "viewer.tool.ink"),
            ("eraser", "viewer.tool.eraser"),
        ):
            self._tool_btns[mid].setText(_tv(i18n_key, lang))
        self.touch_write_btn.setText(_tv("viewer.tool.touch_write", lang))
        self.undo_annot_btn.setText(_tv("viewer.tool.undo", lang))
        self.save_annot_btn.setText(_tv("viewer.save", lang))
        self.pdf_canvas.retranslate(lang)

    def apply_profile(self, profile: ThemeProfile) -> None:
        self._profile = profile
        self._mode = "light" if profile.base_mode == "light" else "dark"
        p = get_palette(self._mode)
        self.backdrop.set_profile(profile)
        self.rail_mode_switch.apply_theme(profile.base_mode)
        self.rail_mode_switch.set_animations_enabled(profile.motion_level != "static")
        self.undo_annot_btn.setIcon(load_svg_icon("reset", color=p["icon_color"], size=13))
        self.save_annot_btn.setIcon(load_svg_icon("check", color=p["icon_color"], size=13))
        self.pdf_canvas.apply_theme(self._mode)

    def cleanup(self) -> None:
        self.backdrop.cleanup()
        self.session.close()


ActiveViewerTab = Union[PdfTabPage, MarkdownTabPage]


class ViewerView(QWidget):
    """Multi-tab PDF/Markdown workspace with TOC/Bookmarks, Kindle-style Dictionary, and Fullscreen."""

    backdrop_toggled = Signal(bool)
    fullscreen_requested = Signal()
    document_inspected = Signal(str, int, int)
    dictionary_lookup_recorded = Signal(str)

    def __init__(
        self,
        *,
        bookmarks_store: BookmarksStore | None = None,
        dictionary_engine: DictionaryEngine | None = None,
        guard: MotionPerformanceGuard | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._lang = "es"
        self._mode = "dark"
        self._is_fullscreen = False
        self._bookmarks = bookmarks_store or BookmarksStore()
        self._dict_engine = dictionary_engine or DictionaryEngine()
        self._guard = guard or MotionPerformanceGuard()
        self._profile = ThemeProfile()
        self._is_dict_detached = False
        self._floating_dict_win: _FloatingDictionaryWindow | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 10, 14, 12)
        root.setSpacing(8)

        # Contextual Labeled Toolbar
        bar_frame = QFrame(self)
        bar_frame.setObjectName("CardPanel")
        bar_flow = FlowLayout(bar_frame, margin=8, h_spacing=6, v_spacing=6)

        self.open_btn = AdaptiveButton("", bar_frame, role="PrimaryAction")
        self.open_btn.clicked.connect(self._prompt_open_document)
        bar_flow.addWidget(self.open_btn)

        self.save_btn = AdaptiveButton("", bar_frame, role="GhostAction")
        self.save_btn.clicked.connect(self.save_current)
        bar_flow.addWidget(self.save_btn)

        self.rail_btn = AdaptiveButton("", bar_frame, role="GhostAction")
        self.rail_btn.clicked.connect(self._toggle_rail)
        bar_flow.addWidget(self.rail_btn)

        self.zoom_out_btn = AdaptiveButton("", bar_frame, role="GhostAction")
        self.zoom_out_btn.clicked.connect(lambda: self._step_zoom(-0.15))
        bar_flow.addWidget(self.zoom_out_btn)

        self.zoom_tag = StatusTag("115%", tone="accent", parent=bar_frame)
        bar_flow.addWidget(self.zoom_tag)

        self.zoom_in_btn = AdaptiveButton("", bar_frame, role="GhostAction")
        self.zoom_in_btn.clicked.connect(lambda: self._step_zoom(0.15))
        bar_flow.addWidget(self.zoom_in_btn)

        self.fit_btn = AdaptiveButton("", bar_frame, role="GhostAction")
        self.fit_btn.clicked.connect(self._fit_width)
        bar_flow.addWidget(self.fit_btn)

        self.prev_btn = AdaptiveButton("", bar_frame, role="GhostAction")
        self.prev_btn.clicked.connect(lambda: self._step_page(-1))
        bar_flow.addWidget(self.prev_btn)

        self.page_tag = StatusTag("0 / 0", tone="muted", parent=bar_frame)
        bar_flow.addWidget(self.page_tag)

        self.next_btn = AdaptiveButton("", bar_frame, role="GhostAction")
        self.next_btn.clicked.connect(lambda: self._step_page(1))
        bar_flow.addWidget(self.next_btn)

        self.rotate_btn = AdaptiveButton("", bar_frame, role="GhostAction")
        self.rotate_btn.clicked.connect(self._rotate_page)
        bar_flow.addWidget(self.rotate_btn)

        self.backdrop_btn = AdaptiveButton("", bar_frame, role="ModeTab")
        self.backdrop_btn.setCheckable(True)
        self.backdrop_btn.clicked.connect(self._on_backdrop_btn_clicked)
        bar_flow.addWidget(self.backdrop_btn)

        self.dict_btn = AdaptiveButton("", bar_frame, role="ModeTab")
        self.dict_btn.setCheckable(True)
        self.dict_btn.clicked.connect(self._toggle_dictionary_panel)
        bar_flow.addWidget(self.dict_btn)

        self.fullscreen_btn = AdaptiveButton("", bar_frame, role="GhostAction")
        self.fullscreen_btn.clicked.connect(self.fullscreen_requested.emit)
        bar_flow.addWidget(self.fullscreen_btn)

        root.addWidget(bar_frame)

        # Body: Multi-Tab Document Area + Collapsible/Detachable Right Offline Dictionary Panel
        self._body_row = QHBoxLayout()
        self._body_row.setContentsMargins(0, 0, 0, 0)
        self._body_row.setSpacing(0)

        left_col = QVBoxLayout()
        left_col.setContentsMargins(0, 0, 0, 0)

        self.tabs = QTabWidget(self)
        self.tabs.setDocumentMode(True)
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.tabCloseRequested.connect(self._close_tab)
        self.tabs.currentChanged.connect(lambda _: self._on_active_tab_changed())
        left_col.addWidget(self.tabs, 1)

        self.empty_lbl = QLabel("", self)
        self.empty_lbl.setProperty("tone", "muted")
        self.empty_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_lbl.setVisible(False)
        left_col.addWidget(self.empty_lbl)
        self._body_row.addLayout(left_col, 1)

        self.dict_panel = DictionaryPanel(self._dict_engine, parent=self)
        self.dict_panel.setVisible(False)
        self.dict_panel.word_looked_up.connect(self.dictionary_lookup_recorded.emit)
        self.dict_panel.close_requested.connect(lambda: QTimer.singleShot(0, self._handle_panel_close_requested))
        self.dict_panel.detach_toggled.connect(
            lambda want: QTimer.singleShot(0, lambda w=want: self._handle_panel_detach_toggle(w))
        )
        self._body_row.addWidget(self.dict_panel)

        root.addLayout(self._body_row, 1)

        save_sc = QShortcut(QKeySequence("Ctrl+S"), self)
        save_sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        save_sc.activated.connect(self.save_current)
        self._save_shortcut = save_sc

        self.retranslate("es")

    def open_document(self, doc_path: str | Path, *, title_override: str | None = None) -> bool:
        """Open a local .pdf or .md/.markdown document into ViewerView."""
        ok, _ = validate_local_any_doc(doc_path, check_exists=True)
        if not ok:
            return False
        resolved = Path(doc_path).resolve()
        ext = resolved.suffix.lower()
        if ext == ".pdf":
            return self.open_pdf(resolved, title_override=title_override)
        if ext in (".md", ".markdown"):
            return self.open_markdown(resolved, title_override=title_override)
        return False

    def open_pdf(self, pdf_path: Path, *, title_override: str | None = None) -> bool:
        ok, _ = validate_local_pdf_path(pdf_path, check_exists=True)
        if not ok:
            return False

        resolved = Path(pdf_path).resolve()
        for idx in range(self.tabs.count()):
            tab = self.tabs.widget(idx)
            if isinstance(tab, PdfTabPage) and tab.pdf_path.resolve() == resolved:
                self.tabs.setCurrentIndex(idx)
                self._on_active_tab_changed()
                return True

        try:
            tab = PdfTabPage(resolved, bookmarks_store=self._bookmarks, guard=self._guard, parent=self.tabs)
        except Exception:
            return False
        tab.retranslate(self._lang)
        tab.apply_profile(self._profile)
        tab.state_changed.connect(self._on_tab_state_changed)
        tab.page_words_ready.connect(self._on_tab_page_words)
        tab.lookup_word_requested.connect(self.lookup_in_dictionary)
        label = title_override or resolved.name
        tab.setProperty("base_tab_title", label)
        new_idx = self.tabs.addTab(tab, label)
        self.tabs.setCurrentIndex(new_idx)
        self.tabs.setVisible(True)
        self.empty_lbl.setVisible(False)
        self._on_active_tab_changed()

        sample_text = tab.session.extract_page_text(0)
        toc_count = len(tab.session.get_toc())
        self.document_inspected.emit(sample_text, tab.session.page_count, toc_count)
        return True

    def open_markdown(self, md_path: Path, *, title_override: str | None = None) -> bool:
        ok, _ = validate_local_markdown_path(md_path, check_exists=True)
        if not ok:
            return False

        resolved = Path(md_path).resolve()
        for idx in range(self.tabs.count()):
            tab = self.tabs.widget(idx)
            if isinstance(tab, MarkdownTabPage) and tab.md_path.resolve() == resolved:
                self.tabs.setCurrentIndex(idx)
                self._on_active_tab_changed()
                return True

        try:
            tab = MarkdownTabPage(resolved, bookmarks_store=self._bookmarks, guard=self._guard, parent=self.tabs)
        except Exception:
            return False
        tab.retranslate(self._lang)
        tab.apply_profile(self._profile)
        tab.state_changed.connect(self._on_tab_state_changed)
        tab.page_words_ready.connect(self._on_tab_page_words)
        tab.lookup_word_requested.connect(self.lookup_in_dictionary)
        label = title_override or resolved.name
        tab.setProperty("base_tab_title", label)
        new_idx = self.tabs.addTab(tab, label)
        self.tabs.setCurrentIndex(new_idx)
        self.tabs.setVisible(True)
        self.empty_lbl.setVisible(False)
        self._on_active_tab_changed()

        sample_text = tab.editor_widget.toPlainText()[:1500]
        toc_count = tab.toc_tree.topLevelItemCount()
        self.document_inspected.emit(sample_text, 1, toc_count)
        return True

    def _on_tab_state_changed(self) -> None:
        for idx in range(self.tabs.count()):
            w = self.tabs.widget(idx)
            if isinstance(w, (PdfTabPage, MarkdownTabPage)):
                base = str(w.property("base_tab_title") or (w.pdf_path.name if isinstance(w, PdfTabPage) else w.md_path.name))
                dirty = w.has_unsaved_changes()
                self.tabs.setTabText(idx, f"* {base}" if dirty else base)
        self._sync_toolbar_state()

    def _on_tab_page_words(self, words: list[str]) -> None:
        sender = self.sender()
        if sender is self._current_tab():
            self.dict_panel.set_page_words(words)

    def _on_active_tab_changed(self) -> None:
        self._sync_toolbar_state()
        tab = self._current_tab()
        if isinstance(tab, PdfTabPage):
            tab.emit_current_page_words()
        elif isinstance(tab, MarkdownTabPage):
            tab.emit_document_words()

    def _prompt_open_document(self) -> None:
        chosen, _ = QFileDialog.getOpenFileName(
            self,
            t("viewer.open", self._lang),
            "",
            "Documentos (*.pdf *.md *.markdown);;PDF (*.pdf);;Markdown (*.md *.markdown)",
        )
        if chosen:
            self.open_document(Path(chosen))

    def _prompt_open_pdf(self) -> None:
        self._prompt_open_document()

    def _current_tab(self) -> ActiveViewerTab | None:
        w = self.tabs.currentWidget()
        return w if isinstance(w, (PdfTabPage, MarkdownTabPage)) else None

    def current_tab(self) -> ActiveViewerTab | None:
        return self._current_tab()

    def has_unsaved_changes(self) -> bool:
        for idx in range(self.tabs.count()):
            w = self.tabs.widget(idx)
            if isinstance(w, (PdfTabPage, MarkdownTabPage)) and w.has_unsaved_changes():
                return True
        return False

    def save_current(self) -> bool:
        tab = self._current_tab()
        if tab is not None and hasattr(tab, "save_document"):
            ok = bool(tab.save_document())
            self._on_tab_state_changed()
            return ok
        return False

    def _close_tab(self, index: int) -> None:
        w = self.tabs.widget(index)
        self.tabs.removeTab(index)
        if isinstance(w, (PdfTabPage, MarkdownTabPage)):
            w.cleanup()
            w.deleteLater()
        if self.tabs.count() == 0:
            self.tabs.setVisible(False)
            self.empty_lbl.setVisible(True)
        self._sync_toolbar_state()

    def _toggle_rail(self) -> None:
        tab = self._current_tab()
        if tab is not None:
            tab.toggle_rail()

    def _step_zoom(self, delta: float) -> None:
        tab = self._current_tab()
        if isinstance(tab, PdfTabPage):
            tab.set_zoom(tab.zoom + delta)

    def _fit_width(self) -> None:
        tab = self._current_tab()
        if isinstance(tab, PdfTabPage):
            tab.fit_width()

    def _step_page(self, delta: int) -> None:
        tab = self._current_tab()
        if isinstance(tab, PdfTabPage):
            tab.set_page(tab.current_page + delta)

    def _rotate_page(self) -> None:
        tab = self._current_tab()
        if isinstance(tab, PdfTabPage):
            tab.rotate_cw()

    def _on_backdrop_btn_clicked(self, checked: bool) -> None:
        self.backdrop_toggled.emit(bool(checked))

    # --- Kindle-style Offline Dictionary Dock / Detach / Close Lifecycle ---

    def is_dictionary_detached(self) -> bool:
        return self._is_dict_detached

    def _ensure_floating_window(self) -> _FloatingDictionaryWindow:
        if self._floating_dict_win is None or not shiboken6.isValid(self._floating_dict_win):
            win = _FloatingDictionaryWindow(parent=self)
            win.setWindowTitle(_tv("dict.floating_title", self._lang))
            win.closed_by_user.connect(self.close_detached_dictionary)
            win.apply_theme(self._mode)
            self._floating_dict_win = win
        return self._floating_dict_win

    def _sync_dict_btn(self, checked: bool) -> None:
        self.dict_btn.blockSignals(True)
        self.dict_btn.setChecked(bool(checked))
        self.dict_btn.blockSignals(False)

    def set_dictionary_visible(self, visible: bool) -> None:
        if not shiboken6.isValid(self.dict_panel):
            return
        want = bool(visible)
        self._sync_dict_btn(want)
        if want:
            if self._is_dict_detached and self._floating_dict_win is not None and shiboken6.isValid(self._floating_dict_win):
                self._floating_dict_win.attach_panel(self.dict_panel)
                self.dict_panel.set_detached_state(True)
                self.dict_panel.setVisible(True)
                self._floating_dict_win.show()
                self._floating_dict_win.raise_()
            else:
                if self.dict_panel.parentWidget() is not self:
                    self.dict_panel.setParent(self)
                if self._body_row.indexOf(self.dict_panel) < 0:
                    self._body_row.addWidget(self.dict_panel)
                self.dict_panel.set_detached_state(False)
                self.dict_panel.setVisible(True)
            tab = self._current_tab()
            if isinstance(tab, PdfTabPage):
                tab.emit_current_page_words()
            elif isinstance(tab, MarkdownTabPage):
                tab.emit_document_words()
        else:
            if self._is_dict_detached and self._floating_dict_win is not None and shiboken6.isValid(self._floating_dict_win):
                self._floating_dict_win.hide()
            self.dict_panel.setVisible(False)

    def _toggle_dictionary_panel(self, checked: bool) -> None:
        self.set_dictionary_visible(bool(checked))

    def lookup_in_dictionary(self, word: str, lang: str | None = None) -> None:
        clean = (word or "").strip()
        if not clean or not shiboken6.isValid(self.dict_panel):
            return
        if lang:
            self.dict_panel.set_dict_lang(lang)
        self.set_dictionary_visible(True)
        self.dict_panel.search_input.blockSignals(True)
        self.dict_panel.search_input.setText(clean)
        self.dict_panel.search_input.blockSignals(False)
        self.dict_panel.lookup_word(clean, record_event=True)

    def detach_dictionary(self) -> None:
        if not shiboken6.isValid(self.dict_panel):
            return
        win = self._ensure_floating_window()
        if not self._is_dict_detached:
            self._body_row.removeWidget(self.dict_panel)
            win.attach_panel(self.dict_panel)
            self._is_dict_detached = True
        self.dict_panel.set_detached_state(True)
        self.dict_panel.setVisible(True)
        win.setWindowTitle(_tv("dict.floating_title", self._lang))
        win.apply_theme(self._mode)
        win.show()
        win.raise_()
        self._sync_dict_btn(True)

    def dock_dictionary(self) -> None:
        if not shiboken6.isValid(self.dict_panel):
            return
        if self._floating_dict_win is not None and shiboken6.isValid(self._floating_dict_win):
            self._floating_dict_win.detach_panel(self.dict_panel)
            self._floating_dict_win.hide()
        if self.dict_panel.parentWidget() is not self:
            self.dict_panel.setParent(self)
        if self._body_row.indexOf(self.dict_panel) < 0:
            self._body_row.addWidget(self.dict_panel)
        self._is_dict_detached = False
        self.dict_panel.set_detached_state(False)
        self.dict_panel.setVisible(True)
        self._sync_dict_btn(True)

    def close_detached_dictionary(self) -> None:
        if not shiboken6.isValid(self.dict_panel):
            return
        if self._floating_dict_win is not None and shiboken6.isValid(self._floating_dict_win):
            self._floating_dict_win.detach_panel(self.dict_panel)
            self._floating_dict_win.hide()
        if self.dict_panel.parentWidget() is not self:
            self.dict_panel.setParent(self)
        if self._body_row.indexOf(self.dict_panel) < 0:
            self._body_row.addWidget(self.dict_panel)
        self._is_dict_detached = False
        self.dict_panel.set_detached_state(False)
        self.set_dictionary_visible(False)

    def _handle_panel_close_requested(self) -> None:
        if self._is_dict_detached:
            self.close_detached_dictionary()
        else:
            self.set_dictionary_visible(False)

    def _handle_panel_detach_toggle(self, want_detach: bool) -> None:
        if want_detach:
            self.detach_dictionary()
        else:
            self.dock_dictionary()

    def set_fullscreen_state(self, is_fullscreen: bool) -> None:
        self._is_fullscreen = bool(is_fullscreen)
        p = get_palette(self._mode)
        key = "viewer.exit_fullscreen" if self._is_fullscreen else "viewer.fullscreen"
        icon = "exit_fullscreen" if self._is_fullscreen else "fullscreen"
        self.fullscreen_btn.setText(t(key, self._lang))
        self.fullscreen_btn.setIcon(load_svg_icon(icon, color=p["icon_color"], size=15))

    def _sync_toolbar_state(self) -> None:
        tab = self._current_tab()
        if tab is None:
            self.zoom_tag.setText("—")
            self.page_tag.setText("0 / 0")
            self.save_btn.setEnabled(False)
            for b in (self.zoom_out_btn, self.zoom_in_btn, self.fit_btn, self.prev_btn, self.next_btn, self.rotate_btn):
                b.setEnabled(False)
            return
        if isinstance(tab, PdfTabPage):
            self.zoom_tag.setText(f"{int(round(tab.zoom * 100))}%")
            self.page_tag.setText(f"{tab.current_page + 1} / {tab.session.page_count}")
            self.save_btn.setEnabled(True)
            for b in (self.zoom_out_btn, self.zoom_in_btn, self.fit_btn, self.prev_btn, self.next_btn, self.rotate_btn):
                b.setEnabled(True)
        else:
            self.zoom_tag.setText("100%")
            self.page_tag.setText("MD")
            self.save_btn.setEnabled(True)
            for b in (self.zoom_out_btn, self.zoom_in_btn, self.fit_btn, self.prev_btn, self.next_btn, self.rotate_btn):
                b.setEnabled(False)

    def retranslate(self, lang: str) -> None:
        self._lang = lang
        self.open_btn.setText(t("viewer.open", lang))
        self.save_btn.setText(_tv("viewer.save", lang))
        self.rail_btn.setText(t("viewer.thumbnails", lang))
        self.zoom_out_btn.setText(t("viewer.zoom_out", lang))
        self.zoom_in_btn.setText(t("viewer.zoom_in", lang))
        self.fit_btn.setText(t("viewer.fit_width", lang))
        self.prev_btn.setText(t("viewer.prev", lang))
        self.next_btn.setText(t("viewer.next", lang))
        self.rotate_btn.setText(t("viewer.rotate", lang))
        self.backdrop_btn.setText(t("viewer.backdrop_toggle", lang))
        self.dict_btn.setText(t("viewer.dictionary", lang))
        self.set_fullscreen_state(self._is_fullscreen)
        self.empty_lbl.setText(_tv("viewer.empty_dual", lang))
        self.dict_panel.retranslate(lang)
        if self._floating_dict_win is not None and shiboken6.isValid(self._floating_dict_win):
            self._floating_dict_win.setWindowTitle(_tv("dict.floating_title", lang))
        for idx in range(self.tabs.count()):
            tab = self.tabs.widget(idx)
            if isinstance(tab, (PdfTabPage, MarkdownTabPage)):
                tab.retranslate(lang)

    def apply_profile(self, profile: ThemeProfile) -> None:
        self._profile = profile
        self._mode = profile.base_mode
        p = get_palette(self._mode)

        self.backdrop_btn.blockSignals(True)
        self.backdrop_btn.setChecked(profile.apply_to_viewer_backdrop)
        self.backdrop_btn.blockSignals(False)

        self.open_btn.setIcon(load_svg_icon("open", color=p["primary_fg"], size=15))
        self.save_btn.setIcon(load_svg_icon("check", color=p["icon_color"], size=15))
        self.rail_btn.setIcon(load_svg_icon("sidebar_toggle", color=p["icon_color"], size=15))
        self.zoom_out_btn.setIcon(load_svg_icon("zoom_out", color=p["icon_color"], size=15))
        self.zoom_in_btn.setIcon(load_svg_icon("zoom_in", color=p["icon_color"], size=15))
        self.fit_btn.setIcon(load_svg_icon("fit_width", color=p["icon_color"], size=15))
        self.prev_btn.setIcon(load_svg_icon("prev", color=p["icon_color"], size=15))
        self.next_btn.setIcon(load_svg_icon("next", color=p["icon_color"], size=15))
        self.rotate_btn.setIcon(load_svg_icon("rotate", color=p["icon_color"], size=15))
        self.backdrop_btn.setIcon(load_svg_icon("palette", color=p["icon_color"], size=15))
        self.dict_btn.setIcon(load_svg_icon("book", color=p["icon_color"], size=15))
        self.set_fullscreen_state(self._is_fullscreen)

        self.zoom_tag.apply_theme(self._mode)
        self.page_tag.apply_theme(self._mode)
        self.dict_panel.apply_theme(self._mode)
        if self._floating_dict_win is not None and shiboken6.isValid(self._floating_dict_win):
            self._floating_dict_win.apply_theme(self._mode)

        for idx in range(self.tabs.count()):
            tab = self.tabs.widget(idx)
            if isinstance(tab, (PdfTabPage, MarkdownTabPage)):
                tab.apply_profile(profile)

    def cleanup(self) -> None:
        if self._floating_dict_win is not None and shiboken6.isValid(self._floating_dict_win):
            self._floating_dict_win.hide()
            self._floating_dict_win.close()
        for idx in range(self.tabs.count()):
            tab = self.tabs.widget(idx)
            if isinstance(tab, (PdfTabPage, MarkdownTabPage)):
                tab.cleanup()
