"""Dialog for sharing the active PDF/MD as an attachment via Desktop Mail (MAPI), Messaging apps, Webmail, or Clipboard."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from ..core.share_service import ShareResult, share_to_channel
from ..theme.palette import MONO_FONT_FAMILY, UI_FONT_FAMILY, get_palette
from .svg_icons import load_svg_icon, load_svg_pixmap
from .widgets import AdaptiveButton, StatusTag


def _format_size(size_bytes: int) -> str:
    if size_bytes >= 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    if size_bytes >= 1024:
        return f"{max(1, size_bytes // 1024)} KB"
    return f"{size_bytes} B"


class ShareDocumentDialog(QDialog):
    """Modal sharing hub preparing real file attachments (MAPI Email, CF_HDROP Clipboard + WhatsApp/Telegram/Teams)."""

    def __init__(
        self,
        doc_path: Path | str,
        *,
        mode: str = "dark",
        share_fn: Callable[[Path | str, str], ShareResult] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._doc_path = Path(doc_path)
        self._mode = "light" if mode == "light" else "dark"
        self._share_fn = share_fn or share_to_channel
        self._channel_buttons: dict[str, AdaptiveButton] = {}

        self.setWindowTitle(f"Compartir · {self._doc_path.name}")
        self.setMinimumWidth(550)
        self.resize(570, 400)

        p = get_palette(self._mode)
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        hdr = QFrame(self)
        hdr.setObjectName("CardPanel")
        hl = QHBoxLayout(hdr)
        hl.setContentsMargins(12, 10, 12, 10)
        hl.setSpacing(10)
        ico = QLabel(hdr)
        ico.setFixedSize(20, 20)
        ico.setPixmap(load_svg_pixmap("share", color=p["icon_color"], size=20))
        hl.addWidget(ico)
        title_lbl = QLabel(self._doc_path.name, hdr)
        title_lbl.setFont(QFont(UI_FONT_FAMILY, 10, QFont.Weight.Bold))
        hl.addWidget(title_lbl, 1)
        size_bytes = self._doc_path.stat().st_size if self._doc_path.exists() else 0
        size_tag = StatusTag(_format_size(size_bytes), tone="accent", parent=hdr)
        size_tag.apply_theme(self._mode)
        hl.addWidget(size_tag)
        root.addWidget(hdr)

        hint = QLabel(
            "Envia el archivo real como adjunto. En mensajeria directa (WhatsApp, Telegram, Teams) "
            "el PDF se copia al portapapeles listo para pegar con Ctrl+V dentro del chat.",
            self,
        )
        hint.setProperty("tone", "secondary")
        hint.setWordWrap(True)
        root.addWidget(hint)

        grid_card = QFrame(self)
        grid_card.setObjectName("CardPanel")
        grid = QGridLayout(grid_card)
        grid.setContentsMargins(12, 12, 12, 12)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)

        channels: tuple[tuple[str, str, str, str], ...] = (
            ("email_mapi", "Correo (Adjunto directo)", "PrimaryAction", "export"),
            ("whatsapp", "WhatsApp (Adjunto PDF)", "GhostAction", "share"),
            ("telegram", "Telegram (Adjunto PDF)", "GhostAction", "share"),
            ("teams", "Microsoft Teams (Adjunto)", "GhostAction", "share"),
            ("email_web", "Correo Web + Portapapeles", "GhostAction", "copy"),
            ("clipboard", "Copiar archivo al portapapeles", "GhostAction", "copy"),
            ("explorer", "Mostrar archivo en Explorador", "GhostAction", "folder"),
        )
        for idx, (ch_id, label, role, icon_name) in enumerate(channels):
            btn = AdaptiveButton(label, grid_card, role=role)
            icon_fg = p["primary_fg"] if role == "PrimaryAction" else p["icon_color"]
            btn.setIcon(load_svg_icon(icon_name, color=icon_fg, size=15))
            btn.setMinimumHeight(34)
            btn.clicked.connect(lambda _=False, ch=ch_id: self._trigger_share(ch))
            row, col = divmod(idx, 2)
            if idx == len(channels) - 1 and col == 0:
                grid.addWidget(btn, row, 0, 1, 2)
            else:
                grid.addWidget(btn, row, col)
            self._channel_buttons[ch_id] = btn
        root.addWidget(grid_card)

        self.status_lbl = QLabel("", self)
        self.status_lbl.setFont(QFont(MONO_FONT_FAMILY, 9))
        self.status_lbl.setWordWrap(True)
        self.status_lbl.setVisible(False)
        root.addWidget(self.status_lbl)

        footer = QHBoxLayout()
        footer.addStretch(1)
        close_btn = AdaptiveButton("Cerrar", self, role="GhostAction")
        close_btn.clicked.connect(self.accept)
        footer.addWidget(close_btn)
        root.addLayout(footer)

    def _trigger_share(self, channel: str) -> ShareResult:
        res = self._share_fn(self._doc_path, channel)
        p = get_palette(self._mode)
        col = p["tag_ok"] if res.ok else p["tag_warn"]
        self.status_lbl.setText(res.message)
        self.status_lbl.setStyleSheet(f"color: {col};")
        self.status_lbl.setVisible(True)
        return res
