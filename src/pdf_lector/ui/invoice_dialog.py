"""Dual-profile InvoiceDialog (Colombia DIAN UBL 2.1 & International Factur-X / EN 16931)."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..core.tools import STANDARD_DPI_CHOICES, STANDARD_SOURCE_LANGS, ToolRegistry
from ..theme.palette import MONO_FONT_FAMILY, UI_FONT_FAMILY, get_palette
from .svg_icons import load_svg_icon
from .widgets import AdaptiveButton, FlowLayout, StatusTag


class InvoiceDialog(QDialog):
    """Unified dialog for visual invoices, DIAN UBL 2.1, Factur-X EN 16931, and validation."""

    def __init__(
        self,
        *,
        lang: str = "es",
        mode: str = "dark",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._lang = lang if lang in ("es", "en", "pt") else "es"
        self._mode = mode if mode in ("dark", "light") else "dark"

        self.setWindowTitle(
            "Facturas Duales · DIAN UBL 2.1 / Factur-X"
            if self._lang == "es"
            else "Dual Invoices · DIAN UBL 2.1 / Factur-X"
        )
        self.setMinimumSize(640, 520)
        self.resize(720, 580)

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(10)

        head = QHBoxLayout()
        title = QLabel(
            "Emision y Validacion de Facturas (DIAN + Factur-X)"
            if self._lang == "es"
            else "Invoice Generation & Validation (DIAN + Factur-X)",
            self,
        )
        title.setFont(QFont(UI_FONT_FAMILY, 14, QFont.Weight.Bold))
        head.addWidget(title, 1)

        self.status_tag = StatusTag("LOCAL · XML / PDF/A-3", tone="ok", parent=self)
        head.addWidget(self.status_tag)
        root.addLayout(head)

        form_card = QFrame(self)
        form_card.setObjectName("CardPanel")
        form = QFormLayout(form_card)
        form.setContentsMargins(12, 10, 12, 10)
        form.setSpacing(8)

        self.profile_combo = QComboBox(form_card)
        self.profile_combo.addItem("Colombia · DIAN UBL 2.1 (XML + NIT + IVA/INC + CUFE + QR)", "dian_ubl21")
        self.profile_combo.addItem("Internacional · Factur-X / ZUGFeRD EN 16931 (PDF/A-3)", "facturx_en16931")
        form.addRow("Perfil fiscal:", self.profile_combo)

        self.invoice_num_edit = QLineEdit("FE-2026-0001", form_card)
        form.addRow("Numero:", self.invoice_num_edit)

        self.emitter_name_edit = QLineEdit("TRODAI SAS", form_card)
        form.addRow("Emisor:", self.emitter_name_edit)

        self.emitter_nit_edit = QLineEdit("900123456-7", form_card)
        form.addRow("NIT / Tax ID:", self.emitter_nit_edit)

        self.buyer_name_edit = QLineEdit("Cliente Local SAS", form_card)
        form.addRow("Receptor:", self.buyer_name_edit)

        self.buyer_nit_edit = QLineEdit("800987654-1", form_card)
        form.addRow("NIT / ID Receptor:", self.buyer_nit_edit)

        self.item_desc_edit = QLineEdit("Servicios de procesamiento documental", form_card)
        form.addRow("Concepto:", self.item_desc_edit)

        qty_row = QHBoxLayout()
        self.qty_spin = QSpinBox(form_card)
        self.qty_spin.setRange(1, 100000)
        self.qty_spin.setValue(1)
        qty_row.addWidget(self.qty_spin)

        self.price_spin = QDoubleSpinBox(form_card)
        self.price_spin.setRange(0.01, 1e9)
        self.price_spin.setValue(100000.0)
        self.price_spin.setDecimals(2)
        qty_row.addWidget(self.price_spin, 1)

        self.iva_combo = QComboBox(form_card)
        for rate in (0.19, 0.05, 0.0):
            self.iva_combo.addItem(f"IVA {int(rate * 100)}%", rate)
        qty_row.addWidget(self.iva_combo)
        form.addRow("Cant. / Valor / IVA:", qty_row)

        opts_row = QHBoxLayout()
        self.dpi_combo = QComboBox(form_card)
        for dpi in STANDARD_DPI_CHOICES:
            self.dpi_combo.addItem(f"{dpi} DPI", dpi)
        self.dpi_combo.setCurrentIndex(1)
        opts_row.addWidget(self.dpi_combo)

        self.lang_combo = QComboBox(form_card)
        for lc in STANDARD_SOURCE_LANGS:
            self.lang_combo.addItem(lc, lc)
        opts_row.addWidget(self.lang_combo)
        form.addRow("Salida / Idioma:", opts_row)

        self.online_consent_chk = QCheckBox(
            "Autorizar envio/extraccion mediante conector online (requiere consentimiento explicito)",
            form_card,
        )
        self.online_consent_chk.setChecked(False)
        form.addRow("", self.online_consent_chk)

        root.addWidget(form_card)

        self.output_log = QPlainTextEdit(self)
        self.output_log.setReadOnly(True)
        self.output_log.setFont(QFont(MONO_FONT_FAMILY, 8))
        self.output_log.setMaximumHeight(140)
        root.addWidget(self.output_log, 1)

        actions = FlowLayout(h_spacing=8, v_spacing=6)
        self.create_btn = AdaptiveButton("Generar Factura Electronica", self, role="PrimaryAction")
        self.create_btn.clicked.connect(self._run_create_einvoice)
        actions.addWidget(self.create_btn)

        self.visual_btn = AdaptiveButton("Crear PDF Visual", self, role="GhostAction")
        self.visual_btn.clicked.connect(self._run_visual_invoice)
        actions.addWidget(self.visual_btn)

        self.validate_btn = AdaptiveButton("Validar XML / PDF/A-3", self, role="GhostAction")
        self.validate_btn.clicked.connect(self._run_validate_einvoice)
        actions.addWidget(self.validate_btn)

        self.close_btn = AdaptiveButton("Cerrar", self, role="GhostAction")
        self.close_btn.clicked.connect(self.accept)
        actions.addWidget(self.close_btn)
        root.addLayout(actions)

        self.apply_theme(self._mode)

    def apply_theme(self, mode: str) -> None:
        self._mode = "light" if mode == "light" else "dark"
        p = get_palette(self._mode)
        self.status_tag.apply_theme(self._mode)
        self.create_btn.setIcon(load_svg_icon("invoice", color=p["primary_fg"], size=14))
        self.visual_btn.setIcon(load_svg_icon("create", color=p["icon_color"], size=14))
        self.validate_btn.setIcon(load_svg_icon("check", color=p["icon_color"], size=14))

    def _collect_invoice_params(self) -> dict[str, object]:
        return {
            "profile": self.profile_combo.currentData(),
            "invoice_number": self.invoice_num_edit.text().strip() or "FE-2026-0001",
            "emitter_name": self.emitter_name_edit.text().strip() or "TRODAI SAS",
            "emitter_nit": self.emitter_nit_edit.text().strip() or "900123456-7",
            "buyer_name": self.buyer_name_edit.text().strip() or "Cliente Local",
            "buyer_nit": self.buyer_nit_edit.text().strip() or "800987654-1",
            "currency": "COP" if self.profile_combo.currentData() == "dian_ubl21" else "EUR",
            "dpi": int(self.dpi_combo.currentData() or 150),
            "source_lang": str(self.lang_combo.currentData() or "auto"),
            "items": [
                {
                    "description": self.item_desc_edit.text().strip() or "Servicio",
                    "qty": int(self.qty_spin.value()),
                    "unit_price": float(self.price_spin.value()),
                    "iva_rate": float(self.iva_combo.currentData() or 0.19),
                }
            ],
        }

    def _run_create_einvoice(self) -> None:
        out_dir = QFileDialog.getExistingDirectory(self, "Carpeta destino factura electronica")
        if not out_dir:
            return
        res = ToolRegistry.get("create_einvoice").run([], Path(out_dir), self._collect_invoice_params())
        self.status_tag.set_tone("ok" if res.success else "risk", "GENERADA" if res.success else "ERROR")
        self.output_log.setPlainText(f"{res.summary}\nArchivos: {[str(p) for p in res.output_paths]}")

    def _run_visual_invoice(self) -> None:
        out_pdf, _ = QFileDialog.getSaveFileName(self, "Guardar factura visual PDF", "factura_visual.pdf", "PDF (*.pdf)")
        if not out_pdf:
            return
        res = ToolRegistry.get("create_visual_invoice").run([], Path(out_pdf), self._collect_invoice_params())
        self.status_tag.set_tone("ok" if res.success else "risk", "PDF LISTO" if res.success else "ERROR")
        self.output_log.setPlainText(res.summary)

    def _run_validate_einvoice(self) -> None:
        chosen, _ = QFileDialog.getOpenFileName(
            self,
            "Seleccionar XML o PDF/A-3 a validar",
            "",
            "E-Invoice (*.xml *.pdf);;Todos (*.*)",
        )
        if not chosen:
            return
        res = ToolRegistry.get("validate_einvoice").run([Path(chosen)], None, {"profile": "auto"})
        self.status_tag.set_tone("ok" if res.success else "risk", "VALIDA" if res.success else "INVALIDA")
        self.output_log.setPlainText(f"{res.summary}\nDetalles: {res.details}")
