"""NotificationsTasksDialog displaying NotificationCenter alerts, active tasks, and resumable sessions."""

from __future__ import annotations

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)
import shiboken6

from ..core.notifications import NotificationCenter
from ..core.task_control import SessionManager, TaskManager
from ..theme.palette import MONO_FONT_FAMILY, UI_FONT_FAMILY, get_palette
from .svg_icons import load_svg_icon
from .widgets import AdaptiveButton, FlowLayout, StatusTag


class NotificationsTasksDialog(QDialog):
    """Unified drawer/dialog for NotificationCenter items, active AsyncTasks, and resumable sessions."""

    def __init__(
        self,
        *,
        notification_center: NotificationCenter | None = None,
        task_manager: TaskManager | None = None,
        session_manager: SessionManager | None = None,
        lang: str = "es",
        mode: str = "dark",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._lang = lang if lang in ("es", "en", "pt") else "es"
        self._mode = mode if mode in ("dark", "light") else "dark"
        self._nc = notification_center or NotificationCenter()
        self._task_mgr = task_manager
        self._session_mgr = session_manager

        self.setWindowTitle("Notificaciones y Tareas · PDF/MD Lector" if self._lang == "es" else "Notifications & Tasks")
        self.setMinimumSize(580, 420)
        self.resize(640, 460)

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(10)

        head = QHBoxLayout()
        title = QLabel("Centro de Notificaciones y Tareas" if self._lang == "es" else "Notifications & Tasks", self)
        title.setFont(QFont(UI_FONT_FAMILY, 14, QFont.Weight.Bold))
        head.addWidget(title, 1)

        self.unread_tag = StatusTag("0 SIN LEER", tone="accent", parent=self)
        head.addWidget(self.unread_tag)
        root.addLayout(head)

        self.tabs = QTabWidget(self)

        # Tab 1: Notifications
        notif_tab = QWidget(self.tabs)
        n_lay = QVBoxLayout(notif_tab)
        n_lay.setContentsMargins(8, 8, 8, 8)
        n_lay.setSpacing(8)

        self.notif_list = QListWidget(notif_tab)
        self.notif_list.setFont(QFont(UI_FONT_FAMILY, 9))
        n_lay.addWidget(self.notif_list, 1)

        n_btns = FlowLayout(h_spacing=8, v_spacing=6)
        self.mark_read_btn = AdaptiveButton("Marcar leida", notif_tab, role="GhostAction")
        self.mark_read_btn.clicked.connect(self._mark_selected_read)
        n_btns.addWidget(self.mark_read_btn)

        self.mark_all_btn = AdaptiveButton("Marcar todas leidas", notif_tab, role="GhostAction")
        self.mark_all_btn.clicked.connect(self._mark_all_read)
        n_btns.addWidget(self.mark_all_btn)

        self.clear_btn = AdaptiveButton("Limpiar", notif_tab, role="RiskAction")
        self.clear_btn.clicked.connect(self._clear_all)
        n_btns.addWidget(self.clear_btn)
        n_lay.addLayout(n_btns)
        self.tabs.addTab(notif_tab, "Notificaciones")

        # Tab 2: Active Tasks & Saved Sessions
        tasks_tab = QWidget(self.tabs)
        t_lay = QVBoxLayout(tasks_tab)
        t_lay.setContentsMargins(8, 8, 8, 8)
        t_lay.setSpacing(8)

        self.tasks_list = QListWidget(tasks_tab)
        self.tasks_list.setFont(QFont(MONO_FONT_FAMILY, 9))
        t_lay.addWidget(self.tasks_list, 1)

        t_btns = FlowLayout(h_spacing=8, v_spacing=6)
        self.refresh_btn = AdaptiveButton("Actualizar", tasks_tab, role="GhostAction")
        self.refresh_btn.clicked.connect(self.refresh)
        t_btns.addWidget(self.refresh_btn)

        self.cancel_tasks_btn = AdaptiveButton("Cancelar activas", tasks_tab, role="RiskAction")
        self.cancel_tasks_btn.clicked.connect(self._cancel_all_tasks)
        t_btns.addWidget(self.cancel_tasks_btn)
        t_lay.addLayout(t_btns)
        self.tabs.addTab(tasks_tab, "Tareas y Sesiones")

        root.addWidget(self.tabs, 1)

        foot = QHBoxLayout()
        foot.addStretch(1)
        self.close_btn = AdaptiveButton("Cerrar", self, role="PrimaryAction")
        self.close_btn.clicked.connect(self.accept)
        foot.addWidget(self.close_btn)
        root.addLayout(foot)

        self._nc.add_listener(lambda: QTimer.singleShot(0, self.refresh))
        self.apply_theme(self._mode)
        self.refresh()

    def refresh(self) -> None:
        if not shiboken6.isValid(self):
            return
        items = self._nc.list_items()
        unread = self._nc.unread_count()
        self.unread_tag.set_tone("warn" if unread > 0 else "ok", f"{unread} SIN LEER")

        self.notif_list.clear()
        if not items:
            empty_item = QListWidgetItem("Sin notificaciones registradas.")
            empty_item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.notif_list.addItem(empty_item)
        else:
            for item in items:
                prefix = "[NUEVO]" if not item.read else "[LEIDO]"
                row = QListWidgetItem(f"{prefix} [{item.kind}] {item.title} — {item.detail}")
                row.setData(Qt.ItemDataRole.UserRole, item.notif_id)
                self.notif_list.addItem(row)

        self.tasks_list.clear()
        active_ids = self._task_mgr.active_tasks() if self._task_mgr is not None else []
        for tid in active_ids:
            self.tasks_list.addItem(f"[ACTIVA] {tid}")
        if self._session_mgr is not None:
            for sess in self._session_mgr.list_sessions():
                self.tasks_list.addItem(f"[SESION {sess.percent}%] {sess.title} ({sess.session_id})")
        if self.tasks_list.count() == 0:
            self.tasks_list.addItem("No hay tareas en segundo plano ni sesiones pausadas.")

    def _mark_selected_read(self) -> None:
        row = self.notif_list.currentItem()
        if row is not None:
            nid = str(row.data(Qt.ItemDataRole.UserRole) or "")
            if nid:
                self._nc.mark_read(nid)
                QTimer.singleShot(0, self.refresh)

    def _mark_all_read(self) -> None:
        self._nc.mark_all_read()
        QTimer.singleShot(0, self.refresh)

    def _clear_all(self) -> None:
        self._nc.clear_all(include_persistent=True)
        QTimer.singleShot(0, self.refresh)

    def _cancel_all_tasks(self) -> None:
        if self._task_mgr is not None:
            self._task_mgr.cancel_all()
        QTimer.singleShot(0, self.refresh)

    def apply_theme(self, mode: str) -> None:
        self._mode = "light" if mode == "light" else "dark"
        p = get_palette(self._mode)
        self.unread_tag.apply_theme(self._mode)
        self.refresh_btn.setIcon(load_svg_icon("refresh", color=p["icon_color"], size=14))
        self.clear_btn.setIcon(load_svg_icon("trash", color=p["tag_risk"], size=14))
