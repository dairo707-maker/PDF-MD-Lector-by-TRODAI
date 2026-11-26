"""Comprehensive M1 Foundation, Dependencies, Security & Lifecycle verification suite."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
import tomllib
import uuid

import pikepdf
import pypdfium2 as pdfium
import pytest
from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer
from PySide6.QtWidgets import QApplication
from reportlab.pdfgen import canvas

import pdf_lector as pdf_lector_pkg
from pdf_lector import APP_NAME, APP_USER_MODEL_ID, __version__, get_base_dir, read_version
from pdf_lector.core.crash_guard import CrashGuard
from pdf_lector.core.feedback_reporter import (
    KIND_BUG,
    anonymize_text,
    build_clipboard_payload,
    build_feedback_report,
    build_github_issue_url,
    build_mailto_uri,
    save_local_report,
    submit_feedback_endpoint,
)
from pdf_lector.core.metadata_sanitizer import (
    SanitizeOptions,
    inspect_pdf_metadata,
    sanitize_pdf_metadata,
)
from pdf_lector.core.notifications import NotificationCenter
from pdf_lector.core.path_guard import (
    ALLOWED_MD_EXTENSIONS,
    resolve_safe_doc_resource,
    validate_local_any_doc,
    validate_local_glossary_path,
    validate_local_markdown_path,
)
from pdf_lector.core.redaction import (
    RedactionOptions,
    RedactionRegion,
    display_rect_to_pdf_rect,
    redact_pdf_regions,
    redact_pdf_terms,
    verify_redaction_clean,
)
from pdf_lector.core.secrets_store import SecretsStore, delete_secret, get_secret, set_secret
from pdf_lector.core.single_instance import SingleInstanceLock
from pdf_lector.core.task_control import (
    AsyncTask,
    SessionManager,
    TaskController,
    TaskManager,
    TaskPausedAndSaved,
    TaskSessionRecord,
    new_session_id,
    stop_point,
)
from pdf_lector.core.temp_manager import TempManager, secure_unlink
from pdf_lector.core.usage_insights import LocalUsageInsights
from pdf_lector.core.win32_bridge import (
    IS_WINDOWS,
    atomic_write_text,
    canonical_lock_key,
    dpapi_protect,
    dpapi_unprotect,
)

ROOT = get_base_dir()


@pytest.fixture(scope="session")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication(["pytest-m1"])


def _pump_until(predicate, timeout_s: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 20)
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


# ---------------------------------------------------------------------------
# Feature #1: Branding, Version & Dependencies
# ---------------------------------------------------------------------------


def test_m1_branding_version_and_pyproject() -> None:
    assert APP_NAME == "PDF/MD Lector"
    assert APP_USER_MODEL_ID == "TRODAI.PDFMDLector"
    assert __version__ == "0.2.0"
    assert read_version() == "0.2.0"
    assert (ROOT / "VERSION").read_text(encoding="utf-8").strip() == "0.2.0"

    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = pyproject["project"]
    assert project["version"] == "0.2.0"
    scripts = project["scripts"]
    assert scripts["pdf-md-lector"] == "pdf_lector.app:main"
    assert scripts["pdf-lector"] == "pdf_lector.app:main"
    assert scripts["pdf-md-lector-mcp"] == "pdf_lector.mcp_server:main"

    deps_joined = "\n".join(project["dependencies"]).lower()
    for expected in (
        "pikepdf",
        "pypdf",
        "pyhanko",
        "pdfplumber",
        "python-docx",
        "openpyxl",
        "markdown-it-py",
        "nh3",
        "fastapi",
        "uvicorn",
        "qrcode",
        "cryptography",
        "winrt-windows.media.ocr",
        "winrt-windows.graphics.imaging",
        "winrt-windows.storage.streams",
    ):
        assert expected in deps_joined, f"Missing runtime dependency: {expected}"

    dev_joined = "\n".join(project["optional-dependencies"]["dev"]).lower()
    assert "pyinstaller" in dev_joined

    for mod_name in (
        "pikepdf",
        "pypdf",
        "pyhanko",
        "pdfplumber",
        "docx",
        "openpyxl",
        "markdown_it",
        "nh3",
        "fastapi",
        "uvicorn",
        "qrcode",
        "cryptography",
        "PyInstaller",
    ):
        __import__(mod_name)


def test_m1_read_version_memoization_and_base_dir_monkeypatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle_dir = tmp_path / "ver_bundle"
    bundle_dir.mkdir()
    monkeypatch.setattr(pdf_lector_pkg, "get_base_dir", lambda: bundle_dir)

    assert read_version() == __version__

    ver_file = bundle_dir / "VERSION"
    ver_file.write_text("0.9.1-test\n", encoding="utf-8")

    read_calls = 0
    orig_read_text = Path.read_text

    def _counting_read_text(self: Path, *args, **kwargs) -> str:
        nonlocal read_calls
        if self == ver_file:
            read_calls += 1
        return orig_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _counting_read_text)
    assert read_version() == "0.9.1-test"
    assert read_version() == "0.9.1-test"
    assert read_version() == "0.9.1-test"
    assert read_calls == 1

    ver_file.write_text("0.9.2-test\n", encoding="utf-8")
    os.utime(ver_file, ns=(ver_file.stat().st_atime_ns, ver_file.stat().st_mtime_ns + 1_000_000))
    assert read_version() == "0.9.2-test"
    assert read_calls == 2


# ---------------------------------------------------------------------------
# Feature #2: path_guard Extensions & Traversal Confinement
# ---------------------------------------------------------------------------


def test_m1_path_guard_md_glossary_and_confinement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert ALLOWED_MD_EXTENSIONS == {".md", ".markdown"}

    md1 = tmp_path / "notes.md"
    md2 = tmp_path / "manual.markdown"
    pdf1 = tmp_path / "doc.pdf"
    glo1 = tmp_path / "es_custom.json"
    bad_txt = tmp_path / "readme.txt"
    for f in (md1, md2, pdf1, glo1, bad_txt):
        f.write_text("ok", encoding="utf-8")

    assert validate_local_markdown_path(md1) == (True, "ok")
    assert validate_local_markdown_path(md2) == (True, "ok")
    assert validate_local_markdown_path(pdf1)[0] is False
    assert validate_local_any_doc(pdf1) == (True, "ok")
    assert validate_local_any_doc(md1) == (True, "ok")
    assert validate_local_any_doc(bad_txt) == (False, "unsupported_extension")
    assert validate_local_glossary_path(glo1) == (True, "ok")
    assert validate_local_glossary_path(md1) == (False, "unsupported_extension")

    unsafe_refs = (
        r"\\evil\share\doc.md",
        "//evil/share/doc.md",
        r"\\?\UNC\evil\share\doc.md",
        r"\\.\pipe\test.md",
        r"\/evil\share\doc.md",
        "%5c%5cevil%5cshare%5cdoc.md",
        "https://example.com/doc.md",
        "http://localhost/doc.md",
        "ftp://host/doc.md",
        "smb://host/share/doc.md",
        "file:///C:/secret/doc.md",
        "data:text/plain,hello",
        "javascript:alert(1)",
        "vbscript:msgbox(1)",
        "java\tscript:alert(1)",
        "doc.md\x00.pdf",
        "C:relative.md",
        str(md1) + ":hidden_stream",
    )
    for bad in unsafe_refs:
        ok, _reason = validate_local_any_doc(bad, check_exists=False)
        assert not ok, f"Unsafe document path unexpectedly accepted: {bad!r}"

    # Cloud-only and remote drive guards
    monkeypatch.setattr("pdf_lector.core.path_guard.is_cloud_only_file", lambda _p: True)
    assert validate_local_markdown_path(md1, check_exists=True) == (False, "cloud_only_blocked")
    monkeypatch.setattr("pdf_lector.core.path_guard.is_cloud_only_file", lambda _p: False)

    monkeypatch.setattr("pdf_lector.core.path_guard.is_remote_drive", lambda _p: True)
    assert validate_local_markdown_path(md1, check_exists=False) == (False, "network_or_url_blocked")
    monkeypatch.setattr("pdf_lector.core.path_guard.is_remote_drive", lambda _p: False)

    # Resource confinement inside base_dir
    base_dir = tmp_path / "workspace"
    (base_dir / "assets").mkdir(parents=True)
    img = base_dir / "assets" / "chart.png"
    img.write_bytes(b"PNG")
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")

    resolved = resolve_safe_doc_resource(base_dir, "assets/chart.png", check_exists=True)
    assert resolved == img.resolve()

    for bad_rel in (
        "../secret.txt",
        r"..\..\secret.txt",
        "%2e%2e%2fsecret.txt",
        "%252e%252e%252fsecret.txt",
        str(outside),
        "/etc/passwd",
        r"\Windows\win.ini",
        "C:rel.png",
        "assets/chart.png:ads",
        r"\\server\share\a.png",
        "https://evil.com/a.png",
        "javascript:alert(1)",
        "vbscript:msgbox(1)",
        "",
    ):
        with pytest.raises(ValueError):
            resolve_safe_doc_resource(base_dir, bad_rel, check_exists=False)


# ---------------------------------------------------------------------------
# Feature #3: Windows DPAPI & SecretsStore
# ---------------------------------------------------------------------------


def test_m1_dpapi_and_secrets_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secret_bytes = b"sk-trodai-super-secret-token-12345"
    protected = dpapi_protect(secret_bytes)
    if IS_WINDOWS:
        assert protected != secret_bytes
        assert dpapi_unprotect(protected) == secret_bytes
        empty_prot = dpapi_protect(b"")
        assert dpapi_unprotect(empty_prot) == b""
        with pytest.raises(OSError):
            dpapi_unprotect(b"corrupted-dpapi-blob")
    with pytest.raises(ValueError):
        dpapi_unprotect(b"")

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    store = SecretsStore(base_dir=tmp_path / "sec")
    store.set_secret("openai_key", "sk-live-secret-999")
    assert store.get_secret("openai_key") == "sk-live-secret-999"
    assert store.list_keys() == ["openai_key"]

    raw_json = store.file_path.read_text(encoding="utf-8")
    assert "sk-live-secret-999" not in raw_json

    assert store.delete_secret("openai_key") is True
    assert store.get_secret("openai_key", default="fallback") == "fallback"

    # Module-level contract using LOCALAPPDATA override (never touches real user folder)
    set_secret("bearer_token", "tok-abc-123")
    assert get_secret("bearer_token") == "tok-abc-123"
    delete_secret("bearer_token")
    assert get_secret("bearer_token") is None

    # Corrupt JSON handled gracefully
    store.file_path.write_text("{corrupt-json", encoding="utf-8")
    assert store.get_secret("anything", default="safe") == "safe"


def test_m1_secrets_store_concurrent_module_writes(tmp_path: Path) -> None:
    conc_dir = tmp_path / "conc_secrets"
    errors: list[Exception] = []

    def _writer(tid: int) -> None:
        try:
            for idx in range(5):
                set_secret(f"m_{tid}_{idx}", f"val_{tid}_{idx}", base_dir=conc_dir)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=_writer, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    keys = SecretsStore(base_dir=conc_dir).list_keys()
    assert len(keys) == 60
    assert get_secret("m_11_4", base_dir=conc_dir) == "val_11_4"


def test_m1_secrets_store_canonical_lock_and_stress(tmp_path: Path) -> None:
    from pdf_lector.core import secrets_store

    canon_dir = tmp_path / "CanonDir"
    canon_file = canon_dir / "secrets.json"
    variants = [canon_file]
    if IS_WINDOWS:
        variants.extend(
            [
                Path(rf"\\?\{canon_file}"),
                Path(str(canon_file).upper()),
                Path(str(canon_file).replace("\\", "/")),
            ]
        )

    locks = [secrets_store._lock_for_path(v) for v in variants]
    assert all(lk is locks[0] for lk in locks)
    expected_key = canonical_lock_key(canon_file)
    assert all(canonical_lock_key(v) == expected_key for v in variants)
    matching_keys = [k for k in secrets_store._PATH_LOCKS if k == expected_key]
    assert len(matching_keys) == 1

    # Repeated concurrent stress writes alternating normal and \\?\ base_dir
    for round_idx in range(5):
        round_dir = tmp_path / f"stress_r{round_idx}"
        round_errors: list[Exception] = []

        def _stress_writer(tid: int, r_dir: Path = round_dir) -> None:
            try:
                for idx in range(5):
                    target_dir = (
                        Path(rf"\\?\{r_dir}")
                        if (IS_WINDOWS and (tid + idx) % 2 == 1)
                        else r_dir
                    )
                    set_secret(f"s_{tid}_{idx}", f"v_{tid}_{idx}", base_dir=target_dir)
            except Exception as exc:
                round_errors.append(exc)

        threads = [threading.Thread(target=_stress_writer, args=(i,)) for i in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert round_errors == []
        assert len(SecretsStore(base_dir=round_dir).list_keys()) == 60
        round_key = canonical_lock_key(round_dir / "secrets.json")
        assert sum(1 for k in secrets_store._PATH_LOCKS if k == round_key) == 1


# ---------------------------------------------------------------------------
# Feature #4: TempManager
# ---------------------------------------------------------------------------


def test_m1_temp_manager_lifecycle_readonly_and_wipe(tmp_path: Path) -> None:
    mgr = TempManager(temp_root=tmp_path, register_atexit=False)
    assert mgr.session_dir.parent == tmp_path / "TRODAI_PDFLector"
    # Lazy creation
    assert not mgr.session_dir.exists()

    ro_file = mgr.create_temp_file(suffix=".pdf", task_id="t1", secure=False)
    ro_file.write_bytes(b"%PDF-1.4")
    os.chmod(ro_file, stat.S_IREAD)

    wipe_file = mgr.create_temp_file(suffix=".txt", task_id="t1", secure=True)
    wipe_file.write_bytes(b"SENSITIVE_TEMP_PAYLOAD" * 100)

    other_file = mgr.create_temp_file(suffix=".tmp", task_id="t2", secure=False)
    other_file.write_bytes(b"keep-me")

    assert mgr.cleanup_task("t1") == []
    assert not ro_file.exists()
    assert not wipe_file.exists()
    assert other_file.exists()

    # Symlink target must NOT be wiped or deleted
    target = tmp_path / "real_target.txt"
    target.write_bytes(b"MUST_SURVIVE")
    link_path = tmp_path / "link_target.txt"
    can_symlink = True
    try:
        link_path.symlink_to(target)
    except (OSError, NotImplementedError):
        can_symlink = False
    if can_symlink:
        assert secure_unlink(link_path, overwrite_zero=True) is True
        assert not link_path.exists()
        assert target.read_bytes() == b"MUST_SURVIVE"

    # Stale PID cleanup removes dead PID dir and preserves live PID dir
    stale_dir = mgr.app_temp_root / "999999_deadbeef"
    stale_dir.mkdir(parents=True)
    (stale_dir / "old.tmp").write_bytes(b"old")
    old_ts = time.time() - 7200
    os.utime(stale_dir, (old_ts, old_ts))

    live_dir = mgr.app_temp_root / f"{os.getpid()}_liveproc"
    live_dir.mkdir(parents=True)
    os.utime(live_dir, (old_ts, old_ts))

    removed = mgr.cleanup_stale_sessions(min_age_seconds=60.0)
    assert stale_dir in removed
    assert not stale_dir.exists()
    assert live_dir.exists()

    assert mgr.cleanup_all() == []
    assert not mgr.session_dir.exists()


# ---------------------------------------------------------------------------
# Feature #5: CrashGuard & Rotating Logs
# ---------------------------------------------------------------------------


def test_m1_crash_guard_recovery_excepthook_and_rotation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logs_dir = tmp_path / "logs"
    orig_sys_hook = sys.excepthook
    orig_thr_hook = threading.excepthook

    cg = CrashGuard(logs_dir=logs_dir, version="0.1.0", max_crash_log_bytes=450, max_app_log_bytes=450)
    try:
        cg.install()
        rep1 = cg.begin_session()
        assert not rep1.dirty
        cg.breadcrumb("opened_hub")
        cg.log_app("INFO", "App booted")
        cg.mark_clean_exit()
    finally:
        cg.uninstall()

    assert sys.excepthook is orig_sys_hook
    assert threading.excepthook is orig_thr_hook

    # Clean restart -> dirty is False
    cg2 = CrashGuard(logs_dir=logs_dir, version="0.1.0", max_crash_log_bytes=450, max_app_log_bytes=450)
    try:
        cg2.install()
        rep2 = cg2.begin_session()
        assert not rep2.dirty
        # Trigger sys.excepthook manually without crashing pytest
        try:
            raise RuntimeError("Synthetic crash hook test")
        except RuntimeError as exc:
            sys.excepthook(type(exc), exc, exc.__traceback__)
        assert "Synthetic crash hook test" in cg2.read_crash_log_tail()
        assert "Synthetic crash hook test" in cg2.read_app_log_tail()

        # Verify rotation works under small max_bytes
        for i in range(15):
            cg2.write_log_block(f"Block {i}", "X" * 80)
            cg2.log_app("INFO", f"Line {i} " + ("Y" * 80))
        assert (logs_dir / "crash.log.1").exists()
        assert (logs_dir / "app.log.1").exists()
    finally:
        cg2.uninstall()

    # Simulate unclean previous session from dead PID
    (logs_dir / "session_lock.json").write_text(
        json.dumps({"pid": 999999, "version": "0.1.0", "clean_exit": False}),
        encoding="utf-8",
    )
    cg3 = CrashGuard(logs_dir=logs_dir, version="0.1.0")
    try:
        cg3.install()
        rep3 = cg3.begin_session()
        assert rep3.dirty is True and rep3.previous_crashed is True
    finally:
        cg3.uninstall()

    # Live other PID should NOT be flagged dirty
    (logs_dir / "session_lock.json").write_text(
        json.dumps({"pid": 888888, "version": "0.1.0", "clean_exit": False}),
        encoding="utf-8",
    )
    monkeypatch.setattr("pdf_lector.core.crash_guard.is_process_alive", lambda pid, **_kw: pid == 888888)
    cg4 = CrashGuard(logs_dir=logs_dir, version="0.1.0")
    try:
        cg4.install()
        rep4 = cg4.begin_session()
        assert rep4.dirty is False
    finally:
        cg4.uninstall()


# ---------------------------------------------------------------------------
# Feature #6: SingleInstanceLock & Tab Forwarding
# ---------------------------------------------------------------------------


def test_m1_single_instance_lock_and_tab_forwarding(qapp: QApplication, tmp_path: Path) -> None:
    del qapp
    unique_key = f"TRODAI.PDFMDLector.Test.{uuid.uuid4().hex}"
    lock1 = SingleInstanceLock(key=unique_key)
    lock2 = SingleInstanceLock(key=unique_key)

    received: list[list[Path]] = []
    sample_pdf = tmp_path / "tab1.pdf"
    sample_md = tmp_path / "tab2.md"
    bad_exe = tmp_path / "ignore.exe"
    sample_pdf.write_bytes(b"%PDF-1.4")
    sample_md.write_text("# Hello", encoding="utf-8")
    bad_exe.write_bytes(b"MZ")

    try:
        assert lock1.acquire(wait_ms=0) is True
        assert lock2.acquire(wait_ms=0) is False
        assert lock1.start_server(lambda paths: received.append(paths)) is True

        sent = lock2.send_to_running_instance(
            [sample_pdf, sample_md, bad_exe, r"\\evil\share\leak.pdf"]
        )
        assert sent is True
        assert _pump_until(lambda: len(received) == 1, timeout_s=3.0)
        assert received[0] == [sample_pdf.resolve(), sample_md.resolve()]
    finally:
        lock2.release()
        lock1.release()

    # Mutex can be cleanly re-acquired after release
    lock3 = SingleInstanceLock(key=unique_key)
    try:
        assert lock3.acquire(wait_ms=0) is True
    finally:
        lock3.release()


def test_m1_single_instance_aborts_oversized_ipc_payload(qapp: QApplication) -> None:
    del qapp
    from PySide6.QtNetwork import QLocalSocket

    unique_key = f"TRODAI.PDFMDLector.Cap.{uuid.uuid4().hex}"
    lock = SingleInstanceLock(key=unique_key)
    received: list[list[Path]] = []
    try:
        assert lock.acquire(wait_ms=0) is True
        assert lock.start_server(lambda paths: received.append(paths)) is True
        sock = QLocalSocket()
        sock.connectToServer(lock.server_name)
        assert sock.waitForConnected(1500)
        sock.write(b"A" * (1024 * 1024 + 64))
        sock.flush()
        _pump_until(lambda: sock.state() == QLocalSocket.LocalSocketState.UnconnectedState, timeout_s=2.0)
        sock.deleteLater()
        assert received == []
    finally:
        lock.release()


# ---------------------------------------------------------------------------
# Feature #7: Destructive PDF Redaction
# ---------------------------------------------------------------------------


def test_m1_redaction_text_split_tj_and_raw_bytes(tmp_path: Path) -> None:
    src_pdf = tmp_path / "source_split.pdf"
    out_pdf = tmp_path / "redacted_split.pdf"

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(612, 792))
    c.setFont("Helvetica", 12)
    c.drawString(72, 720, "ABOVE-NEIGHBOR-LINE")
    c.drawString(72, 700, "KEEP-LEFT SECRET-123-45 KEEP-RIGHT")
    c.drawString(72, 680, "BELOW-NEIGHBOR-LINE")
    c.showPage()
    c.save()

    # Inject an explicit TJ kerning array on the same page
    with pikepdf.Pdf.open(io.BytesIO(buf.getvalue())) as pdf:
        page = pdf.pages[0]
        ops = list(pikepdf.parse_content_stream(page))
        ops.extend(
            [
                pikepdf.ContentStreamInstruction([], pikepdf.Operator("BT")),
                pikepdf.ContentStreamInstruction([pikepdf.Name("/F1"), 12], pikepdf.Operator("Tf")),
                pikepdf.ContentStreamInstruction([72, 650], pikepdf.Operator("Td")),
                pikepdf.ContentStreamInstruction(
                    [
                        pikepdf.Array(
                            [
                                pikepdf.String(b"PRE-"),
                                -20,
                                pikepdf.String(b"TOPSECRET"),
                                -20,
                                pikepdf.String(b"-POST"),
                            ]
                        )
                    ],
                    pikepdf.Operator("TJ"),
                ),
                pikepdf.ContentStreamInstruction([], pikepdf.Operator("ET")),
            ]
        )
        page.Contents = pdf.make_stream(pikepdf.unparse_content_stream(ops))
        pdf.save(src_pdf)

    res = redact_pdf_terms(src_pdf, out_pdf, ["SECRET-123-45", "TOPSECRET"])
    assert res.verified_clean is True
    assert res.pages_modified == 1
    assert res.removed_text_ops >= 2

    clean_ok, findings = verify_redaction_clean(out_pdf, ["SECRET-123-45", "TOPSECRET"])
    assert clean_ok and findings == []

    doc = pdfium.PdfDocument(str(out_pdf))
    try:
        extracted = doc[0].get_textpage().get_text_range()
    finally:
        doc.close()

    assert "SECRET-123-45" not in extracted
    assert "TOPSECRET" not in extracted
    assert "KEEP-LEFT" in extracted
    assert "KEEP-RIGHT" in extracted
    assert "ABOVE-NEIGHBOR-LINE" in extracted
    assert "BELOW-NEIGHBOR-LINE" in extracted
    assert "PRE-" in extracted and "-POST" in extracted

    raw_bytes = out_pdf.read_bytes()
    assert b"SECRET-123-45" not in raw_bytes
    assert b"TOPSECRET" not in raw_bytes
    assert raw_bytes.count(b"%%EOF") == 1


def test_m1_redaction_xobject_vector_image_rotated_annots(tmp_path: Path) -> None:
    src_pdf = tmp_path / "complex_src.pdf"
    out_pdf = tmp_path / "complex_redacted.pdf"

    pdf = pikepdf.Pdf.new()
    font_dict = pikepdf.Dictionary(
        {
            "/Type": pikepdf.Name("/Font"),
            "/Subtype": pikepdf.Name("/Type1"),
            "/BaseFont": pikepdf.Name("/Helvetica"),
            "/Encoding": pikepdf.Name("/WinAnsiEncoding"),
        }
    )
    shared_form = pdf.make_stream(
        b"BT /F1 12 Tf 100 600 Td (FORM-SECRET) Tj ET"
    )
    shared_form["/Type"] = pikepdf.Name("/XObject")
    shared_form["/Subtype"] = pikepdf.Name("/Form")
    shared_form["/BBox"] = pikepdf.Array([0, 0, 612, 792])
    shared_form["/Resources"] = pikepdf.Dictionary({"/Font": pikepdf.Dictionary({"/F1": font_dict})})

    img_stream = pdf.make_stream(b"\xff\x00\x00\x00\xff\x00\x00\x00\xff\xff\xff\x00")
    img_stream["/Type"] = pikepdf.Name("/XObject")
    img_stream["/Subtype"] = pikepdf.Name("/Image")
    img_stream["/Width"] = 2
    img_stream["/Height"] = 2
    img_stream["/ColorSpace"] = pikepdf.Name("/DeviceRGB")
    img_stream["/BitsPerComponent"] = 8

    # Page 0: Form XObject + Image Do + Vector rect inside & outside + BDC ActualText + Annots
    p0_stream = (
        b"q /Fm1 Do Q\n"
        b"q 60 0 0 40 100 400 cm /Im1 Do Q\n"
        b"q 100 300 50 20 re f Q\n"
        b"q 400 300 50 20 re f Q\n"
        b"/Span << /ActualText (HIDDEN-ALT) >> BDC BT /F1 12 Tf 100 500 Td (BAD-SPAN) Tj ET EMC\n"
    )
    page0 = pikepdf.Page(
        pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/Page"),
                "/MediaBox": pikepdf.Array([0, 0, 612, 792]),
                "/Resources": pikepdf.Dictionary(
                    {
                        "/Font": pikepdf.Dictionary({"/F1": font_dict}),
                        "/XObject": pikepdf.Dictionary({"/Fm1": shared_form, "/Im1": img_stream}),
                    }
                ),
                "/Contents": pdf.make_stream(p0_stream),
                "/Annots": pikepdf.Array(
                    [
                        pdf.make_indirect(
                            pikepdf.Dictionary(
                                {
                                    "/Type": pikepdf.Name("/Annot"),
                                    "/Subtype": pikepdf.Name("/Text"),
                                    "/Rect": pikepdf.Array([95, 590, 220, 620]),
                                    "/Contents": pikepdf.String("ANNOT-SECRET"),
                                }
                            )
                        ),
                        pdf.make_indirect(
                            pikepdf.Dictionary(
                                {
                                    "/Type": pikepdf.Name("/Annot"),
                                    "/Subtype": pikepdf.Name("/Text"),
                                    "/Rect": pikepdf.Array([400, 100, 430, 130]),
                                    "/Contents": pikepdf.String("ANNOT-KEEP"),
                                }
                            )
                        ),
                    ]
                ),
            }
        )
    )
    # Page 1: shares same Form XObject (must remain unredacted!)
    page1 = pikepdf.Page(
        pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/Page"),
                "/MediaBox": pikepdf.Array([0, 0, 612, 792]),
                "/Resources": pikepdf.Dictionary(
                    {
                        "/Font": pikepdf.Dictionary({"/F1": font_dict}),
                        "/XObject": pikepdf.Dictionary({"/Fm1": shared_form}),
                    }
                ),
                "/Contents": pdf.make_stream(b"q /Fm1 Do Q\n"),
            }
        )
    )
    # Page 2: /Rotate 90 with text at PDF (100, 700), tested via display_rect_to_pdf_rect
    page2 = pikepdf.Page(
        pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/Page"),
                "/MediaBox": pikepdf.Array([0, 0, 792, 612]),
                "/Rotate": 90,
                "/Resources": pikepdf.Dictionary({"/Font": pikepdf.Dictionary({"/F1": font_dict})}),
                "/Contents": pdf.make_stream(b"BT /F1 12 Tf 100 700 Td (ROT-SECRET) Tj ET"),
            }
        )
    )
    pdf.pages.extend([page0, page1, page2])
    pdf.save(src_pdf)

    # Display coords corresponding to PDF (95..200, 695..715) on /Rotate 90 (MediaBox W=792):
    # display_x = y_pdf (695..715), display_y = W - x_pdf (792-200 .. 792-95 = 592..697)
    disp_rect = (695.0, 592.0, 715.0, 697.0)
    pdf_from_disp = display_rect_to_pdf_rect(disp_rect, (0, 0, 792, 612), 90)
    assert pdf_from_disp == pytest.approx((95.0, 695.0, 200.0, 715.0))

    regions = [
        RedactionRegion(0, (95, 595, 220, 620), "pdf"),   # Form XObject text + overlapping Annot
        RedactionRegion(0, (95, 395, 165, 445), "pdf"),   # Image XObject Do
        RedactionRegion(0, (98, 298, 152, 322), "pdf"),   # Vector rect (100,300,50,20)
        RedactionRegion(0, (95, 495, 195, 520), "pdf"),   # BDC span text
        RedactionRegion(2, disp_rect, "display"),         # Rotated page text via display coords
    ]
    res = redact_pdf_regions(src_pdf, out_pdf, regions, options=RedactionOptions())
    assert res.pages_modified == 2
    assert res.removed_image_ops >= 1
    assert res.removed_vector_ops >= 1
    assert res.removed_annots == 1

    with pikepdf.Pdf.open(out_pdf) as out_p:
        # Page 0: image XObject removed from Resources, vector outside kept, ANNOT-KEEP preserved
        p0 = out_p.pages[0]
        p0_ops = pikepdf.parse_content_stream(p0)
        p0_ops_text = pikepdf.unparse_content_stream(p0_ops)
        assert b"/Im1 Do" not in p0_ops_text
        assert b"400 300 50 20 re" in p0_ops_text
        assert b"100 300 50 20 re" not in p0_ops_text
        assert b"HIDDEN-ALT" not in p0_ops_text
        assert b"BAD-SPAN" not in p0_ops_text
        assert len(p0["/Annots"]) == 1
        assert str(p0["/Annots"][0]["/Contents"]) == "ANNOT-KEEP"

        # Page 1's shared Form XObject must NOT have been mutated!
        p1_form = out_p.pages[1]["/Resources"]["/XObject"]["/Fm1"]
        assert b"FORM-SECRET" in bytes(p1_form.read_bytes())

    doc = pdfium.PdfDocument(str(out_pdf))
    try:
        assert "FORM-SECRET" not in doc[0].get_textpage().get_text_range()
        assert "BAD-SPAN" not in doc[0].get_textpage().get_text_range()
        assert "FORM-SECRET" in doc[1].get_textpage().get_text_range()
        assert "ROT-SECRET" not in doc[2].get_textpage().get_text_range()
    finally:
        doc.close()


def test_m1_redaction_nested_multi_placement_form_and_missing_widths(tmp_path: Path) -> None:
    # 1. Nested & multi-placement shared Form XObjects preserve Page 1 and 2nd placement on Page 0
    src_nested = tmp_path / "nested_form.pdf"
    out_nested = tmp_path / "nested_form_redacted.pdf"
    pdf = pikepdf.Pdf.new()
    font = pikepdf.Dictionary(
        {
            "/Type": pikepdf.Name("/Font"),
            "/Subtype": pikepdf.Name("/Type1"),
            "/BaseFont": pikepdf.Name("/Helvetica"),
            "/Encoding": pikepdf.Name("/WinAnsiEncoding"),
        }
    )
    inner = pdf.make_stream(b"BT /F1 12 Tf 50 0 Td (SHARED-INNER-DATA) Tj ET")
    inner["/Type"] = pikepdf.Name("/XObject")
    inner["/Subtype"] = pikepdf.Name("/Form")
    inner["/BBox"] = pikepdf.Array([0, -10, 300, 30])
    inner["/Resources"] = pikepdf.Dictionary({"/Font": pikepdf.Dictionary({"/F1": font})})

    outer = pdf.make_stream(b"BT /F1 12 Tf 50 15 Td (OUTER-TEXT) Tj ET\nq /FmInner Do Q\n")
    outer["/Type"] = pikepdf.Name("/XObject")
    outer["/Subtype"] = pikepdf.Name("/Form")
    outer["/BBox"] = pikepdf.Array([0, -10, 300, 40])
    outer["/Resources"] = pikepdf.Dictionary(
        {
            "/Font": pikepdf.Dictionary({"/F1": font}),
            "/XObject": pikepdf.Dictionary({"/FmInner": inner}),
        }
    )

    page0 = pikepdf.Page(
        pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/Page"),
                "/MediaBox": pikepdf.Array([0, 0, 612, 792]),
                "/Resources": pikepdf.Dictionary({"/XObject": pikepdf.Dictionary({"/FmOuter": outer})}),
                "/Contents": pdf.make_stream(
                    b"q 1 0 0 1 0 700 cm /FmOuter Do Q\nq 1 0 0 1 0 400 cm /FmOuter Do Q\n"
                ),
            }
        )
    )
    page1 = pikepdf.Page(
        pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/Page"),
                "/MediaBox": pikepdf.Array([0, 0, 612, 792]),
                "/Resources": pikepdf.Dictionary({"/XObject": pikepdf.Dictionary({"/FmOuter": outer})}),
                "/Contents": pdf.make_stream(b"q 1 0 0 1 0 400 cm /FmOuter Do Q\n"),
            }
        )
    )
    pdf.pages.extend([page0, page1])
    pdf.save(src_nested)

    redact_pdf_regions(src_nested, out_nested, [RedactionRegion(0, (45, 695, 260, 725), "pdf")])
    doc = pdfium.PdfDocument(str(out_nested))
    try:
        p0_text = doc[0].get_textpage().get_text_range()
        p1_text = doc[1].get_textpage().get_text_range()
    finally:
        doc.close()

    assert p0_text.count("SHARED-INNER-DATA") == 1
    assert "SHARED-INNER-DATA" in p1_text

    # 2. Custom font lacking /Widths does not suffer 500.0 width drift
    src_drift = tmp_path / "no_widths_drift.pdf"
    out_drift = tmp_path / "no_widths_drift_out.pdf"
    pdf_d = pikepdf.Pdf.new()
    pg_d = pdf_d.add_blank_page()
    custom_font = pikepdf.Dictionary(
        {
            "/Type": pikepdf.Name("/Font"),
            "/Subtype": pikepdf.Name("/Type1"),
            "/BaseFont": pikepdf.Name("/CustomWeirdFontNoWidths"),
        }
    )
    pg_d["/Resources"] = pikepdf.Dictionary({"/Font": pikepdf.Dictionary({"/F1": custom_font})})
    pg_d["/Contents"] = pdf_d.make_stream(
        b"BT /F1 12 Tf 50 700 Td (iiiiiiiiiiiiiiiiiiiiiiiiiiiiiiiiiiii SECRET-DRIFT-123 TAIL) Tj ET"
    )
    pdf_d.save(src_drift)

    res_d = redact_pdf_terms(src_drift, out_drift, ["SECRET-DRIFT-123"])
    assert res_d.verified_clean is True
    doc_d = pdfium.PdfDocument(str(out_drift))
    try:
        drift_text = doc_d[0].get_textpage().get_text_range()
    finally:
        doc_d.close()
    assert "SECRET-DRIFT-123" not in drift_text
    assert "iiiiiiiiiiiiiiiiii" in drift_text
    assert "TAIL" in drift_text
    assert b"SECRET-DRIFT-123" not in out_drift.read_bytes()


def test_m1_redaction_compressed_form_xobject_cleanup_and_verify(tmp_path: Path) -> None:
    import zlib

    # 1. Compressed Form XObject + partial /Resources + shared Form XObject invoked twice
    src_pdf = tmp_path / "compressed_form_src.pdf"
    out_pdf = tmp_path / "compressed_form_out1.pdf"
    out_pdf2 = tmp_path / "compressed_form_out2.pdf"

    pdf = pikepdf.Pdf.new()
    font = pikepdf.Dictionary(
        {
            "/Type": pikepdf.Name("/Font"),
            "/Subtype": pikepdf.Name("/Type1"),
            "/BaseFont": pikepdf.Name("/Helvetica"),
            "/Encoding": pikepdf.Name("/WinAnsiEncoding"),
        }
    )
    ext_gs = pikepdf.Dictionary({"/Type": pikepdf.Name("/ExtGState"), "/CA": 1.0})

    # Partial /Resources on /FmInner (has /ExtGState, omits /Font -> must inherit parent /Font)
    fm_inner = pdf.make_stream(b"BT /F1 14 Tf 10 10 Td (NESTED_SECRET KEEP_INNER) Tj ET")
    fm_inner["/Type"] = pikepdf.Name("/XObject")
    fm_inner["/Subtype"] = pikepdf.Name("/Form")
    fm_inner["/BBox"] = pikepdf.Array([0, 0, 300, 40])
    fm_inner["/Resources"] = pikepdf.Dictionary(
        {"/ExtGState": pikepdf.Dictionary({"/GS1": ext_gs})}
    )

    # Shared Form XObject invoked twice: y=700 (redacted) and y=600 (kept unredacted)
    fm_shared = pdf.make_stream(b"BT /F1 12 Tf 10 10 Td (SHARED_TARGET KEEP_SHARED) Tj ET")
    fm_shared["/Type"] = pikepdf.Name("/XObject")
    fm_shared["/Subtype"] = pikepdf.Name("/Form")
    fm_shared["/BBox"] = pikepdf.Array([0, 0, 300, 40])
    fm_shared["/Resources"] = pikepdf.Dictionary({"/Font": pikepdf.Dictionary({"/F1": font})})

    page0 = pikepdf.Page(
        pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/Page"),
                "/MediaBox": pikepdf.Array([0, 0, 612, 792]),
                "/Resources": pikepdf.Dictionary(
                    {
                        "/Font": pikepdf.Dictionary({"/F1": font}),
                        "/XObject": pikepdf.Dictionary(
                            {"/FmInner": fm_inner, "/FmShared": fm_shared}
                        ),
                    }
                ),
                "/Contents": pdf.make_stream(
                    b"q 1 0 0 1 50 500 cm /FmInner Do Q\n"
                    b"q 1 0 0 1 50 700 cm /FmShared Do Q\n"
                    b"q 1 0 0 1 50 600 cm /FmShared Do Q\n"
                ),
            }
        )
    )
    pdf.pages.append(page0)
    # Save with stream compression enabled so /FmInner and /FmShared carry /Filter /FlateDecode
    pdf.save(src_pdf, compress_streams=True)

    # Verify verify_redaction_clean actually scans page Form XObjects before redaction
    pre_clean, pre_findings = verify_redaction_clean(src_pdf, ["NESTED_SECRET"])
    assert pre_clean is False
    assert any("Form/FmInner" in f for f in pre_findings)

    # Redact NESTED_SECRET (inside /FmInner) and only the y=700 placement of /FmShared
    redact_pdf_regions(
        src_pdf,
        out_pdf,
        [
            RedactionRegion(0, (55, 505, 170, 530), "pdf"),
            RedactionRegion(0, (55, 705, 175, 730), "pdf"),
        ],
    )

    with pikepdf.Pdf.open(out_pdf) as p_out:
        xobjs = p_out.pages[0]["/Resources"]["/XObject"]
        # Non-shared /FmInner was replaced by /FmInner_r1 and original /FmInner removed
        assert "/FmInner_r1" in xobjs
        assert "/FmInner" not in xobjs
        # Shared /FmShared still has an unredacted Do at y=600, so BOTH /FmShared_r1 and /FmShared remain
        assert "/FmShared_r1" in xobjs
        assert "/FmShared" in xobjs

        r1_inner = xobjs["/FmInner_r1"]
        # read_bytes() must decode cleanly without DataDecodingError, and if /FlateDecode is set by save(),
        # the raw bytes must genuinely be compressed rather than uncompressed text tagged /FlateDecode
        inner_bytes = bytes(r1_inner.read_bytes())
        if str(r1_inner.get("/Filter", "")) == "/FlateDecode":
            assert bytes(r1_inner.read_raw_bytes()) != inner_bytes
        assert b"NESTED_SECRET" not in inner_bytes
        assert b"KEEP_INNER" in inner_bytes
        # Partial /Resources on /FmInner preserved /ExtGState AND merged parent /Font
        r1_res = r1_inner["/Resources"]
        assert "/GS1" in r1_res["/ExtGState"]
        assert "/F1" in r1_res["/Font"]

    # Unredacted /FmInner stream was purged from PDF bytes too
    assert b"NESTED_SECRET" not in out_pdf.read_bytes()
    clean_ok, clean_findings = verify_redaction_clean(out_pdf, ["NESTED_SECRET"])
    assert clean_ok and clean_findings == []

    # Second pass redacting KEEP_INNER must succeed without DataDecodingError/PdfError
    res2 = redact_pdf_terms(out_pdf, out_pdf2, ["KEEP_INNER"])
    assert res2.verified_clean is True
    clean_ok2, findings2 = verify_redaction_clean(out_pdf2, ["NESTED_SECRET", "KEEP_INNER"])
    assert clean_ok2 and findings2 == []

    # verify_redaction_clean must fail (False, findings) on corrupt/undecodable streams
    corrupt_pdf = tmp_path / "corrupt_stream.pdf"
    with pikepdf.Pdf.open(out_pdf) as p_bad:
        bad_stream = p_bad.make_stream(b"NOT_VALID_ZLIB_BYTES")
        bad_stream["/Filter"] = pikepdf.Name("/FlateDecode")
        bad_stream["/Type"] = pikepdf.Name("/XObject")
        bad_stream["/Subtype"] = pikepdf.Name("/Form")
        bad_stream["/BBox"] = pikepdf.Array([0, 0, 100, 100])
        p_bad.pages[0]["/Resources"]["/XObject"]["/FmCorrupt"] = bad_stream
        # Force raw uncompressed write keeping /Filter /FlateDecode
        bad_stream.write(zlib.compress(b"ok"))
        bad_stream.write(b"NOT_VALID_ZLIB_BYTES", filter=pikepdf.Name("/FlateDecode"))
        p_bad.save(corrupt_pdf)

    corrupt_ok, corrupt_findings = verify_redaction_clean(corrupt_pdf, ["ANY_TERM"])
    assert corrupt_ok is False
    assert any("decode_error" in f for f in corrupt_findings)


def test_m1_verify_redaction_clean_nested_form_xobjects_depth_ge_2(tmp_path: Path) -> None:
    def _make_font() -> pikepdf.Dictionary:
        return pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/Font"),
                "/Subtype": pikepdf.Name("/Type1"),
                "/BaseFont": pikepdf.Name("/Helvetica"),
                "/Encoding": pikepdf.Name("/WinAnsiEncoding"),
            }
        )

    # 1. Hidden/unrendered forbidden text inside depth-2 Form XObject (/FmOuter -> /FmInner)
    secret_pdf = tmp_path / "nested_depth2_secret.pdf"
    p_secret = pikepdf.Pdf.new()
    fm_inner = p_secret.make_stream(b"BT /F1 12 Tf 10 10 Td (DEEP_NESTED_SECRET) Tj ET")
    fm_inner["/Type"] = pikepdf.Name("/XObject")
    fm_inner["/Subtype"] = pikepdf.Name("/Form")
    fm_inner["/BBox"] = pikepdf.Array([0, 0, 200, 50])
    fm_inner["/Resources"] = pikepdf.Dictionary({"/Font": pikepdf.Dictionary({"/F1": _make_font()})})

    # /FmOuter declares /FmInner in its /Resources/XObject without calling /FmInner Do
    fm_outer = p_secret.make_stream(b"BT /F1 12 Tf 10 10 Td (OUTER_BENIGN) Tj ET")
    fm_outer["/Type"] = pikepdf.Name("/XObject")
    fm_outer["/Subtype"] = pikepdf.Name("/Form")
    fm_outer["/BBox"] = pikepdf.Array([0, 0, 200, 50])
    fm_outer["/Resources"] = pikepdf.Dictionary(
        {
            "/Font": pikepdf.Dictionary({"/F1": _make_font()}),
            "/XObject": pikepdf.Dictionary({"/FmInner": fm_inner}),
        }
    )
    page_s = p_secret.add_blank_page()
    page_s["/Resources"] = pikepdf.Dictionary(
        {
            "/Font": pikepdf.Dictionary({"/F1": _make_font()}),
            "/XObject": pikepdf.Dictionary({"/FmOuter": fm_outer}),
        }
    )
    page_s["/Contents"] = p_secret.make_stream(b"q /FmOuter Do Q\n")
    p_secret.save(secret_pdf)

    ok_secret, findings_secret = verify_redaction_clean(secret_pdf, ["DEEP_NESTED_SECRET"])
    assert ok_secret is False
    assert "pikepdf:page[0]/Form/FmOuter/Form/FmInner:term:DEEP_NESTED_SECRET" in findings_secret

    # 2. Corrupt /FlateDecode stream inside depth-2 Form XObject (/FmOuter -> /FmBadIn)
    corrupt_nested_pdf = tmp_path / "nested_depth2_corrupt.pdf"
    p_bad = pikepdf.Pdf.new()
    fm_bad_in = p_bad.make_stream(b"BAD_ZLIB_BYTES")
    fm_bad_in["/Type"] = pikepdf.Name("/XObject")
    fm_bad_in["/Subtype"] = pikepdf.Name("/Form")
    fm_bad_in["/BBox"] = pikepdf.Array([0, 0, 100, 100])
    fm_bad_in.write(b"BAD_ZLIB_BYTES", filter=pikepdf.Name("/FlateDecode"))

    fm_outer_bad = p_bad.make_stream(b"BT /F1 12 Tf 10 10 Td (OUTER_CLEAN) Tj ET")
    fm_outer_bad["/Type"] = pikepdf.Name("/XObject")
    fm_outer_bad["/Subtype"] = pikepdf.Name("/Form")
    fm_outer_bad["/BBox"] = pikepdf.Array([0, 0, 200, 50])
    fm_outer_bad["/Resources"] = pikepdf.Dictionary(
        {
            "/Font": pikepdf.Dictionary({"/F1": _make_font()}),
            "/XObject": pikepdf.Dictionary({"/FmBadIn": fm_bad_in}),
        }
    )
    page_b = p_bad.add_blank_page()
    page_b["/Resources"] = pikepdf.Dictionary(
        {
            "/Font": pikepdf.Dictionary({"/F1": _make_font()}),
            "/XObject": pikepdf.Dictionary({"/FmOuter": fm_outer_bad}),
        }
    )
    page_b["/Contents"] = p_bad.make_stream(b"q /FmOuter Do Q\n")
    p_bad.save(corrupt_nested_pdf)

    ok_bad, findings_bad = verify_redaction_clean(corrupt_nested_pdf, ["NO_MATCH_TERM"])
    assert ok_bad is False
    assert any(
        f.startswith("pikepdf:page[0]/Form/FmOuter/Form/FmBadIn:decode_error:")
        for f in findings_bad
    )

    # 3. Clean nested Form XObjects (/FmOuter -> /FmInner) return (True, [])
    clean_nested_pdf = tmp_path / "nested_depth2_clean.pdf"
    p_clean = pikepdf.Pdf.new()
    fm_inner_clean = p_clean.make_stream(b"BT /F1 12 Tf 10 10 Td (SAFE_INNER_TEXT) Tj ET")
    fm_inner_clean["/Type"] = pikepdf.Name("/XObject")
    fm_inner_clean["/Subtype"] = pikepdf.Name("/Form")
    fm_inner_clean["/BBox"] = pikepdf.Array([0, 0, 200, 50])
    fm_inner_clean["/Resources"] = pikepdf.Dictionary({"/Font": pikepdf.Dictionary({"/F1": _make_font()})})

    fm_outer_clean = p_clean.make_stream(b"BT /F1 12 Tf 10 10 Td (SAFE_OUTER_TEXT) Tj ET\nq /FmInner Do Q\n")
    fm_outer_clean["/Type"] = pikepdf.Name("/XObject")
    fm_outer_clean["/Subtype"] = pikepdf.Name("/Form")
    fm_outer_clean["/BBox"] = pikepdf.Array([0, 0, 200, 50])
    fm_outer_clean["/Resources"] = pikepdf.Dictionary(
        {
            "/Font": pikepdf.Dictionary({"/F1": _make_font()}),
            "/XObject": pikepdf.Dictionary({"/FmInner": fm_inner_clean}),
        }
    )
    page_c = p_clean.add_blank_page()
    page_c["/Resources"] = pikepdf.Dictionary(
        {
            "/Font": pikepdf.Dictionary({"/F1": _make_font()}),
            "/XObject": pikepdf.Dictionary({"/FmOuter": fm_outer_clean}),
        }
    )
    page_c["/Contents"] = p_clean.make_stream(b"q /FmOuter Do Q\n")
    p_clean.save(clean_nested_pdf, compress_streams=True)

    assert verify_redaction_clean(clean_nested_pdf, ["DEEP_NESTED_SECRET"]) == (True, [])


# ---------------------------------------------------------------------------
# Feature #8: Metadata / XMP Sanitizer
# ---------------------------------------------------------------------------


def test_m1_metadata_sanitizer_info_xmp_pieceinfo_history(tmp_path: Path) -> None:
    src_pdf = tmp_path / "dirty_meta.pdf"
    out_pdf = tmp_path / "clean_meta.pdf"

    pdf = pikepdf.Pdf.new()
    pdf.add_blank_page()
    pdf.trailer["/Info"] = pdf.make_indirect(
        pikepdf.Dictionary(
            {
                "/Author": "Secret Author Name",
                "/Creator": "Secret Creator App",
                "/Producer": "Secret Producer",
                "/Title": "Confidential Title",
            }
        )
    )
    xmp_bytes = (
        b'<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>'
        b"<x:xmpmeta><dc:creator>Secret XMP Creator</dc:creator></x:xmpmeta>"
        b'<?xpacket end="w"?>'
    )
    pdf.Root["/Metadata"] = pdf.make_stream(xmp_bytes)
    pdf.Root["/PieceInfo"] = pikepdf.Dictionary({"/Adobe": pikepdf.Dictionary({"/Private": "SecretPiece"})})
    pdf.Root["/SpiderInfo"] = pikepdf.Dictionary({"/V": 1})
    pdf.pages[0]["/Metadata"] = pdf.make_stream(xmp_bytes)
    pdf.pages[0]["/PieceInfo"] = pikepdf.Dictionary({"/InDesign": pikepdf.Dictionary({"/Private": "PagePiece"})})
    pdf.save(src_pdf)

    # Append an incremental update so src_pdf has 2 %%EOF markers
    with pikepdf.Pdf.open(src_pdf, allow_overwriting_input=True) as inc_pdf:
        inc_pdf.trailer["/Info"]["/Subject"] = "Incremental Secret"
        inc_pdf.save(src_pdf)

    audit = inspect_pdf_metadata(src_pdf)
    assert "/Author" in audit.info_keys
    assert audit.has_xmp_metadata is True
    assert len(audit.piece_info_locations) >= 2
    assert audit.has_spider_info is True

    result = sanitize_pdf_metadata(src_pdf, out_pdf, options=SanitizeOptions())
    assert result.remaining_info_keys == []
    assert result.remaining_xmp is False
    assert result.remaining_piece_info == 0
    assert "/Author" in result.removed_info_keys
    assert result.removed_xmp_streams >= 2
    assert result.removed_piece_info_count >= 2

    with pikepdf.Pdf.open(out_pdf) as cleaned:
        assert "/Info" not in cleaned.trailer
        assert "/Metadata" not in cleaned.Root
        assert "/PieceInfo" not in cleaned.Root
        assert "/SpiderInfo" not in cleaned.Root
        assert "/Metadata" not in cleaned.pages[0]
        assert "/PieceInfo" not in cleaned.pages[0]

    raw_bytes = out_pdf.read_bytes()
    assert raw_bytes.count(b"%%EOF") == 1
    for secret in (
        b"Secret Author Name",
        b"Secret Creator App",
        b"Secret XMP Creator",
        b"SecretPiece",
        b"PagePiece",
        b"Incremental Secret",
    ):
        assert secret not in raw_bytes


def test_m1_metadata_sanitizer_strips_annots_metadata_and_measure(tmp_path: Path) -> None:
    src_pdf = tmp_path / "annot_meta_src.pdf"
    out_pdf = tmp_path / "annot_meta_clean.pdf"

    pdf = pikepdf.Pdf.new()
    page = pdf.add_blank_page()
    annot = pdf.make_indirect(
        pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/Annot"),
                "/Subtype": pikepdf.Name("/Text"),
                "/Rect": pikepdf.Array([20, 20, 40, 40]),
                "/Metadata": pdf.make_stream(b"<x:xmpmeta><dc:creator>ANNOT-XMP-SECRET</dc:creator></x:xmpmeta>"),
                "/Measure": pikepdf.Dictionary({"/Subtype": pikepdf.Name("/RL"), "/Private": "ANNOT-MEASURE-SECRET"}),
            }
        )
    )
    page["/Annots"] = pikepdf.Array([annot])
    pdf.save(src_pdf)

    pre = inspect_pdf_metadata(src_pdf)
    assert pre.has_xmp_metadata is True
    assert any("Annots" in loc for loc in pre.xmp_locations)

    res = sanitize_pdf_metadata(src_pdf, out_pdf)
    assert res.remaining_xmp is False
    assert res.removed_xmp_streams >= 1

    with pikepdf.Pdf.open(out_pdf) as cleaned:
        cleaned_annot = cleaned.pages[0]["/Annots"][0]
        assert "/Metadata" not in cleaned_annot
        assert "/Measure" not in cleaned_annot

    raw = out_pdf.read_bytes()
    assert b"ANNOT-XMP-SECRET" not in raw
    assert b"ANNOT-MEASURE-SECRET" not in raw


# ---------------------------------------------------------------------------
# Feature #9: Feedback Reporter & Cloudflare Worker
# ---------------------------------------------------------------------------


def test_m1_feedback_reporter_anonymizer_and_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("USERNAME", "AliceUser")
    monkeypatch.setenv("COMPUTERNAME", "WORKSTATION-99")
    monkeypatch.setenv("USERDOMAIN", "CORPDOMAIN")

    dirty = (
        r"User AliceUser on WORKSTATION-99 (CORPDOMAIN) opened "
        r"C:\Users\AliceUser\OneDrive - Acme Corp\Contrato_Privado.pdf and "
        r"D:\Users\BobSmith\notas_secretas.md and /home/carol/manual.markdown. "
        r"Email: alice.user@example.com IP: 192.168.1.50:8080 "
        r"IPv6: 2001:db8::1 and [fe80::1%eth0]:443. "
        r"Version v0.1.0 OS 10.0.26100.1742 Time 12:34:56."
    )
    clean = anonymize_text(dirty)
    for leaked in (
        "AliceUser",
        "WORKSTATION-99",
        "CORPDOMAIN",
        "Acme Corp",
        "BobSmith",
        "carol",
        "alice.user@example.com",
        "192.168.1.50",
        "2001:db8::1",
        "fe80::1",
        "Contrato_Privado.pdf",
        "notas_secretas.md",
        "manual.markdown",
    ):
        assert leaked not in clean, f"PII leaked in anonymized output: {leaked}"

    assert "v0.1.0" in clean
    assert "10.0.26100.1742" in clean
    assert "12:34:56" in clean
    assert "<documento.pdf>" in clean
    assert "<documento.md>" in clean
    assert "<documento.markdown>" in clean
    # Idempotency
    assert anonymize_text(clean) == clean

    report = build_feedback_report(
        kind=KIND_BUG,
        title="Error con C:\\Users\\AliceUser\\secreto.pdf",
        description=dirty,
        include_diagnostics=False,
    )
    assert "secreto.pdf" not in report.title
    assert "AliceUser" not in report.markdown_body

    md_path, json_path = save_local_report(report, reports_dir=tmp_path / "reports")
    assert md_path.exists() and json_path.exists()
    assert "AliceUser" not in md_path.read_text(encoding="utf-8")

    # Clipboard payload re-anonymizes user edits
    clip = build_clipboard_payload("Edited by alice@example.com on 10.20.30.40")
    assert "alice@example.com" not in clip and "10.20.30.40" not in clip

    # Endpoint submit blocks insecure non-localhost HTTP and sends over mocked HTTPS transport
    ok_bad, msg_bad = submit_feedback_endpoint(report, "http://insecure.example.com/feedback")
    assert not ok_bad and msg_bad == "insecure_endpoint_blocked"

    captured: list[tuple[str, dict]] = []

    def _mock_transport(url: str, body_bytes: bytes, _headers: dict[str, str], _timeout: float) -> tuple[int, str]:
        captured.append((url, json.loads(body_bytes.decode("utf-8"))))
        return 201, '{"ok": true}'

    ok_send, _ = submit_feedback_endpoint(
        report,
        "https://support.example.com/api/report",
        edited_body="User edited report with 172.16.0.5 and secret.pdf",
        transport=_mock_transport,
    )
    assert ok_send and len(captured) == 1
    assert "172.16.0.5" not in captured[0][1]["body"]
    assert "secret.pdf" not in captured[0][1]["body"]

    # mailto & GitHub Issue URLs enforce caps & fence closure
    long_code = "```python\n" + "\n".join(f"line_{i} = {i}" for i in range(250))
    mailto_uri, mail_trunc = build_mailto_uri("Bug", long_code, max_chars=400)
    assert mail_trunc and len(mailto_uri) <= 400 and mailto_uri.startswith("mailto:")

    gh_url, gh_trunc = build_github_issue_url("Bug", long_code, max_chars=500)
    assert gh_trunc and len(gh_url) <= 500
    assert "%60%60%60" in gh_url

    # Validate Cloudflare worker files
    wrangler_cfg = tomllib.loads((ROOT / "deploy" / "feedback-worker" / "wrangler.toml").read_text(encoding="utf-8"))
    assert wrangler_cfg["name"] == "pdf-md-lector-feedback-worker"
    assert "GITHUB_TOKEN" not in json.dumps(wrangler_cfg)

    worker_js = (ROOT / "deploy" / "feedback-worker" / "worker.js").read_text(encoding="utf-8")
    assert "env.GITHUB_TOKEN" in worker_js
    assert "Access-Control-Allow-Origin" in worker_js
    assert "32768" in worker_js
    assert "unredacted_pii_detected" in worker_js
    assert "ghp_" not in worker_js and "github_pat_" not in worker_js


def test_m1_feedback_anonymizer_edge_cases_and_node_worker(tmp_path: Path) -> None:
    del tmp_path
    cases = [
        (r"D:\Users\Jane Doe\OneDrive - Acme Corp\Secret.pdf", "<documento.pdf>"),
        (r"\\?\C:\Users\Admin\notes.markdown", "<documento.markdown>"),
        (r"\\?\UNC\server\share\dir\secret.pdf", "<documento.pdf>"),
        (r"\\?\unc\my-srv.local\Shared Docs\My Report.markdown", "<documento.markdown>"),
        (r"\\?\UNC\192.168.1.50\share\sub\secret.md", "<documento.md>"),
        (r"C:\Users\Jane Doe\Documents\my confidential report.pdf", "<documento.pdf>"),
        ("/var/tmp/private_dir/secret.pdf", "<documento.pdf>"),
        ("/home/carol/docs/manual.markdown", "<documento.markdown>"),
        ("::ffff:192.0.2.128", "<ip>"),
        (
            r"D:\Users\Jane Doe\OneDrive - Acme Corp\config.ini",
            r"D:\Users\<usuario>\OneDrive - <organizacion>\config.ini",
        ),
        (
            r"Log at \\?\UNC\server\share\dir\secret.txt",
            r"Log at \\?\UNC\<equipo>\<dato_privado>\dir\secret.txt",
        ),
        (
            r"Log at \\server\share\dir\secret.txt",
            r"Log at \\<equipo>\<dato_privado>\dir\secret.txt",
        ),
        (
            r"Log at \\?\UNC\server\share\Users\Admin\notes.txt",
            r"Log at \\?\UNC\<equipo>\<dato_privado>\Users\<usuario>\notes.txt",
        ),
        (
            r"Log at \\server\share\Users\Admin\notes.txt",
            r"Log at \\<equipo>\<dato_privado>\Users\<usuario>\notes.txt",
        ),
        ("See (secret.pdf) and [notes.markdown] now", "See (<documento.pdf>) and [<documento.markdown>] now"),
        (
            r"Copied C:\Users\Alice\a.txt to C:\Users\Bob\b.pdf",
            r"Copied C:\Users\<usuario>\a.txt to <documento.pdf>",
        ),
        ("v0.1.0 10.0.26100.1742 14:05:09", "v0.1.0 10.0.26100.1742 14:05:09"),
    ]
    for raw_in, expected in cases:
        once = anonymize_text(raw_in)
        assert once == expected
        assert anonymize_text(once) == once

    node_bin = shutil.which("node")
    if node_bin is None:
        pytest.skip("Node.js not installed")

    worker_uri = (ROOT / "deploy" / "feedback-worker" / "worker.js").as_uri()
    script = f"""
import worker from {json.dumps(worker_uri)};
const post = async (body, env = {{}}) => {{
  const req = new Request("https://example.workers.dev/", {{
    method: "POST",
    headers: {{ "Content-Type": "application/json" }},
    body: JSON.stringify({{ title: "Bug report", body, kind: "bug", version: "0.1.0" }}),
  }});
  const res = await worker.fetch(req, env);
  return {{ status: res.status, data: await res.json() }};
}};
for (const okText of [
  "Error opening <documento.pdf>",
  "Broken link in <documento.md>",
  "Broken link in <documento.markdown>",
  "In C:\\\\Users\\\\<usuario>\\\\OneDrive - <organizacion>\\\\config.ini",
  "At /Users/<usuario>/config.ini",
  "Log at \\\\\\\\?\\\\UNC\\\\<equipo>\\\\<dato_privado>\\\\Users\\\\<usuario>\\\\notes.txt",
  "Log at \\\\\\\\<equipo>\\\\<dato_privado>\\\\dir\\\\secret.txt",
  "Timestamp 12:34:56 and <ip> ok",
]) {{
  const r = await post(okText, {{}});
  if (r.status !== 503 || r.data.error !== "server_not_configured") {{
    throw new Error("Expected 503 for " + okText + ", got " + JSON.stringify(r));
  }}
}}
for (const badText of [
  "Error opening secret.pdf",
  "Error opening documento.pdf",
  "Error opening <secret.pdf>",
  "Contact alice@example.com",
  "At C:\\\\Users\\\\Alice\\\\config.ini",
  "At OneDrive - Acme Corp",
  "At \\\\\\\\?\\\\UNC\\\\server\\\\share\\\\dir\\\\secret.txt",
  "At \\\\\\\\server\\\\share\\\\dir\\\\secret.txt",
  "Client 2001:db8::1 failed",
  "Client [fe80::1%eth0]:443 failed",
  "Client ::ffff:192.0.2.128 failed",
]) {{
  const r = await post(badText, {{}});
  if (r.status !== 422 || r.data.error !== "unredacted_pii_detected") {{
    throw new Error("Expected 422 for " + badText + ", got " + JSON.stringify(r));
  }}
}}
const origFetch = globalThis.fetch;
globalThis.fetch = async () => new Response(JSON.stringify({{ number: 7, html_url: "https://example.invalid/7" }}), {{ status: 201 }});
try {{
  const created = await post("Clean <documento.pdf> on OneDrive - <organizacion>", {{ GITHUB_TOKEN: "tok" }});
  if (created.status !== 201 || !created.data.ok) {{
    throw new Error("Expected 201 with mocked fetch, got " + JSON.stringify(created));
  }}
}} finally {{
  globalThis.fetch = origFetch;
}}
"""
    proc = subprocess.run(
        [node_bin, "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert proc.returncode == 0, f"Node worker check failed: {proc.stderr or proc.stdout}"


# ---------------------------------------------------------------------------
# Feature #10: NotificationCenter & TaskController
# ---------------------------------------------------------------------------


def test_m1_notification_center_and_task_controller(qapp: QApplication, tmp_path: Path) -> None:
    del qapp
    nc = NotificationCenter(app_dir=tmp_path)
    p1 = nc.add("COMPLETADO", "PDF listo", "Guardado", category="task", persistent=True)
    e1 = nc.add("AVISO", "En sesion", "Solo sesion", category="general", persistent=False)
    assert nc.unread_count() == 2

    # Reload from disk: persistent remains, ephemeral does not
    nc_reloaded = NotificationCenter(app_dir=tmp_path)
    ids = [i.notif_id for i in nc_reloaded.list_items()]
    assert p1.notif_id in ids and e1.notif_id not in ids

    assert nc.mark_read(p1.notif_id) is True
    assert nc.unread_count() == 1
    nc.mark_all_read()
    assert nc.unread_count() == 0
    assert nc.remove(e1.notif_id) is True
    nc.clear_all(include_persistent=True)
    assert nc.list_items() == []

    # Concurrent adds
    errs: list[Exception] = []

    def _worker_add(idx: int) -> None:
        try:
            nc.add("SUGERENCIA", f"N{idx}", f"Detail {idx}", persistent=False)
        except Exception as exc:
            errs.append(exc)

    threads = [threading.Thread(target=_worker_add, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errs == []
    assert len(nc.list_items()) == 10

    # SessionManager & TaskController + AsyncTask
    sm = SessionManager(app_dir=tmp_path)
    sid = new_session_id()
    sm.save_session(
        TaskSessionRecord(
            session_id=sid,
            kind="pdf_ocr",
            title="OCR batch",
            created_at="2026-11-24T00:00:00Z",
            percent=40,
            total_items=10,
            processed_items=4,
            pending_paths=["a.pdf"],
        )
    )
    loaded = sm.load_session(sid)
    assert loaded is not None and loaded.percent == 40
    assert len(sm.list_sessions()) == 1
    assert sm.remove_session(sid) is True

    mgr = TaskManager()
    ctrl = TaskController()
    progress_events: list[int] = []
    canceled_events: list[str] = []
    paused_events: list[tuple[str, int]] = []
    done_events: list[object] = []

    def _cancellable_job(progress_cb, controller: TaskController) -> str:
        for i in range(5):
            stop_point(controller, "stopped")
            progress_cb((i + 1) * 20, f"step {i}")
            if i == 1:
                controller.cancel()
            time.sleep(0.02)
        return "finished"

    t_cancel = AsyncTask(_cancellable_job, controller=ctrl, min_emit_interval_s=0.0)
    mgr.submit(
        t_cancel,
        on_progress=lambda _tid, pct, _msg: progress_events.append(pct),
        on_canceled=lambda _tid, detail: canceled_events.append(detail),
        on_finished=lambda _tid, res: done_events.append(res),
    )
    assert _pump_until(lambda: len(canceled_events) == 1, timeout_s=3.0)
    assert done_events == []
    assert len(progress_events) >= 1

    # Pause-and-save job
    ctrl2 = TaskController()

    def _pause_save_job(controller: TaskController) -> str:
        controller.pause_and_save()
        if controller.should_save_checkpoint:
            raise TaskPausedAndSaved("sess_x", 55, "Saved checkpoint")
        return "never"

    t_pause = AsyncTask(_pause_save_job, controller=ctrl2)
    mgr.submit(
        t_pause,
        on_paused_saved=lambda _tid, s_id, pct, _d: paused_events.append((s_id, pct)),
    )
    assert _pump_until(lambda: len(paused_events) == 1, timeout_s=3.0)
    assert paused_events[0] == ("sess_x", 55)
    assert mgr.wait_for_done(2000)
    assert _pump_until(lambda: len(mgr._retired) == 0, timeout_s=2.0)
    assert len(mgr._active) == 0
    assert len(mgr._retired) == 0


def test_m1_notification_center_persistent_eviction_syncs_disk(tmp_path: Path) -> None:
    nc = NotificationCenter(app_dir=tmp_path, max_items=3)
    p1 = nc.add("COMPLETADO", "Task 1", "Persist 1", persistent=True)
    p2 = nc.add("COMPLETADO", "Task 2", "Persist 2", persistent=True)
    nc.add("AVISO", "Ephemeral 1", "Temp 1", persistent=False)
    # Adding a second non-persistent item evicts p1 past max_items=3 -> must sync disk
    nc.add("AVISO", "Ephemeral 2", "Temp 2", persistent=False)

    nc_reloaded = NotificationCenter(app_dir=tmp_path, max_items=3)
    reloaded_ids = [item.notif_id for item in nc_reloaded.list_items()]
    assert p1.notif_id not in reloaded_ids
    assert p2.notif_id in reloaded_ids


# ---------------------------------------------------------------------------
# Feature #11: LocalUsageInsights Dynamic Quick Actions
# ---------------------------------------------------------------------------


def test_m1_usage_insights_dynamic_quick_actions_and_reset(tmp_path: Path) -> None:
    defaults = [
        "sign_pdf",
        "merge_pdf",
        "delete_pages",
        "unlock_pdf",
        "convert_from_pdf",
        "compress_pdf",
    ]
    insights = LocalUsageInsights(tmp_path / "insights.json")
    assert insights.get_dynamic_quick_actions(defaults, limit=6) == defaults

    for _ in range(5):
        insights.record_tool_use("compress_pdf")
    for _ in range(3):
        insights.record_tool_use("pdf_ocr")
    for _ in range(2):
        insights.record_tool_use("redact_pdf")

    ranked = insights.get_dynamic_quick_actions(defaults, limit=6)
    assert len(ranked) == 6
    assert len(set(ranked)) == 6
    assert ranked[0] == "compress_pdf"
    assert ranked[1] == "pdf_ocr"
    assert ranked[2] == "redact_pdf"

    # Disabled returns defaults cleanly
    insights.set_enabled(False)
    assert insights.get_dynamic_quick_actions(defaults, limit=6) == defaults
    insights.set_enabled(True)

    # Clear history resets to exact 6 initial defaults
    insights.clear_history()
    assert insights.get_dynamic_quick_actions(defaults, limit=6) == defaults


def test_m1_usage_insights_concurrency_and_valid_ids(tmp_path: Path) -> None:
    insights = LocalUsageInsights(tmp_path / "conc_insights.json")
    errors: list[Exception] = []

    def _worker(idx: int) -> None:
        try:
            for _ in range(25):
                insights.record_tool_use(f"tool_{idx % 4}")
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert sum(insights._state.tool_counts.values()) == 200

    # valid_ids acts as strict allowlist on both recorded tools and default_ids
    filtered = insights.get_dynamic_quick_actions(
        ["tool_0", "removed_default_a", "removed_default_b"],
        limit=6,
        valid_ids=["tool_0", "tool_1"],
    )
    assert set(filtered).issubset({"tool_0", "tool_1"})
    insights.set_enabled(False)
    filtered_disabled = insights.get_dynamic_quick_actions(
        ["tool_0", "removed_default_a"],
        limit=6,
        valid_ids=["tool_0", "tool_1"],
    )
    assert filtered_disabled == ["tool_0"]


def test_m1_usage_insights_multi_instance_sync_and_lock_equivalence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pdf_lector.core import usage_insights

    shared_path = tmp_path / "shared_insights.json"
    errors: list[Exception] = []

    def _separate_worker(idx: int) -> None:
        try:
            for _ in range(25):
                LocalUsageInsights(shared_path).record_tool_use(f"tool_{idx % 4}")
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=_separate_worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert sum(LocalUsageInsights(shared_path)._state.tool_counts.values()) == 200

    # Sequential pre-instantiated instances share bucket and stay in sync
    sync_path = tmp_path / "sync_insights.json"
    inst1 = LocalUsageInsights(sync_path)
    inst2 = LocalUsageInsights(sync_path)

    inst1.record_tool_use("merge_pdf")
    inst2.record_tool_use("split_pdf")
    assert inst1._state.tool_counts == {"merge_pdf": 1, "split_pdf": 1}
    assert inst2._state.tool_counts == {"merge_pdf": 1, "split_pdf": 1}

    inst1.set_enabled(False)
    assert inst2.enabled is False
    inst1.set_enabled(True)
    assert inst2.enabled is True

    inst2.clear_history()
    assert inst1._state.tool_counts == {}
    assert inst1.enabled is True

    # External file deletion resets enabled=True and clears counters in-place
    inst1.set_enabled(False)
    sync_path.unlink()
    assert inst2.enabled is True
    assert inst1.enabled is True
    assert inst2._state.tool_counts == {}

    # Rapid external same-size atomic_write_text updates are detected immediately
    payload_a = json.dumps(
        {
            "enabled": True,
            "tool_counts": {"tool_a": 1},
            "doc_type_counts": {},
            "search_topic_counts": {},
            "dict_lang_counts": {},
        }
    )
    payload_b = json.dumps(
        {
            "enabled": True,
            "tool_counts": {"tool_b": 9},
            "doc_type_counts": {},
            "search_topic_counts": {},
            "dict_lang_counts": {},
        }
    )
    assert len(payload_a) == len(payload_b)
    atomic_write_text(sync_path, payload_a)
    assert inst1.get_dynamic_quick_actions(["tool_b", "tool_a"], limit=2)[0] == "tool_a"
    atomic_write_text(sync_path, payload_b)
    assert inst1.get_dynamic_quick_actions(["tool_a", "tool_b"], limit=2)[0] == "tool_b"

    # \\?\ lock equivalence
    if IS_WINDOWS:
        ext_path = Path(rf"\\?\{sync_path}")
        assert usage_insights._lock_for_path(sync_path) is usage_insights._lock_for_path(ext_path)
        expected_key = canonical_lock_key(sync_path)
        assert sum(1 for k in usage_insights._PATH_LOCKS if k == expected_key) == 1
        assert sum(1 for k in usage_insights._BUCKETS if k == expected_key) == 1

    # PermissionError retries and re-raises when exhausted (never silently wipes counters)
    perm_path = tmp_path / "perm_insights.json"
    perm_inst = LocalUsageInsights(perm_path)
    perm_inst.record_tool_use("sign_pdf")

    monkeypatch.setattr(usage_insights.time, "sleep", lambda _s: None)
    orig_read_text = Path.read_text

    def _always_perm_err(self: Path, *args, **kwargs) -> str:
        if self == perm_path:
            raise PermissionError("locked")
        return orig_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _always_perm_err)
    # Invalidate cached stat to force _load()
    perm_inst._bucket.loaded = False
    with pytest.raises(PermissionError):
        perm_inst.record_tool_use("merge_pdf")

    monkeypatch.setattr(Path, "read_text", orig_read_text)
    assert LocalUsageInsights(perm_path)._state.tool_counts == {"sign_pdf": 1}


# ---------------------------------------------------------------------------
# Phase-1 Startup Lazy Gate (< 300ms & zero heavy imports)
# ---------------------------------------------------------------------------


def test_m1_phase1_startup_stays_lazy_and_under_300ms() -> None:
    env = dict(os.environ)
    env["PDF_LECTOR_BENCH_STARTUP"] = "1"
    env["PDF_LECTOR_BENCH_EXIT"] = "1"
    env.setdefault("QT_QPA_PLATFORM", "offscreen")

    code = (
        "import sys, pdf_lector.app as a; "
        "rc = a.launch_app(['--fast-boot']); "
        "heavy = [m for m in ('pikepdf','pypdf','pyhanko','fastapi','uvicorn','markdown_it','nh3') if m in sys.modules]; "
        "print('HEAVY=' + ','.join(heavy)); "
        "raise SystemExit(rc)"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
        check=True,
    )
    match = re.search(r"FIRST_FRAME_MS=([0-9.]+)", proc.stdout)
    assert match is not None, f"Missing FIRST_FRAME_MS in: {proc.stdout}"
    assert float(match.group(1)) < 300.0
    assert "HEAVY=\n" in proc.stdout or proc.stdout.strip().endswith("HEAVY=")
