"""Single-instance mutex + lazy QLocalServer/QLocalSocket tab forwarding."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import getpass
import hashlib
import json
from pathlib import Path
import threading
from typing import Any, Callable, Iterable

from .path_guard import is_unc_or_network_path, validate_local_any_doc
from .win32_bridge import IS_WINDOWS

APP_INSTANCE_ID = "TRODAI.PDFMDLector"
ERROR_ALREADY_EXISTS = 183
ERROR_ACCESS_DENIED = 5
WAIT_OBJECT_0 = 0x00000000
WAIT_ABANDONED = 0x00000080
MAX_IPC_PAYLOAD_BYTES = 1024 * 1024

_NON_WIN_LOCK = threading.RLock()
_NON_WIN_HELD: set[str] = set()


def instance_key(app_id: str = APP_INSTANCE_ID) -> str:
    """Return deterministic per-user instance key for mutex and QLocalServer."""
    try:
        user = getpass.getuser()
    except Exception:
        user = "user"
    digest = hashlib.sha1(f"{app_id}|{user}".encode("utf-8")).hexdigest()[:12]
    return f"{app_id}-{digest}"


class SingleInstanceLock:
    """Win32 mutex + lazy Qt QLocalServer/QLocalSocket for forwarding .pdf/.md tabs."""

    def __init__(
        self,
        key: str | None = None,
        *,
        mutex_name: str | None = None,
        server_name: str | None = None,
    ) -> None:
        base_key = key or instance_key()
        self.key = base_key
        self.mutex_name = mutex_name or base_key
        self.server_name = server_name or base_key
        self._handle: int | None = None
        self._server: Any = None
        self._active_sockets: list[Any] = []
        self._on_activate: Callable[[list[Path]], None] | None = None
        self.acquired = False

    def __enter__(self) -> SingleInstanceLock:
        self.acquire()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()

    def acquire(self, wait_ms: int = 0) -> bool:
        """Acquire primary instance mutex (pure ctypes, zero Qt imports)."""
        if self.acquired:
            return True

        if not IS_WINDOWS:
            with _NON_WIN_LOCK:
                if self.mutex_name in _NON_WIN_HELD:
                    self.acquired = False
                    return False
                _NON_WIN_HELD.add(self.mutex_name)
                self.acquired = True
                return True

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        k32.CreateMutexW.restype = wintypes.HANDLE
        k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k32.WaitForSingleObject.restype = wintypes.DWORD
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.CloseHandle.restype = wintypes.BOOL

        ctypes.set_last_error(0)
        handle = k32.CreateMutexW(None, True, f"Local\\{self.mutex_name}")
        err = ctypes.get_last_error()

        if not handle:
            if err == ERROR_ACCESS_DENIED:
                self.acquired = False
                return False
            self.acquired = True
            return True

        if err == ERROR_ALREADY_EXISTS:
            if wait_ms <= 0:
                # Close handle immediately so same-thread tests cannot re-enter!
                k32.CloseHandle(handle)
                self.acquired = False
                return False
            rc = k32.WaitForSingleObject(handle, int(wait_ms))
            if rc in (WAIT_OBJECT_0, WAIT_ABANDONED):
                self._handle = int(handle)
                self.acquired = True
                return True
            k32.CloseHandle(handle)
            self.acquired = False
            return False

        self._handle = int(handle)
        self.acquired = True
        return True

    def start_server(self, on_activate: Callable[[list[Path]], None]) -> bool:
        """Start QLocalServer lazily to receive forwarded .pdf/.md/.markdown paths."""
        from PySide6.QtNetwork import QLocalServer

        self._on_activate = on_activate
        if self._server is not None:
            return True

        QLocalServer.removeServer(self.server_name)
        server = QLocalServer()
        if not server.listen(self.server_name):
            return False

        server.newConnection.connect(self._handle_new_connection)
        self._server = server
        return True

    def _handle_new_connection(self) -> None:
        if self._server is None:
            return
        while self._server.hasPendingConnections():
            sock = self._server.nextPendingConnection()
            if sock is None:
                continue
            buf = bytearray()
            handled = {"done": False}
            self._active_sockets.append(sock)

            def _process_payload(s: Any = sock, data_buf: bytearray = buf, state: dict[str, bool] = handled) -> None:
                if state["done"]:
                    return
                text = bytes(data_buf).decode("utf-8", errors="replace").strip()
                if not text:
                    return
                state["done"] = True
                self._dispatch_message(text)
                s.disconnectFromServer()

            def _on_ready_read(
                s: Any = sock,
                data_buf: bytearray = buf,
                state: dict[str, bool] = handled,
                cb: Callable[[], None] = _process_payload,
            ) -> None:
                if state["done"]:
                    return
                chunk = bytes(s.readAll())
                if chunk:
                    if len(data_buf) + len(chunk) > MAX_IPC_PAYLOAD_BYTES:
                        state["done"] = True
                        data_buf.clear()
                        s.abort()
                        return
                    data_buf.extend(chunk)
                    if b"\n" in data_buf:
                        cb()

            def _on_disconnected(s: Any = sock, cb: Callable[[], None] = _process_payload) -> None:
                cb()
                if s in self._active_sockets:
                    self._active_sockets.remove(s)
                s.deleteLater()

            sock.readyRead.connect(_on_ready_read)
            sock.disconnected.connect(_on_disconnected)
            if sock.bytesAvailable() > 0:
                _on_ready_read()

    def _dispatch_message(self, raw_text: str) -> None:
        valid_paths: list[Path] = []
        if raw_text != "show":
            for line in raw_text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except Exception:
                    continue
                if isinstance(msg, dict):
                    raw_paths = msg.get("paths", [])
                    if isinstance(raw_paths, list):
                        for item in raw_paths:
                            ok, _ = validate_local_any_doc(str(item), check_exists=True)
                            if ok:
                                valid_paths.append(Path(str(item)).resolve())
        if self._on_activate is not None:
            self._on_activate(valid_paths)

    def send_to_running_instance(
        self,
        paths: Iterable[str | Path] = (),
        *,
        timeout_ms: int = 1500,
    ) -> bool:
        """Forward local .pdf/.md/.markdown paths to the active primary instance."""
        import time
        from PySide6.QtCore import QCoreApplication
        from PySide6.QtNetwork import QLocalSocket

        safe_paths: list[str] = []
        for item in paths:
            raw = str(item).strip()
            if not raw or is_unc_or_network_path(raw):
                continue
            ok, _ = validate_local_any_doc(raw, check_exists=False)
            if ok:
                safe_paths.append(str(Path(raw)))

        payload = (json.dumps({"action": "activate", "paths": safe_paths}) + "\n").encode("utf-8")
        sock = QLocalSocket()
        sock.connectToServer(self.server_name)
        if not sock.waitForConnected(timeout_ms):
            sock.abort()
            sock.deleteLater()
            return False

        written_bytes = sock.write(payload)
        if written_bytes < 0:
            sock.abort()
            sock.deleteLater()
            return False

        sock.flush()
        app = QCoreApplication.instance()
        deadline = time.monotonic() + (max(100, timeout_ms) / 1000.0)
        while sock.bytesToWrite() > 0 and time.monotonic() < deadline:
            if app is not None:
                app.processEvents()
            sock.waitForBytesWritten(min(50, timeout_ms))

        flushed = sock.bytesToWrite() == 0
        sock.disconnectFromServer()
        if app is not None:
            app.processEvents()
        if sock.state() != QLocalSocket.LocalSocketState.UnconnectedState:
            sock.waitForDisconnected(min(300, timeout_ms))
        sock.deleteLater()
        return flushed

    def release(self) -> None:
        for sock in list(self._active_sockets):
            try:
                sock.abort()
                sock.deleteLater()
            except Exception:
                pass
        self._active_sockets.clear()

        if self._server is not None:
            try:
                self._server.close()
                self._server.deleteLater()
            except Exception:
                pass
            self._server = None

        if not IS_WINDOWS:
            with _NON_WIN_LOCK:
                _NON_WIN_HELD.discard(self.mutex_name)
        elif self._handle is not None:
            try:
                k32 = ctypes.WinDLL("kernel32", use_last_error=True)
                k32.ReleaseMutex.argtypes = [wintypes.HANDLE]
                k32.ReleaseMutex.restype = wintypes.BOOL
                k32.CloseHandle.argtypes = [wintypes.HANDLE]
                k32.CloseHandle.restype = wintypes.BOOL
                k32.ReleaseMutex(wintypes.HANDLE(self._handle))
                k32.CloseHandle(wintypes.HANDLE(self._handle))
            except Exception:
                pass
            self._handle = None

        self.acquired = False
