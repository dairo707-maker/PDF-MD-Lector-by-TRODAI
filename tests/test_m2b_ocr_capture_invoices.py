"""Unit and integration tests for M2-B: ToolRegistry (44 tools), OCR, Capture, and Dual-Profile Invoices."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from PIL import Image
import pypdfium2 as pdfium
import pytest
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

from pdf_lector.core import capture_engine, invoice_engine, ocr_engine
from pdf_lector.core.i18n import HUB_CATEGORIES
from pdf_lector.core.tools import ToolRegistry
from tests.e2e.fixtures import (
    make_blank_only_pdf,
    make_facturx_pdfa3,
    make_gfm_markdown_bundle,
    make_invoice_pdf,
    make_invoice_xml,
    make_multipage_toc_pdf,
    make_office_inputs,
    make_sample_images,
    make_scanned_text_pdf,
)


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    return QApplication.instance() or QApplication(["pytest-m2b"])


# ---------------------------------------------------------------------------
# 1. ToolRegistry Completeness (44 unique IDs across 8 categories) & Params
# ---------------------------------------------------------------------------

def test_m2b_registry_exposes_all_44_unique_tools_matching_i18n() -> None:
    expected_ids = {tool.tool_id for cat in HUB_CATEGORIES for tool in cat.tools}
    registered_ids = set(ToolRegistry.list_ids())
    assert len(expected_ids) == 44
    assert expected_ids == registered_ids
    assert len(ToolRegistry.list_ids()) == 44


def test_m2b_applicable_tools_expose_dpi_quality_source_lang_specs() -> None:
    dpi_tools = (
        "convert_to_pdf",
        "convert_from_pdf",
        "images_to_pdf",
        "pdf_to_images",
        "pdf_ocr",
        "job_application_pdf",
        "capture_screen",
        "import_scanner_camera",
        "create_visual_invoice",
        "create_einvoice",
        "pdf_to_einvoice",
    )
    for tid in dpi_tools:
        names = {p.name for p in ToolRegistry.get(tid).params}
        assert "dpi" in names, f"{tid} missing dpi spec"

    lang_tools = (
        "convert_to_pdf",
        "convert_from_pdf",
        "pdf_ocr",
        "import_scanner_camera",
        "create_visual_invoice",
        "create_einvoice",
        "pdf_to_einvoice",
    )
    for tid in lang_tools:
        names = {p.name for p in ToolRegistry.get(tid).params}
        assert "source_lang" in names, f"{tid} missing source_lang spec"


def test_m2b_zero_unicode_emojis_in_assigned_files() -> None:
    emoji_re = re.compile(
        "["
        "\U0001F300-\U0001FAFF"
        "\U00002600-\U000027BF"
        "\U0001F1E6-\U0001F1FF"
        "]+",
        flags=re.UNICODE,
    )
    root = Path(__file__).resolve().parents[1] / "src" / "pdf_lector" / "core"
    for rel in (
        "ocr_engine.py",
        "capture_engine.py",
        "invoice_engine.py",
        "tools/convert.py",
        "tools/create.py",
        "tools/invoices.py",
        "tools/__init__.py",
    ):
        txt = (root / rel).read_text(encoding="utf-8")
        assert not emoji_re.search(txt), f"Emoji found in {rel}"


# ---------------------------------------------------------------------------
# 2. OCR Engine (WinRT, Tesseract probe, RapidOCR hook, searchable PDF/MD/TXT)
# ---------------------------------------------------------------------------

def test_m2b_ocr_clean_child_env_strips_meipass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("_MEIPASS", r"C:\fake\meipass")
    monkeypatch.setenv("_MEIPASS2", r"C:\fake\meipass2")
    cleaned = ocr_engine.clean_child_env({"CUSTOM_KEY": "1"})
    assert "_MEIPASS" not in cleaned
    assert "_MEIPASS2" not in cleaned
    assert cleaned["CUSTOM_KEY"] == "1"


def test_m2b_ocr_lang_validation_and_tesseract_subprocess_mock(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError):
        ocr_engine.normalize_source_lang("klingon")

    img = Image.new("RGB", (200, 80), "#FFFFFF")
    captured_calls: list[dict] = []

    class _Proc:
        returncode = 0
        stdout = b"TEXTO TESSERACT PRUEBA"
        stderr = b""

    def _fake_run(cmd, **kwargs):
        captured_calls.append({"cmd": cmd, "env": kwargs.get("env", {})})
        return _Proc()

    monkeypatch.setenv("_MEIPASS", r"C:\temp\_MEI123")
    monkeypatch.setattr(ocr_engine.subprocess, "run", _fake_run)
    txt, lines = ocr_engine.recognize_image_tesseract(
        img, source_lang="es", tesseract_cmd=Path(r"C:\Tesseract-OCR\tesseract.exe")
    )
    assert txt == "TEXTO TESSERACT PRUEBA"
    assert len(lines) == 1
    assert "-l" in captured_calls[0]["cmd"] and "spa" in captured_calls[0]["cmd"]
    assert "_MEIPASS" not in captured_calls[0]["env"]


def test_m2b_ocr_recognize_fallback_and_no_backend_error() -> None:
    img = Image.new("RGB", (120, 60), "#FFFFFF")
    txt, lines, backend = ocr_engine.recognize_image(
        img, source_lang="en", ocr_provider=lambda _i, l: f"Hello {l}"
    )
    assert txt == "Hello en"
    assert backend == "custom_provider"
    assert len(lines) == 1

    with pytest.raises(ocr_engine.NoOcrBackendError):
        ocr_engine.recognize_image(
            img,
            source_lang="es",
            disable_winrt_probe=True,
            disable_tesseract_probe=True,
            disable_rapidocr_probe=True,
        )


def test_m2b_ocr_pdf_outputs_searchable_pdf_txt_and_md(tmp_path: Path) -> None:
    scan = make_scanned_text_pdf(tmp_path / "scan.pdf", text="FACTURA 900123456")

    out_pdf = tmp_path / "out_ocr.pdf"
    res_pdf = ocr_engine.ocr_pdf(
        scan,
        out_pdf,
        dpi=200,
        source_lang="es",
        output_format="pdf",
        ocr_provider=lambda _i, _l: "FACTURA 900123456\nVALOR 119000",
    )
    assert res_pdf.total_chars > 10
    doc = pdfium.PdfDocument(str(out_pdf))
    try:
        tp = doc[0].get_textpage()
        try:
            assert "900123456" in tp.get_text_range()
        finally:
            tp.close()
    finally:
        doc.close()

    out_md = tmp_path / "out_ocr.md"
    ocr_engine.ocr_pdf(
        scan,
        out_md,
        dpi=150,
        source_lang="es",
        output_format="md",
        ocr_provider=lambda _i, _l: "Encabezado OCR\nDetalle de prueba",
    )
    assert "## Page 1" in out_md.read_text(encoding="utf-8")

    out_txt = tmp_path / "out_ocr.txt"
    ocr_engine.ocr_pdf(
        scan,
        out_txt,
        dpi=150,
        source_lang="en",
        output_format="txt",
        ocr_provider=lambda _i, _l: "Plain OCR Text",
    )
    assert "Plain OCR Text" in out_txt.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 3. Capture Engine (QScreen, WIA COM, QtMultimedia QCamera)
# ---------------------------------------------------------------------------

def test_m2b_capture_screen_region_dpi_and_validation(tmp_path: Path, qt_app: QApplication) -> None:
    del qt_app
    with pytest.raises(ValueError):
        capture_engine.parse_capture_region((0, 0, 0, 100))
    with pytest.raises(ValueError):
        capture_engine.parse_capture_region((0, 0, 100, -1))

    qimg = QImage(300, 150, QImage.Format.Format_RGBA8888)
    qimg.fill(QColor("#0EA5E9"))
    out_pdf = tmp_path / "screen_qimg.pdf"
    capture_engine.capture_region_to_pdf(
        out_pdf,
        region=(10, 20, 300, 150),
        dpi=150,
        quality=90,
        screen_grab_fn=lambda _r: qimg,
    )
    doc = pdfium.PdfDocument(str(out_pdf))
    try:
        w_pt, h_pt = doc[0].get_size()
        assert abs(w_pt - (300 * 72.0 / 150.0)) < 1.0
        assert abs(h_pt - (150 * 72.0 / 150.0)) < 1.0
    finally:
        doc.close()


def test_m2b_capture_wia_and_camera_injectable_probes(tmp_path: Path) -> None:
    img1 = Image.new("RGB", (200, 250), "#FFFFFF")
    img2 = Image.new("RGB", (200, 250), "#F8FAFC")

    out_wia = tmp_path / "wia_multi.pdf"
    saved, details = capture_engine.acquire_device_to_pdf(
        out_wia,
        source_type="wia",
        dpi=200,
        source_lang="es",
        wia_probe_fn=lambda: [capture_engine.CaptureDeviceInfo("wia-1", "Mock WIA", "wia")],
        wia_acquire_fn=lambda did, d: img1 if did == "" else img2,
    )
    assert saved.exists() and details["source_type"] == "wia"

    out_cam = tmp_path / "cam.pdf"
    saved_c, details_c = capture_engine.acquire_device_to_pdf(
        out_cam,
        source_type="camera",
        dpi=150,
        source_lang="en",
        camera_probe_fn=lambda: [capture_engine.CaptureDeviceInfo("cam-0", "Mock Cam", "camera")],
        camera_capture_fn=lambda _did: img2,
    )
    assert saved_c.exists() and details_c["source_type"] == "camera"

    with pytest.raises(capture_engine.CaptureError):
        capture_engine.acquire_device_to_pdf(
            tmp_path / "none.pdf",
            source_type="auto",
            wia_probe_fn=lambda: [],
            camera_probe_fn=lambda: [],
        )


# ---------------------------------------------------------------------------
# 4. Invoice Engine (DIAN UBL 2.1 + Factur-X EN 16931 PDF/A-3 + Extractor)
# ---------------------------------------------------------------------------

def test_m2b_invoice_dian_ubl21_inc_cufe_and_qr_pdf(tmp_path: Path) -> None:
    out_dir = tmp_path / "dian_inc"
    res = ToolRegistry.get("create_einvoice").run(
        [],
        out_dir,
        {
            "profile": "dian_ubl21",
            "invoice_number": "SETP990000123",
            "emitter_nit": "900123456",
            "emitter_name": "TRODAI RESTAURANTE S.A.S.",
            "customer_nit": "800654321",
            "customer_name": "CLIENTE",
            "items": [
                {"description": "Servicio", "qty": 1, "unit_price": 100000.0, "iva_rate": 0.19, "inc_rate": 0.08}
            ],
        },
    )
    assert res.success is True
    assert len(res.details["cufe"]) == 96
    assert res.details["iva_total"] == pytest.approx(19000.0)
    assert res.details["inc_total"] == pytest.approx(8000.0)
    assert res.details["total"] == pytest.approx(127000.0)

    xml_p = next(p for p in res.output_paths if p.suffix == ".xml")
    xml_txt = xml_p.read_text(encoding="utf-8")
    assert "INC" in xml_txt and "IVA" in xml_txt
    val = ToolRegistry.get("validate_einvoice").run([xml_p], tmp_path / "val.json", {})
    assert val.success is True and val.details["valid"] is True


def test_m2b_invoice_facturx_xrechnung_and_validation_edges(tmp_path: Path) -> None:
    fx_pdf = tmp_path / "xrechnung.pdf"
    res = ToolRegistry.get("create_einvoice").run(
        [],
        fx_pdf,
        {
            "profile": "xrechnung",
            "invoice_number": "XR-2026-42",
            "emitter_name": "TRODAI DE GmbH",
            "emitter_vat": "DE999888777",
            "customer_name": "Client GmbH",
            "currency": "EUR",
            "items": [{"description": "Audit", "qty": 2, "unit_price": 150.0, "vat_rate": 0.19}],
        },
    )
    assert res.success is True
    assert ToolRegistry.get("check_pdfa").run([fx_pdf], None, {}).details["conformant"] is True
    val = ToolRegistry.get("validate_einvoice").run([fx_pdf], None, {})
    assert val.success is True and val.details["valid"] is True

    # Reject combined tax > 100%
    bad = ToolRegistry.get("create_einvoice").run(
        [],
        tmp_path / "bad.pdf",
        {
            "profile": "dian_ubl21",
            "emitter_nit": "900123456",
            "items": [{"description": "Bad Tax", "qty": 1, "unit_price": 100.0, "iva_rate": 0.6, "inc_rate": 0.5}],
        },
    )
    assert bad.success is False

    # Reject XXE DOCTYPE in Factur-X XML too
    xxe_fx = make_invoice_xml(tmp_path / "fx_xxe.xml", profile="facturx", valid=True, xxe=True)
    v_xxe = ToolRegistry.get("validate_einvoice").run([xxe_fx], None, {})
    assert v_xxe.success is False and "xxe_or_dtd_forbidden" in v_xxe.details["errors"]


def test_m2b_pdf_to_einvoice_insecure_http_blocked_and_consented_loopback(tmp_path: Path) -> None:
    inv_pdf = make_invoice_pdf(tmp_path / "inv.pdf", profile="dian")
    r_http = ToolRegistry.get("pdf_to_einvoice").run(
        [inv_pdf],
        tmp_path / "out.xml",
        {
            "mode": "online",
            "consent_granted": True,
            "endpoint_url": "http://remote.example.invalid/api",
        },
    )
    assert r_http.success is False

    seen: list[str] = []

    def _fake_post(url: str, body: bytes, headers: dict[str, str]) -> tuple[int, bytes]:
        seen.append(url)
        assert headers["Content-Type"] == "application/json"
        assert b"900123456" in body
        return 200, json.dumps({"ok": True}).encode("utf-8")

    r_ok = ToolRegistry.get("pdf_to_einvoice").run(
        [inv_pdf],
        tmp_path / "out_ok.xml",
        {
            "mode": "online",
            "consent_granted": True,
            "endpoint_url": "https://api.trodai.invalid/v1/extract",
            "http_post_fn": _fake_post,
        },
    )
    assert r_ok.success is True
    assert seen == ["https://api.trodai.invalid/v1/extract"]


# ---------------------------------------------------------------------------
# 5. Convert, Create, and Invoices Tool Handlers
# ---------------------------------------------------------------------------

def test_m2b_convert_tools_webp_tiff_and_unc_guards(tmp_path: Path) -> None:
    imgs = make_sample_images(tmp_path / "imgs")
    pdf_webp = tmp_path / "from_webp.pdf"
    r_img = ToolRegistry.get("images_to_pdf").run([imgs["webp"]], pdf_webp, {"dpi": 200, "quality": 80})
    assert r_img.success is True

    tiff_dir = tmp_path / "tiffs"
    r_tiff = ToolRegistry.get("pdf_to_images").run([pdf_webp], tiff_dir, {"dpi": 150, "format": "tiff"})
    assert r_tiff.success is True and r_tiff.output_paths[0].suffix == ".tiff"

    # UNC output blocked
    r_unc = ToolRegistry.get("convert_to_pdf").run(
        [make_gfm_markdown_bundle(tmp_path / "md")["valid_md"]],
        Path(r"\\192.168.1.50\share\out.pdf"),
        {},
    )
    assert r_unc.success is False

    # Empty pdf_ocr blank page check
    blank = make_blank_only_pdf(tmp_path / "blank.pdf")
    r_ocr = ToolRegistry.get("pdf_ocr").run(
        [blank],
        tmp_path / "blank_ocr.pdf",
        {"dpi": 150, "source_lang": "es", "ocr_provider": lambda _i, _l: ""},
    )
    assert r_ocr.success is True


def test_m2b_job_application_mixed_pdf_and_image_bookmarks(tmp_path: Path) -> None:
    cv = make_multipage_toc_pdf(tmp_path / "cv.pdf", pages=1)
    imgs = make_sample_images(tmp_path / "imgs")
    out_dossier = tmp_path / "dossier_mixed.pdf"
    res = ToolRegistry.get("job_application_pdf").run(
        [cv, imgs["png"]],
        out_dossier,
        {"title": "Dossier TRODAI", "applicant_name": "Candidato QA", "dpi": 150, "quality": 85},
    )
    assert res.success is True
    doc = pdfium.PdfDocument(str(out_dossier))
    try:
        assert len(doc) == 2
        toc_titles = [item.get_title() for item in doc.get_toc()]
        assert "cv" in toc_titles and "sample" in toc_titles
    finally:
        doc.close()


def test_m2b_convert_office_roundtrip(tmp_path: Path) -> None:
    office = make_office_inputs(tmp_path / "office")
    for key in ("docx", "xlsx", "html", "txt"):
        out_pdf = tmp_path / f"{key}.pdf"
        res = ToolRegistry.get("convert_to_pdf").run([office[key]], out_pdf, {"dpi": 150, "source_lang": "es"})
        assert res.success is True and out_pdf.exists()

    src = make_multipage_toc_pdf(tmp_path / "src.pdf", pages=2)
    for fmt in ("md", "docx", "xlsx", "html", "txt"):
        out_f = tmp_path / f"from_pdf.{fmt}"
        res2 = ToolRegistry.get("convert_from_pdf").run([src], out_f, {"target_format": fmt, "source_lang": "en"})
        assert res2.success is True and out_f.exists() and out_f.stat().st_size > 0
