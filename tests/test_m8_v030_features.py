"""v0.3.0 feature & UX tests: borderless icons, deduplicated chrome, installer language,
Wiktionary wikitext default, Ctrl+F search, Ctrl+P printing, F5 presentation + clicker, shortcuts & Hub motion."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from reportlab.pdfgen import canvas as rl_canvas

from pdf_lector.core.dictionary_engine import DictionaryEngine
from pdf_lector.core.pdf_engine import PdfDocumentSession
from pdf_lector.theme.motion_engine import MotionBackdropWidget, MotionPerformanceGuard
from pdf_lector.theme.palette import build_trodai_qss
from pdf_lector.theme.theme_profile import ThemeProfile, read_installer_language
from pdf_lector.ui.ai_panel import AskDocumentPanel
from pdf_lector.ui.dictionary_panel import DictionaryPanel
from pdf_lector.ui.main_window import MainWindow
from pdf_lector.ui.presentation_view import PresentationWindow
from pdf_lector.ui.print_dialog import PrintPdfDialog, parse_page_range
from pdf_lector.ui.shortcuts_dialog import SHORTCUT_GROUPS, ShortcutsDialog
from pdf_lector.ui.widgets import FlowContainer


def _qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    assert isinstance(app, QApplication)
    return app


def _make_sample_pdf(path: Path, pages: list[str]) -> Path:
    c = rl_canvas.Canvas(str(path), pagesize=(612, 792))
    for i, text in enumerate(pages):
        c.setFont("Helvetica", 12)
        c.drawString(72, 720, text)
        c.drawString(72, 680, f"TRODAI Slide {i + 1}")
        if i < len(pages) - 1:
            c.showPage()
    c.save()
    return path


def test_v030_borderless_qss_and_deduplicated_chrome(tmp_path: Path) -> None:
    app = _qapp()
    for mode in ("dark", "light"):
        qss = build_trodai_qss(mode)
        assert "QPushButton#TopUtilityBtn" in qss
        assert "QPushButton#TopUtilityBtn::menu-indicator" in qss
        assert "image: none;" in qss
        assert "border: 1px solid transparent;" in qss
        assert "QFrame#ViewerToolBar" in qss

    win = MainWindow(ThemeProfile(), persist_profile=False)
    try:
        # Deduplicated TopBar: no studio/ai/fullscreen buttons or runtime lang switcher
        for removed in ("top_studio_btn", "top_ai_btn", "top_fullscreen_btn", "lang_combo"):
            assert not hasattr(win, removed)
        # Deduplicated Viewer: no open_btn or settings_rail_btn
        for removed_vw in ("open_btn", "settings_rail_btn"):
            assert not hasattr(win.viewer_view, removed_vw)

        win.show()
        app.processEvents()

        # Centered 32x32 iconOnly TopBar buttons
        for btn in (win.top_notif_btn, win.top_settings_btn, win.top_menu_btn, win.top_theme_btn):
            assert btn.property("iconOnly") == "true"
            assert btn.maximumWidth() == 32 and btn.maximumHeight() == 32
            assert btn.width() == 32 and btn.height() == 32

        # Viewer toolbar buttons include Ctrl+F, Ctrl+P, F5 Present
        for btn in (
            win.viewer_view.find_btn,
            win.viewer_view.print_btn,
            win.viewer_view.present_btn,
            win.viewer_view.fullscreen_btn,
        ):
            assert btn.property("iconOnly") == "true"
            assert btn.maximumWidth() == 32 and btn.maximumHeight() == 32
            assert btn.minimumWidth() <= 32 and btn.minimumHeight() <= 32

        # Dictionary widened + FlowContainer chips + concise button text
        dp = DictionaryPanel(DictionaryEngine(base_dir=tmp_path / "dict_dir"))
        assert dp.minimumWidth() >= 296
        assert isinstance(dp._suggest_host, FlowContainer)
        assert "permiso" not in dp.online_lookup_btn.text().lower()

        ai = AskDocumentPanel()
        assert "preguntar" in ai.title_lbl.text().lower()
        assert hasattr(win, "_open_shortcut")
        assert hasattr(win, "_shortcuts_shortcut")
        app.processEvents()
    finally:
        win.close()


def test_v030_read_installer_language_from_fake_registry() -> None:
    class _FakeKey:
        def __init__(self, vals: dict[str, object]) -> None:
            self._vals = vals

        def __enter__(self) -> "_FakeKey":
            return self

        def __exit__(self, *args) -> None:
            return None

    class _FakeReg:
        HKEY_CURRENT_USER = 1

        def __init__(self, values: dict[str, object] | None = None) -> None:
            self._values = values or {}

        def OpenKey(self, root, subkey: str) -> _FakeKey:
            del root, subkey
            if not self._values:
                raise FileNotFoundError("Missing key")
            return _FakeKey(self._values)

        def QueryValueEx(self, key: _FakeKey, name: str):
            if name not in key._vals:
                raise FileNotFoundError(name)
            return key._vals[name], 1

    assert read_installer_language(reg_module=_FakeReg()) == "es"
    assert read_installer_language(reg_module=_FakeReg({"Language": "en"})) == "en"
    assert read_installer_language(reg_module=_FakeReg({"Language": "pt_br"})) == "pt"


def test_v030_wiktionary_wikitext_fallback_opensearch_and_cache(tmp_path: Path) -> None:
    engine = DictionaryEngine(base_dir=tmp_path / "wikt_dir")

    calls: list[str] = []
    wikitext_sample = (
        "== Espa\u00f1ol ==\n"
        "==== {{adjetivo|es}} ====\n"
        ";1: Que tiene [[derecho]] de [[propiedad]] sobre una cosa.\n"
        "{{sin\u00f3nimo|es|due\u00f1o|titular}}\n"
    )

    def _fake_fetcher(url: str) -> bytes:
        calls.append(url)
        if "action=opensearch" in url:
            return json.dumps(["propie", ["propietario", "propiedad"], ["", ""], ["", ""]]).encode("utf-8")
        if "action=query" in url and "propietario" in url:
            return json.dumps(
                {
                    "query": {
                        "pages": {
                            "123": {
                                "title": "propietario",
                                "revisions": [{"slots": {"main": {"*": wikitext_sample}}}],
                            }
                        }
                    }
                }
            ).encode("utf-8")
        if "en.wiktionary.org/api/rest_v1" in url and "fallbackterm" in url:
            return json.dumps(
                {
                    "en": [
                        {
                            "partOfSpeech": "noun",
                            "definitions": [{"definition": "An illustrative fallback definition."}],
                        }
                    ]
                }
            ).encode("utf-8")
        return json.dumps({"query": {"pages": {"-1": {"missing": ""}}}}).encode("utf-8")

    # 1. Spanish wikitext lookup (consent defaults to True, zero HTTP 501)
    entries = engine.lookup_online_wiktionary("propietario", lang="es", fetcher=_fake_fetcher)
    assert entries
    assert "propiedad" in entries[0].definition.lower()
    assert "due\u00f1o" in entries[0].synonyms

    # 2. Cached lookup uses SQLite without another HTTP hit
    prev_calls = len(calls)
    cached = engine.lookup("propietario", lang="es")
    assert cached
    assert "propiedad" in cached[0].definition.lower()
    assert len(calls) == prev_calls

    # 3. English REST fallback
    entries_en = engine.lookup_online_wiktionary("fallbackterm", lang="en", fetcher=_fake_fetcher)
    assert entries_en
    assert "fallback definition" in entries_en[0].definition.lower()

    # 4. OpenSearch suggestions
    sugg = engine.suggest_online_wiktionary("propie", lang="es", fetcher=_fake_fetcher)
    assert "propietario" in sugg

    # 5. Respects Solo offline
    try:
        engine.lookup_online_wiktionary("cualquier", lang="es", offline_only=True, fetcher=_fake_fetcher)
        raise AssertionError("Expected PermissionError when offline_only=True")
    except PermissionError:
        pass


def test_v030_pdf_search_ctrl_f_and_find_bar(tmp_path: Path) -> None:
    app = _qapp()
    pdf_path = _make_sample_pdf(
        tmp_path / "searchable.pdf",
        ["Alpha contrato inicial", "Segundo folio Contrato FINAL"],
    )
    sess = PdfDocumentSession(pdf_path)
    try:
        hits_ci = sess.search_text("contrato", case_sensitive=False)
        assert len(hits_ci) == 2
        assert hits_ci[0].page_index == 0
        assert hits_ci[1].page_index == 1

        hits_cs = sess.search_text("Contrato", case_sensitive=True)
        assert len(hits_cs) == 1
        assert hits_cs[0].page_index == 1
    finally:
        sess.close()

    win = MainWindow(ThemeProfile(), persist_profile=False)
    try:
        win.show()
        assert win.open_document_path(pdf_path, switch_to_viewer=True)
        app.processEvents()
        vw = win.viewer_view
        vw.open_find_bar()
        assert not vw.find_bar.isHidden()
        hits = vw.run_find("Contrato")
        assert len(hits) == 2
        assert vw.find_count_lbl.text() == "1 / 2"
        vw.find_next()
        assert vw.find_count_lbl.text() == "2 / 2"
        vw.find_prev()
        assert vw.find_count_lbl.text() == "1 / 2"
        vw.close_find_bar()
        assert not vw.find_bar.isVisible()
        assert vw.find_count_lbl.text() == "0 / 0"
        app.processEvents()
    finally:
        win.close()


def test_v030_print_dialog_page_ranges_and_executor(tmp_path: Path) -> None:
    app = _qapp()
    assert parse_page_range("1, 3-4", 5) == [0, 2, 3]
    assert parse_page_range("", 3) == [0, 1, 2]
    try:
        parse_page_range("99", 2)
        raise AssertionError("Expected ValueError for out-of-range page")
    except ValueError:
        pass

    pdf_path = _make_sample_pdf(tmp_path / "print_test.pdf", ["P1", "P2", "P3"])
    sess = PdfDocumentSession(pdf_path)
    captured: list[tuple[list[int], bool, int, bool]] = []
    try:
        dlg = PrintPdfDialog(
            sess,
            current_page=1,
            doc_name="print_test.pdf",
            print_executor=lambda _pr, pages, fit, dpi, auto_o: (
                captured.append((list(pages), fit, dpi, auto_o)) or len(pages)
            ),
        )
        assert dlg.printer_combo.count() >= 1
        assert dlg.paper_combo.count() >= 1
        custom_idx = dlg.range_mode_combo.findData("custom")
        dlg.range_mode_combo.setCurrentIndex(custom_idx)
        dlg.range_edit.setText("1, 3")
        dlg.copies_spin.setValue(2)
        dlg.print_btn.click()
        app.processEvents()
        assert len(captured) == 1
        assert captured[0][0] == [0, 2]
    finally:
        dlg.close()
        sess.close()


def test_v030_presentation_mode_clicker_hud_and_shortcuts_and_hub_motion(tmp_path: Path) -> None:
    app = _qapp()
    pdf_path = _make_sample_pdf(tmp_path / "slides.pdf", ["Slide One", "Slide Two", "Slide Three"])
    sess = PdfDocumentSession(pdf_path)
    closed_pages: list[int] = []
    pres = PresentationWindow()
    pres.closed_at_page.connect(closed_pages.append)
    try:
        pres.start_presentation(sess, start_page=0)
        assert pres.page_lbl.text() == "1 / 3"

        # Wireless clicker forward / backward keys
        QTest.keyClick(pres, Qt.Key.Key_PageDown)
        assert pres.page_lbl.text() == "2 / 3"
        QTest.keyClick(pres, Qt.Key.Key_Space)
        assert pres.page_lbl.text() == "3 / 3"
        QTest.keyClick(pres, Qt.Key.Key_PageUp)
        assert pres.page_lbl.text() == "2 / 3"

        # Blackout / Whiteout toggle
        QTest.keyClick(pres, Qt.Key.Key_B)
        assert pres._blank_mode == "black"
        QTest.keyClick(pres, Qt.Key.Key_B)
        assert pres._blank_mode is None
        QTest.keyClick(pres, Qt.Key.Key_W)
        assert pres._blank_mode == "white"

        # Hover near bottom reveals HUD
        ev = QMouseEvent(
            QMouseEvent.Type.MouseMove,
            QPointF(pres.width() / 2.0, pres.height() - 12.0),
            QPointF(pres.width() / 2.0, pres.height() - 12.0),
            Qt.MouseButton.NoButton,
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
        )
        pres.mouseMoveEvent(ev)
        assert pres.hud.isVisible()

        # Escape exits synced to slide 1
        QTest.keyClick(pres, Qt.Key.Key_Escape)
        assert closed_pages == [1]

        # ShortcutsDialog renders all groups cleanly
        sc_dlg = ShortcutsDialog(mode="dark")
        assert len(SHORTCUT_GROUPS) == 3
        sc_dlg.close()

        # Pointer-reactive backdrop is enabled ONLY on Hub, disabled on Viewer reading surface
        guard = MotionPerformanceGuard()
        hub_bg = MotionBackdropWidget(is_viewer_surface=False, guard=guard)
        vw_bg = MotionBackdropWidget(is_viewer_surface=True, guard=guard)
        assert hub_bg.pointer_interactive is True
        assert vw_bg.pointer_interactive is False
        hub_bg.set_pointer_position(QPoint(120, 60))
        vw_bg.set_pointer_position(QPoint(120, 60))
        assert hub_bg._pointer_pos is not None
        assert vw_bg._pointer_pos is None
        hub_bg.cleanup()
        vw_bg.cleanup()
        app.processEvents()
    finally:
        pres.close()
        sess.close()
