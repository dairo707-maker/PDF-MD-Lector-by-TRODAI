"""v0.4.2 (M11) regression tests: eliminate baked PDFium ghost sticky notes when hiding/moving
and enable pointer-reactive animated backdrop on TopBar + Sidebar while keeping Viewer quiet."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPoint, QPointF
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton, QTextEdit
from reportlab.pdfgen import canvas as rl_canvas

from pdf_lector.core.pdf_annotations import AnnotationRecord, load_pdf_annotations, save_pdf_annotations
from pdf_lector.core.pdf_engine import PdfDocumentSession
from pdf_lector.theme.motion_engine import MotionBackdropWidget
from pdf_lector.theme.palette import get_palette, register_embedded_fonts
from pdf_lector.theme.theme_profile import ThemeProfile
from pdf_lector.ui.main_window import MainWindow
from pdf_lector.ui.notes_panel import NotesPanel
from pdf_lector.ui.presentation_view import PresentationWindow


def _qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    assert isinstance(app, QApplication)
    register_embedded_fonts(app)
    return app


def test_m11_saved_sticky_note_has_no_baked_ghost_and_hides_completely_with_eye_toggle(tmp_path: Path) -> None:
    app = _qapp()
    pdf_path = tmp_path / "ghost_fix.pdf"
    c = rl_canvas.Canvas(str(pdf_path), pagesize=(612, 792))
    c.setFont("Helvetica", 12)
    c.drawString(72, 740, "Texto superior alejado de las notas adhesivas.")
    c.save()

    legacy_note = AnnotationRecord(
        id="note-legacy-24",
        page_index=0,
        kind="text",
        rect_pdf=(120.0, 320.0, 144.0, 344.0),
        points_pdf=(),
        color_hex="#FDE047",
        author="Anonimo",
        timestamp_iso="2026-11-26T22:00:00Z",
        content="Nota pequena antigua.",
        category="General",
    )
    visible_note = AnnotationRecord(
        id="note-callout-vis",
        page_index=0,
        kind="text",
        rect_pdf=(240.0, 420.0, 388.0, 494.0),
        points_pdf=(),
        color_hex="#FDE047",
        author="Anonimo",
        timestamp_iso="2026-11-26T22:05:00Z",
        content="Nota callout visible sin duplicarse.",
        category="Revisar",
        anchor_pdf=(90.0, 510.0),
    )
    save_pdf_annotations(pdf_path, [legacy_note, visible_note], create_backup=False)

    # External viewers still receive draw_annots=True rendering
    ext_sess = PdfDocumentSession(pdf_path)
    try:
        ext_img = ext_sess.render_page(0, scale=1.0, rotation=0, draw_annots=True)
        sx0, sy0, sx1, sy1 = ext_sess.pdf_to_screen_rect(0, visible_note.rect_pdf, scale=1.0, rotation=0)
        ext_colored = sum(
            1
            for x in range(int(sx0) + 4, int(sx1) - 4, 5)
            for y in range(int(sy0) + 2, int(sy1) - 2, 5)
            if ext_img.pixelColor(x, y).red() < 252
            or ext_img.pixelColor(x, y).green() < 252
            or ext_img.pixelColor(x, y).blue() < 252
        )
        assert ext_colored > 30
    finally:
        ext_sess.close()

    win = MainWindow(ThemeProfile(base_mode="light"), persist_profile=False)
    win.resize(1100, 760)
    win.show()
    try:
        assert win.open_document_path(pdf_path, switch_to_viewer=True)
        tab = win.viewer_view.current_tab()
        assert tab is not None
        canvas = tab._page_canvases[0]
        for _ in range(80):
            app.processEvents()
            if canvas._rendered_pixmap is not None:
                break
            QTest.qWait(25)
        assert canvas._rendered_pixmap is not None

        # Base page pixmap rendered inside Viewer must NOT bake saved annotations
        base_img = canvas._rendered_pixmap.toImage()
        for rec in (legacy_note, visible_note):
            sr = canvas._pdf_to_screen_rect(rec.rect_pdf)
            for x in range(int(sr.left()) + 2, int(sr.right()) - 2, 4):
                for y in range(int(sr.top()) + 2, int(sr.bottom()) - 2, 4):
                    px = base_img.pixelColor(min(base_img.width() - 1, x), min(base_img.height() - 1, y))
                    assert px.red() >= 252 and px.green() >= 252 and px.blue() >= 252

        # Hide notes via Eye toggle -> zero ghost boxes visible on canvas
        win.viewer_view.set_notes_visible(False)
        app.processEvents()
        hidden_grab = canvas.grab().toImage()
        for rec in (legacy_note, visible_note):
            sr = canvas._pdf_to_screen_rect(rec.rect_pdf)
            for x in range(int(sr.left()) + 2, int(sr.right()) - 2, 4):
                for y in range(int(sr.top()) + 2, int(sr.bottom()) - 2, 4):
                    px = hidden_grab.pixelColor(min(hidden_grab.width() - 1, x), min(hidden_grab.height() - 1, y))
                    assert px.red() >= 252 and px.green() >= 252 and px.blue() >= 252

        # Re-enable notes via Eye toggle -> notes reappear cleanly
        win.viewer_view.set_notes_visible(True)
        app.processEvents()
        vis_grab = canvas.grab().toImage()
        sr_vis = canvas._pdf_to_screen_rect(visible_note.rect_pdf)
        colored_vis = sum(
            1
            for x in range(int(sr_vis.left()) + 4, int(sr_vis.right()) - 4, 4)
            for y in range(int(sr_vis.top()) + 2, int(sr_vis.bottom()) - 2, 4)
            if vis_grab.pixelColor(x, y).blue() < 245
        )
        assert colored_vis > 25
    finally:
        win.close()


def test_m11_topbar_and_sidebar_have_pointer_reactive_motion_while_viewer_stays_quiet(tmp_path: Path) -> None:
    app = _qapp()
    win = MainWindow(
        ThemeProfile(base_mode="light", motion_level="modern", motion_preset="polygonal_mesh", opacity=0.75),
        persist_profile=False,
    )
    win.resize(1160, 740)
    win.show()
    app.processEvents()
    try:
        assert isinstance(win.sidebar, MotionBackdropWidget)
        assert isinstance(win.topbar, MotionBackdropWidget)
        assert win.sidebar.pointer_interactive is True
        assert win.topbar.pointer_interactive is True
        assert win.hub_view.hero_backdrop.pointer_interactive is True

        win.sidebar.set_pointer_position(QPointF(110.0, 320.0))
        win.topbar.set_pointer_position(QPointF(480.0, 24.0))
        app.processEvents()
        assert win.sidebar._pointer_pos == QPointF(110.0, 320.0)
        assert win.topbar._pointer_pos == QPointF(480.0, 24.0)

        base_col = get_palette("light")["bg_sidebar_hex"].lower()
        for surface in (win.sidebar, win.topbar):
            shot = surface.grab().toImage()
            non_flat = 0
            for x in range(8, shot.width() - 8, 18):
                for y in range(6, shot.height() - 6, 12):
                    if shot.pixelColor(x, y).name().lower() != base_col:
                        non_flat += 1
            assert non_flat > 4

        pdf_path = tmp_path / "viewer_quiet.pdf"
        c = rl_canvas.Canvas(str(pdf_path), pagesize=(612, 792))
        c.drawString(72, 700, "Pagina de lectura")
        c.save()
        assert win.open_document_path(pdf_path, switch_to_viewer=True)
        tab = win.viewer_view.current_tab()
        assert tab is not None
        assert tab.backdrop.pointer_interactive is False
    finally:
        win.close()


def test_m11_inline_sticky_note_editor_overlays_note_with_red_trash_button(tmp_path: Path) -> None:
    app = _qapp()
    pdf_path = tmp_path / "inline_note.pdf"
    c = rl_canvas.Canvas(str(pdf_path), pagesize=(612, 792))
    c.drawString(72, 700, "Linea para nota adhesiva inline.")
    c.save()

    win = MainWindow(ThemeProfile(base_mode="light"), persist_profile=False)
    win.resize(1100, 740)
    win.show()
    try:
        assert win.open_document_path(pdf_path, switch_to_viewer=True)
        tab = win.viewer_view.current_tab()
        assert tab is not None
        canvas = tab._page_canvases[0]
        rec = canvas.add_sticky_note((140.0, 520.0), "Texto inicial", category="Revisar")
        card = canvas.open_sticky_note_card(rec.id)
        assert card is not None
        app.processEvents()

        # Inline editor must sit directly OVER the note rect (not offset to the right)
        sr = canvas._pdf_to_screen_rect(rec.rect_pdf)
        assert abs(card.x() - int(round(sr.left()))) <= 6
        assert abs(card.y() - int(round(sr.top()))) <= 6

        del_btn = card.findChild(QPushButton, "StickyDelBtn")
        assert del_btn is not None
        assert "#DC2626" in del_btn.styleSheet()
        assert not del_btn.icon().isNull()

        editor = card.findChild(QTextEdit, "StickyNoteEditor")
        assert editor is not None
        editor.setPlainText("Editado inline")
        app.processEvents()
        assert rec.content == "Editado inline"

        # NotesPanel action buttons are also icon-only with red trash delete button
        panel = NotesPanel(mode="light", lang="es")
        try:
            panel.set_notes([rec])
            app.processEvents()
            np_del = panel.findChild(QPushButton, "NoteCardDelBtn")
            assert np_del is not None
            assert np_del.text() == ""
            assert not np_del.icon().isNull()
            assert "#DC2626" in np_del.styleSheet()
        finally:
            panel.close()
    finally:
        win.close()


def test_m11_all_ribbons_are_icon_only_with_tooltips(tmp_path: Path) -> None:
    app = _qapp()
    pdf_path = tmp_path / "ribbons.pdf"
    c = rl_canvas.Canvas(str(pdf_path), pagesize=(612, 792))
    c.drawString(72, 700, "Contenido para probar cintas de iconos.")
    c.save()

    md_path = tmp_path / "notes.md"
    md_path.write_text("# Titulo\n\nTexto **ejemplo**.\n", encoding="utf-8")

    win = MainWindow(ThemeProfile(base_mode="dark"), persist_profile=False)
    win.resize(1160, 740)
    win.show()
    try:
        assert win.open_document_path(pdf_path, switch_to_viewer=True)
        pdf_tab = win.viewer_view.current_tab()
        assert pdf_tab is not None
        canvas = pdf_tab._page_canvases[0]

        # Selection popover highlight button is icon-only
        canvas._show_kindle_popover("Contenido", QPoint(120, 120))
        assert canvas._popover is not None
        hl_btns = [b for b in canvas._popover.findChildren(QPushButton) if b.property("role") == "PrimaryAction"]
        assert hl_btns and hl_btns[0].text() == "" and not hl_btns[0].icon().isNull()
        assert bool(hl_btns[0].toolTip().strip())

        # PDF bookmark bar buttons are icon-only + red trash
        assert pdf_tab.bm_add_btn.text() == "" and not pdf_tab.bm_add_btn.icon().isNull()
        assert pdf_tab.bm_del_btn.text() == "" and not pdf_tab.bm_del_btn.icon().isNull()
        assert "#DC2626" in pdf_tab.bm_del_btn.styleSheet()

        # Markdown tab bookmark bar & action toolbar buttons are icon-only
        assert win.open_document_path(md_path, switch_to_viewer=True)
        md_tab = win.viewer_view.current_tab()
        assert md_tab is not None
        assert md_tab.bm_add_btn.text() == "" and not md_tab.bm_add_btn.icon().isNull()
        assert md_tab.bm_del_btn.text() == "" and not md_tab.bm_del_btn.icon().isNull()
        for key in ("list", "table", "link", "save", "save_as"):
            btn = md_tab._fmt_btns[key]
            assert btn.text() == ""
            assert not btn.icon().isNull()
            assert bool(btn.toolTip().strip())

        # Presentation HUD buttons are all icon-only with tooltips
        pres = PresentationWindow()
        try:
            for btn in (pres.prev_btn, pres.next_btn, pres.blank_btn, pres.exit_btn):
                assert btn.text() == ""
                assert not btn.icon().isNull()
                assert bool(btn.toolTip().strip())
        finally:
            pres.close()
    finally:
        win.close()


def test_m11_close_event_and_tab_close_detect_unsaved_changes(tmp_path: Path) -> None:
    app = _qapp()
    pdf_path = tmp_path / "unsaved_guard.pdf"
    c = rl_canvas.Canvas(str(pdf_path), pagesize=(612, 792))
    c.drawString(72, 700, "Documento con cambios pendientes.")
    c.save()

    win = MainWindow(ThemeProfile(base_mode="light"), persist_profile=False)
    win.resize(1100, 720)
    win.show()
    try:
        assert win.open_document_path(pdf_path, switch_to_viewer=True)
        tab = win.viewer_view.current_tab()
        assert tab is not None
        canvas = tab._page_canvases[0]
        canvas.add_sticky_note((120.0, 560.0), "Cambio no guardado")
        assert win.has_unsaved_changes() is True
        assert pdf_path.name in win.viewer_view.unsaved_document_names()

        # 1. Tab close prompt returning "cancel" keeps tab open
        seen_tab_names: list[str] = []
        win.viewer_view._unsaved_tab_close_prompt_fn = lambda name: (seen_tab_names.append(name) or "cancel")
        win.viewer_view._close_tab(0)
        assert pdf_path.name in seen_tab_names
        assert win.viewer_view.tabs.count() == 1

        # 2. MainWindow close prompt returning "cancel" aborts closing
        seen_win_names: list[list[str]] = []
        win._unsaved_close_prompt_fn = lambda names: (seen_win_names.append(names) or "cancel")
        win.close()
        app.processEvents()
        assert win.isVisible() is True
        assert seen_win_names and pdf_path.name in seen_win_names[0]
        assert win.has_unsaved_changes() is True

        # 3. MainWindow close prompt returning "save" saves PDF to disk and closes cleanly
        win._unsaved_close_prompt_fn = lambda _names: "save"
        win.close()
        app.processEvents()
        assert not win.has_unsaved_changes()
        persisted = load_pdf_annotations(pdf_path)
        assert any(r.content == "Cambio no guardado" for r in persisted)
    finally:
        win._unsaved_close_prompt_fn = None
        if win.isVisible():
            win.close()

