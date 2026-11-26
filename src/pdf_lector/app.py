"""Application entrypoint with <300ms 2-phase startup, SingleInstanceLock, CrashGuard, and headless CLI."""

from __future__ import annotations

import argparse
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import tempfile
import time

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from . import APP_NAME, APP_USER_MODEL_ID, read_version
from .core.crash_guard import CrashGuard
from .core.i18n import t
from .core.single_instance import SingleInstanceLock
from .core.win32_bridge import set_app_user_model_id
from .theme.palette import build_trodai_qss, register_embedded_fonts
from .theme.theme_profile import ThemeProfile, default_profile, get_preset, get_theme_config_path
from .ui.splash_screen import SplashScreen


def _fast_load_theme_profile() -> ThemeProfile:
    """Load theme profile JSON without slow media disk stats during Phase-1 boot."""
    target = get_theme_config_path()
    if not target.exists():
        return default_profile()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return ThemeProfile.from_dict(raw, check_media_exists=False)
    except Exception:
        pass
    return default_profile()


def _generate_demo_photo_banner(target_path: Path) -> Path:
    """Create a rich synthetic architectural photo PNG for screenshot and demo testing."""
    from PIL import Image, ImageDraw

    target_path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (1280, 480), "#0A192F")
    draw = ImageDraw.Draw(img)
    for y in range(480):
        ratio = y / 479.0
        r = int(15 + 215 * (ratio ** 1.4))
        g = int(45 + 175 * ratio)
        b = int(95 + 145 * (1.0 - ratio * 0.35))
        draw.line([(0, y), (1280, y)], fill=(min(255, r), min(255, g), min(255, b)))
    for idx, x in enumerate(range(40, 1240, 110)):
        h_box = 140 + ((idx * 53) % 210)
        shade = (18 + idx * 5, 32 + idx * 4, 58 + idx * 6)
        draw.rectangle([x, 480 - h_box, x + 78, 480], fill=shade, outline="#38BDF8")
    img.save(target_path, format="PNG")
    return target_path


def _run_screenshot_capture(app: QApplication, output_dir: Path, profile: ThemeProfile) -> int:
    from .core.bookmarks_store import BookmarksStore
    from .core.dictionary_engine import DictionaryEngine
    from .core.usage_insights import LocalUsageInsights
    from .ui.main_window import MainWindow

    output_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir = Path(tempfile.mkdtemp(prefix="trodai_shots_"))

    splash = SplashScreen(mode="dark", lang="es")
    splash.show()
    splash.set_progress(68, t("splash.step.ui", "es"))
    app.processEvents()
    splash.grab().save(str(output_dir / "proto_splash.png"))
    splash.close()

    win = MainWindow(
        profile,
        persist_profile=False,
        sample_pdf_dir=tmp_dir,
        insights_store=LocalUsageInsights(tmp_dir / "insights.json"),
        bookmarks_store=BookmarksStore(tmp_dir / "bookmarks.json"),
        dictionary_engine=DictionaryEngine(tmp_dir / "dicts"),
    )
    win.ensure_sample_pdf_loaded()
    win.show()
    app.processEvents()

    mesh = get_preset("trodai_mesh", preserve_language="es")
    win.apply_profile(mesh, save=False)
    win.resize(1240, 688)
    win.switch_view(0, immediate=True)
    app.processEvents()
    win.grab().save(str(output_dir / "proto_hub_dark_1200x760.png"))

    arch_light = get_preset("arch_grid", preserve_language="es")
    win.apply_profile(arch_light, save=False)
    win.resize(980, 650)
    win.switch_view(0, immediate=True)
    app.processEvents()
    win.grab().save(str(output_dir / "proto_hub_light_980x650.png"))

    demo_photo = _generate_demo_photo_banner(tmp_dir / "demo_photo_banner.png")
    custom = replace(
        get_preset("custom_banner", preserve_language="es"),
        banner_path=str(demo_photo),
        apply_to_hub_banner=True,
        apply_to_viewer_backdrop=True,
        opacity=0.72,
        blur_radius=4,
        auto_scrim=True,
    )
    win.apply_profile(custom, save=False)
    win.resize(1240, 688)
    win.switch_view(0, immediate=True)
    app.processEvents()
    win.grab().save(str(output_dir / "proto_hub_custom_banner_dark.png"))

    win.apply_profile(mesh, save=False)
    win.viewer_view.set_dictionary_visible(True)
    win.viewer_view.dict_panel.lookup_word("privacidad", record_event=False)
    win.switch_view(1, immediate=True)
    app.processEvents()
    tab = win.viewer_view.current_tab()
    if tab is not None:
        tab.set_rail_mode("both")
        tab.add_bookmark("Revisar Clausula PDF", 0)
        tab.set_zoom(0.75)
    app.processEvents()
    time.sleep(0.25)
    app.processEvents()
    win.grab().save(str(output_dir / "proto_viewer_motion_mesh_dark.png"))

    win.apply_profile(custom, save=False)
    win.switch_view(2, immediate=True)
    app.processEvents()
    win.grab().save(str(output_dir / "proto_theme_studio_dark.png"))

    win.close()
    return 0


def _run_headless_cli(args: argparse.Namespace) -> int | None:
    """Execute headless CLI operations lazily without loading GUI or unrelated engines."""
    if args.version:
        print(f"{APP_NAME} {read_version()}")
        return 0

    if args.merge is not None:
        inputs = [Path(p) for p in args.merge]
        if len(inputs) < 2 or not args.output:
            print("error: --merge requires at least 2 input PDFs and -o/--output", file=sys.stderr)
            return 2
        from .core.tools import ToolRegistry  # noqa: PLC0415

        res = ToolRegistry.get("merge_pdf").run(inputs, Path(args.output), {})
        if not res.success:
            print(res.summary, file=sys.stderr)
            return 1
        print(res.summary)
        return 0

    if args.compress is not None:
        if not args.output:
            print("error: --compress requires -o/--output", file=sys.stderr)
            return 2
        from .core.tools import ToolRegistry  # noqa: PLC0415

        params = {"dpi": int(args.dpi), "quality": int(args.quality)}
        res = ToolRegistry.get("compress_pdf").run([Path(args.compress)], Path(args.output), params)
        if not res.success:
            print(res.summary, file=sys.stderr)
            return 1
        print(res.summary)
        return 0

    if args.ocr is not None:
        if not args.output:
            print("error: --ocr requires -o/--output", file=sys.stderr)
            return 2
        from .core.tools import ToolRegistry  # noqa: PLC0415

        params = {"dpi": int(args.dpi), "source_lang": str(args.source_lang)}
        res = ToolRegistry.get("pdf_ocr").run([Path(args.ocr)], Path(args.output), params)
        if not res.success:
            print(res.summary, file=sys.stderr)
            return 1
        print(res.summary)
        return 0

    if args.serve_api:
        try:
            from .api.server import LocalApiServer  # noqa: PLC0415
        except ImportError as exc:
            print(f"error: local API server module unavailable ({exc})", file=sys.stderr)
            return 1
        srv = LocalApiServer(host=args.api_host, port=int(args.api_port))
        srv.start()
        print(f"API listening on {srv.base_url}")
        try:
            while True:
                time.sleep(0.5)
        except KeyboardInterrupt:
            srv.stop()
        return 0

    if args.native_host:
        try:
            from .core.native_messaging import run_native_host  # noqa: PLC0415
        except ImportError as exc:
            print(f"error: native messaging module unavailable ({exc})", file=sys.stderr)
            return 1
        return int(run_native_host() or 0)

    return None


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pdf-md-lector")
    parser.add_argument("doc_path", nargs="?", help="Optional local PDF or Markdown file to open in Viewer")
    parser.add_argument("--version", action="store_true", help="Print product name and version")
    parser.add_argument("--no-splash", action="store_true", help="Skip startup splash screen")
    parser.add_argument("--fast-boot", action="store_true", help="Skip minimum splash polish delay")
    parser.add_argument("--screenshot-dir", type=Path, default=None, help="Capture verification PNGs and exit")
    parser.add_argument("--merge", nargs="+", metavar="PDF", default=None, help="Merge 2+ local PDFs headlessly")
    parser.add_argument("--compress", metavar="PDF", default=None, help="Compress a local PDF headlessly")
    parser.add_argument("--ocr", metavar="PDF", default=None, help="Run offline OCR on a local PDF headlessly")
    parser.add_argument("-o", "--output", metavar="PATH", default=None, help="Output file path for CLI commands")
    parser.add_argument("--dpi", type=int, default=150, help="Output DPI (36-1200)")
    parser.add_argument("--quality", type=int, default=75, help="Compression quality (10-100)")
    parser.add_argument("--source-lang", default="auto", help="Original document language (auto, es, en, pt, ...)")
    parser.add_argument("--serve-api", action="store_true", help="Start lazy local 127.0.0.1 REST/OpenAPI server")
    parser.add_argument("--api-host", default="127.0.0.1", help="Loopback API bind host")
    parser.add_argument("--api-port", type=int, default=0, help="Loopback API port")
    parser.add_argument("--native-host", action="store_true", help="Run Chrome/Edge Native Messaging stdio host")
    return parser


def launch_app(argv: list[str] | None = None) -> int:
    """Boot PDF/MD Lector with <300ms first frame SplashScreen, CrashGuard, and SingleInstanceLock."""
    t0 = time.perf_counter()
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    cli_rc = _run_headless_cli(args)
    if cli_rc is not None:
        return cli_rc

    bench_exit = os.environ.get("PDF_LECTOR_BENCH_EXIT") == "1"

    set_app_user_model_id(APP_USER_MODEL_ID)
    app = QApplication.instance() or QApplication(sys.argv if argv is None else ["pdf-md-lector", *argv])

    instance_lock: SingleInstanceLock | None = None
    crash_guard: CrashGuard | None = None
    if not bench_exit and args.screenshot_dir is None:
        instance_lock = SingleInstanceLock()
        if not instance_lock.acquire():
            forward_paths = [Path(args.doc_path)] if args.doc_path else []
            instance_lock.send_to_running_instance(forward_paths)
            return 0
        crash_guard = CrashGuard()
        crash_guard.install()
        crash_guard.begin_session()

    profile = _fast_load_theme_profile()

    if args.screenshot_dir is not None:
        register_embedded_fonts(app)
        app.setStyleSheet(build_trodai_qss(profile.base_mode, profile.accent_override))
        return _run_screenshot_capture(app, args.screenshot_dir, profile)

    splash: SplashScreen | None = None
    if not args.no_splash:
        splash = SplashScreen(
            mode=profile.base_mode,
            lang=profile.language,
            accent_override=profile.accent_override,
        )
        splash.show()
        init_msg = {
            "es": "Iniciando PDF/MD Lector...",
            "en": "Starting PDF/MD Lector...",
            "pt": "Iniciando PDF/MD Lector...",
        }.get(profile.language, "Iniciando PDF/MD Lector...")
        splash.set_progress(5, init_msg)

    first_frame_ms = (time.perf_counter() - t0) * 1000.0
    if os.environ.get("PDF_LECTOR_BENCH_STARTUP") == "1":
        print(f"FIRST_FRAME_MS={first_frame_ms:.2f}")
    if bench_exit:
        if splash is not None:
            splash.close()
        return 0

    state: dict[str, object] = {}

    def _step_fonts() -> None:
        if splash is not None:
            splash.set_progress(15, t("splash.step.fonts", profile.language))
        register_embedded_fonts(app)
        if splash is not None:
            splash.set_progress(32, t("splash.step.theme", profile.language))

    def _step_theme() -> None:
        app.setStyleSheet(build_trodai_qss(profile.base_mode, profile.accent_override))
        if splash is not None:
            splash.set_progress(48, t("splash.step.theme", profile.language))

    def _step_window() -> None:
        from .ui.dialog_localizer import DialogLocalizer  # noqa: PLC0415
        from .ui.main_window import MainWindow  # noqa: PLC0415

        if splash is not None:
            splash.set_progress(65, t("splash.step.ui", profile.language))
        localizer = DialogLocalizer(app, lang=profile.language, mode=profile.base_mode)
        win = MainWindow(profile, persist_profile=True, crash_guard=crash_guard)
        win._dialog_localizer = localizer  # type: ignore[attr-defined]
        state["win"] = win

        if instance_lock is not None:
            def _on_forwarded_paths(paths: list[Path]) -> None:
                for p in paths:
                    win.open_document_path(p, switch_to_viewer=True)
                win.showNormal()
                win.raise_()
                win.activateWindow()

            instance_lock.start_server(_on_forwarded_paths)

        if crash_guard is not None:
            crash_guard.install_qt_message_handler()

    def _step_document() -> None:
        from .ui.main_window import MainWindow  # noqa: PLC0415

        win = state["win"]
        assert isinstance(win, MainWindow)
        if splash is not None:
            splash.set_progress(82, t("splash.step.viewer", profile.language))
        crashed = bool(crash_guard is not None and crash_guard.previous_report.previous_crashed)
        recovered_count = 0
        if not args.doc_path:
            recovered_count = win.recover_previous_session(previous_crashed=crashed)
        if args.doc_path:
            opened = win.open_document_path(Path(args.doc_path), switch_to_viewer=True)
            if not opened and recovered_count == 0:
                win.ensure_sample_pdf_loaded()
        elif recovered_count == 0:
            win.ensure_sample_pdf_loaded()
        if splash is not None:
            splash.set_progress(95, t("splash.step.ready", profile.language))

    def _step_finish() -> None:
        from .ui.main_window import MainWindow  # noqa: PLC0415

        win = state["win"]
        assert isinstance(win, MainWindow)
        if splash is None:
            win.show()
        else:
            splash.finish_and_close(win)
        if QApplication.platformName() != "offscreen":
            QTimer.singleShot(1500, win.start_background_update_check)

    def _on_about_to_quit() -> None:
        if crash_guard is not None:
            crash_guard.mark_clean_exit()
            crash_guard.uninstall()
        if instance_lock is not None:
            instance_lock.release()

    app.aboutToQuit.connect(_on_about_to_quit)

    if splash is None or args.fast_boot:
        _step_fonts()
        _step_theme()
        _step_window()
        _step_document()
        _step_finish()
    else:
        steps = [_step_fonts, _step_theme, _step_window, _step_document, _step_finish]

        def _run_next(idx: int = 0) -> None:
            if idx >= len(steps):
                return
            steps[idx]()
            if idx + 1 < len(steps):
                QTimer.singleShot(45, lambda: _run_next(idx + 1))

        QTimer.singleShot(35, lambda: _run_next(0))

    return app.exec()


def main() -> None:
    raise SystemExit(launch_app())


if __name__ == "__main__":
    main()
