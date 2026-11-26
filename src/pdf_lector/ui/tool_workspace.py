"""Dedicated drag-and-drop ToolWorkspace for all 44 tools with DPI/quality/language controls and 1 PrimaryAction."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QProgressBar,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..core.i18n import t
from ..core.tools import ToolDefinition, ToolParamSpec, ToolResult
from ..theme.palette import MONO_FONT_FAMILY, UI_FONT_FAMILY, get_palette
from .svg_icons import load_svg_icon
from .widgets import AdaptiveButton, FlowLayout, StatusTag

_WS_TEXTS: dict[str, dict[str, str]] = {
    "es": {
        "inputs": "Archivos de entrada (arrastra o selecciona)",
        "add_files": "Agregar archivos",
        "clear_files": "Limpiar",
        "output": "Destino de salida",
        "browse_out": "Elegir salida",
        "params": "Parametros y calidad de salida",
        "run": "Ejecutar herramienta",
        "ready": "LISTO",
        "running": "PROCESANDO",
        "done": "COMPLETADO",
        "error": "ERROR",
        "drop_hint": "Suelta archivos locales aqui (PDF, Markdown o imagenes segun la herramienta).",
        "dpi_label": "Resolucion (DPI)",
        "quality_label": "Calidad (10-100)",
        "lang_label": "Idioma del documento",
    },
    "en": {
        "inputs": "Input files (drag & drop or browse)",
        "add_files": "Add files",
        "clear_files": "Clear",
        "output": "Output destination",
        "browse_out": "Choose output",
        "params": "Parameters & output quality",
        "run": "Run tool",
        "ready": "READY",
        "running": "RUNNING",
        "done": "COMPLETED",
        "error": "ERROR",
        "drop_hint": "Drop local files here (PDF, Markdown, or images depending on tool).",
        "dpi_label": "Resolution (DPI)",
        "quality_label": "Quality (10-100)",
        "lang_label": "Source language",
    },
    "pt": {
        "inputs": "Arquivos de entrada (arraste ou selecione)",
        "add_files": "Adicionar arquivos",
        "clear_files": "Limpar",
        "output": "Destino de saida",
        "browse_out": "Escolher saida",
        "params": "Parametros e qualidade de saida",
        "run": "Executar ferramenta",
        "ready": "PRONTO",
        "running": "PROCESSANDO",
        "done": "CONCLUIDO",
        "error": "ERRO",
        "drop_hint": "Solte arquivos locais aqui.",
        "dpi_label": "Resolucao (DPI)",
        "quality_label": "Qualidade (10-100)",
        "lang_label": "Idioma do documento",
    },
}


class ToolWorkspace(QWidget):
    """Unified tool runner workspace with 1 PrimaryAction, DPI/quality/lang controls, and local path guards."""

    completed = Signal(object)

    def __init__(
        self,
        tool_def: ToolDefinition,
        *,
        lang: str = "es",
        mode: str = "dark",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.tool_def = tool_def
        self._lang = lang if lang in ("es", "en", "pt") else "es"
        self._mode = mode if mode in ("dark", "light") else "dark"
        self._inputs: list[Path] = []
        self._output: Path | None = None
        self._last_result: ToolResult | None = None
        self._param_widgets: dict[str, QWidget] = {}

        self.dpi_control: QComboBox | None = None
        self.quality_control: QSpinBox | None = None
        self.lang_control: QComboBox | None = None

        self.setAcceptDrops(True)
        self.setMinimumSize(640, 480)
        txt = _WS_TEXTS[self._lang]
        self.setWindowTitle(t(tool_def.title_key, self._lang))

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)

        # Header
        head = QHBoxLayout()
        head.setSpacing(10)
        title_col = QVBoxLayout()
        title_col.setSpacing(2)

        self.title_lbl = QLabel(t(tool_def.title_key, self._lang), self)
        tf = QFont(UI_FONT_FAMILY, 15)
        tf.setWeight(QFont.Weight.Bold)
        self.title_lbl.setFont(tf)
        title_col.addWidget(self.title_lbl)

        self.desc_lbl = QLabel(t(tool_def.desc_key, self._lang), self)
        self.desc_lbl.setProperty("tone", "secondary")
        self.desc_lbl.setWordWrap(True)
        title_col.addWidget(self.desc_lbl)
        head.addLayout(title_col, 1)

        self.status_tag = StatusTag(txt["ready"], tone="accent", parent=self)
        head.addWidget(self.status_tag, 0, Qt.AlignmentFlag.AlignTop)
        root.addLayout(head)

        # Scrollable workspace body
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        host = QWidget(scroll)
        host.setObjectName("TransparentHost")
        body = QVBoxLayout(host)
        body.setContentsMargins(0, 0, 4, 4)
        body.setSpacing(10)

        # Input Drop Card
        in_card = QFrame(host)
        in_card.setObjectName("CardPanel")
        in_lay = QVBoxLayout(in_card)
        in_lay.setContentsMargins(12, 10, 12, 10)
        in_lay.setSpacing(6)

        in_hdr = QLabel(txt["inputs"], in_card)
        in_hdr.setFont(QFont(UI_FONT_FAMILY, 10, QFont.Weight.Bold))
        in_lay.addWidget(in_hdr)

        self.drop_hint_lbl = QLabel(txt["drop_hint"], in_card)
        self.drop_hint_lbl.setProperty("tone", "muted")
        self.drop_hint_lbl.setWordWrap(True)
        in_lay.addWidget(self.drop_hint_lbl)

        self.inputs_list = QListWidget(in_card)
        self.inputs_list.setMinimumHeight(88)
        self.inputs_list.setMaximumHeight(130)
        in_lay.addWidget(self.inputs_list)

        in_btns = FlowLayout(h_spacing=8, v_spacing=6)
        self.add_files_btn = AdaptiveButton(txt["add_files"], in_card, role="GhostAction")
        self.add_files_btn.clicked.connect(self._browse_inputs)
        in_btns.addWidget(self.add_files_btn)

        self.clear_files_btn = AdaptiveButton(txt["clear_files"], in_card, role="GhostAction")
        self.clear_files_btn.clicked.connect(self.clear_inputs)
        in_btns.addWidget(self.clear_files_btn)
        in_lay.addLayout(in_btns)
        body.addWidget(in_card)

        # Output Card
        out_card = QFrame(host)
        out_card.setObjectName("CardPanel")
        out_lay = QVBoxLayout(out_card)
        out_lay.setContentsMargins(12, 10, 12, 10)
        out_lay.setSpacing(6)

        out_hdr = QLabel(txt["output"], out_card)
        out_hdr.setFont(QFont(UI_FONT_FAMILY, 10, QFont.Weight.Bold))
        out_lay.addWidget(out_hdr)

        out_row = QHBoxLayout()
        out_row.setSpacing(8)
        self.output_edit = QLineEdit(out_card)
        self.output_edit.textChanged.connect(self._on_output_text_changed)
        out_row.addWidget(self.output_edit, 1)

        self.browse_out_btn = AdaptiveButton(txt["browse_out"], out_card, role="GhostAction")
        self.browse_out_btn.clicked.connect(self._browse_output)
        out_row.addWidget(self.browse_out_btn)
        out_lay.addLayout(out_row)
        body.addWidget(out_card)

        # Parameters Card
        param_card = QFrame(host)
        param_card.setObjectName("CardPanel")
        param_lay = QVBoxLayout(param_card)
        param_lay.setContentsMargins(12, 10, 12, 10)
        param_lay.setSpacing(8)

        param_hdr = QLabel(txt["params"], param_card)
        param_hdr.setFont(QFont(UI_FONT_FAMILY, 10, QFont.Weight.Bold))
        param_lay.addWidget(param_hdr)

        self.params_form = QFormLayout()
        self.params_form.setSpacing(8)
        self._build_param_controls(param_card)
        param_lay.addLayout(self.params_form)
        body.addWidget(param_card)

        body.addStretch(1)
        scroll.setWidget(host)
        root.addWidget(scroll, 1)

        # Footer: Progress + Single PrimaryAction
        self.progress_bar = QProgressBar(self)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFixedHeight(6)
        self.progress_bar.setTextVisible(False)
        root.addWidget(self.progress_bar)

        foot = QHBoxLayout()
        foot.setSpacing(10)
        self.summary_lbl = QLabel("", self)
        self.summary_lbl.setProperty("mono", "true")
        self.summary_lbl.setProperty("tone", "secondary")
        self.summary_lbl.setFont(QFont(MONO_FONT_FAMILY, 8))
        self.summary_lbl.setWordWrap(True)
        foot.addWidget(self.summary_lbl, 1)

        self.primary_action_btn = AdaptiveButton(txt["run"], self, role="PrimaryAction")
        self.primary_action_btn.clicked.connect(self.execute_primary_action)
        foot.addWidget(self.primary_action_btn)
        root.addLayout(foot)

        self.apply_theme(self._mode)

    def _build_param_controls(self, parent: QWidget) -> None:
        txt = _WS_TEXTS[self._lang]
        for spec in self.tool_def.params:
            if spec.name == "dpi":
                combo = QComboBox(parent)
                combo.setEditable(True)
                for c in (spec.choices or (72, 150, 200, 300, 600)):
                    combo.addItem(str(c), int(c))
                idx = combo.findData(int(spec.default or 150))
                if idx >= 0:
                    combo.setCurrentIndex(idx)
                else:
                    combo.setEditText(str(spec.default or 150))
                self.dpi_control = combo
                self._param_widgets[spec.name] = combo
                self.params_form.addRow(txt["dpi_label"], combo)
            elif spec.name == "quality":
                spin = QSpinBox(parent)
                spin.setRange(int(spec.min_val or 10), int(spec.max_val or 100))
                spin.setValue(int(spec.default or 75))
                self.quality_control = spin
                self._param_widgets[spec.name] = spin
                self.params_form.addRow(txt["quality_label"], spin)
            elif spec.name == "source_lang":
                combo = QComboBox(parent)
                for code in (spec.choices or ("auto", "es", "en", "pt", "fr", "de", "it", "multi")):
                    combo.addItem(str(code), str(code))
                idx = combo.findData(str(spec.default or "auto"))
                if idx >= 0:
                    combo.setCurrentIndex(idx)
                self.lang_control = combo
                self._param_widgets[spec.name] = combo
                self.params_form.addRow(txt["lang_label"], combo)
            else:
                widget = self._make_generic_control(spec, parent)
                self._param_widgets[spec.name] = widget
                self.params_form.addRow(spec.label or spec.name, widget)

    def _make_generic_control(self, spec: ToolParamSpec, parent: QWidget) -> QWidget:
        if spec.kind == "bool":
            chk = QCheckBox(parent)
            chk.setChecked(bool(spec.default))
            return chk
        if spec.choices:
            combo = QComboBox(parent)
            combo.setEditable(spec.allow_custom)
            for item in spec.choices:
                combo.addItem(str(item), item)
            idx = combo.findData(spec.default)
            if idx >= 0:
                combo.setCurrentIndex(idx)
            elif spec.default is not None:
                combo.setEditText(str(spec.default))
            return combo
        if spec.kind == "int":
            spin = QSpinBox(parent)
            spin.setRange(
                int(spec.min_val if spec.min_val is not None else -999999),
                int(spec.max_val if spec.max_val is not None else 999999),
            )
            spin.setValue(int(spec.default or 0))
            return spin
        if spec.kind == "float":
            dspin = QDoubleSpinBox(parent)
            dspin.setRange(
                float(spec.min_val if spec.min_val is not None else -1e6),
                float(spec.max_val if spec.max_val is not None else 1e6),
            )
            dspin.setValue(float(spec.default or 0.0))
            return dspin
        edit = QLineEdit(parent)
        if spec.default is not None and not isinstance(spec.default, (list, dict)):
            edit.setText(str(spec.default))
        return edit

    def apply_theme(self, mode: str) -> None:
        self._mode = "light" if mode == "light" else "dark"
        p = get_palette(self._mode)
        self.status_tag.apply_theme(self._mode)
        self.add_files_btn.setIcon(load_svg_icon("folder", color=p["icon_color"], size=14))
        self.clear_files_btn.setIcon(load_svg_icon("trash", color=p["icon_color"], size=14))
        self.browse_out_btn.setIcon(load_svg_icon("folder", color=p["icon_color"], size=14))
        self.primary_action_btn.setIcon(load_svg_icon("play", color=p["primary_fg"], size=14))

    def add_input_path(self, path: str | Path) -> bool:
        """Validate and append a local input path; rejects UNC/URL/missing/invalid files."""
        raw = str(path).strip()
        if not raw:
            return False
        probe = [*self._inputs, Path(raw)]
        orig_min = self.tool_def.min_inputs
        try:
            # Validate candidate list via ToolDefinition._validate_inputs (ignoring min_inputs if accumulating)
            ok, validated, _reason = self.tool_def._validate_inputs(probe)
            if not ok:
                ok_single, val_single, _ = self.tool_def._validate_inputs([Path(raw)])
                if not ok_single or not val_single:
                    return False
                candidate = val_single[0]
            else:
                candidate = validated[-1]
        except Exception:
            return False
        del orig_min
        if candidate not in self._inputs:
            self._inputs.append(candidate)
            self.inputs_list.addItem(str(candidate))
        return True

    def clear_inputs(self) -> None:
        self._inputs.clear()
        self.inputs_list.clear()

    def set_inputs(self, paths: list[str | Path]) -> bool:
        self.clear_inputs()
        all_ok = True
        for p in paths:
            if not self.add_input_path(p):
                all_ok = False
        return all_ok

    def set_output(self, path: str | Path) -> bool:
        ok, out_p, _ = self.tool_def._validate_output(path)
        if not ok:
            return False
        self._output = out_p
        self.output_edit.blockSignals(True)
        self.output_edit.setText(str(out_p) if out_p is not None else "")
        self.output_edit.blockSignals(False)
        return True

    def _on_output_text_changed(self, text: str) -> None:
        clean = text.strip()
        self._output = Path(clean) if clean else None

    def collect_params(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        spec_map = {s.name: s for s in self.tool_def.params}
        for name, widget in self._param_widgets.items():
            spec = spec_map.get(name)
            if isinstance(widget, QCheckBox):
                out[name] = widget.isChecked()
            elif isinstance(widget, QSpinBox):
                out[name] = int(widget.value())
            elif isinstance(widget, QDoubleSpinBox):
                out[name] = float(widget.value())
            elif isinstance(widget, QComboBox):
                data = widget.currentData()
                text = widget.currentText().strip()
                if spec and spec.kind == "int":
                    try:
                        out[name] = int(text) if widget.isEditable() and text else int(data)
                    except (TypeError, ValueError):
                        out[name] = text
                else:
                    out[name] = data if (data is not None and not widget.isEditable()) else text
            elif isinstance(widget, QLineEdit):
                txt = widget.text().strip()
                if txt or (spec and spec.kind == "str"):
                    if spec and spec.kind == "list":
                        out[name] = [part.strip() for part in txt.split(",") if part.strip()]
                    else:
                        out[name] = txt
        return out

    def validate_params(self, params: dict[str, Any] | None = None) -> bool:
        target = self.collect_params() if params is None else params
        ok, _merged, _err = self.tool_def.validate_params(target)
        return ok

    def _browse_inputs(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self,
            _WS_TEXTS[self._lang]["add_files"],
            "",
            "Documentos y Medios (*.pdf *.md *.markdown *.png *.jpg *.jpeg *.webp *.xml *.json *.txt);;Todos (*.*)",
        )
        for f in files:
            self.add_input_path(f)

    def _browse_output(self) -> None:
        if self.tool_def.output_kind == "dir":
            chosen = QFileDialog.getExistingDirectory(self, _WS_TEXTS[self._lang]["browse_out"])
        else:
            chosen, _ = QFileDialog.getSaveFileName(
                self,
                _WS_TEXTS[self._lang]["browse_out"],
                str(self._output or "resultado.pdf"),
                "PDF / Archivos (*.pdf *.md *.xml *.json *.png *.zip);;Todos (*.*)",
            )
        if chosen:
            self.set_output(Path(chosen))

    def _on_progress(self, pct: int, msg: str = "") -> None:
        self.progress_bar.setValue(max(0, min(100, int(pct))))
        if msg:
            self.summary_lbl.setText(msg)

    def execute_primary_action(self) -> ToolResult:
        txt = _WS_TEXTS[self._lang]
        self.status_tag.set_tone("warn", txt["running"])
        self.progress_bar.setValue(10)
        params = self.collect_params()
        result = self.tool_def.run(
            self._inputs,
            self._output,
            params,
            progress_cb=self._on_progress,
        )
        self._last_result = result
        if result.success:
            self.progress_bar.setValue(100)
            self.status_tag.set_tone("ok", txt["done"])
        else:
            self.status_tag.set_tone("risk", txt["error"])
        self.summary_lbl.setText(result.summary)
        self.completed.emit(result)
        return result

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        if event.mimeData().hasUrls():
            added = False
            for url in event.mimeData().urls():
                if url.isLocalFile() and self.add_input_path(Path(url.toLocalFile())):
                    added = True
            if added:
                event.acceptProposedAction()
                return
        super().dropEvent(event)
