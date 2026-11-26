"""AboutUpdaterDialog displaying version, MIT license, TRODAI seal, offline toggle, and SHA-256 updater."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import sys
import tempfile

from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QVBoxLayout,
    QWidget,
)

from .. import APP_NAME, COMPANY_NAME, read_version
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


class AboutUpdaterDialog(QDialog):
    """About & SHA-256 release updater dialog with 'Solo offline' toggle."""

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

        self.notes_view = QPlainTextEdit(self)
        self.notes_view.setReadOnly(True)
        self.notes_view.setFont(QFont(MONO_FONT_FAMILY, 8))
        self.notes_view.setPlaceholderText("Las notas de la version apareceran aqui...")
        root.addWidget(self.notes_view, 1)

        self.progress_bar = QProgressBar(self)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFixedHeight(6)
        self.progress_bar.setTextVisible(False)
        root.addWidget(self.progress_bar)

        btns = FlowLayout(h_spacing=8, v_spacing=6)
        self.check_btn = AdaptiveButton("Buscar actualizaciones", self, role="PrimaryAction")
        self.check_btn.clicked.connect(self.check_now)
        btns.addWidget(self.check_btn)

        self.download_btn = AdaptiveButton("Descargar y verificar SHA-256", self, role="GhostAction")
        self.download_btn.setEnabled(False)
        self.download_btn.clicked.connect(self.download_current_release)
        btns.addWidget(self.download_btn)

        self.install_btn = AdaptiveButton("Instalar y reiniciar ahora", self, role="PrimaryAction")
        self.install_btn.setEnabled(False)
        self.install_btn.clicked.connect(lambda: self.install_downloaded_release())
        btns.addWidget(self.install_btn)

        self.close_btn = AdaptiveButton("Cerrar", self, role="GhostAction")
        self.close_btn.clicked.connect(self.accept)
        btns.addWidget(self.close_btn)
        root.addLayout(btns)

        self.apply_theme(self._mode)
        if self._latest_release is not None and self._latest_release.update_available:
            self.notes_view.setPlainText(self._latest_release.notes or f"Version {self._latest_release.version}")
            self.update_tag.set_tone("ok", f"v{self._latest_release.version}")
            self.status_lbl.setText(f"Nueva version disponible: v{self._latest_release.version}")
            self.download_btn.setEnabled(bool(self._latest_release.asset_url and self._latest_release.sums_url))

    def _on_offline_toggled(self, checked: bool) -> None:
        self._state.offline_only = bool(checked)
        save_updater_state(self._state, self._state_path)
        if checked:
            self.update_tag.set_tone("muted", "SOLO OFFLINE")
            self.status_lbl.setText("Modo Solo offline activo: cero conexiones externas.")
        else:
            self.update_tag.set_tone("accent", "LISTO")

    def check_now(self) -> ReleaseInfo | None:
        if self.offline_only_checkbox.isChecked():
            self.status_lbl.setText("Modo Solo offline activo: no se realizaron solicitudes.")
            return None
        rel = check_for_updates(self._current_version, api_url=self._api_url, offline_only=False)
        self._latest_release = rel
        if rel is None:
            self.update_tag.set_tone("warn", "SIN CONEXION")
            self.status_lbl.setText("No se pudo consultar la ultima version.")
            self.download_btn.setEnabled(False)
            return None
        self.notes_view.setPlainText(rel.notes or f"Version {rel.version}")
        if rel.update_available:
            self.update_tag.set_tone("ok", f"v{rel.version}")
            self.status_lbl.setText(f"Nueva version disponible: v{rel.version}")
            self.download_btn.setEnabled(bool(rel.asset_url and rel.sums_url))
            self.install_btn.setEnabled(False)
        else:
            self.update_tag.set_tone("ok", "AL DIA")
            self.status_lbl.setText(f"Estas en la ultima version (v{self._current_version}).")
            self.download_btn.setEnabled(False)
            self.install_btn.setEnabled(False)
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
        try:
            out = download_verified_update(
                asset_url=rel.asset_url,
                sums_url=rel.sums_url,
                dest_dir=Path(tempfile.gettempdir()) / "TRODAI_Updates",
                offline_only=self.offline_only_checkbox.isChecked(),
                progress_cb=self.progress_bar.setValue,
            )
            self._downloaded_installer_path = out
            self.install_btn.setEnabled(True)
            self.update_tag.set_tone("ok", "VERIFICADO")
            self.status_lbl.setText(
                f"Paquete verificado SHA-256: {out}. Haz clic en 'Instalar y reiniciar ahora'."
            )
            return out
        except Exception as exc:
            self._downloaded_installer_path = None
            self.install_btn.setEnabled(False)
            self.update_tag.set_tone("risk", "ERROR SHA-256")
            self.status_lbl.setText(f"Actualizacion rechazada: {exc}")
            return None

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
        launcher(pkg, target_exe=Path(sys.executable))
        self.status_lbl.setText("Instalador iniciado. Cerrando aplicacion para actualizar...")
        if quit_callback is not None:
            quit_callback()
        elif QApplication.platformName() != "offscreen":
            app = QApplication.instance()
            if app is not None:
                app.quit()
        return True

    def apply_theme(self, mode: str) -> None:
        self._mode = "light" if mode == "light" else "dark"
        p = get_palette(self._mode)
        self.update_tag.apply_theme(self._mode)
        self.check_btn.setIcon(load_svg_icon("refresh", color=p["primary_fg"], size=14))
        self.download_btn.setIcon(load_svg_icon("download", color=p["icon_color"], size=14))
        self.install_btn.setIcon(load_svg_icon("check", color=p["primary_fg"], size=14))
