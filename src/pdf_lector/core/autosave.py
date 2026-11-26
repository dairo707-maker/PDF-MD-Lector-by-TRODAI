"""Atomic session autosave & crash recovery for open PDF/Markdown tabs and unsaved edits."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from typing import Literal

from .path_guard import validate_local_any_doc
from .pdf_annotations import AnnotationRecord, sanitize_color_hex, sanitize_note_category


def _default_autosave_dir() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    base = Path(local_app_data) if local_app_data else Path.home() / ".config"
    return base / "TRODAI" / "PDFLector" / "autosave"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def serialize_annotation(rec: AnnotationRecord) -> dict[str, object]:
    """Convert AnnotationRecord to JSON-serializable dict."""
    pts = [
        [[float(coord) for coord in pt] for pt in stroke]
        for stroke in (rec.points_pdf or ())
    ]
    return {
        "id": str(rec.id),
        "page_index": int(rec.page_index),
        "kind": str(rec.kind),
        "rect_pdf": [float(v) for v in rec.rect_pdf[:4]],
        "points_pdf": pts,
        "color_hex": sanitize_color_hex(rec.color_hex),
        "author": str(rec.author),
        "timestamp_iso": str(rec.timestamp_iso),
        "content": str(rec.content),
        "parent_id": rec.parent_id,
        "stroke_width": float(rec.stroke_width),
        "opacity": float(rec.opacity),
        "category": sanitize_note_category(getattr(rec, "category", "General")),
    }


def deserialize_annotation(raw: dict[str, object]) -> AnnotationRecord:
    """Restore AnnotationRecord from persisted autosave dictionary."""
    rect_vals = [float(v) for v in (raw.get("rect_pdf") or [0.0, 0.0, 24.0, 24.0])][:4]
    while len(rect_vals) < 4:
        rect_vals.append(0.0)
    pts_raw = raw.get("points_pdf") or ()
    points_pdf = [
        [tuple(float(c) for c in pt) for pt in stroke]
        for stroke in pts_raw  # type: ignore[union-attr]
        if isinstance(stroke, list)
    ]
    kind = str(raw.get("kind") or "text")
    if kind not in ("highlight", "text", "ink", "freetext", "popup"):
        kind = "text"
    return AnnotationRecord(
        id=str(raw.get("id") or "note-rec"),
        page_index=int(raw.get("page_index") or 0),
        kind=kind,  # type: ignore[arg-type]
        rect_pdf=(rect_vals[0], rect_vals[1], rect_vals[2], rect_vals[3]),
        points_pdf=points_pdf if kind == "ink" else (),
        color_hex=sanitize_color_hex(str(raw.get("color_hex") or "")),
        author=str(raw.get("author") or "Anonimo"),
        timestamp_iso=str(raw.get("timestamp_iso") or ""),
        content=str(raw.get("content") or ""),
        parent_id=str(raw["parent_id"]) if raw.get("parent_id") else None,
        stroke_width=float(raw.get("stroke_width") or 2.0),
        opacity=float(raw.get("opacity") or 0.45),
        category=sanitize_note_category(str(raw.get("category") or "General")),
    )


@dataclass
class AutosaveTabState:
    doc_path: str
    doc_type: Literal["pdf", "md"]
    page_index: int = 0
    zoom: float = 1.0
    rotation: int = 0
    scroll_y: int = 0
    dirty: bool = False
    annotations: list[dict[str, object]] = field(default_factory=list)
    markdown_text: str | None = None


@dataclass
class SessionSnapshot:
    version: int = 1
    saved_at_iso: str = ""
    clean_exit: bool = False
    active_index: int = 0
    tabs: list[AutosaveTabState] = field(default_factory=list)


class SessionAutosaveManager:
    """Atomic JSON snapshot persistence for session tabs and unsaved edits."""

    def __init__(self, base_dir: Path | None = None) -> None:
        self.base_dir = Path(base_dir) if base_dir else _default_autosave_dir()
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.snapshot_path = self.base_dir / "session_snapshot.json"

    def save_snapshot(self, snapshot: SessionSnapshot) -> Path:
        if not snapshot.saved_at_iso:
            snapshot.saved_at_iso = _utc_now_iso()
        payload = asdict(snapshot)
        fd, tmp_name = tempfile.mkstemp(
            prefix=".autosave_", suffix=".json.tmp", dir=str(self.base_dir)
        )
        os.close(fd)
        tmp_path = Path(tmp_name)
        try:
            tmp_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp_path, self.snapshot_path)
        finally:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
        return self.snapshot_path

    def load_snapshot(self) -> SessionSnapshot | None:
        if not self.snapshot_path.exists():
            return None
        try:
            raw = json.loads(self.snapshot_path.read_text(encoding="utf-8-sig"))
        except Exception:
            return None
        if not isinstance(raw, dict):
            return None
        tabs: list[AutosaveTabState] = []
        for item in raw.get("tabs") or []:
            if not isinstance(item, dict):
                continue
            path_str = str(item.get("doc_path") or "").strip()
            ok, _ = validate_local_any_doc(path_str, check_exists=True)
            if not ok:
                continue
            dt = "md" if str(item.get("doc_type")) == "md" else "pdf"
            tabs.append(
                AutosaveTabState(
                    doc_path=path_str,
                    doc_type=dt,
                    page_index=max(0, int(item.get("page_index") or 0)),
                    zoom=max(0.4, min(3.0, float(item.get("zoom") or 1.0))),
                    rotation=int(item.get("rotation") or 0) % 360,
                    scroll_y=max(0, int(item.get("scroll_y") or 0)),
                    dirty=bool(item.get("dirty", False)),
                    annotations=[
                        a for a in (item.get("annotations") or []) if isinstance(a, dict)
                    ],
                    markdown_text=(
                        str(item["markdown_text"])
                        if item.get("markdown_text") is not None
                        else None
                    ),
                )
            )
        return SessionSnapshot(
            version=int(raw.get("version") or 1),
            saved_at_iso=str(raw.get("saved_at_iso") or ""),
            clean_exit=bool(raw.get("clean_exit", False)),
            active_index=max(0, int(raw.get("active_index") or 0)),
            tabs=tabs,
        )

    def mark_clean_exit(self) -> None:
        snap = self.load_snapshot()
        if snap is None:
            snap = SessionSnapshot(clean_exit=True)
        else:
            snap.clean_exit = True
            snap.saved_at_iso = _utc_now_iso()
        self.save_snapshot(snap)

    def should_offer_recovery(self, *, previous_crashed: bool = False) -> bool:
        snap = self.load_snapshot()
        if snap is None or not snap.tabs:
            return False
        if previous_crashed or (not snap.clean_exit):
            return True
        return False

    def clear_snapshot(self) -> None:
        if self.snapshot_path.exists():
            try:
                self.snapshot_path.unlink()
            except OSError:
                pass
