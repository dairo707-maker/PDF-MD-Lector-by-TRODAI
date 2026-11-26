"""Local persistent user bookmarks store per PDF document."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import uuid


def _default_bookmarks_path() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    base = Path(local_app_data) if local_app_data else Path.home() / ".config"
    return base / "TRODAI" / "PDFLector" / "bookmarks.json"


def _doc_key(pdf_path: Path) -> str:
    try:
        resolved = str(Path(pdf_path).resolve())
    except OSError:
        resolved = str(pdf_path)
    return resolved.lower() if os.name == "nt" else resolved


def _atomic_write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
        os.replace(tmp_name, path)
    finally:
        tmp = Path(tmp_name)
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


@dataclass(frozen=True)
class BookmarkEntry:
    bookmark_id: str
    title: str
    page_index: int
    created_at: str


class BookmarksStore:
    """Stores custom user PDF bookmarks safely on local disk."""

    def __init__(self, store_path: Path | None = None) -> None:
        self._path = Path(store_path) if store_path else _default_bookmarks_path()

    def _read_all(self) -> dict[str, list[dict[str, object]]]:
        if not self._path.exists():
            return {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else {}
        except Exception:
            return {}

    def list_bookmarks(self, pdf_path: Path) -> list[BookmarkEntry]:
        key = _doc_key(pdf_path)
        items = self._read_all().get(key, [])
        entries: list[BookmarkEntry] = []
        for raw in items:
            try:
                entries.append(
                    BookmarkEntry(
                        bookmark_id=str(raw["bookmark_id"]),
                        title=str(raw["title"]),
                        page_index=max(0, int(raw["page_index"])),
                        created_at=str(raw.get("created_at", "")),
                    )
                )
            except Exception:
                continue
        entries.sort(key=lambda b: (b.page_index, b.created_at))
        return entries

    def add_bookmark(self, pdf_path: Path, title: str, page_index: int) -> BookmarkEntry:
        clean_title = (title or "").strip()[:80] or f"Pagina {page_index + 1}"
        entry = BookmarkEntry(
            bookmark_id=uuid.uuid4().hex[:10],
            title=clean_title,
            page_index=max(0, int(page_index)),
            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        data = self._read_all()
        key = _doc_key(pdf_path)
        bucket = list(data.get(key, []))
        bucket.append(asdict(entry))
        data[key] = bucket
        _atomic_write_json(self._path, data)
        return entry

    def remove_bookmark(self, pdf_path: Path, bookmark_id: str) -> bool:
        data = self._read_all()
        key = _doc_key(pdf_path)
        bucket = data.get(key, [])
        filtered = [item for item in bucket if str(item.get("bookmark_id")) != bookmark_id]
        if len(filtered) == len(bucket):
            return False
        if filtered:
            data[key] = filtered
        else:
            data.pop(key, None)
        _atomic_write_json(self._path, data)
        return True
