"""Multi-tab PDF/Markdown Adobe-style viewer with continuous scroll, independent/detachable rails, and right tool rail."""

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
    QLineEdit,
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
from ..core.pdf_engine import AsyncPageRenderTask, PdfDocumentSession, SearchHit
from ..core.win32_bridge import apply_trodai_window_chrome
from ..theme.motion_engine import MotionBackdropWidget, MotionPerformanceGuard
from ..theme.palette import UI_FONT_FAMILY, get_palette
from ..theme.theme_profile import ThemeProfile
from .dictionary_panel import DictionaryPanel
from .md_tab_page import MarkdownTabPage
from .notes_panel import NotesPanel
from .pdf_canvas import CLASSIC_HIGHLIGHT_PALETTE, PdfCanvas, SharedAnnotationState
from .presentation_view import PresentationWindow
from .print_dialog import PrintPdfDialog
from .share_dialog import ShareDocumentDialog
from .svg_icons import load_svg_icon
from .widgets import AdaptiveButton, FlowLayout, StatusTag

_LOCAL_VIEWER_TEXTS: dict[str, dict[str, str]] = {
    "es": {
        "viewer.save": "Guardar",
        "viewer.save_as": "Guardar como",
        "viewer.find": "Buscar en documento (Ctrl+F)",
        "viewer.find_placeholder": "Buscar texto en el documento (Ctrl+F)...",
        "viewer.print": "Imprimir PDF (Ctrl+P)",
        "viewer.present": "Modo Presentacion (F5 / Ctrl+L)",
        "viewer.share": "Compartir PDF como adjunto",
        "viewer.toggle_notes_vis": "Mostrar / ocultar notas en el documento (Ctrl+Shift+N)",
        "viewer.empty_dual": "Abre cualquier PDF o Markdown local o suelta un archivo aqui para leerlo.",
        "viewer.tool.select": "Seleccionar",
        "viewer.tool.highlight": "Resaltar texto",
        "viewer.tool.free_highlight": "Resaltado libre",
        "viewer.tool.note": "Nota",
        "viewer.tool.ink": "Tinta",
        "viewer.tool.eraser": "Borrador",
        "viewer.tool.touch_write": "Escribir tactil",
        "viewer.tool.undo": "Deshacer",
        "viewer.rail.ink_tools": "Tinta y anotacion",
        "viewer.rail.notes": "Notas",
        "viewer.rail.ai": "Preguntar IA",
        "viewer.rail.settings": "Configuracion",
        "viewer.rail.detach": "Desacoplar panel",
        "viewer.rail.dock": "Acoplar panel",
        "viewer.rail.close": "Cerrar panel",
        "viewer.rail.thumbs_title": "Miniaturas",
        "viewer.rail.toc_title": "Indice y marcadores",
        "dict.floating_title": "Diccionario · PDF/MD Lector",
    },
    "en": {
        "viewer.save": "Save",
        "viewer.save_as": "Save as",
        "viewer.find": "Find in document (Ctrl+F)",
        "viewer.find_placeholder": "Find text in document (Ctrl+F)...",
        "viewer.print": "Print PDF (Ctrl+P)",
        "viewer.present": "Presentation Mode (F5 / Ctrl+L)",
        "viewer.share": "Share PDF as attachment",
        "viewer.toggle_notes_vis": "Show / hide notes on page (Ctrl+Shift+N)",
        "viewer.empty_dual": "Open any local PDF or Markdown file or drop a file here to read.",
        "viewer.tool.select": "Select",
        "viewer.tool.highlight": "Highlight text",
        "viewer.tool.free_highlight": "Free highlight",
        "viewer.tool.note": "Note",
        "viewer.tool.ink": "Ink",
        "viewer.tool.eraser": "Eraser",
        "viewer.tool.touch_write": "Touch writing",
        "viewer.tool.undo": "Undo",
        "viewer.rail.ink_tools": "Ink & Drawing",
        "viewer.rail.notes": "Notes",
        "viewer.rail.ai": "Ask AI",
        "viewer.rail.settings": "Settings",
        "viewer.rail.detach": "Detach panel",
        "viewer.rail.dock": "Dock panel",
        "viewer.rail.close": "Close panel",
        "viewer.rail.thumbs_title": "Thumbnails",
        "viewer.rail.toc_title": "Index & Bookmarks",
        "dict.floating_title": "Dictionary · PDF/MD Lector",
    },
    "pt": {
        "viewer.save": "Salvar",
        "viewer.save_as": "Salvar como",
        "viewer.find": "Buscar no documento (Ctrl+F)",
        "viewer.find_placeholder": "Buscar texto no documento (Ctrl+F)...",
        "viewer.print": "Imprimir PDF (Ctrl+P)",
        "viewer.present": "Modo Apresentacao (F5 / Ctrl+L)",
        "viewer.share": "Compartilhar PDF como anexo",
        "viewer.toggle_notes_vis": "Mostrar / ocultar notas na pagina (Ctrl+Shift+N)",
        "viewer.empty_dual": "Abra qualquer PDF ou Markdown local ou solte um arquivo aqui para ler.",
        "viewer.tool.select": "Selecionar",
        "viewer.tool.highlight": "Realcar texto",
        "viewer.tool.free_highlight": "Realce livre",
        "viewer.tool.note": "Nota",
        "viewer.tool.ink": "Tinta",
        "viewer.tool.eraser": "Borracha",
        "viewer.tool.touch_write": "Escrita tatil",
        "viewer.tool.undo": "Desfazer",
        "viewer.rail.ink_tools": "Tinta e desenho",
        "viewer.rail.notes": "Notas",
        "viewer.rail.ai": "Perguntar IA",
        "viewer.rail.settings": "Configuracoes",
        "viewer.rail.detach": "Desacoplar painel",
        "viewer.rail.dock": "Acoplar painel",
        "viewer.rail.close": "Fechar painel",
        "viewer.rail.thumbs_title": "Miniaturas",
        "viewer.rail.toc_title": "Indice e marcadores",
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


class _FloatingPaneWindow(QWidget):
    """Reusable non-deleting floating window for detachable viewer panels (Dictionary / Thumbnails / Index)."""

    closed_by_user = Signal()

    def __init__(self, *, width: int = 300, height: int = 480, parent: QWidget | None = None) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.setObjectName("RootCanvas")
        self.setMinimumSize(250, 320)
        self.resize(width, height)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)

    def attach_panel(self, panel: QWidget) -> None:
        if panel.parentWidget() is not self:
            panel.setParent(self)
        if self._layout.indexOf(panel) < 0:
            self._layout.addWidget(panel)

    def detach_panel(self, panel: QWidget) -> None:
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
    """Individual PDF tab with independent closable/detachable Miniaturas & Índice panes and continuous scroll."""

    state_changed = Signal()
    page_words_ready = Signal(list)
    lookup_word_requested = Signal(str)
    rail_state_changed = Signal()

    def __init__(
        self,
        pdf_path: Path,
        *,
        bookmarks_store: BookmarksStore,
        guard: MotionPerformanceGuard | None = None,
        continuous_scroll: bool = True,
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
        self._applied_theme_mode = ""
        self._continuous_scroll = bool(continuous_scroll)
        self._rail_visible = False
        self._rail_mode = "thumbs"
        self._thumbs_open = False
        self._toc_open = False
        self._thumbs_detached = False
        self._toc_detached = False
        self._floating_thumb_win: _FloatingPaneWindow | None = None
        self._floating_toc_win: _FloatingPaneWindow | None = None
        self._active_tasks: dict[int, AsyncPageRenderTask] = {}
        self._rendered_signatures: dict[int, tuple[float, int]] = {}
        self._profile = ThemeProfile()
        self._shared_ann_store = SharedAnnotationState()
        self.shared_annotations = self._shared_ann_store
        self._programmatic_scroll = False

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Left Navigation Rail (Starts hidden for Adobe-style reading space; holds independent Miniaturas & Índice)
        self.nav_rail = QFrame(self)
        self.nav_rail.setObjectName("ThumbRail")
        self.nav_rail.setFixedWidth(248)
        self.nav_rail.setVisible(False)
        rail_layout = QVBoxLayout(self.nav_rail)
        rail_layout.setContentsMargins(6, 6, 6, 6)
        rail_layout.setSpacing(6)

        self.rail_splitter = QSplitter(Qt.Orientation.Vertical, self.nav_rail)
        self.rail_splitter.setHandleWidth(2)

        # 1. TOC + Bookmarks Pane (independent header with Detach + Close X)
        self.toc_pane = QWidget(self.rail_splitter)
        self.toc_pane.setObjectName("TransparentHost")
        toc_layout = QVBoxLayout(self.toc_pane)
        toc_layout.setContentsMargins(0, 0, 0, 0)
        toc_layout.setSpacing(6)

        toc_hdr_bar = QFrame(self.toc_pane)
        toc_hdr_bar.setObjectName("RailPaneHeader")
        th_row = QHBoxLayout(toc_hdr_bar)
        th_row.setContentsMargins(6, 4, 4, 4)
        th_row.setSpacing(4)
        self.toc_hdr_lbl = QLabel("", toc_hdr_bar)
        th_f = QFont(UI_FONT_FAMILY, 9, QFont.Weight.Bold)
        self.toc_hdr_lbl.setFont(th_f)
        th_row.addWidget(self.toc_hdr_lbl, 1)

        self.toc_detach_btn = QPushButton("", toc_hdr_bar)
        self.toc_detach_btn.setProperty("role", "GhostAction")
        self.toc_detach_btn.setProperty("iconOnly", "true")
        self.toc_detach_btn.setFixedSize(24, 24)
        self.toc_detach_btn.setIconSize(QSize(14, 14))
        self.toc_detach_btn.clicked.connect(lambda: QTimer.singleShot(0, self._toggle_toc_detach))
        th_row.addWidget(self.toc_detach_btn)

        self.toc_close_btn = QPushButton("", toc_hdr_bar)
        self.toc_close_btn.setProperty("role", "GhostAction")
        self.toc_close_btn.setProperty("iconOnly", "true")
        self.toc_close_btn.setFixedSize(24, 24)
        self.toc_close_btn.setIconSize(QSize(14, 14))
        self.toc_close_btn.clicked.connect(lambda: QTimer.singleShot(0, lambda: self.set_toc_open(False)))
        th_row.addWidget(self.toc_close_btn)
        toc_layout.addWidget(toc_hdr_bar)

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

        # 2. Thumbnails Pane (independent header with Detach + Close X)
        self.thumb_pane = QWidget(self.rail_splitter)
        self.thumb_pane.setObjectName("TransparentHost")
        thumb_layout = QVBoxLayout(self.thumb_pane)
        thumb_layout.setContentsMargins(0, 0, 0, 0)
        thumb_layout.setSpacing(6)

        thumb_hdr_bar = QFrame(self.thumb_pane)
        thumb_hdr_bar.setObjectName("RailPaneHeader")
        tb_row = QHBoxLayout(thumb_hdr_bar)
        tb_row.setContentsMargins(6, 4, 4, 4)
        tb_row.setSpacing(4)
        self.thumb_hdr_lbl = QLabel("", thumb_hdr_bar)
        self.thumb_hdr_lbl.setFont(th_f)
        tb_row.addWidget(self.thumb_hdr_lbl, 1)

        self.thumb_detach_btn = QPushButton("", thumb_hdr_bar)
        self.thumb_detach_btn.setProperty("role", "GhostAction")
        self.thumb_detach_btn.setProperty("iconOnly", "true")
        self.thumb_detach_btn.setFixedSize(24, 24)
        self.thumb_detach_btn.setIconSize(QSize(14, 14))
        self.thumb_detach_btn.clicked.connect(lambda: QTimer.singleShot(0, self._toggle_thumbs_detach))
        tb_row.addWidget(self.thumb_detach_btn)

        self.thumb_close_btn = QPushButton("", thumb_hdr_bar)
        self.thumb_close_btn.setProperty("role", "GhostAction")
        self.thumb_close_btn.setProperty("iconOnly", "true")
        self.thumb_close_btn.setFixedSize(24, 24)
        self.thumb_close_btn.setIconSize(QSize(14, 14))
        self.thumb_close_btn.clicked.connect(lambda: QTimer.singleShot(0, lambda: self.set_thumbs_open(False)))
        tb_row.addWidget(self.thumb_close_btn)
        thumb_layout.addWidget(thumb_hdr_bar)

        self.thumb_list = QListWidget(self.thumb_pane)
        self.thumb_list.setIconSize(QSize(112, 146))
        self.thumb_list.setSpacing(6)
        self.thumb_list.currentRowChanged.connect(self._on_thumb_selected)
        thumb_layout.addWidget(self.thumb_list, 1)
        self.rail_splitter.addWidget(self.thumb_pane)
        self.rail_splitter.setSizes([250, 260])

        rail_layout.addWidget(self.rail_splitter, 1)
        root.addWidget(self.nav_rail)

        # Document Backdrop + Contextual Ink Bar (HIDDEN by default!) + Scrollable Continuous Multi-Page Stack
        self.canvas_frame = QFrame(self)
        self.canvas_frame.setObjectName("ViewerCanvasFrame")
        canvas_wrap = QVBoxLayout(self.canvas_frame)
        canvas_wrap.setContentsMargins(0, 0, 0, 0)

        self.backdrop = MotionBackdropWidget(self.canvas_frame, guard=guard, is_viewer_surface=True)
        canvas_wrap.addWidget(self.backdrop)

        backdrop_layout = QVBoxLayout(self.backdrop)
        backdrop_layout.setContentsMargins(6, 6, 6, 6)
        backdrop_layout.setSpacing(6)

        # Ink / Touch Writing Bar (hidden unless user explicitly enables from right rail!)
        self.annot_bar = QFrame(self.backdrop)
        self.annot_bar.setObjectName("CardPanel")
        self.annot_bar.setVisible(False)
        annot_flow = FlowLayout(self.annot_bar, margin=6, h_spacing=6, v_spacing=4)

        self._tool_btns: dict[str, AdaptiveButton] = {}
        for mode_id, i18n_key in (
            ("select", "viewer.tool.select"),
            ("highlight", "viewer.tool.highlight"),
            ("free_highlight", "viewer.tool.free_highlight"),
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
        self.undo_annot_btn.clicked.connect(self._undo_last_annotation)
        annot_flow.addWidget(self.undo_annot_btn)

        self.save_annot_btn = AdaptiveButton(_tv("viewer.save", self._lang), self.annot_bar, role="GhostAction")
        self.save_annot_btn.setMinimumHeight(26)
        self.save_annot_btn.setStyleSheet("padding: 3px 9px; font-size: 11px;")
        self.save_annot_btn.clicked.connect(self.save_document)
        annot_flow.addWidget(self.save_annot_btn)

        self.close_annot_btn = QPushButton("", self.annot_bar)
        self.close_annot_btn.setProperty("role", "GhostAction")
        self.close_annot_btn.setFixedSize(26, 26)
        self.close_annot_btn.setStyleSheet("padding: 2px;")
        self.close_annot_btn.clicked.connect(lambda: self.set_annotation_bar_visible(False))
        annot_flow.addWidget(self.close_annot_btn)

        backdrop_layout.addWidget(self.annot_bar)

        self.scroll = QScrollArea(self.backdrop)
        self.scroll.setWidgetResizable(True)
        self.scroll.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        self.scroll.setObjectName("TransparentHost")
        self.scroll.viewport().setObjectName("TransparentHost")

        self.page_host = QWidget(self.scroll)
        self.page_host.setObjectName("TransparentHost")
        self.page_host_layout = QVBoxLayout(self.page_host)
        self.page_host_layout.setContentsMargins(20, 18, 20, 24)
        self.page_host_layout.setSpacing(16)
        self.page_host_layout.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)

        self._page_canvases: list[PdfCanvas] = []
        self._rebuild_page_canvases()

        self.scroll.setWidget(self.page_host)
        self.scroll.verticalScrollBar().valueChanged.connect(self._on_scroll_changed)
        self._scroll_render_timer = QTimer(self)
        self._scroll_render_timer.setSingleShot(True)
        self._scroll_render_timer.setInterval(25)
        self._scroll_render_timer.timeout.connect(self.render_visible_pages)

        backdrop_layout.addWidget(self.scroll, 1)
        root.addWidget(self.canvas_frame, 1)

        # Load persisted ISO 32000 annotations into shared store
        try:
            from ..core.pdf_annotations import load_pdf_annotations

            self.pdf_canvas.load_annotations(load_pdf_annotations(self.pdf_path))
        except Exception:
            pass

        self._populate_toc()
        self._populate_bookmarks()
        self._populate_thumbnails()
        self._sync_pane_visibility()
        self.retranslate(self._lang)
        self.render_visible_pages()
        self.emit_current_page_words()

    def _rebuild_page_canvases(self) -> None:
        while self._page_canvases:
            c = self._page_canvases.pop()
            self.page_host_layout.removeWidget(c)
            c.hide()
            c.deleteLater()

        total = max(1, self.session.page_count)
        for idx in range(total):
            c = PdfCanvas(self.page_host, shared_state=self._shared_ann_store)
            c.set_session(self.session, page_index=idx)
            c.set_rotation_and_zoom(self.rotation, self.zoom)
            c.annotations_changed.connect(self.state_changed.emit)
            c.lookup_word_requested.connect(self.lookup_word_requested.emit)
            c.tool_mode_changed.connect(self._on_canvas_tool_mode_changed)
            c.touch_writing_changed.connect(self._on_canvas_touch_changed)
            c.zoom_delta_requested.connect(lambda d: self.set_zoom(self.zoom + (d * max(1.0, self.zoom * 0.55))))
            c.pan_delta_requested.connect(self._pan_scroll_by)
            if not self._continuous_scroll:
                c.setVisible(idx == self.current_page)
            self.page_host_layout.addWidget(c, 0, Qt.AlignmentFlag.AlignHCenter)
            self._page_canvases.append(c)

        self.pdf_canvas: PdfCanvas = self._page_canvases[min(self.current_page, len(self._page_canvases) - 1)]

    def _on_canvas_tool_mode_changed(self, active_mode: str) -> None:
        self._sync_tool_buttons(active_mode)
        for c in self._page_canvases:
            if c.tool_mode() != active_mode:
                c.blockSignals(True)
                c.set_tool_mode(active_mode)
                c.blockSignals(False)

    def _on_canvas_touch_changed(self, enabled: bool) -> None:
        self._sync_touch_button(enabled)
        for c in self._page_canvases:
            if c.touch_writing_enabled != enabled:
                c.blockSignals(True)
                c.set_touch_writing_enabled(enabled)
                c.blockSignals(False)

    def _undo_last_annotation(self) -> None:
        self.pdf_canvas.undo_last_annotation()

    def _on_annot_tool_clicked(self, mode_id: str) -> None:
        for c in self._page_canvases:
            c.set_tool_mode(mode_id)
        self._sync_tool_buttons(mode_id)

    def _sync_tool_buttons(self, active_mode: str) -> None:
        for mid, btn in self._tool_btns.items():
            btn.blockSignals(True)
            btn.setChecked(mid == active_mode)
            btn.blockSignals(False)

    def _on_color_swatch_clicked(self, col_hex: str) -> None:
        for c in self._page_canvases:
            c.set_highlight_color(col_hex)
            c.set_ink_color(col_hex)

    def _on_touch_write_toggled(self, checked: bool) -> None:
        for c in self._page_canvases:
            c.set_touch_writing_enabled(bool(checked))

    def _sync_touch_button(self, enabled: bool) -> None:
        self.touch_write_btn.blockSignals(True)
        self.touch_write_btn.setChecked(bool(enabled))
        self.touch_write_btn.blockSignals(False)

    def set_annotation_bar_visible(self, visible: bool) -> None:
        want = bool(visible)
        self.annot_bar.setVisible(want)
        if not want and self.pdf_canvas.tool_mode() in ("ink", "eraser"):
            self._on_annot_tool_clicked("select")
        self.rail_state_changed.emit()

    def is_annotation_bar_visible(self) -> bool:
        return self.annot_bar.isVisible()

    def set_continuous_scroll(self, enabled: bool) -> None:
        want = bool(enabled)
        if want == self._continuous_scroll:
            return
        self._continuous_scroll = want
        for idx, c in enumerate(self._page_canvases):
            c.setVisible(True if want else (idx == self.current_page))
        QTimer.singleShot(0, lambda: self.set_page(self.current_page))

    def _pan_scroll_by(self, dx: float, dy: float) -> None:
        hbar = self.scroll.horizontalScrollBar()
        vbar = self.scroll.verticalScrollBar()
        hbar.setValue(hbar.value() - int(round(dx)))
        vbar.setValue(vbar.value() - int(round(dy)))

    def has_unsaved_changes(self) -> bool:
        return bool(self._shared_ann_store.dirty)

    def save_document(self) -> bool:
        from ..core.pdf_annotations import save_pdf_annotations

        annotations = list(self._shared_ann_store.records)
        self.session.close()
        try:
            save_pdf_annotations(self.pdf_path, annotations, create_backup=True, flatten=False)
            self._shared_ann_store.dirty = False
            saved = True
        except Exception:
            saved = False
        finally:
            self.session = PdfDocumentSession(self.pdf_path)
            for idx, c in enumerate(self._page_canvases):
                c.set_session(self.session, page_index=idx)
        if saved:
            self._rendered_signatures.clear()
            self.render_visible_pages()
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
        if overwrite_current:
            self.session.close()
        try:
            saved = save_pdf_annotations(
                self.pdf_path,
                list(self._shared_ann_store.records),
                output_path=output_path,
                create_backup=False,
                flatten=flatten,
            )
            if saved.resolve() == self.pdf_path.resolve():
                self._shared_ann_store.dirty = False
            saved_ok = True
        except Exception:
            saved_ok = False
        finally:
            if overwrite_current:
                self.session = PdfDocumentSession(self.pdf_path)
                for idx, c in enumerate(self._page_canvases):
                    c.set_session(self.session, page_index=idx)
        if saved_ok:
            if overwrite_current:
                self._rendered_signatures.clear()
                self.render_visible_pages()
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

    # --- Independent Closable & Detachable Miniaturas / Indice Panes ---

    def is_thumbs_open(self) -> bool:
        return self._thumbs_open

    def is_toc_open(self) -> bool:
        return self._toc_open

    def _sync_pane_visibility(self) -> None:
        thumbs_docked = self._thumbs_open and not self._thumbs_detached
        toc_docked = self._toc_open and not self._toc_detached
        self.thumb_pane.setVisible(self._thumbs_open)
        self.thumb_list.setVisible(self._thumbs_open)
        self.toc_pane.setVisible(self._toc_open)
        any_docked = thumbs_docked or toc_docked
        self._rail_visible = any_docked
        self.nav_rail.setVisible(any_docked)
        if thumbs_docked and toc_docked:
            self._rail_mode = "both"
            self.rail_splitter.setSizes([250, 260])
        elif thumbs_docked:
            self._rail_mode = "thumbs"
        elif toc_docked:
            self._rail_mode = "toc"
        self.rail_state_changed.emit()

    def set_thumbs_open(self, want_open: bool) -> None:
        self._thumbs_open = bool(want_open)
        if not self._thumbs_open:
            if self._thumbs_detached:
                self.dock_thumbs(show_docked=False)
        else:
            if self._thumbs_detached and self._floating_thumb_win is not None and shiboken6.isValid(self._floating_thumb_win):
                self._floating_thumb_win.show()
                self._floating_thumb_win.raise_()
        self._sync_pane_visibility()

    def set_toc_open(self, want_open: bool) -> None:
        self._toc_open = bool(want_open)
        if not self._toc_open:
            if self._toc_detached:
                self.dock_toc(show_docked=False)
        else:
            if self._toc_detached and self._floating_toc_win is not None and shiboken6.isValid(self._floating_toc_win):
                self._floating_toc_win.show()
                self._floating_toc_win.raise_()
        self._sync_pane_visibility()

    def detach_thumbs(self) -> None:
        if not shiboken6.isValid(self.thumb_pane):
            return
        if self._floating_thumb_win is None or not shiboken6.isValid(self._floating_thumb_win):
            win = _FloatingPaneWindow(width=270, height=500, parent=self)
            win.closed_by_user.connect(lambda: QTimer.singleShot(0, lambda: self.set_thumbs_open(False)))
            self._floating_thumb_win = win
        self._floating_thumb_win.setWindowTitle(_tv("viewer.rail.thumbs_title", self._lang))
        self._floating_thumb_win.apply_theme(self._mode)
        self._floating_thumb_win.attach_panel(self.thumb_pane)
        self._thumbs_open = True
        self._thumbs_detached = True
        self.thumb_pane.setVisible(True)
        self.thumb_list.setVisible(True)
        self._floating_thumb_win.show()
        self._floating_thumb_win.raise_()
        self._sync_detach_icons()
        self._sync_pane_visibility()

    def dock_thumbs(self, *, show_docked: bool = True) -> None:
        if self._floating_thumb_win is not None and shiboken6.isValid(self._floating_thumb_win):
            self._floating_thumb_win.detach_panel(self.thumb_pane)
            self._floating_thumb_win.hide()
        if self.thumb_pane.parentWidget() is not self.rail_splitter:
            self.rail_splitter.addWidget(self.thumb_pane)
        self._thumbs_detached = False
        self._thumbs_open = bool(show_docked)
        self._sync_detach_icons()
        self._sync_pane_visibility()

    def _toggle_thumbs_detach(self) -> None:
        if self._thumbs_detached:
            self.dock_thumbs(show_docked=True)
        else:
            self.detach_thumbs()

    def detach_toc(self) -> None:
        if not shiboken6.isValid(self.toc_pane):
            return
        if self._floating_toc_win is None or not shiboken6.isValid(self._floating_toc_win):
            win = _FloatingPaneWindow(width=285, height=500, parent=self)
            win.closed_by_user.connect(lambda: QTimer.singleShot(0, lambda: self.set_toc_open(False)))
            self._floating_toc_win = win
        self._floating_toc_win.setWindowTitle(_tv("viewer.rail.toc_title", self._lang))
        self._floating_toc_win.apply_theme(self._mode)
        self._floating_toc_win.attach_panel(self.toc_pane)
        self._toc_open = True
        self._toc_detached = True
        self.toc_pane.setVisible(True)
        self._floating_toc_win.show()
        self._floating_toc_win.raise_()
        self._sync_detach_icons()
        self._sync_pane_visibility()

    def dock_toc(self, *, show_docked: bool = True) -> None:
        if self._floating_toc_win is not None and shiboken6.isValid(self._floating_toc_win):
            self._floating_toc_win.detach_panel(self.toc_pane)
            self._floating_toc_win.hide()
        if self.toc_pane.parentWidget() is not self.rail_splitter:
            self.rail_splitter.insertWidget(0, self.toc_pane)
        self._toc_detached = False
        self._toc_open = bool(show_docked)
        self._sync_detach_icons()
        self._sync_pane_visibility()

    def _toggle_toc_detach(self) -> None:
        if self._toc_detached:
            self.dock_toc(show_docked=True)
        else:
            self.detach_toc()

    def set_rail_mode(self, mode: str) -> None:
        """Backward-compatible helper opening Miniaturas, Índice, or both simultaneously."""
        clean = mode if mode in ("thumbs", "toc", "both") else "both"
        self._rail_mode = clean
        if self._thumbs_detached:
            self.dock_thumbs(show_docked=False)
        if self._toc_detached:
            self.dock_toc(show_docked=False)
        self._thumbs_open = clean in ("thumbs", "both")
        self._toc_open = clean in ("toc", "both")
        self._sync_pane_visibility()

    def rail_mode(self) -> str:
        return self._rail_mode

    def _populate_thumbnails(self) -> None:
        self.thumb_list.blockSignals(True)
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
            self.thumb_list.setCurrentRow(self.current_page)
        self.thumb_list.blockSignals(False)

    def _on_thumb_selected(self, row: int) -> None:
        if 0 <= row < self.session.page_count and row != self.current_page:
            self.set_page(row)

    def toggle_rail(self) -> None:
        if self._rail_visible:
            self._thumbs_open = False
            self._toc_open = False
        else:
            self._thumbs_open = True
        self._sync_pane_visibility()

    def _on_scroll_changed(self, _value: int = 0) -> None:
        if not self._continuous_scroll or self._programmatic_scroll or not self._page_canvases:
            return
        for c in self._page_canvases:
            c.hide_popover()

        vbar = self.scroll.verticalScrollBar()
        vp_h = max(1, self.scroll.viewport().height())
        mid_y = vbar.value() + vp_h // 2

        best_idx = self.current_page
        best_dist = 10**9
        for idx, c in enumerate(self._page_canvases):
            center = c.y() + c.height() // 2
            dist = abs(center - mid_y)
            if dist < best_dist:
                best_dist = dist
                best_idx = idx

        if best_idx != self.current_page:
            self.current_page = best_idx
            self.pdf_canvas = self._page_canvases[best_idx]
            if self.thumb_list.currentRow() != best_idx:
                self.thumb_list.blockSignals(True)
                self.thumb_list.setCurrentRow(best_idx)
                self.thumb_list.blockSignals(False)
            self.emit_current_page_words()
            self.state_changed.emit()

        if not self._scroll_render_timer.isActive():
            self._scroll_render_timer.start()

    def set_page(self, page_index: int) -> None:
        if not (0 <= page_index < self.session.page_count):
            return
        self.current_page = page_index
        self.pdf_canvas = self._page_canvases[page_index]
        if self.thumb_list.currentRow() != page_index:
            self.thumb_list.blockSignals(True)
            self.thumb_list.setCurrentRow(page_index)
            self.thumb_list.blockSignals(False)

        if not self._continuous_scroll:
            for idx, c in enumerate(self._page_canvases):
                c.setVisible(idx == page_index)
            self.scroll.verticalScrollBar().setValue(0)
        else:
            self._programmatic_scroll = True
            try:
                target_y = max(0, self.pdf_canvas.y() - 12)
                self.scroll.verticalScrollBar().setValue(target_y)
            finally:
                self._programmatic_scroll = False

        self.render_visible_pages()
        self.emit_current_page_words()
        self.state_changed.emit()

    def emit_current_page_words(self) -> None:
        words = self.session.extract_page_words(self.current_page, limit=16)
        self.page_words_ready.emit(words)

    def set_zoom(self, zoom: float) -> None:
        self.zoom = max(0.10, min(12.0, round(float(zoom), 2)))
        self._rendered_signatures.clear()
        for c in self._page_canvases:
            c.set_rotation_and_zoom(self.rotation, self.zoom)
        if self._continuous_scroll:
            self._programmatic_scroll = True
            try:
                self.scroll.verticalScrollBar().setValue(max(0, self.pdf_canvas.y() - 12))
            finally:
                self._programmatic_scroll = False
        self.render_visible_pages()
        self.state_changed.emit()

    def set_notes_visible(self, visible: bool) -> None:
        want = bool(visible)
        for c in self._page_canvases:
            c.set_notes_visible(want)

    def notes_visible(self) -> bool:
        return bool(self._page_canvases and self._page_canvases[0].notes_visible())

    def fit_width(self) -> None:
        avail_w = max(240, self.scroll.viewport().width() - 64)
        base_w = 612.0
        if 0 <= self.current_page < self.session.page_count:
            try:
                pw, ph = self.session.get_page_size(self.current_page)
                base_w = max(120.0, ph if self.rotation in (90, 270) else pw)
            except Exception:
                pass
        self.set_zoom(avail_w / base_w)

    def rotate_cw(self) -> None:
        self.rotation = (self.rotation + 90) % 360
        self._rendered_signatures.clear()
        for c in self._page_canvases:
            c.set_rotation_and_zoom(self.rotation, self.zoom)
        self.render_visible_pages()
        self.state_changed.emit()

    def _visible_page_indices(self) -> list[int]:
        if not self._page_canvases:
            return []
        if not self._continuous_scroll:
            return [self.current_page]
        v_top = self.scroll.verticalScrollBar().value()
        vp_h = max(200, self.scroll.viewport().height())
        v_bot = v_top + vp_h
        hits: list[int] = []
        for idx, c in enumerate(self._page_canvases):
            top = c.y()
            bot = top + max(1, c.height())
            if bot >= (v_top - vp_h * 0.5) and top <= (v_bot + vp_h * 0.5):
                hits.append(idx)
        if self.current_page not in hits:
            hits.insert(0, self.current_page)
        # Ensure +/- 1 buffer around active viewport
        expanded: list[int] = []
        seen: set[int] = set()
        for idx in hits:
            for cand in (idx, idx - 1, idx + 1):
                if 0 <= cand < len(self._page_canvases) and cand not in seen:
                    seen.add(cand)
                    expanded.append(cand)
        return expanded

    def render_visible_pages(self) -> None:
        if self.session.page_count <= 0:
            return
        sig = (round(self.zoom, 2), self.rotation)
        for idx in self._visible_page_indices():
            if self._rendered_signatures.get(idx) == sig:
                continue
            cached = self.session.get_cached_page(idx, scale=self.zoom, rotation=self.rotation)
            if cached is not None:
                self._apply_page_image(idx, cached)
                continue
            if idx in self._active_tasks:
                continue
            task = AsyncPageRenderTask(
                self.session,
                self.session.doc_id,
                idx,
                self.zoom,
                self.rotation,
            )
            self._active_tasks[idx] = task
            task.signals.rendered.connect(self._on_async_rendered, Qt.ConnectionType.QueuedConnection)
            task.signals.failed.connect(self._on_async_failed, Qt.ConnectionType.QueuedConnection)
            self.backdrop.set_heavy_task_active(True)
            QThreadPool.globalInstance().start(task)

    def render_current_page(self) -> None:
        self.render_visible_pages()

    def _on_async_rendered(self, doc_id: str, page_index: int, scale: float, rotation: int, image: QImage) -> None:
        self._active_tasks.pop(page_index, None)
        if not self._active_tasks:
            self.backdrop.set_heavy_task_active(False)
        if doc_id != self.session.doc_id:
            return
        if abs(scale - self.zoom) < 0.01 and rotation == self.rotation:
            self._apply_page_image(page_index, image)

    def _on_async_failed(self, doc_id: str, page_index: int, error: str) -> None:
        del doc_id, error
        self._active_tasks.pop(page_index, None)
        if not self._active_tasks:
            self.backdrop.set_heavy_task_active(False)

    def _apply_page_image(self, page_index: int, image: QImage) -> None:
        if 0 <= page_index < len(self._page_canvases):
            self._page_canvases[page_index].set_rendered_image(image)
            self._rendered_signatures[page_index] = (round(self.zoom, 2), self.rotation)

    def _sync_detach_icons(self) -> None:
        p = get_palette(self._mode)
        self.toc_detach_btn.setIcon(
            load_svg_icon("split" if self._toc_detached else "detach", color=p["icon_color"], size=12)
        )
        self.toc_detach_btn.setToolTip(
            _tv("viewer.rail.dock" if self._toc_detached else "viewer.rail.detach", self._lang)
        )
        self.thumb_detach_btn.setIcon(
            load_svg_icon("split" if self._thumbs_detached else "detach", color=p["icon_color"], size=12)
        )
        self.thumb_detach_btn.setToolTip(
            _tv("viewer.rail.dock" if self._thumbs_detached else "viewer.rail.detach", self._lang)
        )

    def retranslate(self, lang: str) -> None:
        self._lang = lang
        self.toc_hdr_lbl.setText(_tv("viewer.rail.toc_title", lang))
        self.thumb_hdr_lbl.setText(_tv("viewer.rail.thumbs_title", lang))
        self.toc_close_btn.setToolTip(_tv("viewer.rail.close", lang))
        self.thumb_close_btn.setToolTip(_tv("viewer.rail.close", lang))
        self.toc_empty_lbl.setText(t("viewer.toc_empty", lang))
        self.bm_hdr_lbl.setText(t("viewer.bookmarks_header", lang))
        self.bm_add_btn.setText(t("viewer.bookmark_add", lang))
        self.bm_del_btn.setText(t("viewer.bookmark_del", lang))
        self._sync_detach_icons()
        if self._floating_thumb_win is not None and shiboken6.isValid(self._floating_thumb_win):
            self._floating_thumb_win.setWindowTitle(_tv("viewer.rail.thumbs_title", lang))
        if self._floating_toc_win is not None and shiboken6.isValid(self._floating_toc_win):
            self._floating_toc_win.setWindowTitle(_tv("viewer.rail.toc_title", lang))

        for mid, i18n_key in (
            ("select", "viewer.tool.select"),
            ("highlight", "viewer.tool.highlight"),
            ("free_highlight", "viewer.tool.free_highlight"),
            ("note", "viewer.tool.note"),
            ("ink", "viewer.tool.ink"),
            ("eraser", "viewer.tool.eraser"),
        ):
            self._tool_btns[mid].setText(_tv(i18n_key, lang))
        self.touch_write_btn.setText(_tv("viewer.tool.touch_write", lang))
        self.undo_annot_btn.setText(_tv("viewer.tool.undo", lang))
        self.save_annot_btn.setText(_tv("viewer.save", lang))
        self.close_annot_btn.setToolTip(_tv("viewer.rail.close", lang))
        for c in self._page_canvases:
            c.retranslate(lang)

    def apply_profile(self, profile: ThemeProfile) -> None:
        self._profile = profile
        self._mode = "light" if profile.base_mode == "light" else "dark"
        p = get_palette(self._mode)
        self.backdrop.set_profile(profile)
        self.toc_close_btn.setIcon(load_svg_icon("close", color=p["icon_color"], size=12))
        self.thumb_close_btn.setIcon(load_svg_icon("close", color=p["icon_color"], size=12))
        self.close_annot_btn.setIcon(load_svg_icon("close", color=p["icon_color"], size=12))
        self._sync_detach_icons()
        self.undo_annot_btn.setIcon(load_svg_icon("reset", color=p["icon_color"], size=13))
        self.save_annot_btn.setIcon(load_svg_icon("check", color=p["icon_color"], size=13))
        if self._floating_thumb_win is not None and shiboken6.isValid(self._floating_thumb_win):
            self._floating_thumb_win.apply_theme(self._mode)
        if self._floating_toc_win is not None and shiboken6.isValid(self._floating_toc_win):
            self._floating_toc_win.apply_theme(self._mode)
        for c in self._page_canvases:
            c.apply_theme(self._mode)

    def cleanup(self) -> None:
        self._scroll_render_timer.stop()
        for win in (self._floating_thumb_win, self._floating_toc_win):
            if win is not None and shiboken6.isValid(win):
                win.hide()
                win.close()
        self.backdrop.cleanup()
        self.session.close()


ActiveViewerTab = Union[PdfTabPage, MarkdownTabPage]


class ViewerView(QWidget):
    """Adobe-style multi-tab PDF/Markdown workspace with compact icon toolbar and right vertical tool rail."""

    backdrop_toggled = Signal(bool)
    fullscreen_requested = Signal()
    document_inspected = Signal(str, int, int)
    dictionary_lookup_recorded = Signal(str)
    ai_panel_requested = Signal()
    preferences_requested = Signal()
    active_document_changed = Signal(object)
    document_opened = Signal(object, int)
    document_edited = Signal()

    def __init__(
        self,
        *,
        bookmarks_store: BookmarksStore | None = None,
        dictionary_engine: DictionaryEngine | None = None,
        guard: MotionPerformanceGuard | None = None,
        offline_only_getter=None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._lang = "es"
        self._mode = "dark"
        self._applied_theme_mode = ""
        self._is_fullscreen = False
        self._show_button_labels = False
        self._continuous_scroll = True
        self._bookmarks = bookmarks_store or BookmarksStore()
        self._dict_engine = dictionary_engine or DictionaryEngine()
        self._guard = guard or MotionPerformanceGuard()
        self._offline_only_getter = offline_only_getter
        self._profile = ThemeProfile()
        self._is_dict_detached = False
        self._floating_dict_win: _FloatingPaneWindow | None = None
        self._find_hits: list[SearchHit] = []
        self._find_idx: int = -1
        self._presentation_win: PresentationWindow | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(6)

        # Single-Row Borderless Top Bar (icon-only centered by default; zero redundant controls)
        bar_frame = QFrame(self)
        bar_frame.setObjectName("ViewerToolBar")
        bar_row = QHBoxLayout(bar_frame)
        bar_row.setContentsMargins(6, 4, 6, 4)
        bar_row.setSpacing(6)

        self.save_btn = QPushButton("", bar_frame)
        self.save_btn.setProperty("role", "GhostAction")
        self.save_btn.clicked.connect(self.save_current)
        bar_row.addWidget(self.save_btn, 0, Qt.AlignmentFlag.AlignVCenter)

        self.find_btn = QPushButton("", bar_frame)
        self.find_btn.setProperty("role", "GhostAction")
        self.find_btn.setCheckable(True)
        self.find_btn.clicked.connect(lambda checked: self.open_find_bar() if checked else self.close_find_bar())
        bar_row.addWidget(self.find_btn, 0, Qt.AlignmentFlag.AlignVCenter)

        self.print_btn = QPushButton("", bar_frame)
        self.print_btn.setProperty("role", "GhostAction")
        self.print_btn.clicked.connect(self.open_print_dialog)
        bar_row.addWidget(self.print_btn, 0, Qt.AlignmentFlag.AlignVCenter)

        self.share_btn = QPushButton("", bar_frame)
        self.share_btn.setProperty("role", "GhostAction")
        self.share_btn.clicked.connect(self.open_share_dialog)
        bar_row.addWidget(self.share_btn, 0, Qt.AlignmentFlag.AlignVCenter)

        bar_row.addWidget(self._make_v_sep(bar_frame))

        self.zoom_out_btn = QPushButton("", bar_frame)
        self.zoom_out_btn.setProperty("role", "GhostAction")
        self.zoom_out_btn.clicked.connect(lambda: self._step_zoom(-1))
        bar_row.addWidget(self.zoom_out_btn)

        self.zoom_tag = StatusTag("115%", tone="accent", parent=bar_frame)
        bar_row.addWidget(self.zoom_tag)

        self.zoom_in_btn = QPushButton("", bar_frame)
        self.zoom_in_btn.setProperty("role", "GhostAction")
        self.zoom_in_btn.clicked.connect(lambda: self._step_zoom(1))
        bar_row.addWidget(self.zoom_in_btn)

        self.fit_btn = QPushButton("", bar_frame)
        self.fit_btn.setProperty("role", "GhostAction")
        self.fit_btn.clicked.connect(self._fit_width)
        bar_row.addWidget(self.fit_btn)

        bar_row.addWidget(self._make_v_sep(bar_frame))

        self.prev_btn = QPushButton("", bar_frame)
        self.prev_btn.setProperty("role", "GhostAction")
        self.prev_btn.clicked.connect(lambda: self._step_page(-1))
        bar_row.addWidget(self.prev_btn)

        self.page_tag = StatusTag("0 / 0", tone="muted", parent=bar_frame)
        bar_row.addWidget(self.page_tag)

        self.next_btn = QPushButton("", bar_frame)
        self.next_btn.setProperty("role", "GhostAction")
        self.next_btn.clicked.connect(lambda: self._step_page(1))
        bar_row.addWidget(self.next_btn)

        self.rotate_btn = QPushButton("", bar_frame)
        self.rotate_btn.setProperty("role", "GhostAction")
        self.rotate_btn.clicked.connect(self._rotate_page)
        bar_row.addWidget(self.rotate_btn)

        self.toggle_notes_vis_btn = QPushButton("", bar_frame)
        self.toggle_notes_vis_btn.setProperty("role", "GhostAction")
        self.toggle_notes_vis_btn.setCheckable(True)
        self.toggle_notes_vis_btn.setChecked(True)
        self.toggle_notes_vis_btn.clicked.connect(lambda ch: self.set_notes_visible(bool(ch)))
        bar_row.addWidget(self.toggle_notes_vis_btn, 0, Qt.AlignmentFlag.AlignVCenter)

        bar_row.addStretch(1)

        self.present_btn = QPushButton("", bar_frame)
        self.present_btn.setProperty("role", "GhostAction")
        self.present_btn.clicked.connect(self.open_presentation_mode)
        bar_row.addWidget(self.present_btn, 0, Qt.AlignmentFlag.AlignVCenter)

        self.fullscreen_btn = QPushButton("", bar_frame)
        self.fullscreen_btn.setProperty("role", "GhostAction")
        self.fullscreen_btn.clicked.connect(self.fullscreen_requested.emit)
        bar_row.addWidget(self.fullscreen_btn, 0, Qt.AlignmentFlag.AlignVCenter)

        root.addWidget(bar_frame)

        # Collapsible Ctrl+F In-Document Search Bar
        self.find_bar = QFrame(self)
        self.find_bar.setObjectName("ViewerToolBar")
        find_row = QHBoxLayout(self.find_bar)
        find_row.setContentsMargins(8, 4, 8, 4)
        find_row.setSpacing(6)

        self.find_input = QLineEdit(self.find_bar)
        self.find_input.setMinimumWidth(220)
        self.find_input.setClearButtonEnabled(True)
        self.find_input.textChanged.connect(lambda txt: self.run_find(txt))
        self.find_input.returnPressed.connect(self.find_next)
        find_row.addWidget(self.find_input, 1)

        self.find_case_btn = QPushButton("Aa", self.find_bar)
        self.find_case_btn.setProperty("role", "GhostAction")
        self.find_case_btn.setCheckable(True)
        self.find_case_btn.setFixedSize(34, 28)
        self.find_case_btn.setStyleSheet("padding: 0px 4px;")
        self.find_case_btn.setToolTip("Coincidir mayusculas y minusculas")
        self.find_case_btn.clicked.connect(lambda: self.run_find(self.find_input.text()))
        find_row.addWidget(self.find_case_btn)

        self.find_prev_btn = QPushButton("", self.find_bar)
        self.find_prev_btn.setProperty("role", "GhostAction")
        self.find_prev_btn.setProperty("iconOnly", "true")
        self.find_prev_btn.setFixedSize(28, 28)
        self.find_prev_btn.setToolTip("Coincidencia anterior (Shift+Enter)")
        self.find_prev_btn.clicked.connect(self.find_prev)
        find_row.addWidget(self.find_prev_btn)

        self.find_next_btn = QPushButton("", self.find_bar)
        self.find_next_btn.setProperty("role", "GhostAction")
        self.find_next_btn.setProperty("iconOnly", "true")
        self.find_next_btn.setFixedSize(28, 28)
        self.find_next_btn.setToolTip("Siguiente coincidencia (Enter)")
        self.find_next_btn.clicked.connect(self.find_next)
        find_row.addWidget(self.find_next_btn)

        self.find_count_lbl = QLabel("0 / 0", self.find_bar)
        self.find_count_lbl.setProperty("tone", "muted")
        self.find_count_lbl.setMinimumWidth(52)
        self.find_count_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        find_row.addWidget(self.find_count_lbl)

        self.find_close_btn = QPushButton("", self.find_bar)
        self.find_close_btn.setProperty("role", "GhostAction")
        self.find_close_btn.setProperty("iconOnly", "true")
        self.find_close_btn.setFixedSize(28, 28)
        self.find_close_btn.setToolTip("Cerrar busqueda (Esc)")
        self.find_close_btn.clicked.connect(self.close_find_bar)
        find_row.addWidget(self.find_close_btn)

        find_esc = QShortcut(QKeySequence("Escape"), self.find_bar)
        find_esc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        find_esc.activated.connect(self.close_find_bar)

        self.find_bar.setVisible(False)
        root.addWidget(self.find_bar)

        # Body: Multi-Tab Document Area + Detachable Dictionary + Adobe-style Vertical Right Rail
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

        self.dict_panel = DictionaryPanel(
            self._dict_engine,
            offline_only_getter=self._offline_only_getter,
            parent=self,
        )
        self.dict_panel.setVisible(False)
        self.dict_panel.word_looked_up.connect(self.dictionary_lookup_recorded.emit)
        self.dict_panel.close_requested.connect(lambda: QTimer.singleShot(0, self._handle_panel_close_requested))
        self.dict_panel.detach_toggled.connect(
            lambda want: QTimer.singleShot(0, lambda w=want: self._handle_panel_detach_toggle(w))
        )
        self._body_row.addWidget(self.dict_panel)

        self.notes_panel = NotesPanel(mode=self._mode, lang=self._lang, parent=self)
        self.notes_panel.setVisible(False)
        self.notes_panel.close_requested.connect(lambda: self.set_notes_panel_visible(False))
        self.notes_panel.jump_to_note_requested.connect(self._jump_to_note)
        self.notes_panel.add_note_requested.connect(self._add_note_from_panel)
        self.notes_panel.delete_note_requested.connect(self._delete_note_from_panel)
        self.notes_panel.notes_visibility_toggled.connect(self.set_notes_visible)
        self._body_row.addWidget(self.notes_panel)

        # Adobe-Style Right Vertical Tool Rail
        self.right_rail = QFrame(self)
        self.right_rail.setObjectName("AdobeRightRail")
        self.right_rail.setFixedWidth(44)
        rr_lay = QVBoxLayout(self.right_rail)
        rr_lay.setContentsMargins(6, 8, 6, 8)
        rr_lay.setSpacing(6)
        rr_lay.setAlignment(Qt.AlignmentFlag.AlignHCenter)

        self.thumbs_rail_btn = QPushButton("", self.right_rail)
        self.thumbs_rail_btn.setProperty("role", "ModeTab")
        self.thumbs_rail_btn.setCheckable(True)
        self.thumbs_rail_btn.clicked.connect(self._toggle_thumbs_pane)
        rr_lay.addWidget(self.thumbs_rail_btn)
        self.rail_btn = self.thumbs_rail_btn  # Compatibility alias

        self.toc_rail_btn = QPushButton("", self.right_rail)
        self.toc_rail_btn.setProperty("role", "ModeTab")
        self.toc_rail_btn.setCheckable(True)
        self.toc_rail_btn.clicked.connect(self._toggle_toc_pane)
        rr_lay.addWidget(self.toc_rail_btn)

        rr_lay.addWidget(self._make_h_sep(self.right_rail))

        self.ink_rail_btn = QPushButton("", self.right_rail)
        self.ink_rail_btn.setProperty("role", "ModeTab")
        self.ink_rail_btn.setCheckable(True)
        self.ink_rail_btn.clicked.connect(self._toggle_ink_bar)
        rr_lay.addWidget(self.ink_rail_btn)

        self.notes_rail_btn = QPushButton("", self.right_rail)
        self.notes_rail_btn.setProperty("role", "ModeTab")
        self.notes_rail_btn.setCheckable(True)
        self.notes_rail_btn.clicked.connect(self.set_notes_panel_visible)
        rr_lay.addWidget(self.notes_rail_btn)

        self.dict_btn = QPushButton("", self.right_rail)
        self.dict_btn.setProperty("role", "ModeTab")
        self.dict_btn.setCheckable(True)
        self.dict_btn.clicked.connect(self._toggle_dictionary_panel)
        rr_lay.addWidget(self.dict_btn)

        self.ai_rail_btn = QPushButton("", self.right_rail)
        self.ai_rail_btn.setProperty("role", "ModeTab")
        self.ai_rail_btn.setCheckable(True)
        self.ai_rail_btn.clicked.connect(self.ai_panel_requested.emit)
        rr_lay.addWidget(self.ai_rail_btn)

        self.backdrop_btn = QPushButton("", self.right_rail)
        self.backdrop_btn.setProperty("role", "ModeTab")
        self.backdrop_btn.setCheckable(True)
        self.backdrop_btn.clicked.connect(self._on_backdrop_btn_clicked)
        rr_lay.addWidget(self.backdrop_btn)

        rr_lay.addStretch(1)

        self._body_row.addWidget(self.right_rail)
        root.addLayout(self._body_row, 1)

        save_sc = QShortcut(QKeySequence("Ctrl+S"), self)
        save_sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        save_sc.activated.connect(self.save_current)
        self._save_shortcut = save_sc

        for seq, cb in (
            ("Ctrl+F", self.open_find_bar),
            ("Ctrl+P", self.open_print_dialog),
            ("F5", self.open_presentation_mode),
            ("Ctrl+L", self.open_presentation_mode),
            ("Ctrl+W", self.close_current_tab),
            ("Ctrl++", lambda: self._step_zoom(1)),
            ("Ctrl+=", lambda: self._step_zoom(1)),
            ("Ctrl+-", lambda: self._step_zoom(-1)),
            ("Ctrl+0", self._fit_width),
            ("Ctrl+R", self._rotate_page),
            ("Ctrl+Z", self._undo_current),
            ("Ctrl+Shift+N", self.toggle_notes_visible),
            ("PgDown", lambda: self._step_page(1)),
            ("PgUp", lambda: self._step_page(-1)),
            ("Home", self._jump_first_page),
            ("End", self._jump_last_page),
        ):
            sc = QShortcut(QKeySequence(seq), self)
            sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            sc.activated.connect(cb)

        self.set_show_button_labels(False)
        self.retranslate("es")

    def _make_v_sep(self, parent: QWidget) -> QFrame:
        sep = QFrame(parent)
        sep.setFrameShape(QFrame.Shape.VLine)
        sep.setFixedWidth(1)
        sep.setFixedHeight(20)
        sep.setStyleSheet("background-color: #334155;")
        return sep

    def _make_h_sep(self, parent: QWidget) -> QFrame:
        sep = QFrame(parent)
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFixedHeight(1)
        sep.setStyleSheet("background-color: #334155;")
        return sep

    def _all_toolbar_buttons(self) -> list[tuple[QPushButton, str]]:
        return [
            (self.save_btn, _tv("viewer.save", self._lang)),
            (self.find_btn, _tv("viewer.find", self._lang)),
            (self.print_btn, _tv("viewer.print", self._lang)),
            (self.share_btn, _tv("viewer.share", self._lang)),
            (self.zoom_out_btn, t("viewer.zoom_out", self._lang)),
            (self.zoom_in_btn, t("viewer.zoom_in", self._lang)),
            (self.fit_btn, t("viewer.fit_width", self._lang)),
            (self.prev_btn, t("viewer.prev", self._lang)),
            (self.next_btn, t("viewer.next", self._lang)),
            (self.rotate_btn, t("viewer.rotate", self._lang)),
            (self.toggle_notes_vis_btn, _tv("viewer.toggle_notes_vis", self._lang)),
            (self.present_btn, _tv("viewer.present", self._lang)),
            (
                self.fullscreen_btn,
                t("viewer.exit_fullscreen" if self._is_fullscreen else "viewer.fullscreen", self._lang),
            ),
            (self.thumbs_rail_btn, _tv("viewer.rail.thumbs_title", self._lang)),
            (self.toc_rail_btn, _tv("viewer.rail.toc_title", self._lang)),
            (self.ink_rail_btn, _tv("viewer.rail.ink_tools", self._lang)),
            (self.notes_rail_btn, _tv("viewer.rail.notes", self._lang)),
            (self.dict_btn, t("viewer.dictionary", self._lang)),
            (self.ai_rail_btn, _tv("viewer.rail.ai", self._lang)),
            (self.backdrop_btn, t("viewer.backdrop_toggle", self._lang)),
        ]

    def set_show_button_labels(self, show_labels: bool) -> None:
        self._show_button_labels = bool(show_labels)
        self.right_rail.setFixedWidth(148 if self._show_button_labels else 44)
        for btn, label in self._all_toolbar_buttons():
            btn.setToolTip(label)
            btn.setIconSize(QSize(16, 16))
            if self._show_button_labels:
                btn.setProperty("iconOnly", "false")
                btn.setMinimumSize(0, 30)
                btn.setMaximumSize(16777215, 32)
                btn.setText(label)
                btn.style().unpolish(btn)
                btn.style().polish(btn)
            else:
                btn.setProperty("iconOnly", "true")
                btn.setText("")
                btn.style().unpolish(btn)
                btn.style().polish(btn)
                btn.setFixedSize(32, 32)

    def show_button_labels(self) -> bool:
        return self._show_button_labels

    def set_continuous_scroll(self, enabled: bool) -> None:
        self._continuous_scroll = bool(enabled)
        for idx in range(self.tabs.count()):
            tab = self.tabs.widget(idx)
            if isinstance(tab, PdfTabPage):
                tab.set_continuous_scroll(self._continuous_scroll)

    def set_ai_panel_checked(self, checked: bool) -> None:
        self.ai_rail_btn.blockSignals(True)
        self.ai_rail_btn.setChecked(bool(checked))
        self.ai_rail_btn.blockSignals(False)

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
                self.document_opened.emit(resolved, tab.session.page_count)
                return True

        try:
            tab = PdfTabPage(
                resolved,
                bookmarks_store=self._bookmarks,
                guard=self._guard,
                continuous_scroll=self._continuous_scroll,
                parent=self.tabs,
            )
        except Exception:
            return False
        tab.retranslate(self._lang)
        tab.apply_profile(self._profile)
        tab.state_changed.connect(self._on_tab_state_changed)
        tab.page_words_ready.connect(self._on_tab_page_words)
        tab.lookup_word_requested.connect(self.lookup_in_dictionary)
        tab.rail_state_changed.connect(self._sync_right_rail_state)
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
        self.document_opened.emit(resolved, tab.session.page_count)
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
                self.document_opened.emit(resolved, 1)
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
        self.document_opened.emit(resolved, 1)
        return True

    def _on_tab_state_changed(self) -> None:
        for idx in range(self.tabs.count()):
            w = self.tabs.widget(idx)
            if isinstance(w, (PdfTabPage, MarkdownTabPage)):
                base = str(w.property("base_tab_title") or (w.pdf_path.name if isinstance(w, PdfTabPage) else w.md_path.name))
                dirty = w.has_unsaved_changes()
                self.tabs.setTabText(idx, f"* {base}" if dirty else base)
        self._sync_toolbar_state()
        self._sync_notes_panel()
        self.document_edited.emit()

    def _on_tab_page_words(self, words: list[str]) -> None:
        sender = self.sender()
        if sender is self._current_tab():
            self.dict_panel.set_page_words(words)

    def _on_active_tab_changed(self) -> None:
        self._sync_toolbar_state()
        self._sync_right_rail_state()
        self._sync_notes_panel()
        tab = self._current_tab()
        if isinstance(tab, PdfTabPage):
            tab.set_notes_visible(self.toggle_notes_vis_btn.isChecked())
            tab.emit_current_page_words()
            self.active_document_changed.emit(tab.pdf_path)
        elif isinstance(tab, MarkdownTabPage):
            tab.emit_document_words()
            self.active_document_changed.emit(tab.md_path)
        else:
            self.active_document_changed.emit(None)

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
        self._on_active_tab_changed()

    def _toggle_rail(self) -> None:
        self._toggle_thumbs_pane()

    def _toggle_thumbs_pane(self) -> None:
        tab = self._current_tab()
        if isinstance(tab, PdfTabPage):
            tab.set_thumbs_open(not tab.is_thumbs_open())
        elif tab is not None:
            tab.toggle_rail()
        self._sync_right_rail_state()

    def _toggle_toc_pane(self) -> None:
        tab = self._current_tab()
        if isinstance(tab, PdfTabPage):
            tab.set_toc_open(not tab.is_toc_open())
        elif tab is not None:
            tab.toggle_rail()
        self._sync_right_rail_state()

    def _toggle_ink_bar(self) -> None:
        tab = self._current_tab()
        if isinstance(tab, PdfTabPage):
            tab.set_annotation_bar_visible(not tab.is_annotation_bar_visible())
        self._sync_right_rail_state()

    def _sync_right_rail_state(self) -> None:
        tab = self._current_tab()
        is_pdf = isinstance(tab, PdfTabPage)
        for btn, val in (
            (self.thumbs_rail_btn, tab.is_thumbs_open() if is_pdf else False),
            (self.toc_rail_btn, tab.is_toc_open() if is_pdf else False),
            (self.ink_rail_btn, tab.is_annotation_bar_visible() if is_pdf else False),
        ):
            btn.blockSignals(True)
            btn.setChecked(bool(val))
            btn.setEnabled(tab is not None)
            btn.blockSignals(False)
        self.ink_rail_btn.setEnabled(is_pdf)
        self.notes_rail_btn.setEnabled(is_pdf)
        self.notes_rail_btn.blockSignals(True)
        self.notes_rail_btn.setChecked(self.notes_panel.isVisible() if is_pdf else False)
        self.notes_rail_btn.blockSignals(False)

    def set_notes_panel_visible(self, visible: bool) -> None:
        want = bool(visible)
        self.notes_panel.setVisible(want)
        self.notes_rail_btn.blockSignals(True)
        self.notes_rail_btn.setChecked(want)
        self.notes_rail_btn.blockSignals(False)
        if want:
            self._sync_notes_panel()

    def _sync_notes_panel(self) -> None:
        if not shiboken6.isValid(self.notes_panel):
            return
        tab = self._current_tab()
        if isinstance(tab, PdfTabPage):
            self.notes_panel.set_notes(tab.shared_annotations.records)
        else:
            self.notes_panel.set_notes([])

    def _jump_to_note(self, page_index: int, note_id: str) -> None:
        tab = self._current_tab()
        if not isinstance(tab, PdfTabPage):
            return
        tab.set_page(page_index)
        tab.pdf_canvas.open_sticky_note_card(note_id)

    def _add_note_from_panel(self, content: str, category: str, color_hex: str) -> None:
        tab = self._current_tab()
        if not isinstance(tab, PdfTabPage):
            return
        rec = tab.pdf_canvas.add_sticky_note((72.0, 680.0), content, color_hex=color_hex, category=category)
        self._sync_notes_panel()
        self.document_edited.emit()
        tab.pdf_canvas.open_sticky_note_card(rec.id)

    def _delete_note_from_panel(self, note_id: str) -> None:
        tab = self._current_tab()
        if not isinstance(tab, PdfTabPage):
            return
        tab.pdf_canvas.delete_annotation(note_id)
        self._sync_notes_panel()
        self.document_edited.emit()

    def _step_zoom(self, delta: float) -> None:
        tab = self._current_tab()
        if not isinstance(tab, PdfTabPage):
            return
        if abs(delta) >= 0.99:
            sign = 1.0 if delta > 0 else -1.0
            cur = tab.zoom
            if cur < 1.0:
                step = 0.10
            elif cur < 2.5:
                step = 0.25
            elif cur < 5.0:
                step = 0.50
            else:
                step = 1.00
            tab.set_zoom(cur + sign * step)
        else:
            tab.set_zoom(tab.zoom + delta)

    def set_notes_visible(self, visible: bool) -> None:
        want = bool(visible)
        self.toggle_notes_vis_btn.blockSignals(True)
        self.toggle_notes_vis_btn.setChecked(want)
        self.toggle_notes_vis_btn.blockSignals(False)
        p = get_palette(self._mode)
        self.toggle_notes_vis_btn.setIcon(
            load_svg_icon("eye" if want else "eye_off", color=p["icon_color"], size=16)
        )
        self.notes_panel.set_notes_visibility_state(want)
        tab = self._current_tab()
        if isinstance(tab, PdfTabPage):
            tab.set_notes_visible(want)

    def toggle_notes_visible(self) -> None:
        self.set_notes_visible(not self.toggle_notes_vis_btn.isChecked())

    def open_share_dialog(self) -> None:
        tab = self._current_tab()
        if tab is None:
            return
        doc_path = tab.pdf_path if isinstance(tab, PdfTabPage) else tab.md_path
        dlg = ShareDocumentDialog(doc_path, mode=self._mode, parent=self)
        dlg.exec()

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

    def _ensure_floating_window(self) -> _FloatingPaneWindow:
        if self._floating_dict_win is None or not shiboken6.isValid(self._floating_dict_win):
            win = _FloatingPaneWindow(width=320, height=480, parent=self)
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
                idx = self._body_row.indexOf(self.right_rail)
                if self._body_row.indexOf(self.dict_panel) < 0:
                    self._body_row.insertWidget(max(1, idx), self.dict_panel)
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
        idx = self._body_row.indexOf(self.right_rail)
        if self._body_row.indexOf(self.dict_panel) < 0:
            self._body_row.insertWidget(max(1, idx), self.dict_panel)
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
        idx = self._body_row.indexOf(self.right_rail)
        if self._body_row.indexOf(self.dict_panel) < 0:
            self._body_row.insertWidget(max(1, idx), self.dict_panel)
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
        label = t(key, self._lang)
        self.fullscreen_btn.setToolTip(label)
        self.fullscreen_btn.setText(label if self._show_button_labels else "")
        self.fullscreen_btn.setIcon(load_svg_icon(icon, color=p["icon_color"], size=15))

    def _jump_first_page(self) -> None:
        tab = self._current_tab()
        if isinstance(tab, PdfTabPage):
            tab.set_page(0)

    def _jump_last_page(self) -> None:
        tab = self._current_tab()
        if isinstance(tab, PdfTabPage) and tab.session.page_count > 0:
            tab.set_page(tab.session.page_count - 1)

    def _undo_current(self) -> None:
        tab = self._current_tab()
        if isinstance(tab, PdfTabPage):
            tab._undo_last_annotation()

    def close_current_tab(self) -> None:
        idx = self.tabs.currentIndex()
        if idx >= 0:
            self._close_tab(idx)

    # --- Ctrl+F In-Document Search ---

    def open_find_bar(self) -> None:
        tab = self._current_tab()
        if tab is None:
            return
        self.find_bar.setVisible(True)
        self.find_btn.setChecked(True)
        self.find_input.setFocus()
        self.find_input.selectAll()

    def close_find_bar(self) -> None:
        self.find_bar.setVisible(False)
        self.find_btn.setChecked(False)
        self._clear_search_highlights()

    def _clear_search_highlights(self) -> None:
        self._find_hits = []
        self._find_idx = -1
        self.find_count_lbl.setText("0 / 0")
        for idx in range(self.tabs.count()):
            w = self.tabs.widget(idx)
            if isinstance(w, PdfTabPage):
                for canvas in w._page_canvases:
                    canvas.set_search_highlights([], active_index=-1)

    def _apply_search_hit(self, hit_index: int) -> None:
        tab = self._current_tab()
        if not isinstance(tab, PdfTabPage) or not self._find_hits:
            return
        self._find_idx = hit_index % len(self._find_hits)
        active = self._find_hits[self._find_idx]
        self.find_count_lbl.setText(f"{self._find_idx + 1} / {len(self._find_hits)}")

        for pidx, canvas in enumerate(tab._page_canvases):
            page_hits = [h for h in self._find_hits if h.page_index == pidx]
            if not page_hits:
                canvas.set_search_highlights([], active_index=-1)
                continue
            active_local = -1
            rects = []
            for local_i, h in enumerate(page_hits):
                rects.append(h.rect_pdf)
                if h is active:
                    active_local = local_i
            canvas.set_search_highlights(rects, active_index=active_local)

        if 0 <= active.page_index < tab.session.page_count:
            tab.set_page(active.page_index)
            if active.page_index < len(tab._page_canvases):
                c = tab._page_canvases[active.page_index]
                sr = c._pdf_to_screen_rect(active.rect_pdf)
                tab.scroll.ensureVisible(
                    max(0, c.x() + int(sr.center().x())),
                    max(0, c.y() + int(sr.center().y())),
                    40,
                    90,
                )

    def run_find(self, query: str | None = None) -> list[SearchHit]:
        needle = (self.find_input.text() if query is None else str(query)).strip()
        tab = self._current_tab()
        self._clear_search_highlights()
        if not needle or tab is None:
            return []

        if isinstance(tab, PdfTabPage):
            hits = tab.session.search_text(
                needle,
                case_sensitive=self.find_case_btn.isChecked(),
                max_hits=250,
            )
            self._find_hits = hits
            if hits:
                self._apply_search_hit(0)
            return hits

        if isinstance(tab, MarkdownTabPage):
            found = tab.preview_browser.find(needle) or tab.editor_widget.find(needle)
            self.find_count_lbl.setText("1 / 1" if found else "0 / 0")
            return []
        return []

    def find_next(self) -> None:
        tab = self._current_tab()
        needle = self.find_input.text().strip()
        if isinstance(tab, MarkdownTabPage) and needle:
            found = tab.preview_browser.find(needle) or tab.editor_widget.find(needle)
            if not found:
                cursor = tab.preview_browser.textCursor()
                cursor.movePosition(cursor.MoveOperation.Start)
                tab.preview_browser.setTextCursor(cursor)
                found = tab.preview_browser.find(needle)
            self.find_count_lbl.setText("OK" if found else "0 / 0")
            return
        if not self._find_hits:
            self.run_find()
            return
        self._apply_search_hit(self._find_idx + 1)

    def find_prev(self) -> None:
        if not self._find_hits:
            self.run_find()
            return
        self._apply_search_hit(self._find_idx - 1)

    # --- Ctrl+P System Print & F5/Ctrl+L Presentation Mode ---

    def open_print_dialog(self) -> None:
        tab = self._current_tab()
        if not isinstance(tab, PdfTabPage):
            return
        dlg = PrintPdfDialog(
            tab.session,
            current_page=tab.current_page,
            doc_name=tab.pdf_path.name,
            mode=self._mode,
            parent=self,
        )
        dlg.exec()

    def open_presentation_mode(self) -> None:
        tab = self._current_tab()
        if not isinstance(tab, PdfTabPage):
            return
        if self._presentation_win is None or not shiboken6.isValid(self._presentation_win):
            self._presentation_win = PresentationWindow(self)
            self._presentation_win.closed_at_page.connect(self._on_presentation_closed)
        self._presentation_win.start_presentation(tab.session, start_page=tab.current_page)

    def _on_presentation_closed(self, page_index: int) -> None:
        tab = self._current_tab()
        if isinstance(tab, PdfTabPage) and 0 <= page_index < tab.session.page_count:
            tab.set_page(page_index)

    def _sync_toolbar_state(self) -> None:
        tab = self._current_tab()
        if tab is None:
            self.zoom_tag.setText("—")
            self.page_tag.setText("0 / 0")
            self.save_btn.setEnabled(False)
            self.find_btn.setEnabled(False)
            self.print_btn.setEnabled(False)
            self.share_btn.setEnabled(False)
            self.toggle_notes_vis_btn.setEnabled(False)
            self.present_btn.setEnabled(False)
            for b in (self.zoom_out_btn, self.zoom_in_btn, self.fit_btn, self.prev_btn, self.next_btn, self.rotate_btn):
                b.setEnabled(False)
            return
        if isinstance(tab, PdfTabPage):
            self.zoom_tag.setText(f"{int(round(tab.zoom * 100))}%")
            self.page_tag.setText(f"{tab.current_page + 1} / {tab.session.page_count}")
            self.save_btn.setEnabled(True)
            self.find_btn.setEnabled(True)
            self.print_btn.setEnabled(True)
            self.share_btn.setEnabled(True)
            self.toggle_notes_vis_btn.setEnabled(True)
            self.present_btn.setEnabled(True)
            for b in (self.zoom_out_btn, self.zoom_in_btn, self.fit_btn, self.prev_btn, self.next_btn, self.rotate_btn):
                b.setEnabled(True)
        else:
            self.zoom_tag.setText("100%")
            self.page_tag.setText("MD")
            self.save_btn.setEnabled(True)
            self.find_btn.setEnabled(True)
            self.print_btn.setEnabled(False)
            self.share_btn.setEnabled(True)
            self.toggle_notes_vis_btn.setEnabled(False)
            self.present_btn.setEnabled(False)
            for b in (self.zoom_out_btn, self.zoom_in_btn, self.fit_btn, self.prev_btn, self.next_btn, self.rotate_btn):
                b.setEnabled(False)

    def retranslate(self, lang: str) -> None:
        self._lang = lang
        self.set_show_button_labels(self._show_button_labels)
        self.set_fullscreen_state(self._is_fullscreen)
        self.empty_lbl.setText(_tv("viewer.empty_dual", lang))
        self.find_input.setPlaceholderText(_tv("viewer.find_placeholder", lang))
        self.dict_panel.retranslate(lang)
        self.notes_panel.retranslate(lang)
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

        if self._mode != self._applied_theme_mode:
            self._applied_theme_mode = self._mode
            self.save_btn.setIcon(load_svg_icon("check", color=p["icon_color"], size=16))
            self.find_btn.setIcon(load_svg_icon("search", color=p["icon_color"], size=16))
            self.print_btn.setIcon(load_svg_icon("printer", color=p["icon_color"], size=16))
            self.share_btn.setIcon(load_svg_icon("share", color=p["icon_color"], size=16))
            self.zoom_out_btn.setIcon(load_svg_icon("zoom_out", color=p["icon_color"], size=16))
            self.zoom_in_btn.setIcon(load_svg_icon("zoom_in", color=p["icon_color"], size=16))
            self.fit_btn.setIcon(load_svg_icon("fit_width", color=p["icon_color"], size=16))
            self.prev_btn.setIcon(load_svg_icon("prev", color=p["icon_color"], size=16))
            self.next_btn.setIcon(load_svg_icon("next", color=p["icon_color"], size=16))
            self.rotate_btn.setIcon(load_svg_icon("rotate", color=p["icon_color"], size=16))
            self.toggle_notes_vis_btn.setIcon(
                load_svg_icon(
                    "eye" if self.toggle_notes_vis_btn.isChecked() else "eye_off",
                    color=p["icon_color"],
                    size=16,
                )
            )
            self.present_btn.setIcon(load_svg_icon("presentation", color=p["icon_color"], size=16))
            self.find_prev_btn.setIcon(load_svg_icon("prev", color=p["icon_color"], size=14))
            self.find_next_btn.setIcon(load_svg_icon("next", color=p["icon_color"], size=14))
            self.find_close_btn.setIcon(load_svg_icon("close", color=p["icon_color"], size=13))

            self.thumbs_rail_btn.setIcon(load_svg_icon("thumbs", color=p["icon_color"], size=16))
            self.toc_rail_btn.setIcon(load_svg_icon("toc", color=p["icon_color"], size=16))
            self.ink_rail_btn.setIcon(load_svg_icon("ink", color=p["icon_color"], size=16))
            self.notes_rail_btn.setIcon(load_svg_icon("comment", color=p["icon_color"], size=16))
            self.dict_btn.setIcon(load_svg_icon("book", color=p["icon_color"], size=16))
            self.ai_rail_btn.setIcon(load_svg_icon("ai_spark", color=p["icon_color"], size=16))
            self.backdrop_btn.setIcon(load_svg_icon("palette", color=p["icon_color"], size=16))
            self.set_fullscreen_state(self._is_fullscreen)

            self.zoom_tag.apply_theme(self._mode)
            self.page_tag.apply_theme(self._mode)
            self.dict_panel.apply_theme(self._mode)
            self.notes_panel.apply_theme(self._mode)
            if self._floating_dict_win is not None and shiboken6.isValid(self._floating_dict_win):
                self._floating_dict_win.apply_theme(self._mode)

        for idx in range(self.tabs.count()):
            tab = self.tabs.widget(idx)
            if isinstance(tab, (PdfTabPage, MarkdownTabPage)):
                tab.apply_profile(profile)

    def cleanup(self) -> None:
        if self._presentation_win is not None and shiboken6.isValid(self._presentation_win):
            self._presentation_win.hide()
            self._presentation_win.close()
        if self._floating_dict_win is not None and shiboken6.isValid(self._floating_dict_win):
            self._floating_dict_win.hide()
            self._floating_dict_win.close()
        for idx in range(self.tabs.count()):
            tab = self.tabs.widget(idx)
            if isinstance(tab, (PdfTabPage, MarkdownTabPage)):
                tab.cleanup()
