"""Tier 3 Cross-Feature Pairwise Combinatorial E2E Tests (P-01..P-34)."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from PIL import Image
import pypdfium2 as pdfium
import pytest
from PySide6.QtCore import QPointF
from PySide6.QtWidgets import QApplication, QMessageBox

from tests.e2e.conftest import WORKSPACE_ROOT, load_attr
from tests.e2e.fixtures import (
    decode_nm_stream,
    encode_nm_frame,
    make_acroform_pdf,
    make_gfm_markdown_bundle,
    make_image_pdf,
    make_metadata_dirty_pdf,
    make_multipage_toc_pdf,
    make_office_inputs,
    make_pii_trace_payload,
    make_redaction_target_pdf,
    make_scanned_text_pdf,
    make_self_signed_pkcs12,
    make_theme_package,
    make_update_bundle,
    make_watermark_overlay_pdf,
)

pytestmark = pytest.mark.tier3


def test_p01_ocr_to_markdown_to_dictionary_to_bm25(tmp_path: Path) -> None:
    ToolRegistry = load_attr("pdf_lector.core.tools", "ToolRegistry")
    DictionaryEngine = load_attr("pdf_lector.core.dictionary_engine", "DictionaryEngine")
    ai_rag = load_attr("pdf_lector.core.ai_rag")

    scan_pdf = make_scanned_text_pdf(tmp_path / "scan.pdf", text="PRIVACIDAD EN DOCUMENTO PDF")
    ocr_pdf = tmp_path / "ocr.pdf"
    assert ToolRegistry.get("pdf_ocr").run(
        [scan_pdf],
        ocr_pdf,
        {"dpi": 300, "source_lang": "es", "ocr_provider": lambda _i, _l: "## Privacidad Local\nProtege la p\u00e1gina."},
    ).success

    out_md = tmp_path / "exported.md"
    assert ToolRegistry.get("convert_from_pdf").run([ocr_pdf], out_md, {"target_format": "md", "source_lang": "es"}).success

    eng = DictionaryEngine(base_dir=tmp_path / "dicts")
    assert eng.lookup("privacidad", "es")

    idx = ai_rag.DocumentRagIndex()
    idx.index_markdown(out_md)
    hits = idx.search("Privacidad", top_k=2)
    assert len(hits) >= 1


def test_p02_annotations_backup_flatten_and_compress(tmp_path: Path) -> None:
    pa = load_attr("pdf_lector.core.pdf_annotations")
    ToolRegistry = load_attr("pdf_lector.core.tools", "ToolRegistry")
    pikepdf = load_attr("pikepdf")

    src = make_multipage_toc_pdf(tmp_path / "ann.pdf", pages=2)
    orig_sha = hashlib.sha256(src.read_bytes()).hexdigest()
    recs = [
        pa.AnnotationRecord("hl-1", 0, "highlight", (72.0, 680.0, 200.0, 695.0), (), "#FEF08A", "An\u00f3nimo", "2026-11-24T12:00:00Z", "", None),
        pa.AnnotationRecord("ink-1", 0, "ink", (72.0, 400.0, 150.0, 440.0), [[(72.0, 410.0), (150.0, 430.0)]], "#0EA5E9", "An\u00f3nimo", "2026-11-24T12:00:00Z", "", None),
    ]
    pa.save_pdf_annotations(src, recs, create_backup=True, flatten=False)
    baks = list(tmp_path.glob("ann*.bak*"))
    assert baks and hashlib.sha256(baks[0].read_bytes()).hexdigest() == orig_sha

    flat = tmp_path / "flat.pdf"
    assert ToolRegistry.get("flatten_pdf").run([src], flat, {"dpi": 150}).success
    comp = tmp_path / "comp.pdf"
    assert ToolRegistry.get("compress_pdf").run([flat], comp, {"dpi": 150, "quality": 75}).success
    with pikepdf.open(comp) as pdf:
        assert len(pdf.pages[0].get("/Annots", [])) == 0


def test_p03_theme_motion_markdown_split_and_fullscreen(tmp_path: Path, make_isolated_window, qapp: QApplication) -> None:
    tp_mod = load_attr("pdf_lector.theme.theme_profile")
    win = make_isolated_window(profile=tp_mod.get_preset("trodai_mesh"))
    md_p = make_gfm_markdown_bundle(tmp_path / "md")["valid_md"]
    win.open_document_path(md_p)
    win.show()
    tab = win.viewer_view.current_tab()
    tab.set_view_mode("split")
    win.set_fullscreen_mode(True)
    qapp.processEvents()
    assert win.is_fullscreen_mode and not win.sidebar.isVisible()
    assert tab.editor_widget.isVisible() and tab.preview_browser.isVisible()
    win.set_fullscreen_mode(False)
    qapp.processEvents()
    assert not win.is_fullscreen_mode and win.sidebar.isVisible()


def test_p04_usage_insights_dynamic_quick_actions_and_clear_history(
    make_isolated_window,
    tmp_path: Path,
    qapp: QApplication,
) -> None:
    LocalUsageInsights = load_attr("pdf_lector.core.usage_insights", "LocalUsageInsights")
    insights = LocalUsageInsights(tmp_path / "p04_insights.json")
    win = make_isolated_window(insights_store=insights)
    for _ in range(4):
        insights.record_tool_use("redact_pdf")
    for _ in range(3):
        insights.record_tool_use("pdf_ocr")
    win.hub_view.refresh_recommendations()
    qapp.processEvents()
    top6 = win.hub_view.quick_action_tool_ids()
    assert top6[0] == "redact_pdf" and "pdf_ocr" in top6

    insights.clear_history()
    win.hub_view.refresh_recommendations()
    qapp.processEvents()
    assert win.hub_view.quick_action_tool_ids()[0] == "sign_pdf"


def test_p05_redact_strip_metadata_and_pdfium_inspect(tmp_path: Path) -> None:
    ToolRegistry = load_attr("pdf_lector.core.tools", "ToolRegistry")
    PdfDocumentSession = load_attr("pdf_lector.core.pdf_engine", "PdfDocumentSession")
    pikepdf = load_attr("pikepdf")

    src = make_redaction_target_pdf(tmp_path / "red.pdf", secret="SECRET_TOKEN_XYZ")
    red_out = tmp_path / "redacted.pdf"
    assert ToolRegistry.get("redact_pdf").run(
        [src], red_out, {"targets": [{"page_index": 0, "rect": [60, 620, 420, 675]}], "search_terms": ["SECRET_TOKEN_XYZ"]}
    ).success
    clean_out = tmp_path / "clean.pdf"
    assert ToolRegistry.get("strip_metadata").run([red_out], clean_out, {}).success

    session = PdfDocumentSession(clean_out)
    try:
        assert "SECRET_TOKEN_XYZ" not in session.extract_page_text(0)
        assert "SECRET_TOKEN_XYZ" not in session.extract_page_text(1)
    finally:
        session.close()
    with pikepdf.open(clean_out) as pdf:
        assert "/Metadata" not in pdf.Root and "/PieceInfo" not in pdf.Root


def test_p06_dian_einvoice_pkcs12_sign_and_validate(tmp_path: Path) -> None:
    ToolRegistry = load_attr("pdf_lector.core.tools", "ToolRegistry")
    out_dir = tmp_path / "dian"
    out_dir.mkdir()
    r_inv = ToolRegistry.get("create_einvoice").run(
        [],
        out_dir,
        {
            "profile": "dian_ubl21",
            "invoice_number": "SETP990000002",
            "emitter_nit": "900123456",
            "emitter_name": "TRODAI S.A.S.",
            "customer_nit": "800654321",
            "customer_name": "CLIENTE",
            "items": [{"description": "Servicio", "qty": 1, "unit_price": 100000.0, "iva_rate": 0.19}],
        },
    )
    assert r_inv.success is True
    xml_p = next(p for p in r_inv.output_paths if p.suffix.lower() == ".xml")
    pdf_p = next(p for p in r_inv.output_paths if p.suffix.lower() == ".pdf")
    p12 = make_self_signed_pkcs12(tmp_path / "sign.p12", password="pass")
    signed_pdf = tmp_path / "signed_dian.pdf"
    assert ToolRegistry.get("sign_pdf").run([pdf_p], signed_pdf, {"p12_path": str(p12), "password": "pass"}).success
    r_val = ToolRegistry.get("validate_einvoice").run([xml_p], tmp_path / "val.json", {})
    assert r_val.success is True and r_val.details.get("valid") is True


def test_p07_facturx_check_pdfa_and_extract_attachments(tmp_path: Path) -> None:
    ToolRegistry = load_attr("pdf_lector.core.tools", "ToolRegistry")
    fx_pdf = tmp_path / "fx.pdf"
    assert ToolRegistry.get("create_einvoice").run(
        [],
        fx_pdf,
        {
            "profile": "facturx_en16931",
            "invoice_number": "FX-2026-09",
            "emitter_name": "TRODAI EU",
            "emitter_vat": "DE123456789",
            "customer_name": "Buyer SA",
            "items": [{"description": "License", "qty": 1, "unit_price": 100.0, "vat_rate": 0.19}],
        },
    ).success
    r_pdfa = ToolRegistry.get("check_pdfa").run([fx_pdf], tmp_path / "pdfa.json", {})
    assert r_pdfa.success is True and r_pdfa.details.get("conformant") is True
    att_dir = tmp_path / "atts"
    att_dir.mkdir()
    r_att = ToolRegistry.get("extract_attachments").run([fx_pdf], att_dir, {})
    assert r_att.success is True and any(p.name == "factur-x.xml" for p in r_att.output_paths)


def test_p08_pdfium_hittest_detached_dictionary_and_language_pack(
    tmp_path: Path,
    make_isolated_window,
    qapp: QApplication,
) -> None:
    PdfDocumentSession = load_attr("pdf_lector.core.pdf_engine", "PdfDocumentSession")
    pdf_p = make_multipage_toc_pdf(tmp_path / "hit.pdf", pages=1)
    session = PdfDocumentSession(pdf_p)
    try:
        boxes = session.get_page_word_boxes(0)
        target = next(b for b in boxes if "privacidad" in b.word.lower())
        hit = session.hit_test_word(0, (target.rect_pdf[0] + target.rect_pdf[2]) / 2, (target.rect_pdf[1] + target.rect_pdf[3]) / 2, rotation=90)
        assert hit is not None
    finally:
        session.close()

    win = make_isolated_window()
    pt_payload = json.dumps([{"word": "privacidade", "pos": "subst.", "definition": "Controle local.", "synonyms": ["sigilo"]}]).encode("utf-8")
    win.dictionary_engine.download_language_pack("pt", fetcher=lambda _u: pt_payload)
    win.open_document_path(pdf_p)
    win.show()
    win.switch_view(1, immediate=True)
    win.viewer_view.lookup_in_dictionary("privacidade", lang="pt")
    win.viewer_view.detach_dictionary()
    qapp.processEvents()
    assert win.viewer_view.is_dictionary_detached() is True
    win.viewer_view.dock_dictionary()
    qapp.processEvents()
    assert win.viewer_view.is_dictionary_detached() is False


def test_p09_md_to_pdf_viewer_3mode_rail_and_bookmarks_persistence(tmp_path: Path, make_isolated_window) -> None:
    md_eng = load_attr("pdf_lector.core.markdown_engine")
    gfm = make_gfm_markdown_bundle(tmp_path / "md")["valid_md"]
    out_pdf = tmp_path / "from_md.pdf"
    md_eng.markdown_to_pdf(gfm, out_pdf)

    win = make_isolated_window()
    win.open_document_path(out_pdf)
    tab = win.viewer_view.current_tab()
    tab.set_rail_mode("both")
    assert tab.toc_tree.topLevelItemCount() >= 2
    assert tab.thumb_list.isVisibleTo(tab.nav_rail) and tab.toc_pane.isVisibleTo(tab.nav_rail)
    win.bookmarks_store.add_bookmark(out_pdf, "Bookmark MD", 0)
    assert len(win.bookmarks_store.list_bookmarks(out_pdf)) == 1


def test_p10_single_instance_cli_md_open_and_dirty_flag(tmp_path: Path, make_isolated_window, qapp: QApplication) -> None:
    SingleInstanceLock = load_attr("pdf_lector.core.single_instance", "SingleInstanceLock")
    md_p = make_gfm_markdown_bundle(tmp_path / "md")["valid_md"]
    win = make_isolated_window()
    lock = SingleInstanceLock(key=f"trodai-e2e-p10-{os.getpid()}")
    assert lock.acquire() is True
    assert lock.start_server(lambda paths: [win.open_document_path(Path(p)) for p in paths]) is True
    try:
        sec = SingleInstanceLock(key=lock.key)
        assert sec.acquire() is False
        assert sec.send_to_running_instance([md_p]) is True
        for _ in range(20):
            qapp.processEvents()
        win.viewer_view.current_tab().insert_markdown_snippet("\nDirty line\n")
        assert win.has_unsaved_changes() is True
    finally:
        lock.release()


def test_p11_updater_sha256_notification_and_unsaved_guard(
    tmp_path: Path,
    isolated_appdata: Path,
    local_mock_server,
    make_isolated_window,
    qapp: QApplication,
) -> None:
    del isolated_appdata
    updater = load_attr("pdf_lector.core.updater")
    NotificationCenter = load_attr("pdf_lector.core.notifications", "NotificationCenter")
    bundle = make_update_bundle(tmp_path / "rel", version="0.2.0", base_url=local_mock_server.base_url)
    local_mock_server.set_route("GET", "/releases/latest", 200, bundle["release_json"])
    local_mock_server.set_route("GET", "/releases/download/v0.2.0/PDF-MD-Lector-Setup.exe", 200, bundle["installer_bytes"], "application/octet-stream")
    local_mock_server.set_route("GET", "/releases/download/v0.2.0/SHA256SUMS.txt", 200, bundle["sums_text"], "text/plain")

    nc = NotificationCenter()
    rel = updater.check_for_updates("0.1.0", api_url=f"{local_mock_server.base_url}/releases/latest")
    assert rel is not None
    nc.add("ACTUALIZACION", f"v{rel.version}", rel.notes, category="update")
    assert nc.unread_count() == 1

    win = make_isolated_window()
    md_p = make_gfm_markdown_bundle(tmp_path / "md")["valid_md"]
    win.open_document_path(md_p)
    win.viewer_view.current_tab().insert_markdown_snippet("\nUnsaved\n")
    qapp.processEvents()
    assert win.can_restart_for_update() is False


def test_p12_crash_guard_feedback_reporter_and_support_dialog(
    isolated_appdata: Path,
    tmp_path: Path,
    qapp: QApplication,
) -> None:
    CrashGuard = load_attr("pdf_lector.core.crash_guard", "CrashGuard")
    fr = load_attr("pdf_lector.core.feedback_reporter")
    SupportDialog = load_attr("pdf_lector.ui.support_dialog", "SupportDialog")

    logs_dir = isolated_appdata / "TRODAI" / "PDFLector" / "logs"
    cg = CrashGuard(logs_dir=logs_dir)
    cg.install()
    try:
        cg.begin_session()
        trace, needles = make_pii_trace_payload(tmp_path / "pii.log")
        try:
            raise RuntimeError(trace)
        except RuntimeError as exc:
            cg.log_exception(type(exc), exc, exc.__traceback__)
        cg.mark_clean_exit()
    finally:
        cg.uninstall()

    dlg = SupportDialog(initial_logs=(logs_dir / "crash.log").read_text(encoding="utf-8"))
    try:
        preview = dlg.preview_text()
        for val in needles.values():
            assert val not in preview
        rep = fr.build_feedback_report(
            kind=fr.KIND_BUG,
            title="Crash report",
            description="Crash report",
            error_text=preview,
        )
        md_bak, json_bak = fr.save_local_report(rep)
        assert md_bak.exists() and json_bak.exists()
        qapp.processEvents()
    finally:
        dlg.close()


def test_p13_tool_registry_task_controller_cancel_and_temp_cleanup(tmp_path: Path, qapp: QApplication) -> None:
    tc_mod = load_attr("pdf_lector.core.task_control")
    TempManager = load_attr("pdf_lector.core.temp_manager", "TempManager")
    tm = TempManager(temp_root=tmp_path / "tm", register_atexit=False)
    ctrl = tc_mod.TaskController()
    mgr = tc_mod.TaskManager()
    frame_written = threading.Event()
    canceled_events: list[str] = []

    def _job(controller) -> str:
        tdir = tm.create_temp_dir(task_id="p13")
        (tdir / "frame.png").write_bytes(b"PNG_DATA")
        frame_written.set()
        try:
            for _ in range(50):
                tc_mod.stop_point(controller)
                time.sleep(0.01)
            tm.cleanup_task("p13")
            return "done"
        except tc_mod.TaskCanceledError:
            tm.cleanup_all()
            raise

    task = tc_mod.AsyncTask(_job, controller=ctrl)
    mgr.submit(task, on_canceled=lambda _tid, d: canceled_events.append(d))
    assert frame_written.wait(timeout=2.0)
    ctrl.cancel()
    assert mgr.wait_for_done(5000) is True
    deadline = time.monotonic() + 2.0
    while not canceled_events and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    assert len(canceled_events) == 1
    assert not tm.session_dir.exists()


def test_p14_rest_api_dpapi_bearer_path_guard_and_tools(tmp_path: Path, isolated_appdata: Path) -> None:
    del isolated_appdata
    api_srv = load_attr("pdf_lector.api.server")
    secrets_store = load_attr("pdf_lector.core.secrets_store")
    secrets_store.set_secret("local_api_token", "p14-token")
    p1 = make_multipage_toc_pdf(tmp_path / "a.pdf", pages=1)
    p2 = make_multipage_toc_pdf(tmp_path / "b.pdf", pages=1)
    merged = tmp_path / "merged.pdf"
    comp = tmp_path / "comp.pdf"

    srv = api_srv.LocalApiServer(host="127.0.0.1", port=0)
    srv.start()
    try:
        for route, inputs, out in (
            ("merge_pdf", [str(p1), str(p2)], str(merged)),
            ("compress_pdf", [str(merged)], str(comp)),
        ):
            req = Request(
                f"{srv.base_url}/tools/{route}",
                data=json.dumps({"inputs": inputs, "output": out, "params": {}}).encode("utf-8"),
                headers={"Authorization": "Bearer p14-token", "Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(req, timeout=10) as resp:
                assert resp.status == 200
        assert comp.exists()

        bad_req = Request(
            f"{srv.base_url}/tools/compress_pdf",
            data=json.dumps({"inputs": [r"\\evil\share\x.pdf"], "output": str(tmp_path / "x.pdf"), "params": {}}).encode("utf-8"),
            headers={"Authorization": "Bearer p14-token", "Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(HTTPError) as exc:
            urlopen(bad_req, timeout=5)
        assert exc.value.code in (400, 403, 422)
    finally:
        srv.stop()


def test_p15_mv3_extension_native_messaging_webpage_to_pdf(tmp_path: Path, clean_subprocess_env) -> None:
    manifest = json.loads((WORKSPACE_ROOT / "browser-extension" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["manifest_version"] == 3
    out_pdf = tmp_path / "ext_cap.pdf"
    stream = encode_nm_frame({"action": "webpage_to_pdf", "html": "<h1>MV3 Capture</h1>", "output": str(out_pdf), "dpi": 150}) + encode_nm_frame({"action": "open_pdf", "path": str(out_pdf)})
    proc = subprocess.run(
        [sys.executable, "-m", "pdf_lector.core.native_messaging"],
        input=stream,
        capture_output=True,
        env=clean_subprocess_env,
        timeout=20,
    )
    msgs = decode_nm_stream(proc.stdout)
    assert len(msgs) == 2 and all(m["ok"] is True for m in msgs) and out_pdf.exists()


def test_p16_mcp_stdio_bm25_search_and_path_guard(tmp_path: Path, clean_subprocess_env) -> None:
    pdf_p = make_multipage_toc_pdf(tmp_path / "doc.pdf", pages=4)
    reqs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "search_document", "arguments": {"path": str(pdf_p), "query": "CUFE NIT 900123456"}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "read_pdf_text", "arguments": {"path": "../../secret.pdf"}}},
    ]
    proc = subprocess.run(
        [sys.executable, "-m", "pdf_lector.mcp_server"],
        input="\n".join(json.dumps(r) for r in reqs) + "\n",
        capture_output=True,
        text=True,
        env=clean_subprocess_env,
        timeout=20,
    )
    resps = [json.loads(line) for line in proc.stdout.strip().splitlines() if line.strip()]
    assert "result" in resps[1] and "error" in resps[2]


def test_p17_byok_dpapi_consent_gate_and_temp_cleanup(
    tmp_path: Path,
    isolated_appdata: Path,
    local_mock_server,
) -> None:
    del isolated_appdata
    ai_rag = load_attr("pdf_lector.core.ai_rag")
    secrets_store = load_attr("pdf_lector.core.secrets_store")
    TempManager = load_attr("pdf_lector.core.temp_manager", "TempManager")
    secrets_store.set_secret("byok_openai_key", "sk-p17-test")
    local_mock_server.set_route("POST", "/v1/chat/completions", 200, {"choices": [{"message": {"content": "Cita [p. 1]"}}]})

    idx = ai_rag.DocumentRagIndex()
    idx.index_pdf(make_multipage_toc_pdf(tmp_path / "p.pdf", pages=2))
    blocked = ai_rag.ask_document(idx, "Resumen", provider="openai_compatible", endpoint_url=f"{local_mock_server.base_url}/v1/chat/completions", consent_granted=False)
    assert blocked.consent_blocked is True and local_mock_server.requests_log == []

    allowed = ai_rag.ask_document(idx, "Resumen", provider="openai_compatible", endpoint_url=f"{local_mock_server.base_url}/v1/chat/completions", consent_granted=True)
    assert "p. 1" in allowed.answer_text
    tm = TempManager(temp_root=tmp_path / "tm", register_atexit=False)
    assert tm.cleanup_all() == []


def test_p18_dialog_localizer_adaptive_button_at_980x650(make_isolated_window, qapp: QApplication) -> None:
    DialogLocalizer = load_attr("pdf_lector.ui.dialog_localizer", "DialogLocalizer")
    tp_mod = load_attr("pdf_lector.theme.theme_profile")
    win = make_isolated_window()
    loc = DialogLocalizer(qapp)
    win.show()
    for lang in ("es", "en", "pt"):
        loc.set_language(lang)
        win.retranslate(lang)
        for mode in ("dark", "light"):
            win.apply_profile(replace(tp_mod.get_preset("trodai_corporate"), base_mode=mode), save=False)
            win.resize(980, 650)
            qapp.processEvents()
    box = QMessageBox(QMessageBox.Icon.Warning, "Warn", "Msg", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
    loc.localize_dialog(box)
    assert box.button(QMessageBox.StandardButton.Yes).property("role") == "RiskAction"


def test_p19_touch_stylus_ink_rotated_page_iso32000_save_as(tmp_path: Path, qapp: QApplication) -> None:
    del qapp
    PdfCanvas = load_attr("pdf_lector.ui.pdf_canvas", "PdfCanvas")
    PdfDocumentSession = load_attr("pdf_lector.core.pdf_engine", "PdfDocumentSession")
    pa = load_attr("pdf_lector.core.pdf_annotations")
    pikepdf = load_attr("pikepdf")

    pdf_p = make_multipage_toc_pdf(tmp_path / "rot_ink.pdf", pages=1)
    session = PdfDocumentSession(pdf_p)
    canvas_w = PdfCanvas()
    try:
        canvas_w.set_session(session, page_index=0)
        canvas_w.set_rotation_and_zoom(rotation=90, zoom=1.2)
        canvas_w.set_touch_writing_enabled(True)
        canvas_w.handle_touch_points([(QPointF(50.0, 100.0), 1), (QPointF(120.0, 150.0), 1)], phase="draw")
        out_pdf = tmp_path / "rot_ink_saved.pdf"
        pa.save_pdf_annotations(pdf_p, canvas_w.annotations(), output_path=out_pdf, create_backup=False)
        with pikepdf.open(out_pdf) as pdf:
            assert any(str(a.get("/Subtype")) == "/Ink" for a in pdf.pages[0].get("/Annots", []))
    finally:
        canvas_w.close()
        session.close()


def test_p20_theme_animated_gif_and_battery_saver_guard(tmp_path: Path, qapp: QApplication) -> None:
    tp_mod = load_attr("pdf_lector.theme.theme_profile")
    me_mod = load_attr("pdf_lector.theme.motion_engine")
    pkg = make_theme_package(tmp_path / "themes", kind="valid_gif")
    prof, _ = tp_mod.import_theme_package(pkg)

    battery_state = {"on": False}
    guard = me_mod.MotionPerformanceGuard(os_animations_fn=lambda: True, battery_saver_fn=lambda: battery_state["on"])
    widget = me_mod.MotionBackdropWidget(guard=guard, is_viewer_surface=True)
    try:
        widget.set_profile(prof)
        widget.show()
        qapp.processEvents()
        assert widget.guard_state().status_code == "active"
        battery_state["on"] = True
        assert widget.guard_state().status_code == "battery"
    finally:
        widget.cleanup()
        widget.close()


def test_p21_mocked_hkcu_cli_merge_and_single_instance(
    tmp_path: Path,
    fake_winreg,
    clean_subprocess_env,
) -> None:
    wi = load_attr("pdf_lector.core.windows_integration")
    res = wi.register_windows_integration(
        exe_path=Path(r"C:\Program Files\TRODAI\PDF-MD-Lector.exe"),
        include_markdown=True,
        winreg_backend=fake_winreg,
    )
    assert res.success is True
    p1 = make_multipage_toc_pdf(tmp_path / "a.pdf", pages=1)
    p2 = make_multipage_toc_pdf(tmp_path / "b.pdf", pages=1)
    out = tmp_path / "m.pdf"
    proc = subprocess.run(
        [sys.executable, "-m", "pdf_lector.app", "--merge", str(p1), str(p2), "-o", str(out)],
        capture_output=True,
        env=clean_subprocess_env,
        timeout=20,
    )
    assert proc.returncode == 0 and out.exists()


def test_p22_compare_pdf_after_bookmarks_and_nup_imposition(tmp_path: Path) -> None:
    ToolRegistry = load_attr("pdf_lector.core.tools", "ToolRegistry")
    src = make_multipage_toc_pdf(tmp_path / "orig.pdf", pages=4)
    bm = tmp_path / "bm.pdf"
    nup = tmp_path / "nup.pdf"
    assert ToolRegistry.get("edit_bookmarks").run([src], bm, {"bookmarks": [{"title": "New", "page": 1}]}).success
    assert ToolRegistry.get("nup_pages").run([bm], nup, {"nup": 2}).success
    cmp_res = ToolRegistry.get("compare_pdf").run([src, nup], tmp_path / "diff.json", {})
    assert cmp_res.success is True and cmp_res.details.get("has_differences") is True


def test_p23_scanner_acquire_ocr_and_pdf_to_einvoice(tmp_path: Path) -> None:
    ToolRegistry = load_attr("pdf_lector.core.tools", "ToolRegistry")
    img = Image.new("RGB", (800, 1000), "#FFFFFF")
    scan_pdf = tmp_path / "acquired.pdf"
    assert ToolRegistry.get("import_scanner_camera").run(
        [], scan_pdf, {"dpi": 300, "source_lang": "es", "device_acquire_fn": lambda: img}
    ).success
    ocr_pdf = tmp_path / "ocr.pdf"
    assert ToolRegistry.get("pdf_ocr").run(
        [scan_pdf],
        ocr_pdf,
        {"dpi": 300, "source_lang": "es", "ocr_provider": lambda _i, _l: "NIT 900123456 Subtotal 100000 IVA 19000 Total 119000"},
    ).success
    out_xml = tmp_path / "inv.xml"
    r_inv = ToolRegistry.get("pdf_to_einvoice").run([ocr_pdf], out_xml, {"mode": "local", "dpi": 300, "source_lang": "es"})
    assert r_inv.success is True and out_xml.exists()


def test_p24_build_exe_sha256sums_and_updater_verify(tmp_path: Path, local_mock_server) -> None:
    updater = load_attr("pdf_lector.core.updater")
    bundle = make_update_bundle(tmp_path / "dist", version="0.2.0", base_url=local_mock_server.base_url)
    local_mock_server.set_route("GET", "/setup.exe", 200, bundle["installer_bytes"], "application/octet-stream")
    local_mock_server.set_route("GET", "/SHA256SUMS.txt", 200, bundle["sums_text"], "text/plain")
    exe = updater.download_verified_update(
        asset_url=f"{local_mock_server.base_url}/setup.exe",
        sums_url=f"{local_mock_server.base_url}/SHA256SUMS.txt",
        dest_dir=tmp_path / "dl",
    )
    assert hashlib.sha256(exe.read_bytes()).hexdigest() == bundle["expected_sha256"]


def test_p25_sign_local_dry_run_installer_iss_and_splash_bench(clean_subprocess_env) -> None:
    ps1 = WORKSPACE_ROOT / "scripts" / "sign_local.ps1"
    iss = WORKSPACE_ROOT / "packaging" / "installer.iss"
    assert "PrivilegesRequired=lowest" in iss.read_text(encoding="utf-8")
    r_ps = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1), "-DryRun"],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert r_ps.returncode == 0
    env = dict(clean_subprocess_env)
    env["PDF_LECTOR_BENCH_STARTUP"] = "1"
    env["PDF_LECTOR_BENCH_EXIT"] = "1"
    r_boot = subprocess.run([sys.executable, "-m", "pdf_lector.app"], capture_output=True, text=True, env=env, timeout=15)
    m = re.search(r"FIRST_FRAME_MS=([0-9.]+)", r_boot.stdout)
    assert m is not None and float(m.group(1)) < 300.0


def test_p26_job_application_strip_metadata_and_aes256_protect(tmp_path: Path) -> None:
    ToolRegistry = load_attr("pdf_lector.core.tools", "ToolRegistry")
    pikepdf = load_attr("pikepdf")
    cv = make_metadata_dirty_pdf(tmp_path / "cv.pdf")
    dossier = tmp_path / "dossier.pdf"
    clean = tmp_path / "clean.pdf"
    locked = tmp_path / "locked.pdf"
    assert ToolRegistry.get("job_application_pdf").run([cv], dossier, {}).success
    assert ToolRegistry.get("strip_metadata").run([dossier], clean, {}).success
    assert ToolRegistry.get("protect_pdf").run([clean], locked, {"user_password": "u1", "owner_password": "o1"}).success
    with pikepdf.open(locked, password="u1") as pdf:
        assert "/Metadata" not in pdf.Root


def test_p27_make_fillable_edit_pdf_and_flatten(tmp_path: Path) -> None:
    ToolRegistry = load_attr("pdf_lector.core.tools", "ToolRegistry")
    pikepdf = load_attr("pikepdf")
    acro = make_acroform_pdf(tmp_path / "acro.pdf")
    edited = tmp_path / "edited.pdf"
    flat = tmp_path / "flat.pdf"
    assert ToolRegistry.get("edit_pdf").run([acro], edited, {"page": 1, "text": "Approved", "x": 72, "y": 400}).success
    assert ToolRegistry.get("flatten_pdf").run([edited], flat, {"dpi": 150}).success
    with pikepdf.open(flat) as pdf:
        assert "/AcroForm" not in pdf.Root


def test_p28_html_to_pdf_watermark_and_optimize_web(tmp_path: Path) -> None:
    ToolRegistry = load_attr("pdf_lector.core.tools", "ToolRegistry")
    pikepdf = load_attr("pikepdf")
    office = make_office_inputs(tmp_path / "office")
    pdf1 = tmp_path / "html.pdf"
    pdf2 = tmp_path / "wm.pdf"
    pdf3 = tmp_path / "web.pdf"
    assert ToolRegistry.get("convert_to_pdf").run([office["html"]], pdf1, {"dpi": 150}).success
    assert ToolRegistry.get("add_watermark").run([pdf1], pdf2, {"text": "DRAFT"}).success
    assert ToolRegistry.get("optimize_web").run([pdf2], pdf3, {"dpi": 150}).success
    with pikepdf.open(pdf3) as pdf:
        assert pdf.is_linearized is True


def test_p29_overlay_crop_and_resize_pages(tmp_path: Path) -> None:
    ToolRegistry = load_attr("pdf_lector.core.tools", "ToolRegistry")
    pikepdf = load_attr("pikepdf")
    src = make_multipage_toc_pdf(tmp_path / "src.pdf", pages=1)
    wm = make_watermark_overlay_pdf(tmp_path / "wm.pdf")
    ov = tmp_path / "ov.pdf"
    cr = tmp_path / "cr.pdf"
    rs = tmp_path / "rs.pdf"
    assert ToolRegistry.get("overlay_pdf").run([src, wm], ov, {}).success
    assert ToolRegistry.get("crop_pdf").run([ov], cr, {"margin": 18}).success
    assert ToolRegistry.get("resize_pages").run([cr], rs, {"preset": "Letter"}).success
    with pikepdf.open(rs) as pdf:
        mb = [float(v) for v in pdf.pages[0].MediaBox]
        assert abs((mb[2] - mb[0]) - 612.0) <= 3.0


def test_p30_insights_markdown_editor_and_dictionary(tmp_path: Path, make_isolated_window, qapp: QApplication) -> None:
    LocalUsageInsights = load_attr("pdf_lector.core.usage_insights", "LocalUsageInsights")
    insights = LocalUsageInsights(tmp_path / "p30_insights.json")
    win = make_isolated_window(insights_store=insights)
    md_p = make_gfm_markdown_bundle(tmp_path / "md")["valid_md"]
    win.open_document_path(md_p)
    win.show()
    win.switch_view(1, immediate=True)
    win.viewer_view.lookup_in_dictionary("privacidad", lang="es")
    insights.record_dictionary_lookup("es")
    qapp.processEvents()
    assert win.viewer_view.dict_panel.isVisible() is True
    assert insights.get_suggested_dictionary_lang() == "es"


def test_p31_crash_guard_temp_manager_and_diagnostics_dialog(isolated_appdata: Path, tmp_path: Path, qapp: QApplication) -> None:
    CrashGuard = load_attr("pdf_lector.core.crash_guard", "CrashGuard")
    TempManager = load_attr("pdf_lector.core.temp_manager", "TempManager")
    DiagnosticsDialog = load_attr("pdf_lector.ui.diagnostics_dialog", "DiagnosticsDialog")

    logs_dir = isolated_appdata / "TRODAI" / "PDFLector" / "logs"
    cg = CrashGuard(logs_dir=logs_dir)
    cg.install()
    try:
        cg.begin_session()
        cg.log_app("INFO", "Diagnostic event P31")
        tm = TempManager(temp_root=tmp_path / "tm", register_atexit=False)
        d = tm.create_temp_dir(task_id="t1")
        (d / "tmp.bin").write_bytes(b"123")
        assert tm.cleanup_all() == []
        assert not d.exists() and not tm.session_dir.exists()
        dlg = DiagnosticsDialog(logs_dir=logs_dir)
        try:
            dlg.show()
            qapp.processEvents()
            assert dlg.isVisible()
        finally:
            dlg.close()
        cg.mark_clean_exit()
    finally:
        cg.uninstall()


def test_p32_preferences_anonymous_author_and_iso32000_save(tmp_path: Path, isolated_appdata: Path, qapp: QApplication) -> None:
    del isolated_appdata, qapp
    PreferencesDialog = load_attr("pdf_lector.ui.preferences_dialog", "PreferencesDialog")
    pa = load_attr("pdf_lector.core.pdf_annotations")
    pikepdf = load_attr("pikepdf")
    dlg = PreferencesDialog()
    try:
        author = dlg.default_annotation_author()
        assert author == "An\u00f3nimo"
    finally:
        dlg.close()
    pdf_p = make_multipage_toc_pdf(tmp_path / "p32.pdf", pages=1)
    rec = pa.AnnotationRecord("n1", 0, "text", (72.0, 500.0, 96.0, 524.0), (), "#FEF08A", author, "2026-11-24T12:00:00Z", "Note", None)
    saved = pa.save_pdf_annotations(pdf_p, [rec], create_backup=False)
    with pikepdf.open(saved) as pdf:
        assert str(pdf.pages[0]["/Annots"][0]["/T"]) == "An\u00f3nimo"


def test_p33_rest_api_usage_insights_and_task_controller(tmp_path: Path, isolated_appdata: Path) -> None:
    del isolated_appdata
    api_srv = load_attr("pdf_lector.api.server")
    secrets_store = load_attr("pdf_lector.core.secrets_store")
    LocalUsageInsights = load_attr("pdf_lector.core.usage_insights", "LocalUsageInsights")
    secrets_store.set_secret("local_api_token", "p33-tok")
    ins = LocalUsageInsights()
    src = make_image_pdf(tmp_path / "img.pdf")
    out = tmp_path / "comp.pdf"
    srv = api_srv.LocalApiServer(host="127.0.0.1", port=0)
    srv.start()
    try:
        req = Request(
            f"{srv.base_url}/tools/compress_pdf",
            data=json.dumps({"inputs": [str(src)], "output": str(out), "params": {"quality": 50}}).encode("utf-8"),
            headers={"Authorization": "Bearer p33-tok", "Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req, timeout=10) as resp:
            assert resp.status == 200
        ins.record_tool_use("compress_pdf")
        assert ins.get_recommended_tools()[0][0] == "compress_pdf"
    finally:
        srv.stop()


def test_p34_mcp_client_calls_mcp_server_bm25_and_md(tmp_path: Path, clean_subprocess_env) -> None:
    McpClient = load_attr("pdf_lector.core.mcp_client", "McpStdioClient")
    md_p = make_gfm_markdown_bundle(tmp_path / "md")["valid_md"]
    pdf_p = make_multipage_toc_pdf(tmp_path / "doc.pdf", pages=4)
    client = McpClient([sys.executable, "-m", "pdf_lector.mcp_server"], env=clean_subprocess_env)
    try:
        client.initialize()
        r_md = client.call_tool("read_markdown", {"path": str(md_p)})
        r_search = client.call_tool("search_document", {"path": str(pdf_p), "query": "DIAN UBL"})
        assert "Titulo Principal" in json.dumps(r_md)
        assert "3" in json.dumps(r_search)
    finally:
        client.close()
