"""Dedicated drag-and-drop ToolWorkspace with live preview, multi-file batch queue, output prompt, and 1 PrimaryAction."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QFont, QPixmap
from PySide6.QtWidgets import (
    QApplication,
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
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ..core.i18n import t
from ..core.md_security import sanitize_markdown_html
from ..core.pdf_engine import PdfDocumentSession
from ..core.tools import ToolDefinition, ToolParamSpec, ToolResult, validate_output_dir_path
from ..theme.palette import MONO_FONT_FAMILY, UI_FONT_FAMILY, get_palette
from .svg_icons import load_svg_icon
from .widgets import AdaptiveButton, FlowLayout, StatusTag

_MULTI_INPUT_COMBINE_TOOLS = frozenset(
    {"merge_pdf", "compare_pdf", "images_to_pdf", "overlay_pdf", "job_application_pdf"}
)

_WS_TEXTS: dict[str, dict[str, str]] = {
    "es": {
        "back": "Volver a Herramientas",
        "inputs": "Cola de archivos de entrada (arrastra o selecciona varios)",
        "add_files": "Agregar archivos",
        "move_up": "Subir",
        "move_down": "Bajar",
        "remove_file": "Quitar",
        "clear_files": "Limpiar",
        "output": "Destino de salida (se preguntara si esta vacio)",
        "output_placeholder": "Elige carpeta o archivo de salida...",
        "browse_out": "Elegir salida",
        "params": "Subopciones y parametros de la herramienta",
        "preview": "Previsualizacion del archivo",
        "preview_empty": "Selecciona o agrega un PDF, Markdown o imagen para previsualizarlo antes de procesar.",
        "preview_meta": "{name}  ·  {pages} pag(s)  ·  {kb} KB",
        "queue_badge": "COLA: {count} ARCHIVOS",
        "run": "Ejecutar herramienta",
        "open_result": "Abrir en Visor",
        "open_folder": "Abrir carpeta",
        "ready": "LISTO",
        "running": "PROCESANDO",
        "done": "COMPLETADO",
        "error": "ERROR",
        "cancelled": "Salida no seleccionada (cancelado por el usuario).",
        "batch_summary": "Cola completada: {ok}/{total} archivos procesados correctamente.",
        "drop_hint": "Suelta uno o varios archivos locales aqui. Herramientas individuales procesan la cola por lotes.",
        "dpi_label": "Resolucion (DPI)",
        "quality_label": "Calidad (10-100)",
        "lang_label": "Idioma del documento",
    },
    "en": {
        "back": "Back to Tools",
        "inputs": "Input queue (drag & drop or add multiple files)",
        "add_files": "Add files",
        "move_up": "Up",
        "move_down": "Down",
        "remove_file": "Remove",
        "clear_files": "Clear",
        "output": "Output destination (prompted if empty)",
        "output_placeholder": "Choose output folder or file...",
        "browse_out": "Choose output",
        "params": "Tool sub-options & parameters",
        "preview": "File preview",
        "preview_empty": "Add or select a PDF, Markdown, or image file to preview before running.",
        "preview_meta": "{name}  ·  {pages} page(s)  ·  {kb} KB",
        "queue_badge": "QUEUE: {count} FILES",
        "run": "Run tool",
        "open_result": "Open in Viewer",
        "open_folder": "Open folder",
        "ready": "READY",
        "running": "RUNNING",
        "done": "COMPLETED",
        "error": "ERROR",
        "cancelled": "Output location not chosen (cancelled).",
        "batch_summary": "Batch complete: {ok}/{total} files processed successfully.",
        "drop_hint": "Drop one or multiple local files here. Single-file tools run as a batch queue.",
        "dpi_label": "Resolution (DPI)",
        "quality_label": "Quality (10-100)",
        "lang_label": "Source language",
    },
    "pt": {
        "back": "Voltar as Ferramentas",
        "inputs": "Fila de entrada (arraste ou selecione varios)",
        "add_files": "Adicionar arquivos",
        "move_up": "Subir",
        "move_down": "Descer",
        "remove_file": "Remover",
        "clear_files": "Limpar",
        "output": "Destino de saida (sera solicitado se vazio)",
        "output_placeholder": "Escolha pasta ou arquivo de saida...",
        "browse_out": "Escolher saida",
        "params": "Sub-opcoes e parametros da ferramenta",
        "preview": "Pre-visualizacao do arquivo",
        "preview_empty": "Adicione ou selecione um PDF, Markdown ou imagem para visualizar.",
        "preview_meta": "{name}  ·  {pages} pag(s)  ·  {kb} KB",
        "queue_badge": "FILA: {count} ARQUIVOS",
        "run": "Executar ferramenta",
        "open_result": "Abrir no Visualizador",
        "open_folder": "Abrir pasta",
        "ready": "PRONTO",
        "running": "PROCESSANDO",
        "done": "CONCLUIDO",
        "error": "ERRO",
        "cancelled": "Destino nao escolhido (cancelado).",
        "batch_summary": "Fila concluida: {ok}/{total} arquivos processados.",
        "drop_hint": "Solte um ou varios arquivos locais aqui.",
        "dpi_label": "Resolucao (DPI)",
        "quality_label": "Qualidade (10-100)",
        "lang_label": "Idioma do documento",
    },
}


class ToolWorkspace(QWidget):
    """Unified tool runner workspace with live preview, multi-file queue, output prompt, and 1 PrimaryAction."""

    completed = Signal(object)
    back_requested = Signal()
    open_output_requested = Signal(object)

    def __init__(
        self,
        tool_def: ToolDefinition,
        *,
        lang: str = "es",
        mode: str = "dark",
        show_back: bool = False,
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
        self._preview_page_index: int = 0
        self._preview_page_count: int = 0

        self.dpi_control: QComboBox | None = None
        self.quality_control: QSpinBox | None = None
        self.lang_control: QComboBox | None = None

        self.setAcceptDrops(True)
        self.setMinimumSize(640, 480)
        txt = _WS_TEXTS[self._lang]
        self.setWindowTitle(t(tool_def.title_key, self._lang))

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        # Header
        head = QHBoxLayout()
        head.setSpacing(10)

        self.back_btn = AdaptiveButton(txt["back"], self, role="GhostAction")
        self.back_btn.setVisible(bool(show_back))
        self.back_btn.clicked.connect(self.back_requested.emit)
        head.addWidget(self.back_btn, 0, Qt.AlignmentFlag.AlignTop)

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

        self.queue_tag = StatusTag("", tone="accent", parent=self)
        self.queue_tag.setVisible(False)
        head.addWidget(self.queue_tag, 0, Qt.AlignmentFlag.AlignTop)

        self.status_tag = StatusTag(txt["ready"], tone="accent", parent=self)
        head.addWidget(self.status_tag, 0, Qt.AlignmentFlag.AlignTop)
        root.addLayout(head)

        # Split Left (Inputs/Queue, Output, Sub-options) + Right (Live Preview)
        self._splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self._splitter.setHandleWidth(3)

        left_scroll = QScrollArea(self._splitter)
        left_scroll.setWidgetResizable(True)
        left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        host = QWidget(left_scroll)
        host.setObjectName("TransparentHost")
        body = QVBoxLayout(host)
        body.setContentsMargins(0, 0, 6, 4)
        body.setSpacing(10)

        # Input Drop + Batch Queue Card
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
        self.inputs_list.setMinimumHeight(96)
        self.inputs_list.setMaximumHeight(150)
        self.inputs_list.currentRowChanged.connect(self._on_selected_input_changed)
        in_lay.addWidget(self.inputs_list)

        in_btns = FlowLayout(h_spacing=6, v_spacing=6)
        self.add_files_btn = AdaptiveButton(txt["add_files"], in_card, role="GhostAction")
        self.add_files_btn.clicked.connect(self._browse_inputs)
        in_btns.addWidget(self.add_files_btn)

        self.move_up_btn = AdaptiveButton(txt["move_up"], in_card, role="GhostAction")
        self.move_up_btn.clicked.connect(lambda: self._move_selected_input(-1))
        in_btns.addWidget(self.move_up_btn)

        self.move_down_btn = AdaptiveButton(txt["move_down"], in_card, role="GhostAction")
        self.move_down_btn.clicked.connect(lambda: self._move_selected_input(1))
        in_btns.addWidget(self.move_down_btn)

        self.remove_file_btn = AdaptiveButton(txt["remove_file"], in_card, role="GhostAction")
        self.remove_file_btn.clicked.connect(self._remove_selected_input)
        in_btns.addWidget(self.remove_file_btn)

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
        self.output_edit.setPlaceholderText(txt["output_placeholder"])
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
        left_scroll.setWidget(host)
        self._splitter.addWidget(left_scroll)

        # Right Preview Panel
        self.preview_card = QFrame(self._splitter)
        self.preview_card.setObjectName("CardPanel")
        self.preview_card.setMinimumWidth(260)
        pv_lay = QVBoxLayout(self.preview_card)
        pv_lay.setContentsMargins(12, 10, 12, 10)
        pv_lay.setSpacing(8)

        pv_head = QHBoxLayout()
        pv_hdr = QLabel(txt["preview"], self.preview_card)
        pv_hdr.setFont(QFont(UI_FONT_FAMILY, 10, QFont.Weight.Bold))
        pv_head.addWidget(pv_hdr, 1)

        self.pv_prev_btn = QPushButton("", self.preview_card)
        self.pv_prev_btn.setProperty("role", "GhostAction")
        self.pv_prev_btn.setFixedSize(26, 24)
        self.pv_prev_btn.clicked.connect(lambda: self._step_preview_page(-1))
        pv_head.addWidget(self.pv_prev_btn)

        self.pv_page_tag = StatusTag("0 / 0", tone="muted", parent=self.preview_card)
        pv_head.addWidget(self.pv_page_tag)

        self.pv_next_btn = QPushButton("", self.preview_card)
        self.pv_next_btn.setProperty("role", "GhostAction")
        self.pv_next_btn.setFixedSize(26, 24)
        self.pv_next_btn.clicked.connect(lambda: self._step_preview_page(1))
        pv_head.addWidget(self.pv_next_btn)
        pv_lay.addLayout(pv_head)

        self.preview_meta_lbl = QLabel(txt["preview_empty"], self.preview_card)
        self.preview_meta_lbl.setProperty("mono", "true")
        self.preview_meta_lbl.setProperty("tone", "muted")
        self.preview_meta_lbl.setWordWrap(True)
        pv_lay.addWidget(self.preview_meta_lbl)

        self.preview_image_lbl = QLabel(txt["preview_empty"], self.preview_card)
        self.preview_image_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_image_lbl.setWordWrap(True)
        self.preview_image_lbl.setMinimumHeight(240)
        pv_lay.addWidget(self.preview_image_lbl, 1)

        self.preview_text_browser = QTextBrowser(self.preview_card)
        self.preview_text_browser.setOpenExternalLinks(False)
        self.preview_text_browser.setVisible(False)
        pv_lay.addWidget(self.preview_text_browser, 1)

        self._splitter.addWidget(self.preview_card)
        self._splitter.setSizes([520, 340])
        root.addWidget(self._splitter, 1)

        # Footer: Progress + Secondary Result Buttons + Single PrimaryAction
        self.progress_bar = QProgressBar(self)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFixedHeight(6)
        self.progress_bar.setTextVisible(False)
        root.addWidget(self.progress_bar)

        foot = QHBoxLayout()
        foot.setSpacing(8)
        self.summary_lbl = QLabel("", self)
        self.summary_lbl.setProperty("mono", "true")
        self.summary_lbl.setProperty("tone", "secondary")
        self.summary_lbl.setFont(QFont(MONO_FONT_FAMILY, 8))
        self.summary_lbl.setWordWrap(True)
        foot.addWidget(self.summary_lbl, 1)

        self.open_result_btn = AdaptiveButton(txt["open_result"], self, role="GhostAction")
        self.open_result_btn.setVisible(False)
        self.open_result_btn.clicked.connect(self._emit_open_first_result)
        foot.addWidget(self.open_result_btn)

        self.open_folder_btn = AdaptiveButton(txt["open_folder"], self, role="GhostAction")
        self.open_folder_btn.setVisible(False)
        self.open_folder_btn.clicked.connect(self._open_result_folder)
        foot.addWidget(self.open_folder_btn)

        self.primary_action_btn = AdaptiveButton(txt["run"], self, role="PrimaryAction")
        self.primary_action_btn.clicked.connect(self.execute_primary_action)
        foot.addWidget(self.primary_action_btn)
        root.addLayout(foot)

        self._sync_preview_controls(False)
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
        self.queue_tag.apply_theme(self._mode)
        self.pv_page_tag.apply_theme(self._mode)
        self.back_btn.setIcon(load_svg_icon("prev", color=p["icon_color"], size=14))
        self.add_files_btn.setIcon(load_svg_icon("folder", color=p["icon_color"], size=14))
        self.clear_files_btn.setIcon(load_svg_icon("trash", color=p["icon_color"], size=14))
        self.browse_out_btn.setIcon(load_svg_icon("folder", color=p["icon_color"], size=14))
        self.pv_prev_btn.setIcon(load_svg_icon("prev", color=p["icon_color"], size=13))
        self.pv_next_btn.setIcon(load_svg_icon("next", color=p["icon_color"], size=13))
        self.open_result_btn.setIcon(load_svg_icon("viewer", color=p["icon_color"], size=14))
        self.open_folder_btn.setIcon(load_svg_icon("folder", color=p["icon_color"], size=14))
        self.primary_action_btn.setIcon(load_svg_icon("play", color=p["primary_fg"], size=14))
        self.preview_image_lbl.setStyleSheet(
            f"background-color: {p['bg_canvas']}; border: 1px solid {p['border_strong']}; "
            f"padding: 10px; color: {p['text_muted']};"
        )

    def _is_batch_mode(self) -> bool:
        if len(self._inputs) <= 1:
            return False
        if self.tool_def.max_inputs == 1:
            return True
        return self.tool_def.id not in _MULTI_INPUT_COMBINE_TOOLS

    def _sync_queue_ui(self) -> None:
        txt = _WS_TEXTS[self._lang]
        is_batch = self._is_batch_mode()
        self.queue_tag.setVisible(is_batch)
        if is_batch:
            self.queue_tag.set_tone("accent", txt["queue_badge"].format(count=len(self._inputs)))

    def _refresh_list_labels(self, statuses: dict[int, str] | None = None) -> None:
        cur = self.inputs_list.currentRow()
        self.inputs_list.blockSignals(True)
        self.inputs_list.clear()
        for idx, p in enumerate(self._inputs):
            prefix = f"[{statuses[idx]}] " if (statuses and idx in statuses) else ""
            self.inputs_list.addItem(f"{prefix}{p}")
        if self._inputs:
            self.inputs_list.setCurrentRow(max(0, min(cur, len(self._inputs) - 1)))
        self.inputs_list.blockSignals(False)
        self._sync_queue_ui()

    def add_input_path(self, path: str | Path) -> bool:
        """Validate and append a local input path; rejects UNC/URL/missing/invalid files."""
        raw = str(path).strip()
        if not raw:
            return False
        probe = [*self._inputs, Path(raw)]
        try:
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
        if candidate not in self._inputs:
            self._inputs.append(candidate)
            self.inputs_list.addItem(str(candidate))
            self.inputs_list.setCurrentRow(len(self._inputs) - 1)
            self._sync_queue_ui()
            self._preview_file(candidate, page_index=0)
        return True

    def clear_inputs(self) -> None:
        self._inputs.clear()
        self.inputs_list.clear()
        self._sync_queue_ui()
        self._clear_preview()

    def set_inputs(self, paths: list[str | Path]) -> bool:
        self.clear_inputs()
        all_ok = True
        for p in paths:
            if not self.add_input_path(p):
                all_ok = False
        return all_ok

    def _move_selected_input(self, delta: int) -> None:
        row = self.inputs_list.currentRow()
        if row < 0 or row >= len(self._inputs):
            return
        new_row = row + delta
        if not (0 <= new_row < len(self._inputs)):
            return
        self._inputs[row], self._inputs[new_row] = self._inputs[new_row], self._inputs[row]
        self._refresh_list_labels()
        self.inputs_list.setCurrentRow(new_row)

    def _remove_selected_input(self) -> None:
        row = self.inputs_list.currentRow()
        if 0 <= row < len(self._inputs):
            self._inputs.pop(row)
            self._refresh_list_labels()
            if self._inputs:
                sel = max(0, min(row, len(self._inputs) - 1))
                self.inputs_list.setCurrentRow(sel)
                self._preview_file(self._inputs[sel], page_index=0)
            else:
                self._clear_preview()

    def _on_selected_input_changed(self, row: int) -> None:
        if 0 <= row < len(self._inputs):
            self._preview_file(self._inputs[row], page_index=0)
        else:
            self._clear_preview()

    def _sync_preview_controls(self, is_pdf: bool) -> None:
        self.pv_prev_btn.setEnabled(is_pdf and self._preview_page_count > 1)
        self.pv_next_btn.setEnabled(is_pdf and self._preview_page_count > 1)
        if is_pdf and self._preview_page_count > 0:
            self.pv_page_tag.setText(f"{self._preview_page_index + 1} / {self._preview_page_count}")
        else:
            self.pv_page_tag.setText("—")

    def _step_preview_page(self, delta: int) -> None:
        row = self.inputs_list.currentRow()
        if not (0 <= row < len(self._inputs)):
            return
        nxt = max(0, min(self._preview_page_count - 1, self._preview_page_index + delta))
        if nxt != self._preview_page_index:
            self._preview_file(self._inputs[row], page_index=nxt)

    def _clear_preview(self) -> None:
        txt = _WS_TEXTS[self._lang]
        self._preview_page_index = 0
        self._preview_page_count = 0
        self.preview_meta_lbl.setText(txt["preview_empty"])
        self.preview_text_browser.setVisible(False)
        self.preview_image_lbl.setVisible(True)
        self.preview_image_lbl.setPixmap(QPixmap())
        self.preview_image_lbl.setText(txt["preview_empty"])
        self._sync_preview_controls(False)

    def _preview_file(self, path: Path, *, page_index: int = 0) -> None:
        txt = _WS_TEXTS[self._lang]
        if not path.exists() or not path.is_file():
            self._clear_preview()
            return
        kb = max(1, (path.stat().st_size + 1023) // 1024)
        suf = path.suffix.lower()

        if suf == ".pdf":
            try:
                sess = PdfDocumentSession(path)
                try:
                    total = sess.page_count
                    p_idx = max(0, min(total - 1, int(page_index)))
                    qimg = sess.render_page(p_idx, scale=0.85)
                    self._preview_page_index = p_idx
                    self._preview_page_count = total
                    pm = QPixmap.fromImage(qimg)
                    target_w = max(210, self.preview_image_lbl.width() - 12)
                    target_h = max(220, self.preview_image_lbl.height() - 12)
                    self.preview_text_browser.setVisible(False)
                    self.preview_image_lbl.setVisible(True)
                    self.preview_image_lbl.setPixmap(
                        pm.scaled(target_w, target_h, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
                    )
                    self.preview_meta_lbl.setText(txt["preview_meta"].format(name=path.name, pages=total, kb=kb))
                    self._sync_preview_controls(True)
                    return
                finally:
                    sess.close()
            except Exception:
                pass

        if suf in (".png", ".jpg", ".jpeg", ".webp", ".bmp"):
            pm = QPixmap(str(path))
            if not pm.isNull():
                self._preview_page_index = 0
                self._preview_page_count = 1
                self.preview_text_browser.setVisible(False)
                self.preview_image_lbl.setVisible(True)
                target_w = max(210, self.preview_image_lbl.width() - 12)
                target_h = max(220, self.preview_image_lbl.height() - 12)
                self.preview_image_lbl.setPixmap(
                    pm.scaled(target_w, target_h, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
                )
                self.preview_meta_lbl.setText(txt["preview_meta"].format(name=path.name, pages=1, kb=kb))
                self._sync_preview_controls(False)
                return

        if suf in (".md", ".markdown", ".txt", ".json", ".xml"):
            try:
                content = path.read_text(encoding="utf-8", errors="replace")[:12000]
                self._preview_page_index = 0
                self._preview_page_count = 1
                self.preview_image_lbl.setVisible(False)
                self.preview_text_browser.setVisible(True)
                if suf in (".md", ".markdown"):
                    import markdown  # noqa: PLC0415

                    raw_html = markdown.markdown(content, extensions=["tables", "fenced_code"])
                    self.preview_text_browser.setHtml(sanitize_markdown_html(raw_html, base_dir=path.parent).html)
                else:
                    self.preview_text_browser.setPlainText(content)
                self.preview_meta_lbl.setText(txt["preview_meta"].format(name=path.name, pages=1, kb=kb))
                self._sync_preview_controls(False)
                return
            except Exception:
                pass

        self.preview_meta_lbl.setText(txt["preview_meta"].format(name=path.name, pages=1, kb=kb))
        self.preview_text_browser.setVisible(False)
        self.preview_image_lbl.setVisible(True)
        self.preview_image_lbl.setPixmap(QPixmap())
        self.preview_image_lbl.setText(path.name)
        self._sync_preview_controls(False)

    def set_output(self, path: str | Path) -> bool:
        if self._is_batch_mode():
            ok, out_p, _ = validate_output_dir_path(path)
        else:
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

    def _suggested_output_ext(self, params: dict[str, Any]) -> str:
        tid = self.tool_def.id
        if "convert_from_pdf" in tid:
            fmt = str(params.get("output_format") or "txt").lower().lstrip(".")
            return f".{fmt if fmt else 'txt'}"
        if tid == "create_einvoice" or "xml" in tid:
            return ".xml"
        if tid in ("check_pdfa", "validate_einvoice"):
            return ".json"
        return ".pdf"

    def _prompt_output_location(self, params: dict[str, Any]) -> bool:
        """Always prompt the user where to save output when no destination is set yet."""
        if self.tool_def.output_kind == "dir" or self._is_batch_mode():
            chosen = QFileDialog.getExistingDirectory(self, _WS_TEXTS[self._lang]["browse_out"])
            if not chosen:
                return False
            return self.set_output(Path(chosen))

        first = self._inputs[0] if self._inputs else Path("documento.pdf")
        ext = self._suggested_output_ext(params)
        default_dir = first.parent if first.parent.exists() else Path.cwd()
        suggested = default_dir / f"{first.stem}_{self.tool_def.id}{ext}"
        chosen, _ = QFileDialog.getSaveFileName(
            self,
            _WS_TEXTS[self._lang]["browse_out"],
            str(suggested),
            "PDF / Archivos (*.pdf *.md *.txt *.docx *.xml *.json *.png *.zip);;Todos (*.*)",
        )
        if not chosen:
            return False
        return self.set_output(Path(chosen))

    def _browse_output(self) -> None:
        self._prompt_output_location(self.collect_params())

    def _on_progress(self, pct: int, msg: str = "") -> None:
        self.progress_bar.setValue(max(0, min(100, int(pct))))
        if msg:
            self.summary_lbl.setText(msg)

    def _batch_item_output_path(self, out_base: Path, item: Path, params: dict[str, Any]) -> Path:
        out_dir = out_base if (out_base.is_dir() or not out_base.suffix) else out_base.parent
        out_dir.mkdir(parents=True, exist_ok=True)
        if self.tool_def.output_kind == "dir":
            sub = out_dir / f"{item.stem}_{self.tool_def.id}"
            sub.mkdir(parents=True, exist_ok=True)
            return sub
        ext = self._suggested_output_ext(params)
        return out_dir / f"{item.stem}_{self.tool_def.id}{ext}"

    def execute_primary_action(self) -> ToolResult:
        txt = _WS_TEXTS[self._lang]
        params = self.collect_params()

        # In interactive desktop sessions, always ask where output goes before running if empty
        if self._output is None and not self.tool_def.allow_empty_output and QApplication.platformName() != "offscreen":
            if self._inputs or self.tool_def.min_inputs == 0:
                if not self._prompt_output_location(params):
                    res = ToolResult(success=False, output_paths=[], summary=txt["cancelled"])
                    self._last_result = res
                    self.status_tag.set_tone("ready" if False else "accent", txt["ready"])
                    self.summary_lbl.setText(res.summary)
                    self.completed.emit(res)
                    return res

        self.status_tag.set_tone("warn", txt["running"])
        self.progress_bar.setValue(10)
        self.open_result_btn.setVisible(False)
        self.open_folder_btn.setVisible(False)

        if self._is_batch_mode() and self._output is not None:
            statuses: dict[int, str] = {}
            outputs: list[Path] = []
            ok_count = 0
            total = len(self._inputs)
            for idx, item in enumerate(self._inputs):
                statuses[idx] = "..."
                self._refresh_list_labels(statuses)
                self.progress_bar.setValue(max(10, int((idx / max(1, total)) * 90)))
                QApplication.processEvents()
                item_out = self._batch_item_output_path(self._output, item, params)
                res_i = self.tool_def.run([item], item_out, params)
                if res_i.success:
                    ok_count += 1
                    statuses[idx] = "OK"
                    outputs.extend(res_i.output_paths)
                else:
                    statuses[idx] = "ERR"
            self._refresh_list_labels(statuses)
            result = ToolResult(
                success=(ok_count == total and total > 0),
                output_paths=outputs,
                summary=txt["batch_summary"].format(ok=ok_count, total=total),
                details={"batch_total": total, "batch_ok": ok_count},
            )
        else:
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
            first_viewable = next(
                (Path(p) for p in result.output_paths if Path(p).suffix.lower() in (".pdf", ".md", ".markdown") and Path(p).exists()),
                None,
            )
            self.open_result_btn.setVisible(first_viewable is not None)
            self.open_folder_btn.setVisible(bool(result.output_paths))
        else:
            self.status_tag.set_tone("risk", txt["error"])
        self.summary_lbl.setText(result.summary)
        self.completed.emit(result)
        return result

    def _emit_open_first_result(self) -> None:
        if self._last_result is None:
            return
        for p in self._last_result.output_paths:
            path = Path(p)
            if path.exists() and path.suffix.lower() in (".pdf", ".md", ".markdown"):
                self.open_output_requested.emit(path)
                return

    def _open_result_folder(self) -> None:
        if self._last_result is None or not self._last_result.output_paths:
            return
        first = Path(self._last_result.output_paths[0])
        folder = first if first.is_dir() else first.parent
        if folder.exists() and QApplication.platformName() != "offscreen" and hasattr(os, "startfile"):
            os.startfile(str(folder))  # noqa: S606

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
