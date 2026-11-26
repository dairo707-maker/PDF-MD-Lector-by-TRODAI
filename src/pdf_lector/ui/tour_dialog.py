"""Interactive guided TourDialog with Shiboken6-safe deferred refresh and ES/EN/PT copy."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QProgressBar, QVBoxLayout, QWidget
import shiboken6

from ..theme.palette import MONO_FONT_FAMILY, UI_FONT_FAMILY, get_palette
from .svg_icons import load_svg_icon, load_svg_pixmap
from .widgets import AdaptiveButton, StatusTag

_TOUR_STEPS: dict[str, list[tuple[str, str, str, str]]] = {
    "es": [
        (
            "home",
            "Centro de Herramientas y Acciones Rapidas",
            "Acciones Rapidas se adapta localmente a tus 6 flujos mas usados sin enviar telemetria. Puedes pausar o limpiar el historial en cualquier momento.",
            "SOLO LOCAL",
        ),
        (
            "markdown",
            "Visor Dual PDF + Markdown y Anotaciones",
            "Abre pestanas mixtas PDF y .md/.markdown con 3 modos (Lectura, Dividido, Editor), resaltados ISO 32000, notas en hilo y tinta tactil o con lapiz.",
            "ISO 32000",
        ),
        (
            "book",
            "Diccionario Offline y Privacidad Garantizada",
            "Consulta definiciones ES/EN sin internet desde cualquier palabra seleccionada, desacopla el diccionario o importa glosarios JSON locales.",
            "OFFLINE",
        ),
        (
            "palette",
            "Theme Studio, Guardias y Actualizaciones Verificadas",
            "Personaliza el banner del Hub y el fondo del visor con control de contraste AA y ahorro de bateria. Las actualizaciones verifican SHA-256.",
            "SHA-256",
        ),
    ],
    "en": [
        (
            "home",
            "Tools Hub & Dynamic Quick Actions",
            "Quick Actions adapts locally to your top 6 workflows with zero telemetry. Pause or clear local history anytime.",
            "LOCAL ONLY",
        ),
        (
            "markdown",
            "Dual PDF + Markdown Viewer & Annotations",
            "Open mixed PDF and .md/.markdown tabs with 3 modes (Read, Split, Edit), ISO 32000 highlights, threaded notes, and stylus/touch ink.",
            "ISO 32000",
        ),
        (
            "book",
            "Offline Dictionary & Privacy First",
            "Look up ES/EN definitions offline from any selected word, detach the dictionary window, or import local JSON glossaries.",
            "OFFLINE",
        ),
        (
            "palette",
            "Theme Studio, Guards & Verified Updates",
            "Customize Hub banners and outer viewer backdrops with AA Auto-Scrim and battery guard. Updates enforce SHA-256 verification.",
            "SHA-256",
        ),
    ],
    "pt": [
        (
            "home",
            "Central de Ferramentas e Acoes Rapidas",
            "Acoes Rapidas se adapta localmente aos seus 6 fluxos mais usados sem telemetria. Pause ou limpe o historico a qualquer momento.",
            "LOCAL",
        ),
        (
            "markdown",
            "Visualizador Dual PDF + Markdown e Anotacoes",
            "Abra abas mistas PDF e .md/.markdown com 3 modos, destaques ISO 32000, comentarios em fio e tinta tatil ou caneta.",
            "ISO 32000",
        ),
        (
            "book",
            "Dicionario Offline e Privacidade",
            "Consulte definicoes ES/EN sem internet, desencaixe o painel ou importe glossarios JSON locais.",
            "OFFLINE",
        ),
        (
            "palette",
            "Theme Studio e Atualizacoes Verificadas",
            "Personalize o banner do Hub e o fundo do visualizador com contraste AA e verificacao obrigatoria SHA-256.",
            "SHA-256",
        ),
    ],
}

_TOUR_UI: dict[str, dict[str, str]] = {
    "es": {"win": "Recorrido guiado · PDF/MD Lector", "prev": "Anterior", "next": "Siguiente", "done": "Finalizar"},
    "en": {"win": "Guided Tour · PDF/MD Lector", "prev": "Previous", "next": "Next", "done": "Finish"},
    "pt": {"win": "Tour Guiado · PDF/MD Lector", "prev": "Anterior", "next": "Proximo", "done": "Concluir"},
}


class TourDialog(QDialog):
    """Shiboken6-safe onboarding tour dialog for PDF/MD Lector by TRODAI."""

    def __init__(
        self,
        *,
        lang: str = "es",
        mode: str = "dark",
        on_step_change: Callable[[int], None] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._lang = lang if lang in ("es", "en", "pt") else "es"
        self._mode = mode if mode in ("dark", "light") else "dark"
        self._on_step_change = on_step_change
        self._step_index = 0
        self.completed = False

        ui_txt = _TOUR_UI[self._lang]
        self.setWindowTitle(ui_txt["win"])
        self.setMinimumSize(540, 320)
        self.resize(580, 340)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(12)

        top = QHBoxLayout()
        top.setSpacing(10)
        self.icon_lbl = QLabel(self)
        self.icon_lbl.setFixedSize(28, 28)
        top.addWidget(self.icon_lbl)

        self.step_tag = StatusTag("1 / 4", tone="accent", parent=self)
        top.addWidget(self.step_tag)

        self.badge_tag = StatusTag("LOCAL", tone="ok", parent=self)
        top.addWidget(self.badge_tag)
        top.addStretch(1)
        root.addLayout(top)

        self.title_lbl = QLabel("", self)
        tf = QFont(UI_FONT_FAMILY, 14)
        tf.setWeight(QFont.Weight.Bold)
        self.title_lbl.setFont(tf)
        self.title_lbl.setWordWrap(True)
        root.addWidget(self.title_lbl)

        self.body_lbl = QLabel("", self)
        self.body_lbl.setProperty("tone", "secondary")
        bf = QFont(UI_FONT_FAMILY, 10)
        bf.setWeight(QFont.Weight.Normal)
        self.body_lbl.setFont(bf)
        self.body_lbl.setWordWrap(True)
        self.body_lbl.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        root.addWidget(self.body_lbl, 1)

        self.progress = QProgressBar(self)
        self.progress.setRange(0, 100)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(4)
        root.addWidget(self.progress)

        nav = QHBoxLayout()
        nav.setSpacing(8)
        self.step_mono = QLabel("", self)
        self.step_mono.setProperty("mono", "true")
        self.step_mono.setProperty("tone", "muted")
        mf = QFont(MONO_FONT_FAMILY, 8)
        self.step_mono.setFont(mf)
        nav.addWidget(self.step_mono)
        nav.addStretch(1)

        self.prev_btn = AdaptiveButton(ui_txt["prev"], self, role="GhostAction")
        self.prev_btn.clicked.connect(self.prev_step)
        nav.addWidget(self.prev_btn)

        self.next_btn = AdaptiveButton(ui_txt["next"], self, role="PrimaryAction")
        self.next_btn.clicked.connect(self._on_next_clicked)
        nav.addWidget(self.next_btn)

        root.addLayout(nav)
        self.safe_refresh_step()

    def _steps(self) -> list[tuple[str, str, str, str]]:
        return _TOUR_STEPS.get(self._lang, _TOUR_STEPS["es"])

    def _on_next_clicked(self) -> None:
        if self._step_index >= len(self._steps()) - 1:
            self.finish_tour()
        else:
            self.next_step()

    def next_step(self) -> int:
        if not shiboken6.isValid(self):
            return self._step_index
        steps = self._steps()
        if self._step_index < len(steps) - 1:
            self._step_index += 1
        self.safe_refresh_step()
        if self._on_step_change is not None:
            self._on_step_change(self._step_index)
        return self._step_index

    def prev_step(self) -> int:
        if not shiboken6.isValid(self):
            return self._step_index
        if self._step_index > 0:
            self._step_index -= 1
        self.safe_refresh_step()
        if self._on_step_change is not None:
            self._on_step_change(self._step_index)
        return self._step_index

    def finish_tour(self) -> None:
        self.completed = True
        if shiboken6.isValid(self):
            self.accept()

    def safe_refresh_step(self) -> int | None:
        if not shiboken6.isValid(self):
            return None
        steps = self._steps()
        idx = max(0, min(len(steps) - 1, self._step_index))
        self._step_index = idx
        icon_name, title, body, badge = steps[idx]
        p = get_palette(self._mode)
        ui_txt = _TOUR_UI[self._lang]

        self.icon_lbl.setPixmap(load_svg_pixmap(icon_name, color=p["text_primary"], size=24))
        self.step_tag.setText(f"{idx + 1} / {len(steps)}")
        self.step_tag.apply_theme(self._mode)
        self.badge_tag.setText(badge)
        self.badge_tag.apply_theme(self._mode)
        self.title_lbl.setText(title)
        self.body_lbl.setText(body)
        self.step_mono.setText(f"{idx + 1}/{len(steps)}")
        self.progress.setValue(int(round(((idx + 1) / len(steps)) * 100)))

        self.prev_btn.setEnabled(idx > 0)
        is_last = idx == len(steps) - 1
        self.next_btn.setText(ui_txt["done"] if is_last else ui_txt["next"])
        self.next_btn.setIcon(load_svg_icon("check" if is_last else "next", color=p["primary_fg"], size=14))
        return idx

    def deleteLater(self) -> None:  # noqa: N802
        super().deleteLater()
        QCoreApplication.sendPostedEvents(self, QEvent.Type.DeferredDelete)
