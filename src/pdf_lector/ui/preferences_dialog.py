"""Privacy-first PreferencesDialog defaulting annotation author to 'Anonimo'."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path

from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from ..core.tools import STANDARD_DPI_CHOICES, STANDARD_SOURCE_LANGS
from ..core.updater import load_updater_state, save_updater_state
from ..core.usage_insights import LocalUsageInsights
from ..core.win32_bridge import atomic_write_text, default_app_data_dir
from ..theme.palette import UI_FONT_FAMILY, get_palette
from .svg_icons import load_svg_icon
from .widgets import AdaptiveButton, FlowLayout, StatusTag

DEFAULT_ANONYMOUS_AUTHOR = "An\u00f3nimo"


@dataclass
class UserPreferences:
    annotation_author: str = DEFAULT_ANONYMOUS_AUTHOR
    default_dpi: int = 150
    default_source_lang: str = "auto"
    offline_only: bool = False
    insights_enabled: bool = True
    local_api_enabled: bool = False
    show_button_labels: bool = False
    continuous_scroll: bool = True


def get_preferences_path(app_dir: Path | None = None) -> Path:
    root = Path(app_dir) if app_dir is not None else default_app_data_dir()
    return root / "preferences.json"


def load_preferences(path: Path | None = None) -> UserPreferences:
    target = path or get_preferences_path()
    if not target.exists():
        return UserPreferences()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return UserPreferences()
        author = str(raw.get("annotation_author") or "").strip() or DEFAULT_ANONYMOUS_AUTHOR
        return UserPreferences(
            annotation_author=author,
            default_dpi=int(raw.get("default_dpi") or 150),
            default_source_lang=str(raw.get("default_source_lang") or "auto"),
            offline_only=bool(raw.get("offline_only", False)),
            insights_enabled=bool(raw.get("insights_enabled", True)),
            local_api_enabled=bool(raw.get("local_api_enabled", False)),
            show_button_labels=bool(raw.get("show_button_labels", False)),
            continuous_scroll=bool(raw.get("continuous_scroll", True)),
        )
    except Exception:
        return UserPreferences()


def save_preferences(prefs: UserPreferences, path: Path | None = None) -> Path:
    target = path or get_preferences_path()
    clean_author = (prefs.annotation_author or "").strip() or DEFAULT_ANONYMOUS_AUTHOR
    payload = asdict(prefs)
    payload["annotation_author"] = clean_author
    atomic_write_text(target, json.dumps(payload, indent=2, ensure_ascii=False))
    return target


class PreferencesDialog(QDialog):
    """Privacy, annotation author, DPI/language defaults, and offline preferences dialog."""

    def __init__(
        self,
        *,
        prefs_path: Path | None = None,
        insights_store: LocalUsageInsights | None = None,
        lang: str = "es",
        mode: str = "dark",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._lang = lang if lang in ("es", "en", "pt") else "es"
        self._mode = mode if mode in ("dark", "light") else "dark"
        self._prefs_path = prefs_path or get_preferences_path()
        self._insights = insights_store or LocalUsageInsights()
        self._prefs = load_preferences(self._prefs_path)

        self.setWindowTitle("Preferencias · PDF/MD Lector" if self._lang == "es" else "Preferences · PDF/MD Lector")
        self.setMinimumSize(580, 440)
        self.resize(620, 460)

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(10)

        head = QHBoxLayout()
        title = QLabel("Privacidad y Preferencias Locales" if self._lang == "es" else "Local Privacy & Preferences", self)
        title.setFont(QFont(UI_FONT_FAMILY, 14, QFont.Weight.Bold))
        head.addWidget(title, 1)
        self.privacy_tag = StatusTag("PRIVACIDAD", tone="ok", parent=self)
        head.addWidget(self.privacy_tag)
        root.addLayout(head)

        card = QFrame(self)
        card.setObjectName("CardPanel")
        card_lay = QVBoxLayout(card)
        card_lay.setContentsMargins(14, 12, 14, 12)
        card_lay.setSpacing(10)

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(8)

        self.author_edit = QLineEdit(self._prefs.annotation_author, card)
        self.author_edit.setPlaceholderText(DEFAULT_ANONYMOUS_AUTHOR)
        form.addRow("Autor en anotaciones (ISO 32000):", self.author_edit)

        self.dpi_combo = QComboBox(card)
        for dpi in STANDARD_DPI_CHOICES:
            self.dpi_combo.addItem(f"{dpi} DPI", dpi)
        dpi_idx = max(0, self.dpi_combo.findData(self._prefs.default_dpi))
        self.dpi_combo.setCurrentIndex(dpi_idx)
        form.addRow("Resolucion por defecto:", self.dpi_combo)

        self.lang_combo = QComboBox(card)
        for code in STANDARD_SOURCE_LANGS:
            self.lang_combo.addItem(code, code)
        lang_idx = max(0, self.lang_combo.findData(self._prefs.default_source_lang))
        self.lang_combo.setCurrentIndex(lang_idx)
        form.addRow("Idioma origen OCR / conversion:", self.lang_combo)
        card_lay.addLayout(form)

        up_state = load_updater_state()
        self.offline_only_chk = QCheckBox("Modo Solo Offline (desactiva chequeos de actualizacion)", card)
        self.offline_only_chk.setChecked(self._prefs.offline_only or up_state.offline_only)
        card_lay.addWidget(self.offline_only_chk)

        self.insights_chk = QCheckBox("Activar Acciones Rapidas y sugerencias locales (solo contadores)", card)
        self.insights_chk.setChecked(self._insights.enabled)
        card_lay.addWidget(self.insights_chk)

        self.api_chk = QCheckBox("Permitir API REST local 127.0.0.1 bajo demanda (OFF por defecto)", card)
        self.api_chk.setChecked(self._prefs.local_api_enabled)
        card_lay.addWidget(self.api_chk)

        self.show_labels_chk = QCheckBox(
            "Mostrar etiquetas de texto en botones de barras (solo iconos por defecto)",
            card,
        )
        self.show_labels_chk.setChecked(self._prefs.show_button_labels)
        card_lay.addWidget(self.show_labels_chk)

        self.continuous_scroll_chk = QCheckBox(
            "Desplazamiento vertical continuo entre paginas en Visor PDF",
            card,
        )
        self.continuous_scroll_chk.setChecked(self._prefs.continuous_scroll)
        card_lay.addWidget(self.continuous_scroll_chk)

        root.addWidget(card)
        root.addStretch(1)

        btns = FlowLayout(h_spacing=8, v_spacing=6)
        self.clear_hist_btn = AdaptiveButton("Limpiar historial local", self, role="GhostAction")
        self.clear_hist_btn.clicked.connect(self._clear_insights_history)
        btns.addWidget(self.clear_hist_btn)

        self.save_btn = AdaptiveButton("Guardar preferencias", self, role="PrimaryAction")
        self.save_btn.clicked.connect(self.save_and_close)
        btns.addWidget(self.save_btn)

        self.cancel_btn = AdaptiveButton("Cancelar", self, role="GhostAction")
        self.cancel_btn.clicked.connect(self.reject)
        btns.addWidget(self.cancel_btn)
        root.addLayout(btns)

        self.apply_theme(self._mode)

    def default_annotation_author(self) -> str:
        """Return configured annotation author, strictly defaulting to 'Anonimo' (never OS username)."""
        val = self.author_edit.text().strip()
        return val or DEFAULT_ANONYMOUS_AUTHOR

    def _clear_insights_history(self) -> None:
        self._insights.clear_history()

    def save_and_close(self) -> None:
        updated = UserPreferences(
            annotation_author=self.default_annotation_author(),
            default_dpi=int(self.dpi_combo.currentData() or 150),
            default_source_lang=str(self.lang_combo.currentData() or "auto"),
            offline_only=self.offline_only_chk.isChecked(),
            insights_enabled=self.insights_chk.isChecked(),
            local_api_enabled=self.api_chk.isChecked(),
            show_button_labels=self.show_labels_chk.isChecked(),
            continuous_scroll=self.continuous_scroll_chk.isChecked(),
        )
        save_preferences(updated, self._prefs_path)
        self._insights.set_enabled(updated.insights_enabled)
        up_state = load_updater_state()
        up_state.offline_only = updated.offline_only
        save_updater_state(up_state)
        self.accept()

    def apply_theme(self, mode: str) -> None:
        self._mode = "light" if mode == "light" else "dark"
        p = get_palette(self._mode)
        self.privacy_tag.apply_theme(self._mode)
        self.save_btn.setIcon(load_svg_icon("check", color=p["primary_fg"], size=14))
        self.clear_hist_btn.setIcon(load_svg_icon("trash", color=p["icon_color"], size=14))
