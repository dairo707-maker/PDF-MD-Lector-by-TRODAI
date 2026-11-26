"""Keyboard & Wireless Presentation Clicker Shortcuts Reference Dialog."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..theme.palette import MONO_FONT_FAMILY, UI_FONT_FAMILY, get_palette
from .svg_icons import load_svg_pixmap
from .widgets import AdaptiveButton, StatusTag

SHORTCUT_GROUPS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    (
        "Archivos y Navegacion General",
        (
            ("Ctrl + O", "Abrir documento PDF o Markdown"),
            ("Ctrl + S", "Guardar documento y anotaciones"),
            ("Ctrl + W", "Cerrar pestana activa del visor"),
            ("Ctrl + P", "Imprimir documento PDF"),
            ("Ctrl + F", "Buscar texto dentro del documento"),
            ("Ctrl + K", "Buscar herramienta en Inicio"),
            ("F1", "Ver este listado de atajos"),
        ),
    ),
    (
        "Visor PDF, Zoom y Lectura",
        (
            ("PgDn / Flecha abajo", "Pagina siguiente"),
            ("PgUp / Flecha arriba", "Pagina anterior"),
            ("Home / End", "Ir a la primera / ultima pagina"),
            ("Ctrl + + / Ctrl + -", "Acercar / Alejar zoom"),
            ("Ctrl + 0", "Ajustar al ancho de pagina"),
            ("Ctrl + R", "Rotar pagina 90 grados"),
            ("Ctrl + Z", "Deshacer ultima anotacion o nota"),
            ("F11", "Alternar pantalla completa"),
        ),
    ),
    (
        "Modo Presentacion y Apuntadores",
        (
            ("F5 / Ctrl + L", "Iniciar Modo Presentacion solo PDF"),
            ("PgDn / Espacio / Enter / Clic izq.", "Siguiente diapositiva (compatible con apuntador)"),
            ("PgUp / Backspace / Clic der.", "Diapositiva anterior (compatible con apuntador)"),
            ("B / .", "Pausar con pantalla en negro"),
            ("W", "Pausar con pantalla en blanco"),
            ("Esc", "Salir de Presentacion o cerrar barra de busqueda"),
        ),
    ),
)


class ShortcutsDialog(QDialog):
    """Categorized reference cheatsheet for keyboard and clicker shortcuts."""

    def __init__(self, *, mode: str = "dark", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._mode = "light" if mode == "light" else "dark"
        self.setWindowTitle("Atajos de teclado y presentacion · PDF/MD Lector")
        self.setMinimumSize(560, 440)
        self.resize(680, 540)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        hdr = QFrame(self)
        hdr.setObjectName("CardPanel")
        hl = QHBoxLayout(hdr)
        hl.setContentsMargins(12, 10, 12, 10)
        hl.setSpacing(10)
        p = get_palette(self._mode)
        ico = QLabel(hdr)
        ico.setFixedSize(20, 20)
        ico.setPixmap(load_svg_pixmap("keyboard", color=p["icon_color"], size=20))
        hl.addWidget(ico)
        title = QLabel("Atajos convencionales y apuntadores de presentacion", hdr)
        title.setFont(QFont(UI_FONT_FAMILY, 11, QFont.Weight.Bold))
        title.setWordWrap(True)
        hl.addWidget(title, 1)
        root.addWidget(hdr)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        host = QWidget(scroll)
        host.setObjectName("TransparentHost")
        host_lay = QVBoxLayout(host)
        host_lay.setContentsMargins(0, 0, 4, 0)
        host_lay.setSpacing(10)

        for group_title, items in SHORTCUT_GROUPS:
            card = QFrame(host)
            card.setObjectName("CardPanel")
            cl = QVBoxLayout(card)
            cl.setContentsMargins(12, 10, 12, 10)
            cl.setSpacing(8)
            g_lbl = QLabel(group_title, card)
            g_lbl.setFont(QFont(UI_FONT_FAMILY, 10, QFont.Weight.Bold))
            cl.addWidget(g_lbl)

            grid = QGridLayout()
            grid.setHorizontalSpacing(14)
            grid.setVerticalSpacing(6)
            for row, (keys, desc) in enumerate(items):
                tag = StatusTag(keys, tone="accent", parent=card)
                tag.apply_theme(self._mode)
                grid.addWidget(tag, row, 0, Qt.AlignmentFlag.AlignLeft)
                d_lbl = QLabel(desc, card)
                d_lbl.setProperty("tone", "secondary")
                d_lbl.setWordWrap(True)
                grid.addWidget(d_lbl, row, 1)
            grid.setColumnStretch(1, 1)
            cl.addLayout(grid)
            host_lay.addWidget(card)

        host_lay.addStretch(1)
        scroll.setWidget(host)
        root.addWidget(scroll, 1)

        footer = QHBoxLayout()
        footer.addStretch(1)
        ok_btn = AdaptiveButton("Cerrar", self, role="PrimaryAction")
        ok_btn.clicked.connect(self.accept)
        footer.addWidget(ok_btn)
        root.addLayout(footer)
