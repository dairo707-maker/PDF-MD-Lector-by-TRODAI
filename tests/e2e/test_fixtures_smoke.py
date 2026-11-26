"""Immediate baseline self-tests for deterministic synthetic fixtures and test harness."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

from PIL import Image
import pypdfium2 as pdfium
import pytest

from tests.e2e.fixtures import (
    decode_nm_stream,
    encode_nm_frame,
    make_acroform_pdf,
    make_adversarial_markdown,
    make_blank_and_text_pdf,
    make_blank_only_pdf,
    make_corrupt_pdf,
    make_facturx_pdfa3,
    make_gfm_markdown_bundle,
    make_image_pdf,
    make_invoice_pdf,
    make_invoice_xml,
    make_local_glossary_json,
    make_mcp_requests,
    make_metadata_dirty_pdf,
    make_multipage_toc_pdf,
    make_pdf_with_image_and_attachment,
    make_pii_trace_payload,
    make_redaction_target_pdf,
    make_repairable_bad_xref_pdf,
    make_sample_images,
    make_scanned_text_pdf,
    make_theme_package,
    make_update_bundle,
    make_watermark_overlay_pdf,
)

pytestmark = pytest.mark.fixtures_smoke


def test_smoke_multipage_toc_and_blank_pdfs(tmp_path: Path) -> None:
    toc_pdf = make_multipage_toc_pdf(tmp_path / "toc.pdf", pages=4)
    doc = pdfium.PdfDocument(str(toc_pdf))
    try:
        assert len(doc) == 4
        toc_items = list(doc.get_toc())
        assert len(toc_items) >= 4
        tp0 = doc[0].get_textpage()
        try:
            text0 = tp0.get_text_range()
            assert "Privacidad" in text0
            assert "Electr\u00f3nica" in text0
        finally:
            tp0.close()
    finally:
        doc.close()

    mixed_pdf = make_blank_and_text_pdf(tmp_path / "mixed.pdf")
    doc_m = pdfium.PdfDocument(str(mixed_pdf))
    try:
        assert len(doc_m) == 3
        tp1 = doc_m[1].get_textpage()
        try:
            assert tp1.get_text_range().strip() == ""
        finally:
            tp1.close()
    finally:
        doc_m.close()

    blank_only = make_blank_only_pdf(tmp_path / "blank.pdf")
    doc_b = pdfium.PdfDocument(str(blank_only))
    try:
        assert len(doc_b) == 1
    finally:
        doc_b.close()


def test_smoke_acroform_image_attachment_watermark_pdfs(tmp_path: Path) -> None:
    acro_pdf = make_acroform_pdf(tmp_path / "acro.pdf")
    raw_acro = acro_pdf.read_bytes()
    assert b"/AcroForm" in raw_acro
    assert b"full_name" in raw_acro
    doc_a = pdfium.PdfDocument(str(acro_pdf))
    try:
        assert len(doc_a) == 1
    finally:
        doc_a.close()

    img_pdf = make_image_pdf(tmp_path / "img.pdf")
    doc_i = pdfium.PdfDocument(str(img_pdf))
    try:
        assert len(doc_i) == 1
    finally:
        doc_i.close()

    att_pdf = make_pdf_with_image_and_attachment(tmp_path / "att.pdf")
    raw_att = att_pdf.read_bytes()
    assert b"/EmbeddedFiles" in raw_att
    assert b"/Subtype /Image" in raw_att
    assert b"attachment.xml" in raw_att
    doc_att = pdfium.PdfDocument(str(att_pdf))
    try:
        assert len(doc_att) == 1
        tp = doc_att[0].get_textpage()
        try:
            assert "Image and Attachment PDF" in tp.get_text_range()
        finally:
            tp.close()
    finally:
        doc_att.close()

    wm_pdf = make_watermark_overlay_pdf(tmp_path / "wm.pdf")
    doc_wm = pdfium.PdfDocument(str(wm_pdf))
    try:
        assert len(doc_wm) == 1
    finally:
        doc_wm.close()


def test_smoke_redaction_metadata_scanned_corrupt_and_xref_pdfs(tmp_path: Path) -> None:
    red_pdf = make_redaction_target_pdf(tmp_path / "redact.pdf")
    raw_red = red_pdf.read_bytes()
    assert b"SECRET_TOKEN_XYZ" in raw_red
    assert b"/Prev " in raw_red
    doc_r = pdfium.PdfDocument(str(red_pdf))
    try:
        assert len(doc_r) == 2
        tp = doc_r[0].get_textpage()
        try:
            assert "SECRET_TOKEN_XYZ" in tp.get_text_range()
        finally:
            tp.close()
    finally:
        doc_r.close()

    meta_pdf = make_metadata_dirty_pdf(tmp_path / "meta.pdf")
    raw_meta = meta_pdf.read_bytes()
    assert b"/Metadata" in raw_meta
    assert b"/PieceInfo" in raw_meta
    assert b"Hidden XMP Creator" in raw_meta

    scan_pdf = make_scanned_text_pdf(tmp_path / "scan.pdf")
    doc_s = pdfium.PdfDocument(str(scan_pdf))
    try:
        assert len(doc_s) == 1
        tp = doc_s[0].get_textpage()
        try:
            assert tp.get_text_range().strip() == ""
        finally:
            tp.close()
    finally:
        doc_s.close()

    corrupt_pdf = make_corrupt_pdf(tmp_path / "corrupt.pdf")
    with pytest.raises(Exception):
        pdfium.PdfDocument(str(corrupt_pdf))

    bad_xref_pdf = make_repairable_bad_xref_pdf(tmp_path / "bad_xref.pdf")
    assert b"999999999 00000 n" in bad_xref_pdf.read_bytes()


def test_smoke_dian_and_facturx_invoices(tmp_path: Path) -> None:
    dian_pdf = make_invoice_pdf(tmp_path / "dian.pdf", profile="dian")
    doc_d = pdfium.PdfDocument(str(dian_pdf))
    try:
        tp = doc_d[0].get_textpage()
        try:
            assert "900123456" in tp.get_text_range()
        finally:
            tp.close()
    finally:
        doc_d.close()

    dian_xml_ok = make_invoice_xml(tmp_path / "dian_ok.xml", profile="dian", valid=True)
    dian_xml_bad = make_invoice_xml(tmp_path / "dian_bad.xml", profile="dian", valid=False)
    dian_xml_xxe = make_invoice_xml(tmp_path / "dian_xxe.xml", profile="dian", valid=True, xxe=True)
    root_ok = ET.fromstring(dian_xml_ok.read_text(encoding="utf-8"))
    assert root_ok.tag.endswith("Invoice")
    assert "999999.00" in dian_xml_bad.read_text(encoding="utf-8")
    assert "<!ENTITY xxe" in dian_xml_xxe.read_text(encoding="utf-8")

    fx_xml_ok = make_invoice_xml(tmp_path / "fx_ok.xml", profile="facturx", valid=True)
    root_fx = ET.fromstring(fx_xml_ok.read_text(encoding="utf-8"))
    assert root_fx.tag.endswith("CrossIndustryInvoice")

    fx_pdf_ok = make_facturx_pdfa3(tmp_path / "fx_ok.pdf", valid=True)
    assert b"factur-x.xml" in fx_pdf_ok.read_bytes()
    assert b"pdfaid:part" in fx_pdf_ok.read_bytes()

    fx_pdf_bad = make_facturx_pdfa3(tmp_path / "fx_bad.pdf", valid=False)
    assert b"factur-x.xml" not in fx_pdf_bad.read_bytes()


def test_smoke_markdown_images_glossaries_themes_and_pii(tmp_path: Path) -> None:
    gfm = make_gfm_markdown_bundle(tmp_path / "gfm")
    assert gfm["valid_md"].exists()
    assert gfm["valid_markdown"].exists()
    assert gfm["diagram_png"].exists()
    assert "- [x]" in gfm["valid_md"].read_text(encoding="utf-8")

    adv = make_adversarial_markdown(tmp_path / "adv_md")
    assert "<script>" in adv["xss"].read_text(encoding="utf-8")
    assert "https://example.com" in adv["remote"].read_text(encoding="utf-8")
    assert "outside_secret.png" in adv["traversal"].read_text(encoding="utf-8")
    assert adv["empty"].read_text(encoding="utf-8") == ""
    assert len(adv["huge"].read_text(encoding="utf-8")) > 10000

    imgs = make_sample_images(tmp_path / "imgs")
    with Image.open(imgs["png"]) as im:
        assert im.size == (640, 480)
    with Image.open(imgs["jpg"]) as im:
        assert im.size == (800, 600)
    with Image.open(imgs["webp"]) as im:
        assert im.format == "WEBP"
    with Image.open(imgs["gif"]) as im:
        assert getattr(im, "n_frames", 1) == 2

    g_list = make_local_glossary_json(tmp_path / "g_list.json", fmt="list")
    g_dict = make_local_glossary_json(tmp_path / "g_dict.json", fmt="dict")
    assert isinstance(json.loads(g_list.read_text(encoding="utf-8")), list)
    assert isinstance(json.loads(g_dict.read_text(encoding="utf-8")), dict)

    t_dark = make_theme_package(tmp_path / "themes", kind="valid_dark")
    t_remote = make_theme_package(tmp_path / "themes", kind="unsafe_remote")
    assert json.loads(t_dark.read_text(encoding="utf-8"))["base_mode"] == "dark"
    assert "https://evil.invalid" in t_remote.read_text(encoding="utf-8")

    trace, needles = make_pii_trace_payload(tmp_path / "pii.log")
    for val in needles.values():
        assert val in trace


def test_smoke_updater_nm_mcp_winreg_and_mock_server(
    tmp_path: Path,
    fake_winreg,
    local_mock_server,
) -> None:
    bundle_ok = make_update_bundle(tmp_path / "upd_ok", version="0.2.0", tamper=False)
    actual_sha = hashlib.sha256(bundle_ok["installer_bytes"]).hexdigest()
    assert actual_sha == bundle_ok["expected_sha256"]

    bundle_bad = make_update_bundle(tmp_path / "upd_bad", version="0.2.0", tamper=True)
    bad_sha = hashlib.sha256(bundle_bad["installer_bytes"]).hexdigest()
    assert bad_sha != bundle_bad["expected_sha256"]

    payload = {"action": "open_pdf", "path": "C:/docs/a.pdf"}
    framed = encode_nm_frame(payload)
    decoded = decode_nm_stream(framed + encode_nm_frame({"action": "ping"}))
    assert decoded == [payload, {"action": "ping"}]

    reqs = make_mcp_requests(tmp_path / "a.pdf", tmp_path / "b.md")
    assert [r["method"] for r in reqs] == ["initialize", "tools/list", "tools/call", "tools/call"]

    with fake_winreg.CreateKeyEx(fake_winreg.HKEY_CURRENT_USER, r"Software\Classes\.pdf\OpenWithProgids") as key:
        fake_winreg.SetValueEx(key, "TRODAI.PDFMDLector.PDF", 0, fake_winreg.REG_SZ, "")
    assert fake_winreg.key_exists(r"Software\Classes\.pdf\OpenWithProgids")
    assert fake_winreg.get_value(r"Software\Classes\.pdf\OpenWithProgids", "TRODAI.PDFMDLector.PDF") == ""

    local_mock_server.set_route("GET", "/ping", 200, {"ok": True})
    req = Request(f"{local_mock_server.base_url}/ping", method="GET")
    with urlopen(req, timeout=3) as resp:
        assert resp.status == 200
        assert json.loads(resp.read().decode("utf-8")) == {"ok": True}
    assert len(local_mock_server.requests_log) == 1


def test_smoke_conftest_isolated_window_and_tool_ids(
    make_isolated_window,
    clean_subprocess_env,
    isolated_appdata: Path,
) -> None:
    from tests.e2e.conftest import ALL_TOOL_IDS

    assert len(ALL_TOOL_IDS) == 44
    assert len(set(ALL_TOOL_IDS)) == 44
    assert clean_subprocess_env["QT_QPA_PLATFORM"] == "offscreen"
    assert clean_subprocess_env["LOCALAPPDATA"] == str(isolated_appdata)
    assert not any(k.startswith("_MEIPASS") for k in clean_subprocess_env)

    win = make_isolated_window()
    assert win.hub_view is not None
    assert win.viewer_view is not None
    assert win.studio_view is not None

