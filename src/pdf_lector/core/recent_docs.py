"""Local-only store for recently opened and pinned/favorite PDF and Markdown documents."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import json
from pathlib import Path

from .win32_bridge import atomic_write_text, default_app_data_dir


def _local_now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _norm_path_key(raw: str | Path) -> str:
    try:
        resolved = str(Path(raw).resolve())
    except OSError:
        resolved = str(Path(raw))
    return resolved.lower()


@dataclass
class RecentDocEntry:
    path: str
    name: str
    doc_type: str = "PDF"
    last_opened: str = ""
    pinned: bool = False
    page_count: int = 0

    def display_timestamp(self) -> str:
        """Return compact dd/mm/yyyy HH:mm local timestamp string."""
        raw = (self.last_opened or "").strip()
        if not raw:
            return ""
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return dt.astimezone().strftime("%d/%m/%Y %H:%M")
        except ValueError:
            return raw[:16]


class RecentDocumentsStore:
    """Persistent local recent-documents and pinned-favorites registry (zero telemetry)."""

    def __init__(self, store_path: Path | None = None, *, max_items: int = 24) -> None:
        self._path = Path(store_path) if store_path is not None else (default_app_data_dir() / "recent_docs.json")
        self._max_items = max(4, int(max_items))
        self._entries: list[RecentDocEntry] = self._load()

    @property
    def store_path(self) -> Path:
        return self._path

    def _load(self) -> list[RecentDocEntry]:
        if not self._path.exists():
            return []
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                return []
            out: list[RecentDocEntry] = []
            for item in raw:
                if not isinstance(item, dict):
                    continue
                p_str = str(item.get("path") or "").strip()
                if not p_str:
                    continue
                name = str(item.get("name") or Path(p_str).name)
                doc_type = str(item.get("doc_type") or ("MD" if Path(p_str).suffix.lower() in (".md", ".markdown") else "PDF"))
                out.append(
                    RecentDocEntry(
                        path=p_str,
                        name=name,
                        doc_type=doc_type,
                        last_opened=str(item.get("last_opened") or ""),
                        pinned=bool(item.get("pinned", False)),
                        page_count=max(0, int(item.get("page_count") or 0)),
                    )
                )
            return self._sorted(out)
        except Exception:
            return []

    def _sorted(self, items: list[RecentDocEntry]) -> list[RecentDocEntry]:
        return sorted(items, key=lambda e: (1 if e.pinned else 0, e.last_opened), reverse=True)

    def _save(self) -> None:
        self._entries = self._sorted(self._entries)
        # Keep all pinned entries plus up to max_items unpinned entries
        pinned = [e for e in self._entries if e.pinned]
        unpinned = [e for e in self._entries if not e.pinned][: self._max_items]
        self._entries = pinned + unpinned
        payload = [asdict(e) for e in self._entries]
        atomic_write_text(self._path, json.dumps(payload, indent=2, ensure_ascii=False))

    def list_entries(self, *, only_existing: bool = False) -> list[RecentDocEntry]:
        items = list(self._entries)
        if only_existing:
            items = [e for e in items if Path(e.path).exists()]
        return self._sorted(items)

    def record_open(self, doc_path: str | Path, *, page_count: int = 0) -> RecentDocEntry:
        try:
            resolved = Path(doc_path).resolve()
        except OSError:
            resolved = Path(doc_path)
        key = _norm_path_key(resolved)
        doc_type = "MD" if resolved.suffix.lower() in (".md", ".markdown") else "PDF"
        now_iso = _local_now_iso()

        pinned = False
        kept: list[RecentDocEntry] = []
        for entry in self._entries:
            if _norm_path_key(entry.path) == key:
                pinned = entry.pinned
                if page_count <= 0:
                    page_count = entry.page_count
            else:
                kept.append(entry)

        created = RecentDocEntry(
            path=str(resolved),
            name=resolved.name,
            doc_type=doc_type,
            last_opened=now_iso,
            pinned=pinned,
            page_count=max(0, int(page_count)),
        )
        self._entries = [created, *kept]
        self._save()
        return created

    def toggle_pin(self, doc_path: str | Path) -> bool:
        key = _norm_path_key(doc_path)
        for entry in self._entries:
            if _norm_path_key(entry.path) == key:
                entry.pinned = not entry.pinned
                self._save()
                return entry.pinned
        return False

    def is_pinned(self, doc_path: str | Path) -> bool:
        key = _norm_path_key(doc_path)
        for entry in self._entries:
            if _norm_path_key(entry.path) == key:
                return entry.pinned
        return False

    def remove(self, doc_path: str | Path) -> bool:
        key = _norm_path_key(doc_path)
        before = len(self._entries)
        self._entries = [e for e in self._entries if _norm_path_key(e.path) != key]
        if len(self._entries) != before:
            self._save()
            return True
        return False

    def clear_unpinned(self) -> int:
        before = len(self._entries)
        self._entries = [e for e in self._entries if e.pinned]
        removed = before - len(self._entries)
        if removed:
            self._save()
        return removed
