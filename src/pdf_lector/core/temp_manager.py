"""Session-scoped temporary file & directory manager with secure wipe and stale-PID cleanup."""

from __future__ import annotations

import atexit
from contextlib import contextmanager
import os
from pathlib import Path
import re
import shutil
import tempfile
import threading
import time
from typing import Iterator
import uuid
import weakref

from .path_guard import is_unc_or_network_path
from .win32_bridge import (
    IS_WINDOWS,
    clear_readonly_attribute,
    is_process_alive,
    is_reparse_or_system_file,
)

TEMP_APP_DIR_NAME = "TRODAI_PDFLector"
DEFAULT_STALE_AGE_SECONDS = 3600.0
_SESSION_DIR_RE = re.compile(r"^(\d+)_[0-9a-fA-F]+$")


def _long_path_str(path: Path) -> str:
    text = str(path)
    if IS_WINDOWS and len(text) >= 240 and not text.startswith("\\\\?\\"):
        return f"\\\\?\\{os.path.abspath(text)}"
    return text


def secure_unlink(path: Path, *, overwrite_zero: bool = False) -> bool:
    """Safely unlink a file, clearing read-only attributes and optionally zero-overwriting.

    Never follows or overwrites targets of symlinks or Windows reparse points.
    Returns True if the path no longer exists on disk.
    """
    p = Path(path)
    try:
        if not p.exists() and not p.is_symlink():
            return True
    except OSError:
        return False

    # Never follow or overwrite through symlinks / junctions / system files
    if os.path.islink(p) or is_reparse_or_system_file(p):
        try:
            os.unlink(_long_path_str(p))
            return True
        except OSError:
            return False

    if p.is_dir():
        return False

    clear_readonly_attribute(p)
    lp = _long_path_str(p)

    if overwrite_zero:
        try:
            size = p.stat().st_size
            if size > 0:
                chunk = b"\x00" * 65536
                with open(lp, "r+b") as fh:
                    remaining = size
                    while remaining > 0:
                        to_write = min(remaining, len(chunk))
                        fh.write(chunk[:to_write])
                        remaining -= to_write
                    fh.flush()
                    os.fsync(fh.fileno())
        except OSError:
            pass

    try:
        os.unlink(lp)
        return True
    except OSError:
        return False


def _remove_tree_safe(root: Path, *, secure: bool = False) -> list[Path]:
    """Recursively remove directory contents without following symlinks/junctions."""
    failed: list[Path] = []
    p = Path(root)
    try:
        if not p.exists() and not p.is_symlink():
            return failed
    except OSError:
        return [p]

    if os.path.islink(p) or is_reparse_or_system_file(p):
        try:
            os.unlink(_long_path_str(p))
        except OSError:
            failed.append(p)
        return failed

    if not p.is_dir():
        if not secure_unlink(p, overwrite_zero=secure):
            failed.append(p)
        return failed

    try:
        entries = list(p.iterdir())
    except OSError:
        return [p]

    for entry in entries:
        if os.path.islink(entry) or is_reparse_or_system_file(entry):
            try:
                os.unlink(_long_path_str(entry))
            except OSError:
                failed.append(entry)
        elif entry.is_dir():
            failed.extend(_remove_tree_safe(entry, secure=secure))
        else:
            if not secure_unlink(entry, overwrite_zero=secure):
                failed.append(entry)

    clear_readonly_attribute(p)
    try:
        os.rmdir(_long_path_str(p))
    except OSError:
        try:
            shutil.rmtree(_long_path_str(p), ignore_errors=False)
        except OSError:
            if p.exists():
                failed.append(p)
    return failed


def _atexit_cleanup(manager_ref: weakref.ReferenceType[TempManager]) -> None:
    mgr = manager_ref()
    if mgr is not None:
        mgr.cleanup_all()


class TempManager:
    """Manages %TEMP%\\TRODAI_PDFLector\\<pid>_<uuid> session workspace and task cleanup."""

    def __init__(
        self,
        *,
        temp_root: Path | None = None,
        session_id: str | None = None,
        register_atexit: bool = True,
    ) -> None:
        base = Path(temp_root) if temp_root is not None else Path(tempfile.gettempdir())
        self.app_temp_root = base / TEMP_APP_DIR_NAME
        self.session_tag = session_id or f"{os.getpid()}_{uuid.uuid4().hex[:10]}"
        self.session_dir = self.app_temp_root / self.session_tag
        self._lock = threading.RLock()
        self._task_paths: dict[str, list[tuple[Path, bool]]] = {}
        self._closed = False
        if register_atexit:
            atexit.register(_atexit_cleanup, weakref.ref(self))

    def ensure_session_dir(self) -> Path:
        with self._lock:
            self.session_dir.mkdir(parents=True, exist_ok=True)
            return self.session_dir

    def create_temp_dir(
        self,
        *,
        prefix: str = "task_",
        task_id: str | None = None,
        secure: bool = False,
    ) -> Path:
        sdir = self.ensure_session_dir()
        created = Path(tempfile.mkdtemp(prefix=prefix, dir=str(sdir)))
        self.register_path(created, task_id=task_id, secure=secure)
        return created

    def create_temp_file(
        self,
        *,
        suffix: str = ".tmp",
        prefix: str = "pdf_",
        task_id: str | None = None,
        secure: bool = False,
    ) -> Path:
        sdir = self.ensure_session_dir()
        fd, raw_path = tempfile.mkstemp(suffix=suffix, prefix=prefix, dir=str(sdir))
        os.close(fd)
        created = Path(raw_path)
        self.register_path(created, task_id=task_id, secure=secure)
        return created

    def register_path(
        self,
        path: Path | str,
        *,
        task_id: str | None = None,
        secure: bool = False,
    ) -> Path:
        raw_str = str(path).strip()
        if not raw_str or is_unc_or_network_path(raw_str):
            raise ValueError("network_or_url_blocked")
        p = Path(raw_str)
        if os.path.islink(p) or is_reparse_or_system_file(p):
            raise ValueError("reparse_or_symlink_blocked")
        bucket = task_id or "__session__"
        with self._lock:
            items = self._task_paths.setdefault(bucket, [])
            items.append((p, bool(secure)))
        return p

    @contextmanager
    def task_scope(
        self,
        task_id: str | None = None,
        *,
        secure: bool = False,
    ) -> Iterator[str]:
        tid = task_id or f"task_{uuid.uuid4().hex[:8]}"
        try:
            yield tid
        finally:
            self.cleanup_task(tid, secure=secure if secure else None)

    def cleanup_task(self, task_id: str, *, secure: bool | None = None) -> list[Path]:
        with self._lock:
            entries = self._task_paths.pop(task_id, [])
        failed: list[Path] = []
        # Clean deepest paths first
        for p, item_secure in reversed(entries):
            use_secure = item_secure if secure is None else bool(secure)
            if p.is_dir() and not os.path.islink(p):
                failed.extend(_remove_tree_safe(p, secure=use_secure))
            else:
                if not secure_unlink(p, overwrite_zero=use_secure):
                    failed.append(p)
        return failed

    def cleanup_all(self, *, secure: bool | None = None) -> list[Path]:
        with self._lock:
            task_ids = list(self._task_paths.keys())
            self._closed = True
        failed: list[Path] = []
        for tid in task_ids:
            failed.extend(self.cleanup_task(tid, secure=secure))
        if self.session_dir.exists() or self.session_dir.is_symlink():
            failed.extend(_remove_tree_safe(self.session_dir, secure=bool(secure)))
        return failed

    def cleanup_stale_sessions(
        self,
        *,
        min_age_seconds: float = DEFAULT_STALE_AGE_SECONDS,
    ) -> list[Path]:
        """Remove stale session directories left by dead PIDs."""
        removed: list[Path] = []
        if not self.app_temp_root.exists():
            return removed

        now = time.time()
        current_pid = os.getpid()
        try:
            children = list(self.app_temp_root.iterdir())
        except OSError:
            return removed

        for child in children:
            if child == self.session_dir:
                continue
            if os.path.islink(child) or is_reparse_or_system_file(child):
                continue
            if not child.is_dir():
                continue
            match = _SESSION_DIR_RE.match(child.name)
            if not match:
                continue
            pid = int(match.group(1))
            if pid == current_pid or is_process_alive(pid):
                continue
            try:
                age = now - child.stat().st_mtime
            except OSError:
                continue
            if age < min_age_seconds:
                continue
            failed = _remove_tree_safe(child, secure=False)
            if not failed and not child.exists():
                removed.append(child)
        return removed
