"""Native Windows 11 Win32 bridge via ctypes for DWM Acrylic, DPAPI, process, and file guards."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import stat
import threading
import time
import uuid

IS_WINDOWS = os.name == "nt"

# DWM Window Attributes (Windows 11)
DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWA_SYSTEMBACKDROP_TYPE = 38

DWMWCP_DONOTROUND = 1
DWMSBT_NONE = 1
DWMSBT_TRANSIENTWINDOW = 3  # Acrylic

# SystemParametersInfo
SPI_GETCLIENTAREAANIMATION = 0x1042

# Drive types
DRIVE_REMOTE = 4

# File attributes & cloud-only / OneDrive Files On-Demand masks
INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF
FILE_ATTRIBUTE_READONLY = 0x00000001
FILE_ATTRIBUTE_SYSTEM = 0x00000004
FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
FILE_ATTRIBUTE_OFFLINE = 0x00001000
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x00040000
FILE_ATTRIBUTE_UNPINNED = 0x00100000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x00400000

CLOUD_ONLY_MASK = (
    FILE_ATTRIBUTE_OFFLINE
    | FILE_ATTRIBUTE_RECALL_ON_OPEN
    | FILE_ATTRIBUTE_UNPINNED
    | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
)

# DPAPI & Process query constants
CRYPTPROTECT_UI_FORBIDDEN = 0x01
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259
ERROR_ACCESS_DENIED = 5

# Foreground activation & ShowWindow constants
ASFW_ANY = 0xFFFFFFFF
SW_SHOW = 5
SW_RESTORE = 9
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_SHOWWINDOW = 0x0040


class _SYSTEM_POWER_STATUS(ctypes.Structure):
    _fields_ = [
        ("ACLineStatus", ctypes.c_ubyte),
        ("BatteryFlag", ctypes.c_ubyte),
        ("BatteryLifePercent", ctypes.c_ubyte),
        ("SystemStatusFlag", ctypes.c_ubyte),
        ("BatteryLifeTime", wintypes.DWORD),
        ("BatteryFullLifeTime", wintypes.DWORD),
    ]


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]


_DPAPI_LOCK = threading.Lock()
_DPAPI_CONFIGURED = False
_CRYPT32 = None
_KERNEL32_ERR = None


def _ensure_dpapi() -> tuple[object, object]:
    global _DPAPI_CONFIGURED, _CRYPT32, _KERNEL32_ERR
    if _DPAPI_CONFIGURED:
        return _CRYPT32, _KERNEL32_ERR
    with _DPAPI_LOCK:
        if not _DPAPI_CONFIGURED:
            crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

            crypt32.CryptProtectData.argtypes = [
                ctypes.POINTER(_DATA_BLOB),
                wintypes.LPCWSTR,
                ctypes.POINTER(_DATA_BLOB),
                ctypes.c_void_p,
                ctypes.c_void_p,
                wintypes.DWORD,
                ctypes.POINTER(_DATA_BLOB),
            ]
            crypt32.CryptProtectData.restype = wintypes.BOOL

            crypt32.CryptUnprotectData.argtypes = [
                ctypes.POINTER(_DATA_BLOB),
                ctypes.POINTER(wintypes.LPWSTR),
                ctypes.POINTER(_DATA_BLOB),
                ctypes.c_void_p,
                ctypes.c_void_p,
                wintypes.DWORD,
                ctypes.POINTER(_DATA_BLOB),
            ]
            crypt32.CryptUnprotectData.restype = wintypes.BOOL

            kernel32.LocalFree.argtypes = [ctypes.c_void_p]
            kernel32.LocalFree.restype = ctypes.c_void_p

            _CRYPT32 = crypt32
            _KERNEL32_ERR = kernel32
            _DPAPI_CONFIGURED = True
    return _CRYPT32, _KERNEL32_ERR


def default_app_data_dir() -> Path:
    """Resolve %LOCALAPPDATA%\\TRODAI\\PDFLector dynamically so env overrides in tests work."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    base = Path(local_app_data) if local_app_data else Path.home() / ".config"
    return base / "TRODAI" / "PDFLector"


def _strip_win_extended_prefix(path_str: str) -> str:
    s = path_str.replace("/", "\\") if IS_WINDOWS else path_str
    if len(s) >= 8 and s[:4] == "\\\\?\\" and s[4:8].upper() == "UNC\\":
        return "\\\\" + s[8:]
    if len(s) >= 6 and s[:4] == "\\\\?\\" and s[5] == ":":
        return s[4:]
    return s


def canonical_lock_key(path: str | Path) -> str:
    """Return a stable canonical lock key immune to Windows \\\\?\\ prefix races."""
    pre = _strip_win_extended_prefix(str(path))
    try:
        resolved = str(Path(pre).resolve(strict=False))
    except OSError:
        resolved = pre
    cleaned = _strip_win_extended_prefix(resolved)
    return os.path.normcase(os.path.normpath(os.path.abspath(cleaned)))


def atomic_write_text(path: Path, text: str, retries: int = 5) -> None:
    """Atomically write UTF-8 text via unique staging file and AV-lock retry backoff."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.stem}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        last_err: OSError | None = None
        for attempt in range(max(1, retries)):
            try:
                tmp.replace(path)
                return
            except PermissionError as exc:
                last_err = exc
                time.sleep(0.06 * (attempt + 1))
        if last_err is not None:
            raise last_err
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def dpapi_protect(plaintext: bytes) -> bytes:
    """Encrypt bytes using Windows DPAPI (CryptProtectData, current user scope)."""
    if not isinstance(plaintext, (bytes, bytearray)):
        raise TypeError("plaintext must be bytes or bytearray")
    data = bytes(plaintext)
    if not IS_WINDOWS:
        return data

    crypt32, kernel32 = _ensure_dpapi()
    if data:
        buf = (ctypes.c_byte * len(data)).from_buffer_copy(data)
        in_blob = _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte)))
    else:
        empty_buf = (ctypes.c_byte * 1)()
        in_blob = _DATA_BLOB(0, ctypes.cast(empty_buf, ctypes.POINTER(ctypes.c_byte)))

    out_blob = _DATA_BLOB()
    ctypes.set_last_error(0)
    ok = crypt32.CryptProtectData(
        ctypes.byref(in_blob),
        "TRODAI-PDFLector",
        None,
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),
    )
    if not ok:
        err = ctypes.get_last_error()
        raise OSError(err, f"DPAPI CryptProtectData failed (winerror={err})")

    try:
        if out_blob.cbData == 0 or not out_blob.pbData:
            return b""
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        if out_blob.pbData:
            kernel32.LocalFree(ctypes.cast(out_blob.pbData, ctypes.c_void_p))


def dpapi_unprotect(ciphertext: bytes) -> bytes:
    """Decrypt bytes previously protected with Windows DPAPI (CryptUnprotectData)."""
    if not isinstance(ciphertext, (bytes, bytearray)):
        raise TypeError("ciphertext must be bytes or bytearray")
    data = bytes(ciphertext)
    if not data:
        raise ValueError("Ciphertext cannot be empty")
    if not IS_WINDOWS:
        return data

    crypt32, kernel32 = _ensure_dpapi()
    buf = (ctypes.c_byte * len(data)).from_buffer_copy(data)
    in_blob = _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte)))
    out_blob = _DATA_BLOB()

    ctypes.set_last_error(0)
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        None,
        None,
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),
    )
    if not ok:
        err = ctypes.get_last_error()
        raise OSError(err, f"DPAPI CryptUnprotectData failed (winerror={err})")

    try:
        if out_blob.cbData == 0 or not out_blob.pbData:
            return b""
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        if out_blob.pbData:
            kernel32.LocalFree(ctypes.cast(out_blob.pbData, ctypes.c_void_p))


def apply_trodai_window_chrome(hwnd: int, *, dark_mode: bool = True, acrylic: bool = True) -> bool:
    """Apply strict 0px sharp corners (DWMWCP_DONOTROUND) and DWM Acrylic backdrop on Windows 11."""
    if not IS_WINDOWS or not hwnd:
        return False
    try:
        dwmapi = ctypes.windll.dwmapi
        hwnd_val = wintypes.HWND(hwnd)

        dark_val = ctypes.c_int(1 if dark_mode else 0)
        dwmapi.DwmSetWindowAttribute(
            hwnd_val, DWMWA_USE_IMMERSIVE_DARK_MODE, ctypes.byref(dark_val), ctypes.sizeof(dark_val)
        )

        corner_val = ctypes.c_int(DWMWCP_DONOTROUND)
        dwmapi.DwmSetWindowAttribute(
            hwnd_val, DWMWA_WINDOW_CORNER_PREFERENCE, ctypes.byref(corner_val), ctypes.sizeof(corner_val)
        )

        backdrop_val = ctypes.c_int(DWMSBT_TRANSIENTWINDOW if acrylic else DWMSBT_NONE)
        dwmapi.DwmSetWindowAttribute(
            hwnd_val, DWMWA_SYSTEMBACKDROP_TYPE, ctypes.byref(backdrop_val), ctypes.sizeof(backdrop_val)
        )
        return True
    except Exception:
        return False


def allow_set_foreground_window(pid: int = ASFW_ANY) -> bool:
    """Permit another process (e.g. already-running primary instance) to claim foreground focus."""
    if not IS_WINDOWS:
        return False
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.AllowSetForegroundWindow.argtypes = [wintypes.DWORD]
        user32.AllowSetForegroundWindow.restype = wintypes.BOOL
        return bool(user32.AllowSetForegroundWindow(wintypes.DWORD(int(pid))))
    except Exception:
        return False


def bring_window_to_foreground(hwnd: int) -> bool:
    """Restore (if minimized) and reliably bring hwnd to the Windows 11 foreground."""
    if not IS_WINDOWS or not hwnd:
        return False
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        user32.IsIconic.argtypes = [wintypes.HWND]
        user32.IsIconic.restype = wintypes.BOOL
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.ShowWindow.restype = wintypes.BOOL
        user32.GetForegroundWindow.argtypes = []
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
        user32.AttachThreadInput.restype = wintypes.BOOL
        user32.BringWindowToTop.argtypes = [wintypes.HWND]
        user32.BringWindowToTop.restype = wintypes.BOOL
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.SetForegroundWindow.restype = wintypes.BOOL
        user32.SetWindowPos.argtypes = [
            wintypes.HWND,
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint,
        ]
        user32.SetWindowPos.restype = wintypes.BOOL
        kernel32.GetCurrentThreadId.argtypes = []
        kernel32.GetCurrentThreadId.restype = wintypes.DWORD

        target = wintypes.HWND(hwnd)
        if user32.IsIconic(target):
            user32.ShowWindow(target, SW_RESTORE)
        else:
            user32.ShowWindow(target, SW_SHOW)

        fg_hwnd = user32.GetForegroundWindow()
        fg_tid = user32.GetWindowThreadProcessId(fg_hwnd, None) if fg_hwnd else 0
        cur_tid = kernel32.GetCurrentThreadId()
        attached = False
        if fg_tid and fg_tid != cur_tid:
            attached = bool(user32.AttachThreadInput(wintypes.DWORD(fg_tid), wintypes.DWORD(cur_tid), True))
        try:
            user32.BringWindowToTop(target)
            user32.SetForegroundWindow(target)
        finally:
            if attached:
                user32.AttachThreadInput(wintypes.DWORD(fg_tid), wintypes.DWORD(cur_tid), False)

        if user32.GetForegroundWindow() != target:
            flags = SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW
            user32.SetWindowPos(target, wintypes.HWND(HWND_TOPMOST), 0, 0, 0, 0, flags)
            user32.SetWindowPos(target, wintypes.HWND(HWND_NOTOPMOST), 0, 0, 0, 0, flags)
            user32.BringWindowToTop(target)
            user32.SetForegroundWindow(target)
        return True
    except Exception:
        return False


def set_app_user_model_id(app_id: str) -> None:
    """Register AppUserModelID so Windows 11 taskbar groups the app and icon properly."""
    if not IS_WINDOWS:
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(str(app_id))
    except Exception:
        pass


def windows_animations_enabled() -> bool:
    """Return False when user disabled OS animations in Windows Accessibility/Visual Effects."""
    if not IS_WINDOWS:
        return True
    try:
        enabled = wintypes.BOOL(True)
        ok = ctypes.windll.user32.SystemParametersInfoW(
            SPI_GETCLIENTAREAANIMATION, 0, ctypes.byref(enabled), 0
        )
        return bool(enabled.value) if ok else True
    except Exception:
        return True


def is_battery_saver_active() -> bool:
    """Return True when Windows 11 Battery Saver (SystemStatusFlag bit 0) is active."""
    if not IS_WINDOWS:
        return False
    try:
        status = _SYSTEM_POWER_STATUS()
        ok = ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status))
        if not ok:
            return False
        return bool(status.SystemStatusFlag & 0x01)
    except Exception:
        return False


def is_cloud_only_file(path: Path) -> bool:
    """Return True when a file carries OneDrive cloud-only attributes (avoid hydrating silently)."""
    if not IS_WINDOWS:
        return False
    try:
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        if attrs == INVALID_FILE_ATTRIBUTES:
            return False
        return bool(attrs & CLOUD_ONLY_MASK)
    except Exception:
        return False


def is_remote_drive(path: Path) -> bool:
    """Return True if path resides on a mapped network drive (prevents accidental NTLM leak)."""
    if not IS_WINDOWS:
        return False
    try:
        drive = os.path.splitdrive(str(path))[0]
        if not drive:
            return False
        root = f"{drive}\\"
        drive_type = ctypes.windll.kernel32.GetDriveTypeW(root)
        return drive_type == DRIVE_REMOTE
    except Exception:
        return False


def is_reparse_or_system_file(path: str | Path) -> bool:
    """Return True if path is a symlink, junction/reparse point, or system file."""
    p = Path(path)
    try:
        if p.is_symlink():
            return True
    except OSError:
        return True
    if not IS_WINDOWS:
        return False
    try:
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(p))
        if attrs == INVALID_FILE_ATTRIBUTES:
            return False
        return bool(attrs & (FILE_ATTRIBUTE_REPARSE_POINT | FILE_ATTRIBUTE_SYSTEM))
    except Exception:
        return False


def clear_readonly_attribute(path: str | Path) -> bool:
    """Clear read-only attribute on Windows and make writable via chmod."""
    p = Path(path)
    cleared = False
    if IS_WINDOWS:
        try:
            k32 = ctypes.windll.kernel32
            attrs = k32.GetFileAttributesW(str(p))
            if attrs != INVALID_FILE_ATTRIBUTES and (attrs & FILE_ATTRIBUTE_READONLY):
                if k32.SetFileAttributesW(str(p), attrs & ~FILE_ATTRIBUTE_READONLY):
                    cleared = True
        except Exception:
            pass
    try:
        os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
        cleared = True
    except OSError:
        pass
    return cleared


def is_process_alive(pid: int, *, expected_names: tuple[str, ...] | None = None) -> bool:
    """Check whether a process PID is currently alive without requiring psutil."""
    if not isinstance(pid, int) or pid <= 0:
        return False

    if not IS_WINDOWS:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k32.GetExitCodeProcess.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL
    k32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    k32.QueryFullProcessImageNameW.restype = wintypes.BOOL

    ctypes.set_last_error(0)
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        err = ctypes.get_last_error()
        return err == ERROR_ACCESS_DENIED

    try:
        code = wintypes.DWORD(0)
        if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        if code.value != STILL_ACTIVE:
            return False
        if expected_names:
            buf_len = wintypes.DWORD(520)
            buf = ctypes.create_unicode_buffer(buf_len.value)
            if k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(buf_len)):
                exe_name = Path(buf.value).name.lower()
                if not any(exp.lower() in exe_name for exp in expected_names):
                    return False
        return True
    finally:
        k32.CloseHandle(handle)
