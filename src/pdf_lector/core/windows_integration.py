"""Windows 11 HKCU shell integration, Native Messaging registry, and headless CLI dispatch."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import sys
from typing import Any

from .. import APP_NAME, APP_USER_MODEL_ID, COMPANY_NAME
from .path_guard import (
    _has_control_chars,
    _has_ntfs_ads,
    is_unc_or_network_path,
    validate_local_any_doc,
    validate_local_pdf_path,
)
from .win32_bridge import atomic_write_text, default_app_data_dir

PROGID_PDF = "TRODAI.PDFMDLector.PDF"
PROGID_MD = "TRODAI.PDFMDLector.MD"
NATIVE_HOST_NAME = "com.trodai.pdf_md_lector"
DEFAULT_APPS_URI = "ms-settings:defaultapps"

_CHROME_NM_SUBKEY = rf"Software\Google\Chrome\NativeMessagingHosts\{NATIVE_HOST_NAME}"
_EDGE_NM_SUBKEY = rf"Software\Microsoft\Edge\NativeMessagingHosts\{NATIVE_HOST_NAME}"


@dataclass(frozen=True)
class WindowsIntegrationResult:
    """Outcome of a Windows HKCU registration or unregistration operation."""

    success: bool
    message: str = ""
    keys_written: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _resolve_winreg(winreg_backend: Any | None) -> Any:
    if winreg_backend is not None:
        return winreg_backend
    if os.name != "nt":
        raise RuntimeError("winreg backend is only available on Windows")
    import winreg  # lazy OS-specific import

    return winreg


def _set_reg_sz(backend: Any, hive: int, subkey: str, name: str, value: str, written: list[str]) -> None:
    key = backend.CreateKeyEx(hive, subkey, 0, backend.KEY_WRITE)
    try:
        backend.SetValueEx(key, name, 0, backend.REG_SZ, value)
    finally:
        backend.CloseKey(key)
    written.append(subkey)


def _delete_value_if_exists(backend: Any, hive: int, subkey: str, value_name: str) -> None:
    try:
        key = backend.OpenKeyEx(hive, subkey, 0, backend.KEY_ALL_ACCESS)
    except FileNotFoundError:
        return
    try:
        try:
            backend.DeleteValue(key, value_name)
        except FileNotFoundError:
            pass
    finally:
        backend.CloseKey(key)


def _delete_tree_if_exists(backend: Any, hive: int, subkey: str) -> None:
    try:
        key = backend.OpenKeyEx(hive, subkey, 0, backend.KEY_ALL_ACCESS)
    except FileNotFoundError:
        return

    children: list[str] = []
    try:
        idx = 0
        while True:
            try:
                children.append(backend.EnumKey(key, idx))
                idx += 1
            except OSError:
                break
    finally:
        backend.CloseKey(key)

    for child in children:
        _delete_tree_if_exists(backend, hive, f"{subkey}\\{child}")

    try:
        backend.DeleteKey(hive, subkey)
    except FileNotFoundError:
        pass


def register_windows_integration(
    exe_path: Path | str,
    *,
    include_markdown: bool = False,
    winreg_backend: Any | None = None,
    app_name: str = APP_NAME,
) -> WindowsIntegrationResult:
    """Register HKCU ProgIDs, Explorer context menus, and OpenWithProgids cleanly."""
    written: list[str] = []
    try:
        backend = _resolve_winreg(winreg_backend)
        hive = backend.HKEY_CURRENT_USER
        exe_str = str(exe_path).strip()
        if not exe_str or is_unc_or_network_path(exe_str):
            return WindowsIntegrationResult(
                success=False,
                message="Invalid or unsafe executable path",
                errors=["unsafe_exe_path"],
            )

        pdf_root = rf"Software\Classes\{PROGID_PDF}"
        _set_reg_sz(backend, hive, pdf_root, "", f"{app_name} PDF Document", written)
        _set_reg_sz(backend, hive, pdf_root, "AppUserModelID", APP_USER_MODEL_ID, written)
        _set_reg_sz(backend, hive, rf"{pdf_root}\DefaultIcon", "", f'"{exe_str}",0', written)

        context_verbs: tuple[tuple[str, str, str], ...] = (
            ("open", f"Abrir con {app_name}", f'"{exe_str}" "%1"'),
            ("merge", "Unir PDF (PDF/MD Lector)", f'"{exe_str}" --merge "%1"'),
            ("compress", "Comprimir PDF (PDF/MD Lector)", f'"{exe_str}" --compress "%1"'),
            ("sign", "Firmar PDF (PDF/MD Lector)", f'"{exe_str}" --sign "%1"'),
        )
        for verb, label, command in context_verbs:
            verb_key = rf"{pdf_root}\shell\{verb}"
            _set_reg_sz(backend, hive, verb_key, "", label, written)
            _set_reg_sz(backend, hive, verb_key, "Icon", f'"{exe_str}",0', written)
            _set_reg_sz(backend, hive, rf"{verb_key}\command", "", command, written)

        _set_reg_sz(
            backend,
            hive,
            r"Software\Classes\.pdf\OpenWithProgids",
            PROGID_PDF,
            "",
            written,
        )

        if include_markdown:
            md_root = rf"Software\Classes\{PROGID_MD}"
            _set_reg_sz(backend, hive, md_root, "", f"{app_name} Markdown Document", written)
            _set_reg_sz(backend, hive, md_root, "AppUserModelID", APP_USER_MODEL_ID, written)
            _set_reg_sz(backend, hive, rf"{md_root}\DefaultIcon", "", f'"{exe_str}",0', written)
            _set_reg_sz(backend, hive, rf"{md_root}\shell\open", "", f"Abrir con {app_name}", written)
            _set_reg_sz(backend, hive, rf"{md_root}\shell\open\command", "", f'"{exe_str}" "%1"', written)
            for ext in (".md", ".markdown"):
                _set_reg_sz(
                    backend,
                    hive,
                    rf"Software\Classes\{ext}\OpenWithProgids",
                    PROGID_MD,
                    "",
                    written,
                )

        return WindowsIntegrationResult(
            success=True,
            message="Windows HKCU integration registered",
            keys_written=written,
        )
    except Exception as exc:
        return WindowsIntegrationResult(
            success=False,
            message=str(exc),
            keys_written=written,
            errors=[str(exc)],
        )


def unregister_windows_integration(*, winreg_backend: Any | None = None) -> WindowsIntegrationResult:
    """Remove only TRODAI HKCU ProgIDs and OpenWithProgids entries, preserving third-party associations."""
    try:
        backend = _resolve_winreg(winreg_backend)
        hive = backend.HKEY_CURRENT_USER
        _delete_tree_if_exists(backend, hive, rf"Software\Classes\{PROGID_PDF}")
        _delete_tree_if_exists(backend, hive, rf"Software\Classes\{PROGID_MD}")
        _delete_value_if_exists(backend, hive, r"Software\Classes\.pdf\OpenWithProgids", PROGID_PDF)
        _delete_value_if_exists(backend, hive, r"Software\Classes\.md\OpenWithProgids", PROGID_MD)
        _delete_value_if_exists(backend, hive, r"Software\Classes\.markdown\OpenWithProgids", PROGID_MD)
        return WindowsIntegrationResult(success=True, message="Windows HKCU integration removed")
    except Exception as exc:
        return WindowsIntegrationResult(success=False, message=str(exc), errors=[str(exc)])


def build_native_messaging_manifest(
    host_path: Path | str,
    *,
    allowed_origins: Sequence[str] | None = None,
    name: str = NATIVE_HOST_NAME,
) -> dict[str, Any]:
    """Build Chromium/Edge Native Messaging host manifest dictionary."""
    origins = list(allowed_origins) if allowed_origins else [
        "chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/",
    ]
    return {
        "name": name,
        "description": f"{APP_NAME} by {COMPANY_NAME} Native Messaging Host",
        "path": str(host_path),
        "type": "stdio",
        "allowed_origins": origins,
    }


def write_native_messaging_manifest(
    manifest_path: Path | str,
    host_path: Path | str,
    *,
    allowed_origins: Sequence[str] | None = None,
) -> Path:
    """Write the Native Messaging manifest JSON atomically to local disk."""
    target = Path(manifest_path)
    if is_unc_or_network_path(target):
        raise ValueError("Unsafe manifest path")
    payload = build_native_messaging_manifest(host_path, allowed_origins=allowed_origins)
    atomic_write_text(target, json.dumps(payload, indent=2, ensure_ascii=False))
    return target


def register_native_messaging_host(
    manifest_path: Path | str,
    *,
    winreg_backend: Any | None = None,
    browsers: Sequence[str] = ("chrome", "edge"),
) -> WindowsIntegrationResult:
    """Register Native Messaging Host manifest path in HKCU for Chrome and Edge."""
    written: list[str] = []
    try:
        backend = _resolve_winreg(winreg_backend)
        hive = backend.HKEY_CURRENT_USER
        manifest_str = str(manifest_path).strip()
        if not manifest_str or is_unc_or_network_path(manifest_str):
            return WindowsIntegrationResult(
                success=False,
                message="Invalid or unsafe manifest path",
                errors=["unsafe_manifest_path"],
            )

        browser_map = {
            "chrome": _CHROME_NM_SUBKEY,
            "edge": _EDGE_NM_SUBKEY,
        }
        for browser in browsers:
            subkey = browser_map.get(browser.lower().strip())
            if subkey:
                _set_reg_sz(backend, hive, subkey, "", manifest_str, written)

        return WindowsIntegrationResult(
            success=True,
            message="Native Messaging host registered",
            keys_written=written,
        )
    except Exception as exc:
        return WindowsIntegrationResult(
            success=False,
            message=str(exc),
            keys_written=written,
            errors=[str(exc)],
        )


def unregister_native_messaging_host(*, winreg_backend: Any | None = None) -> WindowsIntegrationResult:
    """Unregister Native Messaging Host registry keys from HKCU."""
    try:
        backend = _resolve_winreg(winreg_backend)
        hive = backend.HKEY_CURRENT_USER
        _delete_tree_if_exists(backend, hive, _CHROME_NM_SUBKEY)
        _delete_tree_if_exists(backend, hive, _EDGE_NM_SUBKEY)
        return WindowsIntegrationResult(success=True, message="Native Messaging host unregistered")
    except Exception as exc:
        return WindowsIntegrationResult(success=False, message=str(exc), errors=[str(exc)])


def open_default_apps_settings(launcher: Callable[[str], Any] | None = None) -> bool:
    """Launch Windows 11 Default Apps settings page (injectable launcher for safe tests)."""
    if launcher is not None:
        launcher(DEFAULT_APPS_URI)
        return True
    if os.name == "nt":
        os.startfile(DEFAULT_APPS_URI)  # type: ignore[attr-defined]
        return True
    return False


def build_cli_parser() -> argparse.ArgumentParser:
    """Build unified CLI argument parser for GUI launch and headless commands."""
    parser = argparse.ArgumentParser(prog="pdf-lector", description="PDF/MD Lector by TRODAI")
    parser.add_argument("inputs", nargs="*", help="Local PDF/MD document paths to open or process")
    parser.add_argument("--no-splash", action="store_true", help="Skip startup splash screen")
    parser.add_argument("--fast-boot", action="store_true", help="Skip minimum splash polish delay")
    parser.add_argument("--screenshot-dir", type=Path, default=None, help="Capture verification PNGs and exit")

    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--merge", action="store_true", help="Headless: merge 2+ PDF files into -o output")
    mode_group.add_argument("--compress", action="store_true", help="Headless: compress a PDF file into -o output")
    mode_group.add_argument("--ocr", action="store_true", help="Headless: run OCR on a PDF into -o output")
    mode_group.add_argument("--sign", action="store_true", help="Headless: sign a PDF using PKCS#12 into -o output")
    mode_group.add_argument("--serve-api", action="store_true", help="Start lazy local REST/OpenAPI server (127.0.0.1)")
    mode_group.add_argument("--native-host", action="store_true", help="Run Chromium/Edge Native Messaging stdio host")

    parser.add_argument("-o", "--output", type=Path, default=None, help="Output file or directory path")
    parser.add_argument("--dpi", type=int, default=150, help="Output DPI (36..1200)")
    parser.add_argument("--quality", type=int, default=80, help="Output compression quality (10..100)")
    parser.add_argument("--source-lang", "--lang", dest="source_lang", default="auto", help="Source document language")
    parser.add_argument("--p12", type=Path, default=None, help="PKCS#12 (.p12) path for --sign")
    parser.add_argument("--password", default="", help="PKCS#12 passphrase for --sign")
    parser.add_argument("--host", default="127.0.0.1", help="Loopback host for --serve-api")
    parser.add_argument("--port", type=int, default=8765, help="Port for --serve-api")
    return parser


def _validate_cli_output(raw_out: Path | None) -> tuple[bool, Path | None, str]:
    if raw_out is None:
        return False, None, "missing_output"
    text = str(raw_out).strip()
    if not text or _has_control_chars(text) or _has_ntfs_ads(text) or is_unc_or_network_path(text):
        return False, None, "unsafe_output_path"
    return True, Path(text), "ok"


def _validate_cli_inputs(raw_inputs: Sequence[str], *, require_pdf: bool = True) -> tuple[bool, list[Path], str]:
    paths: list[Path] = []
    for raw in raw_inputs:
        ok, reason = (
            validate_local_pdf_path(raw, check_exists=True)
            if require_pdf
            else validate_local_any_doc(raw, check_exists=True)
        )
        if not ok:
            return False, [], f"{raw}: {reason}"
        paths.append(Path(raw).resolve())
    return True, paths, "ok"


def run_cli_command(args: argparse.Namespace) -> int | None:
    """Execute headless CLI modes lazily; return None when normal GUI boot should proceed."""
    if getattr(args, "native_host", False):
        from .native_messaging import run_stdio_loop

        return run_stdio_loop()

    if getattr(args, "serve_api", False):
        from ..api.server import LocalApiServer

        srv = LocalApiServer(host=args.host, port=int(args.port))
        srv.serve_forever()
        return 0

    is_merge = bool(getattr(args, "merge", False))
    is_compress = bool(getattr(args, "compress", False))
    is_ocr = bool(getattr(args, "ocr", False))
    is_sign = bool(getattr(args, "sign", False))
    if not (is_merge or is_compress or is_ocr or is_sign):
        return None

    raw_inputs = list(getattr(args, "inputs", []) or [])
    ok_in, in_paths, in_err = _validate_cli_inputs(raw_inputs, require_pdf=True)
    if not ok_in:
        print(f"Error: {in_err}", file=sys.stderr)
        return 2

    ok_out, out_path, out_err = _validate_cli_output(getattr(args, "output", None))
    if not ok_out or out_path is None:
        print(f"Error: {out_err}", file=sys.stderr)
        return 2

    from .tools import ToolRegistry

    if is_merge:
        if len(in_paths) < 2:
            print("Error: --merge requires at least 2 local PDF input files.", file=sys.stderr)
            return 2
        res = ToolRegistry.get("merge_pdf").run(in_paths, out_path, {})
    elif is_compress:
        if len(in_paths) != 1:
            print("Error: --compress requires exactly 1 local PDF input file.", file=sys.stderr)
            return 2
        res = ToolRegistry.get("compress_pdf").run(
            in_paths,
            out_path,
            {"dpi": int(args.dpi), "quality": int(args.quality)},
        )
    elif is_ocr:
        if len(in_paths) != 1:
            print("Error: --ocr requires exactly 1 local PDF input file.", file=sys.stderr)
            return 2
        res = ToolRegistry.get("pdf_ocr").run(
            in_paths,
            out_path,
            {"dpi": int(args.dpi), "source_lang": str(args.source_lang)},
        )
    else:
        if len(in_paths) != 1:
            print("Error: --sign requires exactly 1 local PDF input file.", file=sys.stderr)
            return 2
        p12_path = getattr(args, "p12", None)
        if p12_path is None:
            print("Error: --sign requires --p12 <path>.", file=sys.stderr)
            return 2
        res = ToolRegistry.get("sign_pdf").run(
            in_paths,
            out_path,
            {"p12_path": str(p12_path), "password": str(args.password or "")},
        )

    if res.success:
        print(res.summary)
        return 0
    print(f"Error: {res.summary}", file=sys.stderr)
    return 1


def default_native_manifest_path() -> Path:
    """Return user-local path for the Native Messaging host manifest JSON."""
    return default_app_data_dir() / f"{NATIVE_HOST_NAME}.json"
