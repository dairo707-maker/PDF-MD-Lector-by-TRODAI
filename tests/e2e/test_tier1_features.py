"""Tier 1 Feature Coverage E2E Tests (FA-01..FA-25) for PDF/MD Lector by TRODAI."""

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
import tomllib
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

from PIL import Image
import pypdfium2 as pdfium
import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, Qt
from PySide6.QtGui import QColor, QImage, QKeySequence, QMouseEvent, QPixmap, QTabletEvent, QTouchEvent
from PySide6.QtWidgets import QApplication, QDialogButtonBox, QMessageBox, QPushButton, QWidget

from tests.e2e.conftest import ALL_TOOL_IDS, WORKSPACE_ROOT, load_attr
from tests.e2e.fixtures import (
    decode_nm_stream,
    encode_nm_frame,
    make_acroform_pdf,
    make_blank_and_text_pdf,
    make_facturx_pdfa3,
    make_gfm_markdown_bundle,
    make_image_pdf,
    make_invoice_pdf,
    make_invoice_xml,
    make_local_glossary_json,
    make_mcp_requests,
    make_metadata_dirty_pdf,
    make_multipage_toc_pdf,
    make_office_inputs,
    make_pdf_with_image_and_attachment,
    make_pii_trace_payload,
    make_redaction_target_pdf,
    make_repairable_bad_xref_pdf,
    make_sample_images,
    make_scanned_text_pdf,
    make_self_signed_pkcs12,
    make_theme_package,
    make_update_bundle,
    make_watermark_overlay_pdf,
)

pytestmark = pytest.mark.tier1

EMOJI_PATTERN = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F1E6-\U0001F1FF"
    "]+",
    flags=re.UNICODE,
)


def _find_clipped_buttons(root_widget: QWidget) -> list[str]:
    clipped: list[str] = []
    for btn in root_widget.findChildren(QPushButton):
        if not btn.isVisible():
            continue
        txt = btn.text().strip()
        if not txt:
            continue
        needed = btn.fontMetrics().horizontalAdvance(txt) + 12
        if btn.width() < needed:
            clipped.append(f"{txt} ({btn.width()} < {needed})")
    return clipped


# ---------------------------------------------------------------------------
# FA-01: Branding, Official IDs, Deps, Polygonal SVGs & Zero Emojis
# ---------------------------------------------------------------------------

def test_fa01_t1_01_official_branding_and_pyproject() -> None:
    pkg = load_attr("pdf_lector")
    assert pkg.APP_NAME == "PDF/MD Lector"
    assert pkg.COMPANY_NAME == "TRODAI"
    assert pkg.APP_USER_MODEL_ID == "TRODAI.PDFMDLector"
    assert pkg.read_version() == "0.4.2"
    assert (WORKSPACE_ROOT / "VERSION").read_text(encoding="utf-8").strip() == "0.4.2"

    pyproj = tomllib.loads((WORKSPACE_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    scripts = pyproj["project"]["scripts"]
    assert "pdf-md-lector" in scripts
    assert "pdf-lector" in scripts
    assert "pdf-md-lector-mcp" in scripts


def test_fa01_t1_02_required_runtime_dependencies_importable() -> None:
    for mod_name in (
        "PySide6",
        "pypdfium2",
        "pikepdf",
        "pypdf",
        "pyhanko",
        "reportlab",
        "PIL",
        "pdfplumber",
        "docx",
        "openpyxl",
        "markdown_it",
        "nh3",
        "fastapi",
        "uvicorn",
        "qrcode",
        "cryptography",
    ):
        mod = load_attr(mod_name)
        assert mod is not None


def test_fa01_t1_03_zero_unicode_emojis_in_source_and_hub(make_isolated_window) -> None:
    src_dir = WORKSPACE_ROOT / "src" / "pdf_lector"
    offenders: list[str] = []
    for py_file in src_dir.rglob("*.py"):
        if EMOJI_PATTERN.search(py_file.read_text(encoding="utf-8")):
            offenders.append(str(py_file))
    assert offenders == []

    win = make_isolated_window()
    for card in win.hub_view.all_tool_cards():
        assert not EMOJI_PATTERN.search(card.title_lbl.text())


def test_fa01_t1_04_polygonal_svgs_and_logo_sentinel_separation() -> None:
    icons_dir = WORKSPACE_ROOT / "assets" / "icons"
    svg_files = sorted(icons_dir.glob("*.svg"))
    assert len(svg_files) >= 42
    curve_re = re.compile(r"[CSQTacsqta]")
    for svg_path in svg_files:
        root = ET.parse(svg_path).getroot()
        for elem in root.iter():
            tag = elem.tag.split("}")[-1].lower()
            assert tag not in ("circle", "ellipse"), f"Curved element in {svg_path.name}"
            if tag == "path":
                d_attr = elem.attrib.get("d", "")
                assert not curve_re.search(d_attr), f"Curve cmd in {svg_path.name}: {d_attr}"

    logo_txt = (icons_dir / "pdf_lector_logo.svg").read_text(encoding="utf-8")
    sentinel_txt = (icons_dir / "trodai_sentinel.svg").read_text(encoding="utf-8")
    assert "fill-opacity" not in logo_txt
    assert "currentColor" not in logo_txt
    assert logo_txt != sentinel_txt


def test_fa01_t1_05_window_title_monolithic_trodai_and_opaque_tags(make_isolated_window, qapp: QApplication) -> None:
    SplashScreen = load_attr("pdf_lector.ui.splash_screen", "SplashScreen")
    StatusTag = load_attr("pdf_lector.ui.widgets", "StatusTag")

    splash = SplashScreen(mode="dark", lang="es")
    try:
        assert splash.title_lbl.text() == "PDF/MD Lector"
        assert splash.company_lbl.text().strip().endswith("TRODAI")
    finally:
        splash.finish_and_close()

    win = make_isolated_window()
    assert "PDF/MD Lector" in win.windowTitle()

    tag = StatusTag("PROCESO LOCAL", tone="ok")
    for mode in ("dark", "light"):
        tag.apply_theme(mode)
        assert "rgba(" not in tag.styleSheet().lower()
    qapp.processEvents()


# ---------------------------------------------------------------------------
# FA-02: path_guard, DPAPI secrets_store & TempManager
# ---------------------------------------------------------------------------

def test_fa02_t1_01_valid_local_paths_accepted(tmp_path: Path) -> None:
    pg = load_attr("pdf_lector.core.path_guard")
    pdf_p = make_multipage_toc_pdf(tmp_path / "doc.pdf", pages=2)
    gfm = make_gfm_markdown_bundle(tmp_path / "md")
    glossary = make_local_glossary_json(tmp_path / "glossary.json", fmt="list")
    imgs = make_sample_images(tmp_path / "imgs")

    assert pg.validate_local_pdf_path(pdf_p) == (True, "ok")
    assert pg.validate_local_markdown_path(gfm["valid_md"]) == (True, "ok")
    assert pg.validate_local_markdown_path(gfm["valid_markdown"]) == (True, "ok")
    assert pg.validate_local_any_doc(pdf_p) == (True, "ok")
    assert pg.validate_local_any_doc(gfm["valid_md"]) == (True, "ok")
    assert pg.validate_local_glossary_path(glossary) == (True, "ok")
    ok, reason = pg.validate_local_media_path(imgs["png"], check_exists=True)
    assert ok and reason == "ok"


def test_fa02_t1_02_resolve_safe_doc_resource_within_base_dir(tmp_path: Path) -> None:
    resolve_safe = load_attr("pdf_lector.core.path_guard", "resolve_safe_doc_resource")
    base = tmp_path / "workspace_md"
    gfm = make_gfm_markdown_bundle(base)
    resolved_img = resolve_safe(base, "./local_diagram.png")
    resolved_ref = resolve_safe(base, "sub/ref.md")
    assert resolved_img == gfm["diagram_png"].resolve()
    assert resolved_ref == gfm["ref_md"].resolve()


def test_fa02_t1_03_dpapi_secret_round_trip_and_at_rest_encryption(isolated_appdata: Path) -> None:
    secrets_store = load_attr("pdf_lector.core.secrets_store")
    secret_val = "sk-trodai-e2e-dpapi-998877"
    secrets_store.set_secret("openai_api_key", secret_val)

    secrets_file = isolated_appdata / "TRODAI" / "PDFLector" / "secrets.json"
    assert secrets_file.exists()
    raw_disk = secrets_file.read_text(encoding="utf-8")
    assert secret_val not in raw_disk
    assert secrets_store.get_secret("openai_api_key") == secret_val


def test_fa02_t1_04_secret_update_and_delete_lifecycle(isolated_appdata: Path) -> None:
    del isolated_appdata
    secrets_store = load_attr("pdf_lector.core.secrets_store")
    secrets_store.set_secret("api_bearer_token", "tok-initial")
    assert secrets_store.get_secret("api_bearer_token") == "tok-initial"
    secrets_store.set_secret("api_bearer_token", "tok-rotated")
    assert secrets_store.get_secret("api_bearer_token") == "tok-rotated"
    secrets_store.delete_secret("api_bearer_token")
    assert secrets_store.get_secret("api_bearer_token", default="MISSING") == "MISSING"


def test_fa02_t1_05_temp_manager_task_and_exit_cleanup(tmp_path: Path) -> None:
    TempManager = load_attr("pdf_lector.core.temp_manager", "TempManager")
    tm = TempManager(temp_root=tmp_path / "trodai_tmp", register_atexit=False)
    task_dir = tm.create_temp_dir(task_id="task_01", secure=True)
    secret_tmp = task_dir / "payload.bin"
    secret_tmp.write_bytes(b"SENSITIVE_TEMP_DATA_12345")
    assert secret_tmp.exists()

    assert tm.cleanup_task("task_01") == []
    assert not secret_tmp.exists()
    assert not task_dir.exists()

    other_dir = tm.create_temp_dir(task_id="task_02")
    (other_dir / "leftover.pdf").write_bytes(b"%PDF-1.7\n")
    assert tm.cleanup_all() == []
    assert not other_dir.exists()
    assert not tm.session_dir.exists()


# ---------------------------------------------------------------------------
# FA-03: CrashGuard, SingleInstanceLock, NotificationCenter & TaskController
# ---------------------------------------------------------------------------

def test_fa03_t1_01_crash_guard_clean_sentinel_and_app_log(isolated_appdata: Path) -> None:
    CrashGuard = load_attr("pdf_lector.core.crash_guard", "CrashGuard")
    logs_dir = isolated_appdata / "TRODAI" / "PDFLector" / "logs"
    cg = CrashGuard(logs_dir=logs_dir)
    cg.install()
    try:
        report = cg.begin_session()
        assert report.dirty is False
        cg.log_app("INFO", "Boot sequence started cleanly")
        assert (logs_dir / "app.log").exists()
        cg.mark_clean_exit()
    finally:
        cg.uninstall()
    assert "Boot sequence started cleanly" in (logs_dir / "app.log").read_text(encoding="utf-8")


def test_fa03_t1_02_crash_guard_unclean_exit_and_diagnostics_dialog(
    isolated_appdata: Path,
    qapp: QApplication,
) -> None:
    CrashGuard = load_attr("pdf_lector.core.crash_guard", "CrashGuard")
    DiagnosticsDialog = load_attr("pdf_lector.ui.diagnostics_dialog", "DiagnosticsDialog")
    logs_dir = isolated_appdata / "TRODAI" / "PDFLector" / "logs"

    prior = CrashGuard(logs_dir=logs_dir)
    prior.install()
    prior.begin_session()  # Intentionally omit mark_clean_exit() to leave dirty session sentinel
    prior.uninstall()

    current = CrashGuard(logs_dir=logs_dir)
    current.install()
    try:
        report = current.begin_session()
        assert report.dirty is True
        assert report.previous_crashed is True
        try:
            raise ValueError("Simulated unhandled failure for E2E")
        except ValueError as exc:
            current.log_exception(type(exc), exc, exc.__traceback__)

        crash_log = logs_dir / "crash.log"
        assert crash_log.exists()
        assert "Simulated unhandled failure for E2E" in crash_log.read_text(encoding="utf-8")

        dlg = DiagnosticsDialog(logs_dir=logs_dir)
        try:
            dlg.show()
            qapp.processEvents()
            assert dlg.isVisible()
        finally:
            dlg.close()
        current.mark_clean_exit()
    finally:
        current.uninstall()


def test_fa03_t1_03_single_instance_lock_forwards_pdf_and_md_tabs(
    tmp_path: Path,
    qapp: QApplication,
) -> None:
    SingleInstanceLock = load_attr("pdf_lector.core.single_instance", "SingleInstanceLock")
    pdf_path = make_multipage_toc_pdf(tmp_path / "forwarded.pdf", pages=2)
    md_path = make_gfm_markdown_bundle(tmp_path / "md")["valid_md"]

    key = f"trodai-e2e-si-{os.getpid()}-01"
    received: list[list[Path]] = []
    primary = SingleInstanceLock(key=key)
    assert primary.acquire() is True
    assert primary.start_server(lambda paths: received.append(paths)) is True
    try:
        secondary = SingleInstanceLock(key=key)
        assert secondary.acquire() is False
        sent = secondary.send_to_running_instance([pdf_path, md_path])
        assert sent is True
        deadline = time.monotonic() + 3.0
        while not received and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.01)
        assert len(received) == 1
        assert received[0] == [pdf_path.resolve(), md_path.resolve()]
    finally:
        primary.release()


def test_fa03_t1_04_notification_center_lifecycle_and_drawer(
    isolated_appdata: Path,
    qapp: QApplication,
) -> None:
    del isolated_appdata
    NotificationCenter = load_attr("pdf_lector.core.notifications", "NotificationCenter")
    NotificationsTasksDialog = load_attr("pdf_lector.ui.notifications_drawer", "NotificationsTasksDialog")

    nc = NotificationCenter()
    n1 = nc.add("ACTUALIZACION", "Actualizacion lista", "v0.2.0 disponible", category="update")
    n2 = nc.add("COMPLETADO", "OCR completado", "3 paginas procesadas", category="task")
    assert nc.unread_count() == 2

    assert nc.mark_read(n1.notif_id) is True
    assert nc.unread_count() == 1
    assert nc.remove(n2.notif_id) is True
    assert nc.unread_count() == 0

    dlg = NotificationsTasksDialog(notification_center=nc)
    try:
        dlg.show()
        qapp.processEvents()
        assert dlg.isVisible()
    finally:
        dlg.close()


def test_fa03_t1_05_task_controller_progress_pause_resume_complete(qapp: QApplication) -> None:
    tc_mod = load_attr("pdf_lector.core.task_control")
    ctrl = tc_mod.TaskController()
    mgr = tc_mod.TaskManager()
    seen_progress: list[int] = []
    finished_results: list[object] = []
    started_evt = threading.Event()

    def _work(progress_cb, controller) -> str:
        started_evt.set()
        for step in range(1, 11):
            tc_mod.stop_point(controller)
            progress_cb(step * 10, f"Step {step}")
            time.sleep(0.005)
        return "done"

    ctrl.pause()
    assert ctrl.status == "paused"
    task = tc_mod.AsyncTask(_work, controller=ctrl, min_emit_interval_s=0.0)
    mgr.submit(
        task,
        on_progress=lambda _tid, pct, _msg: seen_progress.append(pct),
        on_finished=lambda _tid, res: finished_results.append(res),
    )
    assert started_evt.wait(timeout=2.0)
    assert ctrl.is_paused is True
    ctrl.resume()
    assert mgr.wait_for_done(5000) is True
    deadline = time.monotonic() + 2.0
    while not finished_results and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    assert finished_results == ["done"]
    assert 100 in seen_progress


# ---------------------------------------------------------------------------
# FA-04: feedback_reporter Anonymizer, SupportDialog & Cloudflare Worker
# ---------------------------------------------------------------------------

def test_fa04_t1_01_feedback_reporter_strict_pii_anonymization(tmp_path: Path) -> None:
    fr = load_attr("pdf_lector.core.feedback_reporter")
    trace, needles = make_pii_trace_payload(tmp_path / "crash.log")
    report = fr.build_feedback_report(
        kind=fr.KIND_BUG,
        title="Error al abrir documento",
        description=f"Crash opening {needles['onedrive_path']} contact {needles['email']}",
        error_text=trace,
    )
    rendered = report.markdown_body
    for key, pii in needles.items():
        assert pii not in rendered, f"PII leaked for {key}: {pii}"
    assert "RuntimeError" in rendered


def test_fa04_t1_02_support_dialog_editable_preview_and_mocked_submit(
    tmp_path: Path,
    isolated_appdata: Path,
    local_mock_server,
    qapp: QApplication,
) -> None:
    del isolated_appdata
    SupportDialog = load_attr("pdf_lector.ui.support_dialog", "SupportDialog")
    local_mock_server.set_route("POST", "/submit", 200, {"ok": True, "id": "rep-001"})
    trace, needles = make_pii_trace_payload(tmp_path / "pii.log")

    dlg = SupportDialog(
        endpoint_url=f"{local_mock_server.base_url}/submit",
        initial_logs=trace,
    )
    try:
        dlg.show()
        qapp.processEvents()
        preview = dlg.preview_text()
        for pii in needles.values():
            assert pii not in preview
        dlg.set_description("Error reproducible al abrir documento")
        qapp.processEvents()
        assert dlg.submit_report() is True
        assert len(local_mock_server.requests_log) == 1
        sent_body = json.loads(local_mock_server.requests_log[0]["body"].decode("utf-8"))
        assert "Error reproducible" in json.dumps(sent_body)
        for pii in needles.values():
            assert pii not in json.dumps(sent_body)
    finally:
        dlg.close()


def test_fa04_t1_03_serverless_local_md_and_json_backup(isolated_appdata: Path, tmp_path: Path) -> None:
    fr = load_attr("pdf_lector.core.feedback_reporter")
    trace, needles = make_pii_trace_payload(tmp_path / "pii.log")
    report = fr.build_feedback_report(
        kind=fr.KIND_SUGGESTION,
        title="Add shortcut",
        description="Add keyboard shortcut",
        error_text=trace,
    )
    md_path, json_path = fr.save_local_report(report)

    assert md_path.exists() and md_path.suffix == ".md"
    assert json_path.exists() and json_path.suffix == ".json"
    assert str(isolated_appdata) in str(md_path)
    parsed = json.loads(json_path.read_text(encoding="utf-8"))
    for pii in needles.values():
        assert pii not in md_path.read_text(encoding="utf-8")
        assert pii not in json.dumps(parsed)


def test_fa04_t1_04_clipboard_copy_and_mailto_link_generation(tmp_path: Path, qapp: QApplication) -> None:
    fr = load_attr("pdf_lector.core.feedback_reporter")
    SupportDialog = load_attr("pdf_lector.ui.support_dialog", "SupportDialog")
    trace, needles = make_pii_trace_payload(tmp_path / "pii.log")
    report = fr.build_feedback_report(
        kind=fr.KIND_BUG,
        title="Report test",
        description="Report test details",
        error_text=trace,
    )

    mailto, _truncated = fr.build_mailto_uri(report.title, report.markdown_body, recipient="support@trodai.example")
    assert mailto.startswith("mailto:support@trodai.example?")
    assert "subject=" in mailto and "body=" in mailto
    for pii in needles.values():
        assert pii not in mailto

    dlg = SupportDialog(initial_logs=trace)
    try:
        dlg.copy_to_clipboard()
        clip = qapp.clipboard().text()
        assert len(clip) > 10
        for pii in needles.values():
            assert pii not in clip
    finally:
        dlg.close()


def test_fa04_t1_05_cloudflare_worker_and_wrangler_structure() -> None:
    worker_js = WORKSPACE_ROOT / "deploy" / "feedback-worker" / "worker.js"
    wrangler = WORKSPACE_ROOT / "deploy" / "feedback-worker" / "wrangler.toml"
    assert worker_js.exists()
    assert wrangler.exists()
    js_src = worker_js.read_text(encoding="utf-8")
    toml_src = wrangler.read_text(encoding="utf-8")
    assert "fetch(" in js_src
    assert "env." in js_src
    assert "POST" in js_src
    assert "name" in toml_src
    assert "compatibility_date" in toml_src


# ---------------------------------------------------------------------------
# FA-05: LocalUsageInsights & Dynamic Quick Actions Ranking
# ---------------------------------------------------------------------------

def test_fa05_t1_01_cold_start_default_6_quick_actions(make_isolated_window) -> None:
    win = make_isolated_window()
    quick_ids = win.hub_view.quick_action_tool_ids()
    assert quick_ids == [
        "sign_pdf",
        "merge_pdf",
        "delete_pages",
        "unlock_pdf",
        "convert_from_pdf",
        "compress_pdf",
    ]


def test_fa05_t1_02_dynamic_reordering_of_quick_actions(make_isolated_window, isolated_appdata: Path) -> None:
    LocalUsageInsights = load_attr("pdf_lector.core.usage_insights", "LocalUsageInsights")
    insights = LocalUsageInsights(isolated_appdata / "TRODAI" / "PDFLector" / "usage_insights.json")
    win = make_isolated_window(insights_store=insights)
    weights = [
        ("pdf_ocr", 10),
        ("redact_pdf", 8),
        ("create_einvoice", 6),
        ("split_pdf", 5),
        ("protect_pdf", 4),
        ("compare_pdf", 3),
    ]
    for tid, count in weights:
        for _ in range(count):
            insights.record_tool_use(tid)
    win.hub_view.refresh_recommendations()
    quick_ids = win.hub_view.quick_action_tool_ids()
    assert quick_ids == ["pdf_ocr", "redact_pdf", "create_einvoice", "split_pdf", "protect_pdf", "compare_pdf"]


def test_fa05_t1_03_clear_history_restores_6_defaults(make_isolated_window, isolated_appdata: Path) -> None:
    LocalUsageInsights = load_attr("pdf_lector.core.usage_insights", "LocalUsageInsights")
    insights = LocalUsageInsights(isolated_appdata / "TRODAI" / "PDFLector" / "usage_insights.json")
    win = make_isolated_window(insights_store=insights)
    for _ in range(12):
        insights.record_tool_use("redact_pdf")
    win.hub_view.refresh_recommendations()
    assert win.hub_view.quick_action_tool_ids()[0] == "redact_pdf"

    insights.clear_history()
    win.hub_view.refresh_recommendations()
    assert win.hub_view.quick_action_tool_ids() == [
        "sign_pdf",
        "merge_pdf",
        "delete_pages",
        "unlock_pdf",
        "convert_from_pdf",
        "compress_pdf",
    ]


def test_fa05_t1_04_document_classification_and_recommendations(tmp_path: Path) -> None:
    ui_mod = load_attr("pdf_lector.core.usage_insights")
    assert ui_mod.classify_document_text("FACTURA ELECTRONICA NIT 900123456 IVA 19% CUFE") == "invoice"
    assert ui_mod.classify_document_text("CONTRATO DE SERVICIOS CLAUSULA FIRMA") == "contract"
    assert ui_mod.classify_document_text("   ", page_count=4) == "scan"
    assert (
        ui_mod.classify_document_text(
            "Informe anual detallado con multiples secciones y analisis tecnico completo",
            page_count=12,
            toc_count=4,
        )
        == "report"
    )

    insights = ui_mod.LocalUsageInsights(tmp_path / "ins.json")
    insights.record_document_opened("FACTURA ELECTRONICA NIT 900123456 CUFE IVA", page_count=2)
    rec_ids = [tid for tid, _ in insights.get_recommended_tools(limit=4)]
    assert "validate_einvoice" in rec_ids


def test_fa05_t1_05_pause_and_resume_usage_insights(tmp_path: Path) -> None:
    LocalUsageInsights = load_attr("pdf_lector.core.usage_insights", "LocalUsageInsights")
    insights = LocalUsageInsights(tmp_path / "ins.json")
    insights.set_enabled(False)
    insights.record_tool_use("rotate_pdf")
    assert insights.get_recommended_tools() == []

    insights.set_enabled(True)
    recs = insights.get_recommended_tools()
    assert len(recs) >= 4
    assert all(tid != "rotate_pdf" for tid, _ in recs)


# ---------------------------------------------------------------------------
# FA-06: ToolRegistry Contract & DPI/Quality/Language Params (44 tool_ids)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tool_id", ALL_TOOL_IDS)
def test_fa06_t1_all_44_tools_registered_in_registry(tool_id: str) -> None:
    ToolRegistry = load_attr("pdf_lector.core.tools", "ToolRegistry")
    defn = ToolRegistry.get(tool_id)
    assert defn.id == tool_id
    assert isinstance(defn.category, str) and defn.category
    assert isinstance(defn.params, (list, tuple))
    assert callable(defn.run)


def test_fa06_t1_01_registry_covers_exact_44_unique_ids() -> None:
    ToolRegistry = load_attr("pdf_lector.core.tools", "ToolRegistry")
    registered = set(ToolRegistry.list_ids())
    assert len(ALL_TOOL_IDS) == 44
    assert set(ALL_TOOL_IDS).issubset(registered)


@pytest.mark.parametrize("dpi", [72, 150, 200, 300, 600, 240])
def test_fa06_t1_02_dpi_presets_and_custom_value_scale_output(tmp_path: Path, dpi: int) -> None:
    ToolRegistry = load_attr("pdf_lector.core.tools", "ToolRegistry")
    pdf_in = make_multipage_toc_pdf(tmp_path / "src.pdf", pages=1)
    out_dir = tmp_path / f"img_{dpi}"
    out_dir.mkdir()
    res = ToolRegistry.get("pdf_to_images").run(
        [pdf_in],
        out_dir,
        {"dpi": dpi, "format": "png"},
    )
    assert res.success is True
    assert len(res.output_paths) == 1
    with Image.open(res.output_paths[0]) as im:
        # Letter width is 8.5 inches -> pixel width ~ 8.5 * dpi
        expected_w = round(8.5 * dpi)
        assert abs(im.width - expected_w) <= 6


@pytest.mark.parametrize("quality", [10, 55, 95])
def test_fa06_t1_03_quality_param_accepted_on_compress_and_images(tmp_path: Path, quality: int) -> None:
    ToolRegistry = load_attr("pdf_lector.core.tools", "ToolRegistry")
    pdf_in = make_image_pdf(tmp_path / "img.pdf", size=(256, 256))
    out_pdf = tmp_path / f"comp_{quality}.pdf"
    res = ToolRegistry.get("compress_pdf").run([pdf_in], out_pdf, {"dpi": 150, "quality": quality})
    assert res.success is True
    assert out_pdf.exists() and out_pdf.stat().st_size > 0


@pytest.mark.parametrize("source_lang", ["auto", "es", "en", "pt", "fr", "de", "it", "multi"])
def test_fa06_t1_04_source_lang_codes_accepted(tmp_path: Path, source_lang: str) -> None:
    ToolRegistry = load_attr("pdf_lector.core.tools", "ToolRegistry")
    gfm = make_gfm_markdown_bundle(tmp_path / f"md_{source_lang}")
    out_pdf = tmp_path / f"out_{source_lang}.pdf"
    res = ToolRegistry.get("convert_to_pdf").run(
        [gfm["valid_md"]],
        out_pdf,
        {"dpi": 150, "source_lang": source_lang},
    )
    assert res.success is True
    assert out_pdf.exists()
    assert res.details.get("source_lang") == source_lang


def test_fa06_t1_05_applicable_tools_expose_dpi_and_tool_workspace_renders(tmp_path: Path, qapp: QApplication) -> None:
    ToolRegistry = load_attr("pdf_lector.core.tools", "ToolRegistry")
    ToolWorkspace = load_attr("pdf_lector.ui.tool_workspace", "ToolWorkspace")

    for tid in (
        "pdf_ocr",
        "pdf_to_images",
        "images_to_pdf",
        "compress_pdf",
        "convert_to_pdf",
        "convert_from_pdf",
        "flatten_pdf",
        "optimize_web",
        "capture_screen",
        "import_scanner_camera",
        "pdf_to_einvoice",
    ):
        names = {p.name for p in ToolRegistry.get(tid).params}
        assert "dpi" in names, f"{tid} missing dpi ToolParamSpec"

    for tid in (
        "pdf_ocr",
        "convert_to_pdf",
        "convert_from_pdf",
        "import_scanner_camera",
        "pdf_to_einvoice",
    ):
        names = {p.name for p in ToolRegistry.get(tid).params}
        assert "source_lang" in names, f"{tid} missing source_lang ToolParamSpec"

    ws = ToolWorkspace(ToolRegistry.get("pdf_ocr"))
    try:
        ws.resize(820, 620)
        ws.show()
        qapp.processEvents()
        assert ws.primary_action_btn.isVisible()
        assert ws.dpi_control is not None
        assert ws.lang_control is not None
        assert _find_clipped_buttons(ws) == []
    finally:
        ws.close()
    del tmp_path


# ---------------------------------------------------------------------------
# FA-07: Organize & Edit PDF Suite (All 20 tools)
# ---------------------------------------------------------------------------

def test_fa07_t1_01_merge_and_split_pdf_round_trip(tmp_path: Path) -> None:
    ToolRegistry = load_attr("pdf_lector.core.tools", "ToolRegistry")
    p1 = make_multipage_toc_pdf(tmp_path / "p1.pdf", pages=4)
    p2 = make_blank_and_text_pdf(tmp_path / "p2.pdf")
    merged = tmp_path / "merged.pdf"
    res_m = ToolRegistry.get("merge_pdf").run([p1, p2], merged, {})
    assert res_m.success is True
    doc = pdfium.PdfDocument(str(merged))
    try:
        assert len(doc) == 7
    finally:
        doc.close()

    split_dir = tmp_path / "split"
    split_dir.mkdir()
    res_s = ToolRegistry.get("split_pdf").run([merged], split_dir, {"ranges": "1-4,5-7"})
    assert res_s.success is True
    assert len(res_s.output_paths) == 2


def test_fa07_t1_02_rotate_delete_pages_and_delete_blank_pages(tmp_path: Path) -> None:
    ToolRegistry = load_attr("pdf_lector.core.tools", "ToolRegistry")
    pikepdf = load_attr("pikepdf")
    mixed = make_blank_and_text_pdf(tmp_path / "mixed.pdf")

    rotated = tmp_path / "rot.pdf"
    assert ToolRegistry.get("rotate_pdf").run([mixed], rotated, {"pages": [1], "angle": 90}).success
    with pikepdf.open(rotated) as pdf:
        assert int(pdf.pages[0].get("/Rotate", 0)) == 90

    deleted = tmp_path / "del.pdf"
    assert ToolRegistry.get("delete_pages").run([mixed], deleted, {"pages": [3]}).success
    with pikepdf.open(deleted) as pdf:
        assert len(pdf.pages) == 2

    no_blanks = tmp_path / "no_blanks.pdf"
    assert ToolRegistry.get("delete_blank_pages").run([mixed], no_blanks, {}).success
    with pikepdf.open(no_blanks) as pdf:
        assert len(pdf.pages) == 2


def test_fa07_t1_03_extract_reorder_and_nup_pages(tmp_path: Path) -> None:
    ToolRegistry = load_attr("pdf_lector.core.tools", "ToolRegistry")
    src = make_multipage_toc_pdf(tmp_path / "src.pdf", pages=4)

    ext = tmp_path / "ext.pdf"
    assert ToolRegistry.get("extract_pages").run([src], ext, {"pages": [2, 4]}).success
    d1 = pdfium.PdfDocument(str(ext))
    try:
        assert len(d1) == 2
    finally:
        d1.close()

    reord = tmp_path / "reord.pdf"
    assert ToolRegistry.get("reorder_pages").run([src], reord, {"order": [4, 3, 2, 1]}).success
    d2 = pdfium.PdfDocument(str(reord))
    try:
        tp0 = d2[0].get_textpage()
        try:
            assert "Anotaciones ISO 32000" in tp0.get_text_range()
        finally:
            tp0.close()
    finally:
        d2.close()

    nup = tmp_path / "nup.pdf"
    assert ToolRegistry.get("nup_pages").run([src], nup, {"nup": 4}).success
    d3 = pdfium.PdfDocument(str(nup))
    try:
        assert len(d3) == 1
    finally:
        d3.close()


def test_fa07_t1_04_extract_images_and_extract_attachments(tmp_path: Path) -> None:
    ToolRegistry = load_attr("pdf_lector.core.tools", "ToolRegistry")
    payload = b"<?xml version='1.0'?><trodai>EXACT_ATT_BYTES</trodai>"
    src = make_pdf_with_image_and_attachment(tmp_path / "bundle.pdf", "attachment.xml", payload)

    img_dir = tmp_path / "imgs"
    img_dir.mkdir()
    res_img = ToolRegistry.get("extract_images").run([src], img_dir, {})
    assert res_img.success is True
    assert len(res_img.output_paths) >= 1
    with Image.open(res_img.output_paths[0]) as im:
        assert im.size == (128, 128)

    att_dir = tmp_path / "atts"
    att_dir.mkdir()
    res_att = ToolRegistry.get("extract_attachments").run([src], att_dir, {})
    assert res_att.success is True
    assert len(res_att.output_paths) >= 1
    assert res_att.output_paths[0].read_bytes() == payload


def test_fa07_t1_05_edit_bookmarks_metadata_and_edit_pdf(tmp_path: Path) -> None:
    ToolRegistry = load_attr("pdf_lector.core.tools", "ToolRegistry")
    pikepdf = load_attr("pikepdf")
    src = make_multipage_toc_pdf(tmp_path / "src.pdf", pages=4)

    bm_out = tmp_path / "bm.pdf"
    assert ToolRegistry.get("edit_bookmarks").run(
        [src], bm_out, {"bookmarks": [{"title": "Custom Sec", "page": 2}]}
    ).success
    d = pdfium.PdfDocument(str(bm_out))
    try:
        assert any("Custom Sec" in (item.get_title() or "") for item in d.get_toc())
    finally:
        d.close()

    meta_out = tmp_path / "meta.pdf"
    assert ToolRegistry.get("edit_metadata").run(
        [src], meta_out, {"title": "TRODAI Doc", "author": "QA Tester"}
    ).success
    with pikepdf.open(meta_out) as pdf:
        assert str(pdf.docinfo.get("/Title", "")) == "TRODAI Doc"
        assert str(pdf.docinfo.get("/Author", "")) == "QA Tester"

    edit_out = tmp_path / "edited.pdf"
    assert ToolRegistry.get("edit_pdf").run(
        [src], edit_out, {"page": 1, "text": "TRODAI_STAMP_EDIT", "x": 72, "y": 500}
    ).success
    d2 = pdfium.PdfDocument(str(edit_out))
    try:
        tp = d2[0].get_textpage()
        try:
            assert "TRODAI_STAMP_EDIT" in tp.get_text_range()
        finally:
            tp.close()
    finally:
        d2.close()


def test_fa07_t1_06_watermark_page_numbers_overlay_and_annotate(tmp_path: Path) -> None:
    ToolRegistry = load_attr("pdf_lector.core.tools", "ToolRegistry")
    pikepdf = load_attr("pikepdf")
    src = make_multipage_toc_pdf(tmp_path / "src.pdf", pages=2)
    wm = make_watermark_overlay_pdf(tmp_path / "wm.pdf", label="CONFIDENCIAL")

    wm_out = tmp_path / "wm_out.pdf"
    assert ToolRegistry.get("add_watermark").run([src], wm_out, {"text": "CONFIDENCIAL"}).success

    num_out = tmp_path / "num_out.pdf"
    assert ToolRegistry.get("add_page_numbers").run([src], num_out, {"format": "Pag {n}/{total}"}).success

    ov_out = tmp_path / "ov_out.pdf"
    assert ToolRegistry.get("overlay_pdf").run([src, wm], ov_out, {}).success

    ann_out = tmp_path / "ann_out.pdf"
    assert ToolRegistry.get("annotate_pdf").run(
        [src],
        ann_out,
        {"annotations": [{"kind": "highlight", "page_index": 0, "rect": [72, 650, 220, 670], "color": "#FEF08A"}]},
    ).success
    with pikepdf.open(ann_out) as pdf:
        annots = pdf.pages[0].get("/Annots", [])
        assert len(annots) >= 1


def test_fa07_t1_07_crop_resize_and_make_fillable(tmp_path: Path) -> None:
    ToolRegistry = load_attr("pdf_lector.core.tools", "ToolRegistry")
    pikepdf = load_attr("pikepdf")
    src = make_multipage_toc_pdf(tmp_path / "src.pdf", pages=2)

    crop_out = tmp_path / "crop.pdf"
    assert ToolRegistry.get("crop_pdf").run([src], crop_out, {"margin": 24}).success
    with pikepdf.open(crop_out) as pdf:
        mb = [float(v) for v in pdf.pages[0].MediaBox]
        cb = [float(v) for v in pdf.pages[0].CropBox]
        assert (cb[2] - cb[0]) < (mb[2] - mb[0])

    a4_out = tmp_path / "a4.pdf"
    assert ToolRegistry.get("resize_pages").run([src], a4_out, {"preset": "A4"}).success
    with pikepdf.open(a4_out) as pdf:
        mb = [float(v) for v in pdf.pages[0].MediaBox]
        assert abs((mb[2] - mb[0]) - 595.28) <= 3.0
        assert abs((mb[3] - mb[1]) - 841.89) <= 3.0

    fill_out = tmp_path / "fillable.pdf"
    assert ToolRegistry.get("make_fillable").run(
        [src],
        fill_out,
        {
            "fields": [
                {"name": "Nombre", "kind": "text", "page": 1, "rect": [72, 600, 260, 624]},
                {"name": "Acepto", "kind": "checkbox", "page": 1, "rect": [72, 560, 90, 578]},
            ]
        },
    ).success
    with pikepdf.open(fill_out) as pdf:
        assert "/AcroForm" in pdf.Root


# ---------------------------------------------------------------------------
# FA-08: Convert, OCR, Create & Capture Suite (8 tools)
# ---------------------------------------------------------------------------

def test_fa08_t1_01_convert_to_pdf_md_docx_xlsx_html_txt(tmp_path: Path) -> None:
    ToolRegistry = load_attr("pdf_lector.core.tools", "ToolRegistry")
    gfm = make_gfm_markdown_bundle(tmp_path / "md")
    office = make_office_inputs(tmp_path / "office")

    for key, src_path in [
        ("md", gfm["valid_md"]),
        ("docx", office["docx"]),
        ("xlsx", office["xlsx"]),
        ("html", office["html"]),
        ("txt", office["txt"]),
    ]:
        out_pdf = tmp_path / f"{key}.pdf"
        res = ToolRegistry.get("convert_to_pdf").run([src_path], out_pdf, {"dpi": 150, "source_lang": "es"})
        assert res.success is True, f"convert_to_pdf failed for {key}: {res.summary}"
        doc = pdfium.PdfDocument(str(out_pdf))
        try:
            assert len(doc) >= 1
            if key == "md":
                assert len(list(doc.get_toc())) >= 2
        finally:
            doc.close()


def test_fa08_t1_02_convert_from_pdf_md_docx_xlsx_html_txt(tmp_path: Path) -> None:
    ToolRegistry = load_attr("pdf_lector.core.tools", "ToolRegistry")
    src = make_multipage_toc_pdf(tmp_path / "src.pdf", pages=3)

    for fmt in ("md", "docx", "xlsx", "html", "txt"):
        out_file = tmp_path / f"export.{fmt}"
        res = ToolRegistry.get("convert_from_pdf").run([src], out_file, {"target_format": fmt, "source_lang": "es"})
        assert res.success is True
        assert out_file.exists() and out_file.stat().st_size > 0


def test_fa08_t1_03_images_to_pdf_and_pdf_to_images_round_trip(tmp_path: Path) -> None:
    ToolRegistry = load_attr("pdf_lector.core.tools", "ToolRegistry")
    imgs = make_sample_images(tmp_path / "imgs")
    pdf_out = tmp_path / "from_imgs.pdf"
    res1 = ToolRegistry.get("images_to_pdf").run([imgs["png"], imgs["jpg"]], pdf_out, {"dpi": 150})
    assert res1.success is True

    out_dir = tmp_path / "exported_imgs"
    out_dir.mkdir()
    res2 = ToolRegistry.get("pdf_to_images").run([pdf_out], out_dir, {"dpi": 150, "format": "png"})
    assert res2.success is True
    assert len(res2.output_paths) == 2


def test_fa08_t1_04_pdf_ocr_adds_searchable_text_layer(tmp_path: Path) -> None:
    ToolRegistry = load_attr("pdf_lector.core.tools", "ToolRegistry")
    scan_pdf = make_scanned_text_pdf(tmp_path / "scan.pdf", text="FACTURA ELECTRONICA NIT 900123456")
    out_pdf = tmp_path / "ocr.pdf"
    res = ToolRegistry.get("pdf_ocr").run(
        [scan_pdf],
        out_pdf,
        {"dpi": 300, "source_lang": "es", "ocr_provider": lambda _img, _lang: "FACTURA ELECTRONICA NIT 900123456"},
    )
    assert res.success is True
    doc = pdfium.PdfDocument(str(out_pdf))
    try:
        tp = doc[0].get_textpage()
        try:
            assert "900123456" in tp.get_text_range()
        finally:
            tp.close()
    finally:
        doc.close()


def test_fa08_t1_05_job_application_capture_screen_and_scanner(tmp_path: Path, qapp: QApplication) -> None:
    del qapp
    ToolRegistry = load_attr("pdf_lector.core.tools", "ToolRegistry")
    cv = make_multipage_toc_pdf(tmp_path / "cv.pdf", pages=1)
    cert = make_multipage_toc_pdf(tmp_path / "cert.pdf", pages=1)

    dossier = tmp_path / "dossier.pdf"
    assert ToolRegistry.get("job_application_pdf").run([cv, cert], dossier, {"title": "Postulacion QA"}).success
    d = pdfium.PdfDocument(str(dossier))
    try:
        assert len(d) == 2
    finally:
        d.close()

    pm = QPixmap(400, 300)
    pm.fill(QColor("#0EA5E9"))
    cap_out = tmp_path / "capture.pdf"
    res_cap = ToolRegistry.get("capture_screen").run(
        [],
        cap_out,
        {"dpi": 150, "region": (0, 0, 400, 300), "screen_grab_fn": lambda _r: pm},
    )
    assert res_cap.success is True and cap_out.exists()

    scan_img = Image.new("RGB", (600, 800), "#FFFFFF")
    scan_out = tmp_path / "wia.pdf"
    res_scan = ToolRegistry.get("import_scanner_camera").run(
        [],
        scan_out,
        {"dpi": 200, "source_lang": "es", "device_acquire_fn": lambda: scan_img},
    )
    assert res_scan.success is True and scan_out.exists()


# ---------------------------------------------------------------------------
# FA-09: Dual-Profile Invoices (DIAN UBL 2.1 & Factur-X EN 16931)
# ---------------------------------------------------------------------------

def test_fa09_t1_01_create_visual_invoice_pdf(tmp_path: Path) -> None:
    ToolRegistry = load_attr("pdf_lector.core.tools", "ToolRegistry")
    out_pdf = tmp_path / "visual_inv.pdf"
    res = ToolRegistry.get("create_visual_invoice").run(
        [],
        out_pdf,
        {
            "emitter": "TRODAI S.A.S.",
            "nit": "900123456",
            "customer": "Cliente Demo",
            "items": [{"description": "Licencia", "qty": 1, "unit_price": 100000, "tax_rate": 0.19}],
        },
    )
    assert res.success is True
    doc = pdfium.PdfDocument(str(out_pdf))
    try:
        tp = doc[0].get_textpage()
        try:
            txt = tp.get_text_range()
            assert "TRODAI" in txt and "900123456" in txt
        finally:
            tp.close()
    finally:
        doc.close()


def test_fa09_t1_02_create_einvoice_dian_ubl21_profile(tmp_path: Path) -> None:
    ToolRegistry = load_attr("pdf_lector.core.tools", "ToolRegistry")
    out_dir = tmp_path / "dian"
    out_dir.mkdir()
    res = ToolRegistry.get("create_einvoice").run(
        [],
        out_dir,
        {
            "profile": "dian_ubl21",
            "invoice_number": "SETP990000001",
            "emitter_nit": "900123456",
            "emitter_name": "TRODAI ANALITICA S.A.S.",
            "customer_nit": "800654321",
            "customer_name": "CLIENTE PRUEBA",
            "items": [{"description": "Consultoria", "qty": 1, "unit_price": 100000.0, "iva_rate": 0.19}],
        },
    )
    assert res.success is True
    xml_paths = [p for p in res.output_paths if p.suffix.lower() == ".xml"]
    assert xml_paths
    xml_txt = xml_paths[0].read_text(encoding="utf-8")
    assert "UBL 2.1" in xml_txt and "900123456" in xml_txt
    assert len(res.details.get("cufe", "")) == 96


def test_fa09_t1_03_create_einvoice_facturx_en16931_pdfa3(tmp_path: Path) -> None:
    ToolRegistry = load_attr("pdf_lector.core.tools", "ToolRegistry")
    pikepdf = load_attr("pikepdf")
    out_pdf = tmp_path / "facturx_out.pdf"
    res = ToolRegistry.get("create_einvoice").run(
        [],
        out_pdf,
        {
            "profile": "facturx_en16931",
            "invoice_number": "FX-2026-001",
            "emitter_name": "TRODAI Europe GmbH",
            "emitter_vat": "DE123456789",
            "customer_name": "Example Client SAS",
            "currency": "EUR",
            "items": [{"description": "License", "qty": 1, "unit_price": 100.0, "vat_rate": 0.19}],
        },
    )
    assert res.success is True
    with pikepdf.open(out_pdf) as pdf:
        assert "factur-x.xml" in list(pdf.attachments.keys())


def test_fa09_t1_04_validate_einvoice_passes_dian_and_facturx(tmp_path: Path) -> None:
    ToolRegistry = load_attr("pdf_lector.core.tools", "ToolRegistry")
    dian_xml = make_invoice_xml(tmp_path / "dian_ok.xml", profile="dian", valid=True)
    fx_pdf = make_facturx_pdfa3(tmp_path / "fx_ok.pdf", valid=True)

    r1 = ToolRegistry.get("validate_einvoice").run([dian_xml], tmp_path / "r1.json", {})
    r2 = ToolRegistry.get("validate_einvoice").run([fx_pdf], tmp_path / "r2.json", {})
    assert r1.success is True and r1.details.get("valid") is True
    assert r2.success is True and r2.details.get("valid") is True


def test_fa09_t1_05_pdf_to_einvoice_local_and_consented_online(tmp_path: Path, local_mock_server) -> None:
    ToolRegistry = load_attr("pdf_lector.core.tools", "ToolRegistry")
    inv_pdf = make_invoice_pdf(tmp_path / "inv.pdf", profile="dian")

    out_local = tmp_path / "extracted.xml"
    r_local = ToolRegistry.get("pdf_to_einvoice").run(
        [inv_pdf],
        out_local,
        {"mode": "local", "profile": "dian_ubl21", "dpi": 200, "source_lang": "es"},
    )
    assert r_local.success is True and out_local.exists()

    local_mock_server.set_route("POST", "/einvoice/extract", 200, {"status": "accepted", "cufe": "a" * 96})
    out_online = tmp_path / "extracted_online.xml"
    r_on = ToolRegistry.get("pdf_to_einvoice").run(
        [inv_pdf],
        out_online,
        {
            "mode": "online",
            "consent_granted": True,
            "endpoint_url": f"{local_mock_server.base_url}/einvoice/extract",
            "dpi": 200,
            "source_lang": "es",
        },
    )
    assert r_on.success is True
    assert len(local_mock_server.requests_log) == 1


# ---------------------------------------------------------------------------
# FA-10: Optimize, Repair, Security, Privacy & View/Check Suite (12 tools)
# ---------------------------------------------------------------------------

def test_fa10_t1_01_compress_and_optimize_web(tmp_path: Path) -> None:
    ToolRegistry = load_attr("pdf_lector.core.tools", "ToolRegistry")
    pikepdf = load_attr("pikepdf")
    src = make_image_pdf(tmp_path / "img.pdf", size=(512, 512))

    comp = tmp_path / "comp.pdf"
    assert ToolRegistry.get("compress_pdf").run([src], comp, {"dpi": 150, "quality": 60}).success

    web = tmp_path / "web.pdf"
    assert ToolRegistry.get("optimize_web").run([src], web, {"dpi": 150}).success
    with pikepdf.open(web) as pdf:
        assert pdf.is_linearized is True


def test_fa10_t1_02_flatten_pdf_bakes_acroform(tmp_path: Path) -> None:
    ToolRegistry = load_attr("pdf_lector.core.tools", "ToolRegistry")
    pikepdf = load_attr("pikepdf")
    acro = make_acroform_pdf(tmp_path / "acro.pdf")
    flat = tmp_path / "flat.pdf"
    assert ToolRegistry.get("flatten_pdf").run([acro], flat, {"dpi": 150}).success
    with pikepdf.open(flat) as pdf:
        assert "/AcroForm" not in pdf.Root


def test_fa10_t1_03_repair_pdf_and_check_pdfa(tmp_path: Path) -> None:
    ToolRegistry = load_attr("pdf_lector.core.tools", "ToolRegistry")
    bad_xref = make_repairable_bad_xref_pdf(tmp_path / "bad_xref.pdf")
    repaired = tmp_path / "repaired.pdf"
    assert ToolRegistry.get("repair_pdf").run([bad_xref], repaired, {}).success
    doc = pdfium.PdfDocument(str(repaired))
    try:
        assert len(doc) == 1
    finally:
        doc.close()

    fx_ok = make_facturx_pdfa3(tmp_path / "fx.pdf", valid=True)
    plain = make_multipage_toc_pdf(tmp_path / "plain.pdf", pages=1)
    r_ok = ToolRegistry.get("check_pdfa").run([fx_ok], tmp_path / "a.json", {})
    r_no = ToolRegistry.get("check_pdfa").run([plain], tmp_path / "b.json", {})
    assert r_ok.success is True and r_ok.details.get("conformant") is True
    assert r_no.details.get("conformant") is False


def test_fa10_t1_04_protect_and_unlock_pdf_aes256(tmp_path: Path) -> None:
    ToolRegistry = load_attr("pdf_lector.core.tools", "ToolRegistry")
    pikepdf = load_attr("pikepdf")
    src = make_multipage_toc_pdf(tmp_path / "src.pdf", pages=2)
    locked = tmp_path / "locked.pdf"
    assert ToolRegistry.get("protect_pdf").run(
        [src], locked, {"user_password": "u123", "owner_password": "o456", "allow_print": False}
    ).success
    with pytest.raises(pikepdf.PasswordError):
        pikepdf.open(locked)

    unlocked = tmp_path / "unlocked.pdf"
    assert ToolRegistry.get("unlock_pdf").run([locked], unlocked, {"password": "o456"}).success
    with pikepdf.open(unlocked) as pdf:
        assert len(pdf.pages) == 2


def test_fa10_t1_05_sign_pdf_pkcs12(tmp_path: Path) -> None:
    ToolRegistry = load_attr("pdf_lector.core.tools", "ToolRegistry")
    pikepdf = load_attr("pikepdf")
    src = make_multipage_toc_pdf(tmp_path / "src.pdf", pages=1)
    p12 = make_self_signed_pkcs12(tmp_path / "signer.p12", password="testpass")
    signed = tmp_path / "signed.pdf"
    res = ToolRegistry.get("sign_pdf").run(
        [src],
        signed,
        {"p12_path": str(p12), "password": "testpass", "reason": "QA Approval"},
    )
    assert res.success is True
    with pikepdf.open(signed) as pdf:
        assert "/ByteRange" in signed.read_bytes().decode("latin-1", errors="ignore") or "/AcroForm" in pdf.Root


def test_fa10_t1_06_redact_pdf_destructive_removal(tmp_path: Path) -> None:
    ToolRegistry = load_attr("pdf_lector.core.tools", "ToolRegistry")
    pypdf = load_attr("pypdf")
    secret = "SECRET_TOKEN_XYZ"
    src = make_redaction_target_pdf(tmp_path / "redact_src.pdf", secret=secret)
    out_pdf = tmp_path / "redacted.pdf"
    res = ToolRegistry.get("redact_pdf").run(
        [src],
        out_pdf,
        {"targets": [{"page_index": 0, "rect": [60, 620, 420, 675]}], "search_terms": [secret]},
    )
    assert res.success is True
    doc = pdfium.PdfDocument(str(out_pdf))
    try:
        for i in range(len(doc)):
            tp = doc[i].get_textpage()
            try:
                assert secret not in tp.get_text_range()
            finally:
                tp.close()
    finally:
        doc.close()

    reader = pypdf.PdfReader(str(out_pdf))
    for page in reader.pages:
        assert secret not in (page.extract_text() or "")
    assert secret.encode("ascii") not in out_pdf.read_bytes()


def test_fa10_t1_07_strip_metadata_removes_info_xmp_and_pieceinfo(tmp_path: Path) -> None:
    ToolRegistry = load_attr("pdf_lector.core.tools", "ToolRegistry")
    pikepdf = load_attr("pikepdf")
    src = make_metadata_dirty_pdf(tmp_path / "dirty_meta.pdf")
    clean = tmp_path / "clean_meta.pdf"
    assert ToolRegistry.get("strip_metadata").run([src], clean, {}).success
    with pikepdf.open(clean) as pdf:
        assert "/Metadata" not in pdf.Root
        assert "/PieceInfo" not in pdf.Root
        assert str(pdf.docinfo.get("/Author", "")) == ""
        assert str(pdf.docinfo.get("/Title", "")) == ""


def test_fa10_t1_08_compare_pdf_and_viewer_preferences(tmp_path: Path, isolated_appdata: Path) -> None:
    del isolated_appdata
    ToolRegistry = load_attr("pdf_lector.core.tools", "ToolRegistry")
    p1 = make_multipage_toc_pdf(tmp_path / "a.pdf", pages=2, title="Doc A")
    p2 = make_multipage_toc_pdf(tmp_path / "b.pdf", pages=3, title="Doc B")

    same = ToolRegistry.get("compare_pdf").run([p1, p1], tmp_path / "same.json", {})
    diff = ToolRegistry.get("compare_pdf").run([p1, p2], tmp_path / "diff.json", {})
    assert same.success is True and same.details.get("has_differences") is False
    assert diff.success is True and diff.details.get("has_differences") is True

    prefs = ToolRegistry.get("viewer_preferences").run(
        [], tmp_path / "prefs.json", {"default_zoom": 1.25, "default_rail_mode": "both"}
    )
    assert prefs.success is True


# ---------------------------------------------------------------------------
# FA-11: Markdown Engine, md_security & MD <-> PDF
# ---------------------------------------------------------------------------

def test_fa11_t1_01_gfm_full_render_to_safe_html(tmp_path: Path) -> None:
    md_eng = load_attr("pdf_lector.core.markdown_engine")
    gfm = make_gfm_markdown_bundle(tmp_path / "md")
    html, blocked = md_eng.render_markdown_to_safe_html(
        gfm["valid_md"].read_text(encoding="utf-8"),
        base_dir=gfm["valid_md"].parent,
    )
    assert "<table>" in html
    assert "<code" in html
    assert "<blockquote>" in html
    assert blocked == []


def test_fa11_t1_02_parse_markdown_toc_headings(tmp_path: Path) -> None:
    md_eng = load_attr("pdf_lector.core.markdown_engine")
    gfm = make_gfm_markdown_bundle(tmp_path / "md")
    toc = md_eng.parse_markdown_toc(gfm["valid_md"].read_text(encoding="utf-8"))
    assert len(toc) >= 4
    assert toc[0].level == 1
    assert any(item.level == 2 for item in toc)
    assert any(item.level == 3 for item in toc)
    assert all(item.anchor for item in toc)


def test_fa11_t1_03_safe_local_image_resolution_in_markdown(tmp_path: Path) -> None:
    md_eng = load_attr("pdf_lector.core.markdown_engine")
    gfm = make_gfm_markdown_bundle(tmp_path / "md")
    html, blocked = md_eng.render_markdown_to_safe_html(
        "![Diagrama](./local_diagram.png)",
        base_dir=gfm["valid_md"].parent,
    )
    assert blocked == []
    assert "local_diagram.png" in html


def test_fa11_t1_04_native_md_to_pdf_with_toc_bookmarks(tmp_path: Path) -> None:
    md_eng = load_attr("pdf_lector.core.markdown_engine")
    gfm = make_gfm_markdown_bundle(tmp_path / "md")
    out_pdf = tmp_path / "md_out.pdf"
    res_path = md_eng.markdown_to_pdf(gfm["valid_md"], out_pdf, dpi=150, source_lang="es", theme="light")
    assert res_path.exists()
    doc = pdfium.PdfDocument(str(res_path))
    try:
        assert len(doc) >= 1
        assert len(list(doc.get_toc())) >= 3
        tp = doc[0].get_textpage()
        try:
            assert "Titulo Principal" in tp.get_text_range()
        finally:
            tp.close()
    finally:
        doc.close()


def test_fa11_t1_05_native_pdf_to_markdown_round_trip(tmp_path: Path) -> None:
    md_eng = load_attr("pdf_lector.core.markdown_engine")
    pdf_in = make_multipage_toc_pdf(tmp_path / "src.pdf", pages=2)
    out_md = tmp_path / "out.md"
    res_path = md_eng.pdf_to_markdown(pdf_in, out_md, dpi=150, source_lang="es")
    assert res_path.exists()
    text = res_path.read_text(encoding="utf-8")
    assert "Privacidad" in text
    assert len(md_eng.parse_markdown_toc(text)) >= 1


def test_fa11_t1_06_markdown_extension_and_dark_theme_pdf(tmp_path: Path) -> None:
    md_eng = load_attr("pdf_lector.core.markdown_engine")
    gfm = make_gfm_markdown_bundle(tmp_path / "md")
    out_pdf = tmp_path / "dark_md.pdf"
    res = md_eng.markdown_to_pdf(gfm["valid_markdown"], out_pdf, dpi=150, source_lang="es", theme="dark")
    assert res.exists() and res.stat().st_size > 0


# ---------------------------------------------------------------------------
# FA-12: PDFium Text-Quad / Word Hit-Testing
# ---------------------------------------------------------------------------

def test_fa12_t1_01_word_hit_test_at_known_coordinates(tmp_path: Path) -> None:
    PdfDocumentSession = load_attr("pdf_lector.core.pdf_engine", "PdfDocumentSession")
    pdf_path = make_multipage_toc_pdf(tmp_path / "spec.pdf", pages=2)
    session = PdfDocumentSession(pdf_path)
    try:
        boxes = session.get_page_word_boxes(0)
        target = next(b for b in boxes if "privacidad" in b.word.lower())
        cx = (target.rect_pdf[0] + target.rect_pdf[2]) / 2.0
        cy = (target.rect_pdf[1] + target.rect_pdf[3]) / 2.0
        hit = session.hit_test_word(0, cx, cy)
        assert hit is not None and "privacidad" in hit.word.lower()
    finally:
        session.close()


def test_fa12_t1_02_selection_rect_text_and_quads(tmp_path: Path) -> None:
    PdfDocumentSession = load_attr("pdf_lector.core.pdf_engine", "PdfDocumentSession")
    pdf_path = make_multipage_toc_pdf(tmp_path / "spec.pdf", pages=2)
    session = PdfDocumentSession(pdf_path)
    try:
        boxes = session.get_page_word_boxes(0)
        first = boxes[0]
        last = boxes[min(4, len(boxes) - 1)]
        sel_text, quads = session.select_text_in_rect(
            0,
            (first.rect_pdf[0], first.rect_pdf[1], last.rect_pdf[2], last.rect_pdf[3]),
        )
        assert len(sel_text.strip()) > 0
        assert len(quads) >= 1
    finally:
        session.close()


def test_fa12_t1_03_multiline_selection_span(tmp_path: Path) -> None:
    PdfDocumentSession = load_attr("pdf_lector.core.pdf_engine", "PdfDocumentSession")
    pdf_path = make_multipage_toc_pdf(tmp_path / "spec.pdf", pages=2)
    session = PdfDocumentSession(pdf_path)
    try:
        text, quads = session.select_text_in_rect(0, (40.0, 620.0, 560.0, 750.0))
        assert "Lectura" in text
        assert len(quads) >= 1
    finally:
        session.close()


def test_fa12_t1_04_rotated_page_coordinate_mapping(tmp_path: Path) -> None:
    PdfDocumentSession = load_attr("pdf_lector.core.pdf_engine", "PdfDocumentSession")
    pdf_path = make_multipage_toc_pdf(tmp_path / "spec.pdf", pages=2)
    session = PdfDocumentSession(pdf_path)
    try:
        boxes = session.get_page_word_boxes(0)
        target = boxes[0]
        cx = (target.rect_pdf[0] + target.rect_pdf[2]) / 2.0
        cy = (target.rect_pdf[1] + target.rect_pdf[3]) / 2.0
        hit = session.hit_test_word(0, cx, cy, rotation=90)
        assert hit is not None
    finally:
        session.close()


def test_fa12_t1_05_accented_spanish_characters_preserved(tmp_path: Path) -> None:
    PdfDocumentSession = load_attr("pdf_lector.core.pdf_engine", "PdfDocumentSession")
    pdf_path = make_multipage_toc_pdf(tmp_path / "spec.pdf", pages=2)
    session = PdfDocumentSession(pdf_path)
    try:
        boxes = session.get_page_word_boxes(0)
        words = [b.word for b in boxes]
        assert any("P\u00e1gina" in w or "Electr\u00f3nica" in w for w in words)
    finally:
        session.close()


# ---------------------------------------------------------------------------
# FA-13: ISO 32000 Annotations & Ink Persistence
# ---------------------------------------------------------------------------

def test_fa13_t1_01_6_color_highlight_iso32000_round_trip(tmp_path: Path) -> None:
    pa = load_attr("pdf_lector.core.pdf_annotations")
    pikepdf = load_attr("pikepdf")
    pdf_path = make_multipage_toc_pdf(tmp_path / "ann.pdf", pages=2)
    palette = ["#FEF08A", "#BBF7D0", "#BAE6FD", "#FBCFE8", "#FED7AA", "#DDD6FE"]
    records = [
        pa.AnnotationRecord(
            id=f"hl-{i}",
            page_index=0,
            kind="highlight",
            rect_pdf=(72.0, 680.0 - i * 18.0, 220.0, 694.0 - i * 18.0),
            points_pdf=(),
            color_hex=col,
            author="An\u00f3nimo",
            timestamp_iso="2026-11-24T12:00:00Z",
            content="",
            parent_id=None,
        )
        for i, col in enumerate(palette)
    ]
    saved = pa.save_pdf_annotations(pdf_path, records, create_backup=False)
    loaded = pa.load_pdf_annotations(saved)
    assert len([r for r in loaded if r.kind == "highlight"]) == 6
    with pikepdf.open(saved) as pdf:
        subtypes = [str(a.get("/Subtype")) for a in pdf.pages[0].get("/Annots", [])]
        assert subtypes.count("/Highlight") == 6


def test_fa13_t1_02_sticky_note_freetext_popup_threaded_comments(tmp_path: Path) -> None:
    pa = load_attr("pdf_lector.core.pdf_annotations")
    pikepdf = load_attr("pikepdf")
    pdf_path = make_multipage_toc_pdf(tmp_path / "notes.pdf", pages=2)
    records = [
        pa.AnnotationRecord(
            id="note-1",
            page_index=0,
            kind="text",
            rect_pdf=(100.0, 500.0, 124.0, 524.0),
            points_pdf=(),
            color_hex="#FEF08A",
            author="Revisor QA",
            timestamp_iso="2026-11-24T12:01:00Z",
            content="Revisar clausula",
            parent_id=None,
        ),
        pa.AnnotationRecord(
            id="reply-1",
            page_index=0,
            kind="popup",
            rect_pdf=(130.0, 480.0, 280.0, 540.0),
            points_pdf=(),
            color_hex="#FEF08A",
            author="An\u00f3nimo",
            timestamp_iso="2026-11-24T12:02:00Z",
            content="Aprobado",
            parent_id="note-1",
        ),
    ]
    saved = pa.save_pdf_annotations(pdf_path, records, create_backup=False)
    loaded = pa.load_pdf_annotations(saved)
    assert any(r.content == "Revisar clausula" for r in loaded)
    assert any(r.parent_id == "note-1" and r.content == "Aprobado" for r in loaded)
    with pikepdf.open(saved) as pdf:
        annots = pdf.pages[0].get("/Annots", [])
        assert any(str(a.get("/Subtype")) == "/Text" for a in annots)


def test_fa13_t1_03_ink_vector_strokes_persistence(tmp_path: Path) -> None:
    pa = load_attr("pdf_lector.core.pdf_annotations")
    pikepdf = load_attr("pikepdf")
    pdf_path = make_multipage_toc_pdf(tmp_path / "ink.pdf", pages=1)
    rec = pa.AnnotationRecord(
        id="ink-1",
        page_index=0,
        kind="ink",
        rect_pdf=(80.0, 400.0, 200.0, 450.0),
        points_pdf=[[(80.0, 410.0), (140.0, 440.0), (200.0, 420.0)]],
        color_hex="#0EA5E9",
        author="An\u00f3nimo",
        timestamp_iso="2026-11-24T12:05:00Z",
        content="",
        parent_id=None,
    )
    saved = pa.save_pdf_annotations(pdf_path, [rec], create_backup=False)
    with pikepdf.open(saved) as pdf:
        ink_annots = [a for a in pdf.pages[0].get("/Annots", []) if str(a.get("/Subtype")) == "/Ink"]
        assert len(ink_annots) == 1
        assert "/InkList" in ink_annots[0]
    loaded = pa.load_pdf_annotations(saved)
    assert any(r.kind == "ink" and len(r.points_pdf) >= 1 for r in loaded)


def test_fa13_t1_04_default_anonymous_author_never_exposes_os_user(tmp_path: Path) -> None:
    pa = load_attr("pdf_lector.core.pdf_annotations")
    pikepdf = load_attr("pikepdf")
    pdf_path = make_multipage_toc_pdf(tmp_path / "anon.pdf", pages=1)
    rec = pa.AnnotationRecord(
        id="anon-1",
        page_index=0,
        kind="text",
        rect_pdf=(72.0, 500.0, 96.0, 524.0),
        points_pdf=(),
        color_hex="#FEF08A",
        author="",
        timestamp_iso="2026-11-24T12:00:00Z",
        content="Nota privada",
        parent_id=None,
    )
    saved = pa.save_pdf_annotations(pdf_path, [rec], create_backup=False)
    os_user = os.environ.get("USERNAME", "")
    with pikepdf.open(saved) as pdf:
        t_val = str(pdf.pages[0]["/Annots"][0].get("/T", ""))
        assert t_val == "An\u00f3nimo"
        if os_user:
            assert t_val != os_user


def test_fa13_t1_05_backup_save_and_save_as_with_flatten(tmp_path: Path) -> None:
    pa = load_attr("pdf_lector.core.pdf_annotations")
    pikepdf = load_attr("pikepdf")
    pdf_path = make_multipage_toc_pdf(tmp_path / "doc.pdf", pages=1)
    orig_hash = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
    rec = pa.AnnotationRecord(
        id="hl-bak",
        page_index=0,
        kind="highlight",
        rect_pdf=(72.0, 680.0, 220.0, 695.0),
        points_pdf=(),
        color_hex="#FEF08A",
        author="An\u00f3nimo",
        timestamp_iso="2026-11-24T12:00:00Z",
        content="",
        parent_id=None,
    )
    pa.save_pdf_annotations(pdf_path, [rec], create_backup=True, flatten=False)
    bak_files = list(tmp_path.glob("doc*.bak*"))
    assert len(bak_files) >= 1
    assert hashlib.sha256(bak_files[0].read_bytes()).hexdigest() == orig_hash

    flat_out = tmp_path / "doc_flat.pdf"
    pa.save_pdf_annotations(pdf_path, [rec], output_path=flat_out, create_backup=False, flatten=True)
    with pikepdf.open(flat_out) as pdf:
        assert len(pdf.pages[0].get("/Annots", [])) == 0


# ---------------------------------------------------------------------------
# FA-14: Interactive PDF Canvas & Touch/Stylus Ink
# ---------------------------------------------------------------------------

def test_fa14_t1_01_canvas_highlight_and_sticky_note_placement(tmp_path: Path, qapp: QApplication) -> None:
    PdfCanvas = load_attr("pdf_lector.ui.pdf_canvas", "PdfCanvas")
    PdfDocumentSession = load_attr("pdf_lector.core.pdf_engine", "PdfDocumentSession")
    session = PdfDocumentSession(make_multipage_toc_pdf(tmp_path / "c.pdf", pages=1))
    canvas_w = PdfCanvas()
    try:
        canvas_w.set_session(session, page_index=0)
        canvas_w.resize(620, 800)
        canvas_w.show()
        qapp.processEvents()
        canvas_w.add_highlight_rect((72.0, 650.0, 240.0, 668.0), color_hex="#BAE6FD")
        canvas_w.add_sticky_note((120.0, 500.0), "Nota en canvas")
        assert len(canvas_w.annotations()) == 2
    finally:
        canvas_w.close()
        session.close()


def test_fa14_t1_02_threaded_reply_and_author_toggle(tmp_path: Path, qapp: QApplication) -> None:
    PdfCanvas = load_attr("pdf_lector.ui.pdf_canvas", "PdfCanvas")
    PdfDocumentSession = load_attr("pdf_lector.core.pdf_engine", "PdfDocumentSession")
    session = PdfDocumentSession(make_multipage_toc_pdf(tmp_path / "c2.pdf", pages=1))
    canvas_w = PdfCanvas()
    try:
        canvas_w.set_session(session, page_index=0)
        canvas_w.set_author("Autor Local")
        parent = canvas_w.add_sticky_note((100.0, 480.0), "Comentario raiz")
        reply = canvas_w.add_reply(parent.id, "Respuesta en hilo")
        assert reply.parent_id == parent.id
        assert parent.author == "Autor Local"
        canvas_w.set_anonymous_author(True)
        anon_note = canvas_w.add_sticky_note((150.0, 460.0), "Comentario anonimo")
        assert anon_note.author == "An\u00f3nimo"
        qapp.processEvents()
    finally:
        canvas_w.close()
        session.close()


def test_fa14_t1_03_stylus_ink_tablet_event_with_pressure(tmp_path: Path, qapp: QApplication) -> None:
    PdfCanvas = load_attr("pdf_lector.ui.pdf_canvas", "PdfCanvas")
    PdfDocumentSession = load_attr("pdf_lector.core.pdf_engine", "PdfDocumentSession")
    session = PdfDocumentSession(make_multipage_toc_pdf(tmp_path / "c3.pdf", pages=1))
    canvas_w = PdfCanvas()
    try:
        canvas_w.set_session(session, page_index=0)
        canvas_w.set_tool_mode("ink")
        canvas_w.resize(620, 800)
        canvas_w.show()
        qapp.processEvents()
        canvas_w.record_ink_stroke([(100.0, 300.0, 0.3), (150.0, 320.0, 0.8), (210.0, 310.0, 0.5)])
        inks = [a for a in canvas_w.annotations() if a.kind == "ink"]
        assert len(inks) == 1
        assert len(inks[0].points_pdf[0]) == 3
    finally:
        canvas_w.close()
        session.close()


def test_fa14_t1_04_touch_ink_mode_toggle_and_pan_zoom_separation(tmp_path: Path, qapp: QApplication) -> None:
    PdfCanvas = load_attr("pdf_lector.ui.pdf_canvas", "PdfCanvas")
    PdfDocumentSession = load_attr("pdf_lector.core.pdf_engine", "PdfDocumentSession")
    session = PdfDocumentSession(make_multipage_toc_pdf(tmp_path / "c4.pdf", pages=1))
    canvas_w = PdfCanvas()
    try:
        canvas_w.set_session(session, page_index=0)
        canvas_w.set_touch_writing_enabled(True)
        assert canvas_w.touch_writing_enabled is True
        canvas_w.handle_touch_points([(QPointF(80.0, 200.0), 1), (QPointF(140.0, 240.0), 1)], phase="draw")
        assert len([a for a in canvas_w.annotations() if a.kind == "ink"]) == 1
        qapp.processEvents()
    finally:
        canvas_w.close()
        session.close()


def test_fa14_t1_05_eraser_undo_and_dictionary_signal(tmp_path: Path, qapp: QApplication) -> None:
    PdfCanvas = load_attr("pdf_lector.ui.pdf_canvas", "PdfCanvas")
    PdfDocumentSession = load_attr("pdf_lector.core.pdf_engine", "PdfDocumentSession")
    session = PdfDocumentSession(make_multipage_toc_pdf(tmp_path / "c5.pdf", pages=1))
    canvas_w = PdfCanvas()
    seen_words: list[str] = []
    try:
        canvas_w.set_session(session, page_index=0)
        canvas_w.lookup_word_requested.connect(seen_words.append)
        canvas_w.add_highlight_rect((72.0, 650.0, 180.0, 668.0), color_hex="#FEF08A")
        assert len(canvas_w.annotations()) == 1
        canvas_w.undo_last_annotation()
        assert len(canvas_w.annotations()) == 0
        canvas_w.request_dictionary_lookup("privacidad")
        assert seen_words == ["privacidad"]
        qapp.processEvents()
    finally:
        canvas_w.close()
        session.close()


# ---------------------------------------------------------------------------
# FA-15: Mixed PDF+MD Viewer & 3-Mode Markdown Tab
# ---------------------------------------------------------------------------

def test_fa15_t1_01_mixed_pdf_and_md_tabs_open_simultaneously(tmp_path: Path, make_isolated_window) -> None:
    win = make_isolated_window()
    pdf_p = make_multipage_toc_pdf(tmp_path / "doc.pdf", pages=2)
    md_p = make_gfm_markdown_bundle(tmp_path / "md")["valid_md"]
    assert win.open_document_path(pdf_p) is True
    assert win.open_document_path(md_p) is True
    assert win.viewer_view.tabs.count() >= 2


def test_fa15_t1_02_markdown_3_mode_segmented_control(tmp_path: Path, make_isolated_window, qapp: QApplication) -> None:
    win = make_isolated_window()
    md_p = make_gfm_markdown_bundle(tmp_path / "md")["valid_md"]
    assert win.open_document_path(md_p) is True
    win.show()
    qapp.processEvents()
    md_tab = win.viewer_view.current_tab()

    md_tab.set_view_mode("read")
    qapp.processEvents()
    assert md_tab.preview_browser.isVisible() and not md_tab.editor_widget.isVisible()

    md_tab.set_view_mode("split")
    qapp.processEvents()
    assert md_tab.preview_browser.isVisible() and md_tab.editor_widget.isVisible()

    md_tab.set_view_mode("edit")
    qapp.processEvents()
    assert md_tab.editor_widget.isVisible() and md_tab.format_toolbar.isVisible()


def test_fa15_t1_03_markdown_toolbar_live_preview_and_save(tmp_path: Path, make_isolated_window, qapp: QApplication) -> None:
    win = make_isolated_window()
    md_p = make_gfm_markdown_bundle(tmp_path / "md")["valid_md"]
    win.open_document_path(md_p)
    md_tab = win.viewer_view.current_tab()
    md_tab.set_view_mode("split")
    md_tab.insert_markdown_snippet("\n## Nueva Seccion E2E\n")
    qapp.processEvents()
    assert md_tab.has_unsaved_changes() is True
    assert "Nueva Seccion E2E" in md_tab.preview_browser.toPlainText()
    assert md_tab.save_document() is True
    assert md_tab.has_unsaved_changes() is False
    assert "Nueva Seccion E2E" in md_p.read_text(encoding="utf-8")


def test_fa15_t1_04_markdown_auto_toc_and_persistent_bookmarks(tmp_path: Path, make_isolated_window) -> None:
    win = make_isolated_window()
    md_p = make_gfm_markdown_bundle(tmp_path / "md")["valid_md"]
    win.open_document_path(md_p)
    md_tab = win.viewer_view.current_tab()
    assert md_tab.toc_tree.topLevelItemCount() >= 2
    win.bookmarks_store.add_bookmark(md_p, "Seccion 2", 1)
    assert len(win.bookmarks_store.list_bookmarks(md_p)) == 1


def test_fa15_t1_05_pdf_3_mode_left_rail_switching(tmp_path: Path, make_isolated_window) -> None:
    win = make_isolated_window()
    pdf_p = make_multipage_toc_pdf(tmp_path / "rail.pdf", pages=2)
    win.open_document_path(pdf_p)
    tab = win.viewer_view.current_tab()
    tab.set_rail_mode("thumbs")
    assert tab.thumb_list.isVisibleTo(tab.nav_rail) and not tab.toc_pane.isVisibleTo(tab.nav_rail)
    tab.set_rail_mode("toc")
    assert tab.toc_pane.isVisibleTo(tab.nav_rail) and not tab.thumb_list.isVisibleTo(tab.nav_rail)
    tab.set_rail_mode("both")
    assert tab.toc_pane.isVisibleTo(tab.nav_rail) and tab.thumb_list.isVisibleTo(tab.nav_rail)


# ---------------------------------------------------------------------------
# FA-16: Kindle-Style Offline Dictionary
# ---------------------------------------------------------------------------

def test_fa16_t1_01_dictionary_closed_by_default_on_document_open(tmp_path: Path, make_isolated_window) -> None:
    win = make_isolated_window()
    pdf_p = make_multipage_toc_pdf(tmp_path / "d.pdf", pages=1)
    win.open_document_path(pdf_p)
    assert win.viewer_view.dict_panel.isVisible() is False
    assert win.viewer_view.dict_btn.isChecked() is False


def test_fa16_t1_02_contextual_buscar_en_diccionario_opens_and_looks_up(tmp_path: Path, make_isolated_window, qapp: QApplication) -> None:
    win = make_isolated_window()
    win.open_document_path(make_multipage_toc_pdf(tmp_path / "d.pdf", pages=1))
    win.show()
    win.switch_view(1, immediate=True)
    win.viewer_view.lookup_in_dictionary("p\u00e1gina")
    qapp.processEvents()
    assert win.viewer_view.dict_panel.isVisible() is True
    assert "pagina" in win.viewer_view.dict_panel.res_word_lbl.text().lower() or "p\u00e1gina" in win.viewer_view.dict_panel.res_word_lbl.text().lower()


def test_fa16_t1_03_detach_dock_and_close_dictionary_panel(tmp_path: Path, make_isolated_window, qapp: QApplication) -> None:
    win = make_isolated_window()
    win.open_document_path(make_multipage_toc_pdf(tmp_path / "d.pdf", pages=1))
    win.show()
    win.switch_view(1, immediate=True)
    win.viewer_view.set_dictionary_visible(True)
    qapp.processEvents()

    win.viewer_view.detach_dictionary()
    qapp.processEvents()
    assert win.viewer_view.is_dictionary_detached() is True

    win.viewer_view.dock_dictionary()
    qapp.processEvents()
    assert win.viewer_view.is_dictionary_detached() is False

    win.viewer_view.set_dictionary_visible(False)
    qapp.processEvents()
    assert win.viewer_view.dict_panel.isVisible() is False
    assert win.viewer_view.dict_btn.isChecked() is False


def test_fa16_t1_04_language_manager_mocked_download_and_remove(tmp_path: Path) -> None:
    DictionaryEngine = load_attr("pdf_lector.core.dictionary_engine", "DictionaryEngine")
    eng = DictionaryEngine(base_dir=tmp_path / "dicts")
    pt_payload = json.dumps(
        [{"word": "privacidade", "pos": "subst.", "definition": "Controle local.", "synonyms": ["sigilo"]}]
    ).encode("utf-8")
    assert eng.download_language_pack("pt", fetcher=lambda _u: pt_payload) == 1
    assert "pt" in eng.installed_languages()
    assert eng.lookup("privacidade", "pt")[0].pos == "subst."
    assert eng.remove_language_pack("pt") is True
    assert "pt" not in eng.installed_languages()


def test_fa16_t1_05_local_json_glossary_import_list_and_dict(tmp_path: Path) -> None:
    DictionaryEngine = load_attr("pdf_lector.core.dictionary_engine", "DictionaryEngine")
    eng = DictionaryEngine(base_dir=tmp_path / "dicts")
    fr_json = make_local_glossary_json(tmp_path / "fr.json", fmt="list")
    de_json = make_local_glossary_json(tmp_path / "de.json", fmt="dict")
    assert eng.import_local_glossary("fr", fr_json) == 2
    assert eng.import_local_glossary("de", de_json) == 2
    assert eng.lookup("contrat", "fr")
    assert eng.lookup("datenschutz", "de")


# ---------------------------------------------------------------------------
# FA-17: Polygonal SVGs & WinOptiAI Widgets/Localizer
# ---------------------------------------------------------------------------

def test_fa17_t1_01_new_ui_polygonal_svgs_present_and_valid() -> None:
    icons_dir = WORKSPACE_ROOT / "assets" / "icons"
    curve_re = re.compile(r"[CSQTacsqta]")
    for icon_name in ("bell.svg", "alert.svg", "refresh.svg", "folder.svg", "markdown.svg", "ink.svg", "ai_spark.svg"):
        svg = icons_dir / icon_name
        assert svg.exists(), f"Missing polygonal SVG: {icon_name}"
        root = ET.parse(svg).getroot()
        for elem in root.iter():
            tag = elem.tag.split("}")[-1].lower()
            assert tag not in ("circle", "ellipse")
            if tag == "path":
                assert not curve_re.search(elem.attrib.get("d", ""))


def test_fa17_t1_02_adaptive_button_multi_variant_sizing(qapp: QApplication) -> None:
    AdaptiveButton = load_attr("pdf_lector.ui.widgets", "AdaptiveButton")
    btn = AdaptiveButton()
    btn.set_variants(["Descargar e instalar actualizacion", "Actualizar", "Instalar"])
    assert btn.sizeHint().width() >= btn.minimumSizeHint().width()
    btn.resize(110, 34)
    qapp.processEvents()
    assert btn.text() in ("Actualizar", "Instalar")
    assert "Descargar" in btn.toolTip()


def test_fa17_t1_03_dialog_localizer_es_en_pt_roles(qapp: QApplication) -> None:
    DialogLocalizer = load_attr("pdf_lector.ui.dialog_localizer", "DialogLocalizer")
    loc = DialogLocalizer(qapp)
    loc.set_language("es")
    box = QMessageBox(QMessageBox.Icon.Information, "Titulo", "Mensaje", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
    loc.localize_dialog(box)
    assert box.button(QMessageBox.StandardButton.Yes).text() == "S\u00ed"
    assert box.button(QMessageBox.StandardButton.Yes).property("role") == "PrimaryAction"
    assert box.button(QMessageBox.StandardButton.No).property("role") == "GhostAction"

    loc.set_language("pt")
    loc.localize_dialog(box)
    assert box.button(QMessageBox.StandardButton.Yes).text() == "Sim"


def test_fa17_t1_04_tour_dialog_navigation_and_completion(qapp: QApplication) -> None:
    TourDialog = load_attr("pdf_lector.ui.tour_dialog", "TourDialog")
    steps_seen: list[int] = []
    dlg = TourDialog(lang="es", on_step_change=steps_seen.append)
    try:
        dlg.show()
        qapp.processEvents()
        dlg.next_step()
        dlg.next_step()
        dlg.finish_tour()
        assert len(steps_seen) >= 2
        assert dlg.completed is True
    finally:
        dlg.close()


def test_fa17_t1_05_svg_icon_renderer_tinted_pixmap(qapp: QApplication) -> None:
    del qapp
    load_svg_icon = load_attr("pdf_lector.ui.svg_icons", "load_svg_icon")
    icon = load_svg_icon("markdown", color="#0EA5E9", size=24)
    assert not icon.isNull()
    assert not icon.pixmap(24, 24).isNull()


# ---------------------------------------------------------------------------
# FA-18: SHA-256 Updater + Banner
# ---------------------------------------------------------------------------

def test_fa18_t1_01_updater_detects_release_and_shows_opaque_banner(
    tmp_path: Path,
    local_mock_server,
    make_isolated_window,
    qapp: QApplication,
) -> None:
    updater = load_attr("pdf_lector.core.updater")
    bundle = make_update_bundle(tmp_path / "rel", version="0.2.0", base_url=local_mock_server.base_url)
    local_mock_server.set_route("GET", "/releases/latest", 200, bundle["release_json"])

    rel = updater.check_for_updates("0.1.0", api_url=f"{local_mock_server.base_url}/releases/latest")
    assert rel is not None and rel.update_available is True
    assert rel.version == "0.2.0"

    win = make_isolated_window()
    win.show_update_banner(rel)
    qapp.processEvents()
    assert win.update_banner.isVisible()
    assert "rgba(" not in win.update_banner.styleSheet().lower()


def test_fa18_t1_02_updater_24h_cadence_respects_last_check(tmp_path: Path, local_mock_server) -> None:
    del tmp_path
    updater = load_attr("pdf_lector.core.updater")
    assert updater.should_run_auto_check(last_update_check_utc=None, offline_only=False) is True
    import datetime as dt

    two_hours_ago = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)).isoformat()
    twenty_five_hours_ago = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=25)).isoformat()
    assert updater.should_run_auto_check(last_update_check_utc=two_hours_ago, offline_only=False) is False
    assert updater.should_run_auto_check(last_update_check_utc=twenty_five_hours_ago, offline_only=False) is True
    del local_mock_server


def test_fa18_t1_03_skip_version_persists_and_hides_background_banner(make_isolated_window, qapp: QApplication) -> None:
    updater = load_attr("pdf_lector.core.updater")
    win = make_isolated_window()
    rel = updater.ReleaseInfo(
        version="0.2.0",
        tag_name="v0.2.0",
        notes="Notes",
        html_url="https://example.invalid",
        asset_url="https://example.invalid/setup.exe",
        sums_url="https://example.invalid/SHA256SUMS.txt",
        update_available=True,
    )
    win.skip_update_version("0.2.0")
    qapp.processEvents()
    assert not win.should_show_background_update(rel)


def test_fa18_t1_04_streaming_download_and_sha256_verification(tmp_path: Path, local_mock_server) -> None:
    updater = load_attr("pdf_lector.core.updater")
    bundle = make_update_bundle(tmp_path / "rel", version="0.2.0", base_url=local_mock_server.base_url)
    local_mock_server.set_route(
        "GET",
        "/releases/download/v0.2.0/PDF-MD-Lector-Setup.exe",
        200,
        bundle["installer_bytes"],
        "application/octet-stream",
    )
    local_mock_server.set_route(
        "GET",
        "/releases/download/v0.2.0/SHA256SUMS.txt",
        200,
        bundle["sums_text"],
        "text/plain",
    )
    pct_events: list[int] = []
    out_exe = updater.download_verified_update(
        asset_url=f"{local_mock_server.base_url}/releases/download/v0.2.0/PDF-MD-Lector-Setup.exe",
        sums_url=f"{local_mock_server.base_url}/releases/download/v0.2.0/SHA256SUMS.txt",
        dest_dir=tmp_path / "dl",
        progress_cb=pct_events.append,
    )
    assert out_exe.exists()
    assert hashlib.sha256(out_exe.read_bytes()).hexdigest() == bundle["expected_sha256"]
    assert 100 in pct_events


def test_fa18_t1_05_detached_update_script_clears_meipass_vars(tmp_path: Path) -> None:
    updater = load_attr("pdf_lector.core.updater")
    script_text = updater.build_detached_update_script(
        installer_path=tmp_path / "PDF-MD-Lector-Setup.exe",
        relaunch_exe=tmp_path / "PDF-MD-Lector-Portable.exe",
    )
    assert "_MEIPASS" in script_text
    assert "PDF-MD-Lector-Setup.exe" in script_text


# ---------------------------------------------------------------------------
# FA-19: Dynamic Hub Quick Actions, ToolWorkspace & App Dialogs
# ---------------------------------------------------------------------------

def test_fa19_t1_01_hub_adapts_quick_actions_and_resets_via_ui(
    make_isolated_window,
    isolated_appdata: Path,
    qapp: QApplication,
) -> None:
    LocalUsageInsights = load_attr("pdf_lector.core.usage_insights", "LocalUsageInsights")
    insights = LocalUsageInsights(isolated_appdata / "TRODAI" / "PDFLector" / "usage_insights.json")
    win = make_isolated_window(insights_store=insights)
    win.show()
    for _ in range(5):
        insights.record_tool_use("pdf_ocr")
    win.hub_view.refresh_recommendations()
    qapp.processEvents()
    assert "pdf_ocr" in win.hub_view.quick_action_tool_ids()
    win.hub_view.rec_clear_btn.click()
    qapp.processEvents()
    assert win.hub_view.quick_action_tool_ids()[0] == "sign_pdf"


def test_fa19_t1_02_tool_workspace_executes_tool_with_one_primary_action(tmp_path: Path, qapp: QApplication) -> None:
    ToolRegistry = load_attr("pdf_lector.core.tools", "ToolRegistry")
    ToolWorkspace = load_attr("pdf_lector.ui.tool_workspace", "ToolWorkspace")
    src = make_multipage_toc_pdf(tmp_path / "src.pdf", pages=2)
    out = tmp_path / "comp.pdf"

    ws = ToolWorkspace(ToolRegistry.get("compress_pdf"))
    try:
        ws.set_inputs([src])
        ws.set_output(out)
        ws.show()
        qapp.processEvents()
        res = ws.execute_primary_action()
        assert res.success is True and out.exists()
    finally:
        ws.close()


def test_fa19_t1_03_hub_search_ctrl_k_filters_cards(make_isolated_window, qapp: QApplication) -> None:
    win = make_isolated_window()
    win.show()
    win.hub_view.set_search_query("DIAN")
    qapp.processEvents()
    visible_cards = [c for c in win.hub_view.all_tool_cards() if c.isVisible()]
    assert len(visible_cards) >= 1


def test_fa19_t1_04_preferences_and_invoice_dialogs_render(qapp: QApplication, isolated_appdata: Path) -> None:
    del isolated_appdata
    PreferencesDialog = load_attr("pdf_lector.ui.preferences_dialog", "PreferencesDialog")
    InvoiceDialog = load_attr("pdf_lector.ui.invoice_dialog", "InvoiceDialog")

    pref = PreferencesDialog()
    inv = InvoiceDialog()
    try:
        pref.show()
        inv.show()
        qapp.processEvents()
        assert pref.isVisible() and inv.isVisible()
        assert inv.profile_combo.count() >= 2
    finally:
        pref.close()
        inv.close()


def test_fa19_t1_05_about_updater_dialog_displays_version_and_offline_toggle(qapp: QApplication) -> None:
    AboutUpdaterDialog = load_attr("pdf_lector.ui.about_updater_dialog", "AboutUpdaterDialog")
    dlg = AboutUpdaterDialog()
    try:
        dlg.show()
        qapp.processEvents()
        assert dlg.isVisible()
        assert "0.4.2" in dlg.version_label.text()
        assert dlg.offline_only_checkbox is not None
    finally:
        dlg.close()


# ---------------------------------------------------------------------------
# FA-20: Theme Studio, <300ms SplashScreen, MainWindow Chrome & F11
# ---------------------------------------------------------------------------

def test_fa20_t1_01_startup_first_frame_under_300ms_and_lazy_imports(clean_subprocess_env) -> None:
    env = dict(clean_subprocess_env)
    env["PDF_LECTOR_BENCH_STARTUP"] = "1"
    env["PDF_LECTOR_BENCH_EXIT"] = "1"
    proc = subprocess.run(
        [sys.executable, "-m", "pdf_lector.app"],
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
        check=True,
    )
    match = re.search(r"FIRST_FRAME_MS=([0-9.]+)", proc.stdout)
    assert match is not None
    assert float(match.group(1)) < 300.0


def test_fa20_t1_02_no_prototype_pill_and_uniform_232x104_cards(make_isolated_window) -> None:
    ToolCard = load_attr("pdf_lector.ui.widgets", "ToolCard")
    win = make_isolated_window()
    assert not hasattr(win, "proto_pill")
    for card in win.hub_view.all_tool_cards():
        assert card.width() == ToolCard.CARD_WIDTH == 232
        assert card.height() == ToolCard.CARD_HEIGHT == 104


def test_fa20_t1_03_theme_studio_animated_banner_scrim_and_portability(
    tmp_path: Path,
    make_isolated_window,
    qapp: QApplication,
) -> None:
    tp_mod = load_attr("pdf_lector.theme.theme_profile")
    me_mod = load_attr("pdf_lector.theme.motion_engine")
    pkg_gif = make_theme_package(tmp_path / "themes", kind="valid_gif")
    prof, warn = tp_mod.import_theme_package(pkg_gif)
    assert warn is None
    assert prof.banner_path.endswith(".gif")

    win = make_isolated_window()
    win.apply_profile(prof, save=False)
    win.show()
    qapp.processEvents()
    swatch_buttons = [
        btn
        for btn in win.studio_view.findChildren(QPushButton)
        if "swatch" in btn.objectName().lower() or "accent" in btn.objectName().lower()
    ]
    assert swatch_buttons == []
    square_color_swatches = [
        btn
        for btn in win.studio_view.findChildren(QPushButton)
        if not btn.text().strip()
        and btn.maximumWidth() <= 36
        and btn.maximumHeight() <= 36
        and "background-color" in btn.styleSheet().lower()
    ]
    assert square_color_swatches == []
    assert me_mod.compute_scrim_alpha(0.95, base_mode="dark", user_opacity=0.65, auto_scrim=True) > 150


def test_fa20_t1_04_window_chrome_resolutions_and_f11_fullscreen(make_isolated_window, qapp: QApplication) -> None:
    tp_mod = load_attr("pdf_lector.theme.theme_profile")
    win = make_isolated_window()
    flags = win.windowFlags()
    assert flags & Qt.WindowType.WindowMinimizeButtonHint
    assert flags & Qt.WindowType.WindowMaximizeButtonHint
    assert flags & Qt.WindowType.WindowCloseButtonHint

    win.show()
    win.switch_view(1, immediate=True)
    win.set_fullscreen_mode(True)
    qapp.processEvents()
    assert win.is_fullscreen_mode and not win.sidebar.isVisible()
    win.set_fullscreen_mode(False)
    qapp.processEvents()
    assert not win.is_fullscreen_mode and win.sidebar.isVisible()

    for mode in ("dark", "light"):
        win.apply_profile(replace(tp_mod.get_preset("trodai_mesh"), base_mode=mode), save=False)
        for w, h in ((980, 650), (1200, 760), (1366, 720)):
            win.resize(w, h)
            for idx in (0, 1, 2):
                win.switch_view(idx, immediate=True)
                qapp.processEvents()
                assert _find_clipped_buttons(win) == []


def test_fa20_t1_05_theme_studio_3_motion_levels(make_isolated_window, qapp: QApplication) -> None:
    tp_mod = load_attr("pdf_lector.theme.theme_profile")
    win = make_isolated_window()
    for level in ("static", "subtle", "modern"):
        prof = replace(tp_mod.get_preset("trodai_mesh"), motion_level=level)
        win.apply_profile(prof, save=False)
        qapp.processEvents()
        assert win.current_profile.motion_level == level


# ---------------------------------------------------------------------------
# FA-21: Windows 11 HKCU Integration (Mocked) & CLI Flags
# ---------------------------------------------------------------------------

def test_fa21_t1_01_mocked_hkcu_pdf_registration(fake_winreg) -> None:
    wi = load_attr("pdf_lector.core.windows_integration")
    res = wi.register_windows_integration(
        exe_path=Path(r"C:\Program Files\TRODAI\PDF-MD-Lector.exe"),
        include_markdown=False,
        winreg_backend=fake_winreg,
    )
    assert res.success is True
    assert fake_winreg.key_exists(r"Software\Classes\TRODAI.PDFMDLector.PDF")
    assert fake_winreg.get_value(r"Software\Classes\.pdf\OpenWithProgids", "TRODAI.PDFMDLector.PDF") is not None
    assert not fake_winreg.key_exists(r"Software\Classes\TRODAI.PDFMDLector.MD")


def test_fa21_t1_02_optional_markdown_registration_flag(fake_winreg) -> None:
    wi = load_attr("pdf_lector.core.windows_integration")
    res = wi.register_windows_integration(
        exe_path=Path(r"C:\Program Files\TRODAI\PDF-MD-Lector.exe"),
        include_markdown=True,
        winreg_backend=fake_winreg,
    )
    assert res.success is True
    assert fake_winreg.key_exists(r"Software\Classes\TRODAI.PDFMDLector.MD")
    assert fake_winreg.get_value(r"Software\Classes\.md\OpenWithProgids", "TRODAI.PDFMDLector.MD") is not None
    assert fake_winreg.get_value(r"Software\Classes\.markdown\OpenWithProgids", "TRODAI.PDFMDLector.MD") is not None


def test_fa21_t1_03_native_messaging_registry_and_default_apps_hook(fake_winreg) -> None:
    wi = load_attr("pdf_lector.core.windows_integration")
    res = wi.register_native_messaging_host(
        manifest_path=Path(r"C:\Program Files\TRODAI\com.trodai.pdf_md_lector.json"),
        winreg_backend=fake_winreg,
    )
    assert res.success is True
    assert fake_winreg.key_exists(r"Software\Google\Chrome\NativeMessagingHosts\com.trodai.pdf_md_lector")
    assert fake_winreg.key_exists(r"Software\Microsoft\Edge\NativeMessagingHosts\com.trodai.pdf_md_lector")
    launched: list[str] = []
    wi.open_default_apps_settings(launcher=launched.append)
    assert launched == ["ms-settings:defaultapps"]


def test_fa21_t1_04_cli_headless_merge_and_compress(tmp_path: Path, clean_subprocess_env) -> None:
    p1 = make_multipage_toc_pdf(tmp_path / "a.pdf", pages=1)
    p2 = make_multipage_toc_pdf(tmp_path / "b.pdf", pages=2)
    merged = tmp_path / "cli_merged.pdf"
    r1 = subprocess.run(
        [sys.executable, "-m", "pdf_lector.app", "--merge", str(p1), str(p2), "-o", str(merged)],
        capture_output=True,
        text=True,
        env=clean_subprocess_env,
        timeout=20,
    )
    assert r1.returncode == 0 and merged.exists()

    comp = tmp_path / "cli_comp.pdf"
    r2 = subprocess.run(
        [sys.executable, "-m", "pdf_lector.app", "--compress", str(merged), "-o", str(comp), "--quality", "60"],
        capture_output=True,
        text=True,
        env=clean_subprocess_env,
        timeout=20,
    )
    assert r2.returncode == 0 and comp.exists()


def test_fa21_t1_05_cli_help_lists_required_flags(clean_subprocess_env) -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "pdf_lector.app", "--help"],
        capture_output=True,
        text=True,
        env=clean_subprocess_env,
        timeout=15,
    )
    assert proc.returncode == 0
    for flag in ("--merge", "--compress", "--ocr", "--serve-api", "--native-host"):
        assert flag in proc.stdout


# ---------------------------------------------------------------------------
# FA-22: Browser Extension (MV3) & 4-Byte LE Native Messaging Host
# ---------------------------------------------------------------------------

def test_fa22_t1_01_mv3_browser_extension_manifest_and_assets() -> None:
    ext_dir = WORKSPACE_ROOT / "browser-extension"
    manifest = json.loads((ext_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["manifest_version"] == 3
    assert "nativeMessaging" in manifest["permissions"]
    assert (ext_dir / "background.js").exists()
    assert (ext_dir / "popup.html").exists()
    assert (ext_dir / "popup.js").exists()
    popup_html = (ext_dir / "popup.html").read_text(encoding="utf-8")
    assert "https://" not in popup_html.lower()


def test_fa22_t1_02_native_messaging_open_pdf_4byte_le_framing(tmp_path: Path, clean_subprocess_env) -> None:
    pdf_p = make_multipage_toc_pdf(tmp_path / "nm.pdf", pages=1)
    frame = encode_nm_frame({"action": "open_pdf", "path": str(pdf_p)})
    proc = subprocess.run(
        [sys.executable, "-m", "pdf_lector.core.native_messaging"],
        input=frame,
        capture_output=True,
        env=clean_subprocess_env,
        timeout=15,
    )
    assert proc.returncode == 0
    msgs = decode_nm_stream(proc.stdout)
    assert len(msgs) == 1 and msgs[0]["ok"] is True


def test_fa22_t1_03_native_messaging_webpage_to_pdf(tmp_path: Path, clean_subprocess_env) -> None:
    out_pdf = tmp_path / "web_cap.pdf"
    frame = encode_nm_frame(
        {"action": "webpage_to_pdf", "html": "<h1>TRODAI Web Capture</h1>", "output": str(out_pdf), "dpi": 150}
    )
    proc = subprocess.run(
        [sys.executable, "-m", "pdf_lector.core.native_messaging"],
        input=frame,
        capture_output=True,
        env=clean_subprocess_env,
        timeout=20,
    )
    assert proc.returncode == 0
    msgs = decode_nm_stream(proc.stdout)
    assert msgs[0]["ok"] is True and out_pdf.exists()


def test_fa22_t1_04_native_messaging_quick_action_compress(tmp_path: Path, clean_subprocess_env) -> None:
    src = make_image_pdf(tmp_path / "src.pdf")
    out_pdf = tmp_path / "nm_comp.pdf"
    frame = encode_nm_frame(
        {"action": "quick_action", "tool_id": "compress_pdf", "inputs": [str(src)], "output": str(out_pdf)}
    )
    proc = subprocess.run(
        [sys.executable, "-m", "pdf_lector.core.native_messaging"],
        input=frame,
        capture_output=True,
        env=clean_subprocess_env,
        timeout=20,
    )
    assert proc.returncode == 0
    msgs = decode_nm_stream(proc.stdout)
    assert msgs[0]["ok"] is True and out_pdf.exists()


def test_fa22_t1_05_native_messaging_multiple_sequential_frames(tmp_path: Path, clean_subprocess_env) -> None:
    pdf_p = make_multipage_toc_pdf(tmp_path / "seq.pdf", pages=1)
    stream = (
        encode_nm_frame({"action": "ping"})
        + encode_nm_frame({"action": "open_pdf", "path": str(pdf_p)})
        + encode_nm_frame({"action": "ping"})
    )
    proc = subprocess.run(
        [sys.executable, "-m", "pdf_lector.core.native_messaging"],
        input=stream,
        capture_output=True,
        env=clean_subprocess_env,
        timeout=15,
    )
    assert proc.returncode == 0
    msgs = decode_nm_stream(proc.stdout)
    assert len(msgs) == 3 and all(m["ok"] is True for m in msgs)


# ---------------------------------------------------------------------------
# FA-23: Lazy Local REST / OpenAPI Server
# ---------------------------------------------------------------------------

def test_fa23_t1_01_api_disabled_by_default_and_lazy_import(clean_subprocess_env) -> None:
    code = (
        "import sys, pdf_lector.app; "
        "assert 'fastapi' not in sys.modules and 'uvicorn' not in sys.modules"
    )
    proc = subprocess.run([sys.executable, "-c", code], env=clean_subprocess_env, capture_output=True, text=True, timeout=15)
    assert proc.returncode == 0, proc.stderr


def test_fa23_t1_02_loopback_health_and_openapi_31_schema(isolated_appdata: Path) -> None:
    del isolated_appdata
    api_srv = load_attr("pdf_lector.api.server")
    secrets_store = load_attr("pdf_lector.core.secrets_store")
    secrets_store.set_secret("local_api_token", "trodai-e2e-bearer-123")

    srv = api_srv.LocalApiServer(host="127.0.0.1", port=0)
    srv.start()
    try:
        req = Request(f"{srv.base_url}/health", headers={"Authorization": "Bearer trodai-e2e-bearer-123"})
        with urlopen(req, timeout=5) as resp:
            assert resp.status == 200
        with urlopen(f"{srv.base_url}/openapi.json", timeout=5) as resp:
            schema = json.loads(resp.read().decode("utf-8"))
            assert schema["openapi"].startswith("3.1")
    finally:
        srv.stop()


def test_fa23_t1_03_authenticated_post_tool_execution(tmp_path: Path, isolated_appdata: Path) -> None:
    del isolated_appdata
    api_srv = load_attr("pdf_lector.api.server")
    secrets_store = load_attr("pdf_lector.core.secrets_store")
    secrets_store.set_secret("local_api_token", "tok-exec-001")
    p1 = make_multipage_toc_pdf(tmp_path / "a.pdf", pages=1)
    p2 = make_multipage_toc_pdf(tmp_path / "b.pdf", pages=1)
    out = tmp_path / "api_merged.pdf"

    srv = api_srv.LocalApiServer(host="127.0.0.1", port=0)
    srv.start()
    try:
        body = json.dumps({"inputs": [str(p1), str(p2)], "output": str(out), "params": {}}).encode("utf-8")
        req = Request(
            f"{srv.base_url}/tools/merge_pdf",
            data=body,
            headers={"Authorization": "Bearer tok-exec-001", "Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req, timeout=10) as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode("utf-8"))
            assert data["success"] is True and out.exists()
    finally:
        srv.stop()


def test_fa23_t1_04_rest_api_markdown_conversion_route(tmp_path: Path, isolated_appdata: Path) -> None:
    del isolated_appdata
    api_srv = load_attr("pdf_lector.api.server")
    secrets_store = load_attr("pdf_lector.core.secrets_store")
    secrets_store.set_secret("local_api_token", "tok-md-002")
    md_p = make_gfm_markdown_bundle(tmp_path / "md")["valid_md"]
    out = tmp_path / "api_md.pdf"

    srv = api_srv.LocalApiServer(host="127.0.0.1", port=0)
    srv.start()
    try:
        body = json.dumps({"inputs": [str(md_p)], "output": str(out), "params": {"dpi": 150}}).encode("utf-8")
        req = Request(
            f"{srv.base_url}/tools/convert_to_pdf",
            data=body,
            headers={"Authorization": "Bearer tok-md-002", "Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req, timeout=10) as resp:
            assert resp.status == 200 and out.exists()
    finally:
        srv.stop()


def test_fa23_t1_05_dpapi_bearer_token_persistence_across_server_restart(isolated_appdata: Path) -> None:
    del isolated_appdata
    api_srv = load_attr("pdf_lector.api.server")
    secrets_store = load_attr("pdf_lector.core.secrets_store")
    secrets_store.set_secret("local_api_token", "persisted-dpapi-token")

    srv1 = api_srv.LocalApiServer(host="127.0.0.1", port=0)
    srv1.start()
    srv1.stop()

    srv2 = api_srv.LocalApiServer(host="127.0.0.1", port=0)
    srv2.start()
    try:
        req = Request(f"{srv2.base_url}/health", headers={"Authorization": "Bearer persisted-dpapi-token"})
        with urlopen(req, timeout=5) as resp:
            assert resp.status == 200
    finally:
        srv2.stop()


# ---------------------------------------------------------------------------
# FA-24: AI "Ask Document" (BM25 + BYOK + pdf-md-lector-mcp stdio)
# ---------------------------------------------------------------------------

def test_fa24_t1_01_local_bm25_pdf_query_exact_page_citation(tmp_path: Path) -> None:
    ai_rag = load_attr("pdf_lector.core.ai_rag")
    pdf_p = make_multipage_toc_pdf(tmp_path / "spec.pdf", pages=4)
    idx = ai_rag.DocumentRagIndex()
    idx.index_pdf(pdf_p)
    hits = idx.search("Factura Electronica DIAN CUFE NIT 900123456", top_k=3)
    assert len(hits) >= 1
    assert hits[0].page_number == 3


def test_fa24_t1_02_local_bm25_markdown_query_heading_citation(tmp_path: Path) -> None:
    ai_rag = load_attr("pdf_lector.core.ai_rag")
    md_p = make_gfm_markdown_bundle(tmp_path / "md")["valid_md"]
    idx = ai_rag.DocumentRagIndex()
    idx.index_markdown(md_p)
    hits = idx.search("Sanitizado PDFium", top_k=3)
    assert len(hits) >= 1
    assert "Detalles Tecnicos" in (hits[0].section or "")


def test_fa24_t1_03_consented_byok_and_dpapi_key_protection(
    tmp_path: Path,
    isolated_appdata: Path,
    local_mock_server,
    qapp: QApplication,
) -> None:
    del isolated_appdata, qapp
    ai_rag = load_attr("pdf_lector.core.ai_rag")
    secrets_store = load_attr("pdf_lector.core.secrets_store")
    secrets_store.set_secret("byok_openai_key", "sk-mock-byok-001")
    local_mock_server.set_route(
        "POST",
        "/v1/chat/completions",
        200,
        {"choices": [{"message": {"content": "Respuesta basada en [p. 3]."}}]},
    )

    pdf_p = make_multipage_toc_pdf(tmp_path / "spec.pdf", pages=4)
    idx = ai_rag.DocumentRagIndex()
    idx.index_pdf(pdf_p)
    ans = ai_rag.ask_document(
        idx,
        "Cual es el NIT?",
        provider="openai_compatible",
        endpoint_url=f"{local_mock_server.base_url}/v1/chat/completions",
        consent_granted=True,
    )
    assert "p. 3" in ans.answer_text
    assert len(local_mock_server.requests_log) == 1


def test_fa24_t1_04_mcp_server_stdio_jsonrpc_lifecycle(tmp_path: Path, clean_subprocess_env) -> None:
    pdf_p = make_multipage_toc_pdf(tmp_path / "mcp.pdf", pages=2)
    md_p = make_gfm_markdown_bundle(tmp_path / "md")["valid_md"]
    reqs = make_mcp_requests(pdf_p, md_p)
    stdin_data = "\n".join(json.dumps(r) for r in reqs) + "\n"

    proc = subprocess.run(
        [sys.executable, "-m", "pdf_lector.mcp_server"],
        input=stdin_data,
        capture_output=True,
        text=True,
        env=clean_subprocess_env,
        timeout=20,
    )
    assert proc.returncode == 0
    lines = [json.loads(line) for line in proc.stdout.strip().splitlines() if line.strip()]
    assert len(lines) == 4
    assert lines[0]["id"] == 1 and "capabilities" in lines[0]["result"]
    assert lines[1]["id"] == 2 and len(lines[1]["result"]["tools"]) >= 3
    assert lines[2]["id"] == 3 and "Privacidad" in json.dumps(lines[2]["result"])
    assert lines[3]["id"] == 4 and "Titulo Principal" in json.dumps(lines[3]["result"])


def test_fa24_t1_05_mcp_client_connects_to_local_stdio_server(tmp_path: Path, clean_subprocess_env) -> None:
    McpClient = load_attr("pdf_lector.core.mcp_client", "McpStdioClient")
    pdf_p = make_multipage_toc_pdf(tmp_path / "cli.pdf", pages=2)
    client = McpClient([sys.executable, "-m", "pdf_lector.mcp_server"], env=clean_subprocess_env)
    try:
        client.initialize()
        tools = client.list_tools()
        assert len(tools) >= 3
        res = client.call_tool("read_pdf_text", {"path": str(pdf_p), "page_index": 0})
        assert "Privacidad" in json.dumps(res)
    finally:
        client.close()


# ---------------------------------------------------------------------------
# FA-25: Packaging, $0 Signing, CI & OSS/Multi-AI Docs
# ---------------------------------------------------------------------------

def test_fa25_t1_01_build_exe_script_present_and_produces_hashes(tmp_path: Path) -> None:
    del tmp_path
    build_script = WORKSPACE_ROOT / "scripts" / "build_exe.py"
    assert build_script.exists()
    txt = build_script.read_text(encoding="utf-8")
    assert "PDF-MD-Lector-Portable" in txt
    assert "SHA256SUMS.txt" in txt


def test_fa25_t1_02_sign_local_ps1_ast_valid_and_dry_run() -> None:
    ps1 = WORKSPACE_ROOT / "scripts" / "sign_local.ps1"
    assert ps1.exists()
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1), "-DryRun"],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert proc.returncode == 0, proc.stderr


def test_fa25_t1_03_installer_iss_structure_and_non_admin_privileges() -> None:
    iss = WORKSPACE_ROOT / "packaging" / "installer.iss"
    assert iss.exists()
    txt = iss.read_text(encoding="utf-8")
    assert "PrivilegesRequired=lowest" in txt
    assert "PrivilegesRequiredOverridesAllowed=dialog" in txt
    assert "PDF-MD-Lector-Setup" in txt
    assert "TRODAI.PDFMDLector" in txt


def test_fa25_t1_04_github_workflow_slsa_and_sha256sums() -> None:
    wf = WORKSPACE_ROOT / ".github" / "workflows" / "build-release.yml"
    assert wf.exists()
    txt = wf.read_text(encoding="utf-8")
    assert "actions/attest-build-provenance" in txt
    assert "SHA256SUMS.txt" in txt
    assert "SignPath" in txt or "signpath" in txt.lower()


def test_fa25_t1_05_oss_and_multi_ai_audit_docs_complete() -> None:
    required_files = [
        "README.md",
        "LICENSE",
        "CHANGELOG.md",
        "CONTRIBUTING.md",
        "SECURITY.md",
        "CLAUDE.md",
        "AGENTS.md",
        ".gitignore",
        ".github/PULL_REQUEST_TEMPLATE.md",
        "docs/ARCHITECTURE.md",
        "docs/API_AND_MCP.md",
        "docs/SECURITY_AND_PRIVACY.md",
        "docs/CODE_SIGNING_AND_INSTALL.md",
        "docs/AUDIT_HANDOFF.md",
    ]
    for rel in required_files:
        p = WORKSPACE_ROOT / rel
        assert p.exists() and p.stat().st_size > 0, f"Missing or empty doc: {rel}"
    issue_dir = WORKSPACE_ROOT / ".github" / "ISSUE_TEMPLATE"
    assert issue_dir.is_dir() and any(issue_dir.iterdir())
