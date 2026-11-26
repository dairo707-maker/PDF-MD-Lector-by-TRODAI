"""Thread-safe in-app NotificationCenter with persistent and session-only notices."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import threading
from typing import Callable
import uuid

from .win32_bridge import atomic_write_text, default_app_data_dir

VALID_KINDS = (
    "COMPLETADO",
    "SUGERENCIA",
    "AVISO",
    "ERROR",
    "ACTUALIZACION",
    "DIAGNOSTICO",
    "SESION",
)
VALID_CATEGORIES = ("task", "session", "update", "diagnostic", "general")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass(slots=True)
class NotificationItem:
    notif_id: str
    timestamp: str
    kind: str
    title: str
    detail: str
    read: bool = False
    category: str = "general"
    persistent: bool = True
    action_tab: int | None = None
    action_id: str | None = None


class NotificationCenter:
    """Thread-safe in-app notifications backed by %LOCALAPPDATA%\\TRODAI\\PDFLector\\notifications.json."""

    def __init__(
        self,
        app_dir: Path | None = None,
        *,
        file_path: Path | None = None,
        max_items: int = 80,
    ) -> None:
        self.app_dir = Path(app_dir) if app_dir is not None else default_app_data_dir()
        self.file_path = Path(file_path) if file_path is not None else (self.app_dir / "notifications.json")
        self.max_items = max(1, int(max_items))
        self._lock = threading.RLock()
        self._items: list[NotificationItem] = []
        self._listeners: list[Callable[[], None]] = []
        self._load()

    def _load(self) -> None:
        if not self.file_path.exists():
            return
        try:
            raw = json.loads(self.file_path.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(raw, list):
            return
        loaded: list[NotificationItem] = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            try:
                loaded.append(
                    NotificationItem(
                        notif_id=str(entry.get("notif_id") or uuid.uuid4().hex[:12]),
                        timestamp=str(entry.get("timestamp") or _utc_now_iso()),
                        kind=str(entry.get("kind") or "AVISO"),
                        title=str(entry.get("title") or ""),
                        detail=str(entry.get("detail") or ""),
                        read=bool(entry.get("read", False)),
                        category=str(entry.get("category") or "general"),
                        persistent=True,
                        action_tab=entry.get("action_tab"),
                        action_id=entry.get("action_id"),
                    )
                )
            except Exception:
                continue
        with self._lock:
            self._items = loaded[: self.max_items]

    def _save_locked(self) -> None:
        persisted = [asdict(item) for item in self._items if item.persistent]
        atomic_write_text(
            self.file_path,
            json.dumps(persisted[: self.max_items], indent=2, ensure_ascii=False),
        )

    def _notify_listeners(self) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for cb in listeners:
            try:
                cb()
            except Exception:
                pass

    def add_listener(self, cb: Callable[[], None]) -> None:
        with self._lock:
            self._listeners.append(cb)

    def list_items(
        self,
        *,
        category: str | None = None,
        unread_only: bool = False,
    ) -> list[NotificationItem]:
        with self._lock:
            items = list(self._items)
        if category is not None:
            items = [i for i in items if i.category == category]
        if unread_only:
            items = [i for i in items if not i.read]
        return items

    def unread_count(self) -> int:
        with self._lock:
            return sum(1 for item in self._items if not item.read)

    def add(
        self,
        kind: str,
        title: str,
        detail: str,
        *,
        category: str = "general",
        persistent: bool = True,
        action_tab: int | None = None,
        action_id: str | None = None,
    ) -> NotificationItem:
        clean_kind = kind if kind in VALID_KINDS else "AVISO"
        clean_cat = category if category in VALID_CATEGORIES else "general"
        item = NotificationItem(
            notif_id=uuid.uuid4().hex[:12],
            timestamp=_utc_now_iso(),
            kind=clean_kind,
            title=str(title),
            detail=str(detail),
            read=False,
            category=clean_cat,
            persistent=bool(persistent),
            action_tab=action_tab,
            action_id=action_id,
        )
        with self._lock:
            self._items.insert(0, item)
            evicted_persistent = False
            if len(self._items) > self.max_items:
                evicted = self._items[self.max_items :]
                evicted_persistent = any(old.persistent for old in evicted)
                self._items = self._items[: self.max_items]
            if item.persistent or evicted_persistent:
                self._save_locked()
        self._notify_listeners()
        return item

    def mark_read(self, notif_id: str) -> bool:
        changed = False
        was_persistent = False
        with self._lock:
            for item in self._items:
                if item.notif_id == notif_id:
                    if not item.read:
                        item.read = True
                        changed = True
                        was_persistent = item.persistent
                    break
            if changed and was_persistent:
                self._save_locked()
        if changed:
            self._notify_listeners()
        return changed

    def mark_all_read(self) -> None:
        changed = False
        with self._lock:
            for item in self._items:
                if not item.read:
                    item.read = True
                    changed = True
            if changed:
                self._save_locked()
        if changed:
            self._notify_listeners()

    def remove(self, notif_id: str) -> bool:
        removed_item: NotificationItem | None = None
        with self._lock:
            for idx, item in enumerate(self._items):
                if item.notif_id == notif_id:
                    removed_item = self._items.pop(idx)
                    break
            if removed_item is not None and removed_item.persistent:
                self._save_locked()
        if removed_item is not None:
            self._notify_listeners()
            return True
        return False

    def clear_all(self, *, include_persistent: bool = True) -> None:
        with self._lock:
            if include_persistent:
                self._items.clear()
                self._save_locked()
            else:
                self._items = [item for item in self._items if item.persistent]
        self._notify_listeners()
