"""CrashGuard & rotating diagnostics logger for %LOCALAPPDATA%\\TRODAI\\PDFLector\\logs\\."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
import faulthandler
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import platform
import subprocess
import sys
import threading
import time
import traceback
from typing import Any, Callable

from .. import __version__ as _PKG_VERSION
from .win32_bridge import IS_WINDOWS, atomic_write_text, default_app_data_dir, is_process_alive

CRASH_LOG_NAME = "crash.log"
APP_LOG_NAME = "app.log"
SESSION_LOCK_NAME = "session_lock.json"
MAX_CRASH_LOG_BYTES = 1_000_000
MAX_BREADCRUMBS = 40
HANG_THRESHOLD_S = 8.0


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass
class PreviousSessionReport:
    """Summary of the preceding session loaded from session_lock.json."""

    dirty: bool
    pid: int = 0
    version: str = ""
    started_at: str = ""
    last_heartbeat: str = ""
    crash_log_tail: str = ""
    event_log_entries: list[str] = field(default_factory=list)

    @property
    def previous_crashed(self) -> bool:
        return self.dirty

    def summary(self) -> str:
        if not self.dirty:
            return "Previous session ended cleanly."
        return (
            f"Unclean previous session detected (pid={self.pid}, version={self.version or 'unknown'}, "
            f"started={self.started_at or 'unknown'}, last_heartbeat={self.last_heartbeat or 'unknown'})."
        )


class CrashGuard:
    """Session sentinel, excepthook, faulthandler, and rotating crash.log/app.log manager."""

    def __init__(
        self,
        logs_dir: Path | None = None,
        *,
        version: str = "",
        app_dir: Path | None = None,
        max_crash_log_bytes: int = MAX_CRASH_LOG_BYTES,
        max_app_log_bytes: int = MAX_CRASH_LOG_BYTES,
        backup_count: int = 3,
    ) -> None:
        if logs_dir is not None:
            self.logs_dir = Path(logs_dir)
        elif app_dir is not None:
            self.logs_dir = Path(app_dir) / "logs"
        else:
            self.logs_dir = default_app_data_dir() / "logs"
        self.logs_dir.mkdir(parents=True, exist_ok=True)

        self.crash_log_path = self.logs_dir / CRASH_LOG_NAME
        self.app_log_path = self.logs_dir / APP_LOG_NAME
        self.session_lock_path = self.logs_dir / SESSION_LOCK_NAME
        self.version = version or _PKG_VERSION
        self.max_crash_log_bytes = max(256, int(max_crash_log_bytes))
        self.max_app_log_bytes = max(256, int(max_app_log_bytes))
        self.backup_count = max(1, int(backup_count))

        self._lock = threading.RLock()
        self._breadcrumbs: deque[str] = deque(maxlen=MAX_BREADCRUMBS)
        self._listeners: list[Callable[[str, str], None]] = []

        self._installed = False
        self._fh_stream: Any = None
        self._app_handler: RotatingFileHandler | None = None
        self._app_logger: logging.Logger | None = None
        self._prev_excepthook: Any = None
        self._prev_threading_excepthook: Any = None
        self._prev_qt_handler: Any = None
        self._qt_handler_installed = False

        self._session_started_at = ""
        self._clean_exit_marked = False
        self._last_ui_tick = time.monotonic()
        self._watchdog_stop = threading.Event()
        self._watchdog_thread: threading.Thread | None = None
        self._previous_report: PreviousSessionReport | None = None

    @property
    def previous_report(self) -> PreviousSessionReport | None:
        return self._previous_report

    def _rotate_crash_log_if_needed(self) -> None:
        try:
            if not self.crash_log_path.exists():
                return
            if self.crash_log_path.stat().st_size < self.max_crash_log_bytes:
                return
        except OSError:
            return

        # Close faulthandler stream briefly if open during rotation
        reopen_fh = self._fh_stream is not None
        if reopen_fh:
            try:
                faulthandler.disable()
            except Exception:
                pass
            try:
                self._fh_stream.close()
            except Exception:
                pass
            self._fh_stream = None

        try:
            for idx in range(self.backup_count, 0, -1):
                src = (
                    self.crash_log_path
                    if idx == 1
                    else self.logs_dir / f"{CRASH_LOG_NAME}.{idx - 1}"
                )
                dst = self.logs_dir / f"{CRASH_LOG_NAME}.{idx}"
                if src.exists():
                    try:
                        if dst.exists():
                            dst.unlink()
                        src.replace(dst)
                    except OSError:
                        pass
        finally:
            if reopen_fh:
                try:
                    self._fh_stream = open(self.crash_log_path, "a", encoding="utf-8")
                    faulthandler.enable(file=self._fh_stream, all_threads=True)
                except Exception:
                    self._fh_stream = None

    def install(self) -> None:
        with self._lock:
            if self._installed:
                return
            self.logs_dir.mkdir(parents=True, exist_ok=True)
            self._rotate_crash_log_if_needed()

            self._fh_stream = open(self.crash_log_path, "a", encoding="utf-8")
            try:
                faulthandler.enable(file=self._fh_stream, all_threads=True)
            except Exception:
                pass

            handler = RotatingFileHandler(
                self.app_log_path,
                maxBytes=self.max_app_log_bytes,
                backupCount=self.backup_count,
                encoding="utf-8",
            )
            handler.setFormatter(
                logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%dT%H:%M:%SZ")
            )
            logger = logging.getLogger(f"pdf_lector.crash_guard.{id(self)}")
            logger.setLevel(logging.DEBUG)
            logger.propagate = False
            logger.handlers.clear()
            logger.addHandler(handler)
            self._app_handler = handler
            self._app_logger = logger

            self._prev_excepthook = sys.excepthook
            self._prev_threading_excepthook = threading.excepthook

            def _sys_hook(exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
                self.log_exception(exc_type, exc_val, exc_tb, context="sys.excepthook")
                if self._prev_excepthook is not None:
                    try:
                        self._prev_excepthook(exc_type, exc_val, exc_tb)
                    except Exception:
                        pass

            def _thread_hook(args: threading.ExceptHookArgs) -> None:
                thread_name = getattr(args.thread, "name", "unknown")
                self.log_exception(
                    args.exc_type,
                    args.exc_value,
                    args.exc_traceback,
                    context=f"threading.excepthook ({thread_name})",
                )
                if self._prev_threading_excepthook is not None:
                    try:
                        self._prev_threading_excepthook(args)
                    except Exception:
                        pass

            sys.excepthook = _sys_hook
            threading.excepthook = _thread_hook
            self._installed = True

    def install_qt_message_handler(self) -> None:
        """Install Qt message handler lazily (never imported during Phase 1)."""
        with self._lock:
            if self._qt_handler_installed:
                return
            try:
                from PySide6.QtCore import QtMsgType, qInstallMessageHandler
            except Exception:
                return

            def _qt_handler(mode: QtMsgType, _context: Any, message: str) -> None:
                level = "INFO"
                if mode in (QtMsgType.QtCriticalMsg, QtMsgType.QtFatalMsg):
                    level = "ERROR"
                elif mode == QtMsgType.QtWarningMsg:
                    level = "WARNING"
                self.log_app(level, f"[Qt] {message}")
                if mode == QtMsgType.QtFatalMsg:
                    self.write_log_block("QtFatalMsg", message)

            self._prev_qt_handler = qInstallMessageHandler(_qt_handler)
            self._qt_handler_installed = True

    def uninstall(self) -> None:
        self.stop_hang_watchdog()
        with self._lock:
            if self._qt_handler_installed:
                try:
                    from PySide6.QtCore import qInstallMessageHandler

                    qInstallMessageHandler(self._prev_qt_handler)
                except Exception:
                    pass
                self._qt_handler_installed = False
                self._prev_qt_handler = None

            if self._installed:
                if sys.excepthook is not None and self._prev_excepthook is not None:
                    sys.excepthook = self._prev_excepthook
                if self._prev_threading_excepthook is not None:
                    threading.excepthook = self._prev_threading_excepthook

                try:
                    faulthandler.disable()
                except Exception:
                    pass

                if self._fh_stream is not None:
                    try:
                        self._fh_stream.flush()
                        self._fh_stream.close()
                    except Exception:
                        pass
                    self._fh_stream = None

                if self._app_logger is not None and self._app_handler is not None:
                    try:
                        self._app_logger.removeHandler(self._app_handler)
                        self._app_handler.flush()
                        self._app_handler.close()
                    except Exception:
                        pass
                self._app_handler = None
                self._app_logger = None
                self._installed = False

    def begin_session(self) -> PreviousSessionReport:
        dirty = False
        prev_pid = 0
        prev_version = ""
        prev_started = ""
        prev_hb = ""

        if self.session_lock_path.exists():
            try:
                raw = json.loads(self.session_lock_path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    dirty = True
                else:
                    prev_pid = int(raw.get("pid", 0) or 0)
                    prev_version = str(raw.get("version", ""))
                    prev_started = str(raw.get("started_at", ""))
                    prev_hb = str(raw.get("last_heartbeat", ""))
                    clean_exit = bool(raw.get("clean_exit", False))
                    if not clean_exit:
                        if prev_pid <= 0 or prev_pid == os.getpid():
                            dirty = True
                        elif not is_process_alive(prev_pid, expected_names=("python", "pdf")):
                            dirty = True
            except Exception:
                dirty = True

        self._session_started_at = _utc_now_iso()
        self._clean_exit_marked = False
        self._write_session_lock(clean_exit=False)

        tail = self.read_crash_log_tail() if dirty else ""
        report = PreviousSessionReport(
            dirty=dirty,
            pid=prev_pid,
            version=prev_version,
            started_at=prev_started,
            last_heartbeat=prev_hb,
            crash_log_tail=tail,
        )
        self._previous_report = report

        self.breadcrumb(f"session_start version={self.version} pid={os.getpid()}")
        self.log_app("INFO", f"Session started (version={self.version}, pid={os.getpid()}, prev_dirty={dirty})")
        if dirty:
            self.write_log_block(
                "Unclean Previous Session",
                report.summary(),
            )
        return report

    def _write_session_lock(self, *, clean_exit: bool) -> None:
        now = _utc_now_iso()
        payload = {
            "pid": os.getpid(),
            "version": self.version,
            "started_at": self._session_started_at or now,
            "last_heartbeat": now,
            "clean_exit": bool(clean_exit),
        }
        try:
            atomic_write_text(self.session_lock_path, json.dumps(payload, indent=2))
        except OSError:
            pass

    def heartbeat(self) -> None:
        with self._lock:
            if not self._clean_exit_marked:
                self._write_session_lock(clean_exit=False)

    def mark_clean_exit(self) -> None:
        with self._lock:
            if self._clean_exit_marked:
                return
            self._clean_exit_marked = True
            self._write_session_lock(clean_exit=True)
            self.breadcrumb("clean_exit")
            self.log_app("INFO", "Session marked clean exit")

    def breadcrumb(self, message: str) -> None:
        entry = f"{_utc_now_iso()} | {message}"
        with self._lock:
            self._breadcrumbs.append(entry)

    def recent_breadcrumbs(self) -> list[str]:
        with self._lock:
            return list(self._breadcrumbs)

    def log_app(self, level: str, message: str) -> None:
        lvl = getattr(logging, str(level).upper(), logging.INFO)
        with self._lock:
            if self._app_logger is not None:
                self._app_logger.log(lvl, message)
            else:
                # Fallback append if log_app called before install()
                line = f"{_utc_now_iso()} [{str(level).upper()}] {message}\n"
                try:
                    with open(self.app_log_path, "a", encoding="utf-8") as fh:
                        fh.write(line)
                except OSError:
                    pass

    def write_log_block(self, title: str, body: str) -> None:
        with self._lock:
            self._rotate_crash_log_if_needed()
            ts = _utc_now_iso()
            crumbs = "\n".join(f"  - {c}" for c in self._breadcrumbs)
            block = (
                f"\n=== [{ts}] {title} ===\n"
                f"{body.rstrip()}\n"
                + (f"Breadcrumbs:\n{crumbs}\n" if crumbs else "")
                + "=== END ===\n"
            )
            try:
                if self._fh_stream is not None:
                    self._fh_stream.write(block)
                    self._fh_stream.flush()
                else:
                    with open(self.crash_log_path, "a", encoding="utf-8") as fh:
                        fh.write(block)
            except OSError:
                pass

    def add_unhandled_listener(self, fn: Callable[[str, str], None]) -> None:
        with self._lock:
            self._listeners.append(fn)

    def log_exception(
        self,
        exc_type: Any,
        exc_val: Any,
        exc_tb: Any,
        *,
        context: str = "unhandled",
    ) -> str:
        tb_text = "".join(traceback.format_exception(exc_type, exc_val, exc_tb))
        title = f"{context}: {getattr(exc_type, '__name__', str(exc_type))}"
        self.write_log_block(title, tb_text)
        self.log_app("ERROR", f"{title}: {exc_val}")
        with self._lock:
            listeners = list(self._listeners)
        for fn in listeners:
            try:
                fn(title, tb_text)
            except Exception:
                pass
        return tb_text

    def read_crash_log_tail(self, max_chars: int = 6000) -> str:
        if not self.crash_log_path.exists():
            return ""
        try:
            text = self.crash_log_path.read_text(encoding="utf-8", errors="replace")
            return text[-max_chars:] if len(text) > max_chars else text
        except OSError:
            return ""

    def read_app_log_tail(self, max_chars: int = 6000) -> str:
        if not self.app_log_path.exists():
            return ""
        try:
            text = self.app_log_path.read_text(encoding="utf-8", errors="replace")
            return text[-max_chars:] if len(text) > max_chars else text
        except OSError:
            return ""

    def ui_tick(self) -> None:
        self._last_ui_tick = time.monotonic()

    def start_hang_watchdog(
        self,
        *,
        threshold_s: float = HANG_THRESHOLD_S,
        poll_s: float = 1.0,
    ) -> None:
        with self._lock:
            if self._watchdog_thread is not None and self._watchdog_thread.is_alive():
                return
            self._watchdog_stop.clear()
            self._last_ui_tick = time.monotonic()

            def _watch() -> None:
                prev_wall = time.time()
                prev_mono = time.monotonic()
                dumped_for_stall = False
                while not self._watchdog_stop.wait(poll_s):
                    now_wall = time.time()
                    now_mono = time.monotonic()
                    wall_dt = now_wall - prev_wall
                    mono_dt = now_mono - prev_mono
                    prev_wall = now_wall
                    prev_mono = now_mono

                    # Skip false hang alert after OS sleep/hibernate
                    if wall_dt - mono_dt > 5.0:
                        self._last_ui_tick = now_mono
                        dumped_for_stall = False
                        continue

                    stall = now_mono - self._last_ui_tick
                    if stall >= threshold_s and not dumped_for_stall:
                        dumped_for_stall = True
                        self.dump_all_threads(f"UI Stall Detected ({stall:.1f}s)")
                    elif stall < threshold_s:
                        dumped_for_stall = False

            self._watchdog_thread = threading.Thread(
                target=_watch,
                name="CrashGuardWatchdog",
                daemon=True,
            )
            self._watchdog_thread.start()

    def stop_hang_watchdog(self) -> None:
        self._watchdog_stop.set()
        t = self._watchdog_thread
        if t is not None and t.is_alive():
            t.join(timeout=1.5)
        self._watchdog_thread = None

    def dump_all_threads(self, title: str = "Thread Dump") -> str:
        frames = sys._current_frames()
        thread_names = {t.ident: t.name for t in threading.enumerate()}
        lines: list[str] = []
        for tid, frame in frames.items():
            name = thread_names.get(tid, "unknown")
            lines.append(f"--- Thread {tid} ({name}) ---")
            lines.extend(traceback.format_stack(frame))
        body = "".join(lines)
        self.write_log_block(title, body)
        return body

    def query_recent_appcrash_events(
        self,
        *,
        max_events: int = 3,
        exe_name: str = "PDF-MD-Lector",
    ) -> list[str]:
        if not IS_WINDOWS:
            return []
        try:
            proc = subprocess.run(
                [
                    "wevtutil",
                    "qe",
                    "Application",
                    f"/q:*[System[Provider[@Name='Application Error']]]",
                    f"/c:{max(1, int(max_events))}",
                    "/rd:true",
                    "/f:text",
                ],
                capture_output=True,
                text=True,
                timeout=12,
                check=False,
            )
            if proc.returncode != 0 or not proc.stdout:
                return []
            blocks = [b.strip() for b in proc.stdout.split("Event[") if exe_name.lower() in b.lower()]
            return [f"Event[{b}" for b in blocks[:max_events]]
        except Exception:
            return []

    def build_diagnostic_text(self) -> str:
        crumbs = "\n".join(self.recent_breadcrumbs()) or "(none)"
        crash_tail = self.read_crash_log_tail(3000) or "(empty)"
        app_tail = self.read_app_log_tail(2000) or "(empty)"
        return (
            f"App Version: {self.version}\n"
            f"OS: {platform.platform()}\n"
            f"Python: {platform.python_version()}\n"
            f"PID: {os.getpid()}\n"
            f"Logs Dir: {self.logs_dir}\n\n"
            f"--- Breadcrumbs ---\n{crumbs}\n\n"
            f"--- crash.log (tail) ---\n{crash_tail}\n\n"
            f"--- app.log (tail) ---\n{app_tail}\n"
        )
