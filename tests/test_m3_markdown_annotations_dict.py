"""Comprehensive M3 unit & integration tests for Markdown, PDFium hit-testing, ISO 32000 annotations, PdfCanvas, mixed ViewerView, and Kindle Dictionary."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pikepdf
import pypdfium2 as pdfium
import pytest
from PySide6.QtCore import QEvent, QPointF, Qt, QUrl
from PySide6.QtGui import QImage, QInputDevice, QPointingDevice, QTabletEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton

sys.path.insert(0, str(Path(__file__).resolve().parent / "e2e"))
from fixtures import (  # type: ignore[import-not-found]
    make_adversarial_markdown,
    make_corrupt_pdf,
    make_encrypted_aes256_pdf,
    make_gfm_markdown_bundle,
    make_local_glossary_json,
    make_multipage_toc_pdf,
    make_scanned_text_pdf,
)

from pdf_lector.core.bookmarks_store import BookmarksStore
from pdf_lector.core.dictionary_engine import DictionaryEngine
from pdf_lector.core.markdown_engine import (
    markdown_to_pdf,
    normalize_toc_outline_levels,
    parse_markdown_toc,
    pdf_to_markdown,
    render_markdown_to_safe_html,
)
from pdf_lector.core.md_security import is_safe_anchor, sanitize_markdown_html, validate_doc_resource_ref
from pdf_lector.core.pdf_annotations import (
    CLASSIC_HIGHLIGHT_COLORS,
    DEFAULT_ANON_AUTHOR,
    AnnotationRecord,
    load_pdf_annotations,
    save_pdf_annotations,
)
from pdf_lector.core.pdf_engine import PdfDocumentSession
from pdf_lector.theme.motion_engine import MotionPerformanceGuard
from pdf_lector.theme.palette import register_embedded_fonts
from pdf_lector.theme.theme_profile import get_preset
from pdf_lector.ui.md_tab_page import MarkdownTabPage
from pdf_lector.ui.pdf_canvas import PdfCanvas
from pdf_lector.ui.viewer_view import PdfTabPage, ViewerView


@pytest.fixture(scope="session")
def qapp() -> QApplication:
    app = QApplication.instance() or QApplication(["pytest-m3"])
    register_embedded_fonts(app)
    return app


# ---------------------------------------------------------------------------
# Feature #20: Markdown Security & Engine (GFM, nh3, path_guard, MD<->PDF)
# ---------------------------------------------------------------------------

def test_md_security_blocks_xss_schemes_unc_and_remote_images(tmp_path: Path) -> None:
    assert is_safe_anchor("#section-1") is True
    assert is_safe_anchor("#../evil") is False
    assert is_safe_anchor("#http://evil") is False

    allowed, _, _ = validate_doc_resource_ref("./local_diagram.png", base_dir=tmp_path, is_image=True)
    assert allowed is True

    allowed_remote, _, reason_remote = validate_doc_resource_ref(
        "https://example.com/pixel.png", base_dir=tmp_path, is_image=True, allow_remote_images=False
    )
    assert not allowed_remote and reason_remote == "remote_image_blocked"

    allowed_remote_opt_in, _, _ = validate_doc_resource_ref(
        "https://example.com/pixel.png", base_dir=tmp_path, is_image=True, allow_remote_images=True
    )
    assert allowed_remote_opt_in is True

    payload = (
        "# Seguridad\n\n"
        "<script>alert(1)</script>\n\n"
        '<img src="local.png" onerror="alert(1)">\n\n'
        '<iframe src="https://evil.invalid"></iframe>\n\n'
        "[js](javascript:alert(1))\n\n"
        "[vbs](vbscript:msgbox(1))\n\n"
        "![tracker](https://example.com/t.png)\n\n"
        r"![unc](\\192.168.1.50\share\leak.png)" + "\n\n"
        "![trav](../../outside_secret.png)\n\n"
        "![file](file:///C:/Windows/win.ini)\n\n"
        "![data](data:text/html;base64,PHNjcmlwdD4=)\n"
    )
    html, blocked = render_markdown_to_safe_html(payload, base_dir=tmp_path, allow_remote_images=False)
    low = html.lower()
    for bad in (
        "<script",
        "onerror=",
        "<iframe",
        "javascript:",
        "vbscript:",
        "https://example.com",
        "192.168.1.50",
        "outside_secret.png",
        "win.ini",
        "data:text/html",
    ):
        assert bad not in low
    assert len(blocked) >= 6

    res = sanitize_markdown_html('<svg onload="alert(1)"></svg><p>ok</p>', base_dir=tmp_path)
    assert "onload" not in res.html.lower()
    assert "<p>ok</p>" in res.html


def test_markdown_toc_smart_root_promotion_and_md_pdf_roundtrip(tmp_path: Path) -> None:
    bundle = make_gfm_markdown_bundle(tmp_path / "md")
    md_text = bundle["valid_md"].read_text(encoding="utf-8")
    toc = parse_markdown_toc(md_text)
    assert len(toc) == 4
    assert [t.level for t in toc] == [1, 2, 3, 2]
    assert normalize_toc_outline_levels(toc) == [0, 0, 1, 0]

    # Code blocks containing '#' must not pollute TOC
    fenced = "# Real\n\n```bash\n# comment\n## not heading\n```\n\n## Second\n"
    fenced_toc = parse_markdown_toc(fenced)
    assert [t.title for t in fenced_toc] == ["Real", "Second"]

    # Native MD -> PDF with TOC bookmarks + dark/light themes
    out_pdf = tmp_path / "gfm_light.pdf"
    markdown_to_pdf(bundle["valid_md"], out_pdf, dpi=150, source_lang="es", theme="light")
    assert out_pdf.exists()
    doc = pdfium.PdfDocument(str(out_pdf))
    try:
        pdf_toc = list(doc.get_toc())
        assert len(pdf_toc) >= 4
        top_level = [b for b in pdf_toc if int(b.level) == 0]
        assert len(top_level) >= 2
    finally:
        doc.close()

    dark_pdf = tmp_path / "gfm_dark.pdf"
    markdown_to_pdf(bundle["valid_markdown"], dark_pdf, dpi=150, source_lang="es", theme="dark")
    assert dark_pdf.exists() and dark_pdf.stat().st_size > 0

    # PDF -> MD roundtrip
    out_md = tmp_path / "roundtrip.md"
    pdf_to_markdown(out_pdf, out_md, dpi=150, source_lang="es")
    rt_text = out_md.read_text(encoding="utf-8")
    assert "Titulo Principal" in rt_text
    assert len(parse_markdown_toc(rt_text)) >= 3


# ---------------------------------------------------------------------------
# Feature #21: PDFium Word Boxes, Hit-Testing, Selection Quads & CW Rotations
# ---------------------------------------------------------------------------

def test_pdfium_word_hit_testing_and_cw_rotation_transforms(tmp_path: Path) -> None:
    pdf_path = make_multipage_toc_pdf(tmp_path / "hit.pdf", pages=2)
    session = PdfDocumentSession(pdf_path)
    try:
        w, h = session.get_page_size(0)
        assert w > 0 and h > 0

        boxes = session.get_page_word_boxes(0)
        assert len(boxes) >= 10
        target = next(b for b in boxes if "privacidad" in b.word.lower())
        cx = (target.rect_pdf[0] + target.rect_pdf[2]) / 2.0
        cy = (target.rect_pdf[1] + target.rect_pdf[3]) / 2.0

        # hit_test_word uses unrotated PDF user-space coordinates across rotation kwargs
        for rot in (0, 90, 180, 270):
            hit = session.hit_test_word(0, cx, cy, rotation=rot)
            assert hit is not None and "privacidad" in hit.word.lower()

        # Round-trip screen <-> PDF coordinates across all CW rotations & scales
        for rot in (0, 90, 180, 270):
            sx, sy = session.pdf_to_screen_point(0, cx, cy, scale=1.5, rotation=rot)
            rx, ry = session.screen_to_pdf_point(0, sx, sy, scale=1.5, rotation=rot)
            assert abs(rx - cx) < 1e-4 and abs(ry - cy) < 1e-4
            hit_scr = session.hit_test_word_screen(0, sx, sy, scale=1.5, rotation=rot)
            assert hit_scr is not None and "privacidad" in hit_scr.word.lower()

        # Selection rect
        text, quads = session.select_text_in_rect(0, (40.0, 620.0, 560.0, 750.0))
        assert "Lectura" in text and len(quads) >= 1

        # Negative / empty / OOB
        assert session.hit_test_word(0, 2.0, 2.0) is None
        assert session.hit_test_word(-1, cx, cy) is None
        assert session.hit_test_word(99, cx, cy) is None
    finally:
        session.close()

    with pytest.raises(RuntimeError):
        session.hit_test_word(0, 100.0, 100.0)

    scan_pdf = make_scanned_text_pdf(tmp_path / "scan.pdf")
    scan_sess = PdfDocumentSession(scan_pdf)
    try:
        assert scan_sess.get_page_word_boxes(0) == []
        assert scan_sess.hit_test_word(0, 200.0, 400.0) is None
    finally:
        scan_sess.close()


# ---------------------------------------------------------------------------
# Feature #22: ISO 32000 Annotations Persistence, Backup, Flatten & Privacy
# ---------------------------------------------------------------------------

def test_iso32000_annotations_roundtrip_backup_flatten_and_security(tmp_path: Path) -> None:
    pdf_path = make_multipage_toc_pdf(tmp_path / "annots.pdf", pages=2)
    orig_sha = hashlib.sha256(pdf_path.read_bytes()).hexdigest()

    records: list[AnnotationRecord] = [
        AnnotationRecord(
            f"hl-{i}",
            0,
            "highlight",
            (72.0, 680.0 - i * 16.0, 200.0, 692.0 - i * 16.0),
            (),
            col,
            "",
            "2026-11-25T10:00:00Z",
            "",
            None,
        )
        for i, col in enumerate(CLASSIC_HIGHLIGHT_COLORS)
    ]
    records.append(
        AnnotationRecord(
            "note-root",
            0,
            "text",
            (90.0, 500.0, 114.0, 524.0),
            (),
            "#FEF08A",
            "Revisor Local",
            "2026-11-25T10:01:00Z",
            "Revisar seccion 1",
            None,
        )
    )
    records.append(
        AnnotationRecord(
            "popup-reply",
            0,
            "popup",
            (120.0, 480.0, 260.0, 540.0),
            (),
            "#FEF08A",
            "",
            "2026-11-25T10:02:00Z",
            "Verificado",
            "note-root",
        )
    )
    records.append(
        AnnotationRecord(
            "ink-1",
            0,
            "ink",
            (80.0, 380.0, 200.0, 430.0),
            [[(80.0, 390.0), (140.0, 420.0), (200.0, 400.0)]],
            "#0EA5E9",
            "",
            "2026-11-25T10:03:00Z",
            "",
            None,
        )
    )
    records.append(
        AnnotationRecord(
            "ft-1",
            1,
            "freetext",
            (72.0, 600.0, 220.0, 630.0),
            (),
            "#ZZZZZZ",  # invalid color -> sanitized
            "",
            "2026-11-25T10:04:00Z",
            "Texto libre",
            None,
        )
    )

    save_pdf_annotations(pdf_path, records, create_backup=True, flatten=False)
    baks = list(tmp_path.glob("annots*.bak*"))
    assert len(baks) == 1
    assert hashlib.sha256(baks[0].read_bytes()).hexdigest() == orig_sha

    loaded = load_pdf_annotations(pdf_path)
    assert len(loaded) == 10
    reply = next(r for r in loaded if r.id == "popup-reply")
    assert reply.parent_id == "note-root" and reply.author == DEFAULT_ANON_AUTHOR
    os_user = os.environ.get("USERNAME", "")
    if os_user:
        assert all(r.author != os_user for r in loaded)
    ft = next(r for r in loaded if r.id == "ft-1")
    assert ft.color_hex.startswith("#") and len(ft.color_hex) == 7

    with pikepdf.open(pdf_path) as pdf:
        ann0 = pdf.pages[0]["/Annots"]
        assert any("/AP" in a and "/N" in a["/AP"] for a in ann0)
        assert any(str(a.get("/Subtype")) == "/Ink" and "/InkList" in a for a in ann0)

    # Flatten output removes /Annots and bakes vector XObjects
    flat_pdf = tmp_path / "annots_flat.pdf"
    save_pdf_annotations(pdf_path, records, output_path=flat_pdf, create_backup=False, flatten=True)
    with pikepdf.open(flat_pdf) as pdf:
        assert len(pdf.pages[0].get("/Annots", [])) == 0
        assert len(pdf.pages[1].get("/Annots", [])) == 0
        assert any(str(k).startswith("/TRODAI_AP_") for k in pdf.pages[0]["/Resources"]["/XObject"].keys())

    # Boundary guards
    with pytest.raises(ValueError):
        load_pdf_annotations(Path(r"\\192.168.1.99\share\doc.pdf"))
    with pytest.raises((ValueError, OSError)):
        load_pdf_annotations(make_corrupt_pdf(tmp_path / "corrupt.pdf"))
    with pytest.raises(PermissionError):
        save_pdf_annotations(make_encrypted_aes256_pdf(tmp_path / "enc.pdf"), records, create_backup=False)
    with pytest.raises(ValueError):
        save_pdf_annotations(
            pdf_path,
            [AnnotationRecord("bad-ink", 0, "ink", (0, 0, 10, 10), [], "#0EA5E9", "", "", "", None)],
            create_backup=False,
        )


# ---------------------------------------------------------------------------
# Feature #23: PdfCanvas Touch / Stylus / Eraser / Undo / Kindle Lookup
# ---------------------------------------------------------------------------

def test_pdf_canvas_touch_stylus_palm_rejection_eraser_undo_and_lookup(tmp_path: Path, qapp: QApplication) -> None:
    session = PdfDocumentSession(make_multipage_toc_pdf(tmp_path / "canvas.pdf", pages=1))
    canvas = PdfCanvas()
    try:
        canvas.set_session(session, page_index=0)
        canvas.set_rotation_and_zoom(90, 1.5)
        canvas.resize(620, 800)
        canvas.show()
        qapp.processEvents()

        # Touch writing 1-finger vs 2-finger pan/zoom vs stylus palm rejection
        canvas.set_touch_writing_enabled(True)
        assert canvas.touch_writing_enabled is True
        assert canvas.handle_touch_points([(QPointF(80.0, 200.0), 1), (QPointF(150.0, 250.0), 1)], phase="draw") == "ink"
        assert len([a for a in canvas.annotations() if a.kind == "ink"]) == 1

        # Two distinct touch IDs -> pan_zoom (no extra ink)
        assert canvas.handle_touch_points([(QPointF(80.0, 200.0), 1), (QPointF(180.0, 260.0), 2)], phase="update") == "pan_zoom"
        assert len([a for a in canvas.annotations() if a.kind == "ink"]) == 1

        # Stylus proximity blocks touch ink (palm rejection)
        canvas.set_stylus_in_proximity(True)
        assert canvas.handle_touch_points([(QPointF(90.0, 210.0), 3), (QPointF(160.0, 270.0), 3)], phase="draw") == "palm_rejected"
        assert len([a for a in canvas.annotations() if a.kind == "ink"]) == 1
        canvas.set_stylus_in_proximity(False)

        # Eraser + Ctrl+Z undo restores & removes cleanly
        note = canvas.add_sticky_note((120.0, 420.0), "Nota tactil")
        assert len(canvas.annotations()) == 2
        erased = canvas.erase_annotation_at(125.0, 425.0, radius_pdf=12.0)
        assert erased is not None and erased.id == note.id
        assert len(canvas.annotations()) == 1
        canvas.undo_last_annotation()
        assert len(canvas.annotations()) == 2

        # Dictionary lookup strips trailing punctuation
        seen: list[str] = []
        canvas.lookup_word_requested.connect(seen.append)
        canvas.request_dictionary_lookup("privacidad,")
        assert seen == ["privacidad"]

        # Paint pass renders without error
        canvas.set_rendered_image(session.render_page(0, scale=1.5, rotation=90))
        canvas.repaint()
        qapp.processEvents()
    finally:
        canvas.close()
        session.close()


# ---------------------------------------------------------------------------
# Feature #24 & #25: Mixed ViewerView, MarkdownTabPage, Kindle Dictionary
# ---------------------------------------------------------------------------

def _find_clipped_buttons(root_widget) -> list[str]:
    clipped: list[str] = []
    for btn in root_widget.findChildren(QPushButton):
        if not btn.isVisible():
            continue
        txt = btn.text().strip()
        if not txt:
            continue
        needed = btn.fontMetrics().horizontalAdvance(txt) + 12
        if btn.width() < needed:
            clipped.append(f"{txt} (w={btn.width()} < needed={needed})")
    return clipped


def test_mixed_viewer_md_modes_dictionary_detach_dock_and_zero_clipping(
    tmp_path: Path, qapp: QApplication
) -> None:
    pdf_p = make_multipage_toc_pdf(tmp_path / "doc.pdf", pages=2)
    bundle = make_gfm_markdown_bundle(tmp_path / "md")
    md_p = bundle["valid_md"]
    adv = make_adversarial_markdown(tmp_path / "adv")

    viewer = ViewerView(
        bookmarks_store=BookmarksStore(tmp_path / "bm.json"),
        dictionary_engine=DictionaryEngine(tmp_path / "dicts"),
        guard=MotionPerformanceGuard(os_animations_fn=lambda: True, battery_saver_fn=lambda: False),
    )
    try:
        viewer.resize(980, 650)
        viewer.show()
        qapp.processEvents()

        # Dictionary starts closed by default
        assert viewer.open_document(pdf_p) is True
        assert viewer.dict_panel.isVisible() is False
        assert viewer.dict_btn.isChecked() is False

        # PdfTabPage wires PdfCanvas and saves annotations with .bak
        pdf_tab = viewer.current_tab()
        assert isinstance(pdf_tab, PdfTabPage)
        pdf_tab.pdf_canvas.add_highlight_rect((72.0, 650.0, 210.0, 668.0), color_hex="#FDE047")
        assert viewer.has_unsaved_changes() is True
        assert viewer.save_current() is True
        assert viewer.has_unsaved_changes() is False
        assert list(tmp_path.glob("doc*.bak*"))

        # Open Markdown tab alongside PDF tab; duplicate path focuses existing tab
        assert viewer.open_document(md_p) is True
        assert viewer.tabs.count() == 2
        assert viewer.open_document(md_p) is True
        assert viewer.tabs.count() == 2

        md_tab = viewer.current_tab()
        assert isinstance(md_tab, MarkdownTabPage)
        assert md_tab.preview_browser.openExternalLinks() is False
        assert md_tab.toc_tree.topLevelItemCount() >= 2

        # 3-mode Markdown switching + edit + save
        md_tab.set_view_mode("read")
        qapp.processEvents()
        assert md_tab.preview_browser.isVisible() and not md_tab.editor_widget.isVisible()

        md_tab.set_view_mode("split")
        qapp.processEvents()
        assert md_tab.preview_browser.isVisible() and md_tab.editor_widget.isVisible()
        md_tab.insert_markdown_snippet("\n## Seccion M3\n")
        qapp.processEvents()
        assert md_tab.has_unsaved_changes() is True
        assert "Seccion M3" in md_tab.preview_browser.toPlainText()
        assert viewer.save_current() is True
        assert md_tab.has_unsaved_changes() is False

        md_tab.set_view_mode("edit")
        qapp.processEvents()
        assert md_tab.editor_widget.isVisible() and md_tab.format_toolbar.isVisible()

        # Unsafe browser resource load is blocked cleanly
        img_res = md_tab.preview_browser.loadResource(2, QUrl("https://evil.invalid/leak.png"))
        assert isinstance(img_res, QImage) and img_res.size().width() == 1
        assert "https://evil.invalid/leak.png" in md_tab.preview_browser._blocked_resources

        # Adversarial MD tab blocks scripts and logs blocked_resources
        combined = tmp_path / "adv" / "combined.md"
        combined.write_text(
            adv["xss"].read_text(encoding="utf-8") + "\n" + adv["traversal"].read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        assert viewer.open_document(combined) is True
        adv_tab = viewer.current_tab()
        assert isinstance(adv_tab, MarkdownTabPage)
        assert "<script" not in adv_tab.preview_browser.toHtml().lower()
        assert len(adv_tab.blocked_resources) >= 4

        # Unsafe paths rejected
        assert viewer.open_document(Path(r"\\evil\share\doc.md")) is False
        assert viewer.open_document(tmp_path / "missing.md") is False

        # Kindle Dictionary lookup -> detach -> close_detached -> reopen -> dock -> hide
        viewer.lookup_in_dictionary("página", lang="es")
        qapp.processEvents()
        assert viewer.dict_panel.isVisible() is True
        assert viewer.dict_btn.isChecked() is True
        assert "pagina" in viewer.dict_panel.res_word_lbl.text().lower() or "página" in viewer.dict_panel.res_word_lbl.text().lower()

        viewer.detach_dictionary()
        qapp.processEvents()
        assert viewer.is_dictionary_detached() is True

        viewer.close_detached_dictionary()
        qapp.processEvents()
        assert viewer.is_dictionary_detached() is False
        assert viewer.dict_btn.isChecked() is False
        assert viewer.dict_panel.isVisible() is False

        viewer.set_dictionary_visible(True)
        qapp.processEvents()
        assert viewer.dict_panel.isVisible() is True
        viewer.detach_dictionary()
        viewer.dock_dictionary()
        qapp.processEvents()
        assert viewer.is_dictionary_detached() is False
        assert viewer.dict_panel.isVisible() is True

        # Zero clipped buttons across dark/light modes and 980x650 / 1240x688
        for mode_id in ("dark", "light"):
            viewer.apply_profile(get_preset("trodai_corporate" if mode_id == "dark" else "arch_grid"))
            for lang_id in ("es", "en", "pt"):
                viewer.retranslate(lang_id)
                for w, h in ((980, 650), (1240, 688)):
                    viewer.resize(w, h)
                    qapp.processEvents()
                    assert _find_clipped_buttons(viewer) == []
    finally:
        viewer.cleanup()
        viewer.close()
        qapp.processEvents()


def test_dictionary_engine_consent_gate_curl_fallback_and_path_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eng = DictionaryEngine(base_dir=tmp_path / "dicts")

    # Without consent or fetcher, outbound download raises PermissionError
    with pytest.raises(PermissionError, match="user_consent_required"):
        eng.download_language_pack("pt")
    with pytest.raises(PermissionError, match="user_consent_required"):
        eng.download_language_pack("pt", fetcher=lambda _u: b"[]", consent_granted=False)

    # curl.exe TLS fallback when urlopen raises OSError
    pt_payload = json.dumps(
        [{"word": "privacidade", "pos": "subst.", "definition": "Controle local.", "synonyms": ["sigilo"]}]
    ).encode("utf-8")

    def _boom_urlopen(*_args, **_kwargs):
        raise OSError("simulated TLS failure")

    def _fake_run(cmd, **kwargs):
        assert cmd[0] == "curl.exe"
        env = kwargs.get("env", {})
        assert not any(k.startswith("_MEIPASS") for k in env)
        return subprocess.CompletedProcess(cmd, 0, stdout=pt_payload, stderr=b"")

    monkeypatch.setattr("pdf_lector.core.dictionary_engine.urlopen", _boom_urlopen)
    monkeypatch.setattr("pdf_lector.core.dictionary_engine.subprocess.run", _fake_run)
    assert eng.download_language_pack("pt", consent_granted=True) == 1
    assert eng.lookup("privacidade", "pt")[0].pos == "subst."

    # Glossary validation via path_guard
    fr_json = make_local_glossary_json(tmp_path / "fr.json", fmt="list")
    assert eng.import_local_glossary("fr", fr_json) == 2
    bad_ext = tmp_path / "bad.txt"
    bad_ext.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported_extension"):
        eng.import_local_glossary("fr", bad_ext)
    with pytest.raises(ValueError, match="network_or_url_blocked"):
        eng.import_local_glossary("fr", Path(r"\\evil\share\fr.json"))


def test_m3_iteration1_regressions(tmp_path: Path, qapp: QApplication) -> None:
    # 1. style= dangerous CSS attributes reported in blocked_resources & stripped
    html_url, blocked_url = render_markdown_to_safe_html(
        '<div style="background:url(https://evil.com/leak.png)">hi</div>',
        base_dir=tmp_path,
    )
    assert "style=" not in html_url.lower() and "evil.com" not in html_url.lower()
    assert "attr:style" in blocked_url and "https://evil.com/leak.png" in blocked_url

    html_expr, blocked_expr = render_markdown_to_safe_html(
        '<p style="width:expression(alert(1))">hi</p>',
        base_dir=tmp_path,
    )
    assert "style=" not in html_expr.lower()
    assert "attr:style" in blocked_expr

    # 2. Output path validation in markdown_to_pdf / pdf_to_markdown rejects bad ext / ADS
    src_md = tmp_path / "src.md"
    src_md.write_text("# Hola\n", encoding="utf-8")
    with pytest.raises(ValueError):
        markdown_to_pdf(src_md, tmp_path / "out.exe")
    src_pdf = make_multipage_toc_pdf(tmp_path / "src.pdf", pages=1)
    with pytest.raises(ValueError):
        pdf_to_markdown(src_pdf, tmp_path / "out.txt")

    # 3. /FreeText /WinAnsiEncoding preserves Spanish diacritics when flattened
    ft_rec = AnnotationRecord(
        "ft-es",
        0,
        "freetext",
        (72.0, 520.0, 320.0, 560.0),
        (),
        "#0EA5E9",
        "An\u00f3nimo",
        "2026-11-26T10:00:00Z",
        "Informaci\u00f3n \u00fatil a\u00f1o ni\u00f1o",
        None,
    )
    flat_ft = tmp_path / "ft_flat.pdf"
    save_pdf_annotations(src_pdf, [ft_rec], output_path=flat_ft, create_backup=False, flatten=True)
    ft_sess = PdfDocumentSession(flat_ft)
    try:
        assert "Informaci\u00f3n \u00fatil a\u00f1o ni\u00f1o" in ft_sess.extract_page_text(0)
    finally:
        ft_sess.close()

    # 4. Non-zero /MediaBox origin coordinate mapping across 0/90/180/270
    mb_pdf = tmp_path / "nonzero_mediabox.pdf"
    with pikepdf.open(src_pdf) as pdf:
        mb = [float(v) for v in pdf.pages[0].MediaBox]
        pdf.pages[0]["/MediaBox"] = pikepdf.Array([mb[0] + 100.0, mb[1] + 100.0, mb[2] + 100.0, mb[3] + 100.0])
        pdf.save(mb_pdf)
    mb_sess = PdfDocumentSession(mb_pdf)
    try:
        boxes = mb_sess.get_page_word_boxes(0)
        target = next(b for b in boxes if "privacidad" in b.word.lower())
        cx = (target.rect_pdf[0] + target.rect_pdf[2]) / 2.0
        cy = (target.rect_pdf[1] + target.rect_pdf[3]) / 2.0
        for rot in (0, 90, 180, 270):
            sx, sy = mb_sess.pdf_to_screen_point(0, cx, cy, scale=1.5, rotation=rot)
            hit = mb_sess.hit_test_word_screen(0, sx, sy, scale=1.5, rotation=rot)
            assert hit is not None and "privacidad" in hit.word.lower()
    finally:
        mb_sess.close()

    # 5. QTabletEvent stylus/eraser + app-level TabletEnterProximity + stale pinch reset
    canvas = PdfCanvas()
    sess = PdfDocumentSession(src_pdf)
    try:
        canvas.set_session(sess, page_index=0)
        canvas.set_tool_mode("ink")
        canvas.set_touch_writing_enabled(True)
        canvas.resize(612, 792)
        canvas.show()
        qapp.processEvents()

        pen_dev = QPointingDevice(
            "stylus",
            10,
            QInputDevice.DeviceType.Stylus,
            QPointingDevice.PointerType.Pen,
            QInputDevice.Capability.Position | QInputDevice.Capability.Pressure,
            1,
            0,
        )
        eraser_dev = QPointingDevice(
            "eraser",
            11,
            QInputDevice.DeviceType.Stylus,
            QPointingDevice.PointerType.Eraser,
            QInputDevice.Capability.Position | QInputDevice.Capability.Pressure,
            1,
            0,
        )

        def _tab_ev(etype, dev, x, y, p, btn=Qt.MouseButton.LeftButton, btns=Qt.MouseButton.LeftButton):
            return QTabletEvent(
                etype,
                dev,
                QPointF(x, y),
                QPointF(x, y),
                p,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                Qt.KeyboardModifier.NoModifier,
                btn,
                btns,
            )

        # App-level TabletEnterProximity activates palm rejection before TabletPress
        qapp.sendEvent(
            qapp,
            _tab_ev(QEvent.Type.TabletEnterProximity, pen_dev, 100.0, 200.0, 0.0, Qt.MouseButton.NoButton, Qt.MouseButton.NoButton),
        )
        assert canvas.handle_touch_points([(QPointF(80.0, 200.0), 1), (QPointF(140.0, 240.0), 1)], phase="draw") == "palm_rejected"

        qapp.sendEvent(canvas, _tab_ev(QEvent.Type.TabletPress, pen_dev, 120.0, 240.0, 0.4))
        qapp.sendEvent(canvas, _tab_ev(QEvent.Type.TabletMove, pen_dev, 170.0, 260.0, 0.8))
        qapp.sendEvent(canvas, _tab_ev(QEvent.Type.TabletRelease, pen_dev, 210.0, 250.0, 0.0, Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton))
        assert len([a for a in canvas.annotations() if a.kind == "ink"]) == 1

        # Eraser pointerType erases stroke without AttributeError
        qapp.sendEvent(canvas, _tab_ev(QEvent.Type.TabletPress, eraser_dev, 170.0, 260.0, 0.5))
        qapp.sendEvent(canvas, _tab_ev(QEvent.Type.TabletRelease, eraser_dev, 170.0, 260.0, 0.0, Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton))
        assert len([a for a in canvas.annotations() if a.kind == "ink"]) == 0

        # Sequential 2->1 finger lift resets pinch centroid so next 2-finger pinch has zero pan jump
        pans: list[tuple[float, float]] = []
        canvas.pan_delta_requested.connect(lambda dx, dy: pans.append((dx, dy)))
        canvas.handle_touch_points([(QPointF(50.0, 50.0), 1), (QPointF(90.0, 90.0), 2)], phase="begin")
        canvas.handle_touch_points([(QPointF(55.0, 55.0), 1), (QPointF(95.0, 95.0), 2)], phase="update")
        canvas.handle_touch_points([(QPointF(55.0, 55.0), 1)], phase="update")
        canvas.handle_touch_points([(QPointF(55.0, 55.0), 1)], phase="end")
        pans.clear()
        canvas.handle_touch_points([(QPointF(300.0, 300.0), 3), (QPointF(360.0, 360.0), 4)], phase="begin")
        assert pans == []
    finally:
        canvas.close()
        sess.close()

    # 6. ViewerView: corrupt PDF & non-UTF8 MD return False; Ctrl+S has zero ambiguity; save_as_document overwrites open PDF
    corrupt_pdf = make_corrupt_pdf(tmp_path / "bad.pdf")
    non_utf8_md = tmp_path / "bad_enc.md"
    non_utf8_md.write_bytes(b"\xff\xfe\x80\x81 invalid")
    edit_md = tmp_path / "edit.md"
    edit_md.write_text("# Inicial\n", encoding="utf-8")
    open_pdf_p = make_multipage_toc_pdf(tmp_path / "open.pdf", pages=1)

    viewer = ViewerView(
        bookmarks_store=BookmarksStore(tmp_path / "bm2.json"),
        dictionary_engine=DictionaryEngine(tmp_path / "dicts2"),
    )
    try:
        viewer.show()
        qapp.processEvents()
        assert viewer.open_document(corrupt_pdf) is False
        assert viewer.open_document(non_utf8_md) is False

        assert viewer.open_document(edit_md) is True
        md_tab = viewer.current_tab()
        assert isinstance(md_tab, MarkdownTabPage)
        md_tab.set_view_mode("split")
        md_tab.editor_widget.setFocus()
        qapp.processEvents()
        ambig: list[str] = []
        viewer._save_shortcut.activatedAmbiguously.connect(lambda: ambig.append("Ctrl+S"))
        md_tab.insert_markdown_snippet("\n## Guardado Ctrl+S\n")
        assert md_tab.has_unsaved_changes() is True
        QTest.keyClick(md_tab.editor_widget, Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier)
        qapp.processEvents()
        assert ambig == []
        assert md_tab.has_unsaved_changes() is False
        assert "Guardado Ctrl+S" in edit_md.read_text(encoding="utf-8")

        assert viewer.open_document(open_pdf_p) is True
        pdf_tab = viewer.current_tab()
        assert isinstance(pdf_tab, PdfTabPage)
        pdf_tab.pdf_canvas.add_highlight_rect((72.0, 680.0, 220.0, 695.0), color_hex="#FDE047")
        assert pdf_tab.save_as_document(output_path=open_pdf_p, flatten=False) is True
        assert pdf_tab.has_unsaved_changes() is False
    finally:
        viewer.cleanup()
        viewer.close()
        qapp.processEvents()
