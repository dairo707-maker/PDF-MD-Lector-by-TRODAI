"""Cooperative TaskController, SessionManager, and Shiboken6-safe AsyncTask / TaskManager."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import inspect
import json
from pathlib import Path
import threading
import time
from typing import Any, Callable, Literal
import uuid

from .win32_bridge import IS_WINDOWS, atomic_write_text, default_app_data_dir

try:
    from PySide6.QtCore import QCoreApplication, QObject, QRunnable, Qt, QThreadPool, Signal
except Exception:  # pragma: no cover - fallback only if Qt unavailable
    QCoreApplication = None  # type: ignore[assignment]
    QObject = object  # type: ignore[misc, assignment]
    QRunnable = object  # type: ignore[misc, assignment]
    Qt = None  # type: ignore[assignment]
    QThreadPool = None  # type: ignore[assignment]
    Signal = None  # type: ignore[assignment]


def qt_is_valid(obj: Any) -> bool:
    """Safely check whether a Shiboken6/PySide6 C++ object is still alive."""
    if obj is None:
        return False
    try:
        import shiboken6

        return bool(shiboken6.isValid(obj))
    except Exception:
        return True


def format_eta_seconds(seconds: float) -> str:
    if seconds < 0 or seconds != seconds:
        return "--:--"
    total = int(round(seconds))
    mins, secs = divmod(total, 60)
    hours, mins = divmod(mins, 60)
    if hours > 0:
        return f"{hours:d}:{mins:02d}:{secs:02d}"
    return f"{mins:02d}:{secs:02d}"


def new_session_id() -> str:
    return f"sess_{uuid.uuid4().hex[:12]}"


class TaskPausedAndSaved(Exception):
    """Raised when a cooperative task pauses and persists a resumable session checkpoint."""

    def __init__(self, session_id: str, percent: int, detail: str = "") -> None:
        self.session_id = session_id
        self.percent = int(percent)
        self.detail = detail
        super().__init__(detail or f"Task paused and saved ({session_id} @ {percent}%)")


class TaskCanceledError(Exception):
    """Raised when a cooperative task is canceled by the user."""

    def __init__(self, detail: str = "Task canceled") -> None:
        self.detail = detail
        super().__init__(detail)


class TaskSaveRequested(Exception):
    """Signaled by checkpoint consumers when pause-and-save was requested."""

    def __init__(self, detail: str = "Save checkpoint requested") -> None:
        self.detail = detail
        super().__init__(detail)


@dataclass(slots=True)
class TaskSessionRecord:
    session_id: str
    kind: str
    title: str
    created_at: str
    percent: int
    total_items: int
    processed_items: int
    pending_paths: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


class SessionManager:
    """Persists resumable task sessions under %LOCALAPPDATA%\\TRODAI\\PDFLector\\sessions\\."""

    def __init__(
        self,
        app_dir: Path | None = None,
        *,
        sessions_dir: Path | None = None,
    ) -> None:
        if sessions_dir is not None:
            self.sessions_dir = Path(sessions_dir)
        elif app_dir is not None:
            self.sessions_dir = Path(app_dir) / "sessions"
        else:
            self.sessions_dir = default_app_data_dir() / "sessions"
        self._lock = threading.RLock()

    def _session_path(self, session_id: str) -> Path:
        safe_id = "".join(c for c in session_id if c.isalnum() or c in ("_", "-"))
        return self.sessions_dir / f"{safe_id}.json"

    def save_session(self, record: TaskSessionRecord) -> Path:
        path = self._session_path(record.session_id)
        with self._lock:
            atomic_write_text(path, json.dumps(asdict(record), indent=2, ensure_ascii=False))
        return path

    def load_session(self, session_id: str) -> TaskSessionRecord | None:
        path = self._session_path(session_id)
        with self._lock:
            if not path.exists():
                return None
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                return None
        if not isinstance(raw, dict):
            return None
        return TaskSessionRecord(
            session_id=str(raw.get("session_id") or session_id),
            kind=str(raw.get("kind") or "task"),
            title=str(raw.get("title") or ""),
            created_at=str(
                raw.get("created_at")
                or datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            ),
            percent=int(raw.get("percent", 0)),
            total_items=int(raw.get("total_items", 0)),
            processed_items=int(raw.get("processed_items", 0)),
            pending_paths=[str(p) for p in raw.get("pending_paths", [])],
            meta=dict(raw.get("meta", {})),
        )

    def list_sessions(self) -> list[TaskSessionRecord]:
        with self._lock:
            if not self.sessions_dir.exists():
                return []
            files = sorted(self.sessions_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        out: list[TaskSessionRecord] = []
        for f in files:
            rec = self.load_session(f.stem)
            if rec is not None:
                out.append(rec)
        return out

    def remove_session(self, session_id: str) -> bool:
        path = self._session_path(session_id)
        with self._lock:
            if not path.exists():
                return False
            try:
                path.unlink()
                return True
            except OSError:
                return False


class TaskController:
    """Thread-safe cooperative pause / pause-and-save / cancel controller."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._paused = False
        self._canceled = False
        self._save_requested = False

    def pause(self) -> None:
        with self._cond:
            self._paused = True
            self._cond.notify_all()

    def pause_and_save(self) -> None:
        with self._cond:
            self._save_requested = True
            self._paused = False
            self._cond.notify_all()

    def resume(self) -> None:
        with self._cond:
            self._paused = False
            self._cond.notify_all()

    def cancel(self) -> None:
        with self._cond:
            self._canceled = True
            self._paused = False
            self._cond.notify_all()

    @property
    def is_paused(self) -> bool:
        with self._cond:
            return self._paused

    @property
    def is_canceled(self) -> bool:
        with self._cond:
            return self._canceled

    @property
    def should_save_checkpoint(self) -> bool:
        with self._cond:
            return self._save_requested

    @property
    def status(self) -> str:
        with self._cond:
            if self._canceled:
                return "canceled"
            if self._save_requested:
                return "save_pause"
            if self._paused:
                return "paused"
            return "running"

    def checkpoint(self) -> Literal["ok", "cancel", "save_pause"]:
        with self._cond:
            while self._paused and not self._canceled and not self._save_requested:
                self._cond.wait(timeout=0.1)
            if self._canceled:
                return "cancel"
            if self._save_requested:
                return "save_pause"
            return "ok"


def stop_point(controller: TaskController | None, cancel_detail: str = "Task canceled") -> None:
    """Block while paused and raise TaskCanceledError / TaskSaveRequested when signaled."""
    if controller is None:
        return
    state = controller.checkpoint()
    if state == "cancel":
        raise TaskCanceledError(cancel_detail)
    if state == "save_pause":
        raise TaskSaveRequested()


class WorkerSignals(QObject):  # type: ignore[misc]
    progress = Signal(str, int, str)  # type: ignore[misc]
    finished = Signal(str, object)  # type: ignore[misc]
    paused_saved = Signal(str, str, int, str)  # type: ignore[misc]
    canceled = Signal(str, str)  # type: ignore[misc]
    error = Signal(str, str)  # type: ignore[misc]


class AsyncTask(QRunnable):  # type: ignore[misc]
    """Cancellable/pausable QRunnable with throttled progress, ETA, and Shiboken6 safety."""

    def __init__(
        self,
        fn: Callable[..., Any],
        *args: Any,
        task_id: str | None = None,
        controller: TaskController | None = None,
        use_com: bool = False,
        min_emit_interval_s: float = 0.10,
        **kwargs: Any,
    ) -> None:
        super().__init__()
        self.setAutoDelete(False)
        self.task_id = task_id or f"task_{uuid.uuid4().hex[:10]}"
        self.fn = fn
        self.args = args
        self.kwargs = dict(kwargs)
        self.controller = controller or TaskController()
        self.use_com = bool(use_com)
        self.min_emit_interval_s = float(min_emit_interval_s)
        self.signals = WorkerSignals()

        self._started_at: float | None = None
        self._last_emit_at = 0.0
        self._last_emit_pct = -1
        self._on_retire: Callable[[AsyncTask], None] | None = None

    def connect_queued(
        self,
        *,
        on_progress: Callable[[str, int, str], None] | None = None,
        on_finished: Callable[[str, Any], None] | None = None,
        on_paused_saved: Callable[[str, str, int, str], None] | None = None,
        on_canceled: Callable[[str, str], None] | None = None,
        on_error: Callable[[str, str], None] | None = None,
    ) -> None:
        conn = Qt.ConnectionType.QueuedConnection
        if on_progress is not None:
            self.signals.progress.connect(on_progress, conn)
        if on_finished is not None:
            self.signals.finished.connect(on_finished, conn)
        if on_paused_saved is not None:
            self.signals.paused_saved.connect(on_paused_saved, conn)
        if on_canceled is not None:
            self.signals.canceled.connect(on_canceled, conn)
        if on_error is not None:
            self.signals.error.connect(on_error, conn)

    def eta_label(self, percent: int | None = None) -> str:
        pct = self._last_emit_pct if percent is None else percent
        if self._started_at is None or pct <= 0 or pct >= 100:
            return "--:--"
        elapsed = max(0.0, time.monotonic() - self._started_at)
        remaining = elapsed * (100.0 - pct) / float(pct)
        return format_eta_seconds(remaining)

    def _emit_safe(self, sig_name: str, *payload: Any) -> None:
        if not qt_is_valid(self.signals):
            return
        try:
            getattr(self.signals, sig_name).emit(*payload)
        except RuntimeError:
            pass

    def _emit_progress(self, pct: int, stage: str = "") -> None:
        clamped = max(0, min(100, int(pct)))
        now = time.monotonic()
        if (
            self._last_emit_pct < 0
            or clamped >= 100
            or clamped > self._last_emit_pct and (now - self._last_emit_at) >= self.min_emit_interval_s
        ):
            self._last_emit_pct = clamped
            self._last_emit_at = now
            self._emit_safe("progress", self.task_id, clamped, str(stage))

    def _build_call_kwargs(self) -> dict[str, Any]:
        call_kwargs = dict(self.kwargs)
        try:
            sig = inspect.signature(self.fn)
            params = sig.parameters
            accepts_varkw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
            if ("progress_cb" in params or accepts_varkw) and "progress_cb" not in call_kwargs:
                call_kwargs["progress_cb"] = self._emit_progress
            if ("controller" in params or accepts_varkw) and "controller" not in call_kwargs:
                call_kwargs["controller"] = self.controller
        except (TypeError, ValueError):
            pass
        return call_kwargs

    def run(self) -> None:
        com_initialized = False
        if self.use_com and IS_WINDOWS:
            try:
                import ctypes

                hr = ctypes.windll.ole32.CoInitializeEx(None, 0x2 | 0x4)
                if hr in (0, 1):
                    com_initialized = True
            except Exception:
                pass

        self._started_at = time.monotonic()
        try:
            call_kwargs = self._build_call_kwargs()
            result = self.fn(*self.args, **call_kwargs)
            if self.controller.is_canceled:
                self._emit_safe("canceled", self.task_id, "Task canceled")
            else:
                self._emit_safe("finished", self.task_id, result)
        except TaskPausedAndSaved as exc:
            self._emit_safe("paused_saved", self.task_id, exc.session_id, exc.percent, exc.detail)
        except TaskSaveRequested as exc:
            self._emit_safe("paused_saved", self.task_id, "", max(0, self._last_emit_pct), exc.detail)
        except TaskCanceledError as exc:
            self._emit_safe("canceled", self.task_id, exc.detail)
        except Exception as exc:
            self._emit_safe("error", self.task_id, f"{type(exc).__name__}: {exc}")
        finally:
            if com_initialized and IS_WINDOWS:
                try:
                    import ctypes

                    ctypes.windll.ole32.CoUninitialize()
                except Exception:
                    pass
            if self._on_retire is not None:
                try:
                    self._on_retire(self)
                except Exception:
                    pass


class TaskManager(QObject):  # type: ignore[misc]
    """Coordinates active AsyncTask instances with Shiboken6-safe deferred retirement."""

    _retire_signal = Signal() if Signal is not None else None  # type: ignore[misc]

    def __init__(self, pool: QThreadPool | None = None, parent: QObject | None = None) -> None:
        if QObject is not object:
            super().__init__(parent)
        self._pool = pool or QThreadPool.globalInstance()
        self._lock = threading.RLock()
        self._active: dict[str, AsyncTask] = {}
        self._retired: list[AsyncTask] = []
        if Qt is not None and self._retire_signal is not None:
            self._retire_signal.connect(self._flush_retired, Qt.ConnectionType.QueuedConnection)

    def submit(
        self,
        task: AsyncTask,
        *,
        on_progress: Callable[[str, int, str], None] | None = None,
        on_finished: Callable[[str, Any], None] | None = None,
        on_paused_saved: Callable[[str, str, int, str], None] | None = None,
        on_canceled: Callable[[str, str], None] | None = None,
        on_error: Callable[[str, str], None] | None = None,
    ) -> AsyncTask:
        task.connect_queued(
            on_progress=on_progress,
            on_finished=on_finished,
            on_paused_saved=on_paused_saved,
            on_canceled=on_canceled,
            on_error=on_error,
        )
        task._on_retire = self._retire_task
        with self._lock:
            self._active[task.task_id] = task
        self._pool.start(task)
        return task

    def _retire_task(self, task: AsyncTask) -> None:
        with self._lock:
            self._active.pop(task.task_id, None)
            self._retired.append(task)
        has_app = QCoreApplication is not None and QCoreApplication.instance() is not None
        if has_app and self._retire_signal is not None and qt_is_valid(self):
            try:
                self._retire_signal.emit()
                return
            except RuntimeError:
                pass
        self._flush_retired()

    def _flush_retired(self) -> None:
        with self._lock:
            self._retired.clear()

    def active_tasks(self) -> list[str]:
        with self._lock:
            return list(self._active.keys())

    def cancel_all(self) -> None:
        with self._lock:
            tasks = list(self._active.values())
        for task in tasks:
            task.controller.cancel()

    def wait_for_done(self, timeout_ms: int = 5000) -> bool:
        return bool(self._pool.waitForDone(int(timeout_ms)))
