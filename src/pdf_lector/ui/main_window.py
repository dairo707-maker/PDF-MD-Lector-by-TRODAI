"""Main window shell for PDF/MD Lector by TRODAI: Collapsible Ribbon Sidebar, Recents, Preferences, AI Chat & Tool Host."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, QSize, QThreadPool, QTimer, Qt, Signal
from PySide6.QtGui import QAction, QDragEnterEvent, QDropEvent, QFont, QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
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
from ..core.ai_chat_history import AiChatHistoryStore
from ..core.autosave import (
    AutosaveTabState,
    SessionAutosaveManager,
    SessionSnapshot,
    deserialize_annotation,
    serialize_annotation,
)
from ..core.bookmarks_store import BookmarksStore
from ..core.crash_guard import CrashGuard
from ..core.dictionary_engine import DictionaryEngine
from ..core.i18n import t
from ..core.notifications import NotificationCenter
from ..core.path_guard import ALLOWED_MEDIA_EXTENSIONS, validate_local_any_doc
from ..core.pdf_engine import generate_sample_pdf
from ..core.recent_docs import RecentDocumentsStore
from ..core.task_control import TaskManager
from ..core.tools import ToolRegistry
from ..core.updater import (
    DEFAULT_RELEASES_API_URL,
    ReleaseInfo,
    check_for_updates,
    load_updater_state,
    save_updater_state,
    should_run_auto_check,
)
from ..core.usage_insights import LocalUsageInsights
from ..core.win32_bridge import apply_trodai_window_chrome
from ..theme.motion_engine import MotionBackdropWidget, MotionPerformanceGuard, ViewTransitionController
from ..theme.palette import MONO_FONT_FAMILY, UI_FONT_FAMILY, build_trodai_qss, get_palette
from ..theme.theme_profile import ThemeProfile, save_theme_profile
from .hub_view import HubView
from .preferences_dialog import UserPreferences, load_preferences
from .svg_icons import load_multicolor_svg_pixmap, load_svg_icon, load_svg_pixmap
from .theme_studio import ThemeStudioView
from .viewer_view import MarkdownTabPage, PdfTabPage, ViewerView
from .widgets import AdaptiveButton, SlidingNavPanel, StatusTag


class _UpdateCheckSignals(QObject):
    finished = Signal(object)


class _BgUpdateCheckWorker(QRunnable):
    def __init__(self, current_version: str, api_url: str, state_path: Path | None) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self.signals = _UpdateCheckSignals()
        self._current_version = current_version
        self._api_url = api_url
        self._state_path = state_path

    def run(self) -> None:
        try:
            state = load_updater_state(self._state_path)
            if state.offline_only:
                self.signals.finished.emit(None)
                return
            rel = check_for_updates(self._current_version, api_url=self._api_url, offline_only=False)
            self.signals.finished.emit(rel)
        except Exception:
            self.signals.finished.emit(None)


class MainWindow(QMainWindow):
    """Dual-workspace application shell honoring 1366x720 screen-safe sizing, Adobe-style reader collapse, and TRODAI UX."""

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
        recent_store: RecentDocumentsStore | None = None,
        ai_history_store: AiChatHistoryStore | None = None,
        notification_center: NotificationCenter | None = None,
        task_manager: TaskManager | None = None,
        crash_guard: CrashGuard | None = None,
        updater_state_path: Path | None = None,
        autosave_dir: Path | None = None,
    ) -> None:
        super().__init__()
        self._profile = initial_profile.sanitized(check_media_exists=False)
        self._persist_profile = persist_profile
        self._applied_base_mode: str = ""
        self._guard = guard or MotionPerformanceGuard()
        self._sample_pdf_dir = sample_pdf_dir
        self._insights = insights_store or LocalUsageInsights()
        self._bookmarks = bookmarks_store or BookmarksStore()
        self.bookmarks_store = self._bookmarks
        self._dict_engine = dictionary_engine or DictionaryEngine()
        self.dictionary_engine = self._dict_engine
        self.recent_docs = recent_store or RecentDocumentsStore()
        self.ai_history_store = ai_history_store or AiChatHistoryStore()
        self.notification_center = notification_center or NotificationCenter()
        self.task_manager = task_manager or TaskManager(parent=self)
        self.crash_guard = crash_guard
        self._updater_state_path = updater_state_path
        self.autosave_manager = SessionAutosaveManager(base_dir=autosave_dir)
        self._active_release: ReleaseInfo | None = None
        self._update_workers: list[_BgUpdateCheckWorker] = []
        self._ai_panel: Any = None
        self._active_tool_workspace: Any = None
        self._is_fullscreen = False
        self._sidebar_collapsed = False
        self._prefs: UserPreferences = load_preferences()
        self._unsaved_close_prompt_fn: Callable[[list[str]], str] | None = None

        self._profile_save_timer = QTimer(self)
        self._profile_save_timer.setSingleShot(True)
        self._profile_save_timer.setInterval(250)
        self._profile_save_timer.timeout.connect(self._flush_profile_save)

        self._autosave_timer = QTimer(self)
        self._autosave_timer.setInterval(30_000)
        self._autosave_timer.timeout.connect(lambda: self.save_autosave_snapshot(clean_exit=False))
        if QApplication.platformName() != "offscreen":
            self._autosave_timer.start()

        self._autosave_debounce_timer = QTimer(self)
        self._autosave_debounce_timer.setSingleShot(True)
        self._autosave_debounce_timer.setInterval(3_000)
        self._autosave_debounce_timer.timeout.connect(lambda: self.save_autosave_snapshot(clean_exit=False))

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

        # Collapsible Left Sidebar (226px expanded <-> 40px ribbon tab in Viewer)
        self.sidebar = MotionBackdropWidget(
            self.root_canvas,
            guard=self._guard,
            is_viewer_surface=False,
            chrome_surface=True,
        )
        self.sidebar.setObjectName("Sidebar")
        self.sidebar.setFixedWidth(226)
        sidebar_root_lay = QVBoxLayout(self.sidebar)
        sidebar_root_lay.setContentsMargins(0, 0, 0, 0)
        sidebar_root_lay.setSpacing(0)

        # Expanded Host
        self._sidebar_expanded = QWidget(self.sidebar)
        self._sidebar_expanded.setObjectName("TransparentHost")
        sb_layout = QVBoxLayout(self._sidebar_expanded)
        sb_layout.setContentsMargins(12, 14, 12, 12)
        sb_layout.setSpacing(8)

        app_head = QHBoxLayout()
        app_head.setSpacing(8)
        self.app_logo_lbl = QLabel(self._sidebar_expanded)
        self.app_logo_lbl.setFixedSize(36, 36)
        app_head.addWidget(self.app_logo_lbl)

        app_txt_col = QVBoxLayout()
        app_txt_col.setSpacing(1)
        self.app_title_lbl = QLabel(APP_NAME, self._sidebar_expanded)
        at_f = QFont(UI_FONT_FAMILY, 12)
        at_f.setWeight(QFont.Weight.Bold)
        self.app_title_lbl.setFont(at_f)
        app_txt_col.addWidget(self.app_title_lbl)

        self.app_ver_lbl = QLabel(f"v{read_version()}", self._sidebar_expanded)
        self.app_ver_lbl.setProperty("mono", "true")
        self.app_ver_lbl.setProperty("tone", "muted")
        self.app_ver_lbl.setFont(QFont(MONO_FONT_FAMILY, 8))
        app_txt_col.addWidget(self.app_ver_lbl)
        app_head.addLayout(app_txt_col, 1)

        self.sidebar_collapse_btn = QPushButton("", self._sidebar_expanded)
        self.sidebar_collapse_btn.setProperty("role", "GhostAction")
        self.sidebar_collapse_btn.setProperty("iconOnly", "true")
        self.sidebar_collapse_btn.setFixedSize(30, 30)
        self.sidebar_collapse_btn.setIconSize(QSize(15, 15))
        self.sidebar_collapse_btn.setToolTip("Contraer panel lateral")
        self.sidebar_collapse_btn.clicked.connect(lambda: self.set_sidebar_collapsed(True))
        app_head.addWidget(self.sidebar_collapse_btn, 0, Qt.AlignmentFlag.AlignTop)
        sb_layout.addLayout(app_head)

        self.open_sidebar_btn = AdaptiveButton("", self._sidebar_expanded, role="PrimaryAction")
        self.open_sidebar_btn.clicked.connect(self._open_document_dialog)
        sb_layout.addWidget(self.open_sidebar_btn)

        nav_sep = QFrame(self._sidebar_expanded)
        nav_sep.setFixedHeight(1)
        nav_sep.setStyleSheet("background-color: #334155;")
        sb_layout.addWidget(nav_sep)

        self.nav_panel = SlidingNavPanel(self._sidebar_expanded)
        self.nav_panel.set_items([
            ("home", ""),
            ("viewer", ""),
            ("palette", ""),
        ])
        self.nav_panel.current_changed.connect(self.switch_view)
        sb_layout.addWidget(self.nav_panel)

        sb_layout.addStretch(1)

        seal_sep = QFrame(self._sidebar_expanded)
        seal_sep.setFixedHeight(1)
        seal_sep.setStyleSheet("background-color: #334155;")
        sb_layout.addWidget(seal_sep)

        self.privacy_tag = StatusTag("PROCESO LOCAL", tone="ok", parent=self._sidebar_expanded)
        sb_layout.addWidget(self.privacy_tag)

        seal_row = QHBoxLayout()
        seal_row.setContentsMargins(2, 4, 2, 2)
        seal_row.setSpacing(8)
        self.trodai_sentinel_lbl = QLabel(self._sidebar_expanded)
        self.trodai_sentinel_lbl.setFixedSize(20, 20)
        seal_row.addWidget(self.trodai_sentinel_lbl)

        self.company_lbl = QLabel(f"by {COMPANY_NAME}", self._sidebar_expanded)
        co_f = QFont(UI_FONT_FAMILY, 10)
        co_f.setWeight(QFont.Weight.Medium)
        co_f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.7)
        self.company_lbl.setFont(co_f)
        seal_row.addWidget(self.company_lbl, 1)
        sb_layout.addLayout(seal_row)
        sidebar_root_lay.addWidget(self._sidebar_expanded)

        # Collapsed Ribbon Host (42px Adobe-style slim vertical tab)
        self._sidebar_ribbon = QWidget(self.sidebar)
        self._sidebar_ribbon.setObjectName("SidebarRibbon")
        self._sidebar_ribbon.setVisible(False)
        rib_lay = QVBoxLayout(self._sidebar_ribbon)
        rib_lay.setContentsMargins(5, 10, 5, 10)
        rib_lay.setSpacing(8)
        rib_lay.setAlignment(Qt.AlignmentFlag.AlignHCenter)

        self.rib_expand_btn = QPushButton("", self._sidebar_ribbon)
        self.rib_expand_btn.setProperty("role", "GhostAction")
        self.rib_expand_btn.setProperty("iconOnly", "true")
        self.rib_expand_btn.setFixedSize(32, 32)
        self.rib_expand_btn.setIconSize(QSize(16, 16))
        self.rib_expand_btn.setToolTip("Desplegar menu lateral")
        self.rib_expand_btn.clicked.connect(lambda: self.set_sidebar_collapsed(False))
        rib_lay.addWidget(self.rib_expand_btn, 0, Qt.AlignmentFlag.AlignHCenter)

        self.rib_open_btn = QPushButton("", self._sidebar_ribbon)
        self.rib_open_btn.setProperty("role", "PrimaryAction")
        self.rib_open_btn.setProperty("iconOnly", "true")
        self.rib_open_btn.setFixedSize(32, 32)
        self.rib_open_btn.setIconSize(QSize(16, 16))
        self.rib_open_btn.clicked.connect(self._open_document_dialog)
        rib_lay.addWidget(self.rib_open_btn, 0, Qt.AlignmentFlag.AlignHCenter)

        rib_sep = QFrame(self._sidebar_ribbon)
        rib_sep.setFixedHeight(1)
        rib_sep.setFixedWidth(24)
        rib_sep.setStyleSheet("background-color: #334155;")
        rib_lay.addWidget(rib_sep, 0, Qt.AlignmentFlag.AlignHCenter)

        self.rib_nav_btns: list[QPushButton] = []
        for idx, _icon in enumerate(("home", "viewer", "palette")):
            rb = QPushButton("", self._sidebar_ribbon)
            rb.setProperty("role", "ModeTab")
            rb.setProperty("iconOnly", "true")
            rb.setCheckable(True)
            rb.setFixedSize(32, 32)
            rb.setIconSize(QSize(16, 16))
            rb.clicked.connect(lambda _=False, i=idx: self.switch_view(i))
            rib_lay.addWidget(rb, 0, Qt.AlignmentFlag.AlignHCenter)
            self.rib_nav_btns.append(rb)

        rib_lay.addStretch(1)
        self.rib_sentinel_lbl = QLabel(self._sidebar_ribbon)
        self.rib_sentinel_lbl.setFixedSize(20, 20)
        rib_lay.addWidget(self.rib_sentinel_lbl, 0, Qt.AlignmentFlag.AlignHCenter)
        sidebar_root_lay.addWidget(self._sidebar_ribbon)

        self._root_h_layout.addWidget(self.sidebar)

        # Right Column: Minimalist TopBar + Solid Update Banner + Stacked Workspace
        main_col = QVBoxLayout()
        main_col.setContentsMargins(0, 0, 0, 0)
        main_col.setSpacing(0)

        self.topbar = MotionBackdropWidget(
            self.root_canvas,
            guard=self._guard,
            is_viewer_surface=False,
            chrome_surface=True,
        )
        self.topbar.setObjectName("TopBar")
        self.topbar.setFixedHeight(48)
        tb_layout = QHBoxLayout(self.topbar)
        tb_layout.setContentsMargins(14, 0, 14, 0)
        tb_layout.setSpacing(6)

        self.top_search = QLineEdit(self.topbar)
        self.top_search.setMinimumWidth(160)
        self.top_search.setMaximumWidth(280)
        self.top_search.textChanged.connect(self._on_top_search)
        tb_layout.addWidget(self.top_search, 1, Qt.AlignmentFlag.AlignVCenter)

        tb_layout.addStretch(1)

        self.top_notif_btn = QPushButton("", self.topbar)
        self.top_notif_btn.setObjectName("TopUtilityBtn")
        self.top_notif_btn.setProperty("iconOnly", "true")
        self.top_notif_btn.setFixedSize(32, 32)
        self.top_notif_btn.setIconSize(QSize(16, 16))
        self.top_notif_btn.setToolTip("Notificaciones y Tareas")
        self.top_notif_btn.clicked.connect(self.open_notifications_dialog)
        tb_layout.addWidget(self.top_notif_btn, 0, Qt.AlignmentFlag.AlignVCenter)

        self.top_settings_btn = QPushButton("", self.topbar)
        self.top_settings_btn.setObjectName("TopUtilityBtn")
        self.top_settings_btn.setProperty("iconOnly", "true")
        self.top_settings_btn.setFixedSize(32, 32)
        self.top_settings_btn.setIconSize(QSize(16, 16))
        self.top_settings_btn.setToolTip("Configuracion y Preferencias")
        self.top_settings_btn.clicked.connect(self.open_preferences_dialog)
        tb_layout.addWidget(self.top_settings_btn, 0, Qt.AlignmentFlag.AlignVCenter)

        self.top_menu_btn = QPushButton("", self.topbar)
        self.top_menu_btn.setObjectName("TopUtilityBtn")
        self.top_menu_btn.setProperty("iconOnly", "true")
        self.top_menu_btn.setFixedSize(32, 32)
        self.top_menu_btn.setIconSize(QSize(16, 16))
        self.top_menu_btn.setToolTip("Mas herramientas, Soporte y Acerca de")
        self._build_utility_menu()
        tb_layout.addWidget(self.top_menu_btn, 0, Qt.AlignmentFlag.AlignVCenter)

        self.top_theme_btn = QPushButton("", self.topbar)
        self.top_theme_btn.setObjectName("TopUtilityBtn")
        self.top_theme_btn.setProperty("iconOnly", "true")
        self.top_theme_btn.setFixedSize(32, 32)
        self.top_theme_btn.setIconSize(QSize(16, 16))
        self.top_theme_btn.clicked.connect(self._toggle_dark_light)
        tb_layout.addWidget(self.top_theme_btn, 0, Qt.AlignmentFlag.AlignVCenter)

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

        # Solid Opaque Recovery Banner (after unexpected shutdown / autosave restore)
        self.recovery_banner = QFrame(self.root_canvas)
        self.recovery_banner.setObjectName("RecoveryBanner")
        self.recovery_banner.setVisible(False)
        rb_lay = QHBoxLayout(self.recovery_banner)
        rb_lay.setContentsMargins(14, 8, 14, 8)
        rb_lay.setSpacing(8)

        self.recovery_banner_lbl = QLabel("", self.recovery_banner)
        self.recovery_banner_lbl.setFont(QFont(UI_FONT_FAMILY, 9, QFont.Weight.Medium))
        self.recovery_banner_lbl.setWordWrap(True)
        rb_lay.addWidget(self.recovery_banner_lbl, 1)

        self.rec_save_all_btn = AdaptiveButton("Guardar todo", self.recovery_banner, role="PrimaryAction")
        self.rec_save_all_btn.clicked.connect(self._on_recovery_save_all)
        rb_lay.addWidget(self.rec_save_all_btn)

        self.rec_close_btn = QPushButton("", self.recovery_banner)
        self.rec_close_btn.setFixedSize(28, 28)
        self.rec_close_btn.clicked.connect(self.recovery_banner.hide)
        rb_lay.addWidget(self.rec_close_btn)
        main_col.addWidget(self.recovery_banner)

        # Workspace Stack (0=Hub, 1=Viewer, 2=Theme Studio, 3=Inline Tool Runner)
        self.stack = QStackedWidget(self.root_canvas)
        self.hub_view = HubView(
            insights=self._insights,
            recent_store=self.recent_docs,
            guard=self._guard,
            parent=self.stack,
        )
        self.viewer_view = ViewerView(
            bookmarks_store=self._bookmarks,
            dictionary_engine=self._dict_engine,
            guard=self._guard,
            offline_only_getter=self.is_offline_only,
            parent=self.stack,
        )
        self.studio_view = ThemeStudioView(self._profile, guard=self._guard, parent=self.stack)
        self.tool_host_page = QWidget(self.stack)
        self.tool_host_page.setObjectName("TransparentHost")
        self._tool_host_layout = QVBoxLayout(self.tool_host_page)
        self._tool_host_layout.setContentsMargins(0, 0, 0, 0)
        self._tool_host_layout.setSpacing(0)

        self.stack.addWidget(self.hub_view)
        self.stack.addWidget(self.viewer_view)
        self.stack.addWidget(self.studio_view)
        self.stack.addWidget(self.tool_host_page)
        main_col.addWidget(self.stack, 1)

        self._root_h_layout.addLayout(main_col, 1)

        self.transitions = ViewTransitionController(self.stack, guard=self._guard)

        # Wire cross-view actions
        self.hub_view.open_pdf_requested.connect(self._open_document_dialog)
        self.hub_view.open_studio_requested.connect(lambda: self.switch_view(2))
        self.hub_view.tool_activated.connect(self.open_tool_workspace)
        self.hub_view.recent_document_requested.connect(lambda p: self.open_document_path(p, switch_to_viewer=True))
        self.studio_view.profile_changed.connect(self.apply_profile)
        self.viewer_view.backdrop_toggled.connect(self._on_viewer_backdrop_toggled)
        self.viewer_view.fullscreen_requested.connect(self.toggle_fullscreen)
        self.viewer_view.document_inspected.connect(self._on_document_inspected)
        self.viewer_view.dictionary_lookup_recorded.connect(self._insights.record_dictionary_lookup)
        self.viewer_view.ai_panel_requested.connect(self.toggle_ai_panel)
        self.viewer_view.active_document_changed.connect(self._on_active_document_changed)
        self.viewer_view.document_opened.connect(self._on_viewer_document_opened)
        self.viewer_view.document_edited.connect(self._schedule_debounced_autosave)
        self.viewer_view.tabs.currentChanged.connect(lambda _idx: self._schedule_debounced_autosave())

        # Keyboard shortcuts: Ctrl+O open, Ctrl+K search, F1 shortcuts, F11 fullscreen, Esc exit fullscreen
        self._open_shortcut = QShortcut(QKeySequence("Ctrl+O"), self)
        self._open_shortcut.activated.connect(self._open_document_dialog)

        self._search_shortcut = QShortcut(QKeySequence("Ctrl+K"), self)
        self._search_shortcut.activated.connect(self._focus_search)

        self._shortcuts_shortcut = QShortcut(QKeySequence("F1"), self)
        self._shortcuts_shortcut.activated.connect(self.open_shortcuts_dialog)

        self._f11_shortcut = QShortcut(QKeySequence("F11"), self)
        self._f11_shortcut.activated.connect(self.toggle_fullscreen)

        self._esc_shortcut = QShortcut(QKeySequence("Escape"), self)
        self._esc_shortcut.activated.connect(self._exit_fullscreen_if_active)

        self.retranslate(self._profile.language)
        self.apply_profile(self._profile, save=False)
        self._apply_user_preferences(self._prefs)
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

        act_shortcuts = QAction("Atajos de teclado y presentacion (F1)", menu)
        act_shortcuts.triggered.connect(self.open_shortcuts_dialog)
        menu.addAction(act_shortcuts)

        act_tour = QAction("Recorrido guiado", menu)
        act_tour.triggered.connect(self.open_tour_dialog)
        menu.addAction(act_tour)

        menu.addSeparator()
        act_about = QAction("Acerca de y Actualizaciones", menu)
        act_about.triggered.connect(self.open_about_updater_dialog)
        menu.addAction(act_about)

        self.top_menu_btn.setMenu(menu)
        self._utility_menu = menu

    def open_shortcuts_dialog(self) -> None:
        from .shortcuts_dialog import ShortcutsDialog

        dlg = ShortcutsDialog(mode=self._profile.base_mode, parent=self)
        dlg.exec()

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

    def set_sidebar_collapsed(self, collapsed: bool) -> None:
        want = bool(collapsed)
        self._sidebar_collapsed = want
        self._sidebar_expanded.setVisible(not want)
        self._sidebar_ribbon.setVisible(want)
        self.sidebar.setFixedWidth(44 if want else 226)

    def is_sidebar_collapsed(self) -> bool:
        return self._sidebar_collapsed

    def _apply_user_preferences(self, prefs: UserPreferences) -> None:
        self._prefs = prefs
        self.viewer_view.set_show_button_labels(prefs.show_button_labels)
        self.viewer_view.set_continuous_scroll(prefs.continuous_scroll)

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

    def is_offline_only(self) -> bool:
        try:
            return bool(load_updater_state(self._updater_state_path).offline_only)
        except Exception:
            return False

    def _schedule_debounced_autosave(self) -> None:
        if QApplication.platformName() != "offscreen" and shiboken6.isValid(self):
            self._autosave_debounce_timer.start()

    def save_all_documents(self) -> int:
        saved = 0
        for idx in range(self.viewer_view.tabs.count()):
            tab = self.viewer_view.tabs.widget(idx)
            if isinstance(tab, (PdfTabPage, MarkdownTabPage)) and tab.has_unsaved_changes():
                if tab.save_document():
                    saved += 1
        self.viewer_view._on_tab_state_changed()
        self.save_autosave_snapshot(clean_exit=False)
        return saved

    def _on_recovery_save_all(self) -> None:
        self.save_all_documents()
        self.recovery_banner.setVisible(False)

    def save_autosave_snapshot(self, *, clean_exit: bool = False) -> Path:
        tabs_payload: list[AutosaveTabState] = []
        for idx in range(self.viewer_view.tabs.count()):
            w = self.viewer_view.tabs.widget(idx)
            if isinstance(w, PdfTabPage):
                vbar = w.scroll.verticalScrollBar()
                dirty = w.has_unsaved_changes()
                annots = [serialize_annotation(a) for a in w.shared_annotations.records] if dirty else []
                tabs_payload.append(
                    AutosaveTabState(
                        doc_path=str(w.pdf_path),
                        doc_type="pdf",
                        page_index=int(w.current_page),
                        zoom=float(w.zoom),
                        rotation=int(w.rotation),
                        scroll_y=int(vbar.value()) if vbar is not None else 0,
                        dirty=dirty,
                        annotations=annots,
                    )
                )
            elif isinstance(w, MarkdownTabPage):
                dirty = w.has_unsaved_changes()
                tabs_payload.append(
                    AutosaveTabState(
                        doc_path=str(w.md_path),
                        doc_type="md",
                        dirty=dirty,
                        markdown_text=w.editor_widget.toPlainText() if dirty else None,
                    )
                )
        snap = SessionSnapshot(
            clean_exit=bool(clean_exit),
            active_index=max(0, self.viewer_view.tabs.currentIndex()),
            tabs=tabs_payload,
        )
        return self.autosave_manager.save_snapshot(snap)

    def recover_previous_session(self, *, previous_crashed: bool = False, force: bool = False) -> int:
        if not force and not self.autosave_manager.should_offer_recovery(previous_crashed=previous_crashed):
            return 0
        snap = self.autosave_manager.load_snapshot()
        if snap is None or not snap.tabs:
            return 0

        restored = 0
        has_dirty_recovered = False
        for tstate in snap.tabs:
            opened = self.viewer_view.open_document(Path(tstate.doc_path))
            if not opened:
                continue
            restored += 1
            tab = self.viewer_view.current_tab()
            if isinstance(tab, PdfTabPage):
                if abs(tab.zoom - tstate.zoom) > 0.01:
                    tab.set_zoom(tstate.zoom)
                if tstate.rotation % 360 != tab.rotation:
                    tab.rotation = tstate.rotation % 360
                    tab._rendered_signatures.clear()
                    for c in tab._page_canvases:
                        c.set_rotation_and_zoom(tab.rotation, tab.zoom)
                if 0 <= tstate.page_index < tab.session.page_count:
                    tab.set_page(tstate.page_index)
                if tstate.scroll_y > 0:
                    tab.scroll.verticalScrollBar().setValue(tstate.scroll_y)
                if tstate.dirty and tstate.annotations:
                    tab.shared_annotations.records = [
                        deserialize_annotation(a) for a in tstate.annotations
                    ]
                    tab.shared_annotations.dirty = True
                    tab.shared_annotations.notify()
                    has_dirty_recovered = True
            elif isinstance(tab, MarkdownTabPage):
                if tstate.dirty and tstate.markdown_text is not None:
                    tab.editor_widget.setPlainText(tstate.markdown_text)
                    has_dirty_recovered = True

        if restored > 0:
            if 0 <= snap.active_index < self.viewer_view.tabs.count():
                self.viewer_view.tabs.setCurrentIndex(snap.active_index)
            self.viewer_view._on_tab_state_changed()
            self.switch_view(1, immediate=True)
            msg = "Se recupero tu ultima sesion autoguardada tras un cierre inesperado."
            if has_dirty_recovered:
                msg += " Tienes cambios sin guardar restaurados."
            self.recovery_banner_lbl.setText(msg)
            self._apply_update_banner_theme()
            self.recovery_banner.setVisible(True)
        return restored

    def start_background_update_check(
        self,
        *,
        api_url: str = DEFAULT_RELEASES_API_URL,
        current_version: str | None = None,
        force: bool = False,
    ) -> bool:
        state = load_updater_state(self._updater_state_path)
        if state.offline_only:
            return False
        if not force and not should_run_auto_check(state):
            return False
        worker = _BgUpdateCheckWorker(
            current_version=current_version or read_version(),
            api_url=api_url,
            state_path=self._updater_state_path,
        )
        self._update_workers.append(worker)
        worker.signals.finished.connect(self._on_bg_update_finished, Qt.ConnectionType.QueuedConnection)
        QThreadPool.globalInstance().start(worker)
        return True

    def _on_bg_update_finished(self, rel: Any) -> None:
        self._update_workers.clear()
        if not shiboken6.isValid(self) or rel is None or not isinstance(rel, ReleaseInfo):
            return
        if self.should_show_background_update(rel):
            self.show_update_banner(rel)

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

    def _on_active_document_changed(self, doc_path: Any) -> None:
        if self._ai_panel is not None and shiboken6.isValid(self._ai_panel):
            self._ai_panel.set_active_document(doc_path if isinstance(doc_path, Path) else None)

    def _on_viewer_document_opened(self, doc_path: Any, page_count: int) -> None:
        if isinstance(doc_path, Path):
            self.recent_docs.record_open(doc_path, page_count=page_count)
            QTimer.singleShot(0, self.hub_view.refresh_recents)

    def _on_ai_citation_clicked(self, page_index: int, _raw: str) -> None:
        tab = self.viewer_view.current_tab()
        if isinstance(tab, PdfTabPage) and page_index >= 0:
            self.switch_view(1)
            tab.set_page(page_index)

    def _close_ai_panel(self) -> None:
        if self._ai_panel is not None and shiboken6.isValid(self._ai_panel):
            self._ai_panel.setVisible(False)
        self.viewer_view.set_ai_panel_checked(False)

    def toggle_ai_panel(self) -> None:
        """Lazily import and toggle ui.ai_panel.AskDocumentPanel on user action."""
        if self._ai_panel is None or not shiboken6.isValid(self._ai_panel):
            try:
                from .ai_panel import AskDocumentPanel  # noqa: PLC0415

                panel = AskDocumentPanel(
                    get_active_document_callback=self.get_active_document_callback,
                    mode=self._profile.base_mode,
                    lang=self._profile.language,
                    history_store=self.ai_history_store,
                    parent=self.root_canvas,
                )
                panel.close_requested.connect(self._close_ai_panel)
                panel.citation_clicked.connect(self._on_ai_citation_clicked)
                self._root_h_layout.addWidget(panel)
                self._ai_panel = panel
            except ImportError:
                self.viewer_view.set_ai_panel_checked(False)
                QMessageBox.information(
                    self,
                    "Preguntar IA",
                    "El panel de consulta IA se activara cuando el modulo ai_panel este instalado.",
                )
                return
        want = not self._ai_panel.isVisible()
        if want:
            self._ai_panel.apply_theme(self._profile.base_mode)
            self._ai_panel.retranslate(self._profile.language)
            self._ai_panel.refresh_active_document()
        self._ai_panel.setVisible(want)
        self.viewer_view.set_ai_panel_checked(want)

    def open_tool_workspace(self, tool_id: str) -> None:
        if tool_id in ("create_visual_invoice", "create_einvoice", "validate_einvoice"):
            self.open_invoice_dialog()
            return
        try:
            defn = ToolRegistry.get(tool_id)
        except Exception as exc:
            QMessageBox.warning(
                self,
                APP_NAME,
                f"No se pudo abrir la herramienta '{tool_id}': {exc}",
            )
            return

        from .tool_workspace import ToolWorkspace

        while self._tool_host_layout.count():
            item = self._tool_host_layout.takeAt(0)
            w = item.widget()
            if w is not None and shiboken6.isValid(w):
                w.hide()
                w.setParent(None)
                w.deleteLater()

        ws = ToolWorkspace(
            defn,
            lang=self._profile.language,
            mode=self._profile.base_mode,
            show_back=True,
            parent=self.tool_host_page,
        )
        ws.back_requested.connect(lambda: self.switch_view(0))
        ws.open_output_requested.connect(lambda p: self.open_document_path(Path(p), switch_to_viewer=True))
        self._tool_host_layout.addWidget(ws, 1)
        self._active_tool_workspace = ws
        self.switch_view(3)

    def open_invoice_dialog(self) -> None:
        from .invoice_dialog import InvoiceDialog

        InvoiceDialog(lang=self._profile.language, mode=self._profile.base_mode, parent=self).exec()

    def open_preferences_dialog(self) -> None:
        from .preferences_dialog import PreferencesDialog

        dlg = PreferencesDialog(
            insights_store=self._insights,
            lang=self._profile.language,
            mode=self._profile.base_mode,
            parent=self,
        )
        if dlg.exec():
            self._apply_user_preferences(load_preferences())
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
            initial_release=self._active_release,
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

        self.recovery_banner.setStyleSheet(
            f"QFrame#RecoveryBanner {{ background-color: {p['bg_glass_elevated']}; "
            f"border-bottom: 1px solid {p['border_strong']}; }}"
        )
        self.recovery_banner_lbl.setStyleSheet(f"color: {p['text_primary']};")
        self.rec_close_btn.setIcon(load_svg_icon("close", color=p["icon_color"], size=13))

    def switch_view(self, index: int, *, immediate: bool = False) -> None:
        self.transitions.switch_to(index, self._profile, immediate=immediate)
        nav_idx = index if index in (0, 1, 2) else 0
        self.nav_panel.set_current_index(nav_idx, animate=(not immediate))
        for i, rb in enumerate(self.rib_nav_btns):
            rb.blockSignals(True)
            rb.setChecked(i == nav_idx)
            rb.blockSignals(False)
        # Auto-collapse left sidebar into ribbon when reading in Viewer so document dominates screen
        if index == 1:
            self.set_sidebar_collapsed(True)
        elif index in (0, 2):
            self.set_sidebar_collapsed(False)

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

    def retranslate(self, lang: str) -> None:
        self.open_sidebar_btn.setText(t("nav.open_pdf", lang))
        self.rib_open_btn.setToolTip(t("nav.open_pdf", lang))
        nav_labels = [t("nav.hub", lang), t("nav.viewer", lang), t("nav.studio", lang)]
        self.nav_panel.update_labels(nav_labels)
        for rb, lbl in zip(self.rib_nav_btns, nav_labels):
            rb.setToolTip(lbl)

        self.top_search.setPlaceholderText(t("topbar.search_placeholder", lang))
        self.privacy_tag.setText(t("sidebar.privacy_tag", lang))

        self.hub_view.retranslate(lang)
        self.viewer_view.retranslate(lang)
        self.studio_view.retranslate(lang)
        if self._ai_panel is not None and shiboken6.isValid(self._ai_panel):
            self._ai_panel.retranslate(lang)

    def _flush_profile_save(self) -> None:
        if self._persist_profile:
            save_theme_profile(self._profile)

    def apply_profile(self, profile: ThemeProfile, *, save: bool = True) -> None:
        self._profile = profile.sanitized(check_media_exists=False)
        p = get_palette(self._profile.base_mode)
        mode_changed = self._profile.base_mode != self._applied_base_mode

        if mode_changed:
            self._applied_base_mode = self._profile.base_mode
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
            self.app_logo_lbl.setPixmap(load_multicolor_svg_pixmap("pdf_lector_logo", size=36))
            sentinel_pm = load_svg_pixmap("trodai_sentinel", color=p["text_secondary"], size=18)
            self.trodai_sentinel_lbl.setPixmap(sentinel_pm)
            self.rib_sentinel_lbl.setPixmap(sentinel_pm)

            self.sidebar_collapse_btn.setIcon(load_svg_icon("sidebar_toggle", color=p["icon_color"], size=14))
            self.rib_expand_btn.setIcon(load_svg_icon("sidebar_toggle", color=p["icon_color"], size=15))
            self.open_sidebar_btn.setIcon(load_svg_icon("open", color=p["primary_fg"], size=15))
            self.rib_open_btn.setIcon(load_svg_icon("open", color=p["primary_fg"], size=15))
            for rb, icon_name in zip(self.rib_nav_btns, ("home", "viewer", "palette")):
                rb.setIcon(load_svg_icon(icon_name, color=p["icon_color"], size=15))

            self.top_notif_btn.setIcon(load_svg_icon("bell", color=p["icon_color"], size=16))
            self.top_settings_btn.setIcon(load_svg_icon("settings", color=p["icon_color"], size=16))
            self.top_menu_btn.setIcon(load_svg_icon("info", color=p["icon_color"], size=16))

            theme_icon = "sun" if self._profile.base_mode == "dark" else "moon"
            theme_tip = t(
                "topbar.toggle_light" if self._profile.base_mode == "dark" else "topbar.toggle_dark",
                self._profile.language,
            )
            self.top_theme_btn.setIcon(load_svg_icon(theme_icon, color=p["icon_color"], size=16))
            self.top_theme_btn.setToolTip(theme_tip)
            self.privacy_tag.apply_theme(self._profile.base_mode)
            self._apply_update_banner_theme()
            if self._ai_panel is not None and shiboken6.isValid(self._ai_panel):
                self._ai_panel.apply_theme(self._profile.base_mode)
            if self._active_tool_workspace is not None and shiboken6.isValid(self._active_tool_workspace):
                self._active_tool_workspace.apply_theme(self._profile.base_mode)

        self.nav_panel.set_animations_enabled(self._profile.motion_level != "static")
        self.sidebar.set_profile(self._profile)
        self.topbar.set_profile(self._profile)
        self.hub_view.apply_profile(self._profile)
        self.viewer_view.apply_profile(self._profile)
        self.studio_view.sync_from_profile(self._profile)

        if save and self._persist_profile:
            self._profile_save_timer.start()

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

    def confirm_unsaved_changes_before_close(
        self,
        *,
        prompt_fn: Callable[[list[str]], str] | None = None,
    ) -> str:
        """Detect unsaved documents before exit and prompt Save / Discard / Cancel."""
        dirty_names = self.viewer_view.unsaved_document_names()
        if not dirty_names:
            return "discard"

        cb = prompt_fn or self._unsaved_close_prompt_fn
        if cb is not None:
            decision = str(cb(dirty_names)).strip().lower()
            return decision if decision in ("save", "discard", "cancel") else "cancel"

        if QApplication.platformName() == "offscreen":
            return "discard"

        lang = self._profile.language
        listed = "\n".join(f"- {name}" for name in dirty_names[:8])
        if len(dirty_names) > 8:
            listed += f"\n... (+{len(dirty_names) - 8})"

        texts = {
            "es": (
                "Cambios sin guardar",
                f"Hay cambios sin guardar en:\n{listed}",
                "Deseas guardar los cambios antes de cerrar la aplicacion?",
                "Guardar y cerrar",
                "Cerrar sin guardar",
                "Cancelar",
            ),
            "en": (
                "Unsaved changes",
                f"There are unsaved changes in:\n{listed}",
                "Do you want to save your changes before closing?",
                "Save & Close",
                "Close without saving",
                "Cancel",
            ),
            "pt": (
                "Alteracoes nao salvas",
                f"Ha alteracoes nao salvas em:\n{listed}",
                "Deseja salvar as alteracoes antes de fechar?",
                "Salvar e fechar",
                "Fechar sem salvar",
                "Cancelar",
            ),
        }.get(lang, None)
        if texts is None:
            texts = (
                "Cambios sin guardar",
                f"Hay cambios sin guardar en:\n{listed}",
                "Deseas guardar los cambios antes de cerrar la aplicacion?",
                "Guardar y cerrar",
                "Cerrar sin guardar",
                "Cancelar",
            )

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(texts[0])
        box.setText(texts[1])
        box.setInformativeText(texts[2])
        btn_save = box.addButton(texts[3], QMessageBox.ButtonRole.AcceptRole)
        btn_discard = box.addButton(texts[4], QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(texts[5], QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(btn_save)
        box.exec()
        clicked = box.clickedButton()
        if clicked is btn_save:
            return "save"
        if clicked is btn_discard:
            return "discard"
        return "cancel"

    def closeEvent(self, event) -> None:
        if self.has_unsaved_changes():
            decision = self.confirm_unsaved_changes_before_close()
            if decision == "cancel":
                event.ignore()
                return
            if decision == "save":
                self.save_all_documents()
                if self.has_unsaved_changes():
                    event.ignore()
                    return
        if self._autosave_timer.isActive():
            self._autosave_timer.stop()
        if self._autosave_debounce_timer.isActive():
            self._autosave_debounce_timer.stop()
        try:
            self.save_autosave_snapshot(clean_exit=True)
        except Exception:
            pass
        if self._profile_save_timer.isActive():
            self._profile_save_timer.stop()
            self._flush_profile_save()
        self.sidebar.cleanup()
        self.topbar.cleanup()
        self.hub_view.cleanup()
        self.viewer_view.cleanup()
        self.studio_view.cleanup()
        super().closeEvent(event)
