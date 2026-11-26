"""v0.2.0 feature tests: crash fixes, AI hardware gate + MCP, dictionary lemmatization/Wiktionary, draggable sticky notes + NotesPanel, autosave recovery & direct updates."""

from __future__ import annotations

import gzip
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import threading
import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPointF, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
import shiboken6

from pdf_lector.core.ai_hardware import LocalAiHardwareReport, probe_local_ai_hardware
from pdf_lector.core.ai_rag import DocumentRagIndex, ask_document
from pdf_lector.core.autosave import SessionAutosaveManager
from pdf_lector.core.dictionary_engine import DictionaryEngine
from pdf_lector.core.pdf_annotations import (
    AnnotationRecord,
    load_pdf_annotations,
    save_pdf_annotations,
)
from pdf_lector.core.pdf_engine import PdfDocumentSession, generate_sample_pdf
from pdf_lector.core.updater import ReleaseInfo, UpdaterState, save_updater_state
from pdf_lector.theme.theme_profile import ThemeProfile
from pdf_lector.ui.about_updater_dialog import AboutUpdaterDialog
from pdf_lector.ui.ai_panel import AskDocumentPanel
from pdf_lector.ui.dictionary_panel import DictionaryPanel
from pdf_lector.ui.main_window import MainWindow
from pdf_lector.ui.pdf_canvas import PdfCanvas
from pdf_lector.ui.viewer_view import ViewerView
from pdf_lector.ui.widgets import ToolCard


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance() or QApplication([])
    return app


def test_toolcard_mouse_press_survives_synchronous_deletion(qapp: QApplication) -> None:
    card = ToolCard("merge", "Unir PDF", "Combinar varios PDF.", "DOCUMENTO", "merge")
    card.show()
    qapp.processEvents()

    def _destroy_on_click(_tid: str) -> None:
        card.hide()
        card.setParent(None)
        card.deleteLater()

    card.clicked.connect(_destroy_on_click)
    QTest.mouseClick(card, Qt.MouseButton.LeftButton)
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapp.processEvents()
    assert not shiboken6.isValid(card)


def test_rag_index_pdf_thread_safe_pypdf(tmp_path: Path) -> None:
    pdf_path = generate_sample_pdf(tmp_path / "rag_sample.pdf")
    idx = DocumentRagIndex()
    added = idx.index_pdf(pdf_path)
    assert added >= 1
    hits = idx.search("TRODAI", top_k=3)
    assert hits and any(h.page_number == 1 for h in hits)


def test_ai_hardware_probe_and_panel_gate(qapp: QApplication) -> None:
    low_hw = probe_local_ai_hardware(
        ram_gb_override=8.0,
        vram_gb_override=0.0,
        ollama_installed_override=False,
    )
    assert low_hw.eligible_for_local_llm is False

    good_hw = probe_local_ai_hardware(
        ram_gb_override=32.0,
        vram_gb_override=8.0,
        ollama_installed_override=True,
    )
    assert good_hw.eligible_for_local_llm is True

    panel = AskDocumentPanel(hardware_report=low_hw, mode="dark", lang="es")
    try:
        panel.show()
        qapp.processEvents()
        # Local BM25 stays active
        assert panel.current_provider() == "local_bm25"
        assert panel.ask_btn.isEnabled()
        assert not panel._hw_gate_box.isVisible()

        # Selecting ollama blocks local generation and offers legal API / MCP switchers
        ollama_idx = panel.provider_combo.findData("ollama")
        assert ollama_idx >= 0
        panel.provider_combo.setCurrentIndex(ollama_idx)
        qapp.processEvents()
        assert panel._hw_gate_box.isVisible()
        assert not panel.ask_btn.isEnabled()

        # 1-click switch to Official MCP restores readiness with consent & DPAPI card
        panel.hw_mcp_btn.click()
        qapp.processEvents()
        assert panel.current_provider() == "mcp_official"
        assert panel.ask_btn.isEnabled()
        assert panel._byok_box.isVisible()
    finally:
        panel.close()


def test_mcp_official_rag_connector_enforces_consent() -> None:
    idx = DocumentRagIndex()
    idx.index_text("Contrato clausula quinta plazo treinta dias.", source_path="demo.pdf", page_number=2)

    blocked = ask_document(idx, "Cual es el plazo?", provider="mcp_official", consent_granted=False)
    assert blocked.consent_blocked is True

    captured: dict[str, object] = {}

    def _mock_transport(url: str, body: bytes, headers: dict[str, str], timeout_s: float) -> tuple[int, str]:
        captured["url"] = url
        captured["payload"] = json.loads(body.decode("utf-8"))
        resp = {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "Plazo de 30 dias [p. 2]."}]}}
        return 200, json.dumps(resp)

    ans = ask_document(
        idx,
        "Cual es el plazo?",
        provider="mcp_official",
        endpoint_url="http://127.0.0.1:8787/mcp",
        api_key="tok-secret",
        consent_granted=True,
        transport=_mock_transport,
    )
    assert ans.consent_blocked is False
    assert "30 dias" in ans.answer_text
    assert captured["payload"]["method"] == "tools/call"  # type: ignore[index]


def test_dictionary_morphology_fuzzy_gzip_and_wiktionary_cache(tmp_path: Path, qapp: QApplication) -> None:
    engine = DictionaryEngine(base_dir=tmp_path / "dicts")

    # Previously missing ES forms now resolve via morphology + expanded bundled lexicon
    assert engine.lookup("Personalizables", "es")
    assert engine.lookup("Fondos", "es")
    assert engine.lookup("backgrounds", "en")
    assert engine.lookup("categories", "en")

    # Fuzzy suggestions when typo/partial
    fuzz = engine.suggest_fuzzy("personalis", "es", limit=5)
    assert any("personaliz" in w.lower() for w in fuzz)

    # Gzip language pack support
    gz_payload = gzip.compress(json.dumps([{"word": "livro", "pos": "subst.", "definition": "Obra impressa.", "synonyms": ["texto"]}]).encode("utf-8"))
    cnt = engine.download_language_pack("pt", fetcher=lambda _u: gz_payload, consent_granted=True)
    assert cnt >= 1
    assert engine.lookup("livro", "pt")

    # Wiktionary consent + offline guard + SQLite caching
    with pytest.raises(PermissionError):
        engine.lookup_online_wiktionary("arquitectura", "es", consent_granted=True, offline_only=True)

    wiki_json = json.dumps(
        {
            "es": [
                {
                    "partOfSpeech": "Sustantivo femenino",
                    "definitions": [{"definition": "Arte y tecnica de <b>proyectar</b> y construir."}],
                }
            ]
        }
    ).encode("utf-8")
    online_hits = engine.lookup_online_wiktionary(
        "arquitectura",
        "es",
        consent_granted=True,
        offline_only=False,
        fetcher=lambda _u: wiki_json,
    )
    assert online_hits and "Arte y tecnica" in online_hits[0].definition
    # Cached locally for future offline lookups
    assert engine.lookup("arquitectura", "es")

    panel = DictionaryPanel(engine)
    try:
        panel.show()
        panel.lookup_word("personalisado", record_event=False)
        qapp.processEvents()
        assert panel._suggest_host.isVisible()
        assert panel._suggest_flow.count() >= 1
        assert panel.online_lookup_btn.isVisible()
    finally:
        panel.close()


def test_sticky_note_category_pdf_roundtrip_drag_card_and_notes_panel(tmp_path: Path, qapp: QApplication) -> None:
    pdf_path = generate_sample_pdf(tmp_path / "notes_test.pdf")
    note = AnnotationRecord(
        id="note-cat-1",
        page_index=0,
        kind="text",
        rect_pdf=(90.0, 620.0, 114.0, 644.0),
        points_pdf=(),
        color_hex="#86EFAC",
        author="Anonimo",
        timestamp_iso="2026-11-26T14:00:00Z",
        content="Revisar clausula",
        category="Importante",
    )
    save_pdf_annotations(pdf_path, [note], create_backup=False)
    loaded = load_pdf_annotations(pdf_path)
    assert any(r.id == "note-cat-1" and r.category == "Importante" for r in loaded)

    # Canvas drag + click floating card + delete
    sess = PdfDocumentSession(pdf_path)
    canvas = PdfCanvas()
    try:
        canvas.set_session(sess, 0)
        canvas.load_annotations(loaded)
        canvas.show()
        qapp.processEvents()

        pt = canvas._pdf_to_screen_point(102.0, 632.0)
        QTest.mouseClick(canvas, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, pt.toPoint())
        qapp.processEvents()
        assert canvas._sticky_card is not None and canvas._sticky_card.isVisible()

        # Move sticky note & delete
        moved = canvas.move_sticky_note("note-cat-1", (160.0, 500.0))
        assert moved is not None and moved.rect_pdf[0] == 160.0
        canvas.delete_annotation("note-cat-1")
        assert canvas.find_annotation("note-cat-1") is None
    finally:
        canvas.close()
        sess.close()

    # ViewerView NotesPanel integration
    viewer = ViewerView()
    try:
        assert viewer.open_pdf(pdf_path)
        viewer.show()
        viewer.set_notes_panel_visible(True)
        viewer._add_note_from_panel("Pendiente validar anexo", "Pendiente", "#7DD3FC")
        qapp.processEvents()
        assert viewer.notes_panel.isVisible()
        assert viewer.notes_panel.count_tag.text() == "2"
    finally:
        viewer.cleanup()
        viewer.close()


def test_autosave_snapshot_and_unexpected_crash_recovery(tmp_path: Path, qapp: QApplication) -> None:
    pdf_path = generate_sample_pdf(tmp_path / "recover_me.pdf")
    autosave_dir = tmp_path / "autosave"

    win1 = MainWindow(
        ThemeProfile(base_mode="dark", language="es"),
        persist_profile=False,
        autosave_dir=autosave_dir,
    )
    try:
        assert win1.open_document_path(pdf_path, switch_to_viewer=True)
        tab = win1.viewer_view.current_tab()
        assert tab is not None
        tab.set_page(1)
        tab.pdf_canvas.add_sticky_note((110.0, 580.0), "Nota antes del corte", category="Revisar")
        win1.save_autosave_snapshot(clean_exit=False)
    finally:
        # Avoid closeEvent marking clean_exit for crash simulation by testing manager directly first
        win1.viewer_view.cleanup()

    mgr = SessionAutosaveManager(base_dir=autosave_dir)
    assert mgr.should_offer_recovery(previous_crashed=True) is True
    assert mgr.should_offer_recovery(previous_crashed=False) is True

    win2 = MainWindow(
        ThemeProfile(base_mode="dark", language="es"),
        persist_profile=False,
        autosave_dir=autosave_dir,
    )
    try:
        win2.show()
        restored = win2.recover_previous_session(previous_crashed=True)
        qapp.processEvents()
        assert restored == 1
        assert win2.recovery_banner.isVisible()
        restored_tab = win2.viewer_view.current_tab()
        assert restored_tab is not None
        assert restored_tab.current_page == 1
        assert any(a.content == "Nota antes del corte" for a in restored_tab.shared_annotations.records)

        # Clean close marks snapshot clean
        win2.close()
        assert mgr.should_offer_recovery(previous_crashed=False) is False
    finally:
        if win2.isVisible():
            win2.close()


def test_about_updater_install_button_and_background_check(tmp_path: Path, qapp: QApplication) -> None:
    installer_bytes = b"MZ-MOCK-INSTALLER-0.2.0"
    digest = hashlib.sha256(installer_bytes).hexdigest()

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/releases/latest":
                port = self.server.server_port
                payload = json.dumps(
                    {
                        "tag_name": "v0.2.0",
                        "body": "Notas de v0.2.0",
                        "assets": [
                            {
                                "name": "PDF-MD-Lector-Setup.exe",
                                "browser_download_url": f"http://127.0.0.1:{port}/Setup.exe",
                            },
                            {
                                "name": "SHA256SUMS.txt",
                                "browser_download_url": f"http://127.0.0.1:{port}/SHA256SUMS.txt",
                            },
                        ],
                    }
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            elif self.path == "/Setup.exe":
                self.send_response(200)
                self.send_header("Content-Length", str(len(installer_bytes)))
                self.end_headers()
                self.wfile.write(installer_bytes)
            elif self.path == "/SHA256SUMS.txt":
                sums = f"{digest}  PDF-MD-Lector-Setup.exe\n".encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Length", str(len(sums)))
                self.end_headers()
                self.wfile.write(sums)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *_args) -> None:
            return

    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        base_url = f"http://127.0.0.1:{srv.server_port}"
        state_path = tmp_path / "updater.json"
        save_updater_state(UpdaterState(offline_only=False), state_path)

        dlg = AboutUpdaterDialog(
            current_version="0.1.0",
            api_url=f"{base_url}/releases/latest",
            state_path=state_path,
        )
        try:
            rel = dlg.check_now()
            assert rel is not None and rel.update_available
            assert dlg.download_btn.isEnabled()
            pkg = dlg.download_current_release()
            assert pkg is not None and pkg.exists()
            assert dlg.install_btn.isEnabled()

            called: dict[str, object] = {}

            def _mock_launcher(p: Path, *, target_exe=None) -> None:
                called["path"] = p

            ok = dlg.install_downloaded_release(
                launcher=_mock_launcher,
                quit_callback=lambda: called.setdefault("quit", True),
            )
            assert ok is True
            assert called.get("path") == pkg
            assert called.get("quit") is True
        finally:
            dlg.close()

        # Also test MainWindow background update detection -> update_banner
        win = MainWindow(
            ThemeProfile(base_mode="dark", language="es"),
            persist_profile=False,
            updater_state_path=state_path,
            autosave_dir=tmp_path / "auto2",
        )
        try:
            assert win.start_background_update_check(
                api_url=f"{base_url}/releases/latest",
                current_version="0.1.0",
                force=True,
            )
            for _ in range(50):
                qapp.processEvents()
                if win.update_banner.isVisible():
                    break
                QTest.qWait(20)
            assert win.update_banner.isVisible()
            assert "0.2.0" in win.update_banner_lbl.text()
        finally:
            win.close()
    finally:
        srv.shutdown()
        srv.server_close()
