"""v0.4.0 feature & UX tests: dark-mode note contrast, scroll-locked highlights, freehand pointer highlighting,
expanded zoom up to 1200%, visible resizable sticky notes with guide line, hide-notes toggle & attachment sharing."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QImage, QMouseEvent
from PySide6.QtWidgets import QApplication
from reportlab.pdfgen import canvas as rl_canvas

from pdf_lector.core.autosave import deserialize_annotation, serialize_annotation
from pdf_lector.core.pdf_annotations import AnnotationRecord, load_pdf_annotations, save_pdf_annotations
from pdf_lector.core.pdf_engine import PdfDocumentSession
from pdf_lector.core.share_service import ShareResult, copy_file_to_clipboard, share_to_channel
from pdf_lector.theme.palette import build_trodai_qss
from pdf_lector.theme.theme_profile import ThemeProfile
from pdf_lector.ui.main_window import MainWindow
from pdf_lector.ui.pdf_canvas import PdfCanvas
from pdf_lector.ui.share_dialog import ShareDocumentDialog


def _qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    assert isinstance(app, QApplication)
    return app


def _make_pdf(path: Path) -> Path:
    c = rl_canvas.Canvas(str(path), pagesize=(612, 792))
    c.setFont("Helvetica", 12)
    c.drawString(72, 710, "Texto de contrato sobre captura de datos y privacidad.")
    c.drawString(72, 680, "Segunda linea de prueba para zoom alto y resaltado.")
    c.save()
    return path


def test_v040_dark_mode_note_card_contrast_and_qss() -> None:
    app = _qapp()
    for mode in ("dark", "light"):
        qss = build_trodai_qss(mode)
        assert "QTextEdit {" in qss

    canvas = PdfCanvas()
    canvas.apply_theme("dark")
    try:
        rec = canvas.add_sticky_note((100.0, 500.0), "Hola, como estas", category="General")
        card = canvas.open_sticky_note_card(rec.id)
        assert card is not None
        assert "#0F172A" in card.styleSheet()
        # Locate QTextEdit directly by objectName
        ed_widgets = [w for w in card.findChildren(object) if getattr(w, "objectName", lambda: "")() == "StickyNoteEditor"]
        assert len(ed_widgets) == 1
        ed = ed_widgets[0]
        assert "#F8FAFC" in ed.styleSheet()
        assert "#111C2E" in ed.styleSheet()
        assert ed.toPlainText() == "Hola, como estas"
        app.processEvents()
    finally:
        canvas.close()


def test_v040_scroll_locked_highlights_and_high_zoom_1200(tmp_path: Path) -> None:
    app = _qapp()
    pdf_path = _make_pdf(tmp_path / "zoom_hl.pdf")
    sess = PdfDocumentSession(pdf_path)
    try:
        # High zoom render up to 12.0 caps bitmap dimensions safely <= 4096px
        img_hi = sess.render_page(0, scale=10.0, rotation=0)
        assert not img_hi.isNull()
        assert max(img_hi.width(), img_hi.height()) <= 4096

        canvas = PdfCanvas()
        canvas.set_session(sess, 0)
        canvas.set_rotation_and_zoom(0, 6.0)
        expected_w, expected_h = canvas.width(), canvas.height()
        assert expected_w == int(round(612 * 6.0))

        # Applying a lower-res or capped QImage must NOT alter locked widget dimensions!
        canvas.set_rendered_image(img_hi)
        assert canvas.width() == expected_w
        assert canvas.height() == expected_h

        # Adding capped zoom step up to 1200% in MainWindow / ViewerView
        win = MainWindow(ThemeProfile(base_mode="dark"), persist_profile=False)
        try:
            assert win.open_document_path(pdf_path, switch_to_viewer=True)
            tab = win.viewer_view.current_tab()
            assert tab is not None
            tab.set_zoom(8.5)
            assert tab.zoom == 8.5
            tab.set_zoom(15.0)
            assert tab.zoom == 12.0
            win.viewer_view._sync_toolbar_state()
            assert win.viewer_view.zoom_tag.text() == "1200%"
            app.processEvents()
        finally:
            win.close()
        canvas.close()
    finally:
        sess.close()


def test_v040_freehand_pointer_highlight_and_pdf_roundtrip(tmp_path: Path) -> None:
    app = _qapp()
    pdf_path = _make_pdf(tmp_path / "free_hl.pdf")
    canvas = PdfCanvas()
    try:
        canvas.set_tool_mode("free_highlight")
        assert canvas.tool_mode() == "free_highlight"
        canvas.set_highlight_color("#FDE047")

        # Mouse drag draws & commits translucent freehand highlight stroke
        p1 = canvas._pdf_to_screen_point(80.0, 650.0)
        p2 = canvas._pdf_to_screen_point(220.0, 652.0)
        canvas.mousePressEvent(
            QMouseEvent(
                QMouseEvent.Type.MouseButtonPress,
                QPointF(p1),
                QPointF(p1),
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )
        )
        canvas.mouseMoveEvent(
            QMouseEvent(
                QMouseEvent.Type.MouseMove,
                QPointF(p2),
                QPointF(p2),
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )
        )
        canvas.mouseReleaseEvent(
            QMouseEvent(
                QMouseEvent.Type.MouseButtonRelease,
                QPointF(p2),
                QPointF(p2),
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )
        )
        annots = canvas.annotations()
        assert len(annots) == 1
        rec = annots[0]
        assert rec.kind == "ink"
        assert 0.2 <= rec.opacity <= 0.7

        save_pdf_annotations(pdf_path, annots, create_backup=False)
        reloaded = load_pdf_annotations(pdf_path)
        assert len(reloaded) == 1
        assert reloaded[0].kind == "ink"
        assert abs(reloaded[0].opacity - rec.opacity) < 0.05
        app.processEvents()
    finally:
        canvas.close()


def test_v040_visible_resizable_notes_anchor_guide_and_hide_toggle(tmp_path: Path) -> None:
    app = _qapp()
    pdf_path = _make_pdf(tmp_path / "notes_anchor.pdf")
    canvas = PdfCanvas()
    try:
        rec = canvas.add_sticky_note(
            (220.0, 600.0),
            "Revisar alcance y fechas.",
            category="Revisar",
            anchor_pdf=(95.0, 705.0),
        )
        assert rec.anchor_pdf == (95.0, 705.0)
        assert (rec.rect_pdf[2] - rec.rect_pdf[0]) >= 140.0
        assert (rec.rect_pdf[3] - rec.rect_pdf[1]) >= 70.0

        # Persist /TRODAIAnchor to PDF and autosave JSON
        save_pdf_annotations(pdf_path, [rec], create_backup=False)
        loaded = load_pdf_annotations(pdf_path)
        assert loaded and loaded[0].anchor_pdf == (95.0, 705.0)

        ser = serialize_annotation(rec)
        restored = deserialize_annotation(ser)
        assert restored.anchor_pdf == (95.0, 705.0)

        # Corner resize handle hit-test + resize preserves anchor_pdf
        sr = canvas._pdf_to_screen_rect(rec.rect_pdf)
        handles = canvas._note_handle_points(sr)
        hit = canvas._note_resize_handle_at(handles["se"])
        assert hit == (rec.id, "se")

        resized = canvas.resize_sticky_note(rec.id, (220.0, 560.0, 460.0, 720.0))
        assert resized is not None
        assert abs((resized.rect_pdf[2] - resized.rect_pdf[0]) - 240.0) < 1e-3
        assert resized.anchor_pdf == (95.0, 705.0)

        # Hide / show notes toggle suppresses hit testing and sticky cards
        canvas.open_sticky_note_card(rec.id)
        assert canvas._sticky_card is not None
        canvas.set_notes_visible(False)
        assert canvas.notes_visible() is False
        assert canvas._sticky_card is None
        assert canvas._note_resize_handle_at(handles["se"]) is None
        assert canvas._annotation_at(260.0, 630.0) is None
        canvas.set_notes_visible(True)
        assert canvas.notes_visible() is True
        assert canvas._annotation_at(260.0, 630.0) is not None
        app.processEvents()
    finally:
        canvas.close()


def test_v040_share_service_clipboard_channels_and_dialog(tmp_path: Path) -> None:
    app = _qapp()
    pdf_path = _make_pdf(tmp_path / "share_me.pdf")

    # Reject invalid / traversal paths
    bad_res = share_to_channel(tmp_path / "does_not_exist.pdf", "whatsapp")
    assert bad_res.ok is False

    # Clipboard file drop
    cb_res = copy_file_to_clipboard(pdf_path)
    assert cb_res.ok is True
    cb = QApplication.clipboard()
    assert cb is not None and cb.mimeData().hasUrls()
    assert Path(cb.mimeData().urls()[0].toLocalFile()).resolve() == pdf_path.resolve()

    # Channels via stubbed hooks (zero external process/browser spawns)
    launched_uris: list[str] = []
    wa_res = share_to_channel(
        pdf_path,
        "whatsapp",
        clipboard_fn=copy_file_to_clipboard,
        launcher_fn=lambda uri: (launched_uris.append(uri) or True),
    )
    assert wa_res.ok is True
    assert any("whatsapp://" in u for u in launched_uris)

    mapi_res = share_to_channel(
        pdf_path,
        "email_mapi",
        mapi_fn=lambda p: ShareResult(ok=True, channel="email_mapi", message=f"MAPI:{p.name}"),
    )
    assert mapi_res.ok is True and "share_me.pdf" in mapi_res.message

    # ShareDocumentDialog renders & dispatches channels cleanly
    calls: list[str] = []
    dlg = ShareDocumentDialog(
        pdf_path,
        mode="dark",
        share_fn=lambda _p, ch: (calls.append(ch) or ShareResult(ok=True, channel=ch, message=f"OK:{ch}")),
    )
    try:
        dlg.show()
        dlg._channel_buttons["whatsapp"].click()
        dlg._channel_buttons["email_mapi"].click()
        assert calls == ["whatsapp", "email_mapi"]
        assert dlg.status_lbl.isVisible()
        assert "OK:email_mapi" in dlg.status_lbl.text()
        app.processEvents()
    finally:
        dlg.close()
        if QApplication.clipboard() is not None:
            QApplication.clipboard().clear()
