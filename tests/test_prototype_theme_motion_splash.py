"""Automated verification suite for PDF Lector by TRODAI prototype (v0.1.0-alpha.2)."""

from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import xml.etree.ElementTree as ET

from PIL import Image, ImageDraw
import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication, QPushButton

from pdf_lector import get_base_dir
from pdf_lector.core.bookmarks_store import BookmarksStore
from pdf_lector.core.dictionary_engine import DictionaryEngine
from pdf_lector.core.path_guard import validate_local_media_path, validate_local_pdf_path
from pdf_lector.core.pdf_engine import PdfDocumentSession, generate_sample_pdf
from pdf_lector.core.usage_insights import LocalUsageInsights, classify_document_text
from pdf_lector.theme.motion_engine import (
    MotionPerformanceGuard,
    compute_image_luminance,
    compute_scrim_alpha,
)
from pdf_lector.theme.palette import register_embedded_fonts
from pdf_lector.theme.theme_profile import (
    ThemeProfile,
    export_theme_package,
    get_preset,
    import_theme_package,
    load_theme_profile,
    save_theme_profile,
)
from pdf_lector.ui.main_window import MainWindow
from pdf_lector.ui.splash_screen import SplashScreen
from pdf_lector.ui.widgets import SlidingSegmentedControl, StatusTag, ToolCard

ROOT = get_base_dir()

EMOJI_PATTERN = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F1E6-\U0001F1FF"
    "]+",
    flags=re.UNICODE,
)


@pytest.fixture(scope="session")
def qapp() -> QApplication:
    app = QApplication.instance() or QApplication(["pytest-pdf-lector"])
    register_embedded_fonts(app)
    return app


def test_zero_unicode_emojis_in_source() -> None:
    src_dir = ROOT / "src" / "pdf_lector"
    offenders: list[str] = []
    for py_file in src_dir.rglob("*.py"):
        text = py_file.read_text(encoding="utf-8")
        if EMOJI_PATTERN.search(text):
            offenders.append(str(py_file))
    assert offenders == [], f"Unicode emojis found in: {offenders}"


def test_all_svgs_strictly_polygonal_and_solid_app_logo() -> None:
    icons_dir = ROOT / "assets" / "icons"
    svg_files = sorted(icons_dir.glob("*.svg"))
    assert len(svg_files) >= 42

    curve_re = re.compile(r"[CSQTacsqta]")
    for svg_path in svg_files:
        tree = ET.parse(svg_path)
        root = tree.getroot()
        for elem in root.iter():
            tag = elem.tag.split("}")[-1].lower()
            assert tag not in ("circle", "ellipse"), f"Forbidden curved element <{tag}> in {svg_path.name}"
            if tag == "path":
                d_attr = elem.attrib.get("d", "")
                assert not curve_re.search(d_attr), f"Curve command found in {svg_path.name}: {d_attr}"

    logo_txt = (icons_dir / "pdf_lector_logo.svg").read_text(encoding="utf-8")
    assert "fill-opacity" not in logo_txt
    assert "currentColor" not in logo_txt
    assert logo_txt != (icons_dir / "trodai_sentinel.svg").read_text(encoding="utf-8")


def test_solid_opaque_status_tags_and_uniform_toolcards(qapp: QApplication, tmp_path: Path) -> None:
    del qapp
    tag = StatusTag("PROCESO LOCAL", tone="ok")
    for mode in ("dark", "light"):
        for tone in ("ok", "accent", "crimson", "warn", "muted", "risk"):
            tag.set_tone(tone)
            tag.apply_theme(mode)
            assert "rgba(" not in tag.styleSheet().lower(), f"Translucent rgba found in {mode}/{tone}"

    win = MainWindow(
        get_preset("trodai_corporate"),
        persist_profile=False,
        sample_pdf_dir=tmp_path,
        insights_store=LocalUsageInsights(tmp_path / "ins.json"),
        bookmarks_store=BookmarksStore(tmp_path / "bm.json"),
        dictionary_engine=DictionaryEngine(tmp_path / "dicts"),
    )
    try:
        cards = win.hub_view.all_tool_cards()
        assert len(cards) >= 50
        for card in cards:
            assert card.width() == ToolCard.CARD_WIDTH == 232
            assert card.height() == ToolCard.CARD_HEIGHT == 104
    finally:
        win.close()


def test_sliding_segmented_control_fluid_switching(qapp: QApplication) -> None:
    del qapp
    seg = SlidingSegmentedControl()
    seg.resize(300, 34)
    seg.set_options([("thumbs", "Miniaturas"), ("toc", "Indice"), ("both", "Ambos")])
    seen: list[str] = []
    seg.selection_changed.connect(seen.append)

    seg.set_current("toc", animate=False, emit_signal=True)
    assert seg.current_key() == "toc"
    assert seen == ["toc"]

    seg.set_current("both", animate=True, emit_signal=True)
    assert seg.current_key() == "both"
    assert seen == ["toc", "both"]


def test_path_guard_blocks_unc_urls_and_invalid_extensions(tmp_path: Path) -> None:
    for unsafe in (
        r"\\192.168.1.50\share\banner.png",
        "//fileserver/public/banner.jpg",
        r"\\?\UNC\server\share\banner.png",
        "https://example.com/banner.png",
        "http://localhost/banner.gif",
        "file://server/share/banner.png",
        "",
    ):
        ok, _ = validate_local_media_path(unsafe, check_exists=False)
        assert not ok, f"Unsafe media path unexpectedly allowed: {unsafe}"

    bad_ext = tmp_path / "script.exe"
    bad_ext.write_bytes(b"MZ")
    ok, reason = validate_local_media_path(bad_ext, check_exists=True)
    assert not ok and reason == "unsupported_extension"

    good_png = tmp_path / "banner.png"
    Image.new("RGB", (32, 32), "#0EA5E9").save(good_png)
    ok, reason = validate_local_media_path(good_png, check_exists=True)
    assert ok and reason == "ok"

    good_pdf = generate_sample_pdf(tmp_path / "sample.pdf")
    ok_pdf, reason_pdf = validate_local_pdf_path(good_pdf, check_exists=True)
    assert ok_pdf and reason_pdf == "ok"


def test_theme_profile_sanitization_and_portability(tmp_path: Path) -> None:
    dirty = ThemeProfile(
        base_mode="neon",
        motion_level="extreme",
        motion_preset="weird",
        banner_path=r"\\evil\share\leak.png",
        opacity=5.0,
        blur_radius=99,
        fps_cap=144,
        language="fr",
    ).sanitized(check_media_exists=False)

    assert dirty.base_mode == "dark"
    assert dirty.motion_level == "static"
    assert dirty.motion_preset == "none"
    assert dirty.banner_path == ""
    assert dirty.opacity == 0.85
    assert dirty.blur_radius == 24
    assert dirty.fps_cap == 60
    assert dirty.language == "es"

    cfg_path = tmp_path / "theme_config.json"
    banner_img = tmp_path / "local_bg.png"
    Image.new("RGB", (64, 64), "#1E293B").save(banner_img)

    profile = replace(
        get_preset("editorial_aurora", preserve_language="en"),
        banner_path=str(banner_img),
        opacity=0.68,
        blur_radius=6,
    )
    save_theme_profile(profile, cfg_path)
    loaded = load_theme_profile(cfg_path)
    assert loaded.banner_path == str(banner_img)
    assert loaded.opacity == 0.68
    assert loaded.blur_radius == 6
    assert loaded.language == "en"

    pkg_path = tmp_path / "custom.trodai-theme.json"
    export_theme_package(profile, pkg_path)
    imported, warning = import_theme_package(pkg_path)
    assert warning is None
    assert imported.banner_path == str(banner_img)

    pkg_path.write_text(
        '{"base_mode": "dark", "motion_level": "modern", "banner_path": "\\\\\\\\server\\\\share\\\\p.png"}',
        encoding="utf-8",
    )
    imported_bad, warning_bad = import_theme_package(pkg_path)
    assert imported_bad.banner_path == ""
    assert warning_bad == "network_or_url_blocked"


def test_motion_performance_guard_states() -> None:
    modern = get_preset("trodai_mesh")
    static = get_preset("trodai_corporate")

    active_guard = MotionPerformanceGuard(os_animations_fn=lambda: True, battery_saver_fn=lambda: False)
    assert active_guard.evaluate(static, window_visible=True).status_code == "static"
    st_active = active_guard.evaluate(modern, window_visible=True)
    assert st_active.should_animate and st_active.status_code == "active" and st_active.target_fps == 60
    assert active_guard.evaluate(modern, window_visible=False).status_code == "hidden"
    assert active_guard.evaluate(modern, window_visible=True, heavy_task_active=True).status_code == "paused"

    battery_guard = MotionPerformanceGuard(os_animations_fn=lambda: True, battery_saver_fn=lambda: True)
    st_bat = battery_guard.evaluate(modern, window_visible=True)
    assert not st_bat.should_animate and st_bat.status_code == "battery"

    os_off_guard = MotionPerformanceGuard(os_animations_fn=lambda: False, battery_saver_fn=lambda: False)
    st_os = os_off_guard.evaluate(modern, window_visible=True)
    assert not st_os.should_animate and st_os.status_code == "os_off"


def test_auto_scrim_luminance_calibration(qapp: QApplication) -> None:
    del qapp
    white_img = QImage(16, 16, QImage.Format.Format_RGB32)
    white_img.fill(QColor("#FFFFFF"))
    dark_img = QImage(16, 16, QImage.Format.Format_RGB32)
    dark_img.fill(QColor("#0A0F1D"))

    lum_white = compute_image_luminance(white_img)
    lum_dark = compute_image_luminance(dark_img)
    assert lum_white > 0.95
    assert lum_dark < 0.08

    assert compute_scrim_alpha(lum_white, base_mode="dark", user_opacity=0.7, auto_scrim=True) > compute_scrim_alpha(
        lum_dark, base_mode="dark", user_opacity=0.7, auto_scrim=True
    )
    assert compute_scrim_alpha(lum_dark, base_mode="light", user_opacity=0.7, auto_scrim=True) > compute_scrim_alpha(
        lum_white, base_mode="light", user_opacity=0.7, auto_scrim=True
    )
    assert compute_scrim_alpha(lum_white, base_mode="dark", user_opacity=0.7, auto_scrim=False) == 0


def test_pdfium_toc_text_extraction_and_bookmarks_store(qapp: QApplication, tmp_path: Path) -> None:
    del qapp
    pdf_path = generate_sample_pdf(tmp_path / "specimen.pdf")
    session = PdfDocumentSession(pdf_path, max_cache_entries=2, max_cache_bytes=16 * 1024 * 1024)
    try:
        assert session.page_count == 4
        toc = session.get_toc()
        assert len(toc) >= 5
        assert toc[0].page_index == 0
        assert any(item.level == 1 for item in toc)

        txt = session.extract_page_text(0)
        assert "PDF Lector" in txt
        words = session.extract_page_words(0, limit=10)
        assert len(words) > 0

        img0 = session.render_page(0, scale=1.0)
        img1 = session.render_page(1, scale=1.0)
        img2 = session.render_page(2, scale=1.0)
        assert not img0.isNull() and not img1.isNull() and not img2.isNull()
        assert session.cache_count == 2
        assert session.get_cached_page(0, scale=1.0) is None
    finally:
        session.close()

    bm_store = BookmarksStore(tmp_path / "bookmarks.json")
    added = bm_store.add_bookmark(pdf_path, "Revisar Factura", 2)
    listed = bm_store.list_bookmarks(pdf_path)
    assert len(listed) == 1 and listed[0].title == "Revisar Factura" and listed[0].page_index == 2
    assert bm_store.remove_bookmark(pdf_path, added.bookmark_id)
    assert bm_store.list_bookmarks(pdf_path) == []


def test_dictionary_engine_offline_and_on_demand_packs(tmp_path: Path) -> None:
    engine = DictionaryEngine(base_dir=tmp_path / "dicts")
    installed = engine.installed_languages()
    assert "es" in installed and "en" in installed

    # Accent-insensitive and plural fallback lookups
    hits_es = engine.lookup("página", "es")
    assert hits_es and "folio" in hits_es[0].synonyms or "hoja" in hits_es[0].synonyms
    hits_en = engine.lookup("privacy", "en")
    assert hits_en and "local" in hits_en[0].definition.lower()
    assert len(engine.suggest("priv", "es")) >= 1

    # Mocked user-triggered language pack download + remove
    pt_payload = json.dumps([
        {"word": "fatura", "pos": "subst.", "definition": "Documento comercial fiscal.", "synonyms": ["recibo"]},
        {"word": "privacidade", "pos": "subst.", "definition": "Controle local dos dados.", "synonyms": ["sigilo"]},
    ]).encode("utf-8")
    count = engine.download_language_pack("pt", fetcher=lambda _url: pt_payload)
    assert count == 2
    assert "pt" in engine.installed_languages()
    assert engine.lookup("privacidade", "pt")[0].pos == "subst."
    assert engine.remove_language_pack("pt")
    assert "pt" not in engine.installed_languages()

    # Local JSON glossary import + UNC rejection
    local_json = tmp_path / "custom_fr.json"
    local_json.write_text(json.dumps({"contrat": "Accord juridique entre parties."}), encoding="utf-8")
    assert engine.import_local_glossary("fr", local_json) == 1
    assert engine.lookup("contrat", "fr")
    with pytest.raises(ValueError, match="network_or_url_blocked"):
        engine.import_local_glossary("fr", Path(r"\\evil\share\bad.json"))


def test_local_usage_insights_recommendations_and_privacy_controls(tmp_path: Path) -> None:
    assert classify_document_text("FACTURA ELECTRONICA NIT 900123 CUFE IVA 19%") == "invoice"
    assert classify_document_text("CONTRATO DE PRESTACION DE SERVICIOS CLAUSULA 1") == "contract"
    assert classify_document_text("   ", page_count=3) == "scan"

    insights = LocalUsageInsights(tmp_path / "insights.json")
    insights.record_document_opened("Factura electronica DIAN UBL 2.1 subtotal IVA", page_count=2)
    insights.record_search("factura dian")
    recs = insights.get_recommended_tools(limit=4)
    rec_ids = [tid for tid, _ in recs]
    assert "validate_einvoice" in rec_ids

    insights.set_enabled(False)
    assert insights.get_recommended_tools() == []
    insights.set_enabled(True)
    insights.clear_history()
    starter_ids = [tid for tid, _ in insights.get_recommended_tools()]
    assert "sign_pdf" in starter_ids


def test_splash_screen_branding_and_loading_bar(qapp: QApplication) -> None:
    splash = SplashScreen(mode="dark", lang="es")
    try:
        splash.show()
        splash.set_progress(65, "Cargando...")
        qapp.processEvents()
        assert splash.title_lbl.text() == "PDF/MD Lector"
        assert "TRODAI" in splash.company_lbl.text()
        assert splash.progress_value == 65
        assert splash.progress_bar.value() == 65
        assert not splash.app_logo_lbl.pixmap().isNull()
        assert not splash.sentinel_lbl.pixmap().isNull()
    finally:
        splash.finish_and_close()


def _find_clipped_buttons(root_widget: QWidget) -> list[str]:
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


def test_window_minmax_fullscreen_viewer_modes_and_zero_clipping(qapp: QApplication, tmp_path: Path) -> None:
    png_path = tmp_path / "custom_banner.png"
    img = Image.new("RGB", (640, 240), "#1D4ED8")
    ImageDraw.Draw(img).rectangle([40, 40, 600, 200], outline="#38BDF8", width=4)
    img.save(png_path)

    win = MainWindow(
        get_preset("trodai_mesh"),
        persist_profile=False,
        sample_pdf_dir=tmp_path,
        insights_store=LocalUsageInsights(tmp_path / "ins.json"),
        bookmarks_store=BookmarksStore(tmp_path / "bm.json"),
        dictionary_engine=DictionaryEngine(tmp_path / "dicts"),
    )
    try:
        # Native Min/Max/Close window buttons enabled & geometry fits screen
        flags = win.windowFlags()
        assert flags & Qt.WindowType.WindowMinimizeButtonHint
        assert flags & Qt.WindowType.WindowMaximizeButtonHint
        assert flags & Qt.WindowType.WindowCloseButtonHint
        screen = qapp.primaryScreen()
        if screen is not None:
            avail = screen.availableGeometry()
            assert win.height() <= avail.height()

        win.ensure_sample_pdf_loaded()
        tab = win.viewer_view.current_tab()
        assert tab is not None
        assert tab.toc_tree.topLevelItemCount() >= 4

        # Verify 3-mode rail switching (thumbs / toc / both)
        tab.set_rail_mode("thumbs")
        assert tab.thumb_list.isVisibleTo(tab.nav_rail) and not tab.toc_pane.isVisibleTo(tab.nav_rail)
        tab.set_rail_mode("toc")
        assert tab.toc_pane.isVisibleTo(tab.nav_rail) and not tab.thumb_list.isVisibleTo(tab.nav_rail)
        tab.set_rail_mode("both")
        assert tab.toc_pane.isVisibleTo(tab.nav_rail) and tab.thumb_list.isVisibleTo(tab.nav_rail)

        # Verify dictionary panel toggle & lookup
        win.viewer_view.set_dictionary_visible(True)
        win.viewer_view.dict_panel.lookup_word("privacidad", record_event=False)
        assert "privacidad" in win.viewer_view.dict_panel.res_word_lbl.text().lower()

        # Verify ThemeStudio has zero accent swatch buttons
        assert not hasattr(win.studio_view, "_swatch_btns")
        assert win.studio_view.apply_banner_path(str(png_path))

        # Verify fullscreen toggle inside Viewer hides Sidebar + TopBar and exits cleanly
        win.switch_view(1, immediate=True)
        win.show()
        qapp.processEvents()
        win.set_fullscreen_mode(True)
        qapp.processEvents()
        assert win.is_fullscreen_mode
        assert not win.sidebar.isVisible() and not win.topbar.isVisible()
        win.set_fullscreen_mode(False)
        qapp.processEvents()
        assert not win.is_fullscreen_mode
        assert win.sidebar.isVisible() and win.topbar.isVisible()

        for mode_id in ("dark", "light"):
            win.apply_profile(
                replace(get_preset("arch_grid" if mode_id == "light" else "trodai_mesh"), base_mode=mode_id),
                save=False,
            )
            for w, h in ((980, 650), (1240, 688)):
                win.resize(w, h)
                for tab_idx in (0, 1, 2):
                    win.switch_view(tab_idx, immediate=True)
                    qapp.processEvents()
                    clipped = _find_clipped_buttons(win)
                    assert clipped == [], f"Clipped buttons in mode={mode_id} {w}x{h} view={tab_idx}: {clipped}"
    finally:
        win.close()
        qapp.processEvents()


def test_startup_first_frame_under_300ms() -> None:
    env = dict(os.environ)
    env["PDF_LECTOR_BENCH_STARTUP"] = "1"
    env["PDF_LECTOR_BENCH_EXIT"] = "1"
    env.setdefault("QT_QPA_PLATFORM", "offscreen")

    proc = subprocess.run(
        [sys.executable, "-m", "pdf_lector.app"],
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
        check=True,
    )
    match = re.search(r"FIRST_FRAME_MS=([0-9.]+)", proc.stdout)
    assert match is not None, f"Missing FIRST_FRAME_MS in stdout: {proc.stdout} stderr={proc.stderr}"
    ms = float(match.group(1))
    assert ms < 300.0, f"First frame startup took {ms:.2f} ms (limit < 300 ms)"
