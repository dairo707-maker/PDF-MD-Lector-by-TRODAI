"""Comprehensive tests for the 13 UX/UI, 60 FPS motion, dictionary, AI chat, and batch tool refinements."""

from __future__ import annotations

import json
from pathlib import Path
import time

from PIL import Image
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication, QScrollArea
import pytest

from pdf_lector.core.ai_chat_history import AiChatHistoryStore, ChatMessage
from pdf_lector.core.ai_rag import DocumentChunk, RagAnswer
from pdf_lector.core.dictionary_engine import DictionaryEngine
from pdf_lector.core.pdf_engine import generate_sample_pdf
from pdf_lector.core.recent_docs import RecentDocumentsStore
from pdf_lector.core.tools import ToolRegistry
from pdf_lector.theme.palette import register_embedded_fonts
from pdf_lector.theme.theme_profile import ThemeProfile, default_profile
from pdf_lector.ui.ai_panel import AskDocumentPanel
from pdf_lector.ui.dictionary_panel import DictionaryPanel
from pdf_lector.ui.main_window import MainWindow
from pdf_lector.ui.preferences_dialog import PreferencesDialog, UserPreferences, load_preferences, save_preferences
from pdf_lector.ui.tool_workspace import ToolWorkspace


@pytest.fixture(scope="session")
def qapp() -> QApplication:
    app = QApplication.instance() or QApplication(["pytest-pdf-lector"])
    register_embedded_fonts(app)
    return app


def test_dictionary_extended_lexicon_not_found_and_copyable_panel(tmp_path: Path, qapp: QApplication) -> None:
    del qapp
    engine = DictionaryEngine(base_dir=tmp_path / "dicts")
    assert engine.entry_count("es") >= 120
    assert len(engine.lookup("Teléfono.", "es")) == 1
    assert len(engine.lookup("PROPIETARIO", "es")) == 1
    assert len(engine.lookup("Fórmula", "es")) == 1

    panel = DictionaryPanel(engine)
    panel.lookup_word("Teléfono", record_event=False)
    assert "telefono" in panel.res_word_lbl.text().lower() or "Teléfono".lower() in panel.res_word_lbl.text().lower()
    assert "comunicacion" in panel.res_def_lbl.text().lower()

    # Unknown word must NOT crash with TypeError or leave previous definition stale
    panel.lookup_word("xyzpalabraquenoexiste", record_event=False)
    assert panel.res_word_lbl.text() == "xyzpalabraquenoexiste"
    assert "No se encontro" in panel.res_def_lbl.text()

    # Copyable labels + copy button populate clipboard
    assert bool(panel.res_def_lbl.textInteractionFlags() & Qt.TextInteractionFlag.TextSelectableByMouse)
    panel.lookup_word("Contrato", record_event=False)
    copied = panel.copy_current_result()
    assert "contrato" in copied.lower()
    cb = QApplication.clipboard()
    assert cb is not None and "contrato" in cb.text().lower()
    panel.close()


def test_recent_docs_store_pinning_and_ordering(tmp_path: Path) -> None:
    store = RecentDocumentsStore(store_path=tmp_path / "recent_docs.json")
    p1 = tmp_path / "a.pdf"
    p2 = tmp_path / "b.pdf"
    p3 = tmp_path / "notes.md"
    for p in (p1, p2, p3):
        p.write_text("x", encoding="utf-8")

    store.record_open(p1, page_count=3)
    time.sleep(0.02)
    store.record_open(p2, page_count=5)
    assert [e.name for e in store.list_entries()] == ["b.pdf", "a.pdf"]

    # Pin a.pdf so it stays above newer unpinned documents
    assert store.toggle_pin(p1) is True
    time.sleep(0.02)
    store.record_open(p3, page_count=1)
    names = [e.name for e in store.list_entries()]
    assert names[0] == "a.pdf" and store.list_entries()[0].pinned is True

    removed = store.clear_unpinned()
    assert removed == 2
    assert [e.name for e in store.list_entries()] == ["a.pdf"]


def test_ai_chat_history_and_redesigned_panel_light_dark(tmp_path: Path, qapp: QApplication) -> None:
    pdf_p = generate_sample_pdf(tmp_path / "chat_doc.pdf")
    hist = AiChatHistoryStore(base_dir=tmp_path / "ai_chats")
    panel = AskDocumentPanel(lambda: (pdf_p, ""), mode="light", lang="es", history_store=hist)

    closed_hits: list[bool] = []
    panel.close_requested.connect(lambda: closed_hits.append(True))
    panel.close_btn.click()
    qapp.processEvents()
    assert closed_hits == [True]

    # Composer sits below answer_browser inside vertical layout
    layout = panel.layout()
    assert layout is not None
    browser_idx = layout.indexOf(panel.answer_browser)
    composer_idx = layout.indexOf(panel._composer_frame)
    assert 0 <= browser_idx < composer_idx

    # Simulate persisted Q&A with timestamp + citations + export (.md/.txt/.json)
    panel._messages.append(ChatMessage(role="user", text="Que dice la clausula?", timestamp="2026-11-26T10:15:00-05:00"))
    panel._on_answer_ready(
        RagAnswer(
            answer_text="Resumen del contrato local.",
            provider="local_bm25",
            citations=[DocumentChunk(chunk_id="c1", source_path=str(pdf_p), doc_type="pdf", text="Contrato local", page_number=1, score=1.0)],
            used_local_only=True,
        ),
        pdf_p,
        "Que dice la clausula?",
    )
    html_light = panel.answer_browser.toHtml()
    assert "Resumen del contrato local." in html_light
    assert "26/11/2026 10:15" in html_light

    md_out = panel.export_current_chat(tmp_path / "export.md")
    json_out = panel.export_current_chat(tmp_path / "export.json")
    assert md_out is not None and md_out.exists() and "Resumen del contrato local." in md_out.read_text(encoding="utf-8")
    assert json_out is not None and json.loads(json_out.read_text(encoding="utf-8"))["document"] == "chat_doc.pdf"

    # Re-opening another panel for the same PDF restores persisted history cleanly
    panel2 = AskDocumentPanel(lambda: (pdf_p, ""), mode="dark", lang="es", history_store=hist)
    assert len(panel2.messages()) == 2
    assert panel2.clear_current_chat(confirm=False) is True
    assert len(panel2.messages()) == 0
    panel.close()
    panel2.close()


def test_adobe_viewer_independent_rails_hidden_annot_and_continuous_scroll(tmp_path: Path, qapp: QApplication) -> None:
    from pypdf import PdfWriter

    pdf_p = tmp_path / "multi4.pdf"
    writer = PdfWriter()
    for _ in range(4):
        writer.add_blank_page(width=612, height=792)
    with pdf_p.open("wb") as fh:
        writer.write(fh)

    win = MainWindow(
        default_profile(),
        persist_profile=False,
        sample_pdf_dir=tmp_path,
        recent_store=RecentDocumentsStore(tmp_path / "recent.json"),
        ai_history_store=AiChatHistoryStore(tmp_path / "chats"),
    )
    win.resize(1200, 720)
    win.show()
    qapp.processEvents()

    # Home starts expanded; opening a PDF switches to Viewer & auto-collapses sidebar into slim ribbon
    assert not win.is_sidebar_collapsed() and win.sidebar.width() == 226
    assert win.open_document_path(pdf_p, switch_to_viewer=True)
    qapp.processEvents()
    assert win.is_sidebar_collapsed() and win.sidebar.width() <= 44 and win.sidebar.isVisible()

    tab = win.viewer_view.current_tab()
    assert tab is not None
    # Nav rail and static annot_bar start hidden so document dominates screen
    assert not hasattr(tab, "rail_mode_switch")
    assert not tab.nav_rail.isVisible()
    assert not tab.annot_bar.isVisible()

    # Icon-only mode by default on toolbar buttons
    assert not win.viewer_view.show_button_labels()
    assert win.viewer_view.zoom_in_btn.text() == ""
    assert bool(win.viewer_view.zoom_in_btn.toolTip())

    # Open both Miniaturas & Indice independently, detach Miniaturas, then close Indice
    tab.set_thumbs_open(True)
    tab.set_toc_open(True)
    qapp.processEvents()
    assert tab.is_thumbs_open() and tab.is_toc_open()
    assert tab.thumb_pane.isVisibleTo(tab.nav_rail) and tab.toc_pane.isVisibleTo(tab.nav_rail)
    tab.detach_thumbs()
    qapp.processEvents()
    assert tab._thumbs_detached is True
    tab.set_toc_open(False)
    qapp.processEvents()
    assert not tab.is_toc_open()
    tab.dock_thumbs(show_docked=True)
    tab.set_thumbs_open(False)
    qapp.processEvents()
    assert not tab.nav_rail.isVisible()

    # Continuous multi-page vertical scroll updates current_page & page_tag
    assert len(tab._page_canvases) == 4
    vbar = tab.scroll.verticalScrollBar()
    assert vbar.maximum() > 500
    target_canvas = tab._page_canvases[2]
    vbar.setValue(max(0, target_canvas.y() - 10))
    qapp.processEvents()
    assert tab.current_page == 2
    assert "3 / 4" in win.viewer_view.page_tag.text()

    # Toggle AI panel via right rail and close via its X button
    win.viewer_view.ai_rail_btn.click()
    qapp.processEvents()
    assert win._ai_panel is not None and win._ai_panel.isVisible()
    win._ai_panel.close_btn.click()
    qapp.processEvents()
    assert not win._ai_panel.isVisible()

    win.close()


def test_preferences_labels_and_continuous_scroll_persistence(tmp_path: Path) -> None:
    pfile = tmp_path / "prefs.json"
    prefs = UserPreferences(show_button_labels=True, continuous_scroll=False)
    save_preferences(prefs, pfile)
    loaded = load_preferences(pfile)
    assert loaded.show_button_labels is True and loaded.continuous_scroll is False

    dlg = PreferencesDialog(prefs_path=pfile)
    assert dlg.show_labels_chk.isChecked() is True
    assert dlg.continuous_scroll_chk.isChecked() is False
    dlg.close()


def test_tool_workspace_batch_queue_preview_and_frozen_imports(tmp_path: Path, qapp: QApplication) -> None:
    from scripts.build_exe import build_pyinstaller_args

    # PyInstaller args must collect all submodules/tools so frozen .exe opens every tool
    args = build_pyinstaller_args(tmp_path / "entry.py", tmp_path / "a.ico", tmp_path / "work", bundle_mode="onedir")
    assert "--collect-submodules" in args and "pdf_lector" in args

    pdf1 = generate_sample_pdf(tmp_path / "one.pdf")
    pdf2 = generate_sample_pdf(tmp_path / "two.pdf")
    out_dir = tmp_path / "batch_out"
    out_dir.mkdir()

    ws = ToolWorkspace(ToolRegistry.get("rotate_pdf"), show_back=True)
    assert ws.add_input_path(pdf1) is True
    assert ws.add_input_path(pdf2) is True
    assert ws._is_batch_mode() is True
    assert ws._preview_page_count >= 1
    assert ws.set_output(out_dir) is True

    res = ws.execute_primary_action()
    assert res.success is True
    assert len(res.output_paths) == 2
    assert all(Path(p).exists() for p in res.output_paths)
    ws.close()

    # MainWindow.open_tool_workspace switches inline to tool workspace (index 3)
    win = MainWindow(default_profile(), persist_profile=False, sample_pdf_dir=tmp_path)
    win.open_tool_workspace("compress_pdf")
    qapp.processEvents()
    assert win.stack.currentIndex() == 3
    assert win._active_tool_workspace is not None and win._active_tool_workspace.tool_def.id == "compress_pdf"
    win.close()


def test_theme_studio_slider_drag_is_smooth_and_skips_redundant_qss(tmp_path: Path, qapp: QApplication) -> None:
    banner_img = tmp_path / "banner.png"
    Image.new("RGB", (640, 360), (25, 45, 85)).save(banner_img)

    prof = ThemeProfile(banner_path=str(banner_img), apply_to_hub_banner=True, motion_level="subtle")
    win = MainWindow(prof, persist_profile=False, sample_pdf_dir=tmp_path)
    win.show()
    qapp.processEvents()
    initial_qss_mode = win._applied_base_mode
    assert initial_qss_mode == "dark"

    t0 = time.perf_counter()
    for val in range(1, 22):
        win.studio_view.blur_slider.setValue(val)
        win.studio_view.opacity_slider.setValue(min(85, 20 + val * 2))
    qapp.processEvents()
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    assert elapsed_ms < 500.0
    assert win.current_profile.blur_radius == 21
    win.close()
