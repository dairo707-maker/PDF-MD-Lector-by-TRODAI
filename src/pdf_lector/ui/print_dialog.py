"""System Print Dialog with dynamic Windows printer & paper discovery, DPI, orientation & page range."""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QPageLayout, QPageSize, QPainter
from PySide6.QtPrintSupport import QPrintDialog, QPrinter, QPrinterInfo
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..core.pdf_engine import PdfDocumentSession
from ..theme.palette import MONO_FONT_FAMILY, UI_FONT_FAMILY, get_palette
from .svg_icons import load_svg_icon
from .widgets import AdaptiveButton, StatusTag
from PySide6.QtGui import QFont

_STANDARD_FALLBACK_PAPERS: tuple[tuple[str, QPageSize.PageSizeId], ...] = (
    ("Carta (Letter 8.5x11 in)", QPageSize.PageSizeId.Letter),
    ("A4 (210x297 mm)", QPageSize.PageSizeId.A4),
    ("Oficio / Legal (8.5x14 in)", QPageSize.PageSizeId.Legal),
    ("Tabloide / Ledger (11x17 in)", QPageSize.PageSizeId.Tabloid),
    ("A3 (297x420 mm)", QPageSize.PageSizeId.A3),
    ("A5 (148x210 mm)", QPageSize.PageSizeId.A5),
)


def parse_page_range(spec: str, total_pages: int) -> list[int]:
    """Parse 1-based page specification like '1-3, 5' into sorted 0-based page indices."""
    if total_pages <= 0:
        return []
    clean = (spec or "").strip()
    if not clean:
        return list(range(total_pages))

    indices: list[int] = []
    seen: set[int] = set()
    for part in clean.split(","):
        tok = part.strip()
        if not tok:
            continue
        if "-" in tok:
            bounds = tok.split("-", 1)
            if len(bounds) != 2:
                raise ValueError(f"Rango invalido: {tok}")
            try:
                start = int(bounds[0].strip())
                end = int(bounds[1].strip())
            except ValueError as exc:
                raise ValueError(f"Rango invalido: {tok}") from exc
            lo, hi = min(start, end), max(start, end)
            for p_num in range(lo, hi + 1):
                idx = p_num - 1
                if 0 <= idx < total_pages and idx not in seen:
                    seen.add(idx)
                    indices.append(idx)
        else:
            try:
                p_num = int(tok)
            except ValueError as exc:
                raise ValueError(f"Pagina invalida: {tok}") from exc
            idx = p_num - 1
            if 0 <= idx < total_pages and idx not in seen:
                seen.add(idx)
                indices.append(idx)

    if not indices:
        raise ValueError("El rango no coincide con ninguna pagina valida del documento.")
    return indices


def render_pages_to_printer(
    session: PdfDocumentSession,
    printer: QPrinter,
    page_indices: list[int],
    *,
    fit_to_page: bool = True,
    dpi: int = 300,
    auto_orientation: bool = False,
) -> int:
    """Render selected PDF pages cleanly to a configured QPrinter via QPainter."""
    if not page_indices:
        return 0
    scale = max(1.0, min(4.2, float(dpi) / 72.0))
    painter = QPainter()
    if not painter.begin(printer):
        raise RuntimeError("No se pudo iniciar la impresion en la impresora seleccionada.")

    printed = 0
    try:
        for i, page_idx in enumerate(page_indices):
            if i > 0:
                if auto_orientation:
                    try:
                        pw, ph = session.get_page_size(page_idx)
                        orient = (
                            QPageLayout.Orientation.Landscape
                            if pw > ph
                            else QPageLayout.Orientation.Portrait
                        )
                        printer.setPageOrientation(orient)
                    except Exception:
                        pass
                printer.newPage()

            img = session.render_page(page_idx, scale=scale, rotation=0)
            if img.isNull():
                continue
            vp = painter.viewport()
            if fit_to_page:
                target = img.size()
                target.scale(vp.size(), Qt.AspectRatioMode.KeepAspectRatio)
                x = (vp.width() - target.width()) // 2
                y = (vp.height() - target.height()) // 2
                painter.drawImage(vp.adjusted(x, y, -(vp.width() - x - target.width()), -(vp.height() - y - target.height())), img)
            else:
                painter.drawImage(0, 0, img)
            printed += 1
    finally:
        painter.end()
    return printed


class PrintPdfDialog(QDialog):
    """System-aware Print Dialog querying real printers, paper formats, DPI & page ranges."""

    def __init__(
        self,
        session: PdfDocumentSession,
        *,
        current_page: int = 0,
        doc_name: str = "Documento.pdf",
        mode: str = "dark",
        print_executor: Callable[[QPrinter, list[int], bool, int, bool], int] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._session = session
        self._current_page = max(0, min(max(0, session.page_count - 1), int(current_page)))
        self._doc_name = doc_name
        self._mode = "light" if mode == "light" else "dark"
        self._print_executor = print_executor
        self._printer = QPrinter(QPrinter.PrinterMode.HighResolution)
        self._printers_by_name: dict[str, QPrinterInfo] = {}

        self.setWindowTitle(f"Imprimir · {doc_name}")
        self.setMinimumWidth(480)
        self.resize(520, 460)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        # Header summary
        hdr_card = QFrame(self)
        hdr_card.setObjectName("CardPanel")
        hl = QHBoxLayout(hdr_card)
        hl.setContentsMargins(12, 10, 12, 10)
        title_lbl = QLabel(doc_name, hdr_card)
        title_lbl.setFont(QFont(UI_FONT_FAMILY, 11, QFont.Weight.Bold))
        hl.addWidget(title_lbl, 1)
        pages_tag = StatusTag(f"{session.page_count} pags.", tone="accent", parent=hdr_card)
        pages_tag.apply_theme(self._mode)
        hl.addWidget(pages_tag)
        root.addWidget(hdr_card)

        form_card = QFrame(self)
        form_card.setObjectName("CardPanel")
        form = QFormLayout(form_card)
        form.setContentsMargins(14, 12, 14, 12)
        form.setSpacing(9)

        self.printer_combo = QComboBox(form_card)
        self._populate_printers()
        self.printer_combo.currentIndexChanged.connect(self._on_printer_changed)
        form.addRow("Impresora:", self.printer_combo)

        self.paper_combo = QComboBox(form_card)
        form.addRow("Tamano de papel:", self.paper_combo)

        self.range_mode_combo = QComboBox(form_card)
        self.range_mode_combo.addItem(f"Todas las paginas (1-{max(1, session.page_count)})", "all")
        self.range_mode_combo.addItem(f"Pagina actual (p. {self._current_page + 1})", "current")
        self.range_mode_combo.addItem("Rango personalizado (ej. 1-3, 5)", "custom")
        self.range_mode_combo.currentIndexChanged.connect(self._on_range_mode_changed)
        form.addRow("Paginas:", self.range_mode_combo)

        self.range_edit = QLineEdit(form_card)
        self.range_edit.setPlaceholderText("Ej: 1-3, 5")
        self.range_edit.setEnabled(False)
        form.addRow("Especificar paginas:", self.range_edit)

        self.copies_spin = QSpinBox(form_card)
        self.copies_spin.setRange(1, 99)
        self.copies_spin.setValue(1)
        form.addRow("Copias:", self.copies_spin)

        self.orientation_combo = QComboBox(form_card)
        self.orientation_combo.addItem("Automatica (segun pagina)", "auto")
        self.orientation_combo.addItem("Vertical (Retrato)", "portrait")
        self.orientation_combo.addItem("Horizontal (Paisaje)", "landscape")
        form.addRow("Orientacion:", self.orientation_combo)

        self.dpi_combo = QComboBox(form_card)
        self.dpi_combo.addItem("150 DPI (Borrador rapido)", 150)
        self.dpi_combo.addItem("300 DPI (Estandar nitido)", 300)
        self.dpi_combo.addItem("600 DPI (Alta precision)", 600)
        self.dpi_combo.setCurrentIndex(1)
        form.addRow("Resolucion / Calidad:", self.dpi_combo)

        self.color_combo = QComboBox(form_card)
        self.color_combo.addItem("Color", "color")
        self.color_combo.addItem("Escala de grises (B/N)", "grayscale")
        form.addRow("Modo de color:", self.color_combo)

        self.scale_combo = QComboBox(form_card)
        self.scale_combo.addItem("Ajustar a pagina", "fit")
        self.scale_combo.addItem("Tamano real (100%)", "actual")
        form.addRow("Escala:", self.scale_combo)
        root.addWidget(form_card, 1)

        self._on_printer_changed()

        footer = QHBoxLayout()
        footer.setSpacing(8)
        self.native_props_btn = AdaptiveButton("Propiedades del controlador...", self, role="GhostAction")
        self.native_props_btn.clicked.connect(self._open_native_properties)
        footer.addWidget(self.native_props_btn)
        footer.addStretch(1)

        self.cancel_btn = AdaptiveButton("Cancelar", self, role="GhostAction")
        self.cancel_btn.clicked.connect(self.reject)
        footer.addWidget(self.cancel_btn)

        self.print_btn = AdaptiveButton("Imprimir", self, role="PrimaryAction")
        p = get_palette(self._mode)
        self.print_btn.setIcon(load_svg_icon("printer", color=p["primary_fg"], size=15))
        self.print_btn.clicked.connect(self._on_print_clicked)
        footer.addWidget(self.print_btn)
        root.addLayout(footer)

    def _populate_printers(self) -> None:
        self.printer_combo.blockSignals(True)
        self.printer_combo.clear()
        self._printers_by_name.clear()

        avail = QPrinterInfo.availablePrinters()
        def_name = QPrinterInfo.defaultPrinterName()
        default_idx = 0
        for idx, info in enumerate(avail):
            name = info.printerName()
            if not name:
                continue
            self._printers_by_name[name] = info
            label = f"{name} (Predeterminada)" if name == def_name else name
            self.printer_combo.addItem(label, name)
            if name == def_name:
                default_idx = self.printer_combo.count() - 1

        if self.printer_combo.count() == 0:
            self.printer_combo.addItem("Microsoft Print to PDF", "Microsoft Print to PDF")
        else:
            self.printer_combo.setCurrentIndex(default_idx)
        self.printer_combo.blockSignals(False)

    def _on_printer_changed(self) -> None:
        name = str(self.printer_combo.currentData() or "")
        info = self._printers_by_name.get(name)
        if info is not None and not info.isNull():
            self._printer = QPrinter(info, QPrinter.PrinterMode.HighResolution)
        self.paper_combo.clear()
        added = 0
        if info is not None and not info.isNull():
            seen_names: set[str] = set()
            for ps in info.supportedPageSizes()[:24]:
                pname = ps.name()
                if pname and pname not in seen_names:
                    seen_names.add(pname)
                    self.paper_combo.addItem(pname, ps.id())
                    added += 1
        if added == 0:
            for label, ps_id in _STANDARD_FALLBACK_PAPERS:
                self.paper_combo.addItem(label, ps_id)

    def _on_range_mode_changed(self) -> None:
        is_custom = str(self.range_mode_combo.currentData() or "all") == "custom"
        self.range_edit.setEnabled(is_custom)
        if is_custom:
            self.range_edit.setFocus()

    def _selected_page_indices(self) -> list[int]:
        mode = str(self.range_mode_combo.currentData() or "all")
        total = self._session.page_count
        if mode == "current":
            return [self._current_page] if 0 <= self._current_page < total else []
        if mode == "custom":
            return parse_page_range(self.range_edit.text(), total)
        return list(range(total))

    def _configure_printer(self) -> tuple[bool, int, bool]:
        self._printer.setDocName(self._doc_name)
        self._printer.setCopyCount(int(self.copies_spin.value()))

        ps_data = self.paper_combo.currentData()
        if isinstance(ps_data, QPageSize.PageSizeId):
            self._printer.setPageSize(QPageSize(ps_data))

        dpi = int(self.dpi_combo.currentData() or 300)
        self._printer.setResolution(dpi)

        is_gray = str(self.color_combo.currentData() or "color") == "grayscale"
        self._printer.setColorMode(
            QPrinter.ColorMode.GrayScale if is_gray else QPrinter.ColorMode.Color
        )

        orient_mode = str(self.orientation_combo.currentData() or "auto")
        auto_orient = orient_mode == "auto"
        if orient_mode == "landscape":
            self._printer.setPageOrientation(QPageLayout.Orientation.Landscape)
        else:
            self._printer.setPageOrientation(QPageLayout.Orientation.Portrait)

        fit = str(self.scale_combo.currentData() or "fit") == "fit"
        return fit, dpi, auto_orient

    def _open_native_properties(self) -> None:
        self._configure_printer()
        dlg = QPrintDialog(self._printer, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.copies_spin.setValue(max(1, self._printer.copyCount()))

    def _on_print_clicked(self) -> None:
        try:
            pages = self._selected_page_indices()
            fit, dpi, auto_orient = self._configure_printer()
            if self._print_executor is not None:
                self._print_executor(self._printer, pages, fit, dpi, auto_orient)
            else:
                render_pages_to_printer(
                    self._session,
                    self._printer,
                    pages,
                    fit_to_page=fit,
                    dpi=dpi,
                    auto_orientation=auto_orient,
                )
            self.accept()
        except Exception as exc:
            QMessageBox.warning(self, "Error al imprimir", str(exc))
