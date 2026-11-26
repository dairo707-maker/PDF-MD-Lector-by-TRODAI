"""WinOptiAI-style DialogLocalizer assigning semantic roles and ES/EN/PT labels to Qt dialogs."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QMessageBox, QPushButton, QWidget
import shiboken6

from .widgets import set_button_role

_STD_BTN = QMessageBox.StandardButton
_BOX_BTN = QDialogButtonBox.StandardButton

_MSG_LABELS: dict[str, dict[QMessageBox.StandardButton, str]] = {
    "es": {
        _STD_BTN.Ok: "Aceptar",
        _STD_BTN.Yes: "S\u00ed",
        _STD_BTN.YesToAll: "S\u00ed a todo",
        _STD_BTN.No: "No",
        _STD_BTN.NoToAll: "No a todo",
        _STD_BTN.Cancel: "Cancelar",
        _STD_BTN.Close: "Cerrar",
        _STD_BTN.Save: "Guardar",
        _STD_BTN.SaveAll: "Guardar todo",
        _STD_BTN.Discard: "Descartar",
        _STD_BTN.Apply: "Aplicar",
        _STD_BTN.Reset: "Restablecer",
        _STD_BTN.Retry: "Reintentar",
        _STD_BTN.Ignore: "Ignorar",
        _STD_BTN.Abort: "Abortar",
        _STD_BTN.Help: "Ayuda",
        _STD_BTN.Open: "Abrir",
    },
    "en": {
        _STD_BTN.Ok: "OK",
        _STD_BTN.Yes: "Yes",
        _STD_BTN.YesToAll: "Yes to All",
        _STD_BTN.No: "No",
        _STD_BTN.NoToAll: "No to All",
        _STD_BTN.Cancel: "Cancel",
        _STD_BTN.Close: "Close",
        _STD_BTN.Save: "Save",
        _STD_BTN.SaveAll: "Save All",
        _STD_BTN.Discard: "Discard",
        _STD_BTN.Apply: "Apply",
        _STD_BTN.Reset: "Reset",
        _STD_BTN.Retry: "Retry",
        _STD_BTN.Ignore: "Ignore",
        _STD_BTN.Abort: "Abort",
        _STD_BTN.Help: "Help",
        _STD_BTN.Open: "Open",
    },
    "pt": {
        _STD_BTN.Ok: "OK",
        _STD_BTN.Yes: "Sim",
        _STD_BTN.YesToAll: "Sim para todos",
        _STD_BTN.No: "N\u00e3o",
        _STD_BTN.NoToAll: "N\u00e3o para todos",
        _STD_BTN.Cancel: "Cancelar",
        _STD_BTN.Close: "Fechar",
        _STD_BTN.Save: "Salvar",
        _STD_BTN.SaveAll: "Salvar tudo",
        _STD_BTN.Discard: "Descartar",
        _STD_BTN.Apply: "Aplicar",
        _STD_BTN.Reset: "Redefinir",
        _STD_BTN.Retry: "Tentar novamente",
        _STD_BTN.Ignore: "Ignorar",
        _STD_BTN.Abort: "Abortar",
        _STD_BTN.Help: "Ajuda",
        _STD_BTN.Open: "Abrir",
    },
}

_BOX_TO_MSG: tuple[tuple[QDialogButtonBox.StandardButton, QMessageBox.StandardButton], ...] = (
    (_BOX_BTN.Ok, _STD_BTN.Ok),
    (_BOX_BTN.Yes, _STD_BTN.Yes),
    (_BOX_BTN.YesToAll, _STD_BTN.YesToAll),
    (_BOX_BTN.No, _STD_BTN.No),
    (_BOX_BTN.NoToAll, _STD_BTN.NoToAll),
    (_BOX_BTN.Cancel, _STD_BTN.Cancel),
    (_BOX_BTN.Close, _STD_BTN.Close),
    (_BOX_BTN.Save, _STD_BTN.Save),
    (_BOX_BTN.SaveAll, _STD_BTN.SaveAll),
    (_BOX_BTN.Discard, _STD_BTN.Discard),
    (_BOX_BTN.Apply, _STD_BTN.Apply),
    (_BOX_BTN.Reset, _STD_BTN.Reset),
    (_BOX_BTN.Retry, _STD_BTN.Retry),
    (_BOX_BTN.Ignore, _STD_BTN.Ignore),
    (_BOX_BTN.Abort, _STD_BTN.Abort),
    (_BOX_BTN.Help, _STD_BTN.Help),
    (_BOX_BTN.Open, _STD_BTN.Open),
)

_PRIMARY_STD = frozenset(
    {_STD_BTN.Ok, _STD_BTN.Yes, _STD_BTN.YesToAll, _STD_BTN.Save, _STD_BTN.SaveAll, _STD_BTN.Open, _STD_BTN.Apply}
)
_GHOST_STD = frozenset(
    {_STD_BTN.No, _STD_BTN.NoToAll, _STD_BTN.Cancel, _STD_BTN.Close, _STD_BTN.Help, _STD_BTN.Ignore, _STD_BTN.Reset}
)
_RISK_STD = frozenset({_STD_BTN.Discard, _STD_BTN.Abort})


class DialogLocalizer(QObject):
    """Application-wide localizer ensuring ES/EN/PT labels and semantic roles on Qt dialogs."""

    def __init__(
        self,
        app: QApplication | None = None,
        *,
        lang: str = "es",
        mode: str = "dark",
    ) -> None:
        super().__init__(app)
        self._lang = lang if lang in ("es", "en", "pt") else "es"
        self._mode = mode if mode in ("dark", "light") else "dark"
        if app is not None and shiboken6.isValid(app):
            app.installEventFilter(self)

    @property
    def language(self) -> str:
        return self._lang

    def set_language(self, lang: str) -> None:
        self._lang = lang if lang in ("es", "en", "pt") else "es"

    def set_mode(self, mode: str) -> None:
        self._mode = mode if mode in ("dark", "light") else "dark"

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.Show and isinstance(watched, QDialog):
            self.localize_dialog(watched)
        return super().eventFilter(watched, event)

    def localize_dialog(self, dialog: QWidget) -> None:
        if not shiboken6.isValid(dialog):
            return
        labels = _MSG_LABELS.get(self._lang, _MSG_LABELS["es"])

        if isinstance(dialog, QMessageBox):
            is_risk_box = dialog.icon() in (QMessageBox.Icon.Warning, QMessageBox.Icon.Critical)
            for std_btn, text in labels.items():
                btn = dialog.button(std_btn)
                if btn is not None and shiboken6.isValid(btn):
                    btn.setText(text)
                    if std_btn in _RISK_STD or (is_risk_box and std_btn in _PRIMARY_STD):
                        set_button_role(btn, "RiskAction")
                    elif std_btn in _PRIMARY_STD:
                        set_button_role(btn, "PrimaryAction")
                    elif std_btn in _GHOST_STD:
                        set_button_role(btn, "GhostAction")
                    else:
                        set_button_role(btn, "GhostAction")
            return

        for box in dialog.findChildren(QDialogButtonBox):
            if not shiboken6.isValid(box):
                continue
            for box_btn, std_btn in _BOX_TO_MSG:
                btn = box.button(box_btn)
                if isinstance(btn, QPushButton) and shiboken6.isValid(btn):
                    btn.setText(labels[std_btn])
                    if std_btn in _RISK_STD:
                        set_button_role(btn, "RiskAction")
                    elif std_btn in _PRIMARY_STD:
                        set_button_role(btn, "PrimaryAction")
                    else:
                        set_button_role(btn, "GhostAction")
