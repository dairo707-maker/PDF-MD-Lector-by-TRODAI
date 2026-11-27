"""AboutUpdaterDialog displaying version, MIT license, TRODAI seal, offline toggle, rendered release notes, and 1-click SHA-256 updater."""

from __future__ import annotations

from collections.abc import Callable
import inspect
from pathlib import Path
import sys
import tempfile

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Qt, Signal
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)
import shiboken6

from .. import APP_NAME, COMPANY_NAME, read_version
from ..core.markdown_engine import render_markdown_to_safe_html
from ..core.updater import (
    DEFAULT_RELEASES_API_URL,
    ReleaseInfo,
    check_for_updates,
    download_verified_update,
    launch_detached_update,
    load_updater_state,
    save_updater_state,
)
from ..theme.palette import MONO_FONT_FAMILY, UI_FONT_FAMILY, get_palette
from .svg_icons import load_multicolor_svg_pixmap, load_svg_icon
from .widgets import AdaptiveButton, FlowLayout, StatusTag


class _DownloadSignals(QObject):
    progress = Signal(int)
    completed = Signal(object)
    failed = Signal(str)


class _DownloadUpdateTask(QRunnable):
    """Background worker for SHA-256 verified update downloads without blocking the UI thread."""

    def __init__(self, *, asset_url: str, sums_url: str, dest_dir: Path, offline_only: bool) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._asset_url = asset_url
        self._sums_url = sums_url
        self._dest_dir = dest_dir
        self._offline_only = offline_only
        self.signals = _DownloadSignals()

    def run(self) -> None:
        try:
            out = download_verified_update(
                asset_url=self._asset_url,
                sums_url=self._sums_url,
                dest_dir=self._dest_dir,
                offline_only=self._offline_only,
                progress_cb=lambda pct: self.signals.progress.emit(int(pct)),
            )
            self.signals.completed.emit(out)
        except Exception as exc:
            self.signals.failed.emit(str(exc))


class AboutUpdaterDialog(QDialog):
    """About & SHA-256 release updater dialog with 'Solo offline' toggle and rendered release notes."""

    def __init__(
        self,
        *,
        current_version: str | None = None,
        api_url: str = DEFAULT_RELEASES_API_URL,
        state_path: Path | None = None,
        has_unsaved_fn: Callable[[], bool] | None = None,
        has_active_tasks_fn: Callable[[], bool] | None = None,
        initial_release: ReleaseInfo | None = None,
        lang: str = "es",
        mode: str = "dark",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._lang = lang if lang in ("es", "en", "pt") else "es"
        self._mode = mode if mode in ("dark", "light") else "dark"
        self._current_version = current_version or read_version()
        self._api_url = api_url
        self._state_path = state_path
        self._has_unsaved_fn = has_unsaved_fn
        self._has_active_tasks_fn = has_active_tasks_fn
        self._state = load_updater_state(self._state_path)
        self._latest_release: ReleaseInfo | None = initial_release
        self._downloaded_installer_path: Path | None = None
        self._raw_notes_md: str = ""
        self._downloading: bool = False
        self._download_task: _DownloadUpdateTask | None = None

        self.setWindowTitle(f"Acerca de y Actualizaciones · {APP_NAME}")
        self.setMinimumSize(560, 440)
        self.resize(600, 480)

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)

        # Brand Header
        brand_card = QFrame(self)
        brand_card.setObjectName("CardPanel")
        bh = QHBoxLayout(brand_card)
        bh.setContentsMargins(14, 12, 14, 12)
        bh.setSpacing(12)

        self.logo_lbl = QLabel(brand_card)
        self.logo_lbl.setFixedSize(48, 48)
        self.logo_lbl.setPixmap(load_multicolor_svg_pixmap("pdf_lector_logo", size=48))
        bh.addWidget(self.logo_lbl)

        txt_col = QVBoxLayout()
        txt_col.setSpacing(2)
        title_lbl = QLabel(f"{APP_NAME} · by {COMPANY_NAME}", brand_card)
        title_lbl.setFont(QFont(UI_FONT_FAMILY, 14, QFont.Weight.Bold))
        txt_col.addWidget(title_lbl)

        self.version_label = QLabel(f"Version v{self._current_version} · Licencia MIT (Open Source)", brand_card)
        self.version_label.setProperty("mono", "true")
        self.version_label.setFont(QFont(MONO_FONT_FAMILY, 9))
        txt_col.addWidget(self.version_label)
        bh.addLayout(txt_col, 1)

        self.update_tag = StatusTag("VERIFICAR", tone="muted", parent=brand_card)
        bh.addWidget(self.update_tag)
        root.addWidget(brand_card)

        self.offline_only_checkbox = QCheckBox(
            "Solo offline (desactivar comprobacion automatica y manual de actualizaciones)",
            self,
        )
        self.offline_only_checkbox.setChecked(self._state.offline_only)
        self.offline_only_checkbox.toggled.connect(self._on_offline_toggled)
        root.addWidget(self.offline_only_checkbox)

        self.status_lbl = QLabel("Listo para comprobar actualizaciones en GitHub Releases.", self)
        self.status_lbl.setProperty("tone", "secondary")
        self.status_lbl.setWordWrap(True)
        root.addWidget(self.status_lbl)

        self.notes_view = QTextBrowser(self)
        self.notes_view.setReadOnly(True)
        self.notes_view.setOpenExternalLinks(False)
        root.addWidget(self.notes_view, 1)

        self.progress_bar = QProgressBar(self)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFixedHeight(6)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setVisible(False)
        root.addWidget(self.progress_bar)

        btns = FlowLayout(h_spacing=8, v_spacing=6)
        self.check_btn = AdaptiveButton("Buscar actualizaciones", self, role="PrimaryAction")
        self.check_btn.clicked.connect(self.check_now)
        btns.addWidget(self.check_btn)

        # Kept hidden in 1-click UX mode while preserving API compatibility for programmatic callers
        self.download_btn = AdaptiveButton("Descargar y verificar SHA-256", self, role="GhostAction")
        self.download_btn.setEnabled(False)
        self.download_btn.setVisible(False)
        self.download_btn.clicked.connect(self.download_current_release)
        btns.addWidget(self.download_btn)

        self.install_btn = AdaptiveButton("Actualizar y reiniciar ahora", self, role="PrimaryAction")
        self.install_btn.setEnabled(False)
        self.install_btn.setVisible(False)
        self.install_btn.clicked.connect(self.update_and_restart_now)
        btns.addWidget(self.install_btn)

        self.close_btn = AdaptiveButton("Cerrar", self, role="GhostAction")
        self.close_btn.clicked.connect(self.accept)
        btns.addWidget(self.close_btn)
        root.addLayout(btns)

        self._set_release_notes_markdown("")
        self.apply_theme(self._mode)
        if self._latest_release is not None:
            self._apply_release_state(self._latest_release)
        else:
            self._sync_update_actions_visibility()

    def _set_release_notes_markdown(self, md_text: str) -> None:
        self._raw_notes_md = md_text or ""
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

        if self._raw_notes_md.strip():
            body_html, _ = render_markdown_to_safe_html(
                self._raw_notes_md,
                base_dir=None,
                allow_remote_images=False,
            )
        else:
            body_html = f"<p style='color:{muted};'>Las notas de la version apareceran aqui al comprobar actualizaciones.</p>"

        styled_html = (
            "<!DOCTYPE html><html><head><meta charset='utf-8'><style>"
            f"body {{ background-color: {bg}; color: {sec}; font-family: '{ui_fam}', sans-serif; "
            f"font-size: 12.5px; font-weight: 400; line-height: 1.5; margin: 12px; }}"
            f"h1, h2, h3, h4 {{ color: {fg}; font-weight: 700; margin-top: 10px; margin-bottom: 5px; }}"
            f"h1 {{ font-size: 15px; }} h2 {{ font-size: 13.5px; }} h3 {{ font-size: 12.5px; }}"
            f"ul, ol {{ margin-top: 4px; margin-bottom: 6px; padding-left: 18px; }}"
            f"li {{ margin-bottom: 3px; }}"
            f"strong, b {{ font-weight: 700; color: {fg}; }}"
            f"a {{ color: {link_col}; text-decoration: underline; }}"
            f"code, pre {{ font-family: '{mono_fam}', monospace; background-color: {code_bg}; "
            f"color: {fg}; font-weight: 400; padding: 1px 4px; }}"
            f"pre {{ padding: 8px; border: 1px solid {border}; }}"
            f"blockquote {{ margin-left: 10px; padding-left: 8px; color: {muted}; border-left: 3px solid {border}; }}"
            "</style></head><body>"
            f"{body_html}"
            "</body></html>"
        )
        self.notes_view.setHtml(styled_html)

    def _sync_update_actions_visibility(self) -> None:
        rel = self._latest_release
        offline = self.offline_only_checkbox.isChecked()
        can_update = bool(
            rel is not None
            and rel.update_available
            and rel.asset_url
            and rel.sums_url
            and not offline
        )
        self.download_btn.setEnabled(can_update and not self._downloading)
        self.install_btn.setVisible(can_update)
        self.install_btn.setEnabled(can_update and not self._downloading)
        if not can_update and not self._downloading:
            self.progress_bar.setVisible(False)

    def _apply_release_state(self, rel: ReleaseInfo) -> None:
        self._set_release_notes_markdown(rel.notes or f"Version {rel.version}")
        if rel.update_available:
            self.update_tag.set_tone("ok", f"v{rel.version}")
            if rel.asset_url and rel.sums_url:
                self.status_lbl.setText(
                    f"Nueva version disponible: v{rel.version}. Haz clic en 'Actualizar y reiniciar ahora'."
                )
            else:
                self.status_lbl.setText(
                    f"Nueva version disponible: v{rel.version} (sin paquete descargable directo)."
                )
        else:
            self.update_tag.set_tone("ok", "AL DIA")
            self.status_lbl.setText(f"Estas en la ultima version (v{self._current_version}).")
        self._sync_update_actions_visibility()

    def _on_offline_toggled(self, checked: bool) -> None:
        self._state.offline_only = bool(checked)
        save_updater_state(self._state, self._state_path)
        if checked:
            self.update_tag.set_tone("muted", "SOLO OFFLINE")
            self.status_lbl.setText("Modo Solo offline activo: cero conexiones externas.")
        else:
            self.update_tag.set_tone("accent", "LISTO")
        self._sync_update_actions_visibility()

    def check_now(self) -> ReleaseInfo | None:
        if self.offline_only_checkbox.isChecked():
            self.status_lbl.setText("Modo Solo offline activo: no se realizaron solicitudes.")
            self._sync_update_actions_visibility()
            return None
        rel = check_for_updates(self._current_version, api_url=self._api_url, offline_only=False)
        self._latest_release = rel
        self._downloaded_installer_path = None
        if rel is None:
            self.update_tag.set_tone("warn", "SIN CONEXION")
            self.status_lbl.setText("No se pudo consultar la ultima version.")
            self._sync_update_actions_visibility()
            return None
        self._apply_release_state(rel)
        return rel

    def download_current_release(self) -> Path | None:
        rel = self._latest_release
        if rel is None or not rel.asset_url or not rel.sums_url:
            return None
        if self._has_unsaved_fn and self._has_unsaved_fn():
            self.status_lbl.setText("Guarda tus cambios en PDF/Markdown antes de actualizar.")
            return None
        if self._has_active_tasks_fn and self._has_active_tasks_fn():
            self.status_lbl.setText("Espera a que terminen las tareas activas antes de actualizar.")
            return None
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(0)
        try:
            out = download_verified_update(
                asset_url=rel.asset_url,
                sums_url=rel.sums_url,
                dest_dir=Path(tempfile.gettempdir()) / "TRODAI_Updates",
                offline_only=self.offline_only_checkbox.isChecked(),
                progress_cb=self.progress_bar.setValue,
            )
            self._downloaded_installer_path = out
            self.install_btn.setVisible(True)
            self.install_btn.setEnabled(True)
            self.update_tag.set_tone("ok", "VERIFICADO")
            self.status_lbl.setText(
                f"Paquete verificado SHA-256: {out.name}. Iniciando actualizacion..."
            )
            return out
        except Exception as exc:
            self._downloaded_installer_path = None
            self.update_tag.set_tone("risk", "ERROR SHA-256")
            self.status_lbl.setText(f"Actualizacion rechazada: {exc}")
            self._sync_update_actions_visibility()
            return None

    def update_and_restart_now(self) -> None:
        """One-click updater: download & verify SHA-256 with progress, then launch installer and quit."""
        if self._downloaded_installer_path is not None and self._downloaded_installer_path.exists():
            self.install_downloaded_release()
            return
        rel = self._latest_release
        if rel is None or not rel.update_available or not rel.asset_url or not rel.sums_url:
            return
        if self._has_unsaved_fn and self._has_unsaved_fn():
            self.status_lbl.setText("Guarda tus cambios en PDF/Markdown antes de actualizar.")
            return
        if self._has_active_tasks_fn and self._has_active_tasks_fn():
            self.status_lbl.setText("Espera a que terminen las tareas activas antes de actualizar.")
            return

        if QApplication.platformName() == "offscreen":
            out = self.download_current_release()
            if out is not None:
                self.install_downloaded_release()
            return

        self._downloading = True
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(0)
        self.install_btn.setEnabled(False)
        self.check_btn.setEnabled(False)
        self.update_tag.set_tone("accent", "DESCARGANDO")
        self.status_lbl.setText(f"Descargando v{rel.version} y verificando firma SHA-256...")

        task = _DownloadUpdateTask(
            asset_url=rel.asset_url,
            sums_url=rel.sums_url,
            dest_dir=Path(tempfile.gettempdir()) / "TRODAI_Updates",
            offline_only=self.offline_only_checkbox.isChecked(),
        )
        self._download_task = task
        task.signals.progress.connect(self._on_download_progress, Qt.ConnectionType.QueuedConnection)
        task.signals.completed.connect(self._on_download_completed, Qt.ConnectionType.QueuedConnection)
        task.signals.failed.connect(self._on_download_failed, Qt.ConnectionType.QueuedConnection)
        QThreadPool.globalInstance().start(task)

    def _on_download_progress(self, pct: int) -> None:
        if shiboken6.isValid(self):
            self.progress_bar.setValue(max(0, min(100, int(pct))))

    def _on_download_completed(self, out_path: object) -> None:
        if not shiboken6.isValid(self):
            return
        self._downloading = False
        self._download_task = None
        self.check_btn.setEnabled(True)
        if isinstance(out_path, Path) and out_path.exists():
            self._downloaded_installer_path = out_path
            self.progress_bar.setValue(100)
            self.update_tag.set_tone("ok", "VERIFICADO")
            self.status_lbl.setText(f"Paquete verificado SHA-256: {out_path.name}. Iniciando instalador...")
            self.install_downloaded_release()
        else:
            self._sync_update_actions_visibility()

    def _on_download_failed(self, err_msg: str) -> None:
        if not shiboken6.isValid(self):
            return
        self._downloading = False
        self._download_task = None
        self._downloaded_installer_path = None
        self.check_btn.setEnabled(True)
        self.update_tag.set_tone("risk", "ERROR SHA-256")
        self.status_lbl.setText(f"Actualizacion rechazada: {err_msg}")
        self._sync_update_actions_visibility()

    def install_downloaded_release(
        self,
        *,
        launcher: Callable[..., object] = launch_detached_update,
        quit_callback: Callable[[], None] | None = None,
    ) -> bool:
        """Launch verified installer detached and request application quit for clean replacement."""
        pkg = self._downloaded_installer_path
        if pkg is None or not pkg.exists():
            self.status_lbl.setText("Descarga y verifica primero el paquete SHA-256.")
            return False
        if self._has_unsaved_fn and self._has_unsaved_fn():
            self.status_lbl.setText("Guarda tus cambios en PDF/Markdown antes de actualizar.")
            return False
        if self._has_active_tasks_fn and self._has_active_tasks_fn():
            self.status_lbl.setText("Espera a que terminen las tareas activas antes de actualizar.")
            return False

        relaunch_exe = Path(sys.executable) if getattr(sys, "frozen", False) else None
        try:
            if launcher is launch_detached_update:
                launcher(installer_path=pkg, relaunch_exe=relaunch_exe)
            else:
                params = inspect.signature(launcher).parameters
                if "installer_path" in params:
                    launcher(installer_path=pkg, relaunch_exe=relaunch_exe)
                elif "target_exe" in params:
                    launcher(pkg, target_exe=Path(sys.executable))
                else:
                    launcher(pkg)
        except Exception as exc:
            self.update_tag.set_tone("risk", "ERROR")
            self.status_lbl.setText(f"No se pudo iniciar el instalador: {exc}")
            self._sync_update_actions_visibility()
            return False

        self.status_lbl.setText("Instalador iniciado. Cerrando aplicacion para actualizar...")
        if quit_callback is not None:
            quit_callback()
        elif QApplication.platformName() != "offscreen":
            app = QApplication.instance()
            if self.isVisible():
                self.accept()
            if app is not None:
                QTimer.singleShot(50, app.quit)
        return True

    def apply_theme(self, mode: str) -> None:
        self._mode = "light" if mode == "light" else "dark"
        p = get_palette(self._mode)
        self.update_tag.apply_theme(self._mode)
        self.check_btn.setIcon(load_svg_icon("refresh", color=p["primary_fg"], size=14))
        self.download_btn.setIcon(load_svg_icon("download", color=p["icon_color"], size=14))
        self.install_btn.setIcon(load_svg_icon("check", color=p["primary_fg"], size=14))
        self._set_release_notes_markdown(self._raw_notes_md)
