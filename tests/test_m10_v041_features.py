"""v0.4.1 (M10) regression tests: ISO 32000 /FreeTextCallout external compatibility,
PDF hyperlinks, IBeam text cursor, zoom anchor preservation, icon-only annot ribbon,
and 1-click updater with rendered Markdown release notes."""

from __future__ import annotations

import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import threading

import pikepdf
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication, QTextBrowser
from reportlab.pdfgen import canvas as rl_canvas

from pdf_lector.core.pdf_annotations import AnnotationRecord, load_pdf_annotations, save_pdf_annotations
from pdf_lector.core.pdf_engine import PdfDocumentSession, is_safe_pdf_uri
from pdf_lector.core.updater import UpdaterState, save_updater_state
from pdf_lector.theme.palette import register_embedded_fonts
from pdf_lector.theme.theme_profile import ThemeProfile
from pdf_lector.ui.about_updater_dialog import AboutUpdaterDialog
from pdf_lector.ui.main_window import MainWindow
from pdf_lector.ui.pdf_canvas import PdfCanvas


def _qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    assert isinstance(app, QApplication)
    register_embedded_fonts(app)
    return app


def _make_multipage_links_pdf(path: Path) -> Path:
    c = rl_canvas.Canvas(str(path), pagesize=(612, 792))
    # Page 1: text + external link + internal link to page 2
    c.setFont("Helvetica", 12)
    c.drawString(72, 700, "Documento principal para probar cursor IBeam y enlaces.")
    c.drawString(72, 660, "Abrir documentacion externa")
    c.linkURL("https://example.org/docs", (70, 654, 240, 676), relative=0)
    c.drawString(72, 620, "Ir a segunda pagina")
    c.bookmarkPage("page2_dest")
    c.linkRect("GoPage2", "page2_dest", (70, 614, 210, 636), relative=0)
    # Unsafe script link on page 1 (must be filtered by get_page_links)
    c.linkURL("javascript:alert(1)", (70, 560, 190, 580), relative=0)
    c.showPage()

    # Page 2
    c.setFont("Helvetica", 12)
    c.drawString(72, 700, "Pagina 2 destino del hipervinculo interno.")
    c.drawString(72, 420, "Zona media de lectura en pagina 2 para probar zoom.")
    c.bookmarkPage("page2_dest")
    c.showPage()

    # Page 3
    c.setFont("Helvetica", 12)
    c.drawString(72, 700, "Pagina 3 adicional.")
    c.showPage()
    c.save()
    return path


def test_m10_visible_sticky_note_iso_freetext_callout_and_external_render(tmp_path: Path) -> None:
    pdf_path = tmp_path / "callout.pdf"
    c = rl_canvas.Canvas(str(pdf_path), pagesize=(612, 792))
    c.setFont("Helvetica", 12)
    c.drawString(72, 710, "Clausula 5: revision contractual.")
    c.save()

    rec = AnnotationRecord(
        id="note-callout-1",
        page_index=0,
        kind="text",
        rect_pdf=(260.0, 610.0, 408.0, 684.0),
        points_pdf=(),
        color_hex="#FDE047",
        author="Anonimo",
        timestamp_iso="2026-11-26T20:00:00Z",
        content="Revisar total neto antes de aprobar.",
        category="Revisar",
        anchor_pdf=(95.0, 705.0),
    )
    save_pdf_annotations(pdf_path, [rec], create_backup=False)

    # 1. Inspect native ISO 32000 dictionaries via pikepdf
    with pikepdf.open(pdf_path) as pdf:
        annot = pdf.pages[0]["/Annots"][0]
        assert str(annot["/Subtype"]) == "/FreeText"
        assert str(annot["/IT"]) == "/FreeTextCallout"
        assert "/CL" in annot and len(annot["/CL"]) == 6
        assert "/RD" in annot and len(annot["/RD"]) == 4
        assert "/AP" in annot and "/N" in annot["/AP"]

    # 2. Render pure PDFium page WITHOUT PdfCanvas overlay -> note box & leader are visibly drawn
    sess = PdfDocumentSession(pdf_path)
    try:
        img = sess.render_page(0, scale=1.0, rotation=0)
        sx0, sy0, sx1, sy1 = sess.pdf_to_screen_rect(0, rec.rect_pdf, scale=1.0, rotation=0)
        non_white = 0
        for px in range(int(sx0) + 4, int(sx1) - 4, 4):
            for py in range(int(sy0) + 2, int(sy1) - 2, 4):
                col = img.pixelColor(px, py)
                if col.red() < 252 or col.green() < 252 or col.blue() < 252:
                    non_white += 1
        assert non_white > 40
    finally:
        sess.close()

    # 3. Roundtrip back via load_pdf_annotations preserves kind="text", rect_pdf, anchor_pdf
    loaded = load_pdf_annotations(pdf_path)
    assert len(loaded) == 1
    out = loaded[0]
    assert out.kind == "text"
    assert out.category == "Revisar"
    assert out.content == "Revisar total neto antes de aprobar."
    assert out.anchor_pdf is not None and abs(out.anchor_pdf[0] - 95.0) < 1e-2 and abs(out.anchor_pdf[1] - 705.0) < 1e-2
    assert abs((out.rect_pdf[2] - out.rect_pdf[0]) - 148.0) < 1e-2

    # 4. External Acrobat-style /FreeTextCallout (without /TRODAI* keys) loads cleanly as sticky note
    ext_pdf = tmp_path / "acrobat_callout.pdf"
    c2 = rl_canvas.Canvas(str(ext_pdf), pagesize=(612, 792))
    c2.drawString(72, 700, "External PDF")
    c2.save()
    with pikepdf.open(ext_pdf, allow_overwriting_input=True) as pdf2:
        ext_annot = pdf2.make_indirect(
            pikepdf.Dictionary(
                {
                    "/Type": pikepdf.Name("/Annot"),
                    "/Subtype": pikepdf.Name("/FreeText"),
                    "/IT": pikepdf.Name("/FreeTextCallout"),
                    "/Rect": pikepdf.Array([100.0, 580.0, 420.0, 710.0]),
                    "/RD": pikepdf.Array([160.0, 0.0, 0.0, 50.0]),
                    "/CL": pikepdf.Array([110.0, 700.0, 240.0, 620.0, 260.0, 620.0]),
                    "/Contents": "Nota externa Acrobat",
                    "/Subj": "Importante",
                    "/C": pikepdf.Array([0.5, 0.9, 0.6]),
                }
            )
        )
        pdf2.pages[0]["/Annots"] = pikepdf.Array([ext_annot])
        pdf2.save(ext_pdf)
    ext_loaded = load_pdf_annotations(ext_pdf)
    assert len(ext_loaded) == 1
    assert ext_loaded[0].kind == "text"
    assert ext_loaded[0].content == "Nota externa Acrobat"
    assert ext_loaded[0].anchor_pdf == (110.0, 700.0)
    assert ext_loaded[0].rect_pdf == (260.0, 580.0, 420.0, 660.0)


def test_m10_ibeam_cursor_near_text_and_pdf_hyperlinks(tmp_path: Path) -> None:
    app = _qapp()
    pdf_path = _make_multipage_links_pdf(tmp_path / "links.pdf")
    assert is_safe_pdf_uri("https://example.org/docs")
    assert is_safe_pdf_uri("mailto:team@example.org")
    assert not is_safe_pdf_uri("javascript:alert(1)")
    assert not is_safe_pdf_uri("file:///C:/Windows/win.ini")

    win = MainWindow(ThemeProfile(base_mode="dark"), persist_profile=False)
    try:
        assert win.open_document_path(pdf_path, switch_to_viewer=True)
        tab = win.viewer_view.current_tab()
        assert tab is not None
        sess = tab.session
        links = sess.get_page_links(0)
        uris = [lnk.uri for lnk in links if lnk.uri]
        targets = [lnk.target_page for lnk in links if lnk.target_page is not None]
        assert "https://example.org/docs" in uris
        assert all("javascript" not in u.lower() for u in uris)
        assert 1 in targets

        canvas = tab._page_canvases[0]
        assert canvas.tool_mode() == "select"

        # Hover over normal text -> IBeamCursor
        text_pt = canvas._pdf_to_screen_point(100.0, 703.0)
        canvas.mouseMoveEvent(
            QMouseEvent(
                QMouseEvent.Type.MouseMove,
                QPointF(text_pt),
                QPointF(text_pt),
                Qt.MouseButton.NoButton,
                Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier,
            )
        )
        assert canvas.cursor().shape() == Qt.CursorShape.IBeamCursor

        # Hover over empty blank margin -> ArrowCursor
        blank_pt = canvas._pdf_to_screen_point(560.0, 120.0)
        canvas.mouseMoveEvent(
            QMouseEvent(
                QMouseEvent.Type.MouseMove,
                QPointF(blank_pt),
                QPointF(blank_pt),
                Qt.MouseButton.NoButton,
                Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier,
            )
        )
        assert canvas.cursor().shape() == Qt.CursorShape.ArrowCursor

        # Hover over external URI link -> PointingHandCursor + URI tooltip
        ext_pt = canvas._pdf_to_screen_point(130.0, 665.0)
        canvas.mouseMoveEvent(
            QMouseEvent(
                QMouseEvent.Type.MouseMove,
                QPointF(ext_pt),
                QPointF(ext_pt),
                Qt.MouseButton.NoButton,
                Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier,
            )
        )
        assert canvas.cursor().shape() == Qt.CursorShape.PointingHandCursor
        assert "example.org/docs" in canvas.toolTip()

        clicked_uris: list[str] = []
        canvas.link_uri_requested.connect(clicked_uris.append)
        for ev_type, btn, btns in (
            (QMouseEvent.Type.MouseButtonPress, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton),
            (QMouseEvent.Type.MouseButtonRelease, Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton),
        ):
            canvas.mousePressEvent(QMouseEvent(ev_type, QPointF(ext_pt), QPointF(ext_pt), btn, btns, Qt.KeyboardModifier.NoModifier)) if ev_type == QMouseEvent.Type.MouseButtonPress else canvas.mouseReleaseEvent(QMouseEvent(ev_type, QPointF(ext_pt), QPointF(ext_pt), btn, btns, Qt.KeyboardModifier.NoModifier))
        assert clicked_uris == ["https://example.org/docs"]

        # Clean click on internal GoTo link jumps tab to page 2 (index 1)
        assert tab.current_page == 0
        goto_pt = canvas._pdf_to_screen_point(120.0, 625.0)
        canvas.mousePressEvent(
            QMouseEvent(
                QMouseEvent.Type.MouseButtonPress,
                QPointF(goto_pt),
                QPointF(goto_pt),
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )
        )
        canvas.mouseReleaseEvent(
            QMouseEvent(
                QMouseEvent.Type.MouseButtonRelease,
                QPointF(goto_pt),
                QPointF(goto_pt),
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier,
            )
        )
        app.processEvents()
        assert tab.current_page == 1
    finally:
        win.close()


def test_m10_zoom_preserves_reading_position(tmp_path: Path) -> None:
    app = _qapp()
    pdf_path = _make_multipage_links_pdf(tmp_path / "zoom_pos.pdf")
    win = MainWindow(ThemeProfile(base_mode="dark"), persist_profile=False)
    win.resize(980, 660)
    win.show()
    try:
        assert win.open_document_path(pdf_path, switch_to_viewer=True)
        tab = win.viewer_view.current_tab()
        assert tab is not None
        app.processEvents()

        vbar = tab.scroll.verticalScrollBar()
        vp_h = max(1, tab.scroll.viewport().height())
        p2_canvas = tab._page_canvases[1]
        desired_center = p2_canvas.y() + int(round(p2_canvas.height() * 0.55))
        vbar.setValue(max(0, desired_center - vp_h // 2))
        app.processEvents()

        before_rel_y = ((vbar.value() + vp_h * 0.5) - p2_canvas.y()) / max(1.0, float(p2_canvas.height()))
        assert tab.current_page == 1

        for z in (2.2, 3.6, 1.15):
            tab.set_zoom(z)
            app.processEvents()
            cur_vp_h = max(1, tab.scroll.viewport().height())
            after_rel_y = ((vbar.value() + cur_vp_h * 0.5) - p2_canvas.y()) / max(1.0, float(p2_canvas.height()))
            assert tab.current_page == 1
            assert abs(after_rel_y - before_rel_y) < 0.08
    finally:
        win.close()


def test_m10_annot_bar_icon_only_and_swatches_vertically_centered(tmp_path: Path) -> None:
    app = _qapp()
    pdf_path = _make_multipage_links_pdf(tmp_path / "annot_ui.pdf")
    win = MainWindow(ThemeProfile(base_mode="light"), persist_profile=False)
    win.resize(1100, 700)
    win.show()
    try:
        assert win.open_document_path(pdf_path, switch_to_viewer=True)
        tab = win.viewer_view.current_tab()
        assert tab is not None
        tab.set_annotation_bar_visible(True)
        app.processEvents()

        all_btns = [
            *tab._tool_btns.values(),
            tab.touch_write_btn,
            tab.undo_annot_btn,
            tab.save_annot_btn,
            tab.close_annot_btn,
        ]
        for btn in all_btns:
            assert btn.text() == ""
            assert not btn.icon().isNull()
            assert bool(btn.toolTip().strip())

        ref_cy = tab._tool_btns["select"].mapTo(tab.annot_bar, QPoint(0, tab._tool_btns["select"].height() // 2)).y()
        for sw in tab._swatch_btns:
            sw_cy = sw.mapTo(tab.annot_bar, QPoint(0, sw.height() // 2)).y()
            assert abs(sw_cy - ref_cy) <= 2
    finally:
        win.close()


def test_m10_updater_conditional_one_click_and_rendered_md_notes(tmp_path: Path) -> None:
    app = _qapp()
    installer_bytes = b"TRODAI-FAKE-UPDATER-041"
    digest = hashlib.sha256(installer_bytes).hexdigest()
    md_notes = "## Novedades v0.4.1\n- **Mejora:** Notas legibles en HTML y flujo directo."
    mode_payload = {"tag": "v0.4.1"}

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/releases/latest":
                payload = json.dumps(
                    {
                        "tag_name": mode_payload["tag"],
                        "body": md_notes,
                        "html_url": "https://example.invalid/rel",
                        "assets": [
                            {
                                "name": "PDF-MD-Lector-Setup.exe",
                                "browser_download_url": f"http://127.0.0.1:{srv.server_port}/Setup.exe",
                                "size": len(installer_bytes),
                            },
                            {
                                "name": "SHA256SUMS.txt",
                                "browser_download_url": f"http://127.0.0.1:{srv.server_port}/SHA256SUMS.txt",
                                "size": 90,
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
        state_path = tmp_path / "upd.json"
        save_updater_state(UpdaterState(offline_only=False), state_path)

        # Up-to-date: install button and progress bar stay hidden
        dlg_ok = AboutUpdaterDialog(
            current_version="0.4.1",
            api_url=f"{base_url}/releases/latest",
            state_path=state_path,
        )
        try:
            rel_same = dlg_ok.check_now()
            assert rel_same is not None and not rel_same.update_available
            assert not dlg_ok.install_btn.isVisible()
            assert not dlg_ok.progress_bar.isVisible()
            assert isinstance(dlg_ok.notes_view, QTextBrowser)
            assert not dlg_ok.notes_view.openExternalLinks()
            html = dlg_ok.notes_view.toHtml()
            assert "<h2" in html and "<strong>" in html or "font-weight:700" in html.replace(" ", "")
            assert "## " not in dlg_ok.notes_view.toPlainText()
        finally:
            dlg_ok.close()

        # Newer update available: 1-click button becomes visible and triggers download + launcher with keyword args
        dlg_up = AboutUpdaterDialog(
            current_version="0.4.0",
            api_url=f"{base_url}/releases/latest",
            state_path=state_path,
        )
        try:
            rel_new = dlg_up.check_now()
            assert rel_new is not None and rel_new.update_available
            dlg_up.show()
            app.processEvents()
            assert dlg_up.install_btn.isVisible() and dlg_up.install_btn.isEnabled()

            captured: dict[str, object] = {}

            def _strict_kw_launcher(*, installer_path: Path, relaunch_exe: Path | None = None) -> None:
                captured["installer_path"] = installer_path
                captured["relaunch_exe"] = relaunch_exe

            pkg = dlg_up.download_current_release()
            assert pkg is not None and pkg.exists()
            ok = dlg_up.install_downloaded_release(
                launcher=_strict_kw_launcher,
                quit_callback=lambda: captured.setdefault("quit", True),
            )
            assert ok is True
            assert captured["installer_path"] == pkg
            assert captured["quit"] is True
        finally:
            dlg_up.close()
    finally:
        srv.shutdown()
        srv.server_close()
