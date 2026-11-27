"""v0.4.4 (M13) regression & stress tests: Dark/Light theme switch speed, foreground activation, Fit menu, and multi-tab PDF RAM safety."""

from __future__ import annotations

from pathlib import Path
import time
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent, QThreadPool, Qt
from PySide6.QtWidgets import QApplication, QGridLayout
from reportlab.pdfgen import canvas as rl_canvas

from pdf_lector.core.pdf_engine import PdfDocumentSession
from pdf_lector.core.single_instance import SingleInstanceLock
from pdf_lector.core.win32_bridge import allow_set_foreground_window, bring_window_to_foreground
from pdf_lector.theme.motion_engine import MotionBackdropWidget
from pdf_lector.theme.palette import build_trodai_qss, register_embedded_fonts
from pdf_lector.theme.theme_profile import default_profile
from pdf_lector.ui.main_window import MainWindow


def _qapp() -> QApplication:
    app = QApplication.instance() or QApplication(["pytest-m13"])
    register_embedded_fonts(app)
    return app


def _make_multipage_pdf(path: Path, pages: int = 6) -> Path:
    c = rl_canvas.Canvas(str(path), pagesize=(612, 792))
    for i in range(pages):
        c.setFont("Helvetica-Bold", 14)
        c.drawString(72, 720, f"Documento PDF {path.name} - Pagina {i + 1}")
        c.setFont("Helvetica", 11)
        c.drawString(72, 690, f"Contenido de prueba pagina {i + 1} para rendimiento y vista.")
        c.rect(72, 520, 460, 120, stroke=1, fill=0)
        c.showPage()
    c.save()
    return path


def test_dark_light_switch_fast_and_no_global_motion_event_filter(tmp_path: Path) -> None:
    app = _qapp()
    for mode in ("dark", "light"):
        qss = build_trodai_qss(mode)
        star_block = qss.split("* {", 1)[1].split("}", 1)[0]
        assert "border-radius: 0px;" in star_block
        assert "font-family:" not in star_block
        assert "QPushButton#FitMenuBtn::menu-indicator" in qss

    assert "eventFilter" not in MotionBackdropWidget.__dict__

    # Clean up any un-deleted closed windows from prior test modules in this session
    for w in list(app.topLevelWidgets()):
        if not w.isVisible():
            w.deleteLater()
    app.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()

    prof = default_profile()
    app.setStyleSheet(build_trodai_qss(prof.base_mode))
    win = MainWindow(prof, persist_profile=False, sample_pdf_dir=tmp_path)
    try:
        win.ensure_sample_pdf_loaded()
        win.switch_view(1, immediate=True)
        win.resize(1180, 740)
        win.show()
        app.processEvents()

        t0 = time.perf_counter()
        win._toggle_dark_light()
        to_light_ms = (time.perf_counter() - t0) * 1000.0
        app.processEvents()
        assert win.current_profile.base_mode == "light"
        assert win.viewer_view._mode == "light"

        t1 = time.perf_counter()
        win._toggle_dark_light()
        to_dark_ms = (time.perf_counter() - t1) * 1000.0
        app.processEvents()
        assert win.current_profile.base_mode == "dark"
        assert win.viewer_view._mode == "dark"

        assert to_light_ms < 3200.0
        assert to_dark_ms < 3200.0
    finally:
        win.close()
        win.deleteLater()
        app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_explorer_foreground_activation_hooks(tmp_path: Path) -> None:
    app = _qapp()
    # Zero HWND and ASFW_ANY must never raise even in headless/offscreen runs
    res_asfw = allow_set_foreground_window()
    assert isinstance(res_asfw, bool)
    assert bring_window_to_foreground(0) is False

    # Secondary forwarding process grants foreground rights via allow_set_foreground_window
    lock = SingleInstanceLock(key=f"m13_fg_{tmp_path.name}")
    assert lock.acquire() is True
    forwarded: list[list[Path]] = []
    lock.start_server(lambda paths: forwarded.append(list(paths)))
    sample_pdf = _make_multipage_pdf(tmp_path / "open_from_explorer.pdf", pages=2)
    try:
        with patch("pdf_lector.core.win32_bridge.allow_set_foreground_window", return_value=True) as mock_asfw:
            sent = lock.send_to_running_instance([sample_pdf], timeout_ms=1500)
            assert sent is True
            assert mock_asfw.call_count >= 1
        deadline = time.monotonic() + 1.5
        while not forwarded and time.monotonic() < deadline:
            app.processEvents()
        assert len(forwarded) == 1
        assert forwarded[0][0].name == sample_pdf.name
    finally:
        lock.release()

    # MainWindow.bring_to_front restores minimized state cleanly
    win = MainWindow(default_profile(), persist_profile=False, sample_pdf_dir=tmp_path)
    try:
        win.showMinimized()
        app.processEvents()
        win.bring_to_front()
        app.processEvents()
        assert win.isVisible() is True
        assert not (win.windowState() & Qt.WindowState.WindowMinimized)
    finally:
        win.close()
        win.deleteLater()
        app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_fit_menu_all_modes_layout_and_auto_refit(tmp_path: Path) -> None:
    app = _qapp()
    pdf_path = _make_multipage_pdf(tmp_path / "fit_layout_6p.pdf", pages=6)

    win = MainWindow(default_profile(), persist_profile=False, sample_pdf_dir=tmp_path)
    try:
        assert win.open_document_path(pdf_path, switch_to_viewer=True) is True
        win.resize(1220, 760)
        win.show()
        app.processEvents()

        vw = win.viewer_view
        tab = vw.current_tab()
        assert tab is not None
        assert vw.fit_btn.objectName() == "FitMenuBtn"
        menu = vw.fit_btn.menu()
        assert menu is not None
        actions = [a for a in menu.actions() if not a.isSeparator()]
        assert len(actions) == 7

        # Actual size = 100%
        vw.act_actual_size.trigger()
        app.processEvents()
        assert abs(tab.zoom - 1.0) < 0.02
        assert tab.fit_mode() == "actual_size"
        assert vw.act_actual_size.isChecked() is True

        # Fit width >= Fit page for portrait LETTER page inside wide viewport
        vw.act_fit_width.trigger()
        app.processEvents()
        z_width_1220 = tab.zoom
        assert tab.fit_mode() == "fit_width"
        assert vw.act_fit_width.isChecked() is True

        vw.act_fit_page.trigger()
        app.processEvents()
        z_page = tab.zoom
        assert tab.fit_mode() == "fit_page"
        assert vw.act_fit_page.isChecked() is True
        assert z_page <= z_width_1220 + 0.02

        vw.act_fit_height.trigger()
        app.processEvents()
        assert tab.fit_mode() == "fit_height"
        assert vw.act_fit_height.isChecked() is True

        # Two-page spread layout uses 2 columns in QGridLayout
        vw.act_layout_two_page.trigger()
        app.processEvents()
        assert tab.page_layout_mode() == "two_page"
        assert isinstance(tab.page_host_layout, QGridLayout)
        pos0 = tab.page_host_layout.getItemPosition(0)
        pos1 = tab.page_host_layout.getItemPosition(1)
        assert pos0[:2] == (0, 0)
        assert pos1[:2] == (0, 1)

        # Single-page layout shows strictly 1 canvas
        vw.act_layout_single.trigger()
        app.processEvents()
        assert tab.page_layout_mode() == "single"
        visible_count = sum(1 for c in tab._page_canvases if not c.isHidden())
        assert visible_count == 1

        # Back to continuous + fit_width, then resize viewport to verify auto-refit
        vw.act_layout_continuous.trigger()
        vw.act_fit_width.trigger()
        app.processEvents()
        z_before_resize = tab.zoom

        win.resize(940, 700)
        app.processEvents()
        time.sleep(0.09)
        app.processEvents()
        assert tab.zoom < z_before_resize - 0.05

        # Manual zoom switches fit_mode back to 'custom' so auto-refit stops overriding
        vw._step_zoom(1)
        app.processEvents()
        assert tab.fit_mode() == "custom"
        z_manual = tab.zoom
        win.resize(1240, 760)
        app.processEvents()
        time.sleep(0.09)
        app.processEvents()
        assert abs(tab.zoom - z_manual) < 0.01
    finally:
        win.close()
        win.deleteLater()
        app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_multitab_pdf_memory_eviction_and_lazy_thumbnails(tmp_path: Path) -> None:
    app = _qapp()
    pdfs = [_make_multipage_pdf(tmp_path / f"book_{i}.pdf", pages=25) for i in range(4)]

    win = MainWindow(default_profile(), persist_profile=False, sample_pdf_dir=tmp_path)
    try:
        win.resize(1120, 740)
        win.show()
        for p in pdfs:
            assert win.open_document_path(p, switch_to_viewer=True) is True
        QThreadPool.globalInstance().waitForDone(5000)
        app.processEvents()

        vw = win.viewer_view
        assert vw.tabs.count() == 4
        tab3 = vw.tabs.widget(3)
        assert tab3 is vw.current_tab()

        # Page size cache populated without repeating PDFium handle lookups
        sz = tab3.session.get_page_size(0)
        assert 0 in tab3.session._page_sizes_cache
        assert tab3.session.get_page_size(0) == sz

        # Thumbnails must remain lazy (not rendered while thumb pane is closed)
        assert len(tab3._rendered_thumbs) == 0

        # Opening thumbs pane lazily renders only visible batch (NOT all 25 upfront at open)
        tab3.set_thumbs_open(True)
        app.processEvents()
        QThreadPool.globalInstance().waitForDone(5000)
        app.processEvents()
        assert 0 < len(tab3._rendered_thumbs) <= 25

        # Scroll deep into 25-page tab and ensure rendered canvas pixmaps stay bounded
        tab3.set_page(18)
        QThreadPool.globalInstance().waitForDone(5000)
        app.processEvents()
        rendered_in_tab3 = sum(1 for c in tab3._page_canvases if c.has_rendered_image())
        assert rendered_in_tab3 <= 8

        # Switch back to Tab 0 -> Background tabs 1, 2, 3 must release canvas pixmaps & trim cache
        vw.tabs.setCurrentIndex(0)
        QThreadPool.globalInstance().waitForDone(5000)
        app.processEvents()

        for bg_idx in (1, 2, 3):
            bg_tab = vw.tabs.widget(bg_idx)
            assert sum(1 for c in bg_tab._page_canvases if c.has_rendered_image()) == 0
            assert bg_tab.session.cache_count <= 4
            assert bg_tab.session.cache_bytes <= 12 * 1024 * 1024

        tab0 = vw.tabs.widget(0)
        assert sum(1 for c in tab0._page_canvases if c.has_rendered_image()) >= 1
    finally:
        win.close()
        win.deleteLater()
        app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
