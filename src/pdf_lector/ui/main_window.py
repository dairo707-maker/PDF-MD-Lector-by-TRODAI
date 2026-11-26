"""Main window shell for PDF/MD Lector by TRODAI: Min/Max/Close, F11 Fullscreen, Update Banner, Dialogs & Dual Workspace."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
from typing import Any

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QAction, QDragEnterEvent, QDropEvent, QFont, QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
import shiboken6

from .. import APP_NAME, COMPANY_NAME, get_base_dir, read_version
from ..core.bookmarks_store import BookmarksStore
from ..core.crash_guard import CrashGuard
from ..core.dictionary_engine import DictionaryEngine
from ..core.i18n import t
from ..core.notifications import NotificationCenter
from ..core.path_guard import ALLOWED_MEDIA_EXTENSIONS, validate_local_any_doc
from ..core.pdf_engine import generate_sample_pdf
from ..core.task_control import TaskManager
from ..core.tools import ToolRegistry
from ..core.updater import ReleaseInfo, load_updater_state, save_updater_state
from ..core.usage_insights import LocalUsageInsights
from ..core.win32_bridge import apply_trodai_window_chrome
from ..theme.motion_engine import MotionPerformanceGuard, ViewTransitionController
from ..theme.palette import MONO_FONT_FAMILY, UI_FONT_FAMILY, build_trodai_qss, get_palette
from ..theme.theme_profile import ThemeProfile, save_theme_profile
from .hub_view import HubView
from .svg_icons import load_multicolor_svg_pixmap, load_svg_icon, load_svg_pixmap
from .theme_studio import ThemeStudioView
from .viewer_view import MarkdownTabPage, PdfTabPage, ViewerView
from .widgets import AdaptiveButton, SlidingNavPanel, StatusTag


class MainWindow(QMainWindow):
    """Dual-workspace application shell honoring 1366x720 screen-safe sizing, Min/Max/Close, Fullscreen, and TRODAI UX."""

    def __init__(
        self,
        initial_profile: ThemeProfile,
        *,
        persist_profile: bool = True,
        guard: MotionPerformanceGuard | None = None,
        sample_pdf_dir: Path | None = None,
        insights_store: LocalUsageInsights | None = None,
        bookmarks_store: BookmarksStore | None = None,
        dictionary_engine: DictionaryEngine | None = None,
        notification_center: NotificationCenter | None = None,
        task_manager: TaskManager | None = None,
        crash_guard: CrashGuard | None = None,
        updater_state_path: Path | None = None,
    ) -> None:
        super().__init__()
        self._profile = initial_profile.sanitized(check_media_exists=False)
        self._persist_profile = persist_profile
        self._guard = guard or MotionPerformanceGuard()
        self._sample_pdf_dir = sample_pdf_dir
        self._insights = insights_store or LocalUsageInsights()
        self._bookmarks = bookmarks_store or BookmarksStore()
        self.bookmarks_store = self._bookmarks
        self._dict_engine = dictionary_engine or DictionaryEngine()
        self.dictionary_engine = self._dict_engine
        self.notification_center = notification_center or NotificationCenter()
        self.task_manager = task_manager or TaskManager(parent=self)
        self.crash_guard = crash_guard
        self._updater_state_path = updater_state_path
        self._active_release: ReleaseInfo | None = None
        self._ai_panel: QWidget | None = None
        self._is_fullscreen = False

        self.setWindowTitle(f"{APP_NAME} · by {COMPANY_NAME}")
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.WindowMinimizeButtonHint
            | Qt.WindowType.WindowMaximizeButtonHint
            | Qt.WindowType.WindowCloseButtonHint
        )
        self.setMinimumSize(920, 580)
        self._apply_screen_safe_initial_geometry()
        self.setAcceptDrops(True)

        ico_path = get_base_dir() / "assets" / "icons" / "pdf_lector.ico"
        if ico_path.exists():
            self.setWindowIcon(QIcon(str(ico_path)))

        # Root Canvas
        self.root_canvas = QWidget(self)
        self.root_canvas.setObjectName("RootCanvas")
        self.setCentralWidget(self.root_canvas)

        self._root_h_layout = QHBoxLayout(self.root_canvas)
        self._root_h_layout.setContentsMargins(0, 0, 0, 0)
        self._root_h_layout.setSpacing(0)

        # Compact Left Sidebar
        self.sidebar = QWidget(self.root_canvas)
        self.sidebar.setObjectName("Sidebar")
        self.sidebar.setFixedWidth(226)
        sb_layout = QVBoxLayout(self.sidebar)
        sb_layout.setContentsMargins(12, 14, 12, 12)
        sb_layout.setSpacing(8)

        # App Identity Header
        app_head = QHBoxLayout()
        app_head.setSpacing(10)
        self.app_logo_lbl = QLabel(self.sidebar)
        self.app_logo_lbl.setFixedSize(38, 38)
        app_head.addWidget(self.app_logo_lbl)

        app_txt_col = QVBoxLayout()
        app_txt_col.setSpacing(1)
        self.app_title_lbl = QLabel(APP_NAME, self.sidebar)
        at_f = QFont(UI_FONT_FAMILY, 13)
        at_f.setWeight(QFont.Weight.Bold)
        self.app_title_lbl.setFont(at_f)
        app_txt_col.addWidget(self.app_title_lbl)

        self.app_ver_lbl = QLabel(f"v{read_version()}", self.sidebar)
        self.app_ver_lbl.setProperty("mono", "true")
        self.app_ver_lbl.setProperty("tone", "muted")
        self.app_ver_lbl.setFont(QFont(MONO_FONT_FAMILY, 8))
        app_txt_col.addWidget(self.app_ver_lbl)
        app_head.addLayout(app_txt_col, 1)
        sb_layout.addLayout(app_head)

        self.open_sidebar_btn = AdaptiveButton("", self.sidebar, role="PrimaryAction")
        self.open_sidebar_btn.clicked.connect(self._open_document_dialog)
        sb_layout.addWidget(self.open_sidebar_btn)

        nav_sep = QFrame(self.sidebar)
        nav_sep.setFixedHeight(1)
        nav_sep.setStyleSheet("background-color: #334155;")
        sb_layout.addWidget(nav_sep)

        self.nav_panel = SlidingNavPanel(self.sidebar)
        self.nav_panel.set_items([
            ("home", ""),
            ("viewer", ""),
            ("palette", ""),
        ])
        self.nav_panel.current_changed.connect(self.switch_view)
        sb_layout.addWidget(self.nav_panel)

        sb_layout.addStretch(1)

        # Separated Company Seal Footer ("by TRODAI" + Hexa-Vault Sentinel)
        seal_sep = QFrame(self.sidebar)
        seal_sep.setFixedHeight(1)
        seal_sep.setStyleSheet("background-color: #334155;")
        sb_layout.addWidget(seal_sep)

        self.privacy_tag = StatusTag("PROCESO LOCAL", tone="ok", parent=self.sidebar)
        sb_layout.addWidget(self.privacy_tag)

        seal_row = QHBoxLayout()
        seal_row.setContentsMargins(2, 4, 2, 2)
        seal_row.setSpacing(8)
        self.trodai_sentinel_lbl = QLabel(self.sidebar)
        self.trodai_sentinel_lbl.setFixedSize(20, 20)
        seal_row.addWidget(self.trodai_sentinel_lbl)

        self.company_lbl = QLabel(f"by {COMPANY_NAME}", self.sidebar)
        co_f = QFont(UI_FONT_FAMILY, 10)
        co_f.setWeight(QFont.Weight.Medium)
        co_f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.7)
        self.company_lbl.setFont(co_f)
        seal_row.addWidget(self.company_lbl, 1)
        sb_layout.addLayout(seal_row)

        self._root_h_layout.addWidget(self.sidebar)

        # Right Column: Minimalist TopBar + Solid Update Banner + Stacked Dual Workspace
        main_col = QVBoxLayout()
        main_col.setContentsMargins(0, 0, 0, 0)
        main_col.setSpacing(0)

        self.topbar = QWidget(self.root_canvas)
        self.topbar.setObjectName("TopBar")
        self.topbar.setFixedHeight(50)
        tb_layout = QHBoxLayout(self.topbar)
        tb_layout.setContentsMargins(14, 8, 14, 8)
        tb_layout.setSpacing(8)

        self.top_search = QLineEdit(self.topbar)
        self.top_search.setMinimumWidth(140)
        self.top_search.setMaximumWidth(280)
        self.top_search.textChanged.connect(self._on_top_search)
        tb_layout.addWidget(self.top_search, 1)

        tb_layout.addStretch(1)

        self.top_studio_btn = AdaptiveButton("", self.topbar)
        self.top_studio_btn.setObjectName("TopUtilityBtn")
        self.top_studio_btn.clicked.connect(lambda: self.switch_view(2))
        tb_layout.addWidget(self.top_studio_btn)

        self.top_ai_btn = QPushButton("", self.topbar)
        self.top_ai_btn.setObjectName("TopUtilityBtn")
        self.top_ai_btn.setFixedSize(34, 30)
        self.top_ai_btn.setCheckable(True)
        self.top_ai_btn.setToolTip("Preguntar al documento (IA)")
        self.top_ai_btn.clicked.connect(self.toggle_ai_panel)
        tb_layout.addWidget(self.top_ai_btn)

        self.top_notif_btn = QPushButton("", self.topbar)
        self.top_notif_btn.setObjectName("TopUtilityBtn")
        self.top_notif_btn.setFixedSize(34, 30)
        self.top_notif_btn.setToolTip("Notificaciones y Tareas")
        self.top_notif_btn.clicked.connect(self.open_notifications_dialog)
        tb_layout.addWidget(self.top_notif_btn)

        self.top_menu_btn = QPushButton("", self.topbar)
        self.top_menu_btn.setObjectName("TopUtilityBtn")
        self.top_menu_btn.setFixedSize(34, 30)
        self.top_menu_btn.setToolTip("Opciones, Soporte y Acerca de")
        self._build_utility_menu()
        tb_layout.addWidget(self.top_menu_btn)

        self.top_fullscreen_btn = QPushButton("", self.topbar)
        self.top_fullscreen_btn.setObjectName("TopUtilityBtn")
        self.top_fullscreen_btn.setFixedSize(34, 30)
        self.top_fullscreen_btn.clicked.connect(self.toggle_fullscreen)
        tb_layout.addWidget(self.top_fullscreen_btn)

        self.top_theme_btn = QPushButton("", self.topbar)
        self.top_theme_btn.setObjectName("TopUtilityBtn")
        self.top_theme_btn.setFixedSize(34, 30)
        self.top_theme_btn.clicked.connect(self._toggle_dark_light)
        tb_layout.addWidget(self.top_theme_btn)

        self.lang_combo = QComboBox(self.topbar)
        self.lang_combo.setFixedSize(64, 30)
        self.lang_combo.addItem("ES", "es")
        self.lang_combo.addItem("EN", "en")
        self.lang_combo.addItem("PT", "pt")
        lang_idx = max(0, self.lang_combo.findData(self._profile.language))
        self.lang_combo.setCurrentIndex(lang_idx)
        self.lang_combo.currentIndexChanged.connect(self._on_language_changed)
        tb_layout.addWidget(self.lang_combo)

        main_col.addWidget(self.topbar)

        # Solid Opaque Top Update Banner (zero rgba)
        self.update_banner = QFrame(self.root_canvas)
        self.update_banner.setObjectName("UpdateBanner")
        self.update_banner.setVisible(False)
        ub_lay = QHBoxLayout(self.update_banner)
        ub_lay.setContentsMargins(14, 8, 14, 8)
        ub_lay.setSpacing(8)

        self.update_banner_lbl = QLabel("", self.update_banner)
        self.update_banner_lbl.setFont(QFont(UI_FONT_FAMILY, 9, QFont.Weight.Medium))
        self.update_banner_lbl.setWordWrap(True)
        ub_lay.addWidget(self.update_banner_lbl, 1)

        self.ub_notes_btn = AdaptiveButton("Ver novedades", self.update_banner, role="GhostAction")
        self.ub_notes_btn.clicked.connect(self.open_about_updater_dialog)
        ub_lay.addWidget(self.ub_notes_btn)

        self.ub_install_btn = AdaptiveButton("Descargar e instalar", self.update_banner, role="PrimaryAction")
        self.ub_install_btn.clicked.connect(self.open_about_updater_dialog)
        ub_lay.addWidget(self.ub_install_btn)

        self.ub_skip_btn = AdaptiveButton("Omitir esta version", self.update_banner, role="GhostAction")
        self.ub_skip_btn.clicked.connect(self._skip_active_release)
        ub_lay.addWidget(self.ub_skip_btn)

        self.ub_close_btn = QPushButton("", self.update_banner)
        self.ub_close_btn.setFixedSize(28, 28)
        self.ub_close_btn.clicked.connect(self.update_banner.hide)
        ub_lay.addWidget(self.ub_close_btn)
        main_col.addWidget(self.update_banner)

        # Workspace Stack
        self.stack = QStackedWidget(self.root_canvas)
        self.hub_view = HubView(insights=self._insights, guard=self._guard, parent=self.stack)
        self.viewer_view = ViewerView(
            bookmarks_store=self._bookmarks,
            dictionary_engine=self._dict_engine,
            guard=self._guard,
            parent=self.stack,
        )
        self.studio_view = ThemeStudioView(self._profile, guard=self._guard, parent=self.stack)

        self.stack.addWidget(self.hub_view)
        self.stack.addWidget(self.viewer_view)
        self.stack.addWidget(self.studio_view)
        main_col.addWidget(self.stack, 1)

        self._root_h_layout.addLayout(main_col, 1)

        self.transitions = ViewTransitionController(self.stack, guard=self._guard)

        # Wire cross-view actions
        self.hub_view.open_pdf_requested.connect(self._open_document_dialog)
        self.hub_view.open_studio_requested.connect(lambda: self.switch_view(2))
        self.hub_view.tool_activated.connect(self.open_tool_workspace)
        self.studio_view.profile_changed.connect(self.apply_profile)
        self.viewer_view.backdrop_toggled.connect(self._on_viewer_backdrop_toggled)
        self.viewer_view.fullscreen_requested.connect(self.toggle_fullscreen)
        self.viewer_view.document_inspected.connect(self._on_document_inspected)
        self.viewer_view.dictionary_lookup_recorded.connect(self._insights.record_dictionary_lookup)

        # Keyboard shortcuts: Ctrl+K search, F11 fullscreen, Esc exit fullscreen
        self._search_shortcut = QShortcut(QKeySequence("Ctrl+K"), self)
        self._search_shortcut.activated.connect(self._focus_search)

        self._f11_shortcut = QShortcut(QKeySequence("F11"), self)
        self._f11_shortcut.activated.connect(self.toggle_fullscreen)

        self._esc_shortcut = QShortcut(QKeySequence("Escape"), self)
        self._esc_shortcut.activated.connect(self._exit_fullscreen_if_active)

        self.retranslate(self._profile.language)
        self.apply_profile(self._profile, save=False)
        self.switch_view(0, immediate=True)

    def _build_utility_menu(self) -> None:
        menu = QMenu(self.top_menu_btn)
        act_prefs = QAction("Preferencias y Privacidad", menu)
        act_prefs.triggered.connect(self.open_preferences_dialog)
        menu.addAction(act_prefs)

        act_inv = QAction("Facturas Duales (DIAN / Factur-X)", menu)
        act_inv.triggered.connect(self.open_invoice_dialog)
        menu.addAction(act_inv)

        act_diag = QAction("Diagnostico y Logs", menu)
        act_diag.triggered.connect(self.open_diagnostics_dialog)
        menu.addAction(act_diag)

        act_sup = QAction("Centro de Soporte", menu)
        act_sup.triggered.connect(self.open_support_dialog)
        menu.addAction(act_sup)

        act_tour = QAction("Recorrido guiado", menu)
        act_tour.triggered.connect(self.open_tour_dialog)
        menu.addAction(act_tour)

        menu.addSeparator()
        act_about = QAction("Acerca de y Actualizaciones", menu)
        act_about.triggered.connect(self.open_about_updater_dialog)
        menu.addAction(act_about)

        self.top_menu_btn.setMenu(menu)
        self._utility_menu = menu

    def _apply_screen_safe_initial_geometry(self) -> None:
        screen = QApplication.primaryScreen()
        if screen is None:
            self.resize(1366, 720)
            return
        avail = screen.availableGeometry()
        w = min(1366, max(920, avail.width() - 36))
        h = min(720, max(580, avail.height() - 40))
        self.resize(w, h)
        x = avail.x() + max(0, (avail.width() - w) // 2)
        y = avail.y() + max(0, (avail.height() - h) // 2)
        self.move(x, y)

    def ensure_sample_pdf_loaded(self) -> Path:
        base = self._sample_pdf_dir or (Path(tempfile.gettempdir()) / "TRODAI_PDFLector")
        sample = generate_sample_pdf(base / "Muestra_TRODAI.pdf")
        self.viewer_view.open_pdf(sample, title_override=t("viewer.sample_tab", self._profile.language))
        return sample

    def open_document_path(self, path: str | Path, *, switch_to_viewer: bool = True) -> bool:
        """Open a validated local .pdf, .md, or .markdown file into ViewerView."""
        ok = self.viewer_view.open_document(path)
        if ok and switch_to_viewer:
            self.switch_view(1)
        return ok

    def open_pdf_file(self, pdf_path: Path, *, switch_to_viewer: bool = True) -> bool:
        return self.open_document_path(pdf_path, switch_to_viewer=switch_to_viewer)

    def has_unsaved_changes(self) -> bool:
        return self.viewer_view.has_unsaved_changes()

    def can_restart_for_update(self) -> bool:
        """Return True only if zero tabs have unsaved changes and zero background tasks are active."""
        return (not self.has_unsaved_changes()) and (len(self.task_manager.active_tasks()) == 0)

    def get_active_document_callback(self) -> tuple[Path | None, str]:
        """Provide (active_doc_path, extracted_text) lazily to AskDocumentPanel."""
        tab = self.viewer_view.current_tab()
        if isinstance(tab, PdfTabPage):
            chunks = [tab.session.extract_page_text(i) for i in range(min(20, tab.session.page_count))]
            return tab.pdf_path, "\n".join(chunks)
        if isinstance(tab, MarkdownTabPage):
            return tab.md_path, tab.editor_widget.toPlainText()
        return None, ""

    def toggle_ai_panel(self) -> None:
        """Lazily import and toggle ui.ai_panel.AskDocumentPanel on user action."""
        if self._ai_panel is None or not shiboken6.isValid(self._ai_panel):
            try:
                from .ai_panel import AskDocumentPanel  # noqa: PLC0415

                panel = AskDocumentPanel(
                    get_active_document_callback=self.get_active_document_callback,
                    parent=self.root_canvas,
                )
                self._root_h_layout.addWidget(panel)
                self._ai_panel = panel
            except ImportError:
                self.top_ai_btn.setChecked(False)
                QMessageBox.information(
                    self,
                    "Preguntar al documento",
                    "El panel de consulta IA se activara cuando el modulo ai_panel este instalado.",
                )
                return
        want = not self._ai_panel.isVisible()
        self._ai_panel.setVisible(want)
        self.top_ai_btn.setChecked(want)

    def open_tool_workspace(self, tool_id: str) -> None:
        if tool_id in ("create_visual_invoice", "create_einvoice", "validate_einvoice"):
            self.open_invoice_dialog()
            return
        try:
            defn = ToolRegistry.get(tool_id)
        except KeyError:
            return
        from .tool_workspace import ToolWorkspace

        dlg = QDialog(self)
        dlg.setWindowTitle(t(defn.title_key, self._profile.language))
        dlg.setMinimumSize(680, 520)
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(0, 0, 0, 0)
        ws = ToolWorkspace(defn, lang=self._profile.language, mode=self._profile.base_mode, parent=dlg)
        lay.addWidget(ws)
        dlg.exec()

    def open_invoice_dialog(self) -> None:
        from .invoice_dialog import InvoiceDialog

        InvoiceDialog(lang=self._profile.language, mode=self._profile.base_mode, parent=self).exec()

    def open_preferences_dialog(self) -> None:
        from .preferences_dialog import PreferencesDialog

        PreferencesDialog(
            insights_store=self._insights,
            lang=self._profile.language,
            mode=self._profile.base_mode,
            parent=self,
        ).exec()
        QTimer.singleShot(0, self.hub_view.refresh_recommendations)

    def open_notifications_dialog(self) -> None:
        from .notifications_drawer import NotificationsTasksDialog

        NotificationsTasksDialog(
            notification_center=self.notification_center,
            task_manager=self.task_manager,
            lang=self._profile.language,
            mode=self._profile.base_mode,
            parent=self,
        ).exec()

    def open_support_dialog(self) -> None:
        from .support_dialog import SupportDialog

        logs = self.crash_guard.read_crash_log_tail(4000) if self.crash_guard is not None else ""
        SupportDialog(
            initial_logs=logs,
            crash_guard=self.crash_guard,
            lang=self._profile.language,
            mode=self._profile.base_mode,
            parent=self,
        ).exec()

    def open_diagnostics_dialog(self) -> None:
        from .diagnostics_dialog import DiagnosticsDialog

        DiagnosticsDialog(
            crash_guard=self.crash_guard,
            lang=self._profile.language,
            mode=self._profile.base_mode,
            parent=self,
        ).exec()

    def open_about_updater_dialog(self) -> None:
        from .about_updater_dialog import AboutUpdaterDialog

        AboutUpdaterDialog(
            state_path=self._updater_state_path,
            has_unsaved_fn=self.has_unsaved_changes,
            has_active_tasks_fn=lambda: len(self.task_manager.active_tasks()) > 0,
            lang=self._profile.language,
            mode=self._profile.base_mode,
            parent=self,
        ).exec()

    def open_tour_dialog(self) -> None:
        from .tour_dialog import TourDialog

        TourDialog(lang=self._profile.language, mode=self._profile.base_mode, parent=self).exec()

    def show_update_banner(self, release_info: ReleaseInfo) -> None:
        """Show solid opaque top update banner for a detected ReleaseInfo."""
        self._active_release = release_info
        self.update_banner_lbl.setText(
            f"Nueva version disponible: v{release_info.version} (actual v{read_version()}) · Verificacion SHA-256"
        )
        self._apply_update_banner_theme()
        self.update_banner.setVisible(True)
        if not self.isVisible() and QApplication.platformName() == "offscreen":
            self.show()

    def skip_update_version(self, version: str) -> None:
        state = load_updater_state(self._updater_state_path)
        state.skipped_version = str(version).strip()
        save_updater_state(state, self._updater_state_path)
        self.update_banner.setVisible(False)

    def should_show_background_update(self, release_info: ReleaseInfo) -> bool:
        state = load_updater_state(self._updater_state_path)
        if state.offline_only or not release_info.update_available:
            return False
        if state.skipped_version and release_info.version == state.skipped_version:
            return False
        return True

    def _skip_active_release(self) -> None:
        if self._active_release is not None:
            self.skip_update_version(self._active_release.version)
        else:
            self.update_banner.setVisible(False)

    def _apply_update_banner_theme(self) -> None:
        p = get_palette(self._profile.base_mode)
        self.update_banner.setStyleSheet(
            f"QFrame#UpdateBanner {{ background-color: {p['bg_glass_elevated']}; "
            f"border-bottom: 1px solid {p['border_selected']}; }}"
        )
        self.update_banner_lbl.setStyleSheet(f"color: {p['text_primary']};")
        self.ub_close_btn.setIcon(load_svg_icon("close", color=p["icon_color"], size=13))

    def switch_view(self, index: int, *, immediate: bool = False) -> None:
        self.transitions.switch_to(index, self._profile, immediate=immediate)
        self.nav_panel.set_current_index(index, animate=(not immediate))

    @property
    def current_profile(self) -> ThemeProfile:
        return self._profile

    @property
    def is_fullscreen_mode(self) -> bool:
        return self._is_fullscreen

    def toggle_fullscreen(self) -> None:
        self.set_fullscreen_mode(not self._is_fullscreen)

    def _exit_fullscreen_if_active(self) -> None:
        if self._is_fullscreen:
            self.set_fullscreen_mode(False)

    def set_fullscreen_mode(self, enabled: bool) -> None:
        self._is_fullscreen = bool(enabled)
        if self._is_fullscreen:
            in_viewer = self.stack.currentIndex() == 1
            self.sidebar.setVisible(not in_viewer)
            self.topbar.setVisible(not in_viewer)
            self.showFullScreen()
        else:
            self.sidebar.setVisible(True)
            self.topbar.setVisible(True)
            self.showNormal()

        self.viewer_view.set_fullscreen_state(self._is_fullscreen)
        self._sync_fullscreen_icon()

    def _sync_fullscreen_icon(self) -> None:
        p = get_palette(self._profile.base_mode)
        icon = "exit_fullscreen" if self._is_fullscreen else "fullscreen"
        self.top_fullscreen_btn.setIcon(load_svg_icon(icon, color=p["icon_color"], size=15))

    def _on_document_inspected(self, sample_text: str, page_count: int, toc_count: int) -> None:
        self._insights.record_document_opened(sample_text, page_count=page_count, toc_count=toc_count)
        QTimer.singleShot(0, self.hub_view._safe_refresh_recommendations)

    def _focus_search(self) -> None:
        if self.stack.currentIndex() != 0:
            self.switch_view(0)
        self.hub_view.focus_search()

    def _on_top_search(self, text: str) -> None:
        if text.strip() and self.stack.currentIndex() != 0:
            self.switch_view(0)
        self.hub_view.set_external_filter(text)

    def _open_document_dialog(self) -> None:
        chosen, _ = QFileDialog.getOpenFileName(
            self,
            t("nav.open_pdf", self._profile.language),
            "",
            "Documentos (*.pdf *.md *.markdown);;PDF (*.pdf);;Markdown (*.md *.markdown)",
        )
        if chosen:
            self.open_document_path(Path(chosen), switch_to_viewer=True)

    def _toggle_dark_light(self) -> None:
        next_mode = "light" if self._profile.base_mode == "dark" else "dark"
        updated = replace(self._profile, base_mode=next_mode)
        self.apply_profile(updated)

    def _on_viewer_backdrop_toggled(self, enabled: bool) -> None:
        updated = replace(self._profile, apply_to_viewer_backdrop=bool(enabled))
        self.apply_profile(updated)

    def _on_language_changed(self, idx: int) -> None:
        lang = str(self.lang_combo.itemData(idx) or "es")
        updated = replace(self._profile, language=lang)
        self.retranslate(lang)
        self.apply_profile(updated)

    def retranslate(self, lang: str) -> None:
        self.open_sidebar_btn.setText(t("nav.open_pdf", lang))
        self.nav_panel.update_labels([
            t("nav.hub", lang),
            t("nav.viewer", lang),
            t("nav.studio", lang),
        ])
        self.top_search.setPlaceholderText(t("topbar.search_placeholder", lang))
        self.top_studio_btn.setText(t("topbar.theme_studio", lang))
        self.top_fullscreen_btn.setToolTip(t("topbar.fullscreen", lang))
        self.privacy_tag.setText(t("sidebar.privacy_tag", lang))

        self.hub_view.retranslate(lang)
        self.viewer_view.retranslate(lang)
        self.studio_view.retranslate(lang)

    def apply_profile(self, profile: ThemeProfile, *, save: bool = True) -> None:
        self._profile = profile.sanitized(check_media_exists=False)
        p = get_palette(self._profile.base_mode)

        app = QApplication.instance()
        if isinstance(app, QApplication):
            app.setStyleSheet(build_trodai_qss(self._profile.base_mode))

        try:
            apply_trodai_window_chrome(
                int(self.winId()),
                dark_mode=(self._profile.base_mode == "dark"),
                acrylic=False,
            )
        except Exception:
            pass

        self.nav_panel.apply_theme(self._profile.base_mode)
        self.nav_panel.set_animations_enabled(self._profile.motion_level != "static")

        self.app_logo_lbl.setPixmap(load_multicolor_svg_pixmap("pdf_lector_logo", size=38))
        self.trodai_sentinel_lbl.setPixmap(
            load_svg_pixmap("trodai_sentinel", color=p["text_secondary"], size=18)
        )

        self.open_sidebar_btn.setIcon(load_svg_icon("open", color=p["primary_fg"], size=15))
        self.top_studio_btn.setIcon(load_svg_icon("palette", color=p["icon_color"], size=15))
        self.top_ai_btn.setIcon(load_svg_icon("ai_spark", color=p["icon_color"], size=15))
        self.top_notif_btn.setIcon(load_svg_icon("bell", color=p["icon_color"], size=15))
        self.top_menu_btn.setIcon(load_svg_icon("settings", color=p["icon_color"], size=15))
        self._sync_fullscreen_icon()

        theme_icon = "sun" if self._profile.base_mode == "dark" else "moon"
        theme_tip = t(
            "topbar.toggle_light" if self._profile.base_mode == "dark" else "topbar.toggle_dark",
            self._profile.language,
        )
        self.top_theme_btn.setIcon(load_svg_icon(theme_icon, color=p["icon_color"], size=16))
        self.top_theme_btn.setToolTip(theme_tip)
        self.privacy_tag.apply_theme(self._profile.base_mode)
        self._apply_update_banner_theme()

        self.hub_view.apply_profile(self._profile)
        self.viewer_view.apply_profile(self._profile)
        self.studio_view.sync_from_profile(self._profile)

        if save and self._persist_profile:
            save_theme_profile(self._profile)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                if url.isLocalFile():
                    suf = Path(url.toLocalFile()).suffix.lower()
                    if suf in (".pdf", ".md", ".markdown") or suf in ALLOWED_MEDIA_EXTENSIONS:
                        event.acceptProposedAction()
                        return
        super().dragEnterEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                if not url.isLocalFile():
                    continue
                local_path = Path(url.toLocalFile())
                suf = local_path.suffix.lower()
                if suf in (".pdf", ".md", ".markdown"):
                    ok, _ = validate_local_any_doc(local_path, check_exists=True)
                    if ok and self.open_document_path(local_path, switch_to_viewer=True):
                        event.acceptProposedAction()
                        return
                elif suf in ALLOWED_MEDIA_EXTENSIONS:
                    if self.studio_view.apply_banner_path(str(local_path)):
                        self.switch_view(2)
                        event.acceptProposedAction()
                        return
        super().dropEvent(event)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        try:
            apply_trodai_window_chrome(
                int(self.winId()),
                dark_mode=(self._profile.base_mode == "dark"),
                acrylic=False,
            )
        except Exception:
            pass

    def closeEvent(self, event) -> None:
        self.hub_view.cleanup()
        self.viewer_view.cleanup()
        self.studio_view.cleanup()
        super().closeEvent(event)
