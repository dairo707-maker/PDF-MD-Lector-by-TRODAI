"""Tier 4 Realistic End-to-End Application Workloads (W-01..W-18)."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pypdfium2 as pdfium
import pytest
from PySide6.QtWidgets import QApplication

from tests.e2e.conftest import WORKSPACE_ROOT, load_attr
from tests.e2e.fixtures import (
    decode_nm_stream,
    encode_nm_frame,
    make_adversarial_markdown,
    make_blank_and_text_pdf,
    make_gfm_markdown_bundle,
    make_multipage_toc_pdf,
    make_pii_trace_payload,
    make_redaction_target_pdf,
    make_sample_images,
    make_scanned_text_pdf,
    make_self_signed_pkcs12,
    make_theme_package,
    make_update_bundle,
)

pytestmark = pytest.mark.tier4


def test_w01_legal_dossier_assembly(tmp_path: Path, make_isolated_window) -> None:
    ToolRegistry = load_attr("pdf_lector.core.tools", "ToolRegistry")
    imgs = make_sample_images(tmp_path / "imgs")
    img_pdf = tmp_path / "scans.pdf"
    assert ToolRegistry.get("images_to_pdf").run([imgs["png"], imgs["jpg"]], img_pdf, {"dpi": 300}).success

    p1 = make_multipage_toc_pdf(tmp_path / "d1.pdf", pages=2)
    p2 = make_blank_and_text_pdf(tmp_path / "d2.pdf")
    merged = tmp_path / "merged.pdf"
    assert ToolRegistry.get("merge_pdf").run([p1, p2, img_pdf], merged, {}).success

    cleaned = tmp_path / "cleaned.pdf"
    assert ToolRegistry.get("delete_blank_pages").run([merged], cleaned, {}).success

    numbered = tmp_path / "numbered.pdf"
    assert ToolRegistry.get("add_page_numbers").run([cleaned], numbered, {"format": "Fol. {n}/{total}"}).success

    final_pdf = tmp_path / "dossier_final.pdf"
    assert ToolRegistry.get("edit_bookmarks").run(
        [numbered], final_pdf, {"bookmarks": [{"title": "Anexos", "page": 4}]}
    ).success

    win = make_isolated_window()
    assert win.open_document_path(final_pdf) is True
    tab = win.viewer_view.current_tab()
    assert tab.session.page_count == 6
    assert tab.toc_tree.topLevelItemCount() >= 1


def test_w02_job_application_packet(tmp_path: Path) -> None:
    ToolRegistry = load_attr("pdf_lector.core.tools", "ToolRegistry")
    pikepdf = load_attr("pikepdf")
    cv = make_multipage_toc_pdf(tmp_path / "cv.pdf", pages=2, title="CV")
    certs = make_multipage_toc_pdf(tmp_path / "certs.pdf", pages=2, title="Certificados")

    packet = tmp_path / "packet.pdf"
    assert ToolRegistry.get("job_application_pdf").run([cv, certs], packet, {"title": "Postulacion"}).success
    cropped = tmp_path / "cropped.pdf"
    assert ToolRegistry.get("crop_pdf").run([packet], cropped, {"margin": 12}).success
    resized = tmp_path / "resized.pdf"
    assert ToolRegistry.get("resize_pages").run([cropped], resized, {"preset": "Letter"}).success
    final = tmp_path / "final_packet.pdf"
    assert ToolRegistry.get("compress_pdf").run([resized], final, {"dpi": 150, "quality": 70}).success
    with pikepdf.open(final) as pdf:
        assert len(pdf.pages) == 4


def test_w03_technical_markdown_spec_authoring_and_pdf_publish(
    tmp_path: Path,
    make_isolated_window,
    qapp: QApplication,
) -> None:
    md_eng = load_attr("pdf_lector.core.markdown_engine")
    md_p = make_gfm_markdown_bundle(tmp_path / "spec")["valid_md"]
    win = make_isolated_window()
    win.open_document_path(md_p)
    win.show()
    tab = win.viewer_view.current_tab()
    tab.set_view_mode("split")
    tab.insert_markdown_snippet("\n### 2.1 Conclusion\nDocumento publicado.\n")
    qapp.processEvents()
    assert tab.save_document() is True

    published_pdf = tmp_path / "spec_published.pdf"
    md_eng.markdown_to_pdf(md_p, published_pdf, dpi=150, source_lang="es", theme="light")
    assert win.open_document_path(published_pdf) is True
    assert win.viewer_view.tabs.count() >= 2


def test_w04_untrusted_external_markdown_security_inspection(
    tmp_path: Path,
    make_isolated_window,
    qapp: QApplication,
) -> None:
    adv = make_adversarial_markdown(tmp_path / "untrusted")
    combined = tmp_path / "untrusted" / "evil_bundle.md"
    combined.write_text(
        adv["xss"].read_text(encoding="utf-8")
        + "\n"
        + adv["remote"].read_text(encoding="utf-8")
        + "\n"
        + adv["traversal"].read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    win = make_isolated_window()
    assert win.open_document_path(combined) is True
    tab = win.viewer_view.current_tab()
    tab.set_view_mode("read")
    qapp.processEvents()
    html = tab.preview_browser.toHtml().lower()
    assert "<script" not in html
    assert "javascript:" not in html
    assert tab.preview_browser.openExternalLinks() is False
    assert len(tab.blocked_resources) >= 4


def test_w05_colombia_dian_ubl21_creation_signing_and_audit(tmp_path: Path, make_isolated_window) -> None:
    ToolRegistry = load_attr("pdf_lector.core.tools", "ToolRegistry")
    out_dir = tmp_path / "dian_w05"
    out_dir.mkdir()
    r_inv = ToolRegistry.get("create_einvoice").run(
        [],
        out_dir,
        {
            "profile": "dian_ubl21",
            "invoice_number": "SETP990000010",
            "emitter_nit": "900123456",
            "emitter_name": "TRODAI ANALITICA S.A.S.",
            "customer_nit": "800654321",
            "customer_name": "CLIENTE AUDITOR",
            "items": [{"description": "Auditoria", "qty": 2, "unit_price": 50000.0, "iva_rate": 0.19}],
        },
    )
    assert r_inv.success is True
    xml_p = next(p for p in r_inv.output_paths if p.suffix.lower() == ".xml")
    pdf_p = next(p for p in r_inv.output_paths if p.suffix.lower() == ".pdf")
    p12 = make_self_signed_pkcs12(tmp_path / "dian.p12", password="pass")
    signed = tmp_path / "dian_signed.pdf"
    assert ToolRegistry.get("sign_pdf").run([pdf_p], signed, {"p12_path": str(p12), "password": "pass"}).success
    r_val = ToolRegistry.get("validate_einvoice").run([xml_p], tmp_path / "val.json", {})
    assert r_val.success is True and r_val.details.get("valid") is True
    win = make_isolated_window()
    assert win.open_document_path(signed) is True


def test_w06_european_facturx_pdfa3_round_trip(tmp_path: Path) -> None:
    ToolRegistry = load_attr("pdf_lector.core.tools", "ToolRegistry")
    fx_pdf = tmp_path / "facturx_w06.pdf"
    assert ToolRegistry.get("create_einvoice").run(
        [],
        fx_pdf,
        {
            "profile": "facturx_en16931",
            "invoice_number": "FX-2026-100",
            "emitter_name": "TRODAI EU GmbH",
            "emitter_vat": "DE123456789",
            "customer_name": "Client SAS",
            "items": [{"description": "Suite License", "qty": 1, "unit_price": 200.0, "vat_rate": 0.19}],
        },
    ).success
    assert ToolRegistry.get("check_pdfa").run([fx_pdf], tmp_path / "pdfa.json", {}).details.get("conformant") is True
    atts_dir = tmp_path / "atts"
    atts_dir.mkdir()
    r_att = ToolRegistry.get("extract_attachments").run([fx_pdf], atts_dir, {})
    fx_xml = next(p for p in r_att.output_paths if p.name == "factur-x.xml")
    assert ToolRegistry.get("validate_einvoice").run([fx_xml], tmp_path / "val.json", {}).details.get("valid") is True


def test_w07_scanned_paper_invoice_ocr_to_einvoice_offline(tmp_path: Path) -> None:
    ToolRegistry = load_attr("pdf_lector.core.tools", "ToolRegistry")
    scan = make_scanned_text_pdf(
        tmp_path / "scanned_inv.pdf",
        text="FACTURA ELECTRONICA NIT 900123456\nSUBTOTAL 100000 IVA 19000 TOTAL 119000",
    )
    ocr_pdf = tmp_path / "ocr_inv.pdf"
    assert ToolRegistry.get("pdf_ocr").run(
        [scan],
        ocr_pdf,
        {"dpi": 300, "source_lang": "es", "ocr_provider": lambda _i, _l: "FACTURA NIT 900123456 SUBTOTAL 100000 IVA 19000 TOTAL 119000"},
    ).success
    xml_out = tmp_path / "inv_out.xml"
    r_ext = ToolRegistry.get("pdf_to_einvoice").run(
        [ocr_pdf], xml_out, {"mode": "local", "profile": "dian_ubl21", "dpi": 300, "source_lang": "es"}
    )
    assert r_ext.success is True and xml_out.exists()


def test_w08_confidential_contract_redact_strip_and_aes256_lock(tmp_path: Path) -> None:
    ToolRegistry = load_attr("pdf_lector.core.tools", "ToolRegistry")
    pikepdf = load_attr("pikepdf")
    src = make_redaction_target_pdf(tmp_path / "contract.pdf", secret="SECRET_TOKEN_XYZ")
    red = tmp_path / "red.pdf"
    clean = tmp_path / "clean.pdf"
    locked = tmp_path / "locked.pdf"
    unlocked = tmp_path / "unlocked.pdf"

    assert ToolRegistry.get("redact_pdf").run(
        [src], red, {"targets": [{"page_index": 0, "rect": [60, 620, 420, 675]}], "search_terms": ["SECRET_TOKEN_XYZ"]}
    ).success
    assert ToolRegistry.get("strip_metadata").run([red], clean, {}).success
    assert ToolRegistry.get("protect_pdf").run([clean], locked, {"user_password": "u1", "owner_password": "o1"}).success
    with pytest.raises(pikepdf.PasswordError):
        pikepdf.open(locked)
    assert ToolRegistry.get("unlock_pdf").run([locked], unlocked, {"password": "u1"}).success
    doc = pdfium.PdfDocument(str(unlocked))
    try:
        tp = doc[0].get_textpage()
        try:
            assert "SECRET_TOKEN_XYZ" not in tp.get_text_range()
        finally:
            tp.close()
    finally:
        doc.close()


def test_w09_academic_reading_annotations_ink_backup_and_detached_dictionary(
    tmp_path: Path,
    make_isolated_window,
    qapp: QApplication,
) -> None:
    pa = load_attr("pdf_lector.core.pdf_annotations")
    pdf_p = make_multipage_toc_pdf(tmp_path / "paper.pdf", pages=2)
    orig_sha = hashlib.sha256(pdf_p.read_bytes()).hexdigest()

    win = make_isolated_window()
    win.open_document_path(pdf_p)
    win.show()
    win.switch_view(1, immediate=True)
    assert win.viewer_view.dict_panel.isVisible() is False

    win.viewer_view.lookup_in_dictionary("criptografica", lang="es")
    win.viewer_view.detach_dictionary()
    qapp.processEvents()
    assert win.viewer_view.is_dictionary_detached() is True

    tab = win.viewer_view.current_tab()
    tab.pdf_canvas.add_highlight_rect((72.0, 650.0, 220.0, 668.0), color_hex="#FEF08A")
    tab.pdf_canvas.add_sticky_note((100.0, 500.0), "Nota academica")
    tab.pdf_canvas.record_ink_stroke([(80.0, 300.0, 0.4), (160.0, 320.0, 0.8)])
    pa.save_pdf_annotations(pdf_p, tab.pdf_canvas.annotations(), create_backup=True)

    baks = list(tmp_path.glob("paper*.bak*"))
    assert baks and hashlib.sha256(baks[0].read_bytes()).hexdigest() == orig_sha
    win.viewer_view.dock_dictionary()
    win.viewer_view.set_dictionary_visible(False)
    qapp.processEvents()
    assert win.viewer_view.dict_panel.isVisible() is False


def test_w10_multilingual_custom_glossary_and_language_pack_lifecycle(tmp_path: Path) -> None:
    DictionaryEngine = load_attr("pdf_lector.core.dictionary_engine", "DictionaryEngine")
    eng = DictionaryEngine(base_dir=tmp_path / "dicts")
    glossary = tmp_path / "fr_custom.json"
    glossary.write_text(json.dumps({"contrat": "Accord juridique."}), encoding="utf-8")
    assert eng.import_local_glossary("fr", glossary) == 1
    assert eng.lookup("contrat", "fr")

    pt_payload = json.dumps([{"word": "fatura", "pos": "subst.", "definition": "Documento fiscal.", "synonyms": ["recibo"]}]).encode("utf-8")
    assert eng.download_language_pack("pt", fetcher=lambda _u: pt_payload) == 1
    assert eng.lookup("fatura", "pt")
    assert eng.remove_language_pack("pt") is True


def test_w11_theme_studio_photo_gif_scrim_modes_and_f11(
    tmp_path: Path,
    make_isolated_window,
    qapp: QApplication,
) -> None:
    tp_mod = load_attr("pdf_lector.theme.theme_profile")
    me_mod = load_attr("pdf_lector.theme.motion_engine")
    pkg = make_theme_package(tmp_path / "themes", kind="valid_gif")
    prof, _ = tp_mod.import_theme_package(pkg)
    exported = tmp_path / "exported.trodai-theme.json"
    tp_mod.export_theme_package(prof, exported)
    reimported, warn = tp_mod.import_theme_package(exported)
    assert warn is None

    win = make_isolated_window()
    win.show()
    for mode in ("dark", "light"):
        win.apply_profile(replace(reimported, base_mode=mode), save=False)
        for w, h in ((1366, 720), (980, 650)):
            win.resize(w, h)
            qapp.processEvents()
    win.switch_view(1, immediate=True)
    win.set_fullscreen_mode(True)
    qapp.processEvents()
    assert win.is_fullscreen_mode
    win.set_fullscreen_mode(False)
    assert me_mod.compute_scrim_alpha(0.9, base_mode="dark", user_opacity=0.65, auto_scrim=True) > 150


def test_w12_adaptive_hub_reordering_pause_and_privacy_reset(
    make_isolated_window,
    tmp_path: Path,
    qapp: QApplication,
) -> None:
    LocalUsageInsights = load_attr("pdf_lector.core.usage_insights", "LocalUsageInsights")
    insights = LocalUsageInsights(tmp_path / "w12_insights.json")
    win = make_isolated_window(insights_store=insights)
    for _ in range(5):
        insights.record_tool_use("pdf_ocr")
        insights.record_tool_use("redact_pdf")
    win.hub_view.refresh_recommendations()
    qapp.processEvents()
    top6 = win.hub_view.quick_action_tool_ids()
    assert "pdf_ocr" in top6 and "redact_pdf" in top6

    insights.set_enabled(False)
    insights.record_tool_use("nup_pages")
    assert insights.get_recommended_tools() == []

    insights.set_enabled(True)
    insights.clear_history()
    win.hub_view.refresh_recommendations()
    qapp.processEvents()
    assert win.hub_view.quick_action_tool_ids()[0] == "sign_pdf"


def test_w13_crash_guard_and_anonymized_support_report_lifecycle(
    isolated_appdata: Path,
    tmp_path: Path,
    local_mock_server,
    qapp: QApplication,
) -> None:
    CrashGuard = load_attr("pdf_lector.core.crash_guard", "CrashGuard")
    SupportDialog = load_attr("pdf_lector.ui.support_dialog", "SupportDialog")
    fr = load_attr("pdf_lector.core.feedback_reporter")

    local_mock_server.set_route("POST", "/submit", 200, {"ok": True})
    logs_dir = isolated_appdata / "TRODAI" / "PDFLector" / "logs"
    cg = CrashGuard(logs_dir=logs_dir)
    cg.install()
    try:
        cg.begin_session()
        trace, needles = make_pii_trace_payload(tmp_path / "pii.log")
        try:
            raise ValueError(trace)
        except ValueError as exc:
            cg.log_exception(type(exc), exc, exc.__traceback__)
        cg.mark_clean_exit()
    finally:
        cg.uninstall()

    dlg = SupportDialog(
        endpoint_url=f"{local_mock_server.base_url}/submit",
        initial_logs=(logs_dir / "crash.log").read_text(encoding="utf-8"),
    )
    try:
        dlg.set_description("Error al procesar documento local")
        dlg.copy_to_clipboard()
        assert qapp.clipboard().text()
        assert dlg.submit_report() is True
        rep = fr.build_feedback_report(
            kind=fr.KIND_BUG,
            title="Error al procesar documento local",
            description="Error",
            error_text=dlg.preview_text(),
        )
        md_p, json_p = fr.save_local_report(rep)
        assert md_p.exists() and json_p.exists()
        sent = local_mock_server.requests_log[0]["body"].decode("utf-8")
        for val in needles.values():
            assert val not in sent
    finally:
        dlg.close()


def test_w14_verified_sha256_release_upgrade_flow(
    tmp_path: Path,
    isolated_appdata: Path,
    local_mock_server,
    make_isolated_window,
    qapp: QApplication,
) -> None:
    del isolated_appdata
    updater = load_attr("pdf_lector.core.updater")
    NotificationCenter = load_attr("pdf_lector.core.notifications", "NotificationCenter")

    good_bundle = make_update_bundle(tmp_path / "rel_ok", version="0.2.0", tamper=False, base_url=local_mock_server.base_url)
    local_mock_server.set_route("GET", "/releases/latest", 200, good_bundle["release_json"])
    local_mock_server.set_route("GET", "/releases/download/v0.2.0/PDF-MD-Lector-Setup.exe", 200, good_bundle["installer_bytes"], "application/octet-stream")
    local_mock_server.set_route("GET", "/releases/download/v0.2.0/SHA256SUMS.txt", 200, good_bundle["sums_text"], "text/plain")

    nc = NotificationCenter()
    rel = updater.check_for_updates("0.1.0", api_url=f"{local_mock_server.base_url}/releases/latest")
    assert rel is not None and rel.update_available
    nc.add("ACTUALIZACION", "Nueva version 0.2.0", rel.notes, category="update")

    win = make_isolated_window()
    md_p = make_gfm_markdown_bundle(tmp_path / "md")["valid_md"]
    win.open_document_path(md_p)
    win.viewer_view.current_tab().insert_markdown_snippet("\nEdit\n")
    assert win.can_restart_for_update() is False
    assert win.viewer_view.current_tab().save_document() is True
    assert win.can_restart_for_update() is True

    dl_exe = updater.download_verified_update(
        asset_url=f"{local_mock_server.base_url}/releases/download/v0.2.0/PDF-MD-Lector-Setup.exe",
        sums_url=f"{local_mock_server.base_url}/releases/download/v0.2.0/SHA256SUMS.txt",
        dest_dir=tmp_path / "dl",
    )
    assert hashlib.sha256(dl_exe.read_bytes()).hexdigest() == good_bundle["expected_sha256"]
    qapp.processEvents()


def test_w15_windows11_mock_hkcu_and_batch_cli_automation(
    tmp_path: Path,
    fake_winreg,
    clean_subprocess_env,
) -> None:
    import subprocess
    import sys

    wi = load_attr("pdf_lector.core.windows_integration")
    assert wi.register_windows_integration(
        exe_path=Path(r"C:\Program Files\TRODAI\PDF-MD-Lector.exe"),
        include_markdown=True,
        winreg_backend=fake_winreg,
    ).success
    p1 = make_multipage_toc_pdf(tmp_path / "a.pdf", pages=1)
    p2 = make_multipage_toc_pdf(tmp_path / "b.pdf", pages=1)
    merged = tmp_path / "merged.pdf"
    comp = tmp_path / "comp.pdf"
    assert subprocess.run([sys.executable, "-m", "pdf_lector.app", "--merge", str(p1), str(p2), "-o", str(merged)], env=clean_subprocess_env, capture_output=True, timeout=20).returncode == 0
    assert subprocess.run([sys.executable, "-m", "pdf_lector.app", "--compress", str(merged), "-o", str(comp)], env=clean_subprocess_env, capture_output=True, timeout=20).returncode == 0
    assert comp.exists()


def test_w16_mv3_browser_extension_native_messaging_capture(tmp_path: Path, clean_subprocess_env) -> None:
    import subprocess
    import sys

    ext_dir = WORKSPACE_ROOT / "browser-extension"
    manifest = json.loads((ext_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["manifest_version"] == 3 and "nativeMessaging" in manifest["permissions"]
    out_pdf = tmp_path / "captured_page.pdf"
    frames = encode_nm_frame({"action": "webpage_to_pdf", "html": "<h1>Captured Web Page</h1>", "output": str(out_pdf), "dpi": 150})
    proc = subprocess.run([sys.executable, "-m", "pdf_lector.core.native_messaging"], input=frames, capture_output=True, env=clean_subprocess_env, timeout=20)
    assert proc.returncode == 0
    msgs = decode_nm_stream(proc.stdout)
    assert msgs[0]["ok"] is True and out_pdf.exists()


def test_w17_authenticated_loopback_rest_api_workflow(tmp_path: Path, isolated_appdata: Path) -> None:
    del isolated_appdata
    api_srv = load_attr("pdf_lector.api.server")
    secrets_store = load_attr("pdf_lector.core.secrets_store")
    secrets_store.set_secret("local_api_token", "w17-secret-token")

    md_p = make_gfm_markdown_bundle(tmp_path / "md")["valid_md"]
    pdf_out = tmp_path / "w17.pdf"
    comp_out = tmp_path / "w17_comp.pdf"

    srv = api_srv.LocalApiServer(host="127.0.0.1", port=0)
    srv.start()
    try:
        with urlopen(f"{srv.base_url}/openapi.json", timeout=5) as resp:
            assert json.loads(resp.read().decode("utf-8"))["openapi"].startswith("3.1")
        with pytest.raises(HTTPError) as unauth:
            urlopen(f"{srv.base_url}/health", timeout=5)
        assert unauth.value.code == 401

        for tid, inp, outp in (("convert_to_pdf", md_p, pdf_out), ("compress_pdf", pdf_out, comp_out)):
            req = Request(
                f"{srv.base_url}/tools/{tid}",
                data=json.dumps({"inputs": [str(inp)], "output": str(outp), "params": {"dpi": 150}}).encode("utf-8"),
                headers={"Authorization": "Bearer w17-secret-token", "Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(req, timeout=10) as resp:
                assert resp.status == 200 and outp.exists()
    finally:
        srv.stop()


def test_w18_hybrid_ai_bm25_consented_byok_and_mcp_stdio(
    tmp_path: Path,
    isolated_appdata: Path,
    local_mock_server,
    clean_subprocess_env,
) -> None:
    del isolated_appdata
    import subprocess
    import sys

    ai_rag = load_attr("pdf_lector.core.ai_rag")
    secrets_store = load_attr("pdf_lector.core.secrets_store")
    secrets_store.set_secret("byok_openai_key", "sk-w18-secret")
    local_mock_server.set_route("POST", "/v1/chat/completions", 200, {"choices": [{"message": {"content": "Cita combinada [p. 3] y [## 1. Arquitectura]."}}]})

    pdf_p = make_multipage_toc_pdf(tmp_path / "spec.pdf", pages=4)
    md_p = make_gfm_markdown_bundle(tmp_path / "md")["valid_md"]
    idx = ai_rag.DocumentRagIndex()
    idx.index_pdf(pdf_p)
    idx.index_markdown(md_p)

    local_hits = idx.search("DIAN UBL 2.1", top_k=2)
    assert local_hits and local_hits[0].page_number == 3
    assert local_mock_server.requests_log == []

    ans = ai_rag.ask_document(
        idx,
        "Resume arquitectura y DIAN",
        provider="openai_compatible",
        endpoint_url=f"{local_mock_server.base_url}/v1/chat/completions",
        consent_granted=True,
    )
    assert "p. 3" in ans.answer_text

    mcp_reqs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "search_document", "arguments": {"path": str(md_p), "query": "Sanitizado"}}},
    ]
    proc = subprocess.run(
        [sys.executable, "-m", "pdf_lector.mcp_server"],
        input="\n".join(json.dumps(r) for r in mcp_reqs) + "\n",
        capture_output=True,
        text=True,
        env=clean_subprocess_env,
        timeout=20,
    )
    resps = [json.loads(line) for line in proc.stdout.strip().splitlines() if line.strip()]
    assert resps[1]["id"] == 2 and "result" in resps[1]
