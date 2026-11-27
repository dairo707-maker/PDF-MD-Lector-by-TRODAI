"""M4 unit and UI integration tests: polygonal SVGs, WinOptiAI widgets/localizer, SHA-256 updater, Hub, Workspaces & Dialogs."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton
import shiboken6

from pdf_lector import get_base_dir
from pdf_lector.core.bookmarks_store import BookmarksStore
from pdf_lector.core.dictionary_engine import DictionaryEngine
from pdf_lector.core.notifications import NotificationCenter
from pdf_lector.core.pdf_engine import generate_sample_pdf
from pdf_lector.core.tools import ToolRegistry
from pdf_lector.core.updater import (
    ReleaseInfo,
    build_detached_update_script,
    check_for_updates,
    download_verified_update,
    parse_sha256sums,
    should_run_auto_check,
)
from pdf_lector.core.usage_insights import LocalUsageInsights
from pdf_lector.theme.palette import register_embedded_fonts
from pdf_lector.theme.theme_profile import get_preset
from pdf_lector.ui.about_updater_dialog import AboutUpdaterDialog
from pdf_lector.ui.diagnostics_dialog import DiagnosticsDialog
from pdf_lector.ui.dialog_localizer import DialogLocalizer
from pdf_lector.ui.invoice_dialog import InvoiceDialog
from pdf_lector.ui.main_window import MainWindow
from pdf_lector.ui.notifications_drawer import NotificationsTasksDialog
from pdf_lector.ui.preferences_dialog import PreferencesDialog
from pdf_lector.ui.support_dialog import SupportDialog
from pdf_lector.ui.svg_icons import load_svg_icon
from pdf_lector.ui.tool_workspace import ToolWorkspace
from pdf_lector.ui.tour_dialog import TourDialog
from pdf_lector.ui.widgets import AdaptiveButton, StatusTag, ToolCard


@pytest.fixture(scope="session")
def qapp() -> QApplication:
    app = QApplication.instance() or QApplication(["pytest-m4"])
    register_embedded_fonts(app)
    return app


def test_m4_new_polygonal_svgs_and_icon_renderer(qapp: QApplication) -> None:
    del qapp
    icons_dir = get_base_dir() / "assets" / "icons"
    curve_re = re.compile(r"[CSQTacsqta]")
    for name in ("bell", "alert", "refresh", "folder", "markdown", "ink", "ai_spark", "settings", "bug", "copy"):
        svg = icons_dir / f"{name}.svg"
        assert svg.exists(), f"Missing {svg}"
        root = ET.parse(svg).getroot()
        for elem in root.iter():
            tag = elem.tag.split("}")[-1].lower()
            assert tag not in ("circle", "ellipse")
            if tag == "path":
                assert not curve_re.search(elem.attrib.get("d", ""))
        icon = load_svg_icon(name, color="#F8FAFC", size=20)
        assert not icon.isNull()
    assert load_svg_icon("definitely_missing_icon_abc").isNull()


def test_m4_adaptive_button_and_dialog_localizer_roles(qapp: QApplication) -> None:
    btn = AdaptiveButton()
    btn.set_variants(["Descargar e instalar actualizacion completa", "Actualizar", "OK"])
    assert btn.sizeHint().width() >= btn.minimumSizeHint().width()
    btn.resize(110, 34)
    qapp.processEvents()
    assert btn.text() in ("Actualizar", "OK")
    assert "Descargar" in btn.toolTip()
    btn.resize(58, 32)
    qapp.processEvents()
    assert btn.text() == "OK"

    loc = DialogLocalizer(qapp)
    loc.set_language("pt")
    box = QMessageBox(QMessageBox.Icon.Warning, "Aviso", "Confirmar?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
    loc.localize_dialog(box)
    assert box.button(QMessageBox.StandardButton.Yes).text() == "Sim"
    assert box.button(QMessageBox.StandardButton.Yes).property("role") == "RiskAction"
    assert box.button(QMessageBox.StandardButton.Cancel).property("role") == "GhostAction"

    loc.set_language("unknown-xx")
    loc.localize_dialog(box)
    assert box.button(QMessageBox.StandardButton.Yes).text() == "S\u00ed"


def test_m4_tour_dialog_shiboken6_deleted_guard(qapp: QApplication) -> None:
    steps: list[int] = []
    dlg = TourDialog(lang="es", on_step_change=steps.append)
    dlg.next_step()
    dlg.finish_tour()
    assert dlg.completed is True
    assert steps == [1]
    dlg.deleteLater()
    qapp.processEvents()
    assert shiboken6.isValid(dlg) is False
    assert dlg.safe_refresh_step() is None


def test_m4_updater_cadence_offline_sha256_and_meipass_script(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    assert should_run_auto_check(None, offline_only=False) is True
    assert should_run_auto_check((now - timedelta(hours=3)).isoformat(), offline_only=False) is False
    assert should_run_auto_check((now - timedelta(hours=25)).isoformat(), offline_only=False) is True
    assert should_run_auto_check(None, offline_only=True) is False

    called: list[str] = []
    assert (
        check_for_updates(
            "0.1.0",
            offline_only=True,
            fetcher=lambda u, _t: (called.append(u) or b"{}"),
        )
        is None
    )
    assert called == []

    payload = {
        "tag_name": "v0.3.0",
        "body": "Release v0.3.0",
        "html_url": "https://example.invalid/rel",
        "assets": [
            {"name": "PDF-MD-Lector-Setup.exe", "browser_download_url": "https://example.invalid/setup.exe", "size": 123},
            {"name": "SHA256SUMS.txt", "browser_download_url": "https://example.invalid/SHA256SUMS.txt", "size": 70},
        ],
    }
    rel = check_for_updates("0.1.0", fetcher=lambda _u, _t: json.dumps(payload).encode("utf-8"))
    assert rel is not None and rel.update_available is True and rel.version == "0.3.0"

    digest = "a" * 64
    assert parse_sha256sums(f"{digest}  PDF-MD-Lector-Setup.exe\n")["PDF-MD-Lector-Setup.exe"] == digest

    script = build_detached_update_script(
        installer_path=tmp_path / "PDF-MD-Lector-Setup.exe",
        relaunch_exe=tmp_path / "PDF-MD-Lector-Portable.exe",
    )
    assert "_MEIPASS" in script and "PDF-MD-Lector-Setup.exe" in script


def test_m4_updater_local_http_verification_and_tamper_delete(tmp_path: Path) -> None:
    from tests.e2e.conftest import LocalMockServer
    from tests.e2e.fixtures import make_update_bundle

    srv = LocalMockServer().start()
    try:
        good = make_update_bundle(tmp_path / "good", version="0.2.0", tamper=False, base_url=srv.base_url)
        srv.set_route("GET", "/releases/download/v0.2.0/PDF-MD-Lector-Setup.exe", 200, good["installer_bytes"], "application/octet-stream")
        srv.set_route("GET", "/releases/download/v0.2.0/SHA256SUMS.txt", 200, good["sums_text"], "text/plain")
        pcts: list[int] = []
        out = download_verified_update(
            asset_url=f"{srv.base_url}/releases/download/v0.2.0/PDF-MD-Lector-Setup.exe",
            sums_url=f"{srv.base_url}/releases/download/v0.2.0/SHA256SUMS.txt",
            dest_dir=tmp_path / "dl_ok",
            progress_cb=pcts.append,
        )
        assert out.exists() and hashlib.sha256(out.read_bytes()).hexdigest() == good["expected_sha256"]
        assert 100 in pcts

        bad = make_update_bundle(tmp_path / "bad", version="0.2.0", tamper=True, base_url=srv.base_url)
        srv.set_route("GET", "/releases/download/v0.2.0/PDF-MD-Lector-Setup.exe", 200, bad["installer_bytes"], "application/octet-stream")
        dl_bad = tmp_path / "dl_bad"
        with pytest.raises(ValueError, match="sha256"):
            download_verified_update(
                asset_url=f"{srv.base_url}/releases/download/v0.2.0/PDF-MD-Lector-Setup.exe",
                sums_url=f"{srv.base_url}/releases/download/v0.2.0/SHA256SUMS.txt",
                dest_dir=dl_bad,
            )
        assert not (dl_bad / "PDF-MD-Lector-Setup.exe").exists()
    finally:
        srv.stop()


def test_m4_hub_dynamic_quick_actions_tool_workspace_and_dialogs(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    insights = LocalUsageInsights(tmp_path / "insights.json")
    win = MainWindow(
        get_preset("trodai_corporate"),
        persist_profile=False,
        sample_pdf_dir=tmp_path,
        insights_store=insights,
        bookmarks_store=BookmarksStore(tmp_path / "bm.json"),
        dictionary_engine=DictionaryEngine(tmp_path / "dicts"),
        notification_center=NotificationCenter(file_path=tmp_path / "notif.json"),
        updater_state_path=tmp_path / "upd.json",
    )
    try:
        assert not hasattr(win, "proto_pill")
        assert win.hub_view.quick_action_tool_ids() == [
            "sign_pdf",
            "merge_pdf",
            "delete_pages",
            "unlock_pdf",
            "convert_from_pdf",
            "compress_pdf",
        ]
        for _ in range(6):
            insights.record_tool_use("pdf_ocr")
        win.hub_view.refresh_recommendations()
        assert win.hub_view.quick_action_tool_ids()[0] == "pdf_ocr"

        win.hub_view.rec_clear_btn.click()
        qapp.processEvents()
        assert win.hub_view.quick_action_tool_ids()[0] == "sign_pdf"

        # Verify ToolWorkspace executes tool with 1 PrimaryAction and rejects UNC
        src_pdf = generate_sample_pdf(tmp_path / "sample.pdf")
        out_pdf = tmp_path / "compressed.pdf"
        ws = ToolWorkspace(ToolRegistry.get("compress_pdf"))
        try:
            assert ws.add_input_path(Path(r"\\evil\share\a.pdf")) is False
            assert ws.validate_params({"dpi": 9999}) is False
            ws.set_inputs([src_pdf])
            ws.set_output(out_pdf)
            res = ws.execute_primary_action()
            assert res.success is True and out_pdf.exists()
            primary_btns = [b for b in ws.findChildren(QPushButton) if b.property("role") == "PrimaryAction"]
            assert len(primary_btns) == 1
        finally:
            ws.close()

        # Verify Update Banner & skipped_version
        rel = ReleaseInfo(
            version="0.4.0",
            tag_name="v0.4.0",
            notes="Notes",
            html_url="https://example.invalid",
            asset_url="https://example.invalid/setup.exe",
            sums_url="https://example.invalid/SHA256SUMS.txt",
            update_available=True,
        )
        win.show_update_banner(rel)
        assert win.update_banner.isVisible()
        assert "rgba(" not in win.update_banner.styleSheet().lower()
        win.skip_update_version("0.4.0")
        assert not win.should_show_background_update(rel)

        # Verify all 6 M4 dialogs open cleanly
        for dlg in (
            InvoiceDialog(),
            PreferencesDialog(prefs_path=tmp_path / "prefs.json", insights_store=insights),
            NotificationsTasksDialog(notification_center=win.notification_center),
            SupportDialog(initial_logs="RuntimeError: C:\\Users\\Alice\\OneDrive\\secret.pdf"),
            DiagnosticsDialog(logs_dir=tmp_path / "logs"),
            AboutUpdaterDialog(state_path=tmp_path / "upd.json"),
        ):
            try:
                dlg.show()
                qapp.processEvents()
                assert dlg.isVisible()
            finally:
                dlg.close()
    finally:
        win.close()


def test_m4_cli_version_and_phase1_bench_under_300ms() -> None:
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    ver = subprocess.run(
        [sys.executable, "-m", "pdf_lector.app", "--version"],
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
        check=True,
    )
    assert "0.4.1" in ver.stdout

    env["PDF_LECTOR_BENCH_STARTUP"] = "1"
    env["PDF_LECTOR_BENCH_EXIT"] = "1"
    boot = subprocess.run(
        [sys.executable, "-m", "pdf_lector.app"],
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
        check=True,
    )
    match = re.search(r"FIRST_FRAME_MS=([0-9.]+)", boot.stdout)
    assert match is not None and float(match.group(1)) < 300.0
