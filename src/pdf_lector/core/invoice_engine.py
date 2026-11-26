"""Dual-profile e-invoice engine: Colombia DIAN UBL 2.1 and Factur-X / EN 16931 PDF/A-3."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from xml.sax.saxutils import escape as xml_escape
import xml.etree.ElementTree as ET

from .ocr_engine import normalize_source_lang, recognize_image
from .path_guard import is_unc_or_network_path, validate_local_media_path, validate_local_pdf_path
from .secrets_store import get_secret
from .temp_manager import TempManager
from .win32_bridge import atomic_write_text, is_cloud_only_file

_UBL_INVOICE_NS = "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
_UBL_CBC_NS = "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"
_UBL_CAC_NS = "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"

_CII_RSM_NS = "urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100"
_CII_RAM_NS = "urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100"
_CII_UDT_NS = "urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100"
_PDFAID_NS = "http://www.aiim.org/pdfa/ns/id/"

_XXE_FORBIDDEN_RE = re.compile(r"<!\s*(DOCTYPE|ENTITY)\b", re.IGNORECASE)
_CUFE_HEX_RE = re.compile(r"^[0-9a-fA-F]{96}$")


class InvoiceValidationError(ValueError):
    """Raised when invoice input payload or XML fails schema/business validation."""


@dataclass(frozen=True)
class InvoiceLineItem:
    description: str
    qty: float
    unit_price: float
    iva_rate: float = 0.0
    inc_rate: float = 0.0

    @property
    def line_extension(self) -> float:
        return round(self.qty * self.unit_price, 2)

    @property
    def iva_amount(self) -> float:
        return round(self.line_extension * self.iva_rate, 2)

    @property
    def inc_amount(self) -> float:
        return round(self.line_extension * self.inc_rate, 2)


@dataclass(frozen=True)
class InvoicePayload:
    profile: str  # "dian_ubl21" | "facturx_en16931"
    invoice_number: str
    issue_date: str
    issue_time: str
    currency: str
    emitter_name: str
    emitter_id: str
    customer_name: str
    customer_id: str
    items: list[InvoiceLineItem]
    technical_key: str = "fc8eac422eba16e22ffd8c6f94b3f40a6e38162c2"
    environment_code: str = "2"
    notes: str = ""
    subprofile: str = "facturx"

    @property
    def subtotal(self) -> float:
        return round(sum(it.line_extension for it in self.items), 2)

    @property
    def iva_total(self) -> float:
        return round(sum(it.iva_amount for it in self.items), 2)

    @property
    def inc_total(self) -> float:
        return round(sum(it.inc_amount for it in self.items), 2)

    @property
    def tax_total(self) -> float:
        return round(self.iva_total + self.inc_total, 2)

    @property
    def grand_total(self) -> float:
        return round(self.subtotal + self.tax_total, 2)


@dataclass(frozen=True)
class InvoiceValidationReport:
    valid: bool
    profile: str
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)


def normalize_invoice_profile(raw_profile: str | None) -> str:
    val = (raw_profile or "dian_ubl21").strip().lower()
    if val in ("dian", "dian_ubl21", "ubl21", "colombia_dian", "colombia"):
        return "dian_ubl21"
    if val in ("facturx", "facturx_en16931", "zugferd", "xrechnung", "en16931", "eu", "european"):
        return "facturx_en16931"
    raise ValueError(f"Unsupported invoice profile: {raw_profile!r}")


def _parse_items(raw_items: Sequence[dict[str, Any]] | None) -> list[InvoiceLineItem]:
    if not raw_items:
        raise InvoiceValidationError("At least one invoice line item is required")

    parsed: list[InvoiceLineItem] = []
    for idx, row in enumerate(raw_items):
        if not isinstance(row, dict):
            raise InvoiceValidationError(f"Item {idx + 1} must be an object")
        desc = str(row.get("description") or row.get("name") or "").strip()
        if not desc:
            raise InvoiceValidationError(f"Item {idx + 1} requires a non-empty description")

        try:
            qty = float(row.get("qty", row.get("quantity", 1)))
            unit_price = float(row.get("unit_price", row.get("price", 0.0)))
        except (TypeError, ValueError) as exc:
            raise InvoiceValidationError(f"Item {idx + 1} has invalid numeric qty/unit_price") from exc

        if qty <= 0:
            raise InvoiceValidationError(f"Item {idx + 1} qty must be > 0 (got {qty})")
        if unit_price < 0:
            raise InvoiceValidationError(f"Item {idx + 1} unit_price cannot be negative (got {unit_price})")

        raw_iva = row.get("iva_rate", row.get("vat_rate", row.get("tax_rate", 0.0)))
        raw_inc = row.get("inc_rate", 0.0)
        try:
            iva_rate = float(raw_iva if raw_iva is not None else 0.0)
            inc_rate = float(raw_inc if raw_inc is not None else 0.0)
        except (TypeError, ValueError) as exc:
            raise InvoiceValidationError(f"Item {idx + 1} has invalid tax rate") from exc

        if iva_rate < 0.0 or iva_rate > 1.0:
            raise InvoiceValidationError(f"Item {idx + 1} IVA/VAT rate must be in 0.0..1.0 (got {iva_rate})")
        if inc_rate < 0.0 or inc_rate > 1.0:
            raise InvoiceValidationError(f"Item {idx + 1} INC rate must be in 0.0..1.0 (got {inc_rate})")
        if (iva_rate + inc_rate) > 1.0:
            raise InvoiceValidationError(f"Item {idx + 1} combined tax rate exceeds 100%")

        parsed.append(
            InvoiceLineItem(
                description=desc,
                qty=qty,
                unit_price=unit_price,
                iva_rate=iva_rate,
                inc_rate=inc_rate,
            )
        )
    return parsed


def build_invoice_payload(params: dict[str, Any]) -> InvoicePayload:
    """Normalize and validate dictionary parameters into an InvoicePayload."""
    raw_prof = str(params.get("profile") or "dian_ubl21")
    profile = normalize_invoice_profile(raw_prof)
    subprofile = raw_prof.strip().lower()

    now_utc = datetime.now(timezone.utc)
    default_num = "SETP990000001" if profile == "dian_ubl21" else "FX-2026-001"
    inv_num = str(params.get("invoice_number") or default_num).strip()
    if not inv_num:
        raise InvoiceValidationError("invoice_number cannot be empty")

    issue_date = str(params.get("issue_date") or now_utc.strftime("%Y-%m-%d")).strip()
    issue_time = str(params.get("issue_time") or "12:00:00-05:00").strip()
    default_curr = "COP" if profile == "dian_ubl21" else "EUR"
    currency = str(params.get("currency") or default_curr).strip().upper()
    if len(currency) != 3 or not currency.isalpha():
        raise InvoiceValidationError(f"Invalid ISO-4217 currency code: {currency!r}")

    emitter_name = str(params.get("emitter_name") or params.get("emitter") or "").strip()
    emitter_id = str(
        params.get("emitter_nit") or params.get("nit") or params.get("emitter_vat") or ""
    ).strip()
    customer_name = str(params.get("customer_name") or params.get("customer") or "").strip()
    customer_id = str(
        params.get("customer_nit") or params.get("customer_vat") or params.get("customer_id") or ""
    ).strip()

    if not emitter_name:
        emitter_name = "TRODAI S.A.S." if profile == "dian_ubl21" else "TRODAI Europe GmbH"
    if profile == "dian_ubl21" and not emitter_id:
        raise InvoiceValidationError("Colombia DIAN profile requires emitter_nit / nit")
    if not emitter_id:
        emitter_id = "DE123456789"
    if not customer_name:
        customer_name = "Cliente"
    if not customer_id:
        customer_id = "222222222222" if profile == "dian_ubl21" else "FR000000000"

    items = _parse_items(params.get("items"))
    tech_key = str(params.get("technical_key") or "fc8eac422eba16e22ffd8c6f94b3f40a6e38162c2").strip()
    env_code = str(params.get("environment_code") or "2").strip()
    notes = str(params.get("notes") or "").strip()

    return InvoicePayload(
        profile=profile,
        invoice_number=inv_num,
        issue_date=issue_date,
        issue_time=issue_time,
        currency=currency,
        emitter_name=emitter_name,
        emitter_id=emitter_id,
        customer_name=customer_name,
        customer_id=customer_id,
        items=items,
        technical_key=tech_key,
        environment_code=env_code,
        notes=notes,
        subprofile=subprofile,
    )


def calculate_dian_cufe(payload: InvoicePayload) -> str:
    """Compute 96-hex-char SHA-384 CUFE according to DIAN technical specification."""
    nit_fe = re.sub(r"[^0-9A-Za-z]", "", payload.emitter_id.split("-")[0])
    num_adq = re.sub(r"[^0-9A-Za-z]", "", payload.customer_id.split("-")[0])
    raw = (
        f"{payload.invoice_number}"
        f"{payload.issue_date}"
        f"{payload.issue_time}"
        f"{payload.subtotal:.2f}"
        f"01{payload.iva_total:.2f}"
        f"04{payload.inc_total:.2f}"
        f"030.00"
        f"{payload.grand_total:.2f}"
        f"{nit_fe}"
        f"{num_adq}"
        f"{payload.technical_key}"
        f"{payload.environment_code}"
    )
    return hashlib.sha384(raw.encode("utf-8")).hexdigest()


def build_dian_ubl21_xml(payload: InvoicePayload) -> tuple[str, str]:
    """Generate DIAN UBL 2.1 XML string and its SHA-384 CUFE."""
    cufe = calculate_dian_cufe(payload)
    curr = xml_escape(payload.currency)

    tax_subtotals: list[str] = []
    if payload.iva_total > 0 or payload.inc_total == 0:
        pct = (payload.iva_total / payload.subtotal * 100.0) if payload.subtotal > 0 else 0.0
        tax_subtotals.append(
            "    <cac:TaxSubtotal>\n"
            f'      <cbc:TaxableAmount currencyID="{curr}">{payload.subtotal:.2f}</cbc:TaxableAmount>\n'
            f'      <cbc:TaxAmount currencyID="{curr}">{payload.iva_total:.2f}</cbc:TaxAmount>\n'
            f"      <cac:TaxCategory><cbc:Percent>{pct:.2f}</cbc:Percent>"
            "<cac:TaxScheme><cbc:ID>01</cbc:ID><cbc:Name>IVA</cbc:Name></cac:TaxScheme></cac:TaxCategory>\n"
            "    </cac:TaxSubtotal>\n"
        )
    if payload.inc_total > 0:
        pct_inc = (payload.inc_total / payload.subtotal * 100.0) if payload.subtotal > 0 else 0.0
        tax_subtotals.append(
            "    <cac:TaxSubtotal>\n"
            f'      <cbc:TaxableAmount currencyID="{curr}">{payload.subtotal:.2f}</cbc:TaxableAmount>\n'
            f'      <cbc:TaxAmount currencyID="{curr}">{payload.inc_total:.2f}</cbc:TaxAmount>\n'
            f"      <cac:TaxCategory><cbc:Percent>{pct_inc:.2f}</cbc:Percent>"
            "<cac:TaxScheme><cbc:ID>04</cbc:ID><cbc:Name>INC</cbc:Name></cac:TaxScheme></cac:TaxCategory>\n"
            "    </cac:TaxSubtotal>\n"
        )

    line_blocks: list[str] = []
    for idx, item in enumerate(payload.items, start=1):
        line_blocks.append(
            "  <cac:InvoiceLine>\n"
            f"    <cbc:ID>{idx}</cbc:ID>\n"
            f'    <cbc:InvoicedQuantity unitCode="EA">{item.qty:.2f}</cbc:InvoicedQuantity>\n'
            f'    <cbc:LineExtensionAmount currencyID="{curr}">{item.line_extension:.2f}</cbc:LineExtensionAmount>\n'
            f"    <cac:Item><cbc:Description>{xml_escape(item.description)}</cbc:Description></cac:Item>\n"
            f'    <cac:Price><cbc:PriceAmount currencyID="{curr}">{item.unit_price:.2f}</cbc:PriceAmount></cac:Price>\n'
            "  </cac:InvoiceLine>\n"
        )

    xml_str = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<Invoice xmlns="{_UBL_INVOICE_NS}" '
        f'xmlns:cbc="{_UBL_CBC_NS}" '
        f'xmlns:cac="{_UBL_CAC_NS}">\n'
        "  <cbc:UBLVersionID>UBL 2.1</cbc:UBLVersionID>\n"
        "  <cbc:ProfileID>DIAN 2.1: Factura Electronica de Venta</cbc:ProfileID>\n"
        f"  <cbc:ID>{xml_escape(payload.invoice_number)}</cbc:ID>\n"
        f'  <cbc:UUID schemeName="CUFE-SHA384">{cufe}</cbc:UUID>\n'
        f"  <cbc:IssueDate>{xml_escape(payload.issue_date)}</cbc:IssueDate>\n"
        f"  <cbc:IssueTime>{xml_escape(payload.issue_time)}</cbc:IssueTime>\n"
        "  <cbc:InvoiceTypeCode>01</cbc:InvoiceTypeCode>\n"
        f"  <cbc:DocumentCurrencyCode>{curr}</cbc:DocumentCurrencyCode>\n"
        "  <cac:AccountingSupplierParty><cac:Party><cac:PartyTaxScheme>\n"
        f"    <cbc:RegistrationName>{xml_escape(payload.emitter_name)}</cbc:RegistrationName>\n"
        f"    <cbc:CompanyID>{xml_escape(payload.emitter_id)}</cbc:CompanyID>\n"
        "  </cac:PartyTaxScheme></cac:Party></cac:AccountingSupplierParty>\n"
        "  <cac:AccountingCustomerParty><cac:Party><cac:PartyTaxScheme>\n"
        f"    <cbc:RegistrationName>{xml_escape(payload.customer_name)}</cbc:RegistrationName>\n"
        f"    <cbc:CompanyID>{xml_escape(payload.customer_id)}</cbc:CompanyID>\n"
        "  </cac:PartyTaxScheme></cac:Party></cac:AccountingCustomerParty>\n"
        "  <cac:TaxTotal>\n"
        f'    <cbc:TaxAmount currencyID="{curr}">{payload.tax_total:.2f}</cbc:TaxAmount>\n'
        + "".join(tax_subtotals)
        + "  </cac:TaxTotal>\n"
        "  <cac:LegalMonetaryTotal>\n"
        f'    <cbc:LineExtensionAmount currencyID="{curr}">{payload.subtotal:.2f}</cbc:LineExtensionAmount>\n'
        f'    <cbc:TaxExclusiveAmount currencyID="{curr}">{payload.subtotal:.2f}</cbc:TaxExclusiveAmount>\n'
        f'    <cbc:TaxInclusiveAmount currencyID="{curr}">{payload.grand_total:.2f}</cbc:TaxInclusiveAmount>\n'
        f'    <cbc:PayableAmount currencyID="{curr}">{payload.grand_total:.2f}</cbc:PayableAmount>\n'
        "  </cac:LegalMonetaryTotal>\n"
        + "".join(line_blocks)
        + "</Invoice>\n"
    )
    return xml_str, cufe


def build_facturx_cii_xml(payload: InvoicePayload) -> str:
    """Generate Factur-X / ZUGFeRD / XRechnung EN 16931 CII XML."""
    guideline = "urn:cen.eu:en16931:2017"
    if payload.subprofile == "xrechnung":
        guideline = "urn:cen.eu:en16931:2017#compliant#urn:xeinkauf.de:kosit:xrechnung_3.0"
    curr = xml_escape(payload.currency)

    line_blocks: list[str] = []
    for idx, item in enumerate(payload.items, start=1):
        pct = round((item.iva_rate + item.inc_rate) * 100.0, 2)
        line_blocks.append(
            "    <ram:IncludedSupplyChainTradeLineItem>\n"
            f"      <ram:AssociatedDocumentLineDocument><ram:LineID>{idx}</ram:LineID></ram:AssociatedDocumentLineDocument>\n"
            f"      <ram:SpecifiedTradeProduct><ram:Name>{xml_escape(item.description)}</ram:Name></ram:SpecifiedTradeProduct>\n"
            "      <ram:SpecifiedLineTradeAgreement>\n"
            f"        <ram:NetPriceProductTradePrice><ram:ChargeAmount>{item.unit_price:.2f}</ram:ChargeAmount></ram:NetPriceProductTradePrice>\n"
            "      </ram:SpecifiedLineTradeAgreement>\n"
            "      <ram:SpecifiedLineTradeDelivery>\n"
            f'        <ram:BilledQuantity unitCode="C62">{item.qty:.2f}</ram:BilledQuantity>\n'
            "      </ram:SpecifiedLineTradeDelivery>\n"
            "      <ram:SpecifiedLineTradeSettlement>\n"
            "        <ram:ApplicableTradeTax>\n"
            "          <ram:TypeCode>VAT</ram:TypeCode>\n"
            f"          <ram:CategoryCode>{'S' if pct > 0 else 'Z'}</ram:CategoryCode>\n"
            f"          <ram:RateApplicablePercent>{pct:.2f}</ram:RateApplicablePercent>\n"
            "        </ram:ApplicableTradeTax>\n"
            "        <ram:SpecifiedTradeSettlementLineMonetarySummation>\n"
            f"          <ram:LineTotalAmount>{item.line_extension:.2f}</ram:LineTotalAmount>\n"
            "        </ram:SpecifiedTradeSettlementLineMonetarySummation>\n"
            "      </ram:SpecifiedLineTradeSettlement>\n"
            "    </ram:IncludedSupplyChainTradeLineItem>\n"
        )

    date_compact = payload.issue_date.replace("-", "")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rsm:CrossIndustryInvoice '
        f'xmlns:rsm="{_CII_RSM_NS}" '
        f'xmlns:ram="{_CII_RAM_NS}" '
        f'xmlns:udt="{_CII_UDT_NS}">\n'
        "  <rsm:ExchangedDocumentContext>\n"
        "    <ram:GuidelineSpecifiedDocumentContextParameter>\n"
        f"      <ram:ID>{guideline}</ram:ID>\n"
        "    </ram:GuidelineSpecifiedDocumentContextParameter>\n"
        "  </rsm:ExchangedDocumentContext>\n"
        "  <rsm:ExchangedDocument>\n"
        f"    <ram:ID>{xml_escape(payload.invoice_number)}</ram:ID>\n"
        "    <ram:TypeCode>380</ram:TypeCode>\n"
        f'    <ram:IssueDateTime><udt:DateTimeString format="102">{xml_escape(date_compact)}</udt:DateTimeString></ram:IssueDateTime>\n'
        "  </rsm:ExchangedDocument>\n"
        "  <rsm:SupplyChainTradeTransaction>\n"
        + "".join(line_blocks)
        + "    <ram:ApplicableHeaderTradeAgreement>\n"
        "      <ram:SellerTradeParty>\n"
        f"        <ram:Name>{xml_escape(payload.emitter_name)}</ram:Name>\n"
        f'        <ram:SpecifiedTaxRegistration><ram:ID schemeID="VA">{xml_escape(payload.emitter_id)}</ram:ID></ram:SpecifiedTaxRegistration>\n'
        "      </ram:SellerTradeParty>\n"
        "      <ram:BuyerTradeParty>\n"
        f"        <ram:Name>{xml_escape(payload.customer_name)}</ram:Name>\n"
        f'        <ram:SpecifiedTaxRegistration><ram:ID schemeID="VA">{xml_escape(payload.customer_id)}</ram:ID></ram:SpecifiedTaxRegistration>\n'
        "      </ram:BuyerTradeParty>\n"
        "    </ram:ApplicableHeaderTradeAgreement>\n"
        "    <ram:ApplicableHeaderTradeDelivery />\n"
        "    <ram:ApplicableHeaderTradeSettlement>\n"
        f"      <ram:InvoiceCurrencyCode>{curr}</ram:InvoiceCurrencyCode>\n"
        "      <ram:SpecifiedTradeSettlementHeaderMonetarySummation>\n"
        f"        <ram:LineTotalAmount>{payload.subtotal:.2f}</ram:LineTotalAmount>\n"
        f"        <ram:TaxBasisTotalAmount>{payload.subtotal:.2f}</ram:TaxBasisTotalAmount>\n"
        f'        <ram:TaxTotalAmount currencyID="{curr}">{payload.tax_total:.2f}</ram:TaxTotalAmount>\n'
        f"        <ram:GrandTotalAmount>{payload.grand_total:.2f}</ram:GrandTotalAmount>\n"
        f"        <ram:DuePayableAmount>{payload.grand_total:.2f}</ram:DuePayableAmount>\n"
        "      </ram:SpecifiedTradeSettlementHeaderMonetarySummation>\n"
        "    </ram:ApplicableHeaderTradeSettlement>\n"
        "  </rsm:SupplyChainTradeTransaction>\n"
        "</rsm:CrossIndustryInvoice>\n"
    )


def _render_qr_image(qr_text: str) -> Any:
    import qrcode

    qr = qrcode.QRCode(border=1, box_size=6)
    qr.add_data(qr_text)
    qr.make(fit=True)
    return qr.make_image(fill_color="black", back_color="white").convert("RGB")


def create_visual_invoice_pdf(
    payload: InvoicePayload,
    output_pdf: Path,
    *,
    cufe: str = "",
    logo_path: Path | str | None = None,
    source_lang: str = "auto",
) -> Path:
    """Render a professional visual invoice PDF with QR code and itemized table."""
    if is_unc_or_network_path(output_pdf):
        raise ValueError("network_or_url_blocked")
    normalize_source_lang(source_lang)

    safe_logo: Path | None = None
    if logo_path is not None and str(logo_path).strip():
        ok_logo, reason_logo = validate_local_media_path(logo_path, check_exists=True)
        if not ok_logo:
            raise ValueError(f"Invalid logo path ({reason_logo})")
        safe_logo = Path(str(logo_path).strip()).resolve()

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    out_path = Path(output_pdf)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    temp_mgr = TempManager(register_atexit=False)

    is_en = source_lang.strip().lower() == "en"
    title_lbl = (
        ("ELECTRONIC SALES INVOICE - DIAN UBL 2.1" if is_en else "FACTURA ELECTRONICA DE VENTA - DIAN UBL 2.1")
        if payload.profile == "dian_ubl21"
        else "INVOICE - FACTUR-X EN 16931"
    )

    if payload.profile == "dian_ubl21":
        qr_data = (
            f"NumFac={payload.invoice_number} "
            f"FecFac={payload.issue_date} "
            f"NitFac={payload.emitter_id} "
            f"DocAdq={payload.customer_id} "
            f"ValFac={payload.subtotal:.2f} "
            f"ValIva={payload.iva_total:.2f} "
            f"ValOtroIm={payload.inc_total:.2f} "
            f"ValTolFac={payload.grand_total:.2f} "
            f"CUFE={cufe}"
        )
    else:
        qr_data = f"FACTUR-X-EN16931|{payload.invoice_number}|{payload.emitter_id}|{payload.grand_total:.2f}{payload.currency}"

    try:
        with temp_mgr.task_scope(secure=False) as tid:
            fd, raw_stage = tempfile.mkstemp(
                prefix=f".{out_path.stem}.{os.getpid()}.",
                suffix=".inv.tmp.pdf",
                dir=str(out_path.parent),
            )
            os.close(fd)
            stage = Path(raw_stage)
            temp_mgr.register_path(stage, task_id=tid, secure=False)

            w, h = LETTER
            c = canvas.Canvas(str(stage), pagesize=LETTER)
            c.setTitle(f"{payload.invoice_number} - {payload.emitter_name}")
            c.setAuthor("PDF/MD Lector by TRODAI")

            # Header bar
            c.setFillColor(colors.HexColor("#0A0F1D"))
            c.rect(0, h - 86, w, 86, stroke=0, fill=1)
            c.setFillColor(colors.HexColor("#F8FAFC"))
            c.setFont("Helvetica-Bold", 14)
            c.drawString(48, h - 42, title_lbl)
            c.setFont("Helvetica", 10)
            c.drawString(48, h - 62, f"No. {payload.invoice_number}   |   {payload.issue_date}   |   {payload.currency}")

            if safe_logo is not None:
                c.drawImage(ImageReader(str(safe_logo)), w - 110, h - 74, width=62, height=62, preserveAspectRatio=True)

            # Parties
            y = h - 122
            c.setFillColor(colors.HexColor("#0F172A"))
            c.setFont("Helvetica-Bold", 11)
            c.drawString(48, y, f"{'Emitter' if is_en else 'Emisor'}: {payload.emitter_name}")
            c.setFont("Helvetica", 10)
            c.drawString(48, y - 16, f"{'ID/NIT/VAT' if is_en else 'NIT / ID'}: {payload.emitter_id}")
            c.setFont("Helvetica-Bold", 11)
            c.drawString(48, y - 42, f"{'Customer' if is_en else 'Adquirente'}: {payload.customer_name}")
            c.setFont("Helvetica", 10)
            c.drawString(48, y - 58, f"{'ID/NIT/VAT' if is_en else 'NIT / ID'}: {payload.customer_id}")

            # QR Code
            qr_img = _render_qr_image(qr_data)
            c.drawImage(ImageReader(qr_img), w - 156, y - 74, width=108, height=108)

            # Table header
            y_tbl = y - 106
            c.setFillColor(colors.HexColor("#1E293B"))
            c.rect(48, y_tbl, w - 96, 22, stroke=0, fill=1)
            c.setFillColor(colors.white)
            c.setFont("Helvetica-Bold", 9)
            c.drawString(56, y_tbl + 7, "#" if is_en else "Item")
            c.drawString(88, y_tbl + 7, "Description" if is_en else "Descripcion")
            c.drawRightString(360, y_tbl + 7, "Qty" if is_en else "Cant.")
            c.drawRightString(440, y_tbl + 7, "Unit" if is_en else "V. Unit")
            c.drawRightString(500, y_tbl + 7, "Tax" if is_en else "Imp.")
            c.drawRightString(w - 56, y_tbl + 7, "Total")

            row_y = y_tbl - 20
            c.setFillColor(colors.HexColor("#0F172A"))
            c.setFont("Helvetica", 9)
            for idx, item in enumerate(payload.items, start=1):
                if row_y < 140:
                    c.showPage()
                    row_y = h - 72
                    c.setFont("Helvetica", 9)
                tax_amt = item.iva_amount + item.inc_amount
                line_tot = item.line_extension + tax_amt
                c.drawString(56, row_y, str(idx))
                c.drawString(88, row_y, item.description[:44])
                c.drawRightString(360, row_y, f"{item.qty:g}")
                c.drawRightString(440, row_y, f"{item.unit_price:,.2f}")
                c.drawRightString(500, row_y, f"{tax_amt:,.2f}")
                c.drawRightString(w - 56, row_y, f"{line_tot:,.2f}")
                c.setStrokeColor(colors.HexColor("#E2E8F0"))
                c.line(48, row_y - 5, w - 48, row_y - 5)
                row_y -= 20

            # Totals box
            tot_y = max(90, row_y - 24)
            c.setFont("Helvetica", 10)
            c.drawRightString(480, tot_y + 36, "Subtotal:")
            c.drawRightString(w - 56, tot_y + 36, f"{payload.subtotal:,.2f} {payload.currency}")
            c.drawRightString(480, tot_y + 20, "IVA / VAT:")
            c.drawRightString(w - 56, tot_y + 20, f"{payload.iva_total:,.2f} {payload.currency}")
            if payload.inc_total > 0:
                c.drawRightString(480, tot_y + 4, "INC:")
                c.drawRightString(w - 56, tot_y + 4, f"{payload.inc_total:,.2f} {payload.currency}")
            c.setFont("Helvetica-Bold", 11)
            c.drawRightString(480, tot_y - 16, "TOTAL:")
            c.drawRightString(w - 56, tot_y - 16, f"{payload.grand_total:,.2f} {payload.currency}")

            if cufe:
                c.setFont("Helvetica", 7)
                c.setFillColor(colors.HexColor("#475569"))
                c.drawString(48, 38, f"CUFE: {cufe}")

            c.showPage()
            c.save()
            os.replace(stage, out_path)
    finally:
        temp_mgr.cleanup_all()

    return out_path


def _embed_facturx_pdfa3(visual_pdf: Path, cii_xml_bytes: bytes, output_pdf: Path) -> Path:
    """Upgrade a visual PDF to PDF/A-3B with attached factur-x.xml via pikepdf."""
    import pikepdf

    pdfa_xmp = (
        b'<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>'
        b'<x:xmpmeta xmlns:x="adobe:ns:meta/">'
        b'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        b'<rdf:Description xmlns:pdfaid="http://www.aiim.org/pdfa/ns/id/" '
        b'xmlns:fx="urn:factur-x:pdfa:CrossIndustryDocument:invoice:1p0#">'
        b"<pdfaid:part>3</pdfaid:part>"
        b"<pdfaid:conformance>B</pdfaid:conformance>"
        b"<fx:DocumentType>INVOICE</fx:DocumentType>"
        b"<fx:DocumentFileName>factur-x.xml</fx:DocumentFileName>"
        b"<fx:Version>1.0</fx:Version>"
        b"<fx:ConformanceLevel>EN 16931</fx:ConformanceLevel>"
        b"</rdf:Description></rdf:RDF></x:xmpmeta>"
        b'<?xpacket end="w"?>'
    )

    out_path = Path(output_pdf)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    temp_mgr = TempManager(register_atexit=False)

    try:
        with temp_mgr.task_scope(secure=False) as tid:
            fd, raw_stage = tempfile.mkstemp(
                prefix=f".{out_path.stem}.{os.getpid()}.",
                suffix=".fx.tmp.pdf",
                dir=str(out_path.parent),
            )
            os.close(fd)
            stage = Path(raw_stage)
            temp_mgr.register_path(stage, task_id=tid, secure=False)

            with pikepdf.open(visual_pdf) as pdf:
                meta_stream = pikepdf.Stream(pdf, pdfa_xmp)
                meta_stream[pikepdf.Name.Type] = pikepdf.Name.Metadata
                meta_stream[pikepdf.Name.Subtype] = pikepdf.Name.XML
                pdf.Root[pikepdf.Name.Metadata] = pdf.make_indirect(meta_stream)

                filespec = pikepdf.AttachedFileSpec(
                    pdf,
                    cii_xml_bytes,
                    filename="factur-x.xml",
                    mime_type="text/xml",
                    relationship=pikepdf.Name.Data,
                    description="Factur-X EN 16931 XML invoice",
                )
                pdf.attachments["factur-x.xml"] = filespec
                pdf.Root[pikepdf.Name.AF] = pikepdf.Array([filespec.obj])
                pdf.save(stage)

            os.replace(stage, out_path)
    finally:
        temp_mgr.cleanup_all()

    return out_path


def create_einvoice(
    params: dict[str, Any],
    output: Path,
) -> tuple[list[Path], dict[str, Any]]:
    """Create DIAN UBL 2.1 XML+PDF or Factur-X EN 16931 PDF/A-3 e-invoice."""
    if is_unc_or_network_path(output):
        raise ValueError("network_or_url_blocked")

    payload = build_invoice_payload(params)
    source_lang = normalize_source_lang(str(params.get("source_lang") or "auto"))
    out = Path(output)

    if payload.profile == "dian_ubl21":
        xml_str, cufe = build_dian_ubl21_xml(payload)
        safe_stem = re.sub(r"[^0-9A-Za-z._-]", "_", payload.invoice_number) or "dian_invoice"
        if out.suffix.lower() == ".xml":
            xml_path = out
            pdf_path = out.with_suffix(".pdf")
        elif out.suffix.lower() == ".pdf":
            pdf_path = out
            xml_path = out.with_suffix(".xml")
        else:
            out.mkdir(parents=True, exist_ok=True)
            xml_path = out / f"{safe_stem}.xml"
            pdf_path = out / f"{safe_stem}.pdf"

        atomic_write_text(xml_path, xml_str)
        create_visual_invoice_pdf(
            payload,
            pdf_path,
            cufe=cufe,
            logo_path=params.get("logo_path"),
            source_lang=source_lang,
        )
        return [xml_path, pdf_path], {
            "profile": "dian_ubl21",
            "invoice_number": payload.invoice_number,
            "cufe": cufe,
            "subtotal": payload.subtotal,
            "iva_total": payload.iva_total,
            "inc_total": payload.inc_total,
            "total": payload.grand_total,
            "currency": payload.currency,
            "source_lang": source_lang,
        }

    # Factur-X / ZUGFeRD / XRechnung EN 16931 PDF/A-3
    cii_xml = build_facturx_cii_xml(payload)
    cii_bytes = cii_xml.encode("utf-8")
    safe_stem = re.sub(r"[^0-9A-Za-z._-]", "_", payload.invoice_number) or "facturx_invoice"

    if out.suffix.lower() == ".pdf":
        pdf_path = out
        xml_path = None
    elif out.suffix.lower() == ".xml":
        xml_path = out
        pdf_path = out.with_suffix(".pdf")
    else:
        out.mkdir(parents=True, exist_ok=True)
        pdf_path = out / f"{safe_stem}.pdf"
        xml_path = out / "factur-x.xml"

    temp_mgr = TempManager(register_atexit=False)
    try:
        with temp_mgr.task_scope(secure=False) as tid:
            tmp_dir = temp_mgr.create_temp_dir(task_id=tid, secure=False)
            raw_visual = tmp_dir / "visual.pdf"
            create_visual_invoice_pdf(
                payload,
                raw_visual,
                logo_path=params.get("logo_path"),
                source_lang=source_lang,
            )
            _embed_facturx_pdfa3(raw_visual, cii_bytes, pdf_path)
    finally:
        temp_mgr.cleanup_all()

    outputs = [pdf_path]
    if xml_path is not None:
        atomic_write_text(xml_path, cii_xml)
        outputs.append(xml_path)

    return outputs, {
        "profile": "facturx_en16931",
        "subprofile": payload.subprofile,
        "invoice_number": payload.invoice_number,
        "subtotal": payload.subtotal,
        "tax_total": payload.tax_total,
        "total": payload.grand_total,
        "currency": payload.currency,
        "embedded_xml": "factur-x.xml",
        "source_lang": source_lang,
    }


def _local_tag(elem: ET.Element) -> str:
    return elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag


def _find_first_text(root: ET.Element, tag_name: str) -> str:
    for elem in root.iter():
        if _local_tag(elem) == tag_name and elem.text is not None:
            val = elem.text.strip()
            if val:
                return val
    return ""


def _parse_float_safe(raw: str) -> float | None:
    if not raw:
        return None
    try:
        return float(raw.replace(",", "").strip())
    except ValueError:
        return None


def _validate_xml_string(xml_text: str, profile_hint: str = "auto") -> InvoiceValidationReport:
    if _XXE_FORBIDDEN_RE.search(xml_text):
        return InvoiceValidationReport(
            valid=False,
            profile="unknown",
            errors=["xxe_or_dtd_forbidden"],
        )

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        return InvoiceValidationReport(
            valid=False,
            profile="unknown",
            errors=[f"malformed_xml: {exc}"],
        )

    root_tag = _local_tag(root)
    errors: list[str] = []
    warnings: list[str] = []

    if root_tag == "Invoice" or (profile_hint == "dian_ubl21" and "CrossIndustryInvoice" not in root_tag):
        profile = "dian_ubl21"
        if root_tag != "Invoice":
            errors.append(f"unexpected_root_tag:{root_tag}")
        if _UBL_INVOICE_NS not in root.tag:
            errors.append("missing_ubl_invoice_namespace")
        ubl_ver = _find_first_text(root, "UBLVersionID")
        if ubl_ver != "UBL 2.1":
            errors.append("invalid_or_missing_ubl_version")
        inv_id = _find_first_text(root, "ID")
        if not inv_id:
            errors.append("missing_invoice_id")
        cufe = _find_first_text(root, "UUID")
        if not _CUFE_HEX_RE.match(cufe):
            errors.append("invalid_cufe_sha384")

        # Supplier NIT
        supplier_nit = ""
        supplier_name = ""
        for elem in root.iter():
            if _local_tag(elem) == "AccountingSupplierParty":
                supplier_nit = _find_first_text(elem, "CompanyID")
                supplier_name = _find_first_text(elem, "RegistrationName")
                break
        if not supplier_name:
            errors.append("missing_supplier_name")
        if not supplier_nit:
            errors.append("missing_supplier_nit")

        # Monetary consistency
        tax_excl = None
        tax_incl = None
        payable = None
        tax_amt = None
        for elem in root.iter():
            tname = _local_tag(elem)
            if tname == "LegalMonetaryTotal":
                tax_excl = _parse_float_safe(_find_first_text(elem, "TaxExclusiveAmount"))
                tax_incl = _parse_float_safe(_find_first_text(elem, "TaxInclusiveAmount"))
                payable = _parse_float_safe(_find_first_text(elem, "PayableAmount"))
            elif tname == "TaxTotal" and tax_amt is None:
                for child in elem:
                    if _local_tag(child) == "TaxAmount" and child.text:
                        tax_amt = _parse_float_safe(child.text)
                        break

        if payable is None or payable < 0:
            errors.append("invalid_payable_amount")
        elif tax_excl is not None and tax_amt is not None:
            if abs((tax_excl + tax_amt) - payable) > 0.05:
                errors.append("monetary_total_mismatch")
        if tax_incl is not None and payable is not None and abs(tax_incl - payable) > 0.05:
            errors.append("tax_inclusive_payable_mismatch")

        return InvoiceValidationReport(
            valid=len(errors) == 0,
            profile=profile,
            errors=errors,
            warnings=warnings,
            summary={
                "invoice_id": inv_id,
                "cufe": cufe,
                "supplier_nit": supplier_nit,
                "payable": payable,
            },
        )

    if root_tag == "CrossIndustryInvoice" or profile_hint == "facturx_en16931":
        profile = "facturx_en16931"
        if root_tag != "CrossIndustryInvoice":
            errors.append(f"unexpected_root_tag:{root_tag}")
        guideline = ""
        for elem in root.iter():
            if _local_tag(elem) == "GuidelineSpecifiedDocumentContextParameter":
                guideline = _find_first_text(elem, "ID")
                break
        if "en16931" not in guideline.lower():
            errors.append("missing_en16931_guideline")

        doc_id = ""
        for elem in root.iter():
            if _local_tag(elem) == "ExchangedDocument":
                doc_id = _find_first_text(elem, "ID")
                break
        if not doc_id:
            errors.append("missing_document_id")

        seller_name = ""
        buyer_name = ""
        for elem in root.iter():
            tname = _local_tag(elem)
            if tname == "SellerTradeParty":
                seller_name = _find_first_text(elem, "Name")
            elif tname == "BuyerTradeParty":
                buyer_name = _find_first_text(elem, "Name")
        if not seller_name:
            errors.append("missing_seller_name")
        if not buyer_name:
            errors.append("missing_buyer_name")

        tax_basis = None
        tax_tot = None
        grand_tot = None
        due_payable = None
        for elem in root.iter():
            if _local_tag(elem) == "SpecifiedTradeSettlementHeaderMonetarySummation":
                tax_basis = _parse_float_safe(_find_first_text(elem, "TaxBasisTotalAmount"))
                tax_tot = _parse_float_safe(_find_first_text(elem, "TaxTotalAmount"))
                grand_tot = _parse_float_safe(_find_first_text(elem, "GrandTotalAmount"))
                due_payable = _parse_float_safe(_find_first_text(elem, "DuePayableAmount"))
                break

        if grand_tot is None or grand_tot < 0:
            errors.append("invalid_grand_total")
        elif tax_basis is not None and tax_tot is not None:
            if abs((tax_basis + tax_tot) - grand_tot) > 0.05:
                errors.append("monetary_total_mismatch")
        if due_payable is not None and grand_tot is not None and abs(due_payable - grand_tot) > 0.05:
            warnings.append("due_payable_differs_from_grand_total")

        return InvoiceValidationReport(
            valid=len(errors) == 0,
            profile=profile,
            errors=errors,
            warnings=warnings,
            summary={
                "invoice_id": doc_id,
                "guideline": guideline,
                "seller": seller_name,
                "grand_total": grand_tot,
            },
        )

    return InvoiceValidationReport(
        valid=False,
        profile="unknown",
        errors=[f"unrecognized_invoice_root:{root_tag}"],
    )


def validate_einvoice_file(input_path: Path, *, profile: str = "auto") -> InvoiceValidationReport:
    """Validate a DIAN UBL 2.1 XML or Factur-X CII XML / PDF/A-3 invoice."""
    if is_unc_or_network_path(input_path):
        raise ValueError("network_or_url_blocked")
    p = Path(input_path).resolve(strict=True)
    if is_cloud_only_file(p):
        raise ValueError("cloud_only_blocked")

    prof_hint = "auto" if (profile or "auto").strip().lower() == "auto" else normalize_invoice_profile(profile)
    ext = p.suffix.lower()

    if ext == ".xml":
        raw_text = p.read_text(encoding="utf-8", errors="replace")
        return _validate_xml_string(raw_text, profile_hint=prof_hint)

    if ext == ".pdf":
        ok_pdf, reason = validate_local_pdf_path(p, check_exists=True)
        if not ok_pdf:
            return InvoiceValidationReport(valid=False, profile="facturx_en16931", errors=[reason])

        import pikepdf

        try:
            with pikepdf.open(p) as pdf:
                # Check PDF/A-3 XMP
                has_pdfa3 = False
                meta = pdf.Root.get("/Metadata")
                if isinstance(meta, pikepdf.Stream):
                    xmp_str = bytes(meta.read_bytes()).decode("utf-8", errors="replace")
                    if not _XXE_FORBIDDEN_RE.search(xmp_str):
                        try:
                            xroot = ET.fromstring(xmp_str)
                            part = ""
                            for el in xroot.iter():
                                if el.tag == f"{{{_PDFAID_NS}}}part" and el.text:
                                    part = el.text.strip()
                                elif f"{{{_PDFAID_NS}}}part" in el.attrib:
                                    part = el.attrib[f"{{{_PDFAID_NS}}}part"].strip()
                            has_pdfa3 = part == "3"
                        except ET.ParseError:
                            has_pdfa3 = False

                xml_bytes: bytes | None = None
                att_name = ""
                for candidate_name in ("factur-x.xml", "zugferd-invoice.xml", "xrechnung.xml"):
                    if candidate_name in pdf.attachments:
                        att_name = candidate_name
                        xml_bytes = bytes(pdf.attachments[candidate_name].get_file().read_bytes())
                        break
                if xml_bytes is None:
                    for name, spec in pdf.attachments.items():
                        if str(name).lower().endswith(".xml"):
                            att_name = str(name)
                            xml_bytes = bytes(spec.get_file().read_bytes())
                            break

                if xml_bytes is None:
                    return InvoiceValidationReport(
                        valid=False,
                        profile="facturx_en16931",
                        errors=["missing_embedded_einvoice_xml"],
                    )

                report = _validate_xml_string(
                    xml_bytes.decode("utf-8", errors="replace"),
                    profile_hint="facturx_en16931" if prof_hint == "auto" else prof_hint,
                )
                errs = list(report.errors)
                warns = list(report.warnings)
                if not has_pdfa3:
                    warns.append("missing_pdfa3_xmp_marker")
                summary = dict(report.summary)
                summary["attachment_name"] = att_name
                summary["has_pdfa3_marker"] = has_pdfa3
                return InvoiceValidationReport(
                    valid=len(errs) == 0,
                    profile=report.profile,
                    errors=errs,
                    warnings=warns,
                    summary=summary,
                )
        except (pikepdf.PdfError, OSError) as exc:
            return InvoiceValidationReport(
                valid=False,
                profile="facturx_en16931",
                errors=[f"invalid_pdf:{exc}"],
            )

    return InvoiceValidationReport(
        valid=False,
        profile="unknown",
        errors=[f"unsupported_file_extension:{ext}"],
    )


def _extract_pdf_text_or_ocr(
    pdf_path: Path,
    *,
    dpi: int = 200,
    source_lang: str = "auto",
    ocr_provider: Callable[[Any, str], Any] | None = None,
) -> tuple[str, bool]:
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(str(pdf_path))
    chunks: list[str] = []
    used_ocr = False
    try:
        for idx in range(len(doc)):
            page = doc[idx]
            tp = None
            try:
                tp = page.get_textpage()
                txt = (tp.get_text_range() or "").strip()
            finally:
                if tp is not None:
                    tp.close()
                page.close()
            if txt:
                chunks.append(txt)

        joined = "\n".join(chunks).strip()
        if len(joined) >= 20:
            return joined, False

        # Fallback to local OCR when PDF lacks a text layer
        used_ocr = True
        scale = int(dpi) / 72.0
        ocr_chunks: list[str] = []
        for idx in range(len(doc)):
            page = doc[idx]
            try:
                bmp = page.render(scale=scale)
                pil_img = bmp.to_pil()
            finally:
                page.close()
            try:
                rec_txt, _lines, _backend = recognize_image(
                    pil_img,
                    source_lang=source_lang,
                    ocr_provider=ocr_provider,
                )
                if rec_txt:
                    ocr_chunks.append(rec_txt)
            except Exception:
                pass
        return "\n".join(ocr_chunks).strip(), used_ocr
    finally:
        doc.close()


def _parse_amount_match(pattern: str, text: str) -> float | None:
    m = re.search(pattern, text, re.IGNORECASE)
    if not m:
        return None
    raw = m.group(1).replace(" ", "").replace(",", "")
    try:
        return float(raw)
    except ValueError:
        return None


def extract_invoice_fields_from_text(text: str, *, profile: str = "dian_ubl21") -> dict[str, Any]:
    """Heuristically extract invoice metadata, NIT/VAT, and totals from PDF/OCR text."""
    norm_prof = normalize_invoice_profile(profile)
    nit_match = re.search(r"(?:NIT|VAT|ID)\s*[:#]?\s*([A-Z]{0,2}[0-9][0-9.\-]{5,15})", text, re.IGNORECASE)
    inv_match = re.search(
        r"(?:NumFac|Factura|Invoice|No\.?|#)\s*[:#]?\s*([A-Z]{1,6}[-]?[0-9]{3,12})",
        text,
        re.IGNORECASE,
    )
    date_match = re.search(r"(20[0-9]{2}-[0-9]{2}-[0-9]{2})", text)
    emitter_match = re.search(r"(?:Emisor|Seller)\s*[:]\s*([^\r\n|]+)", text, re.IGNORECASE)
    customer_match = re.search(r"(?:Adquirente|Cliente|Buyer)\s*[:]\s*([^\r\n|]+)", text, re.IGNORECASE)

    subtotal = _parse_amount_match(r"(?:Subtotal|Net|Base)\s*[:=]?\s*([0-9][0-9.,]*)", text)
    tax = _parse_amount_match(r"(?:IVA|VAT)(?:\s*\([^)]*\))?\s*[:=]?\s*([0-9][0-9.,]*)", text)
    total = _parse_amount_match(r"(?:Total|ValTolFac|Grand\s*Total)\s*[:=]?\s*([0-9][0-9.,]*)", text)

    if subtotal is None and total is not None and tax is not None:
        subtotal = max(0.0, round(total - tax, 2))
    if subtotal is None:
        subtotal = total if total is not None else 100.0
    if tax is None:
        tax = round(subtotal * 0.19, 2) if total is None else max(0.0, round(total - subtotal, 2))
    rate = round(tax / subtotal, 4) if subtotal > 0 else 0.0
    if rate < 0.0 or rate > 1.0:
        rate = 0.0

    currency = "COP" if norm_prof == "dian_ubl21" else "EUR"
    if "EUR" in text.upper():
        currency = "EUR"
    elif "USD" in text.upper():
        currency = "USD"
    elif "COP" in text.upper():
        currency = "COP"

    emitter_id = nit_match.group(1) if nit_match else ("900123456" if norm_prof == "dian_ubl21" else "DE123456789")
    return {
        "profile": norm_prof,
        "invoice_number": inv_match.group(1) if inv_match else ("SETP990000001" if norm_prof == "dian_ubl21" else "FX-2026-001"),
        "issue_date": date_match.group(1) if date_match else datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "currency": currency,
        "emitter_name": emitter_match.group(1).strip() if emitter_match else "Extracted Emitter",
        "emitter_nit": emitter_id,
        "emitter_vat": emitter_id,
        "customer_name": customer_match.group(1).strip() if customer_match else "Extracted Customer",
        "customer_nit": "222222222222",
        "items": [
            {
                "description": "Extracted Invoice Item",
                "qty": 1,
                "unit_price": round(subtotal, 2),
                "iva_rate": rate,
            }
        ],
    }


def _validate_online_endpoint(url: str) -> str:
    raw = str(url or "").strip()
    if not raw:
        raise ValueError("empty_endpoint_url")
    parsed = urlparse(raw)
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()
    if scheme == "https":
        return raw
    if scheme == "http" and host in ("127.0.0.1", "localhost", "::1"):
        return raw
    raise ValueError("insecure_endpoint_blocked")


def pdf_to_einvoice(
    pdf_path: Path,
    output: Path,
    *,
    mode: str = "local",
    profile: str = "dian_ubl21",
    dpi: int = 200,
    source_lang: str = "auto",
    consent_granted: bool = False,
    endpoint_url: str = "",
    ocr_provider: Callable[[Any, str], Any] | None = None,
    http_post_fn: Callable[[str, bytes, dict[str, str]], tuple[int, bytes]] | None = None,
) -> tuple[list[Path], dict[str, Any]]:
    """Extract invoice fields from PDF locally (rules/OCR) or via consented online endpoint."""
    ok_pdf, reason = validate_local_pdf_path(pdf_path, check_exists=True)
    if not ok_pdf:
        raise ValueError(f"Invalid input PDF ({reason})")
    if is_unc_or_network_path(output):
        raise ValueError("network_or_url_blocked")

    norm_mode = (mode or "local").strip().lower()
    if norm_mode not in ("local", "online"):
        raise ValueError(f"Unsupported extraction mode: {mode!r}")
    norm_lang = normalize_source_lang(source_lang)
    norm_prof = normalize_invoice_profile(profile)

    if norm_mode == "online" and not consent_granted:
        raise PermissionError("Online e-invoice extraction requires explicit consent_granted=True")

    src_path = Path(pdf_path).resolve()
    text, used_ocr = _extract_pdf_text_or_ocr(
        src_path,
        dpi=dpi,
        source_lang=norm_lang,
        ocr_provider=ocr_provider,
    )
    fields = extract_invoice_fields_from_text(text, profile=norm_prof)
    fields["source_lang"] = norm_lang

    remote_response: dict[str, Any] = {}
    if norm_mode == "online":
        safe_url = _validate_online_endpoint(endpoint_url)
        req_payload = json.dumps(
            {
                "profile": norm_prof,
                "source_lang": norm_lang,
                "dpi": int(dpi),
                "extracted_fields": fields,
                "text_excerpt": text[:4000],
            }
        ).encode("utf-8")

        headers = {"Content-Type": "application/json"}
        token = get_secret("einvoice_api_token")
        if token:
            headers["Authorization"] = f"Bearer {token}"

        if http_post_fn is not None:
            status_code, resp_bytes = http_post_fn(safe_url, req_payload, headers)
        else:
            req = Request(safe_url, data=req_payload, headers=headers, method="POST")
            with urlopen(req, timeout=20) as resp:  # noqa: S310
                status_code = int(getattr(resp, "status", 200))
                resp_bytes = resp.read()

        if status_code < 200 or status_code >= 300:
            raise RuntimeError(f"Online e-invoice endpoint returned HTTP {status_code}")

        try:
            remote_response = json.loads(resp_bytes.decode("utf-8"))
        except Exception:
            remote_response = {"raw": resp_bytes.decode("utf-8", errors="replace")}

    outputs, details = create_einvoice(fields, Path(output))
    details["mode"] = norm_mode
    details["used_ocr"] = used_ocr
    details["dpi"] = int(dpi)
    details["source_lang"] = norm_lang
    if remote_response:
        details["remote_response"] = remote_response
    return outputs, details
