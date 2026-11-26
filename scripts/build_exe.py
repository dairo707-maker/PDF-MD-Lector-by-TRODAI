"""Build script for PDF-MD-Lector-Portable.exe, PDF-MD-Lector-Setup.exe (Inno Setup 6), and SHA256SUMS.txt."""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
DIST = ROOT / "dist"
BUILD = ROOT / "build"

PORTABLE_EXE_NAME = "PDF-MD-Lector-Portable"
APP_BUNDLE_NAME = "PDF-MD-Lector"
SETUP_EXE_NAME = "PDF-MD-Lector-Setup"

# Qt modules unused by PDF/MD Lector (Markdown uses nh3 + QTextBrowser instead of QtWebEngine).
# Note: PySide6.QtMultimedia is intentionally KEPT for QCamera document capture.
QT_EXCLUDES: tuple[str, ...] = (
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick",
    "PySide6.QtPdf",
    "PySide6.QtPdfWidgets",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DRender",
    "PySide6.QtQuick",
    "PySide6.QtQml",
)

HIDDEN_SUBMODULES: tuple[str, ...] = (
    "pdf_lector.core.tools.organize",
    "pdf_lector.core.tools.optimize",
    "pdf_lector.core.tools.security",
    "pdf_lector.core.tools.compare",
    "pdf_lector.core.tools.convert",
    "pdf_lector.core.tools.create",
    "pdf_lector.core.tools.invoices",
    "pdf_lector.ui.tool_workspace",
    "pdf_lector.ui.ai_panel",
    "pdf_lector.ui.invoice_dialog",
    "pdf_lector.ui.preferences_dialog",
    "pdf_lector.ui.notifications_drawer",
    "pdf_lector.ui.diagnostics_dialog",
    "pdf_lector.ui.support_dialog",
    "pdf_lector.ui.tour_dialog",
    "pdf_lector.ui.about_updater_dialog",
    "pdf_lector.ui.print_dialog",
    "pdf_lector.ui.presentation_view",
    "pdf_lector.ui.shortcuts_dialog",
)


def read_version() -> str:
    """Read application version from VERSION file, falling back to 0.1.0."""
    vf = ROOT / "VERSION"
    if vf.exists():
        text = vf.read_text(encoding="utf-8").strip()
        if text:
            return text
    return "0.1.0"


def clean_child_env(base_env: dict[str, str] | None = None) -> dict[str, str]:
    """Return a subprocess environment stripped of PyInstaller _MEIPASS* and PYTHONHOME variables."""
    env = dict(os.environ if base_env is None else base_env)
    for key in list(env.keys()):
        if key.startswith("_MEIPASS") or key == "PYTHONHOME":
            env.pop(key, None)
    return env


def find_iscc() -> Path | None:
    """Locate Inno Setup 6 compiler (ISCC.exe) across standard Windows install paths."""
    in_path = shutil.which("iscc") or shutil.which("ISCC.exe")
    if in_path:
        return Path(in_path)

    candidates = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Inno Setup 6" / "ISCC.exe",
        Path(r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"),
        Path(r"C:\Program Files\Inno Setup 6\ISCC.exe"),
    ]
    for cand in candidates:
        if cand.exists():
            return cand
    return None


def _draw_codex_shield_emblem(
    draw: ImageDraw.ImageDraw,
    x0: int,
    y0: int,
    size: int,
) -> None:
    """Draw a polygonal codex-shield emblem matching assets/icons/pdf_lector_logo.svg."""

    def _pt(nx: float, ny: float) -> tuple[float, float]:
        return (x0 + nx * (size - 1) / 100.0, y0 + ny * (size - 1) / 100.0)

    shield_outer = [_pt(50, 5), _pt(89, 18), _pt(89, 64), _pt(50, 95), _pt(11, 64), _pt(11, 18)]
    draw.polygon(shield_outer, fill=(15, 23, 42, 255), outline=(71, 85, 105, 255))

    shield_inner = [_pt(50, 11), _pt(83, 22), _pt(83, 61), _pt(50, 88), _pt(17, 61), _pt(17, 22)]
    draw.polygon(shield_inner, fill=(30, 41, 59, 255), outline=(51, 65, 85, 255))

    codex = [_pt(27, 28), _pt(73, 28), _pt(73, 70), _pt(50, 76), _pt(27, 70)]
    draw.polygon(codex, fill=(248, 250, 252, 255), outline=(203, 213, 225, 255))

    line_w = max(1, round(size * 0.028))
    for ly in (36, 43, 50, 57):
        draw.line([_pt(33, ly), _pt(43, ly)], fill=(51, 65, 85, 255), width=line_w)
        draw.line([_pt(57, ly), _pt(67, ly)], fill=(51, 65, 85, 255), width=line_w)

    ribbon = [_pt(45, 27), _pt(55, 27), _pt(55, 81), _pt(50, 75), _pt(45, 81)]
    draw.polygon(ribbon, fill=(220, 38, 38, 255), outline=(153, 27, 27, 255))

    clasp = [_pt(39, 65), _pt(61, 65), _pt(57, 72), _pt(43, 72)]
    draw.polygon(clasp, fill=(15, 23, 42, 255), outline=(226, 232, 240, 255))


def ensure_app_icon() -> Path:
    """Return existing assets/icons/pdf_lector.ico or generate fallback multi-size .ico."""
    ico_path = ASSETS / "icons" / "pdf_lector.ico"
    if ico_path.exists() and ico_path.stat().st_size > 0:
        return ico_path

    ico_path.parent.mkdir(parents=True, exist_ok=True)
    sizes = [256, 128, 64, 48, 32, 16]
    images: list[Image.Image] = []
    for sz in sizes:
        img = Image.new("RGBA", (sz, sz), (0, 0, 0, 0))
        _draw_codex_shield_emblem(ImageDraw.Draw(img), 0, 0, sz)
        images.append(img)

    images[0].save(
        ico_path,
        format="ICO",
        sizes=[(s, s) for s in sizes],
        append_images=images[1:],
    )
    return ico_path


def _render_svg_logo_rgba(size: int) -> Image.Image:
    """Render assets/icons/pdf_lector_logo.svg with 4x supersampled anti-aliasing (or Pillow fallback)."""
    svg_path = ASSETS / "icons" / "pdf_lector_logo.svg"
    if svg_path.exists():
        try:
            from PySide6.QtCore import Qt
            from PySide6.QtGui import QGuiApplication, QImage, QPainter
            from PySide6.QtSvg import QSvgRenderer

            created_app = False
            if QGuiApplication.instance() is None:
                os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
                QGuiApplication([])
                created_app = True
            del created_app

            hi = max(64, size * 4)
            qimg = QImage(hi, hi, QImage.Format.Format_RGBA8888)
            qimg.fill(Qt.GlobalColor.transparent)
            renderer = QSvgRenderer(str(svg_path))
            if renderer.isValid():
                painter = QPainter(qimg)
                painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
                renderer.render(painter)
                painter.end()
                raw_bytes = bytes(qimg.constBits()[: hi * hi * 4])
                pil_hi = Image.frombytes("RGBA", (hi, hi), raw_bytes, "raw", "RGBA")
                return pil_hi.resize((size, size), Image.Resampling.LANCZOS)
        except Exception:
            pass

    hi = max(64, size * 4)
    emblem_hi = Image.new("RGBA", (hi, hi), (0, 0, 0, 0))
    _draw_codex_shield_emblem(ImageDraw.Draw(emblem_hi), 0, 0, hi)
    return emblem_hi.resize((size, size), Image.Resampling.LANCZOS)


def _render_wizard_pair(target_dir: Path, scale: float, suffix: str) -> tuple[Path, Path]:
    w_lg = int(round(164 * scale))
    h_lg = int(round(314 * scale))
    large_path = target_dir / f"wizard_large{suffix}.bmp"
    large = Image.new("RGB", (w_lg, h_lg), (10, 15, 29))
    ld = ImageDraw.Draw(large)
    step = max(14, int(round(28 * scale)))
    for gy in range(step, h_lg, step):
        ld.line([(0, gy), (w_lg - 1, gy)], fill=(19, 30, 54), width=1)
    for gx in range(step, w_lg, step):
        ld.line([(gx, 0), (gx, h_lg - 1)], fill=(19, 30, 54), width=1)
    ld.rectangle([0, 0, w_lg - 1, h_lg - 1], outline=(51, 65, 95), width=max(1, int(round(scale))))

    logo_lg_sz = int(round(88 * scale))
    emblem_lg = _render_svg_logo_rgba(logo_lg_sz)
    large.paste(emblem_lg, (int(round(38 * scale)), int(round(72 * scale))), emblem_lg)

    bar_x0 = int(round(38 * scale))
    ld.rectangle(
        [bar_x0, int(round(178 * scale)), int(round(125 * scale)), int(round(181 * scale))],
        fill=(220, 38, 38),
    )
    ld.rectangle(
        [bar_x0, int(round(188 * scale)), int(round(98 * scale)), int(round(190 * scale))],
        fill=(59, 130, 246),
    )
    large.save(large_path, format="BMP")

    w_sm = int(round(55 * scale))
    h_sm = int(round(58 * scale))
    small_path = target_dir / f"wizard_small{suffix}.bmp"
    small = Image.new("RGB", (w_sm, h_sm), (10, 15, 29))
    sd = ImageDraw.Draw(small)
    logo_sm_sz = int(round(42 * scale))
    emblem_sm = _render_svg_logo_rgba(logo_sm_sz)
    small.paste(emblem_sm, (int(round(6 * scale)), int(round(8 * scale))), emblem_sm)
    sd.rectangle([0, 0, w_sm - 1, h_sm - 1], outline=(59, 130, 246), width=max(1, int(round(scale))))
    small.save(small_path, format="BMP")

    return large_path, small_path


def generate_installer_wizard_images(output_dir: Path | None = None) -> tuple[Path, Path]:
    """Generate anti-aliased HiDPI (100%, 150%, 200%) TRODAI BMP banners for Inno Setup."""
    target_dir = output_dir if output_dir is not None else (ASSETS / "icons")
    target_dir.mkdir(parents=True, exist_ok=True)
    large_path, small_path = _render_wizard_pair(target_dir, 1.0, "")
    _render_wizard_pair(target_dir, 1.5, "_150")
    _render_wizard_pair(target_dir, 2.0, "_200")
    return large_path, small_path


def find_signtool() -> Path | None:
    """Locate Windows SDK signtool.exe if Authenticode signing via PFX/SHA1 is configured."""
    found = shutil.which("signtool") or shutil.which("signtool.exe")
    if found:
        return Path(found)
    kits_root = Path(r"C:\Program Files (x86)\Windows Kits\10\bin")
    if kits_root.exists():
        for exe in sorted(kits_root.rglob("x64/signtool.exe"), reverse=True):
            return exe
    return None


def maybe_sign_binary(binary: Path) -> None:
    """Optional Authenticode signing hook (PFX/SHA1 via signtool.exe, or local self-signed via sign_local.ps1)."""
    if not binary.exists():
        return

    pfx = os.environ.get("PDF_LECTOR_SIGN_PFX", "").strip()
    sha1 = os.environ.get("PDF_LECTOR_SIGN_SHA1", "").strip()
    if pfx or sha1:
        signtool = find_signtool()
        if signtool is None:
            print(f"Warning: signing configured but signtool.exe not found; {binary.name} left unsigned.")
            return
        cmd = [
            str(signtool),
            "sign",
            "/fd",
            "SHA256",
            "/tr",
            os.environ.get("PDF_LECTOR_SIGN_TSA", "http://timestamp.digicert.com"),
            "/td",
            "SHA256",
        ]
        if pfx:
            cmd.extend(["/f", pfx])
            pwd = os.environ.get("PDF_LECTOR_SIGN_PASS", "").strip()
            if pwd:
                cmd.extend(["/p", pwd])
        else:
            cmd.extend(["/sha1", sha1])
        cmd.append(str(binary))
        res = subprocess.run(cmd, cwd=str(ROOT), env=clean_child_env())
        if res.returncode != 0:
            raise RuntimeError(f"signtool failed for {binary.name} (exit {res.returncode})")
        print(f"Authenticode signed OK: {binary.name}")
        return

    if os.environ.get("PDF_LECTOR_SIGN_LOCAL", "").strip() in ("1", "true", "yes"):
        ps1 = ROOT / "scripts" / "sign_local.ps1"
        if ps1.exists():
            res = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(ps1),
                    "-TargetPath",
                    str(binary),
                ],
                cwd=str(ROOT),
                env=clean_child_env(),
            )
            if res.returncode != 0:
                raise RuntimeError(f"sign_local.ps1 failed for {binary.name} (exit {res.returncode})")
            print(f"Local self-signed Authenticode OK: {binary.name}")


def build_pyinstaller_args(
    entry_script: Path,
    ico_path: Path,
    work_dir: Path,
    dist_dir: Path = DIST,
    *,
    bundle_mode: str = "onefile",
    app_name: str | None = None,
) -> list[str]:
    """Build deterministic PyInstaller CLI argument list (--onefile portable or --onedir installed app)."""
    mode_flag = "--onedir" if bundle_mode == "onedir" else "--onefile"
    name = app_name or (APP_BUNDLE_NAME if bundle_mode == "onedir" else PORTABLE_EXE_NAME)
    args = [
        mode_flag,
        "--noconsole",
        "--windowed",
        "--name",
        name,
        "--icon",
        str(ico_path),
        "--distpath",
        str(dist_dir),
        "--workpath",
        str(work_dir),
        "--specpath",
        str(work_dir),
        "--paths",
        str(ROOT / "src"),
        "--collect-submodules",
        "pdf_lector",
        "--add-data",
        f"{ROOT / 'VERSION'};.",
        "--add-data",
        f"{ASSETS / 'fonts'};assets/fonts",
        "--add-data",
        f"{ASSETS / 'icons'};assets/icons",
    ]
    for hidden_mod in HIDDEN_SUBMODULES:
        args.extend(["--hidden-import", hidden_mod])
    browser_ext = ROOT / "browser-extension"
    if browser_ext.is_dir():
        args.extend(["--add-data", f"{browser_ext};browser-extension"])

    for mod in QT_EXCLUDES:
        args.extend(["--exclude-module", mod])

    args.append(str(entry_script))
    return args


def run_pyinstaller(args: list[str]) -> None:
    """Execute PyInstaller with clean child environment and Python SSL DLL precedence."""
    env = clean_child_env()
    py_dlls = str(Path(sys.base_prefix) / "DLLs")
    py_base = str(Path(sys.base_prefix))
    env["PATH"] = f"{py_dlls};{py_base};{env.get('PATH', '')}"

    extra_ssl: list[str] = []
    for dll_name in ("libssl-3-x64.dll", "libcrypto-3-x64.dll"):
        dll_path = Path(py_dlls) / dll_name
        if dll_path.exists():
            extra_ssl.extend(["--add-binary", f"{dll_path};."])

    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", *extra_ssl, *args]
    res = subprocess.run(cmd, cwd=str(ROOT), env=env)
    if res.returncode != 0:
        raise RuntimeError(f"PyInstaller failed with exit code {res.returncode}")


def write_sha256_manifest(files: list[Path], output_path: Path | None = None) -> Path:
    """Write deterministic sorted SHA256SUMS.txt (64-hex + two spaces + filename, LF line endings)."""
    target = output_path if output_path is not None else (DIST / "SHA256SUMS.txt")
    target.parent.mkdir(parents=True, exist_ok=True)
    existing = sorted((f for f in files if f.exists() and f.is_file()), key=lambda p: p.name)
    lines: list[str] = []
    for f in existing:
        digest = hashlib.sha256(f.read_bytes()).hexdigest().lower()
        lines.append(f"{digest}  {f.name}")
    content = ("\n".join(lines) + "\n") if lines else ""
    target.write_bytes(content.encode("utf-8"))
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build PDF/MD Lector Portable, Setup installer, and SHA256SUMS.txt")
    parser.add_argument("--portable-only", action="store_true", help="Skip Inno Setup installer compilation")
    parser.add_argument("--setup-only", action="store_true", help="Skip rebuilding standalone Portable.exe")
    parser.add_argument("--dry-run", action="store_true", help="Print planned build commands without compiling")
    args = parser.parse_args(argv)

    version = read_version()
    ico_path = ensure_app_icon()

    work_dir = Path(tempfile.gettempdir()) / f"pdf_lector_pyi_{os.getpid()}"
    entry_script = work_dir / "pyinstaller_entry.py"

    stage_dist = work_dir / "stage_dist"
    pyi_portable_args = build_pyinstaller_args(entry_script, ico_path, work_dir, dist_dir=DIST, bundle_mode="onefile")
    pyi_onedir_args = build_pyinstaller_args(
        entry_script, ico_path, work_dir, dist_dir=stage_dist, bundle_mode="onedir", app_name=APP_BUNDLE_NAME
    )
    portable_exe = DIST / f"{PORTABLE_EXE_NAME}.exe"
    onedir_dir = DIST / APP_BUNDLE_NAME
    onedir_exe = onedir_dir / f"{APP_BUNDLE_NAME}.exe"
    setup_exe = DIST / f"{SETUP_EXE_NAME}.exe"
    iscc = find_iscc()

    if args.dry_run:
        print(f"Version: {version}")
        print(f"Icon: {ico_path}")
        print(f"PyInstaller args (portable): {' '.join(pyi_portable_args)}")
        print(f"PyInstaller args (onedir): {' '.join(pyi_onedir_args)}")
        print(f"Inno Setup compiler: {iscc if iscc is not None else 'NOT FOUND'}")
        return 0

    DIST.mkdir(parents=True, exist_ok=True)
    BUILD.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    stage_dist.mkdir(parents=True, exist_ok=True)
    generate_installer_wizard_images()
    entry_script.write_text(
        "from pdf_lector.app import main\nif __name__ == '__main__':\n    raise SystemExit(main())\n",
        encoding="utf-8",
    )

    artifacts: list[Path] = []
    if not args.setup_only:
        run_pyinstaller(pyi_portable_args)
        maybe_sign_binary(portable_exe)
    if portable_exe.exists():
        artifacts.append(portable_exe)

    if not args.portable_only:
        run_pyinstaller(pyi_onedir_args)
        staged_bundle = stage_dist / APP_BUNDLE_NAME
        if staged_bundle.is_dir():
            shutil.rmtree(onedir_dir, ignore_errors=True)
            shutil.copytree(staged_bundle, onedir_dir, dirs_exist_ok=True)
        maybe_sign_binary(onedir_exe)
        if iscc is not None:
            iss_file = ROOT / "packaging" / "installer.iss"
            res = subprocess.run(
                [str(iscc), f"/DMyAppVersion={version}", str(iss_file)],
                cwd=str(ROOT),
                env=clean_child_env(),
            )
            if res.returncode != 0:
                raise RuntimeError(f"ISCC failed with exit code {res.returncode}")
            maybe_sign_binary(setup_exe)
            if setup_exe.exists():
                artifacts.append(setup_exe)
        else:
            print("Warning: Inno Setup 6 (ISCC.exe) not found; skipping PDF-MD-Lector-Setup.exe.")

    sums_path = write_sha256_manifest(artifacts, DIST / "SHA256SUMS.txt")
    print(f"Build complete (v{version}):")
    for art in artifacts:
        print(f"  {art}")
    print(f"  {sums_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
