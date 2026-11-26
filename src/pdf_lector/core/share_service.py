"""Local Windows PDF sharing service: Clipboard file drop (CF_HDROP), Simple MAPI Email & messaging launchers."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
from typing import Callable
from urllib.parse import quote

from PySide6.QtCore import QMimeData, QUrl
from PySide6.QtWidgets import QApplication

from .path_guard import validate_local_any_doc


@dataclass(frozen=True)
class ShareResult:
    """Outcome of a local sharing / attachment preparation action."""

    ok: bool
    channel: str
    message: str


def _clean_child_env() -> dict[str, str]:
    env = dict(os.environ)
    for key in list(env.keys()):
        if key.startswith("_MEIPASS") or key == "PYTHONHOME":
            env.pop(key, None)
    return env


def copy_file_to_clipboard(path: Path | str) -> ShareResult:
    """Place the validated local document file onto the system clipboard (CF_HDROP URL)."""
    ok, reason = validate_local_any_doc(path, check_exists=True)
    if not ok:
        return ShareResult(ok=False, channel="clipboard", message=reason)
    resolved = Path(path).resolve()
    cb = QApplication.clipboard()
    if cb is None:
        return ShareResult(ok=False, channel="clipboard", message="Portapapeles del sistema no disponible.")
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(resolved))])
    mime.setText(str(resolved))
    cb.setMimeData(mime)
    return ShareResult(
        ok=True,
        channel="clipboard",
        message=f"Archivo '{resolved.name}' copiado al portapapeles. Pega con Ctrl+V para enviarlo como adjunto.",
    )


def send_via_mapi_email(
    path: Path | str,
    *,
    subject: str = "",
    body: str = "",
) -> ShareResult:
    """Open default Windows desktop mail client with the validated file attached via MAPISendMailW."""
    ok, reason = validate_local_any_doc(path, check_exists=True)
    if not ok:
        return ShareResult(ok=False, channel="email_mapi", message=reason)
    resolved = Path(path).resolve()

    if os.name != "nt":
        return ShareResult(ok=False, channel="email_mapi", message="MAPI solo disponible en Windows.")

    try:
        mapi = ctypes.WinDLL("mapi32")
        send_fn = mapi.MAPISendMailW
    except Exception as exc:
        return ShareResult(ok=False, channel="email_mapi", message=f"Cliente MAPI de escritorio no disponible: {exc}")

    class _MapiFileDescW(ctypes.Structure):
        _fields_ = [
            ("ulReserved", wintypes.ULONG),
            ("flFlags", wintypes.ULONG),
            ("nPosition", wintypes.ULONG),
            ("lpszPathName", wintypes.LPCWSTR),
            ("lpszFileName", wintypes.LPCWSTR),
            ("lpFileType", ctypes.c_void_p),
        ]

    class _MapiMessageW(ctypes.Structure):
        _fields_ = [
            ("ulReserved", wintypes.ULONG),
            ("lpszSubject", wintypes.LPCWSTR),
            ("lpszNoteText", wintypes.LPCWSTR),
            ("lpszMessageType", wintypes.LPCWSTR),
            ("lpszDateReceived", wintypes.LPCWSTR),
            ("lpszConversationID", wintypes.LPCWSTR),
            ("flFlags", wintypes.ULONG),
            ("lpOriginator", ctypes.c_void_p),
            ("nRecipCount", wintypes.ULONG),
            ("lpRecips", ctypes.c_void_p),
            ("nFileCount", wintypes.ULONG),
            ("lpFiles", ctypes.POINTER(_MapiFileDescW)),
        ]

    subj_txt = subject.strip() or f"Documento adjunto: {resolved.name}"
    body_txt = body.strip() or f"Se adjunta el documento {resolved.name}."

    file_desc = _MapiFileDescW(
        ulReserved=0,
        flFlags=0,
        nPosition=0xFFFFFFFF,
        lpszPathName=str(resolved),
        lpszFileName=resolved.name,
        lpFileType=None,
    )
    msg = _MapiMessageW(
        ulReserved=0,
        lpszSubject=subj_txt,
        lpszNoteText=body_txt,
        lpszMessageType=None,
        lpszDateReceived=None,
        lpszConversationID=None,
        flFlags=0,
        lpOriginator=None,
        nRecipCount=0,
        lpRecips=None,
        nFileCount=1,
        lpFiles=ctypes.pointer(file_desc),
    )

    send_fn.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(_MapiMessageW),
        wintypes.ULONG,
        wintypes.ULONG,
    ]
    send_fn.restype = wintypes.ULONG

    MAPI_DIALOG = 0x00000008
    MAPI_LOGON_UI = 0x00000001
    USER_ABORT = 1
    rc = int(send_fn(None, None, ctypes.byref(msg), MAPI_DIALOG | MAPI_LOGON_UI, 0))
    if rc == 0:
        return ShareResult(
            ok=True,
            channel="email_mapi",
            message=f"Cliente de correo abierto con '{resolved.name}' adjunto.",
        )
    if rc == USER_ABORT:
        return ShareResult(
            ok=True,
            channel="email_mapi",
            message="Envio cancelado en el cliente de correo.",
        )
    return ShareResult(
        ok=False,
        channel="email_mapi",
        message=f"No se pudo abrir el cliente de correo predeterminado (codigo MAPI {rc}).",
    )


def _default_uri_launcher(target: str) -> bool:
    if os.name == "nt":
        try:
            os.startfile(target)  # type: ignore[attr-defined]
            return True
        except Exception:
            return False
    return False


def share_to_channel(
    path: Path | str,
    channel: str,
    *,
    clipboard_fn: Callable[[Path], ShareResult] | None = None,
    mapi_fn: Callable[[Path], ShareResult] | None = None,
    launcher_fn: Callable[[str], bool] | None = None,
    explorer_fn: Callable[[Path], bool] | None = None,
) -> ShareResult:
    """Dispatch PDF/MD sharing across local Email MAPI, Messaging apps (Clipboard+URI), Webmail & Explorer."""
    ok, reason = validate_local_any_doc(path, check_exists=True)
    if not ok:
        return ShareResult(ok=False, channel=channel, message=reason)
    resolved = Path(path).resolve()
    ch = (channel or "").strip().lower()

    cb_exec = clipboard_fn or copy_file_to_clipboard
    mapi_exec = mapi_fn or send_via_mapi_email
    open_uri = launcher_fn or _default_uri_launcher

    if ch == "clipboard":
        return cb_exec(resolved)

    if ch == "email_mapi":
        res = mapi_exec(resolved)
        if res.ok:
            return res
        cb_res = cb_exec(resolved)
        subj = quote(f"Documento adjunto: {resolved.name}")
        body = quote(f"Adjunto el documento {resolved.name} (pegalo con Ctrl+V desde el portapapeles).")
        open_uri(f"mailto:?subject={subj}&body={body}")
        extra = " Archivo copiado al portapapeles (Ctrl+V)." if cb_res.ok else ""
        return ShareResult(
            ok=cb_res.ok,
            channel="email_mapi",
            message=f"No se detecto cliente MAPI directo; se abrio mailto:{extra}",
        )

    uri_targets: dict[str, tuple[str, str, str]] = {
        "whatsapp": ("whatsapp://send", "https://web.whatsapp.com/", "WhatsApp"),
        "telegram": ("tg://resolve", "https://web.telegram.org/", "Telegram"),
        "teams": ("msteams://", "https://teams.microsoft.com/", "Microsoft Teams"),
        "email_web": (
            f"mailto:?subject={quote('Documento: ' + resolved.name)}",
            "",
            "Correo web",
        ),
    }
    if ch in uri_targets:
        cb_res = cb_exec(resolved)
        primary_uri, fallback_uri, label = uri_targets[ch]
        launched = bool(open_uri(primary_uri))
        if not launched and fallback_uri:
            launched = bool(open_uri(fallback_uri))
        return ShareResult(
            ok=cb_res.ok or launched,
            channel=ch,
            message=(
                f"'{resolved.name}' copiado como adjunto. Abre el chat en {label} y presiona Ctrl+V para enviarlo."
            ),
        )

    if ch == "explorer":
        if explorer_fn is not None:
            opened = bool(explorer_fn(resolved))
        else:
            try:
                subprocess.Popen(
                    ["explorer.exe", f"/select,{resolved}"],
                    env=_clean_child_env(),
                )
                opened = True
            except Exception:
                opened = False
        return ShareResult(
            ok=opened,
            channel="explorer",
            message=(
                f"Explorador abierto seleccionando '{resolved.name}' para arrastrarlo a cualquier chat."
                if opened
                else "No se pudo abrir el Explorador de Windows."
            ),
        )

    return ShareResult(ok=False, channel=ch, message=f"Canal no soportado: {ch}")
