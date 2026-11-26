"""Tier 2 Boundary, Corner, Negative & Security E2E Tests (FA-01..FA-25)."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from PySide6.QtCore import QPointF
from PySide6.QtWidgets import QApplication, QMessageBox

from tests.e2e.conftest import WORKSPACE_ROOT, load_attr
from tests.e2e.fixtures import (
    decode_nm_stream,
    encode_nm_frame,
    make_adversarial_markdown,
    make_blank_only_pdf,
    make_corrupt_pdf,
    make_encrypted_aes256_pdf,
    make_facturx_pdfa3,
    make_gfm_markdown_bundle,
    make_invoice_pdf,
    make_invoice_xml,
    make_local_glossary_json,
    make_multipage_toc_pdf,
    make_pii_trace_payload,
    make_sample_images,
    make_scanned_text_pdf,
    make_self_signed_pkcs12,
    make_theme_package,
    make_update_bundle,
)

pytestmark = pytest.mark.tier2


# ---------------------------------------------------------------------------
# FA-01: Branding, IDs, Phase-1 Boot & Assets (Boundary / Negative)
# ---------------------------------------------------------------------------

def test_fa01_t2_01_read_version_fallback_when_version_file_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pkg = load_attr("pdf_lector")
    empty_dir = tmp_path / "bundle_empty"
    empty_dir.mkdir()
    monkeypatch.setattr(pkg, "get_base_dir", lambda: empty_dir)
    assert pkg.read_version() == pkg.__version__

    empty_ver = empty_dir / "VERSION"
    empty_ver.write_text("   \n", encoding="utf-8")
    assert pkg.read_version() == pkg.__version__


def test_fa01_t2_02_cli_unknown_flag_exits_2(clean_subprocess_env) -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "pdf_lector.app", "--unknown-flag-xyz"],
        capture_output=True,
        text=True,
        env=clean_subprocess_env,
        timeout=15,
    )
    assert proc.returncode == 2
    assert "usage" in proc.stderr.lower() or "error" in proc.stderr.lower()


def test_fa01_t2_03_missing_svg_icon_returns_empty_qicon_safely(qapp: QApplication) -> None:
    del qapp
    load_svg_icon = load_attr("pdf_lector.ui.svg_icons", "load_svg_icon")
    icon = load_svg_icon("nonexistent_icon_id_xyz", color="#FFFFFF")
    assert icon.isNull()


def test_fa01_t2_04_phase1_boot_excludes_heavy_engines(clean_subprocess_env) -> None:
    code = (
        "import os, sys; "
        "os.environ['PDF_LECTOR_BENCH_STARTUP']='1'; "
        "os.environ['PDF_LECTOR_BENCH_EXIT']='1'; "
        "from pdf_lector.app import launch_app; "
        "rc = launch_app([]); "
        "heavy = [m for m in ('pikepdf','pyhanko','fastapi','uvicorn','openpyxl','docx','markdown_it','nh3') if m in sys.modules]; "
        "assert rc == 0 and heavy == [], f'Heavy modules loaded in phase 1: {heavy}'"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=clean_subprocess_env,
        timeout=15,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"


def test_fa01_t2_05_windows_safe_appid_and_progids_no_slash() -> None:
    pkg = load_attr("pdf_lector")
    for ident in (pkg.APP_USER_MODEL_ID, "TRODAI.PDFMDLector.PDF", "TRODAI.PDFMDLector.MD"):
        assert "/" not in ident
        assert "\\" not in ident
        assert " " not in ident


# ---------------------------------------------------------------------------
# FA-02: path_guard, DPAPI secrets_store & TempManager (Boundary / Negative)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "unsafe_ref",
    [
        r"\\192.168.1.50\share\secret.pdf",
        "//fileserver/public/secret.md",
        r"\\?\UNC\server\share\secret.pdf",
        r"\\.\PhysicalDrive0",
        "https://evil.invalid/payload.md",
        "http://example.invalid/tracker.png",
        "javascript:alert(1)",
        "../../Windows/System32/drivers/etc/hosts",
        "file://server/share/secret.pdf",
        "ftp://ftp.example.invalid/doc.pdf",
        "smb://fileserver/share/doc.md",
        "data:text/html,<script>alert(1)</script>",
    ],
)
def test_fa02_t2_01_unc_urls_and_traversal_blocked(tmp_path: Path, unsafe_ref: str) -> None:
    pg = load_attr("pdf_lector.core.path_guard")
    assert pg.is_unc_or_network_path(unsafe_ref) or ".." in unsafe_ref
    with pytest.raises((ValueError, PermissionError, OSError)):
        pg.resolve_safe_doc_resource(tmp_path, unsafe_ref)


def test_fa02_t2_02_remote_drive_and_cloud_only_blocked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pg = load_attr("pdf_lector.core.path_guard")
    pdf_p = make_multipage_toc_pdf(tmp_path / "cloud.pdf", pages=1)

    monkeypatch.setattr(pg, "is_cloud_only_file", lambda _p: True)
    assert pg.validate_local_pdf_path(pdf_p) == (False, "cloud_only_blocked")

    monkeypatch.setattr(pg, "is_cloud_only_file", lambda _p: False)
    monkeypatch.setattr(pg, "is_remote_drive", lambda _p: True)
    assert pg.validate_local_pdf_path(pdf_p) == (False, "network_or_url_blocked")


def test_fa02_t2_03_media_size_boundary_25mb_plus_1(tmp_path: Path) -> None:
    pg = load_attr("pdf_lector.core.path_guard")
    big_png = tmp_path / "oversize.png"
    with big_png.open("wb") as handle:
        handle.seek(25 * 1024 * 1024)
        handle.write(b"\x00")
    ok, reason = pg.validate_local_media_path(big_png, check_exists=True)
    assert not ok and reason == "file_too_large"


def test_fa02_t2_04_null_byte_empty_dir_and_wrong_extension(tmp_path: Path) -> None:
    pg = load_attr("pdf_lector.core.path_guard")
    exe_p = tmp_path / "bad.exe"
    exe_p.write_bytes(b"MZ")
    pdf_dir = tmp_path / "dir.pdf"
    pdf_dir.mkdir()
    for bad in ("", "   ", "doc.pdf\x00.exe", tmp_path, exe_p, pdf_dir):
        ok_pdf, reason_pdf = pg.validate_local_pdf_path(bad)
        assert ok_pdf is False and reason_pdf != "ok"
        ok_md, reason_md = pg.validate_local_markdown_path(bad)
        assert ok_md is False and reason_md != "ok"
        ok_gl, reason_gl = pg.validate_local_glossary_path(bad)
        assert ok_gl is False and reason_gl != "ok"


def test_fa02_t2_05_tampered_dpapi_ciphertext_and_corrupt_secrets_json(isolated_appdata: Path) -> None:
    secrets_store = load_attr("pdf_lector.core.secrets_store")
    secrets_file = isolated_appdata / "TRODAI" / "PDFLector" / "secrets.json"
    secrets_file.parent.mkdir(parents=True, exist_ok=True)
    secrets_file.write_text("{corrupt_json", encoding="utf-8")
    assert secrets_store.get_secret("missing_key", default="safe_fallback") == "safe_fallback"

    secrets_file.write_text(
        json.dumps({"schema_version": 1, "secrets": {"openai_api_key": "NOT_VALID_DPAPI_BASE64!!!"}}),
        encoding="utf-8",
    )
    assert secrets_store.get_secret("openai_api_key", default="safe_fallback") == "safe_fallback"

    secrets_file.write_text(
        json.dumps({"schema_version": 1, "secrets": {"openai_api_key": "Y29ycnVwdC1kcGFwaS1ibG9i"}}),
        encoding="utf-8",
    )
    assert secrets_store.get_secret("openai_api_key", default="safe_fallback") == "safe_fallback"


def test_fa02_t2_06_temp_manager_handles_already_deleted_and_empty_dirs(tmp_path: Path) -> None:
    TempManager = load_attr("pdf_lector.core.temp_manager", "TempManager")
    tm = TempManager(temp_root=tmp_path / "tmp_root", register_atexit=False)
    d = tm.create_temp_dir(task_id="t_missing", secure=True)
    f1 = d / "gone.bin"
    f2 = d / "present.bin"
    f1.write_bytes(b"A" * 64)
    f2.write_bytes(b"SECRET_WIPE_ME" * 10)
    f1.unlink()  # Already deleted externally
    assert tm.cleanup_task("t_missing") == []
    assert not f2.exists()
    assert not d.exists()


# ---------------------------------------------------------------------------
# FA-03: CrashGuard, SingleInstanceLock, NotificationCenter & TaskController
# ---------------------------------------------------------------------------

def test_fa03_t2_01_crash_guard_log_rotation_upper_bound(isolated_appdata: Path) -> None:
    CrashGuard = load_attr("pdf_lector.core.crash_guard", "CrashGuard")
    logs_dir = isolated_appdata / "TRODAI" / "PDFLector" / "logs"
    cg = CrashGuard(logs_dir=logs_dir, max_app_log_bytes=1024, backup_count=2)
    cg.install()
    try:
        cg.begin_session()
        for i in range(120):
            cg.log_app("INFO", f"Log rotation line {i:04d} - " + ("X" * 40))
        app_logs = list(logs_dir.glob("app.log*"))
        assert len(app_logs) <= 3
        assert (logs_dir / "app.log").stat().st_size <= 2048
        cg.mark_clean_exit()
    finally:
        cg.uninstall()


def test_fa03_t2_02_single_instance_rejects_unsafe_forwarded_paths(qapp: QApplication) -> None:
    SingleInstanceLock = load_attr("pdf_lector.core.single_instance", "SingleInstanceLock")
    key = f"trodai-e2e-si-neg-{os.getpid()}"
    received: list[list[Path]] = []
    primary = SingleInstanceLock(key=key)
    assert primary.acquire() is True
    assert primary.start_server(lambda paths: received.append(paths)) is True
    try:
        secondary = SingleInstanceLock(key=key)
        assert secondary.acquire() is False
        secondary.send_to_running_instance([r"\\evil\share\bad.pdf", "../../secret.md"])
        for _ in range(20):
            qapp.processEvents()
        assert all(paths == [] for paths in received)
    finally:
        primary.release()


def test_fa03_t2_03_single_instance_stale_lock_recovery(qapp: QApplication) -> None:
    del qapp
    SingleInstanceLock = load_attr("pdf_lector.core.single_instance", "SingleInstanceLock")
    key = f"trodai-e2e-stale-recovery-{os.getpid()}"
    lock = SingleInstanceLock(key=key)
    assert lock.acquire() is True
    assert lock.start_server(lambda _p: None) is True
    lock.release()
    lock2 = SingleInstanceLock(key=key)
    try:
        assert lock2.acquire() is True
        assert lock2.start_server(lambda _p: None) is True
    finally:
        lock2.release()


def test_fa03_t2_04_task_controller_mid_run_cancel_cleans_partial(tmp_path: Path, qapp: QApplication) -> None:
    tc_mod = load_attr("pdf_lector.core.task_control")
    ctrl = tc_mod.TaskController()
    mgr = tc_mod.TaskManager()
    partial_file = tmp_path / "partial.pdf"
    partial_written = threading.Event()
    canceled_events: list[str] = []

    def _work(controller) -> str:
        partial_file.write_bytes(b"%PDF-partial")
        partial_written.set()
        try:
            for _ in range(50):
                tc_mod.stop_point(controller, "Mid-run cancel")
                time.sleep(0.01)
            return "done"
        except tc_mod.TaskCanceledError:
            partial_file.unlink(missing_ok=True)
            raise

    task = tc_mod.AsyncTask(_work, controller=ctrl)
    mgr.submit(task, on_canceled=lambda _tid, detail: canceled_events.append(detail))
    assert partial_written.wait(timeout=2.0)
    ctrl.cancel()
    assert mgr.wait_for_done(5000) is True
    deadline = time.monotonic() + 2.0
    while not canceled_events and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    assert len(canceled_events) == 1
    assert ctrl.status == "canceled"
    assert not partial_file.exists()


def test_fa03_t2_05_task_controller_idempotent_double_cancel(qapp: QApplication) -> None:
    tc_mod = load_attr("pdf_lector.core.task_control")
    ctrl = tc_mod.TaskController()
    mgr = tc_mod.TaskManager()
    finished_results: list[object] = []

    task = tc_mod.AsyncTask(lambda: "ok", controller=ctrl)
    mgr.submit(task, on_finished=lambda _tid, res: finished_results.append(res))
    assert mgr.wait_for_done(5000) is True
    deadline = time.monotonic() + 2.0
    while not finished_results and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    assert finished_results == ["ok"]
    ctrl.cancel()
    ctrl.cancel()
    assert ctrl.is_canceled is True
    assert ctrl.checkpoint() == "cancel"


# ---------------------------------------------------------------------------
# FA-04: feedback_reporter & SupportDialog (Boundary / Negative)
# ---------------------------------------------------------------------------

def test_fa04_t2_01_zero_onedrive_org_or_doc_text_leakage(tmp_path: Path) -> None:
    fr = load_attr("pdf_lector.core.feedback_reporter")
    trace, needles = make_pii_trace_payload(tmp_path / "pii.log")
    report = fr.build_feedback_report(
        kind=fr.KIND_BUG,
        title="Contract open failure",
        description="Failed on private contract",
        error_text=trace,
    )
    for needle in needles.values():
        assert needle not in report.markdown_body
    assert "Fundacion Mamonal" not in report.markdown_body


def test_fa04_t2_02_http_500_preserves_local_anonymized_backup(
    isolated_appdata: Path,
    tmp_path: Path,
    local_mock_server,
) -> None:
    fr = load_attr("pdf_lector.core.feedback_reporter")
    local_mock_server.set_route("POST", "/submit", 500, {"error": "server_down"})
    trace, _ = make_pii_trace_payload(tmp_path / "pii.log")
    report = fr.build_feedback_report(
        kind=fr.KIND_BUG,
        title="Server down test",
        description="Server down test",
        error_text=trace,
    )
    ok, msg = fr.submit_feedback_endpoint(
        report,
        f"{local_mock_server.base_url}/submit",
        allow_localhost=True,
    )
    assert ok is False
    assert "500" in msg
    backup_md_path, backup_json_path = fr.save_local_report(report)
    assert backup_md_path.exists()
    assert backup_json_path.exists()
    assert str(isolated_appdata) in str(backup_md_path)


def test_fa04_t2_03_empty_feedback_submission_rejected() -> None:
    fr = load_attr("pdf_lector.core.feedback_reporter")
    report = fr.build_feedback_report(kind=fr.KIND_BUG, title="", description="   \n\t  ", error_text="")
    assert report.description.strip() == ""
    ok_empty, msg_empty = fr.submit_feedback_endpoint(report, "")
    assert ok_empty is False and msg_empty == "empty_endpoint_url"
    ok_ws, msg_ws = fr.submit_feedback_endpoint(report, "   ")
    assert ok_ws is False and msg_ws == "empty_endpoint_url"


def test_fa04_t2_04_oversized_200kb_trace_bounded_and_scrubbed_head_and_tail() -> None:
    fr = load_attr("pdf_lector.core.feedback_reporter")
    head_pii = r"C:\Users\AliceHead\OneDrive\SecretHead.pdf"
    tail_pii = "tail.leak@example.org 10.20.30.40"
    big_trace = f"{head_pii}\n" + ("TRACE_LINE_FILLER\n" * 12000) + f"\n{tail_pii}"
    report = fr.build_feedback_report(
        kind=fr.KIND_BUG,
        title="Big trace",
        description="Big trace",
        error_text=big_trace,
    )
    assert "AliceHead" not in report.markdown_body
    assert "tail.leak@example.org" not in report.markdown_body
    assert "10.20.30.40" not in report.markdown_body

    captured_bytes: list[bytes] = []

    def _capture_transport(_url: str, body_bytes: bytes, _headers: dict[str, str], _timeout: float) -> tuple[int, str]:
        captured_bytes.append(body_bytes)
        return 200, "ok"

    ok, _msg = fr.submit_feedback_endpoint(
        report,
        "https://support.example.invalid/report",
        transport=_capture_transport,
    )
    assert ok is True and len(captured_bytes) == 1
    assert len(captured_bytes[0]) <= fr.MAX_PAYLOAD_BYTES
    sent_text = captured_bytes[0].decode("utf-8")
    assert "AliceHead" not in sent_text
    assert "tail.leak@example.org" not in sent_text
    assert "10.20.30.40" not in sent_text


def test_fa04_t2_05_insecure_non_https_remote_endpoint_blocked() -> None:
    fr = load_attr("pdf_lector.core.feedback_reporter")
    report = fr.build_feedback_report(kind=fr.KIND_BUG, title="Test unsafe url", description="Test unsafe url")
    for bad_url in (
        "http://insecure.example.invalid/hook",
        "http://127.0.0.1:8080/hook",
        "file:///C:/temp/leak.json",
        "ftp://example.invalid/x",
    ):
        ok, reason = fr.submit_feedback_endpoint(report, bad_url)
        assert ok is False and reason == "insecure_endpoint_blocked"


# ---------------------------------------------------------------------------
# FA-05: LocalUsageInsights (Boundary / Negative)
# ---------------------------------------------------------------------------

def test_fa05_t2_01_zero_filenames_or_secret_text_persisted(tmp_path: Path) -> None:
    LocalUsageInsights = load_attr("pdf_lector.core.usage_insights", "LocalUsageInsights")
    store = tmp_path / "insights.json"
    ins = LocalUsageInsights(store)
    ins.record_document_opened("FACTURA SECRET_SALARY_99999 NIT 900111222", page_count=2)
    ins.record_search("factura confidencial")
    raw = store.read_text(encoding="utf-8")
    assert "SECRET_SALARY_99999" not in raw
    assert "900111222" not in raw
    assert "confidencial" not in raw


def test_fa05_t2_02_corrupt_usage_insights_json_recovers(tmp_path: Path) -> None:
    LocalUsageInsights = load_attr("pdf_lector.core.usage_insights", "LocalUsageInsights")
    store = tmp_path / "insights.json"
    store.write_text("{invalid json", encoding="utf-8")
    ins1 = LocalUsageInsights(store)
    assert ins1.enabled is True
    store.write_text('["not_a_dict"]', encoding="utf-8")
    ins2 = LocalUsageInsights(store)
    assert len(ins2.get_recommended_tools()) >= 4


def test_fa05_t2_03_short_search_and_empty_inputs_ignored(tmp_path: Path) -> None:
    LocalUsageInsights = load_attr("pdf_lector.core.usage_insights", "LocalUsageInsights")
    store = tmp_path / "insights.json"
    ins = LocalUsageInsights(store)
    ins.record_search("")
    ins.record_search("ab")
    ins.record_tool_use("")
    ins.record_dictionary_lookup("")
    assert not store.exists() or json.loads(store.read_text(encoding="utf-8"))["tool_counts"] == {}


def test_fa05_t2_04_insights_persist_across_reload(tmp_path: Path) -> None:
    LocalUsageInsights = load_attr("pdf_lector.core.usage_insights", "LocalUsageInsights")
    store = tmp_path / "insights.json"
    ins1 = LocalUsageInsights(store)
    ins1.record_tool_use("pdf_ocr")
    ins1.record_tool_use("pdf_ocr")
    ins2 = LocalUsageInsights(store)
    assert ins2.get_recommended_tools()[0][0] == "pdf_ocr"


def test_fa05_t2_05_unknown_tool_id_in_store_handled_safely(tmp_path: Path, make_isolated_window) -> None:
    LocalUsageInsights = load_attr("pdf_lector.core.usage_insights", "LocalUsageInsights")
    store = tmp_path / "ins_unknown.json"
    store.write_text(json.dumps({"enabled": True, "tool_counts": {"legacy_ghost_tool": 99}}), encoding="utf-8")
    win = make_isolated_window(insights_store=LocalUsageInsights(store))
    win.hub_view.refresh_recommendations()
    assert len(win.hub_view.quick_action_tool_ids()) == 6


# ---------------------------------------------------------------------------
# FA-06: ToolRegistry & DPI/Quality/Lang Boundaries
# ---------------------------------------------------------------------------

def test_fa06_t2_01_unknown_tool_id_raises() -> None:
    ToolRegistry = load_attr("pdf_lector.core.tools", "ToolRegistry")
    with pytest.raises((KeyError, ValueError)):
        ToolRegistry.get("nonexistent_tool_xyz")


@pytest.mark.parametrize("dpi", [0, -72, 35, 36, 1200, 1201])
def test_fa06_t2_02_dpi_bva_limits(tmp_path: Path, dpi: int) -> None:
    ToolRegistry = load_attr("pdf_lector.core.tools", "ToolRegistry")
    src = make_multipage_toc_pdf(tmp_path / "src.pdf", pages=1)
    out_dir = tmp_path / f"out_{dpi}"
    out_dir.mkdir()
    tool = ToolRegistry.get("pdf_to_images")
    if 36 <= dpi <= 1200:
        res = tool.run([src], out_dir, {"dpi": dpi, "format": "png"})
        assert res.success is True
    else:
        try:
            res = tool.run([src], out_dir, {"dpi": dpi, "format": "png"})
            assert res.success is False
        except ValueError:
            pass


@pytest.mark.parametrize("quality", [-5, 0, 9, 10, 100, 101])
def test_fa06_t2_03_quality_bva_limits(tmp_path: Path, quality: int) -> None:
    ToolRegistry = load_attr("pdf_lector.core.tools", "ToolRegistry")
    src = make_multipage_toc_pdf(tmp_path / "src.pdf", pages=1)
    out_pdf = tmp_path / f"q_{quality}.pdf"
    tool = ToolRegistry.get("compress_pdf")
    if 10 <= quality <= 100:
        res = tool.run([src], out_pdf, {"dpi": 150, "quality": quality})
        assert res.success is True
    else:
        try:
            res = tool.run([src], out_pdf, {"dpi": 150, "quality": quality})
            assert res.success is False
        except ValueError:
            pass


@pytest.mark.parametrize("bad_lang", ["", "klingon_xx", "123", "es;rm", "zz-invalid"])
def test_fa06_t2_04_unsupported_source_lang_rejected(tmp_path: Path, bad_lang: str) -> None:
    ToolRegistry = load_attr("pdf_lector.core.tools", "ToolRegistry")
    scan = make_scanned_text_pdf(tmp_path / "scan.pdf")
    out_pdf = tmp_path / "ocr.pdf"
    try:
        res = ToolRegistry.get("pdf_ocr").run([scan], out_pdf, {"dpi": 150, "source_lang": bad_lang})
        assert res.success is False
    except ValueError:
        pass


def test_fa06_t2_05_empty_inputs_and_unc_input_rejected(tmp_path: Path) -> None:
    ToolRegistry = load_attr("pdf_lector.core.tools", "ToolRegistry")
    tool = ToolRegistry.get("merge_pdf")
    for bad_inputs in ([], [Path(r"\\192.168.1.9\share\a.pdf")], [tmp_path / "missing.pdf"]):
        try:
            res = tool.run(bad_inputs, tmp_path / "out.pdf", {})
            assert res.success is False
        except (ValueError, FileNotFoundError):
            pass


# ---------------------------------------------------------------------------
# FA-07: Organize & Edit Suite (Boundary / Negative)
# ---------------------------------------------------------------------------

def test_fa07_t2_01_delete_all_pages_rejected(tmp_path: Path) -> None:
    ToolRegistry = load_attr("pdf_lector.core.tools", "ToolRegistry")
    src = make_multipage_toc_pdf(tmp_path / "src.pdf", pages=3)
    res = ToolRegistry.get("delete_pages").run([src], tmp_path / "empty.pdf", {"pages": [1, 2, 3]})
    assert res.success is False


def test_fa07_t2_02_out_of_range_and_reversed_page_indices_rejected(tmp_path: Path) -> None:
    ToolRegistry = load_attr("pdf_lector.core.tools", "ToolRegistry")
    src = make_multipage_toc_pdf(tmp_path / "src.pdf", pages=2)
    assert ToolRegistry.get("extract_pages").run([src], tmp_path / "e.pdf", {"pages": [0, 99]}).success is False
    assert ToolRegistry.get("split_pdf").run([src], tmp_path, {"ranges": "5-1"}).success is False


def test_fa07_t2_03_corrupt_and_encrypted_pdf_without_password_fail_cleanly(tmp_path: Path) -> None:
    ToolRegistry = load_attr("pdf_lector.core.tools", "ToolRegistry")
    corrupt = make_corrupt_pdf(tmp_path / "corrupt.pdf")
    enc = make_encrypted_aes256_pdf(tmp_path / "enc.pdf")
    assert ToolRegistry.get("split_pdf").run([corrupt], tmp_path, {"ranges": "1"}).success is False
    assert ToolRegistry.get("merge_pdf").run([enc, enc], tmp_path / "m.pdf", {}).success is False


def test_fa07_t2_04_extract_images_and_attachments_on_blank_pdf(tmp_path: Path) -> None:
    ToolRegistry = load_attr("pdf_lector.core.tools", "ToolRegistry")
    blank = make_blank_only_pdf(tmp_path / "blank.pdf")
    d_img = tmp_path / "imgs"
    d_att = tmp_path / "atts"
    d_img.mkdir()
    d_att.mkdir()
    r1 = ToolRegistry.get("extract_images").run([blank], d_img, {})
    r2 = ToolRegistry.get("extract_attachments").run([blank], d_att, {})
    assert r1.success is True and r1.output_paths == []
    assert r2.success is True and r2.output_paths == []


def test_fa07_t2_05_invalid_nup_and_overcrop_rejected(tmp_path: Path) -> None:
    ToolRegistry = load_attr("pdf_lector.core.tools", "ToolRegistry")
    src = make_multipage_toc_pdf(tmp_path / "src.pdf", pages=2)
    assert ToolRegistry.get("nup_pages").run([src], tmp_path / "n0.pdf", {"nup": 0}).success is False
    assert ToolRegistry.get("crop_pdf").run([src], tmp_path / "ov.pdf", {"margin": 5000}).success is False


def test_fa07_t2_06_reorder_pages_invalid_indices_rejected(tmp_path: Path) -> None:
    ToolRegistry = load_attr("pdf_lector.core.tools", "ToolRegistry")
    src = make_multipage_toc_pdf(tmp_path / "src.pdf", pages=4)
    assert ToolRegistry.get("reorder_pages").run([src], tmp_path / "bad.pdf", {"order": [1, 1, 99]}).success is False


# ---------------------------------------------------------------------------
# FA-08: Convert, OCR, Create & Capture (Boundary / Negative)
# ---------------------------------------------------------------------------

def test_fa08_t2_01_unsupported_extension_in_convert_tools(tmp_path: Path) -> None:
    ToolRegistry = load_attr("pdf_lector.core.tools", "ToolRegistry")
    bad_exe = tmp_path / "bad.exe"
    bad_exe.write_bytes(b"MZ")
    src = make_multipage_toc_pdf(tmp_path / "src.pdf", pages=1)
    assert ToolRegistry.get("convert_to_pdf").run([bad_exe], tmp_path / "o.pdf", {}).success is False
    assert ToolRegistry.get("convert_from_pdf").run([src], tmp_path / "o.exe", {"target_format": "exe"}).success is False


def test_fa08_t2_02_corrupt_image_in_images_to_pdf(tmp_path: Path) -> None:
    ToolRegistry = load_attr("pdf_lector.core.tools", "ToolRegistry")
    imgs = make_sample_images(tmp_path / "imgs")
    out_pdf = tmp_path / "out.pdf"
    res = ToolRegistry.get("images_to_pdf").run([imgs["corrupt_png"]], out_pdf, {})
    assert res.success is False
    assert not out_pdf.exists()


def test_fa08_t2_03_blank_page_ocr_completes_gracefully(tmp_path: Path) -> None:
    ToolRegistry = load_attr("pdf_lector.core.tools", "ToolRegistry")
    blank = make_blank_only_pdf(tmp_path / "blank.pdf")
    out_pdf = tmp_path / "ocr_blank.pdf"
    res = ToolRegistry.get("pdf_ocr").run(
        [blank], out_pdf, {"dpi": 150, "source_lang": "es", "ocr_provider": lambda _i, _l: ""}
    )
    assert res.success is True and out_pdf.exists()


def test_fa08_t2_04_ocr_no_backend_available_returns_clear_failure(tmp_path: Path) -> None:
    ToolRegistry = load_attr("pdf_lector.core.tools", "ToolRegistry")
    scan = make_scanned_text_pdf(tmp_path / "scan.pdf")
    res = ToolRegistry.get("pdf_ocr").run(
        [scan], tmp_path / "ocr_fail.pdf", {"dpi": 150, "source_lang": "es", "ocr_provider": None, "disable_winrt_probe": True}
    )
    assert res.success is False


def test_fa08_t2_05_zero_region_capture_and_no_scanner_device(tmp_path: Path) -> None:
    ToolRegistry = load_attr("pdf_lector.core.tools", "ToolRegistry")
    r_cap = ToolRegistry.get("capture_screen").run([], tmp_path / "cap.pdf", {"region": (0, 0, 0, 0)})
    r_scn = ToolRegistry.get("import_scanner_camera").run(
        [], tmp_path / "scn.pdf", {"device_acquire_fn": lambda: None}
    )
    assert r_cap.success is False
    assert r_scn.success is False


# ---------------------------------------------------------------------------
# FA-09: Dual-Profile Invoices (Boundary / Negative / Security)
# ---------------------------------------------------------------------------

def test_fa09_t2_01_validate_einvoice_rejects_invalid_dian_xml(tmp_path: Path) -> None:
    ToolRegistry = load_attr("pdf_lector.core.tools", "ToolRegistry")
    bad_xml = make_invoice_xml(tmp_path / "dian_bad.xml", profile="dian", valid=False)
    res = ToolRegistry.get("validate_einvoice").run([bad_xml], tmp_path / "rep.json", {})
    assert res.success is False or res.details.get("valid") is False


def test_fa09_t2_02_validate_einvoice_rejects_facturx_missing_xml(tmp_path: Path) -> None:
    ToolRegistry = load_attr("pdf_lector.core.tools", "ToolRegistry")
    bad_fx = make_facturx_pdfa3(tmp_path / "fx_bad.pdf", valid=False)
    res = ToolRegistry.get("validate_einvoice").run([bad_fx], tmp_path / "rep.json", {})
    assert res.success is False or res.details.get("valid") is False


def test_fa09_t2_03_pdf_to_einvoice_blocks_online_without_consent(tmp_path: Path, local_mock_server) -> None:
    ToolRegistry = load_attr("pdf_lector.core.tools", "ToolRegistry")
    inv_pdf = make_invoice_pdf(tmp_path / "inv.pdf", profile="dian")
    res = ToolRegistry.get("pdf_to_einvoice").run(
        [inv_pdf],
        tmp_path / "out.xml",
        {
            "mode": "online",
            "consent_granted": False,
            "endpoint_url": f"{local_mock_server.base_url}/einvoice/extract",
        },
    )
    assert res.success is False
    assert local_mock_server.requests_log == []


def test_fa09_t2_04_invoice_negative_price_or_excessive_tax_rejected(tmp_path: Path) -> None:
    ToolRegistry = load_attr("pdf_lector.core.tools", "ToolRegistry")
    res = ToolRegistry.get("create_einvoice").run(
        [],
        tmp_path,
        {
            "profile": "dian_ubl21",
            "emitter_nit": "900123456",
            "items": [{"description": "Bad", "qty": 0, "unit_price": -100.0, "iva_rate": 1.5}],
        },
    )
    assert res.success is False


def test_fa09_t2_05_validate_einvoice_xxe_payload_safe(tmp_path: Path) -> None:
    ToolRegistry = load_attr("pdf_lector.core.tools", "ToolRegistry")
    xxe_xml = make_invoice_xml(tmp_path / "xxe.xml", profile="dian", valid=True, xxe=True)
    res = ToolRegistry.get("validate_einvoice").run([xxe_xml], tmp_path / "rep.json", {})
    assert res.success is False or res.details.get("valid") is False


# ---------------------------------------------------------------------------
# FA-10: Optimize, Repair, Security & View/Check (Boundary / Negative)
# ---------------------------------------------------------------------------

def test_fa10_t2_01_unlock_wrong_password_and_protect_empty_password(tmp_path: Path) -> None:
    ToolRegistry = load_attr("pdf_lector.core.tools", "ToolRegistry")
    enc = make_encrypted_aes256_pdf(tmp_path / "enc.pdf")
    plain = make_multipage_toc_pdf(tmp_path / "plain.pdf", pages=1)
    assert ToolRegistry.get("unlock_pdf").run([enc], tmp_path / "u.pdf", {"password": "wrong"}).success is False
    assert ToolRegistry.get("protect_pdf").run([plain], tmp_path / "p.pdf", {"user_password": "", "owner_password": ""}).success is False


def test_fa10_t2_02_sign_pdf_corrupt_p12_and_wrong_passphrase(tmp_path: Path) -> None:
    ToolRegistry = load_attr("pdf_lector.core.tools", "ToolRegistry")
    plain = make_multipage_toc_pdf(tmp_path / "plain.pdf", pages=1)
    bad_p12 = tmp_path / "bad.p12"
    bad_p12.write_bytes(b"NOT_A_PKCS12_BLOB")
    good_p12 = make_self_signed_pkcs12(tmp_path / "good.p12", password="secret")

    assert ToolRegistry.get("sign_pdf").run([plain], tmp_path / "s1.pdf", {"p12_path": str(bad_p12), "password": "x"}).success is False
    assert ToolRegistry.get("sign_pdf").run([plain], tmp_path / "s2.pdf", {"p12_path": str(good_p12), "password": "wrong"}).success is False


def test_fa10_t2_03_repair_pdf_on_irrecoverable_garbage(tmp_path: Path) -> None:
    ToolRegistry = load_attr("pdf_lector.core.tools", "ToolRegistry")
    corrupt = make_corrupt_pdf(tmp_path / "garbage.pdf")
    assert ToolRegistry.get("repair_pdf").run([corrupt], tmp_path / "out.pdf", {}).success is False


def test_fa10_t2_04_compare_pdf_page_mismatch_and_missing_second_input(tmp_path: Path) -> None:
    ToolRegistry = load_attr("pdf_lector.core.tools", "ToolRegistry")
    p4 = make_multipage_toc_pdf(tmp_path / "p4.pdf", pages=4)
    p2 = make_multipage_toc_pdf(tmp_path / "p2.pdf", pages=2)
    diff = ToolRegistry.get("compare_pdf").run([p4, p2], tmp_path / "diff.json", {})
    assert diff.success is True and diff.details.get("has_differences") is True
    missing = ToolRegistry.get("compare_pdf").run([p4], tmp_path / "err.json", {})
    assert missing.success is False


def test_fa10_t2_05_redact_pdf_invalid_rect_and_out_of_range_page(tmp_path: Path) -> None:
    ToolRegistry = load_attr("pdf_lector.core.tools", "ToolRegistry")
    plain = make_multipage_toc_pdf(tmp_path / "plain.pdf", pages=1)
    r1 = ToolRegistry.get("redact_pdf").run([plain], tmp_path / "r1.pdf", {"targets": [{"page_index": 99, "rect": [10, 10, 50, 50]}]})
    r2 = ToolRegistry.get("redact_pdf").run([plain], tmp_path / "r2.pdf", {"targets": [{"page_index": 0, "rect": [0, 0, 0, 0]}]})
    assert r1.success is False
    assert r2.success is False


def test_fa10_t2_06_non_destructive_redaction_regression_guard(tmp_path: Path) -> None:
    ToolRegistry = load_attr("pdf_lector.core.tools", "ToolRegistry")
    from tests.e2e.fixtures import make_redaction_target_pdf

    secret = "SECRET_TOKEN_XYZ"
    src = make_redaction_target_pdf(tmp_path / "xobj_inc.pdf", secret=secret)
    out = tmp_path / "redacted.pdf"
    res = ToolRegistry.get("redact_pdf").run(
        [src],
        out,
        {"targets": [{"page_index": 0, "rect": [60, 620, 420, 675]}], "search_terms": [secret]},
    )
    assert res.success is True
    assert secret.encode("ascii") not in out.read_bytes()


# ---------------------------------------------------------------------------
# FA-11: Markdown Engine & md_security (Boundary / Security)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "malicious_md,forbidden_token",
    [
        ("<script>alert(1)</script>", "<script"),
        ('<img src="local.png" onerror="alert(1)">', "onerror="),
        ('<iframe src="https://evil.invalid"></iframe>', "<iframe"),
        ("[click](javascript:alert(1))", "javascript:"),
        ("![tracker](https://example.com/pixel.png)", "https://example.com"),
        (r"![unc](\\192.168.1.50\share\leak.png)", "192.168.1.50"),
        ('<svg onload="alert(1)"></svg>', "onload="),
        ('<object data="javascript:alert(1)"></object>', "<object"),
        ('<meta http-equiv="refresh" content="0;url=https://evil.invalid">', "<meta"),
        ("[bad](vbscript:msgbox(1))", "vbscript:"),
    ],
)
def test_fa11_t2_01_xss_and_unsafe_resources_blocked(tmp_path: Path, malicious_md: str, forbidden_token: str) -> None:
    md_eng = load_attr("pdf_lector.core.markdown_engine")
    html, blocked = md_eng.render_markdown_to_safe_html(malicious_md, base_dir=tmp_path, allow_remote_images=False)
    assert forbidden_token not in html.lower()
    if "https://" in malicious_md or "192.168.1.50" in malicious_md or "javascript:" in malicious_md:
        assert len(blocked) >= 1


def test_fa11_t2_02_path_traversal_file_and_data_uri_blocked(tmp_path: Path) -> None:
    md_eng = load_attr("pdf_lector.core.markdown_engine")
    adv = make_adversarial_markdown(tmp_path / "adv")
    html, blocked = md_eng.render_markdown_to_safe_html(
        adv["traversal"].read_text(encoding="utf-8"),
        base_dir=tmp_path / "adv",
        allow_remote_images=False,
    )
    assert "outside_secret.png" not in html
    assert "win.ini" not in html.lower()
    assert len(blocked) >= 3


def test_fa11_t2_03_empty_and_headingless_markdown_safe(tmp_path: Path) -> None:
    md_eng = load_attr("pdf_lector.core.markdown_engine")
    adv = make_adversarial_markdown(tmp_path / "adv")
    assert md_eng.parse_markdown_toc("") == []
    html, blocked = md_eng.render_markdown_to_safe_html("", base_dir=tmp_path)
    assert isinstance(html, str) and blocked == []
    out_pdf = tmp_path / "empty_md.pdf"
    md_eng.markdown_to_pdf(adv["empty"], out_pdf)
    assert out_pdf.exists()


def test_fa11_t2_04_huge_markdown_with_unclosed_fence_completes(tmp_path: Path) -> None:
    md_eng = load_attr("pdf_lector.core.markdown_engine")
    adv = make_adversarial_markdown(tmp_path / "adv")
    txt = adv["huge"].read_text(encoding="utf-8")
    toc = md_eng.parse_markdown_toc(txt)
    assert len(toc) >= 500
    html, _ = md_eng.render_markdown_to_safe_html(txt, base_dir=tmp_path / "adv")
    assert len(html) > 1000


def test_fa11_t2_05_non_markdown_extension_rejected_by_markdown_to_pdf(tmp_path: Path) -> None:
    md_eng = load_attr("pdf_lector.core.markdown_engine")
    bad = tmp_path / "bad.exe"
    bad.write_bytes(b"MZ")
    with pytest.raises((ValueError, OSError)):
        md_eng.markdown_to_pdf(bad, tmp_path / "out.pdf")


# ---------------------------------------------------------------------------
# FA-12: PDFium Text-Quad / Hit-Testing (Boundary / Negative)
# ---------------------------------------------------------------------------

def test_fa12_t2_01_margin_whitespace_hit_test_returns_none(tmp_path: Path) -> None:
    PdfDocumentSession = load_attr("pdf_lector.core.pdf_engine", "PdfDocumentSession")
    session = PdfDocumentSession(make_multipage_toc_pdf(tmp_path / "s.pdf", pages=1))
    try:
        assert session.hit_test_word(0, 5.0, 5.0) is None
    finally:
        session.close()


def test_fa12_t2_02_image_only_blank_page_hit_test_empty(tmp_path: Path) -> None:
    PdfDocumentSession = load_attr("pdf_lector.core.pdf_engine", "PdfDocumentSession")
    session = PdfDocumentSession(make_scanned_text_pdf(tmp_path / "scan.pdf"))
    try:
        assert session.get_page_word_boxes(0) == []
        assert session.hit_test_word(0, 200.0, 400.0) is None
    finally:
        session.close()


def test_fa12_t2_03_out_of_bounds_coordinates_safe(tmp_path: Path) -> None:
    PdfDocumentSession = load_attr("pdf_lector.core.pdf_engine", "PdfDocumentSession")
    session = PdfDocumentSession(make_multipage_toc_pdf(tmp_path / "s.pdf", pages=1))
    try:
        assert session.hit_test_word(0, -50.0, -50.0) is None
        assert session.hit_test_word(0, 99999.0, 99999.0) is None
    finally:
        session.close()


def test_fa12_t2_04_out_of_range_page_index_handled(tmp_path: Path) -> None:
    PdfDocumentSession = load_attr("pdf_lector.core.pdf_engine", "PdfDocumentSession")
    session = PdfDocumentSession(make_multipage_toc_pdf(tmp_path / "s.pdf", pages=1))
    try:
        assert session.hit_test_word(-1, 100.0, 100.0) is None
        assert session.hit_test_word(999, 100.0, 100.0) is None
    finally:
        session.close()


def test_fa12_t2_05_closed_session_guard_raises_runtime_error(tmp_path: Path) -> None:
    PdfDocumentSession = load_attr("pdf_lector.core.pdf_engine", "PdfDocumentSession")
    session = PdfDocumentSession(make_multipage_toc_pdf(tmp_path / "s.pdf", pages=1))
    session.close()
    with pytest.raises(RuntimeError):
        session.hit_test_word(0, 100.0, 100.0)


# ---------------------------------------------------------------------------
# FA-13: ISO 32000 Annotations Persistence (Boundary / Negative)
# ---------------------------------------------------------------------------

def test_fa13_t2_01_unc_path_rejected_in_annotations(tmp_path: Path) -> None:
    del tmp_path
    pa = load_attr("pdf_lector.core.pdf_annotations")
    with pytest.raises(ValueError):
        pa.load_pdf_annotations(Path(r"\\192.168.1.9\share\doc.pdf"))


def test_fa13_t2_02_corrupt_pdf_rejected_without_crash(tmp_path: Path) -> None:
    pa = load_attr("pdf_lector.core.pdf_annotations")
    corrupt = make_corrupt_pdf(tmp_path / "bad.pdf")
    with pytest.raises((ValueError, OSError, RuntimeError)):
        pa.load_pdf_annotations(corrupt)


def test_fa13_t2_03_invalid_page_index_or_empty_ink_rejected(tmp_path: Path) -> None:
    pa = load_attr("pdf_lector.core.pdf_annotations")
    pdf_p = make_multipage_toc_pdf(tmp_path / "p.pdf", pages=1)
    bad_rec = pa.AnnotationRecord(
        id="bad-1",
        page_index=99,
        kind="ink",
        rect_pdf=(0.0, 0.0, 0.0, 0.0),
        points_pdf=[],
        color_hex="#FEF08A",
        author="An\u00f3nimo",
        timestamp_iso="2026-11-24T12:00:00Z",
        content="",
        parent_id=None,
    )
    with pytest.raises(ValueError):
        pa.save_pdf_annotations(pdf_p, [bad_rec], create_backup=False)


def test_fa13_t2_04_invalid_color_hex_handled_safely(tmp_path: Path) -> None:
    pa = load_attr("pdf_lector.core.pdf_annotations")
    pdf_p = make_multipage_toc_pdf(tmp_path / "col.pdf", pages=1)
    rec = pa.AnnotationRecord(
        id="col-1",
        page_index=0,
        kind="highlight",
        rect_pdf=(72.0, 680.0, 180.0, 695.0),
        points_pdf=(),
        color_hex="#ZZZZZZ",
        author="An\u00f3nimo",
        timestamp_iso="2026-11-24T12:00:00Z",
        content="",
        parent_id=None,
    )
    saved = pa.save_pdf_annotations(pdf_p, [rec], create_backup=False)
    loaded = pa.load_pdf_annotations(saved)
    assert len(loaded) == 1 and re.fullmatch(r"#[0-9A-Fa-f]{6}", loaded[0].color_hex)


def test_fa13_t2_05_encrypted_pdf_cannot_save_annotations_without_unlock(tmp_path: Path) -> None:
    pa = load_attr("pdf_lector.core.pdf_annotations")
    enc = make_encrypted_aes256_pdf(tmp_path / "enc.pdf")
    rec = pa.AnnotationRecord(
        id="e-1",
        page_index=0,
        kind="text",
        rect_pdf=(72.0, 500.0, 96.0, 524.0),
        points_pdf=(),
        color_hex="#FEF08A",
        author="An\u00f3nimo",
        timestamp_iso="2026-11-24T12:00:00Z",
        content="Blocked",
        parent_id=None,
    )
    with pytest.raises((PermissionError, ValueError, RuntimeError)):
        pa.save_pdf_annotations(enc, [rec], create_backup=False)


# ---------------------------------------------------------------------------
# FA-14: Interactive PDF Canvas (Boundary / Negative)
# ---------------------------------------------------------------------------

def test_fa14_t2_01_palm_rejection_blocks_touch_when_stylus_active(tmp_path: Path, qapp: QApplication) -> None:
    del qapp
    PdfCanvas = load_attr("pdf_lector.ui.pdf_canvas", "PdfCanvas")
    PdfDocumentSession = load_attr("pdf_lector.core.pdf_engine", "PdfDocumentSession")
    session = PdfDocumentSession(make_multipage_toc_pdf(tmp_path / "p.pdf", pages=1))
    canvas_w = PdfCanvas()
    try:
        canvas_w.set_session(session, page_index=0)
        canvas_w.set_touch_writing_enabled(True)
        canvas_w.set_stylus_in_proximity(True)
        canvas_w.handle_touch_points([(QPointF(100.0, 100.0), 1), (QPointF(150.0, 150.0), 1)], phase="draw")
        assert len([a for a in canvas_w.annotations() if a.kind == "ink"]) == 0
    finally:
        canvas_w.close()
        session.close()


def test_fa14_t2_02_undo_on_empty_stack_is_noop(tmp_path: Path, qapp: QApplication) -> None:
    del qapp
    PdfCanvas = load_attr("pdf_lector.ui.pdf_canvas", "PdfCanvas")
    PdfDocumentSession = load_attr("pdf_lector.core.pdf_engine", "PdfDocumentSession")
    session = PdfDocumentSession(make_multipage_toc_pdf(tmp_path / "p.pdf", pages=1))
    canvas_w = PdfCanvas()
    try:
        canvas_w.set_session(session, page_index=0)
        canvas_w.undo_last_annotation()
        assert canvas_w.annotations() == []
    finally:
        canvas_w.close()
        session.close()


def test_fa14_t2_03_single_point_zero_length_stroke_discarded(tmp_path: Path, qapp: QApplication) -> None:
    del qapp
    PdfCanvas = load_attr("pdf_lector.ui.pdf_canvas", "PdfCanvas")
    PdfDocumentSession = load_attr("pdf_lector.core.pdf_engine", "PdfDocumentSession")
    session = PdfDocumentSession(make_multipage_toc_pdf(tmp_path / "p.pdf", pages=1))
    canvas_w = PdfCanvas()
    try:
        canvas_w.set_session(session, page_index=0)
        canvas_w.record_ink_stroke([(120.0, 220.0, 0.5)])
        assert len([a for a in canvas_w.annotations() if a.kind == "ink"]) == 0
    finally:
        canvas_w.close()
        session.close()


def test_fa14_t2_04_rotated_and_zoomed_canvas_coordinates_mapped(tmp_path: Path, qapp: QApplication) -> None:
    del qapp
    PdfCanvas = load_attr("pdf_lector.ui.pdf_canvas", "PdfCanvas")
    PdfDocumentSession = load_attr("pdf_lector.core.pdf_engine", "PdfDocumentSession")
    session = PdfDocumentSession(make_multipage_toc_pdf(tmp_path / "p.pdf", pages=1))
    canvas_w = PdfCanvas()
    try:
        canvas_w.set_session(session, page_index=0)
        canvas_w.set_rotation_and_zoom(rotation=90, zoom=1.75)
        rec = canvas_w.add_highlight_rect((72.0, 650.0, 180.0, 670.0), color_hex="#FEF08A")
        assert rec.rect_pdf[2] > rec.rect_pdf[0]
    finally:
        canvas_w.close()
        session.close()


def test_fa14_t2_05_rapid_mode_switching_no_errors(tmp_path: Path, qapp: QApplication) -> None:
    PdfCanvas = load_attr("pdf_lector.ui.pdf_canvas", "PdfCanvas")
    PdfDocumentSession = load_attr("pdf_lector.core.pdf_engine", "PdfDocumentSession")
    session = PdfDocumentSession(make_multipage_toc_pdf(tmp_path / "p.pdf", pages=1))
    canvas_w = PdfCanvas()
    try:
        canvas_w.set_session(session, page_index=0)
        for mode in ("select", "highlight", "note", "ink", "eraser", "select"):
            canvas_w.set_tool_mode(mode)
            qapp.processEvents()
            assert canvas_w.tool_mode() == mode
        assert canvas_w.tool_mode() == "select"
        assert session.page_count == 1
    finally:
        canvas_w.close()
        session.close()


# ---------------------------------------------------------------------------
# FA-15: Mixed PDF+MD Viewer (Boundary / Negative)
# ---------------------------------------------------------------------------

def test_fa15_t2_01_duplicate_path_focuses_existing_tab(tmp_path: Path, make_isolated_window) -> None:
    win = make_isolated_window()
    md_p = make_gfm_markdown_bundle(tmp_path / "md")["valid_md"]
    win.open_document_path(md_p)
    count_after_first = win.viewer_view.tabs.count()
    win.open_document_path(md_p)
    assert win.viewer_view.tabs.count() == count_after_first


def test_fa15_t2_02_dirty_markdown_tab_tracks_unsaved_state(tmp_path: Path, make_isolated_window) -> None:
    win = make_isolated_window()
    md_p = make_gfm_markdown_bundle(tmp_path / "md")["valid_md"]
    win.open_document_path(md_p)
    assert win.has_unsaved_changes() is False
    tab = win.viewer_view.current_tab()
    tab.insert_markdown_snippet("\nUnsaved edit\n")
    assert win.has_unsaved_changes() is True


def test_fa15_t2_03_unsafe_unc_or_exe_rejected_by_open_document_path(tmp_path: Path, make_isolated_window) -> None:
    win = make_isolated_window()
    before = win.viewer_view.tabs.count()
    bad_exe = tmp_path / "malware.exe"
    bad_exe.write_bytes(b"MZ")
    assert win.open_document_path(Path(r"\\evil\share\doc.md")) is False
    assert win.open_document_path(bad_exe) is False
    assert win.open_document_path(tmp_path / "missing.md") is False
    assert win.viewer_view.tabs.count() == before


def test_fa15_t2_04_empty_and_headingless_markdown_tabs_open_cleanly(tmp_path: Path, make_isolated_window) -> None:
    win = make_isolated_window()
    adv = make_adversarial_markdown(tmp_path / "adv")
    assert win.open_document_path(adv["empty"]) is True
    tab = win.viewer_view.current_tab()
    assert tab.toc_tree.topLevelItemCount() == 0


def test_fa15_t2_05_external_links_disabled_in_markdown_preview(tmp_path: Path, make_isolated_window) -> None:
    win = make_isolated_window()
    md_p = make_gfm_markdown_bundle(tmp_path / "md")["valid_md"]
    win.open_document_path(md_p)
    tab = win.viewer_view.current_tab()
    assert tab.preview_browser.openExternalLinks() is False


# ---------------------------------------------------------------------------
# FA-16: Kindle-Style Dictionary (Boundary / Negative)
# ---------------------------------------------------------------------------

def test_fa16_t2_01_unc_and_remote_json_glossary_blocked(tmp_path: Path) -> None:
    DictionaryEngine = load_attr("pdf_lector.core.dictionary_engine", "DictionaryEngine")
    eng = DictionaryEngine(base_dir=tmp_path / "dicts")
    with pytest.raises(ValueError, match="network_or_url_blocked"):
        eng.import_local_glossary("fr", Path(r"\\evil\share\bad.json"))
    with pytest.raises(ValueError, match="network_or_url_blocked"):
        eng.import_local_glossary("fr", "https://evil.invalid/bad.json")  # type: ignore[arg-type]


def test_fa16_t2_02_malformed_and_empty_glossary_json_rejected(tmp_path: Path) -> None:
    DictionaryEngine = load_attr("pdf_lector.core.dictionary_engine", "DictionaryEngine")
    eng = DictionaryEngine(base_dir=tmp_path / "dicts")
    bad = make_local_glossary_json(tmp_path / "bad.json", fmt="malformed")
    empty = make_local_glossary_json(tmp_path / "empty.json", fmt="empty")
    with pytest.raises((ValueError, json.JSONDecodeError)):
        eng.import_local_glossary("fr", bad)
    with pytest.raises(ValueError, match="empty_glossary"):
        eng.import_local_glossary("fr", empty)


def test_fa16_t2_03_closing_detached_dictionary_syncs_toggle_button(
    tmp_path: Path,
    make_isolated_window,
    qapp: QApplication,
) -> None:
    win = make_isolated_window()
    win.open_document_path(make_multipage_toc_pdf(tmp_path / "d.pdf", pages=1))
    win.show()
    win.switch_view(1, immediate=True)
    win.viewer_view.set_dictionary_visible(True)
    win.viewer_view.detach_dictionary()
    qapp.processEvents()
    win.viewer_view.close_detached_dictionary()
    qapp.processEvents()
    assert win.viewer_view.dict_btn.isChecked() is False
    # Reopen cleanly without Shiboken6 deleted-object error
    win.viewer_view.set_dictionary_visible(True)
    qapp.processEvents()
    assert win.viewer_view.dict_panel.isVisible() is True


def test_fa16_t2_04_bundled_es_en_removal_protected(tmp_path: Path) -> None:
    DictionaryEngine = load_attr("pdf_lector.core.dictionary_engine", "DictionaryEngine")
    eng = DictionaryEngine(base_dir=tmp_path / "dicts")
    assert eng.remove_language_pack("es") is False
    assert eng.remove_language_pack("en") is False
    assert "es" in eng.installed_languages() and "en" in eng.installed_languages()


def test_fa16_t2_05_unknown_word_returns_empty_and_suggests(tmp_path: Path) -> None:
    DictionaryEngine = load_attr("pdf_lector.core.dictionary_engine", "DictionaryEngine")
    eng = DictionaryEngine(base_dir=tmp_path / "dicts")
    assert eng.lookup("xyznonexistentword", "es") == []
    assert len(eng.suggest("priv", "es")) >= 1


# ---------------------------------------------------------------------------
# FA-17: WinOptiAI Widgets & DialogLocalizer (Boundary / Negative)
# ---------------------------------------------------------------------------

def test_fa17_t2_01_adaptive_button_extreme_narrow_container(qapp: QApplication) -> None:
    AdaptiveButton = load_attr("pdf_lector.ui.widgets", "AdaptiveButton")
    btn = AdaptiveButton()
    btn.set_variants(["Descargar e instalar actualizacion completa", "Instalar", "OK"])
    btn.resize(60, 32)
    qapp.processEvents()
    assert btn.text() == "OK"


def test_fa17_t2_02_dialog_localizer_warning_sets_risk_action_role(qapp: QApplication) -> None:
    DialogLocalizer = load_attr("pdf_lector.ui.dialog_localizer", "DialogLocalizer")
    loc = DialogLocalizer(qapp)
    loc.set_language("es")
    box = QMessageBox(QMessageBox.Icon.Warning, "Alerta", "Confirmar", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
    loc.localize_dialog(box)
    assert box.button(QMessageBox.StandardButton.Yes).property("role") == "RiskAction"


def test_fa17_t2_03_tour_dialog_deleted_mid_callback_guarded(qapp: QApplication) -> None:
    shiboken6 = load_attr("shiboken6")
    TourDialog = load_attr("pdf_lector.ui.tour_dialog", "TourDialog")
    dlg = TourDialog(lang="es")
    dlg.close()
    dlg.deleteLater()
    qapp.processEvents()
    assert shiboken6.isValid(dlg) is False
    res = dlg.safe_refresh_step()
    assert res is None
    assert shiboken6.isValid(dlg) is False


def test_fa17_t2_04_unknown_language_fallback_to_es_in_dialog_localizer(qapp: QApplication) -> None:
    DialogLocalizer = load_attr("pdf_lector.ui.dialog_localizer", "DialogLocalizer")
    loc = DialogLocalizer(qapp)
    loc.set_language("xx-unknown")
    box = QMessageBox(QMessageBox.Icon.Information, "T", "M", QMessageBox.StandardButton.Yes)
    loc.localize_dialog(box)
    assert box.button(QMessageBox.StandardButton.Yes).text() == "S\u00ed"


def test_fa17_t2_05_status_tag_unknown_tone_and_zero_rgba(qapp: QApplication) -> None:
    del qapp
    StatusTag = load_attr("pdf_lector.ui.widgets", "StatusTag")
    tag = StatusTag("LOCAL", tone="unknown_tone")
    tag.apply_theme("dark")
    assert "rgba(" not in tag.styleSheet().lower()


# ---------------------------------------------------------------------------
# FA-18: SHA-256 Updater (Boundary / Negative / Tamper)
# ---------------------------------------------------------------------------

def test_fa18_t2_01_corrupt_binary_rejected_and_deleted(tmp_path: Path, local_mock_server) -> None:
    updater = load_attr("pdf_lector.core.updater")
    bundle = make_update_bundle(tmp_path / "rel_bad", version="0.2.0", tamper=True, base_url=local_mock_server.base_url)
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
    dest = tmp_path / "dl"
    with pytest.raises(ValueError, match="sha256"):
        updater.download_verified_update(
            asset_url=f"{local_mock_server.base_url}/releases/download/v0.2.0/PDF-MD-Lector-Setup.exe",
            sums_url=f"{local_mock_server.base_url}/releases/download/v0.2.0/SHA256SUMS.txt",
            dest_dir=dest,
        )
    assert not (dest / "PDF-MD-Lector-Setup.exe").exists()


def test_fa18_t2_02_missing_sha256sums_refuses_update(tmp_path: Path, local_mock_server) -> None:
    updater = load_attr("pdf_lector.core.updater")
    bundle = make_update_bundle(tmp_path / "rel", version="0.2.0", tamper=False, base_url=local_mock_server.base_url)
    local_mock_server.set_route("GET", "/setup.exe", 200, bundle["installer_bytes"], "application/octet-stream")
    with pytest.raises((ValueError, HTTPError, RuntimeError)):
        updater.download_verified_update(
            asset_url=f"{local_mock_server.base_url}/setup.exe",
            sums_url=f"{local_mock_server.base_url}/missing_SHA256SUMS.txt",
            dest_dir=tmp_path / "dl2",
        )


def test_fa18_t2_03_offline_only_toggle_makes_zero_network_calls(local_mock_server) -> None:
    updater = load_attr("pdf_lector.core.updater")
    assert updater.should_run_auto_check(last_update_check_utc=None, offline_only=True) is False
    res = updater.check_for_updates(
        "0.1.0",
        api_url=f"{local_mock_server.base_url}/releases/latest",
        offline_only=True,
    )
    assert res is None
    assert local_mock_server.requests_log == []


def test_fa18_t2_04_unsaved_changes_blocks_update_restart(tmp_path: Path, make_isolated_window) -> None:
    win = make_isolated_window()
    md_p = make_gfm_markdown_bundle(tmp_path / "md")["valid_md"]
    win.open_document_path(md_p)
    win.viewer_view.current_tab().insert_markdown_snippet("\nDirty before update\n")
    assert win.can_restart_for_update() is False


def test_fa18_t2_05_http_404_releases_endpoint_returns_none_safely(local_mock_server) -> None:
    updater = load_attr("pdf_lector.core.updater")
    res = updater.check_for_updates("0.1.0", api_url=f"{local_mock_server.base_url}/not-found")
    assert res is None


# ---------------------------------------------------------------------------
# FA-19: Hub, ToolWorkspace & App Dialogs (Boundary / Negative)
# ---------------------------------------------------------------------------

def test_fa19_t2_01_paused_insights_freezes_hub_quick_actions(make_isolated_window, tmp_path: Path) -> None:
    LocalUsageInsights = load_attr("pdf_lector.core.usage_insights", "LocalUsageInsights")
    insights = LocalUsageInsights(tmp_path / "ins_paused.json")
    win = make_isolated_window(insights_store=insights)
    insights.set_enabled(False)
    insights.record_tool_use("redact_pdf")
    win.hub_view.refresh_recommendations()
    assert win.hub_view.quick_action_tool_ids()[0] == "sign_pdf"


def test_fa19_t2_02_tool_workspace_rejects_invalid_dpi_and_unc(tmp_path: Path, qapp: QApplication) -> None:
    del qapp
    ToolRegistry = load_attr("pdf_lector.core.tools", "ToolRegistry")
    ToolWorkspace = load_attr("pdf_lector.ui.tool_workspace", "ToolWorkspace")
    ws = ToolWorkspace(ToolRegistry.get("pdf_ocr"))
    try:
        assert ws.add_input_path(Path(r"\\evil\share\scan.pdf")) is False
        assert ws.validate_params({"dpi": 9999, "quality": 150}) is False
    finally:
        ws.close()
    del tmp_path


def test_fa19_t2_03_hub_search_zero_matches_shows_empty_label(make_isolated_window, qapp: QApplication) -> None:
    win = make_isolated_window()
    win.show()
    win.hub_view.set_search_query("zzzznonexistenttool999")
    qapp.processEvents()
    assert win.hub_view.empty_lbl.isVisible() is True


def test_fa19_t2_04_support_dialog_cancel_makes_zero_network_calls(local_mock_server, qapp: QApplication) -> None:
    SupportDialog = load_attr("pdf_lector.ui.support_dialog", "SupportDialog")
    dlg = SupportDialog(endpoint_url=f"{local_mock_server.base_url}/submit")
    try:
        dlg.show()
        qapp.processEvents()
        dlg.reject()
        assert local_mock_server.requests_log == []
    finally:
        dlg.close()


def test_fa19_t2_05_preferences_dialog_defaults_anonymous_author(qapp: QApplication, isolated_appdata: Path) -> None:
    del qapp, isolated_appdata
    PreferencesDialog = load_attr("pdf_lector.ui.preferences_dialog", "PreferencesDialog")
    dlg = PreferencesDialog()
    try:
        assert dlg.default_annotation_author() == "An\u00f3nimo"
    finally:
        dlg.close()


# ---------------------------------------------------------------------------
# FA-20: Theme Studio & MainWindow (Boundary / Negative)
# ---------------------------------------------------------------------------

def test_fa20_t2_01_corrupt_theme_package_rejected_cleanly(tmp_path: Path) -> None:
    tp_mod = load_attr("pdf_lector.theme.theme_profile")
    c1 = make_theme_package(tmp_path / "t1", kind="corrupt_json")
    c2 = make_theme_package(tmp_path / "t2", kind="corrupt_list")
    with pytest.raises((ValueError, json.JSONDecodeError)):
        tp_mod.import_theme_package(c1)
    with pytest.raises(ValueError):
        tp_mod.import_theme_package(c2)


def test_fa20_t2_02_remote_and_unc_banners_blocked_in_theme_import(tmp_path: Path) -> None:
    tp_mod = load_attr("pdf_lector.theme.theme_profile")
    for kind in ("unsafe_remote", "unsafe_unc"):
        pkg = make_theme_package(tmp_path / kind, kind=kind)
        prof, warning = tp_mod.import_theme_package(pkg)
        assert prof.banner_path == ""
        assert warning == "network_or_url_blocked"
        assert prof.opacity <= 0.85 and prof.blur_radius <= 24 and prof.fps_cap <= 60


def test_fa20_t2_03_theme_studio_rejects_oversized_or_bad_extension_banner(tmp_path: Path, make_isolated_window) -> None:
    win = make_isolated_window()
    bad = tmp_path / "bad.svg"
    bad.write_text("<svg></svg>", encoding="utf-8")
    assert win.studio_view.apply_banner_path(str(bad)) is False


def test_fa20_t2_04_motion_performance_guard_all_pause_states() -> None:
    tp_mod = load_attr("pdf_lector.theme.theme_profile")
    me_mod = load_attr("pdf_lector.theme.motion_engine")
    modern = tp_mod.get_preset("trodai_mesh")
    assert me_mod.MotionPerformanceGuard(os_animations_fn=lambda: True, battery_saver_fn=lambda: True).evaluate(modern, window_visible=True).status_code == "battery"
    assert me_mod.MotionPerformanceGuard(os_animations_fn=lambda: False, battery_saver_fn=lambda: False).evaluate(modern, window_visible=True).status_code == "os_off"
    assert me_mod.MotionPerformanceGuard(os_animations_fn=lambda: True, battery_saver_fn=lambda: False).evaluate(modern, window_visible=False).status_code == "hidden"
    assert me_mod.MotionPerformanceGuard(os_animations_fn=lambda: True, battery_saver_fn=lambda: False).evaluate(modern, window_visible=True, heavy_task_active=True).status_code == "paused"


def test_fa20_t2_05_splash_progress_clamped_0_to_100(qapp: QApplication) -> None:
    del qapp
    SplashScreen = load_attr("pdf_lector.ui.splash_screen", "SplashScreen")
    splash = SplashScreen()
    try:
        splash.set_progress(-25, "Low")
        assert splash.progress_value == 0
        splash.set_progress(250, "High")
        assert splash.progress_value == 100
    finally:
        splash.finish_and_close()


# ---------------------------------------------------------------------------
# FA-21: Windows 11 HKCU & CLI (Boundary / Negative)
# ---------------------------------------------------------------------------

def test_fa21_t2_01_unregister_preserves_third_party_progid(fake_winreg) -> None:
    wi = load_attr("pdf_lector.core.windows_integration")
    with fake_winreg.CreateKeyEx(fake_winreg.HKEY_CURRENT_USER, r"Software\Classes\.pdf\OpenWithProgids") as k:
        fake_winreg.SetValueEx(k, "AcroExch.Document", 0, fake_winreg.REG_SZ, "")
    wi.register_windows_integration(
        exe_path=Path(r"C:\Program Files\TRODAI\PDF-MD-Lector.exe"),
        include_markdown=True,
        winreg_backend=fake_winreg,
    )
    wi.unregister_windows_integration(winreg_backend=fake_winreg)
    assert not fake_winreg.key_exists(r"Software\Classes\TRODAI.PDFMDLector.PDF")
    assert fake_winreg.get_value(r"Software\Classes\.pdf\OpenWithProgids", "AcroExch.Document") == ""


def test_fa21_t2_02_registry_permission_error_handled_cleanly(fake_winreg) -> None:
    wi = load_attr("pdf_lector.core.windows_integration")
    fake_winreg.fail_writes_with = PermissionError
    res = wi.register_windows_integration(
        exe_path=Path(r"C:\Program Files\TRODAI\PDF-MD-Lector.exe"),
        include_markdown=False,
        winreg_backend=fake_winreg,
    )
    assert res.success is False


def test_fa21_t2_03_cli_merge_missing_output_or_single_input_fails(tmp_path: Path, clean_subprocess_env) -> None:
    p1 = make_multipage_toc_pdf(tmp_path / "one.pdf", pages=1)
    proc = subprocess.run(
        [sys.executable, "-m", "pdf_lector.app", "--merge", str(p1)],
        capture_output=True,
        text=True,
        env=clean_subprocess_env,
        timeout=15,
    )
    assert proc.returncode != 0


@pytest.mark.parametrize("cli_flag", ["--merge", "--compress", "--ocr"])
def test_fa21_t2_04_cli_unc_path_rejected(clean_subprocess_env, tmp_path: Path, cli_flag: str) -> None:
    args = [sys.executable, "-m", "pdf_lector.app", cli_flag, r"\\10.0.0.1\share\secret.pdf"]
    if cli_flag == "--merge":
        args.append(r"\\10.0.0.1\share\secret2.pdf")
    args.extend(["-o", str(tmp_path / "o.pdf")])
    proc = subprocess.run(
        args,
        capture_output=True,
        text=True,
        env=clean_subprocess_env,
        timeout=15,
    )
    assert proc.returncode != 0


def test_fa21_t2_05_cli_missing_input_file_rejected(clean_subprocess_env, tmp_path: Path) -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "pdf_lector.app", "--ocr", str(tmp_path / "does_not_exist.pdf"), "-o", str(tmp_path / "o.pdf")],
        capture_output=True,
        text=True,
        env=clean_subprocess_env,
        timeout=15,
    )
    assert proc.returncode != 0


# ---------------------------------------------------------------------------
# FA-22: Native Messaging Host (Boundary / Negative / Security)
# ---------------------------------------------------------------------------

def test_fa22_t2_01_truncated_header_and_empty_eof_exit_cleanly(clean_subprocess_env) -> None:
    for raw in (b"", b"\x10\x00"):
        proc = subprocess.run(
            [sys.executable, "-m", "pdf_lector.core.native_messaging"],
            input=raw,
            capture_output=True,
            env=clean_subprocess_env,
            timeout=10,
        )
        assert proc.returncode == 0
        assert proc.stdout == b""


def test_fa22_t2_02_oversized_payload_over_1mb_rejected(clean_subprocess_env) -> None:
    bad_frame = struct.pack("<I", 2 * 1024 * 1024) + b"{}"
    proc = subprocess.run(
        [sys.executable, "-m", "pdf_lector.core.native_messaging"],
        input=bad_frame,
        capture_output=True,
        env=clean_subprocess_env,
        timeout=10,
    )
    if proc.stdout:
        msgs = decode_nm_stream(proc.stdout)
        assert msgs[0]["ok"] is False


def test_fa22_t2_03_malformed_json_body_returns_framed_error(clean_subprocess_env) -> None:
    bad_frame = encode_nm_frame(b"{bad json")
    proc = subprocess.run(
        [sys.executable, "-m", "pdf_lector.core.native_messaging"],
        input=bad_frame,
        capture_output=True,
        env=clean_subprocess_env,
        timeout=10,
    )
    msgs = decode_nm_stream(proc.stdout)
    assert msgs[0]["ok"] is False


def test_fa22_t2_04_unsafe_scheme_and_unc_path_rejected(clean_subprocess_env, tmp_path: Path) -> None:
    stream = encode_nm_frame({"action": "open_pdf", "path": r"\\evil\share\a.pdf"}) + encode_nm_frame(
        {"action": "webpage_to_pdf", "url": "javascript:alert(1)", "output": str(tmp_path / "x.pdf")}
    )
    proc = subprocess.run(
        [sys.executable, "-m", "pdf_lector.core.native_messaging"],
        input=stream,
        capture_output=True,
        env=clean_subprocess_env,
        timeout=10,
    )
    msgs = decode_nm_stream(proc.stdout)
    assert len(msgs) == 2 and all(m["ok"] is False for m in msgs)


def test_fa22_t2_05_unknown_action_rejected(clean_subprocess_env) -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "pdf_lector.core.native_messaging"],
        input=encode_nm_frame({"action": "exec_shell", "cmd": "whoami"}),
        capture_output=True,
        env=clean_subprocess_env,
        timeout=10,
    )
    msgs = decode_nm_stream(proc.stdout)
    assert msgs[0]["ok"] is False


# ---------------------------------------------------------------------------
# FA-23: Lazy Local REST API (Boundary / Negative / Security)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "auth_header",
    [None, "Bearer wrong-token", "Basic dGVzdA==", "Bearer "],
)
def test_fa23_t2_01_missing_or_invalid_bearer_returns_401(isolated_appdata: Path, auth_header: str | None) -> None:
    del isolated_appdata
    api_srv = load_attr("pdf_lector.api.server")
    secrets_store = load_attr("pdf_lector.core.secrets_store")
    secrets_store.set_secret("local_api_token", "valid-secret-token")

    srv = api_srv.LocalApiServer(host="127.0.0.1", port=0)
    srv.start()
    try:
        headers = {"Authorization": auth_header} if auth_header is not None else {}
        req = Request(f"{srv.base_url}/health", headers=headers)
        with pytest.raises(HTTPError) as exc_info:
            urlopen(req, timeout=5)
        assert exc_info.value.code == 401
    finally:
        srv.stop()


def test_fa23_t2_02_unc_and_traversal_in_api_body_rejected(tmp_path: Path, isolated_appdata: Path) -> None:
    del isolated_appdata
    api_srv = load_attr("pdf_lector.api.server")
    secrets_store = load_attr("pdf_lector.core.secrets_store")
    secrets_store.set_secret("local_api_token", "valid-api-tok")
    srv = api_srv.LocalApiServer(host="127.0.0.1", port=0)
    srv.start()
    try:
        body = json.dumps({"inputs": [r"\\evil\share\a.pdf"], "output": str(tmp_path / "out.pdf"), "params": {}}).encode("utf-8")
        req = Request(
            f"{srv.base_url}/tools/compress_pdf",
            data=body,
            headers={"Authorization": "Bearer valid-api-tok", "Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(HTTPError) as exc_info:
            urlopen(req, timeout=5)
        assert exc_info.value.code in (400, 403, 422)
    finally:
        srv.stop()


def test_fa23_t2_03_unknown_tool_returns_404(tmp_path: Path, isolated_appdata: Path) -> None:
    del isolated_appdata
    api_srv = load_attr("pdf_lector.api.server")
    secrets_store = load_attr("pdf_lector.core.secrets_store")
    secrets_store.set_secret("local_api_token", "valid-api-tok")
    srv = api_srv.LocalApiServer(host="127.0.0.1", port=0)
    srv.start()
    try:
        body = json.dumps({"inputs": [], "output": str(tmp_path / "o.pdf"), "params": {}}).encode("utf-8")
        req = Request(
            f"{srv.base_url}/tools/nonexistent_tool",
            data=body,
            headers={"Authorization": "Bearer valid-api-tok", "Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(HTTPError) as exc_info:
            urlopen(req, timeout=5)
        assert exc_info.value.code == 404
    finally:
        srv.stop()


def test_fa23_t2_04_invalid_dpi_param_rejected_422_or_400(tmp_path: Path, isolated_appdata: Path) -> None:
    del isolated_appdata
    api_srv = load_attr("pdf_lector.api.server")
    secrets_store = load_attr("pdf_lector.core.secrets_store")
    secrets_store.set_secret("local_api_token", "valid-api-tok")
    src = make_multipage_toc_pdf(tmp_path / "a.pdf", pages=1)
    srv = api_srv.LocalApiServer(host="127.0.0.1", port=0)
    srv.start()
    try:
        body = json.dumps({"inputs": [str(src)], "output": str(tmp_path / "o.pdf"), "params": {"dpi": 99999}}).encode("utf-8")
        req = Request(
            f"{srv.base_url}/tools/compress_pdf",
            data=body,
            headers={"Authorization": "Bearer valid-api-tok", "Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(HTTPError) as exc_info:
            urlopen(req, timeout=5)
        assert exc_info.value.code in (400, 422)
    finally:
        srv.stop()


def test_fa23_t2_05_non_loopback_bind_rejected(isolated_appdata: Path) -> None:
    del isolated_appdata
    api_srv = load_attr("pdf_lector.api.server")
    with pytest.raises(ValueError):
        api_srv.LocalApiServer(host="0.0.0.0", port=0)


# ---------------------------------------------------------------------------
# FA-24: AI "Ask Document" & MCP Server (Boundary / Negative / Security)
# ---------------------------------------------------------------------------

def test_fa24_t2_01_cloud_byok_blocked_without_consent(
    tmp_path: Path,
    isolated_appdata: Path,
    local_mock_server,
) -> None:
    del isolated_appdata
    ai_rag = load_attr("pdf_lector.core.ai_rag")
    secrets_store = load_attr("pdf_lector.core.secrets_store")
    secrets_store.set_secret("byok_openai_key", "sk-blocked-test")
    local_mock_server.set_route("POST", "/v1/chat/completions", 200, {"choices": [{"message": {"content": "leaked"}}]})

    idx = ai_rag.DocumentRagIndex()
    idx.index_pdf(make_multipage_toc_pdf(tmp_path / "p.pdf", pages=1))
    ans = ai_rag.ask_document(
        idx,
        "Pregunta sin consentimiento",
        provider="openai_compatible",
        endpoint_url=f"{local_mock_server.base_url}/v1/chat/completions",
        consent_granted=False,
    )
    assert ans.consent_blocked is True
    assert local_mock_server.requests_log == []


@pytest.mark.parametrize(
    "unsafe_path",
    [r"\\192.168.1.9\c$\secret.pdf", "../../Windows/System32/drivers/etc/hosts"],
)
def test_fa24_t2_02_mcp_rejects_unc_and_traversal_paths(clean_subprocess_env, unsafe_path: str) -> None:
    reqs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "read_pdf_text", "arguments": {"path": unsafe_path}}},
    ]
    proc = subprocess.run(
        [sys.executable, "-m", "pdf_lector.mcp_server"],
        input="\n".join(json.dumps(r) for r in reqs) + "\n",
        capture_output=True,
        text=True,
        env=clean_subprocess_env,
        timeout=15,
    )
    lines = [json.loads(line) for line in proc.stdout.strip().splitlines() if line.strip()]
    assert "error" in lines[1]


@pytest.mark.parametrize(
    "raw_line,expected_code",
    [
        ("{bad json", -32700),
        ('{"jsonrpc":"2.0","id":9,"method":"unknown/method","params":{}}', -32601),
        ('{"jsonrpc":"2.0","id":10,"params":{}}', -32600),
        ('{"jsonrpc":"2.0","id":11,"method":"tools/call","params":{"name":"unknown_mcp_tool","arguments":{}}}', -32602),
    ],
)
def test_fa24_t2_03_mcp_malformed_json_and_unknown_method(clean_subprocess_env, raw_line: str, expected_code: int) -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "pdf_lector.mcp_server"],
        input=raw_line + "\n",
        capture_output=True,
        text=True,
        env=clean_subprocess_env,
        timeout=15,
    )
    resp = json.loads(proc.stdout.strip().splitlines()[0])
    assert resp["error"]["code"] in (expected_code, -32601, -32602, -32600, -32700)


def test_fa24_t2_04_bm25_empty_or_scanned_doc_no_zerodivision(tmp_path: Path) -> None:
    ai_rag = load_attr("pdf_lector.core.ai_rag")
    idx = ai_rag.DocumentRagIndex()
    idx.index_pdf(make_scanned_text_pdf(tmp_path / "scan.pdf"))
    assert idx.search("cualquier consulta", top_k=5) == []


def test_fa24_t2_05_unreachable_byok_endpoint_does_not_leak_api_key(
    tmp_path: Path,
    isolated_appdata: Path,
    local_mock_server,
) -> None:
    del isolated_appdata
    ai_rag = load_attr("pdf_lector.core.ai_rag")
    secrets_store = load_attr("pdf_lector.core.secrets_store")
    secret_key = "sk-do-not-leak-in-error-message"
    secrets_store.set_secret("byok_openai_key", secret_key)
    local_mock_server.set_route("POST", "/v1/chat/completions", 401, {"error": "Unauthorized"})

    idx = ai_rag.DocumentRagIndex()
    idx.index_pdf(make_multipage_toc_pdf(tmp_path / "p.pdf", pages=1))
    ans = ai_rag.ask_document(
        idx,
        "Test error",
        provider="openai_compatible",
        endpoint_url=f"{local_mock_server.base_url}/v1/chat/completions",
        consent_granted=True,
    )
    assert secret_key not in ans.answer_text
    assert secret_key not in (ans.error or "")


# ---------------------------------------------------------------------------
# FA-25: Packaging, $0 Signing, CI & Docs (Boundary / Negative)
# ---------------------------------------------------------------------------

def test_fa25_t2_01_sign_local_dry_run_never_mutates_root_cert_store() -> None:
    ps1 = WORKSPACE_ROOT / "scripts" / "sign_local.ps1"
    snap_cmd = "Get-ChildItem Cert:\\CurrentUser\\Root | Select-Object -ExpandProperty Thumbprint"
    before = subprocess.run(["powershell", "-NoProfile", "-Command", snap_cmd], capture_output=True, text=True, timeout=15)
    run = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1), "-InstallTrust", "-DryRun"],
        capture_output=True,
        text=True,
        timeout=20,
    )
    after = subprocess.run(["powershell", "-NoProfile", "-Command", snap_cmd], capture_output=True, text=True, timeout=15)
    assert run.returncode == 0
    assert before.stdout == after.stdout


def test_fa25_t2_02_build_exe_excludes_qtwebengine() -> None:
    txt = (WORKSPACE_ROOT / "scripts" / "build_exe.py").read_text(encoding="utf-8")
    assert "QtWebEngineCore" in txt and "QtWebEngineWidgets" in txt


def test_fa25_t2_03_installer_iss_forbids_admin_only_and_user_paths() -> None:
    txt = (WORKSPACE_ROOT / "packaging" / "installer.iss").read_text(encoding="utf-8")
    assert "PrivilegesRequired=admin" not in txt
    assert "C:\\Users\\" not in txt


def test_fa25_t2_04_feedback_worker_and_src_contain_no_hardcoded_github_pat() -> None:
    pat_re = re.compile(r"(ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")
    for folder in (WORKSPACE_ROOT / "src", WORKSPACE_ROOT / "deploy" / "feedback-worker"):
        if folder.exists():
            for f in folder.rglob("*"):
                if f.is_file() and f.suffix in (".py", ".js", ".toml"):
                    assert not pat_re.search(f.read_text(encoding="utf-8")), f"Hardcoded PAT in {f}"


def test_fa25_t2_05_sha256sums_format_validation(tmp_path: Path) -> None:
    bundle = make_update_bundle(tmp_path / "sums", version="0.2.0")
    line_re = re.compile(r"^[0-9a-f]{64}  \S+$")
    for line in bundle["sums_text"].strip().splitlines():
        assert line_re.match(line)
    assert hashlib.sha256(bundle["installer_bytes"]).hexdigest() == bundle["expected_sha256"]
